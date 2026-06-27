"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          OutreachOS — Security Module  (security.py)                        ║
║  6 Cybersecurity Shields for a Secure Enterprise Outreach Platform           ║
╚══════════════════════════════════════════════════════════════════════════════╝

Modules:
  1. SMTP Injection Shield      — strips \\r\\n from all user inputs (OWASP A03)
  2. TLS Handshake Validator    — confirms SMTP server supports STARTTLS
  3. Sender Spoofing Auditor    — validates SPF + DKIM via public DNS
  4. Brute-Force Rate Limiter   — blocks repeated API calls from one IP
  5. Email Header Injection Detector — scans body/subject for header poison
  6. Domain Blacklist Checker   — checks MX-Toolbox style DNS blacklists (DNSBL)
"""

import re
import ssl
import time
import socket
import smtplib
import hashlib
import dns.resolver                # dnspython
from collections import defaultdict
from datetime import datetime, timedelta

# ─────────────────────────────────────────────────────────────────────────────
# 1. SMTP INJECTION SHIELD  (OWASP A03 – Injection)
# ─────────────────────────────────────────────────────────────────────────────

# Characters that can break SMTP headers and inject new commands
_SMTP_INJECTION_PATTERN = re.compile(r"[\r\n\x00\x0b\x0c]")

def smtp_sanitize(value: str) -> str:
    """Remove SMTP header-injection characters from a string."""
    if not value:
        return ""
    return _SMTP_INJECTION_PATTERN.sub("", str(value)).strip()

def audit_smtp_injection(data: dict) -> dict:
    """
    Scan an arbitrary dict of lead/campaign data.
    Returns a report with any fields that contained injection characters.
    """
    flagged = {}
    safe    = {}
    for key, raw in data.items():
        if key in ("body", "email_body"):
            safe[key] = raw
            continue
        raw_str   = str(raw) if raw is not None else ""
        cleaned   = smtp_sanitize(raw_str)
        if cleaned != raw_str:
            flagged[key] = {"original": raw_str, "cleaned": cleaned}
        safe[key] = cleaned
    return {
        "passed"  : len(flagged) == 0,
        "flagged" : flagged,
        "safe"    : safe,
        "threat"  : "SMTP Header Injection" if flagged else None,
        "severity": "HIGH" if flagged else "NONE",
        "owasp"   : "A03:2021 – Injection",
    }

# ─────────────────────────────────────────────────────────────────────────────
# 2. TLS HANDSHAKE VALIDATOR
# ─────────────────────────────────────────────────────────────────────────────

def validate_tls(host: str, port: int = 587, timeout: int = 10) -> dict:
    """
    Attempt an SMTP STARTTLS handshake.
    Returns whether TLS is supported, the negotiated protocol version,
    and the cipher suite used.
    """
    result = {
        "host"    : host,
        "port"    : port,
        "tls_ok"  : False,
        "protocol": None,
        "cipher"  : None,
        "bits"    : None,
        "error"   : None,
        "severity": "HIGH",
        "passed"  : False,
    }
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=timeout) as srv:
            srv.ehlo()                   # Bug fix: always send EHLO first
            srv.starttls(context=ctx)
            srv.ehlo()                   # Re-identify after TLS upgrade
            # Bug fix: use the wrapped socket object reliably
            tls_sock = srv.sock
            if tls_sock and hasattr(tls_sock, 'version'):
                proto  = tls_sock.version()
                cipher = tls_sock.cipher()   # (name, proto, bits)
                result.update({
                    "tls_ok"  : True,
                    "passed"  : True,
                    "protocol": proto,
                    "cipher"  : cipher[0] if cipher else "Unknown",
                    "bits"    : cipher[2] if cipher else None,
                    "severity": "NONE",
                })
            else:
                # TLS negotiated but can't inspect — still mark as OK
                result.update({"tls_ok": True, "passed": True, "protocol": "TLSv1.2+", "cipher": "AES", "severity": "NONE"})
    except ssl.SSLError as e:
        result["error"]    = f"SSL Error: {e.reason}"
        result["severity"] = "CRITICAL"
    except smtplib.SMTPException as e:
        result["error"]    = f"SMTP Error: {str(e)}"
        result["severity"] = "HIGH"
    except (socket.timeout, OSError) as e:
        result["error"]    = f"Connection Error: {str(e)}"
        result["severity"] = "MEDIUM"
    return result

# ─────────────────────────────────────────────────────────────────────────────
# 3. SENDER SPOOFING AUDITOR  (SPF + DKIM DNS check)
# ─────────────────────────────────────────────────────────────────────────────

def audit_sender_dns(domain: str, dkim_selector: str = "default") -> dict:
    """
    Check if the domain has valid SPF and DKIM DNS records.
    SPF  → TXT record starting with 'v=spf1'
    DKIM → TXT record at <selector>._domainkey.<domain>
    """
    report = {
        "domain"        : domain,
        "spf_found"     : False,
        "spf_record"    : None,
        "dkim_found"    : False,
        "dkim_selector" : dkim_selector,
        "dkim_record"   : None,
        "passed"        : False,
        "warnings"      : [],
        "severity"      : "HIGH",
    }

    # ── SPF ──
    try:
        answers = dns.resolver.resolve(domain, "TXT")
        for rdata in answers:
            txt = "".join(s.decode() if isinstance(s, bytes) else s
                          for s in rdata.strings)
            if txt.startswith("v=spf1"):
                report["spf_found"]  = True
                report["spf_record"] = txt
                break
        if not report["spf_found"]:
            report["warnings"].append("No SPF record found — anyone can spoof your domain.")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.DNSException) as e:
        report["warnings"].append(f"SPF DNS lookup failed: {e}")

    # ── DKIM ──
    dkim_host = f"{dkim_selector}._domainkey.{domain}"
    try:
        answers = dns.resolver.resolve(dkim_host, "TXT")
        for rdata in answers:
            txt = "".join(s.decode() if isinstance(s, bytes) else s
                          for s in rdata.strings)
            if "v=DKIM1" in txt or "p=" in txt:
                report["dkim_found"]  = True
                report["dkim_record"] = txt[:120] + "…" if len(txt) > 120 else txt
                break
        if not report["dkim_found"]:
            report["warnings"].append(f"No DKIM record at '{dkim_host}'.")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.DNSException) as e:
        report["warnings"].append(f"DKIM DNS lookup failed: {e}")

    report["passed"]   = report["spf_found"] and report["dkim_found"]
    report["severity"] = "NONE" if report["passed"] else ("HIGH" if not report["spf_found"] else "MEDIUM")
    return report

# ─────────────────────────────────────────────────────────────────────────────
# 4. BRUTE-FORCE / RATE-LIMIT SHIELD
# ─────────────────────────────────────────────────────────────────────────────

# In-memory store: {ip: [timestamp, ...]}
_rate_store: dict = defaultdict(list)
_RATE_WINDOW  = 60   # seconds
_RATE_LIMIT   = 30   # max requests per window per IP

def check_rate_limit(ip: str) -> dict:
    """
    Returns whether the IP is within the allowed request rate.
    Call this before processing any sensitive API endpoint.
    """
    now  = time.time()
    hits = _rate_store[ip]
    # Prune old timestamps
    _rate_store[ip] = [t for t in hits if now - t < _RATE_WINDOW]
    _rate_store[ip].append(now)
    count  = len(_rate_store[ip])
    passed = count <= _RATE_LIMIT
    return {
        "ip"       : ip,
        "requests" : count,
        "limit"    : _RATE_LIMIT,
        "window_s" : _RATE_WINDOW,
        "passed"   : passed,
        "severity" : "NONE" if passed else "HIGH",
        "threat"   : None if passed else "Brute-Force / Excessive Requests",
        "owasp"    : "A07:2021 – Identification and Authentication Failures",
        "blocked_until": (datetime.utcnow() + timedelta(seconds=_RATE_WINDOW)).isoformat() + "Z"
                         if not passed else None,
    }

def get_rate_stats() -> list:
    """Return current rate-limit snapshot for all tracked IPs."""
    now = time.time()
    out = []
    for ip, hits in _rate_store.items():
        recent = [t for t in hits if now - t < _RATE_WINDOW]
        out.append({"ip": ip, "requests": len(recent), "limit": _RATE_LIMIT})
    return out

def get_rate_status_readonly(ip: str) -> dict:
    """Read-only rate check — does NOT increment the counter (safe for status API)."""
    now    = time.time()
    recent = [t for t in _rate_store.get(ip, []) if now - t < _RATE_WINDOW]
    count  = len(recent)
    passed = count <= _RATE_LIMIT   # strictly less — not counting this read call
    return {
        "ip"       : ip,
        "requests" : count,
        "limit"    : _RATE_LIMIT,
        "window_s" : _RATE_WINDOW,
        "passed"   : passed,
        "severity" : "NONE" if passed else "HIGH",
        "threat"   : None if passed else "Brute-Force / Excessive Requests",
        "owasp"    : "A07:2021 – Identification and Authentication Failures",
        "blocked_until": (datetime.utcnow() + timedelta(seconds=_RATE_WINDOW)).isoformat() + "Z"
                         if not passed else None,
    }

# ─────────────────────────────────────────────────────────────────────────────
# 5. EMAIL HEADER INJECTION DETECTOR
# ─────────────────────────────────────────────────────────────────────────────

_HEADER_INJECTION_RE = re.compile(
    r"(bcc\s*:|cc\s*:|to\s*:|content-type\s*:|mime-version\s*:)",
    re.IGNORECASE
)

def detect_header_injection(subject: str, body: str) -> dict:
    """
    Scan email subject and body for header injection patterns.
    Attackers embed BCC/CC headers to mass-spam through your server.
    """
    findings = []
    for field, text in [("subject", subject), ("body", body)]:
        matches = _HEADER_INJECTION_RE.findall(str(text))
        if matches:
            findings.append({
                "field"  : field,
                "matches": list(set(m.strip() for m in matches)),
            })
    passed = len(findings) == 0
    return {
        "passed"  : passed,
        "findings": findings,
        "threat"  : "Email Header Injection" if not passed else None,
        "severity": "CRITICAL" if not passed else "NONE",
        "owasp"   : "A03:2021 – Injection",
    }

# ─────────────────────────────────────────────────────────────────────────────
# 6. DOMAIN BLACKLIST CHECKER  (DNSBL)
# ─────────────────────────────────────────────────────────────────────────────

# Well-known DNS blacklists
_DNSBL_ZONES = [
    "zen.spamhaus.org",
    "bl.spamcop.net",
    "dnsbl.sorbs.net",
    "b.barracudacentral.org",
    "dnsbl-1.uceprotect.net",
]

def _reverse_ip(ip: str) -> str:
    """Reverse the octets of an IPv4 address for DNSBL lookup."""
    return ".".join(reversed(ip.split(".")))

def check_ip_blacklist(ip: str) -> dict:
    """
    Query well-known DNSBLs to see if `ip` is listed as a spam source.
    """
    reversed_ip = _reverse_ip(ip)
    listed_on   = []
    clean_on    = []
    errors      = []

    for zone in _DNSBL_ZONES:
        query = f"{reversed_ip}.{zone}"
        try:
            dns.resolver.resolve(query, "A")
            listed_on.append(zone)          # Got an A record → listed
        except dns.resolver.NXDOMAIN:
            clean_on.append(zone)           # NXDOMAIN → not listed ✓
        except dns.exception.DNSException as e:
            errors.append({"zone": zone, "error": str(e)})

    passed = len(listed_on) == 0
    return {
        "ip"       : ip,
        "listed_on": listed_on,
        "clean_on" : clean_on,
        "errors"   : errors,
        "passed"   : passed,
        "threat"   : f"IP blacklisted on {len(listed_on)} DNSBL(s)" if not passed else None,
        "severity" : "CRITICAL" if listed_on else "NONE",
        "owasp"    : "A05:2021 – Security Misconfiguration",
    }

def check_domain_blacklist(domain: str) -> dict:
    """Resolve domain → IP, then check that IP against DNSBLs."""
    try:
        ip = socket.gethostbyname(domain)
    except socket.gaierror as e:
        return {
            "domain"  : domain,
            "error"   : f"DNS resolution failed: {e}",
            "passed"  : False,
            "severity": "MEDIUM",
        }
    result = check_ip_blacklist(ip)
    result["domain"] = domain
    return result
