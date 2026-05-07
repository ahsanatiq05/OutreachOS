from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import sqlite3, json, smtplib, threading, time, requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
import os
from email.header import Header
import anthropic
import csv, io, re

def sanitize(text: str) -> str:
    """Ensure text is safe UTF-8 and strip problematic invisible chars."""
    if not text:
        return ""

    # Normalize common problematic unicode manually
    replacements = {
        "\u200b": "",   # zero-width space
        "\u200c": "",
        "\u200d": "",
        "\ufeff": "",   # BOM
    }
    for k, v in replacements.items():
        text = text.replace(k, v)

    # Force clean UTF-8
    return text.encode("utf-8", "ignore").decode("utf-8").strip()

app = Flask(__name__)
CORS(app)

@app.route("/")
def index():
    return send_file("outreachos-dashboard.html")

DB = "agency.db"

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
    d = request.json
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
    d = request.json
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO clients(name,company,email,monthly_fee) VALUES(?,?,?,?)",
        (d["name"], d["company"], d["email"], d.get("monthly_fee", 0))
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
    if client_id:
        query += f" WHERE camp.client_id = {client_id}"
    query += " GROUP BY camp.id ORDER BY camp.created_at DESC"
    rows = conn.execute(query).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/campaigns", methods=["POST"])
def create_campaign():
    d = request.json
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO campaigns(client_id,name,target_industry,target_role,value_prop,email_prompt,campaign_type) VALUES(?,?,?,?,?,?,?)",
        (d["client_id"], d["name"], d["target_industry"], d["target_role"], d["value_prop"], d.get("email_prompt"), d.get("campaign_type", "b2b"))
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
    d = request.json
    conn = get_db()
    conn.execute("UPDATE campaigns SET email_prompt=? WHERE id=?", (d.get("email_prompt"), cid))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

# ── LEADS ────────────────────────────────────────────────────────────────────

@app.route("/api/leads", methods=["GET"])
def get_leads():
    cid = request.args.get("campaign_id")
    conn = get_db()
    query = "SELECT * FROM leads"
    if cid:
        query += f" WHERE campaign_id = {cid}"
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query).fetchall()
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

@app.route("/api/leads/upload", methods=["POST"])
def upload_leads():
    cid = request.form.get("campaign_id")
    file = request.files.get("file")
    if not file or not cid:
        return jsonify({"error": "Missing file or campaign_id"}), 400
    content = file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    conn = get_db()
    count = 0
    for raw_row in reader:
        # Normalize keys to lowercase and strip whitespace
        row = {str(k).strip().lower(): v for k, v in raw_row.items() if k}
        
        # Map user's specific columns to our database fields
        company = row.get("company name", row.get("company", ""))
        website = row.get("website", "")
        linkedin = row.get("linkedin", "")
        role = row.get("focus area", row.get("role", ""))
        
        email = row.get("email", "")
        first_name = row.get("first_name", "")
        
        # Check for dynamic matches for email and name
        for k in row.keys():
            if "email" in k and not email:
                email = row[k]
            elif "name" in k and "company" not in k and not first_name:
                first_name = row[k]
            elif "direct rec" in k and "email" not in k and not first_name:
                first_name = row[k]  # Using direct recruiter as name
                
        conn.execute("""
            INSERT INTO leads(campaign_id,first_name,last_name,email,company,role,linkedin,website,custom_data)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (cid, first_name, row.get("last_name",""),
              email, company, role, linkedin, website, json.dumps(raw_row)))
        count += 1
    conn.commit()
    conn.close()
    log_activity("leads", f"Uploaded {count} leads to campaign {cid}")
    return jsonify({"imported": count, "status": "ok"})

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

# ── AI EMAIL GENERATION ───────────────────────────────────────────────────────

# ── CONFIGURATION: AI FALLBACK CHAIN ──
PROVIDERS = [
    {
        "name": "OpenRouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key": "REDACTED_OPENROUTER_KEY",
        "model": "nvidia/nemotron-3-super-120b-a12b:free"
    }
]

@app.route("/api/generate-emails", methods=["POST"])
def generate_emails():
    d = request.json
    campaign_id = d["campaign_id"]
    lead_id = d.get("lead_id")
    mode = d.get("mode", "new")
    force_reg = d.get("force_regenerate", False) # fallback for old clients

    conn = get_db()
    campaign_row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not campaign_row:
        conn.close()
        return jsonify({"error": "No campaign found"}), 404
    campaign = dict(campaign_row)

    if lead_id:
        leads_rows = conn.execute(
            "SELECT * FROM leads WHERE id=?", (lead_id,)
        ).fetchall()
    elif mode == "all":
        leads_rows = conn.execute(
            "SELECT * FROM leads WHERE campaign_id=?", (campaign_id,)
        ).fetchall()
    elif mode == "unsent" or force_reg:
        leads_rows = conn.execute(
            "SELECT * FROM leads WHERE campaign_id=? AND status IN ('new', 'ready')", (campaign_id,)
        ).fetchall()
    elif mode == "sent":
        leads_rows = conn.execute(
            "SELECT * FROM leads WHERE campaign_id=? AND status NOT IN ('new', 'ready')", (campaign_id,)
        ).fetchall()
    else: # "new"
        leads_rows = conn.execute(
            "SELECT * FROM leads WHERE campaign_id=? AND status='new' AND (email_body IS NULL OR email_body = '')", (campaign_id,)
        ).fetchall()
    leads = [dict(r) for r in leads_rows]
    conn.close()

    if not leads:
        return jsonify({"error": "No leads found for the selected mode"}), 404

    log_activity("ai", f"Starting generation for {len(leads)} leads with fallback support")

    def generate_batch():
        user_info = get_cached_context()
        
        for lead in leads:
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
                            return "{" + key + "}"
                            
                    format_vars = SafeDict({
                        'first_name': lead.get('first_name') or 'there',
                        'last_name': lead.get('last_name') or '',
                        'company': lead.get('company') or 'your company',
                        'role': lead.get('role') or '',
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
                        
                    try:
                        formatted_prompt = base_prompt.format_map(format_vars)
                    except Exception as e:
                        formatted_prompt = f"Instructions/Template:\n{base_prompt}\n\nTarget: {lead.get('first_name')} at {lead.get('company')}"

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
                            {"role": "system", "content": "You are a professional, expert cold email copywriter. Follow the user's instructions EXACTLY as provided."},
                            {"role": "user", "content": prompt}
                        ],
                        "temperature": 0.7
                    }
                    
                    headers = {
                        "Authorization": f"Bearer {current_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "http://localhost:5000",
                        "X-Title": "OutreachOS"
                    }

                    resp = requests.post(provider["url"], headers=headers, json=payload, timeout=20)
                    
                    if resp.status_code in [429, 500, 502, 503]:
                        log_activity("ai", f"{provider['name']} busy/limit hit. Trying next fallback...")
                        continue

                    resp.raise_for_status()
                    data = resp.json()
                    text = data["choices"][0]["message"]["content"].strip()

                    subject_match = re.search(r"SUBJECT:\s*(.*)", text, re.IGNORECASE)
                    body_match = re.search(r"BODY:\s*([\s\S]*)", text, re.IGNORECASE)
                    subject = sanitize(subject_match.group(1)) if subject_match else "Quick Question"
                    body = sanitize(body_match.group(1)) if body_match else text

                    with sqlite3.connect(DB) as thread_conn:
                        # Precaution: if the lead was already sent, regenerating it resets it to 'ready'
                        # which might skew stats slightly, but is required to resend.
                        thread_conn.execute(
                            "UPDATE leads SET email_body=?, status='ready' WHERE id=?",
                            (f"SUBJECT: {subject}\n\nBODY:\n{body}", lead["id"])
                        )
                        thread_conn.commit()
                    
                    success = True
                    break # Success! Move to next lead

                except Exception as e:
                    log_activity("error", f"Provider {provider['name']} failed: {str(e)}")
                    continue

            if not success:
                log_activity("error", f"Lead {lead['id']} failed all providers.")
            
            time.sleep(2) # Balanced delay for Cerebras/Groq limits

        log_activity("ai", f"Completed generation batch for campaign {campaign_id}")

    threading.Thread(target=generate_batch, daemon=True).start()
    return jsonify({"queued": len(leads), "status": "generating"})

# ── SCRAPE LEADS ─────────────────────────────────────────────────────────────

@app.route("/api/scrape-leads", methods=["POST"])
def scrape_leads():
    """Scrape leads from Hunter.io or Apollo using domain search"""
    d = request.json
    campaign_id = d["campaign_id"]
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
    row = conn.execute("SELECT id,host,port,username,from_name,active FROM smtp_config WHERE active=1 LIMIT 1").fetchone()
    conn.close()
    return jsonify(dict(row) if row else {})

@app.route("/api/smtp", methods=["POST"])
def save_smtp():
    d = request.json
    conn = get_db()
    conn.execute("UPDATE smtp_config SET active=0")
    conn.execute(
        "INSERT INTO smtp_config(host,port,username,password,from_name) VALUES(?,?,?,?,?)",
        (d["host"], d["port"], d["username"], d["password"], d["from_name"])
    )
    conn.commit()
    conn.close()
    log_activity("smtp", "SMTP config updated")
    return jsonify({"status": "ok"})

@app.route("/api/send-emails", methods=["POST"])
def send_emails():
    d = request.json
    campaign_id = d.get("campaign_id")
    lead_id = d.get("lead_id")
    lead_ids = d.get("lead_ids")
    duration_seconds = d.get("duration_seconds")
    delay = d.get("delay_seconds", 30)

    def send_batch():
        with sqlite3.connect(DB) as thread_conn:
            thread_conn.row_factory = sqlite3.Row
            smtp_cfg = thread_conn.execute("SELECT * FROM smtp_config WHERE active=1 LIMIT 1").fetchone()
            
            if not smtp_cfg:
                log_activity("error", "No SMTP config found during send batch")
                return

            # Re-fetch leads inside the thread to ensure fresh data
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
                    "SELECT * FROM leads WHERE campaign_id=? AND status='ready'", (campaign_id,)
                ).fetchall()
            else:
                leads = []

            if not leads:
                return

            total_leads = len(leads)

            for i, lead in enumerate(leads):
                try:
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
                    msg["Subject"] = str(Header(subject, "utf-8"))
                    msg["From"] = str(Header(smtp_cfg['from_name'], "utf-8")) + f" <{smtp_cfg['username']}>"
                    msg["To"] = lead["email"]
                    msg.attach(MIMEText(body, "plain", "utf-8"))

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

                    # Connection logic inside the loop to handle timeouts
                    with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"]) as server:
                        server.starttls()
                        server.login(smtp_cfg["username"], smtp_cfg["password"])
                        server.send_message(msg)

                    # Mark as sent using the thread's connection
                    thread_conn.execute("UPDATE leads SET status='sent', sent_at=datetime('now') WHERE id=?", (lead["id"],))
                    thread_conn.execute("UPDATE campaigns SET emails_sent=emails_sent+1 WHERE id=?", (c_id,))
                    thread_conn.commit()
                    
                    import random
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

    threading.Thread(target=send_batch, daemon=True).start()
    return jsonify({"status": "sending"})

# ── STATS ─────────────────────────────────────────────────────────────────────

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

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)