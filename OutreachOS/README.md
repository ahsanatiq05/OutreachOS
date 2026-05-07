# OutreachOS — Cold Email Agency Platform

A full cold email lead generation agency system built with Python (Flask) + vanilla HTML/JS.
Better than Naïve — zero monthly SaaS fee, you own everything.

## What It Does

- **Client Management** — Track clients, retainer fees, MRR
- **Campaign Management** — Multi-campaign per client with target industry/role/value prop
- **Lead Management** — Upload CSV or scrape via Hunter.io API
- **AI Email Generation** — Claude writes hyper-personalized emails for every lead
- **SMTP Sending** — Sends via any SMTP provider with configurable delays
- **Dashboard** — Live stats: reply rate, booking rate, pipeline value, activity feed

## Setup (5 minutes)

### 1. Install Python dependencies
```bash
cd backend
pip install -r requirements.txt
```

### 2. Start the backend
```bash
python app.py
```
Server runs at http://localhost:5000

### 3. Open the dashboard
Open `frontend/index.html` in your browser (just double-click it)

## Usage Flow

1. **Settings** → Add your SMTP config + Anthropic API key
2. **Clients** → Add your first client
3. **Campaigns** → Create a campaign (target industry, role, value prop)
4. **Leads** → Upload CSV or scrape a domain with Hunter.io
5. **Leads** → Click "AI Generate Emails" → Claude personalizes every email
6. **Leads** → Click "Send Emails" → Sends via SMTP with delay

## CSV Format
```
first_name, last_name, email, company, role, linkedin, website
John, Smith, john@acme.com, Acme Corp, CEO, linkedin.com/in/jsmith, acme.com
```

## SMTP Setup Examples

**Gmail:**
- Host: smtp.gmail.com | Port: 587
- Use an App Password (not your regular password)
- Enable 2FA first, then generate App Password at myaccount.google.com

**Outlook:**
- Host: smtp-mail.outlook.com | Port: 587

**Mailgun / SendGrid:**
- Use their SMTP credentials (better deliverability for bulk sending)

## APIs Needed

- **Anthropic API** — for AI email generation (get at console.anthropic.com)
- **Hunter.io API** (optional) — for domain scraping (free tier: 25 searches/mo)

## Selling This as a Service

Typical agency pricing:
- Setup fee: $500–1,000
- Monthly retainer: $500–2,000/client
- Per-meeting bonus: $100–300/call booked

With 5 clients at $1,000/mo = $5,000 MRR
