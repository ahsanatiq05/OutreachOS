# OutreachOS — Cold Email Agency Platform

A full cold email lead generation agency system built with Python (Flask) + vanilla HTML/JS.
Zero monthly SaaS fee — you own everything.

## What It Does

- **Client Management** — Track clients, retainer fees, MRR
- **Campaign Management** — Multi-campaign per client with target industry/role/value prop
- **Lead Management** — Upload CSV or scrape via Hunter.io / Apollo APIs
- **AI Email Generation** — NVIDIA NIM writes hyper-personalized cold emails for every lead
- **CSV Enricher** — Automatically scrape email/contact data from company websites
- **Google Maps Scraper** — Scrape local business leads directly from Google Maps
- **SMTP Sending** — Sends via any SMTP provider with configurable delays & jitter
- **Open Tracking** — 1x1 pixel tracker marks leads as "opened"
- **Security Suite** — SMTP injection shield, TLS validator, DNS audit, header injection scan, blacklist checker
- **Dashboard** — Live stats: reply rate, booking rate, pipeline value, activity feed

## Setup (5 minutes)

### 1. Clone and install dependencies

```bash
git clone https://github.com/YOUR_USERNAME/outreachos.git
cd outreachos
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
# Edit .env and fill in your values (see .env.example for reference)
```

### 3. Start the server

```bash
python app.py
```

Server runs at `http://localhost:5000` — open that in your browser.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `NVIDIA_API_KEY` | ✅ Yes | NVIDIA NIM API key for AI email generation |
| `UNSUBSCRIBE_EMAIL` | No | Email shown in List-Unsubscribe header (defaults to `unsubscribe@example.com`) |
| `SERVER_URL` | No | Public URL for open-tracking pixel (auto-detected if blank) |
| `DATABASE_URL` | No | SQLite DB path (defaults to `agency.db`) |
| `PORT` | No | Server port (defaults to `5000`) |

Get your free NVIDIA NIM API key at [build.nvidia.com](https://build.nvidia.com/).

## Usage Flow

1. **Settings** → Add your SMTP config
2. **My Context** → Fill in your bio and projects (used for AI personalization)
3. **Clients** → Add your first client
4. **Campaigns** → Create a campaign (target industry, role, value prop, email prompt)
5. **Leads** → Upload a CSV or scrape leads from Google Maps / Hunter.io / Apollo
6. **Leads** → Click "AI Generate Emails" → NVIDIA NIM personalizes every email
7. **Leads** → Click "Send Emails" → Sends via SMTP with delay and jitter

## CSV Format

```
first_name,last_name,email,company,role,linkedin,website
John,Smith,john@acme.com,Acme Corp,CEO,linkedin.com/in/jsmith,acme.com
```

Any extra columns are automatically passed to the AI as context variables.

## SMTP Setup Examples

**Gmail:**
- Host: `smtp.gmail.com` | Port: `587`
- Use an App Password (not your regular password)
- Enable 2FA first, then generate App Password at myaccount.google.com

**Outlook:**
- Host: `smtp-mail.outlook.com` | Port: `587`

**Mailgun / SendGrid:**
- Use their SMTP credentials (better deliverability for bulk sending)

## Deployment

**Heroku / Render:**
```bash
# Set env vars in your platform's dashboard, then:
git push heroku main
```

The included `Procfile` uses Gunicorn with 2 workers and 4 threads.

## License

MIT — free to use, modify, and self-host.
