from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import sqlite3, json, smtplib, threading, time, requests, random, csv, io, re, os, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.header import Header
from datetime import datetime
import anthropic
from security import (
    audit_smtp_injection,
    validate_tls,
    audit_sender_dns,
    check_rate_limit,
    get_rate_stats,
    get_rate_status_readonly,
    detect_header_injection,
    check_domain_blacklist,
    smtp_sanitize,
)
from apscheduler.schedulers.background import BackgroundScheduler
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── FLASK APP ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

scheduler = BackgroundScheduler(daemon=True)
scheduler.start()

# ── PARALLEL BATCH TRACKER ────────────────────────────────────────────────────
BATCH_STATUS = {}
_batch_lock = threading.Lock()

def new_batch(batch_id, total):
    with _batch_lock:
        BATCH_STATUS[batch_id] = {"total": total, "done": 0, "failed": 0, "status": "running"}

def update_batch(batch_id, success=True):
    with _batch_lock:
        if batch_id in BATCH_STATUS:
            if success:
                BATCH_STATUS[batch_id]["done"] += 1
            else:
                BATCH_STATUS[batch_id]["failed"] += 1
            b = BATCH_STATUS[batch_id]
            if b["done"] + b["failed"] >= b["total"]:
                b["status"] = "complete"

def sanitize(text: str) -> str:
    """Ensure text is safe UTF-8 and strip problematic invisible chars."""
    if not text:
        return ""
    for ch in ["\u200b", "\u200c", "\u200d", "\ufeff"]:
        text = text.replace(ch, "")
    return text.encode("utf-8", "ignore").decode("utf-8").strip()

@app.route("/api/batch-status", methods=["GET"])
def batch_status():
    return jsonify(BATCH_STATUS)

@app.route("/")
def index():
    return send_file("outreachos-dashboard.html", mimetype="text/html; charset=utf-8")

DB = os.environ.get("DATABASE_URL", "agency.db")

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, company TEXT, email TEXT,
            status TEXT DEFAULT 'active',
            monthly_fee REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER, name TEXT, target_industry TEXT,
            target_role TEXT, value_prop TEXT,
            email_prompt TEXT,
            status TEXT DEFAULT 'draft',
            campaign_type TEXT DEFAULT 'b2b',
            emails_sent INTEGER DEFAULT 0,
            replies INTEGER DEFAULT 0,
            calls_booked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(client_id) REFERENCES clients(id)
        );
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER, first_name TEXT, last_name TEXT,
            email TEXT, company TEXT, role TEXT, linkedin TEXT,
            website TEXT, status TEXT DEFAULT 'new',
            email_body TEXT, personalization_notes TEXT,
            sent_at TEXT, replied_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
        );
        CREATE TABLE IF NOT EXISTS smtp_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host TEXT, port INTEGER, username TEXT,
            password TEXT, from_name TEXT, active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT, message TEXT, meta TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS user_context (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bio TEXT,
            projects TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS scrape_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            source TEXT DEFAULT 'campaign_scraper',
            lead_count INTEGER DEFAULT 0,
            leads_json TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    # Migration: add email_prompt and attachment_path
    try:
        conn.execute("ALTER TABLE campaigns ADD COLUMN email_prompt TEXT")
    except: pass
    try:
        conn.execute("ALTER TABLE campaigns ADD COLUMN attachment_path TEXT")
    except: pass
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN custom_data TEXT")
    except: pass
    try:
        conn.execute("ALTER TABLE campaigns ADD COLUMN system_prompt TEXT")
    except: pass
    try:
        conn.execute("ALTER TABLE scrape_history ADD COLUMN source TEXT")
    except: pass
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN opened_at TEXT")
    except: pass
    conn.commit()
    conn.close()

def log_activity(type_, msg, meta=None):
    conn = get_db()
    conn.execute("INSERT INTO activity_log(type,message,meta) VALUES(?,?,?)",
                 (type_, msg, json.dumps(meta) if meta else None))
    conn.commit()
    conn.close()

# ── CONTEXT CACHE ──
CONTEXT_CACHE = {"data": None, "last_updated": 0}

def get_cached_context():
    global CONTEXT_CACHE
    now = time.time()
    # 10 days in seconds = 864,000
    if CONTEXT_CACHE["data"] is None or (now - CONTEXT_CACHE["last_updated"]) > 864000:
        conn = get_db()
        row = conn.execute("SELECT * FROM user_context ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        if row:
            CONTEXT_CACHE["data"] = {"bio": row["bio"], "projects": row["projects"]}
        else:
            CONTEXT_CACHE["data"] = {"bio": "", "projects": ""}
        CONTEXT_CACHE["last_updated"] = now
    return CONTEXT_CACHE["data"]

@app.route("/api/user-context", methods=["GET"])
def get_user_context():
    return jsonify(get_cached_context())

@app.route("/api/user-context", methods=["POST"])
def update_user_context():
    global CONTEXT_CACHE
    d = request.json or {}
    conn = get_db()
    conn.execute("INSERT INTO user_context(bio, projects) VALUES(?,?)", (d.get("bio"), d.get("projects")))
    conn.commit()
    conn.close()
    # Force cache refresh
    CONTEXT_CACHE["data"] = {"bio": d.get("bio"), "projects": d.get("projects")}
    CONTEXT_CACHE["last_updated"] = time.time()
    return jsonify({"status": "ok"})


@app.route("/api/clients", methods=["GET"])
def get_clients():
    conn = get_db()
    rows = conn.execute("""
        SELECT c.*, 
            COUNT(DISTINCT camp.id) as campaign_count,
            COALESCE(SUM(camp.emails_sent),0) as total_emails,
            COALESCE(SUM(camp.calls_booked),0) as total_calls
        FROM clients c
        LEFT JOIN campaigns camp ON camp.client_id = c.id
        GROUP BY c.id ORDER BY c.created_at DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/clients", methods=["POST"])
def create_client():
    d = request.json or {}
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO clients(name,company,email,monthly_fee) VALUES(?,?,?,?)",
        (d.get("name", ""), d.get("company", ""), d.get("email", ""), d.get("monthly_fee", 0))
    )
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    log_activity("client", f"New client added: {d['name']} @ {d['company']}")
    return jsonify({"id": cid, "status": "ok"})

@app.route("/api/clients/<int:cid>", methods=["DELETE"])
def delete_client(cid):
    conn = get_db()
    conn.execute("DELETE FROM clients WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

# ── CAMPAIGNS ────────────────────────────────────────────────────────────────

@app.route("/api/campaigns", methods=["GET"])
def get_campaigns():
    client_id = request.args.get("client_id")
    conn = get_db()
    query = """
        SELECT camp.*, c.name as client_name, c.company as client_company,
               COUNT(l.id) as lead_count
        FROM campaigns camp
        JOIN clients c ON c.id = camp.client_id
        LEFT JOIN leads l ON l.campaign_id = camp.id
    """
    params = ()
    if client_id:
        query += " WHERE camp.client_id = ?"
        params = (client_id,)
    query += " GROUP BY camp.id ORDER BY camp.created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/campaigns", methods=["POST"])
def create_campaign():
    d = request.json or {}
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO campaigns(client_id,name,target_industry,target_role,value_prop,email_prompt,campaign_type) VALUES(?,?,?,?,?,?,?)",
        (d.get("client_id"), d.get("name", ""), d.get("target_industry", ""), d.get("target_role", ""), d.get("value_prop", ""), d.get("email_prompt"), d.get("campaign_type", "b2b"))
    )
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    log_activity("campaign", f"Campaign created: {d['name']}")
    return jsonify({"id": cid, "status": "ok"})

@app.route("/api/campaigns/<int:cid>", methods=["DELETE"])
def delete_campaign(cid):
    conn = get_db()
    conn.execute("DELETE FROM leads WHERE campaign_id=?", (cid,))
    conn.execute("DELETE FROM campaigns WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/campaigns/<int:cid>/prompt", methods=["PATCH"])
def update_campaign_prompt(cid):
    d = request.json or {}
    conn = get_db()
    conn.execute("UPDATE campaigns SET email_prompt=?, system_prompt=? WHERE id=?", (d.get("email_prompt"), d.get("system_prompt"), cid))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

# ── LEADS ────────────────────────────────────────────────────────────────────

@app.route("/api/leads", methods=["GET"])
def get_leads():
    cid = request.args.get("campaign_id")
    conn = get_db()
    query = "SELECT * FROM leads"
    params = ()
    if cid:
        query += " WHERE campaign_id = ?"
        params = (cid,)
    query += " ORDER BY status, created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/leads", methods=["DELETE"])
def delete_all_leads():
    cid = request.args.get("campaign_id")
    conn = get_db()
    if cid:
        conn.execute("DELETE FROM leads WHERE campaign_id=?", (cid,))
    else:
        conn.execute("DELETE FROM leads")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/leads/clean", methods=["POST"])
def clean_leads():
    d = request.get_json(silent=True) or {}
    cid = d.get("campaign_id") or request.args.get("campaign_id")
    conn = get_db()
    deleted = 0
    if cid:
        cursor = conn.execute("""
            DELETE FROM leads 
            WHERE campaign_id=? AND (status IN ('sent', 'replied', 'booked') OR email IS NULL OR trim(email) = '')
        """, (cid,))
        deleted += cursor.rowcount
        
        # Deduplicate emails within the campaign (keep the first one)
        cursor2 = conn.execute("""
            DELETE FROM leads
            WHERE campaign_id=? AND id NOT IN (
                SELECT MIN(id) FROM leads WHERE campaign_id=? GROUP BY lower(trim(email))
            ) AND email IS NOT NULL AND trim(email) != ''
        """, (cid, cid))
        deleted += cursor2.rowcount
    else:
        cursor = conn.execute("""
            DELETE FROM leads 
            WHERE status IN ('sent', 'replied', 'booked') OR email IS NULL OR trim(email) = ''
        """)
        deleted += cursor.rowcount
        
        # Deduplicate emails globally
        cursor2 = conn.execute("""
            DELETE FROM leads
            WHERE id NOT IN (
                SELECT MIN(id) FROM leads GROUP BY lower(trim(email))
            ) AND email IS NOT NULL AND trim(email) != ''
        """)
        deleted += cursor2.rowcount
        
    conn.commit()
    conn.close()
    log_activity("leads", f"Cleaned leads: removed {deleted} leads (duplicates, sent, or missing email)" + (f" for campaign {cid}" if cid else ""))
    return jsonify({"status": "ok", "deleted": deleted})


@app.route("/api/leads/upload", methods=["POST"])
def upload_leads():
    cid = request.form.get("campaign_id")
    file = request.files.get("file")
    if not file or not cid:
        return jsonify({"error": "Missing file or campaign_id"}), 400
    content = file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    conn = get_db()
    
    # Query all emails in the database to prevent duplicates globally
    existing_emails = {row['email'].strip().lower() for row in conn.execute("SELECT email FROM leads WHERE email IS NOT NULL AND email != ''").fetchall()}
    
    count = 0
    skipped = 0
    for raw_row in reader:
        # Normalize keys to lowercase and strip whitespace
        row = {str(k).strip().lower(): v for k, v in raw_row.items() if k}
        
        # Map user's specific columns to our database fields
        company = row.get("company name", row.get("company", ""))
        website = row.get("website", "")
        linkedin = row.get("linkedin", "")
        role = row.get("focus area", row.get("role", ""))
        
        email = row.get("email", "").strip()
        first_name = row.get("first_name", "")
        
        # Check for dynamic matches for email and name
        for k in row.keys():
            if "email" in k and not email:
                email = row[k].strip()
            elif "name" in k and "company" not in k and not first_name:
                first_name = row[k]
            elif "direct rec" in k and "email" not in k and not first_name:
                first_name = row[k]  # Using direct recruiter as name
        
        if email and email.lower() in existing_emails:
            skipped += 1
            continue
                
        conn.execute("""
            INSERT INTO leads(campaign_id,first_name,last_name,email,company,role,linkedin,website,custom_data)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (cid, first_name, row.get("last_name",""),
              email, company, role, linkedin, website, json.dumps(raw_row)))
        count += 1
        if email:
            existing_emails.add(email.lower())
    conn.commit()
    conn.close()
    log_activity("leads", f"Uploaded {count} leads to campaign {cid} (skipped {skipped} duplicates)")
    return jsonify({"imported": count, "skipped": skipped, "status": "ok"})


@app.route("/api/leads", methods=["POST"])
@app.route("/api/leads/manual", methods=["POST"])
def create_lead():
    """Create a single manual lead. `campaign_id` is required; all other fields are optional."""
    d = request.json or {}
    cid = d.get("campaign_id")
    if not cid:
        return jsonify({"error": "campaign_id required"}), 400

    first_name = d.get("first_name", "")
    last_name = d.get("last_name", "")
    email = d.get("email", "")
    company = d.get("company", "")
    role = d.get("role", "")
    linkedin = d.get("linkedin", "")
    website = d.get("website", "")
    custom = d.get("custom_data")

    try:
        custom_json = json.dumps(custom) if custom is not None else None
    except Exception:
        custom_json = None

    conn = get_db()
    cur = conn.execute("""
        INSERT INTO leads(campaign_id,first_name,last_name,email,company,role,linkedin,website,custom_data)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, (cid, first_name, last_name, email, company, role, linkedin, website, custom_json))
    conn.commit()
    lid = cur.lastrowid
    conn.close()

    log_activity("leads", f"Manual lead added to campaign {cid}: {first_name} {last_name} <{email}>")
    return jsonify({"id": lid, "status": "ok"})

@app.route("/api/leads/<int:lid>/status", methods=["PATCH"])
def update_lead_status(lid):
    d = request.json
    conn = get_db()
    conn.execute("UPDATE leads SET status=? WHERE id=?", (d["status"], lid))
    if d["status"] == "replied":
        conn.execute("UPDATE leads SET replied_at=datetime('now') WHERE id=?", (lid,))
        lead = conn.execute("SELECT campaign_id FROM leads WHERE id=?", (lid,)).fetchone()
        if lead:
            conn.execute("UPDATE campaigns SET replies=replies+1 WHERE id=?", (lead["campaign_id"],))
    if d["status"] == "booked":
        lead = conn.execute("SELECT campaign_id FROM leads WHERE id=?", (lid,)).fetchone()
        if lead:
            conn.execute("UPDATE campaigns SET calls_booked=calls_booked+1 WHERE id=?", (lead["campaign_id"],))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/track/open/<int:lid>", methods=["GET"])
def track_email_open(lid):
    conn = get_db()
    lead = conn.execute("SELECT status, opened_at FROM leads WHERE id=?", (lid,)).fetchone()
    should_log = False
    if lead:
        conn.execute("UPDATE leads SET opened_at=datetime('now') WHERE id=?", (lid,))
        if lead["status"] == "sent":
            conn.execute("UPDATE leads SET status='opened' WHERE id=?", (lid,))
            should_log = True
        conn.commit()
    conn.close()
    
    if should_log:
        log_activity("leads", f"Lead #{lid} opened the email")
    
    # 1x1 transparent GIF data
    pixel_data = b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x4c\x01\x00\x3b'
    return Response(pixel_data, mimetype='image/gif', headers={
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        'Expires': '0'
    })

# ── AI EMAIL GENERATION ───────────────────────────────────────────────────────

# ── CONFIGURATION: AI PROVIDER ──
PROVIDERS = [
    {
        "name": "NVIDIA NIM",
        "url": "https://integrate.api.nvidia.com/v1",
        "key": os.environ.get("NVIDIA_API_KEY"),
        "model": "nvidia/nemotron-3-super-120b-a12b"
    }
]

@app.route("/api/generate-emails", methods=["POST"])
def generate_emails():
    d = request.json or {}
    campaign_id = d.get("campaign_id")
    if not campaign_id:
        return jsonify({"error": "campaign_id is required"}), 400
    lead_id = d.get("lead_id")
    lead_ids = d.get("lead_ids")
    mode = d.get("mode", "new")
    force_reg = d.get("force_regenerate", False) # fallback for old clients

    conn = get_db()
    campaign_row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not campaign_row:
        conn.close()
        return jsonify({"error": "No campaign found"}), 404
    campaign = dict(campaign_row)

    if lead_ids:
        placeholders = ','.join('?' for _ in lead_ids)
        leads_rows = conn.execute(
            f"SELECT * FROM leads WHERE id IN ({placeholders})", tuple(lead_ids)
        ).fetchall()
    elif lead_id:
        leads_rows = conn.execute(
            "SELECT * FROM leads WHERE id=?", (lead_id,)
        ).fetchall()
    elif mode == "all":
        leads_rows = conn.execute(
            "SELECT * FROM leads WHERE campaign_id=? ORDER BY status, created_at DESC", (campaign_id,)
        ).fetchall()
    elif mode == "unsent" or force_reg:
        leads_rows = conn.execute(
            "SELECT * FROM leads WHERE campaign_id=? AND status IN ('new', 'ready') ORDER BY status, created_at DESC", (campaign_id,)
        ).fetchall()
    elif mode == "sent":
        leads_rows = conn.execute(
            "SELECT * FROM leads WHERE campaign_id=? AND status NOT IN ('new', 'ready') ORDER BY status, created_at DESC", (campaign_id,)
        ).fetchall()
    else: # "new"
        leads_rows = conn.execute(
            "SELECT * FROM leads WHERE campaign_id=? AND status='new' AND (email_body IS NULL OR email_body = '') ORDER BY status, created_at DESC", (campaign_id,)
        ).fetchall()
    leads = [dict(r) for r in leads_rows]
    conn.close()

    start_idx = d.get("start_index")
    end_idx = d.get("end_index")
    if start_idx is not None and end_idx is not None:
        try:
            s = max(0, int(start_idx) - 1)
            e = int(end_idx)
            leads = leads[s:e]
        except ValueError:
            pass

    if not leads:
        return jsonify({"error": "No leads found for the selected mode"}), 404

    log_activity("ai", f"Starting generation for {len(leads)} leads with fallback support")

    def generate_batch():
        user_info = get_cached_context()
        batch_id = f"gen_{campaign_id}_{int(time.time())}"
        new_batch(batch_id, len(leads))
        log_activity("ai", f"Starting PARALLEL generation for {len(leads)} leads | batch: {batch_id}")

        def process_lead(lead):
            """Generate email for one lead. Returns True on success."""
            success = False
            for provider in PROVIDERS:
                try:
                    # Use strictly the provider's key
                    current_key = provider["key"]
                    if not current_key or "YOUR_" in current_key:
                        continue # Skip providers without valid keys

                    # Construct Prompt
                    base_prompt = campaign.get('email_prompt')
                    if not base_prompt:
                        base_prompt = "Write a personalized cold email for {first_name} at {company}."
                    
                    class SafeDict(dict):
                        def __missing__(self, key):
                            return f"[{key.replace('_',' ')} not provided]"
                            
                    format_vars = SafeDict({
                        'first_name': lead.get('first_name') or 'Hiring Team',
                        'last_name': lead.get('last_name') or '',
                        'company': lead.get('company') or 'your team',
                        'role': lead.get('role') or 'the team',
                        'target_industry': campaign.get('target_industry') or '',
                        'target_role': campaign.get('target_role') or '',
                        'value_prop': campaign.get('value_prop') or '',
                        'linkedin': lead.get('linkedin') or '',
                        'website': lead.get('website') or '',
                        'website_context': f"your recent projects and work at {lead.get('company') or 'your company'}"
                    })
                    
                    custom_data_str = lead.get("custom_data")
                    custom_data = {}
                    if custom_data_str:
                        try:
                            custom_data = json.loads(custom_data_str)
                        except: pass
                    
                    for k, v in custom_data.items():
                        # Let user use raw headers as template vars (e.g., {Company Name})
                        format_vars[k] = v
                        # Support common casing variations automatically
                        kl = k.lower().replace(' ', '_')
                        format_vars[kl] = v
                        format_vars[k.title()] = v
                        format_vars[k.replace('_', ' ').title()] = v
                        
                    try:
                        formatted_prompt = base_prompt.format_map(format_vars)
                    except Exception as e:
                        formatted_prompt = f"Instructions:\n{base_prompt}\n\nTarget: {lead.get('first_name') or 'Hiring Team'} at {lead.get('company') or 'your company'}"

                    # Strip legacy instructions from DB prompt to avoid confusing the AI
                    formatted_prompt = re.sub(r"Format your response EXACTLY as:.*", "", formatted_prompt, flags=re.IGNORECASE | re.DOTALL)
                    formatted_prompt = formatted_prompt.strip()

                    custom_fields_text = "\n".join([f"- {k}: {v}" for k, v in custom_data.items() if v])

                    prompt = f"""{formatted_prompt}

PROSPECT RAW DATA (From CSV):
{custom_fields_text}

-----------------------
MY DETAILS (Use these if needed for context or signature):
{user_info['bio']}

MY PROJECTS:
{user_info['projects']}
-----------------------
"""

                    payload = {
                        "model": provider["model"],
                        "messages": [
                            {"role": "system", "content": """You are an expert cold email copywriter.

REQUIRED OUTPUT FORMAT:
<email>
<subject>Write the actual subject line here</subject>
<body>
Write the actual email body here
</body>
</email>

RULES:
- You MUST wrap your final email in the exact XML tags above.
- NEVER write the words "SUBJECT:" or "BODY:" inside the tags. Just write the text.
- NO conversational text or explanations outside the tags.
- NO bolding or asterisks (**). 
- NO curly or square brackets.
- If data is missing, use a professional fallback like 'Hiring Team'."""},
                            {"role": "user", "content": f"Using the following instructions and data, write the final email. DO NOT output literal dots (...). You must output the actual generated email wrapped inside the <email> XML tags as instructed.\n\nINSTRUCTIONS:\n{formatted_prompt}"}
                        ],
                        "temperature": 0.6,
                        "top_p": 0.95,
                        "max_tokens": 1000,
                        "chat_template_kwargs": {"enable_thinking": False}
                    }
                    
                    headers = {
                        "Authorization": f"Bearer {current_key}",
                        "Content-Type": "application/json"
                    }

                    url = provider["url"]
                    if "/chat/completions" not in url:
                        url = f"{url.rstrip('/')}/chat/completions"

                    log_activity("ai", f"Calling {url} | model={provider['model']} | key_prefix={current_key[:12]}")
                    resp = requests.post(url, headers=headers, json=payload, timeout=180)

                    if resp.status_code in [429, 500, 502, 503]:
                        log_activity("ai", f"{provider['name']} busy/limit hit ({resp.status_code}). Retrying next provider...")
                        continue

                    if not resp.ok:
                        log_activity("error", f"API Error ({resp.status_code}): {resp.text[:500]}")
                        continue

                    data = resp.json()
                    message = data["choices"][0]["message"]
                    text = message.get("content", "").strip()

                    # Strip any leaked reasoning trace (safety net)
                    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

                    # First try to extract from XML tags if the model followed instructions
                    subject_xml = re.search(r"<subject>(.*?)</subject>", text, re.IGNORECASE | re.DOTALL)
                    body_xml = re.search(r"<body>(.*?)</body>", text, re.IGNORECASE | re.DOTALL)

                    if subject_xml and body_xml:
                        subject = subject_xml.group(1).strip()
                        body = body_xml.group(1).strip()
                    else:
                        # Fallback: Extract the LAST occurrence of SUBJECT and BODY
                        # The model often narrates its thinking before outputting the real email.
                        all_subj = list(re.finditer(r"(?:^|\n)\*{0,2}SUBJECT:\*{0,2}\s*(.*?)(?=\n|$)", text, re.IGNORECASE))
                        all_body = list(re.finditer(r"(?:^|\n)\*{0,2}BODY:\*{0,2}\s*([\s\S]*)", text, re.IGNORECASE))

                        subject = all_subj[-1].group(1).strip() if all_subj else "Quick Question"
                        body    = all_body[-1].group(1).strip() if all_body else text

                    # Ultimate cleanup for both XML and fallback paths
                    subject = re.sub(r"^\*{0,2}SUBJECT:\*{0,2}\s*", "", subject, flags=re.IGNORECASE).strip()
                    body = re.sub(r"^\*{0,2}BODY:\*{0,2}\s*", "", body, flags=re.IGNORECASE).strip()
                    body = re.sub(r"^\*{0,2}SUBJECT:\*{0,2}.*?\n", "", body, flags=re.IGNORECASE).strip()
                    
                    # Strip any unfilled <placeholder> tokens
                    body    = re.sub(r"<[^>]{1,80}>", "", body).strip()
                    subject = re.sub(r"<[^>]{1,80}>", "", subject).strip() or "Quick Question"
                    
                    # Safety check for verbatim instruction leaks
                    if "then newline" in subject.lower() or "no extra text" in subject.lower():
                        subject = "Quick Question"

                    subject = sanitize(subject)
                    body = sanitize(body)

                    with sqlite3.connect(DB) as thread_conn:
                        # Precaution: if the lead was already sent, regenerating it resets it to 'ready'
                        # which might skew stats slightly, but is required to resend.
                        thread_conn.execute(
                            "UPDATE leads SET email_body=?, status='ready' WHERE id=?",
                            (f"SUBJECT: {subject}\n\nBODY:\n{body}", lead["id"])
                        )
                        thread_conn.commit()
                    
                    success = True
                    return True  # Signal success for this lead

                except requests.Timeout:
                    log_activity("error", f"Provider {provider['name']} timed out (model too slow). Trying next provider...")
                    continue
                except Exception as e:
                    log_activity("error", f"Provider {provider['name']} failed: {str(e)}")
                    continue

            if not success:
                log_activity("error", f"Lead {lead['id']} failed all providers — skipping (batch continues).")
            return success

        # --- Run leads in parallel (max 3 at a time to respect API limits) ---
        max_workers = 3
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_lead, lead): lead for lead in leads}
            for future in as_completed(futures):
                lead = futures[future]
                try:
                    ok = future.result()
                    update_batch(batch_id, success=ok)
                except Exception as exc:
                    log_activity("error", f"Lead {lead['id']} thread crashed: {exc}")
                    update_batch(batch_id, success=False)

        log_activity("ai", f"Completed generation batch {batch_id} for campaign {campaign_id}")

    scheduled_time = d.get("scheduled_time")
    if scheduled_time:
        try:
            dt = datetime.fromisoformat(scheduled_time.replace("Z", "+00:00"))
            scheduler.add_job(generate_batch, 'date', run_date=dt)
            log_activity("ai", f"Scheduled generation batch for campaign {campaign_id} at {dt}")
            return jsonify({"queued": len(leads), "status": "scheduled"})
        except Exception as e:
            return jsonify({"error": f"Invalid scheduled_time format: {e}"}), 400
    else:
        threading.Thread(target=generate_batch, daemon=True).start()
        return jsonify({"queued": len(leads), "status": "generating"})

# ── SCRAPE LEADS ─────────────────────────────────────────────────────────────

MAPS_SCRAPE_STATUS = {"status": "idle", "logs": [], "leads": [], "progress": 0}

@app.route("/api/scrape-google-maps/status", methods=["GET"])
def scrape_google_maps_status():
    return jsonify(MAPS_SCRAPE_STATUS)

@app.route("/api/scrape-google-maps/save", methods=["POST"])
def scrape_google_maps_save():
    d = request.json or {}
    campaign_id = d.get("campaign_id")
    leads_to_save = d.get("leads", [])
    if not campaign_id:
        return jsonify({"error": "campaign_id is required"}), 400
    
    conn = get_db()
    count = 0
    for l in leads_to_save:
        conn.execute("""
            INSERT INTO leads(campaign_id, first_name, last_name, email, company, role, website, custom_data)
            VALUES(?,?,?,?,?,?,?,?)
        """, (campaign_id, "", "", l.get("email", ""), l.get("company name", ""), "",
              l.get("website", ""),
              json.dumps({"address": l.get("address", ""), "phone": l.get("phone", "")})))
        count += 1
    conn.commit()
    conn.close()
    
    log_activity("leads", f"Saved {count} scraped leads to campaign {campaign_id}")
    return jsonify({"status": "ok", "saved": count})

@app.route("/api/scrape-google-maps", methods=["POST"])
def trigger_scrape_google_maps():
    global MAPS_SCRAPE_STATUS
    d = request.json or {}
    query           = d.get("query")
    limit           = int(d.get("limit", 10))
    require_website = bool(d.get("require_website", True))
    require_email   = bool(d.get("require_email", False))
    if not query:
        return jsonify({"error": "query is required"}), 400
        
    MAPS_SCRAPE_STATUS = {"status": "running", "logs": ["Starting Google Maps Scraper..."], "leads": [], "progress": 0}
    
    def run_async_scrape():
        global MAPS_SCRAPE_STATUS
        import asyncio
        import importlib
        import sys

        # Force-reload the scraper module from disk every time,
        # bypassing Python's sys.modules cache — so editing
        # google_maps_scraper.py takes effect without restarting Flask.
        try:
            if "google_maps_scraper" in sys.modules:
                import google_maps_scraper as gms_module
                importlib.reload(gms_module)
            else:
                import google_maps_scraper as gms_module
            scrape_google_maps = gms_module.scrape_google_maps
        except Exception as import_err:
            MAPS_SCRAPE_STATUS["status"] = "failed"
            MAPS_SCRAPE_STATUS["logs"].append(f"Failed to import scraper module: {import_err}")
            return

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def handle_log(msg):
            MAPS_SCRAPE_STATUS["logs"].append(msg)

        try:
            results = loop.run_until_complete(scrape_google_maps(
                query,
                limit,
                log_callback    = handle_log,
                require_website = require_website,
                require_email   = require_email,
            ))
            MAPS_SCRAPE_STATUS["leads"] = results
            MAPS_SCRAPE_STATUS["status"] = "completed"
            MAPS_SCRAPE_STATUS["logs"].append(f"Scraping successfully finished. Found {len(results)} leads.")
        except Exception as e:
            MAPS_SCRAPE_STATUS["status"] = "failed"
            MAPS_SCRAPE_STATUS["logs"].append(f"Fatal error during scraping: {str(e)}")
        finally:
            loop.close()
            
    threading.Thread(target=run_async_scrape, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/scrape-google-maps/campaign", methods=["POST"])
def trigger_campaign_scrape():
    """
    Launch automated_campaign_runner in a background thread.
    Accepts: { niche, locations, limit, output, require_website, require_email }
    Reuses MAPS_SCRAPE_STATUS so the existing /status poll works unchanged.
    """
    global MAPS_SCRAPE_STATUS

    if MAPS_SCRAPE_STATUS.get("status") == "running":
        return jsonify({"error": "A scrape is already running. Please wait for it to finish."}), 409

    d               = request.json or {}
    niche           = d.get("niche", "").strip()
    locations       = d.get("locations", "").strip()
    limit           = int(d.get("limit", 100))
    output          = d.get("output", "campaign_leads.csv").strip() or "campaign_leads.csv"
    require_website = bool(d.get("require_website", True))
    require_email   = bool(d.get("require_email", False))

    if not niche:
        return jsonify({"error": "niche is required"}), 400
    if not locations:
        return jsonify({"error": "locations is required"}), 400

    MAPS_SCRAPE_STATUS = {
        "status": "running",
        "logs": [f"🚀 Campaign started: {niche} across {len([l for l in locations.split(',') if l.strip()])} location(s)"],
        "leads": [],
        "progress": 0,
    }

    def run_campaign():
        global MAPS_SCRAPE_STATUS
        import asyncio
        import importlib
        import sys

        try:
            if "google_maps_scraper" in sys.modules:
                import google_maps_scraper as gms
                importlib.reload(gms)
            else:
                import google_maps_scraper as gms
            campaign_runner = gms.automated_campaign_runner
        except Exception as err:
            MAPS_SCRAPE_STATUS["status"] = "failed"
            MAPS_SCRAPE_STATUS["logs"].append(f"Failed to import scraper: {err}")
            return

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def handle_log(msg):
            MAPS_SCRAPE_STATUS["logs"].append(msg)

        try:
            results = loop.run_until_complete(campaign_runner(
                niche            = niche,
                locations_str    = locations,
                total_limit      = limit,
                csv_output       = output,
                log_callback     = handle_log,
                require_website  = require_website,
                require_email    = require_email,
            ))
            MAPS_SCRAPE_STATUS["leads"]  = results
            MAPS_SCRAPE_STATUS["status"] = "completed"
            MAPS_SCRAPE_STATUS["logs"].append(
                f"🎉 Campaign complete! {len(results)} unique leads saved to {output}"
            )
            # Auto-save a scrape history snapshot
            if results:
                try:
                    import datetime as _dt
                    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
                    snap_label = f"{niche} — {ts} ({len(results)} leads)"
                    with sqlite3.connect(DB) as snap_conn:
                        snap_conn.execute(
                            "INSERT INTO scrape_history(label, source, lead_count, leads_json) VALUES(?,?,?,?)",
                            (snap_label, "campaign_scraper", len(results), json.dumps(results))
                        )
                        snap_conn.commit()
                    log_activity("scrape", f"Auto-saved scrape snapshot: {snap_label}")
                except Exception as snap_err:
                    log_activity("error", f"Failed to save scrape snapshot: {snap_err}")
        except Exception as e:
            MAPS_SCRAPE_STATUS["status"] = "failed"
            MAPS_SCRAPE_STATUS["logs"].append(f"Fatal error during campaign: {str(e)}")
        finally:
            loop.close()

    threading.Thread(target=run_campaign, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/scrape-leads", methods=["POST"])
def scrape_leads():
    """Scrape leads from Hunter.io or Apollo using domain search"""
    d = request.json or {}
    campaign_id = d.get("campaign_id")
    if not campaign_id:
        return jsonify({"error": "campaign_id is required"}), 400
    domain = d.get("domain")
    source = d.get("source", "hunter") # 'hunter' or 'apollo'
    api_key = d.get("api_key") or d.get("hunter_key") # fallback for backwards compat

    if not domain or not api_key:
        return jsonify({"error": "Domain and API key required"}), 400

    try:
        conn = get_db()
        count = 0
        if source == "hunter":
            url = f"https://api.hunter.io/v2/domain-search?domain={domain}&api_key={api_key}&limit=10"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            emails = data.get("data", {}).get("emails", [])
            for e in emails:
                conn.execute("""
                    INSERT INTO leads(campaign_id,first_name,last_name,email,company,role)
                    VALUES(?,?,?,?,?,?)
                """, (campaign_id,
                      e.get("first_name", ""),
                      e.get("last_name", ""),
                      e.get("value", ""),
                      data.get("data", {}).get("organization", domain),
                      e.get("position", "")))
                count += 1
        elif source == "apollo":
            url = "https://api.apollo.io/v1/mixed_people/search"
            payload = {
                "api_key": api_key,
                "q_organization_domains": domain,
                "per_page": 10
            }
            headers = {"Cache-Control": "no-cache", "Content-Type": "application/json"}
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            data = resp.json()
            people = data.get("people", [])
            for p in people:
                org = p.get("organization", {})
                conn.execute("""
                    INSERT INTO leads(campaign_id,first_name,last_name,email,company,role,linkedin)
                    VALUES(?,?,?,?,?,?,?)
                """, (campaign_id,
                      p.get("first_name", ""),
                      p.get("last_name", ""),
                      p.get("email", ""),
                      org.get("name", domain) if org else domain,
                      p.get("title", ""),
                      p.get("linkedin_url", "")))
                count += 1
        else:
            conn.close()
            return jsonify({"error": "Unknown source"}), 400

        conn.commit()
        conn.close()
        log_activity("scrape", f"Scraped {count} leads from {domain} via {source}")
        return jsonify({"found": count, "status": "ok"})
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500

@app.route("/api/campaigns/<int:cid>/resume", methods=["POST"])
def upload_resume(cid):
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file"}), 400
    
    # Save to a resumes folder
    if not os.path.exists("resumes"):
        os.makedirs("resumes")
    
    filename = f"resume_{cid}_{int(time.time())}.pdf"
    path = os.path.join("resumes", filename)
    file.save(path)
    
    conn = get_db()
    conn.execute("UPDATE campaigns SET attachment_path=? WHERE id=?", (path, cid))
    conn.commit()
    conn.close()
    
    log_activity("campaign", f"Resume uploaded for campaign {cid}")
    return jsonify({"status": "ok", "path": path})

# ── SMTP & SENDING ────────────────────────────────────────────────────────────

@app.route("/api/smtp", methods=["GET"])
def get_smtp():
    conn = get_db()
    rows = conn.execute("SELECT id,host,port,username,from_name,active FROM smtp_config").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/smtp", methods=["POST"])
def save_smtp():
    d = request.json
    conn = get_db()
    conn.execute(
        "INSERT INTO smtp_config(host,port,username,password,from_name,active) VALUES(?,?,?,?,?,1)",
        (d["host"], d["port"], d["username"], d["password"], d["from_name"])
    )
    conn.commit()
    conn.close()
    log_activity("smtp", f"Added SMTP server: {d['username']}")
    return jsonify({"status": "ok"})

@app.route("/api/smtp/toggle", methods=["POST"])
def toggle_smtp():
    d = request.json or {}
    sid = d.get("id")
    active = int(d.get("active", 1))
    conn = get_db()
    conn.execute("UPDATE smtp_config SET active=? WHERE id=?", (active, sid))
    conn.commit()
    conn.close()
    log_activity("smtp", f"Toggled SMTP ID {sid} active status to {active}")
    return jsonify({"status": "ok"})

@app.route("/api/smtp/delete", methods=["POST"])
def delete_smtp():
    d = request.json or {}
    sid = d.get("id")
    conn = get_db()
    conn.execute("DELETE FROM smtp_config WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    log_activity("smtp", f"Deleted SMTP ID {sid}")
    return jsonify({"status": "ok"})

@app.route("/api/send-emails", methods=["POST"])
def send_emails():
    d = request.json or {}
    campaign_id = d.get("campaign_id")
    lead_id = d.get("lead_id")
    lead_ids = d.get("lead_ids")
    duration_seconds = d.get("duration_seconds")
    delay = d.get("delay_seconds", 30)
    url_root = request.url_root

    def send_batch():
        with sqlite3.connect(DB) as thread_conn:
            thread_conn.row_factory = sqlite3.Row
            smtp_configs = thread_conn.execute("SELECT * FROM smtp_config WHERE active=1").fetchall()
            
            if not smtp_configs:
                log_activity("error", "No active SMTP configs found during send batch")
                return

            if lead_ids:
                placeholders = ','.join('?' for _ in lead_ids)
                leads = thread_conn.execute(
                    f"SELECT * FROM leads WHERE id IN ({placeholders}) AND email_body IS NOT NULL", tuple(lead_ids)
                ).fetchall()
            elif lead_id:
                leads = thread_conn.execute(
                    "SELECT * FROM leads WHERE id=?", (lead_id,)
                ).fetchall()
            elif campaign_id:
                leads = thread_conn.execute(
                    "SELECT * FROM leads WHERE campaign_id=? AND status='ready' ORDER BY status, created_at DESC", (campaign_id,)
                ).fetchall()
            else:
                leads = []

            leads_list = [dict(r) for r in leads]
            
            start_idx = d.get("start_index")
            end_idx = d.get("end_index")
            if start_idx is not None and end_idx is not None and campaign_id and not lead_ids and not lead_id:
                try:
                    s = max(0, int(start_idx) - 1)
                    e = int(end_idx)
                    leads_list = leads_list[s:e]
                except ValueError:
                    pass
            leads = leads_list

            if not leads:
                return

            total_leads = len(leads)

            for i, lead in enumerate(leads):
                try:
                    # Rotate SMTP configurations
                    smtp_cfg = smtp_configs[i % len(smtp_configs)]

                    c_id = lead["campaign_id"]
                    # Get the attachment path for this campaign
                    c_data = thread_conn.execute("SELECT attachment_path FROM campaigns WHERE id=?", (c_id,)).fetchone()
                    attachment_path = c_data["attachment_path"] if c_data else None

                    email_text = lead["email_body"] or ""
                    subject_match = re.search(r"SUBJECT:\s*(.*)", email_text, re.IGNORECASE)
                    subject = subject_match.group(1).strip() if subject_match else "Quick question"
                    body_match = re.search(r"BODY:\s*([\s\S]*)", email_text, re.IGNORECASE)
                    body = body_match.group(1).strip() if body_match else email_text

                    subject = sanitize(subject)
                    body = sanitize(body)

                    msg = MIMEMultipart()
                    safe_subject = smtp_sanitize(subject)
                    safe_from_name = smtp_sanitize(smtp_cfg["from_name"])
                    safe_from_user = smtp_sanitize(smtp_cfg["username"])
                    safe_to = smtp_sanitize(lead["email"])
                    msg["Subject"] = str(Header(safe_subject, "utf-8"))
                    msg["From"] = str(Header(safe_from_name, "utf-8")) + f" <{safe_from_user}>"
                    msg["To"] = safe_to
                    
                    # List-Unsubscribe Header
                    unsubscribe_email = os.environ.get("UNSUBSCRIBE_EMAIL", "unsubscribe@example.com")
                    msg["List-Unsubscribe"] = f"<mailto:{unsubscribe_email}?subject=unsubscribe>"
                    
                    # Convert to HTML body with open tracking pixel
                    base_url = os.environ.get("SERVER_URL") or url_root
                    html_body = f"""<html>
                    <body style="font-family: Arial, sans-serif; font-size: 14px; color: #333333; line-height: 1.6;">
                    {body.replace(chr(10), '<br>')}
                    <img src="{base_url.rstrip('/')}/api/track/open/{lead['id']}" width="1" height="1" style="display:none;" />
                    </body>
                    </html>"""
                    
                    alt_part = MIMEMultipart("alternative")
                    alt_part.attach(MIMEText(body, "plain", "utf-8"))
                    alt_part.attach(MIMEText(html_body, "html", "utf-8"))
                    msg.attach(alt_part)

                    if attachment_path and os.path.exists(attachment_path):
                        try:
                            with open(attachment_path, "rb") as f:
                                part = MIMEBase("application", "octet-stream")
                                part.set_payload(f.read())
                            encoders.encode_base64(part)
                            part.add_header(
                                "Content-Disposition",
                                f"attachment; filename={os.path.basename(attachment_path)}",
                            )
                            msg.attach(part)
                        except Exception as ae:
                            print(f"Attachment error: {ae}")

                    # Connection logic inside the loop to handle TLS vs SSL
                    port = int(smtp_cfg["port"])
                    if port == 465:
                        server_ctx = smtplib.SMTP_SSL(smtp_cfg["host"], port)
                    else:
                        server_ctx = smtplib.SMTP(smtp_cfg["host"], port)

                    with server_ctx as server:
                        if port != 465:
                            server.starttls()
                        server.login(smtp_cfg["username"], smtp_cfg["password"])
                        server.send_message(msg)

                    # Mark as sent using the thread's connection
                    thread_conn.execute("UPDATE leads SET status='sent', sent_at=datetime('now') WHERE id=?", (lead["id"],))
                    thread_conn.execute("UPDATE campaigns SET emails_sent=emails_sent+1 WHERE id=?", (c_id,))
                    thread_conn.commit()

                    if i < total_leads - 1:
                        if duration_seconds is not None and duration_seconds > 0:
                            avg_delay = duration_seconds / max(1, total_leads)
                            jitter = random.uniform(0.5, 1.5)
                            jittered_delay = max(1.0, avg_delay * jitter)
                            time.sleep(jittered_delay)
                        else:
                            jitter = random.uniform(5, 15)
                            time.sleep(delay + jitter)
                except Exception as e:
                    print(f"Failed to send to {lead['email']}: {e}")
                    log_activity("error", f"SMTP Error ({lead['email']}): {str(e)}")

        log_activity("send", f"Completed sending batch for campaign {campaign_id or 'selected leads'}")

    scheduled_time = d.get("scheduled_time")
    if scheduled_time:
        try:
            dt = datetime.fromisoformat(scheduled_time.replace("Z", "+00:00"))
            scheduler.add_job(send_batch, 'date', run_date=dt)
            log_activity("send", f"Scheduled send batch for campaign {campaign_id or 'selected leads'} at {dt}")
            return jsonify({"status": "scheduled"})
        except Exception as e:
            return jsonify({"error": f"Invalid scheduled_time format: {e}"}), 400
    else:
        threading.Thread(target=send_batch, daemon=True).start()
        return jsonify({"status": "sending"})

# ── STATS ─────────────────────────────────────────────────────────────────────

# ── SCRAPE HISTORY ───────────────────────────────────────────────────────────

@app.route("/api/scrape-history", methods=["GET"])
def get_scrape_history():
    """Return all scrape history snapshots (no leads_json to keep response small)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, label, source, lead_count, created_at FROM scrape_history ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/scrape-history/snapshot", methods=["POST"])
def save_scrape_snapshot():
    """Manually save the current MAPS_SCRAPE_STATUS leads as a history snapshot."""
    d = request.json or {}
    leads = MAPS_SCRAPE_STATUS.get("leads", [])
    if not leads:
        return jsonify({"error": "No leads in current scrape status to snapshot"}), 400
    label = d.get("label") or f"Manual Snapshot — {len(leads)} leads"
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO scrape_history(label, source, lead_count, leads_json) VALUES(?,?,?,?)",
        (label, "manual", len(leads), json.dumps(leads))
    )
    conn.commit()
    hid = cur.lastrowid
    conn.close()
    log_activity("scrape", f"Saved manual scrape snapshot: {label}")
    return jsonify({"id": hid, "label": label, "lead_count": len(leads), "status": "ok"})


@app.route("/api/scrape-history/<int:hid>/import", methods=["POST"])
def import_scrape_history(hid):
    """Import leads from a scrape history snapshot into a campaign, skipping global email duplicates."""
    d = request.json or {}
    campaign_id = d.get("campaign_id")
    if not campaign_id:
        return jsonify({"error": "campaign_id is required"}), 400

    conn = get_db()
    row = conn.execute("SELECT * FROM scrape_history WHERE id=?", (hid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Snapshot not found"}), 404

    try:
        leads = json.loads(row["leads_json"] or "[]")
    except Exception:
        conn.close()
        return jsonify({"error": "Invalid leads data in snapshot"}), 500

    # Collect all existing emails to prevent any duplicates globally
    existing_emails = {
        row_[0].strip().lower()
        for row_ in conn.execute(
            "SELECT email FROM leads WHERE email IS NOT NULL AND email != ''"
        ).fetchall()
    }

    def clean_phone(p):
        return re.sub(r'[^\x20-\x7E]', '', p or '').strip()

    added = 0
    skipped = 0
    for lead in leads:
        email   = str(lead.get("email", "")).strip()
        company = str(lead.get("company name", lead.get("company", ""))).strip()
        website = str(lead.get("website", "")).strip()
        phone   = clean_phone(lead.get("phone", ""))
        address = str(lead.get("address", "")).strip()

        if not email or email.lower() in existing_emails:
            skipped += 1
            continue

        custom_data = json.dumps({"address": address, "phone": phone, "source": f"history_import_{hid}"})
        conn.execute(
            "INSERT INTO leads(campaign_id,first_name,last_name,email,company,role,website,custom_data) VALUES(?,?,?,?,?,?,?,?)",
            (campaign_id, "", "", email, company, "", website, custom_data)
        )
        existing_emails.add(email.lower())
        added += 1

    conn.commit()
    log_activity("leads", f"Imported {added} leads from history snapshot #{hid} into campaign {campaign_id} ({skipped} skipped as duplicates)")
    conn.close()
    return jsonify({"imported": added, "skipped": skipped, "status": "ok"})


@app.route("/api/scrape-history/<int:hid>/delete", methods=["POST"])
def delete_scrape_history(hid):
    """Delete a scrape history snapshot."""
    conn = get_db()
    conn.execute("DELETE FROM scrape_history WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/stats", methods=["GET"])
def get_stats():
    conn = get_db()
    clients = conn.execute("SELECT COUNT(*) as n FROM clients").fetchone()["n"]
    campaigns = conn.execute("SELECT COUNT(*) as n FROM campaigns").fetchone()["n"]
    leads = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
    sent = conn.execute("SELECT COUNT(*) as n FROM leads WHERE status='sent'").fetchone()["n"]
    replied = conn.execute("SELECT COUNT(*) as n FROM leads WHERE status='replied'").fetchone()["n"]
    booked = conn.execute("SELECT COUNT(*) as n FROM leads WHERE status='booked'").fetchone()["n"]
    revenue = conn.execute("SELECT COALESCE(SUM(monthly_fee),0) as n FROM clients WHERE status='active'").fetchone()["n"]
    recent = conn.execute("SELECT * FROM activity_log ORDER BY created_at DESC LIMIT 8").fetchall()
    conn.close()
    reply_rate = round((replied / sent * 100), 1) if sent > 0 else 0
    book_rate = round((booked / replied * 100), 1) if replied > 0 else 0
    return jsonify({
        "clients": clients, "campaigns": campaigns, "leads": leads,
        "sent": sent, "replied": replied, "booked": booked,
        "reply_rate": reply_rate, "book_rate": book_rate,
        "monthly_revenue": revenue,
        "activity": [dict(r) for r in recent]
    })


# ── SECURITY ENDPOINTS ────────────────────────────────────────────────────────

@app.route("/api/security/smtp-injection-audit", methods=["POST"])
def api_smtp_injection_audit():
    data = request.json or {}
    result = audit_smtp_injection(data)
    log_activity("security", f"[SMTP Injection Shield] {'THREAT DETECTED' if not result['passed'] else 'PASSED'} — {len(result['flagged'])} field(s) flagged")
    return jsonify(result)

@app.route("/api/security/tls-validate", methods=["POST"])
def api_tls_validate():
    d = request.json or {}
    host = d.get("host", "")
    port = int(d.get("port", 587))
    if not host:
        return jsonify({"error": "host is required"}), 400
    result = validate_tls(host, port)
    status = "SECURE" if result["tls_ok"] else "INSECURE"
    log_activity("security", f"[TLS Validator] {host}:{port} → {status} | cipher={result.get('cipher')} | proto={result.get('protocol')}")
    return jsonify(result)

@app.route("/api/security/dns-audit", methods=["POST"])
def api_dns_audit():
    d = request.json or {}
    domain = d.get("domain", "")
    dkim_selector = d.get("dkim_selector", "default")
    if not domain:
        return jsonify({"error": "domain is required"}), 400
    result = audit_sender_dns(domain, dkim_selector)
    log_activity("security", f"[DNS Spoofing Auditor] {domain} → SPF={'✓' if result['spf_found'] else '✗'} DKIM={'✓' if result['dkim_found'] else '✗'}")
    return jsonify(result)

@app.route("/api/security/rate-status", methods=["GET"])
def api_rate_status():
    ip = request.remote_addr or "127.0.0.1"
    check = get_rate_status_readonly(ip)
    stats = get_rate_stats()
    return jsonify({"current_ip": check, "all_ips": stats})

@app.route("/api/security/header-injection-scan", methods=["POST"])
def api_header_injection_scan():
    d = request.json or {}
    subject = d.get("subject", "")
    body = d.get("body", "")
    result = detect_header_injection(subject, body)
    log_activity("security", f"[Header Injection Detector] {'THREAT DETECTED' if not result['passed'] else 'CLEAN'} — {len(result['findings'])} finding(s)")
    return jsonify(result)

@app.route("/api/security/blacklist-check", methods=["POST"])
def api_blacklist_check():
    d = request.json or {}
    domain = d.get("domain", "")
    if not domain:
        return jsonify({"error": "domain is required"}), 400
    result = check_domain_blacklist(domain)
    status = "BLACKLISTED" if not result.get("passed") else "CLEAN"
    log_activity("security", f"[Blacklist Checker] {domain} → {status} | listed_on={result.get('listed_on', [])}")
    return jsonify(result)

@app.route("/api/security/full-scan", methods=["POST"])
def api_full_security_scan():
    d = request.json or {}
    host = d.get("smtp_host", "")
    port = int(d.get("smtp_port", 587))
    domain = d.get("domain", "")
    report = {}
    if host:
        report["tls"] = validate_tls(host, port)
    if domain:
        report["dns"] = audit_sender_dns(domain, d.get("dkim_selector", "default"))
        report["blacklist"] = check_domain_blacklist(domain)
    report["rate"] = check_rate_limit(request.remote_addr or "127.0.0.1")
    lead_data = d.get("sample_lead", {})
    if lead_data:
        report["smtp_injection"] = audit_smtp_injection(lead_data)
    log_activity("security", f"[Full Security Scan] Completed for domain={domain} smtp={host}")
    return jsonify(report)
# ── CSV ENRICHER ENDPOINTS ───────────────────────────────────────────────────

ENRICH_STATUS = {
    "status": "idle",
    "progress": 0,
    "total": 0,
    "current": 0,
    "logs": [],
    "input_file": None,
    "output_file": None,
    "detected_website_col": None,
    "headers": []
}

@app.route("/api/enrich/upload", methods=["POST"])
def enrich_upload_csv():
    global ENRICH_STATUS
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400
    
    SCRATCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scratch")
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    temp_path = os.path.join(SCRATCH_DIR, "upload_enrich.csv")
    file.save(temp_path)
    
    try:
        with open(temp_path, mode="r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            first_row = next(reader, None)
            if not first_row:
                return jsonify({"error": "Empty CSV file"}), 400
            
            headers = [h.strip() for h in first_row]
            
            preview_rows = []
            for _ in range(5):
                row = next(reader, None)
                if row:
                    preview_rows.append(row)
                else:
                    break
        
        detected_col = None
        # Detect website column based on header keywords
        for h in headers:
            h_lower = h.lower()
            if any(k in h_lower for k in ["website", "domain", "url", "link", "web"]):
                detected_col = h
                break
                
        # Fallback: check first row values
        if not detected_col and preview_rows:
            first_data = preview_rows[0]
            for idx, val in enumerate(first_data):
                val_str = str(val).strip().lower()
                if val_str.startswith(("http://", "https://", "www.")) or any(val_str.endswith(ext) for ext in [".com", ".org", ".net", ".io", ".co"]):
                    if idx < len(headers):
                        detected_col = headers[idx]
                        break
        
        # Default to first column
        if not detected_col and headers:
            detected_col = headers[0]
            
        ENRICH_STATUS = {
            "status": "idle",
            "progress": 0,
            "total": 0,
            "current": 0,
            "logs": ["CSV uploaded. Ready to start enrichment."],
            "input_file": temp_path,
            "output_file": os.path.join(SCRATCH_DIR, "enriched_output.csv"),
            "detected_website_col": detected_col,
            "headers": headers
        }
        
        return jsonify({
            "status": "ok",
            "headers": headers,
            "detected_website_col": detected_col,
            "preview_rows": preview_rows[:3]
        })
        
    except Exception as e:
        return jsonify({"error": f"Failed to parse CSV: {str(e)}"}), 500

@app.route("/api/enrich/start", methods=["POST"])
def enrich_start():
    global ENRICH_STATUS
    if ENRICH_STATUS.get("status") == "running":
        return jsonify({"error": "An enrichment task is already running."}), 409
        
    d = request.json or {}
    website_column = d.get("website_column") or ENRICH_STATUS.get("detected_website_col")
    respect_robots = bool(d.get("respect_robots", True))
    
    if not ENRICH_STATUS.get("input_file"):
        return jsonify({"error": "No CSV file has been uploaded yet."}), 400
    if not website_column:
        return jsonify({"error": "website_column is required"}), 400
        
    ENRICH_STATUS["status"] = "running"
    ENRICH_STATUS["progress"] = 0
    ENRICH_STATUS["current"] = 0
    ENRICH_STATUS["logs"] = ["🚀 Starting CSV enrichment..."]
    
    def run_enrich():
        global ENRICH_STATUS
        import asyncio
        import importlib
        import sys
        
        try:
            if "enricher" in sys.modules:
                import enricher as enrich_mod
                importlib.reload(enrich_mod)
            else:
                import enricher as enrich_mod
            enrich_csv_task = enrich_mod.enrich_csv_task
        except Exception as err:
            ENRICH_STATUS["status"] = "failed"
            ENRICH_STATUS["logs"].append(f"Failed to import enricher: {err}")
            return

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        def handle_progress(current, total, msg):
            ENRICH_STATUS["current"] = current
            ENRICH_STATUS["total"] = total
            if total > 0:
                ENRICH_STATUS["progress"] = int((current / total) * 100)
            ENRICH_STATUS["logs"].append(msg)
            if len(ENRICH_STATUS["logs"]) > 200:
                ENRICH_STATUS["logs"].pop(0)
            
        try:
            loop.run_until_complete(enrich_csv_task(
                ENRICH_STATUS["input_file"],
                ENRICH_STATUS["output_file"],
                website_column,
                {"respect_robots": respect_robots},
                handle_progress
            ))
            ENRICH_STATUS["status"] = "completed"
        except Exception as e:
            ENRICH_STATUS["status"] = "failed"
            ENRICH_STATUS["logs"].append(f"Fatal error during enrichment: {str(e)}")
        finally:
            loop.close()
            
    threading.Thread(target=run_enrich, daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/api/enrich/status", methods=["GET"])
def enrich_status_check():
    return jsonify(ENRICH_STATUS)

@app.route("/api/enrich/download", methods=["GET"])
def enrich_download_output():
    if not ENRICH_STATUS.get("output_file") or not os.path.exists(ENRICH_STATUS["output_file"]):
        return jsonify({"error": "No enriched file available for download."}), 404
    return send_file(ENRICH_STATUS["output_file"], as_attachment=True, download_name="enriched_leads.csv")

# Call init_db at module level so gunicorn workers initialize the DB
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)