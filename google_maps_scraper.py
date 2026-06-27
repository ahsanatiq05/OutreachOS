"""
google_maps_scraper.py
======================
A production-grade, anti-ban Google Maps lead scraper.

TWO MODES:
  Single query (original):
      python google_maps_scraper.py "HVAC Atlanta" 25

  Automated campaign (new):
      python google_maps_scraper.py --campaign \
          --niche "HVAC" \
          --locations "Buckhead GA, Roswell GA, Alpharetta GA" \
          --limit 150 \
          --output my_leads.csv
"""

import sys
import os
import csv
import re
import random
import asyncio
import argparse
import time as _time
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import aiohttp
from urllib.parse import urljoin

# Fix Windows console encoding for unicode characters
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-infobars",
    "--window-size=1280,800",
    "--disable-extensions",
    "--disable-web-security",
    "--disable-features=IsolateOrigins,site-per-process",
]

SOCIAL_MEDIA_DOMAINS = [
    "facebook.com", "instagram.com", "twitter.com", "tiktok.com",
    "linkedin.com", "pinterest.com", "youtube.com", "snapchat.com",
    "wa.me", "whatsapp.com",
]

CONTACT_PATHS = [
    "/contact", "/contact-us", "/about", "/about-us",
    "/reach-us", "/get-in-touch", "/connect", "/info",
    "/contact.html", "/contact-us.html", "/about.html",
]

SKIP_EMAIL_PATTERNS = [
    '.png', '.jpg', '.jpeg', '.gif', '.css', '.js', '.svg', '.webp',
    'sentry', 'example', 'wixpress', 'squarespace', 'wordpress',
    'noreply', 'no-reply', 'placeholder', 'youremail', 'your-email',
    'user@', 'test@', 'support@example', 'admin@example',
]

GENERIC_EMAIL_PREFIXES = (
    'info@', 'contact@', 'sales@', 'hello@', 'support@', 'admin@', 
    'office@', 'inquiries@', 'marketing@', 'help@', 'team@', 'bookings@', 'press@', 'careers@',
    'general@', 'enquiries@'
)

CSV_HEADERS = ["company name", "website", "phone", "address", "email"]


# ═══════════════════════════════════════════════════════════════════════════════
#  EMAIL EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_emails(html_content: str) -> list:
    """Extract valid email addresses from HTML (regex + mailto: + deobfuscation)."""
    # Standard regex
    emails = list(re.findall(
        r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', html_content
    ))
    # mailto: hrefs (most reliable on business sites)
    emails += re.findall(
        r'mailto:([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
        html_content, re.IGNORECASE
    )
    # Deobfuscate "name [at] domain [dot] com" patterns
    for m in re.finditer(
        r'([a-zA-Z0-9._%+-]+)\s*(?:\[at\]|\(at\)|\s+at\s+|\[AT\]|@AT@)\s*'
        r'([a-zA-Z0-9.-]+)\s*(?:\[dot\]|\(dot\)|\s+dot\s+|\[DOT\])\.\s*([a-zA-Z]{2,})',
        html_content, re.IGNORECASE
    ):
        emails.append(f"{m.group(1)}@{m.group(2)}.{m.group(3)}")

    seen, valid = set(), []
    for e in emails:
        e = e.lower().strip()
        if e in seen:
            continue
        seen.add(e)
        if not any(pat in e for pat in SKIP_EMAIL_PATTERNS):
            valid.append(e)

    # Prioritize personal emails by sorting generic ones to the back
    valid.sort(key=lambda x: 1 if x.startswith(GENERIC_EMAIL_PREFIXES) else 0)
    
    return valid


async def crawl_for_email(url: str, log_fn=None) -> str:
    """
    3-stage email crawler:
      1. Scan homepage HTML (regex + mailto: + deobfuscation).
      2. Follow contact/about anchor links found on homepage.
      3. Directly probe common contact-page paths.
    Returns the first valid email found, or empty string.
    """
    def log(msg):
        print(msg)
        if log_fn:
            log_fn(msg)

    headers = {
        "User-Agent": CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    base_url = url.rstrip("/")
    fallback_email = ""

    def process_emails(found_emails):
        nonlocal fallback_email
        if not found_emails:
            return None
        for e in found_emails:
            if not e.startswith(GENERIC_EMAIL_PREFIXES):
                return e  # Return personal email immediately!
            if not fallback_email:
                fallback_email = e  # Save the first generic email as fallback
        return None

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            # Stage 1 + 2: homepage + contact anchor links
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=12), allow_redirects=True
                ) as resp:
                    html = await resp.text(errors='replace')
                    emails = extract_emails(html)
                    personal = process_emails(emails)
                    if personal: return personal

                    soup = BeautifulSoup(html, 'html.parser')
                    visited = set()
                    for a in soup.find_all('a', href=True):
                        if any(k in a['href'].lower() for k in [
                            'contact', 'about', 'reach', 'info', 'connect'
                        ]):
                            contact_url = urljoin(url, a['href'])
                            if contact_url in visited:
                                continue
                            visited.add(contact_url)
                            try:
                                log(f"  Crawling contact page: {contact_url}")
                                async with session.get(
                                    contact_url,
                                    timeout=aiohttp.ClientTimeout(total=10),
                                    allow_redirects=True
                                ) as cr:
                                    contact_html = await cr.text(errors='replace')
                                    emails = extract_emails(contact_html)
                                    personal = process_emails(emails)
                                    if personal: return personal
                            except Exception:
                                continue
            except Exception as e:
                log(f"  Homepage fetch failed for {url}: {e}")

            # Stage 3: probe common paths directly
            for path in CONTACT_PATHS:
                probe_url = base_url + path
                try:
                    async with session.get(
                        probe_url,
                        timeout=aiohttp.ClientTimeout(total=8),
                        allow_redirects=True
                    ) as pr:
                        if pr.status < 400:
                            probe_html = await pr.text(errors='replace')
                            emails = extract_emails(probe_html)
                            personal = process_emails(emails)
                            if personal:
                                log(f"  Found email via probed path: {probe_url}")
                                return personal
                except Exception:
                    continue

            return fallback_email
    except Exception as e:
        log(f"  Error crawling {url}: {e}")
        return fallback_email


# ═══════════════════════════════════════════════════════════════════════════════
#  CARD TEXT PARSER
# ═══════════════════════════════════════════════════════════════════════════════

def parse_card_text(card_text: str, name: str):
    """
    Parse the raw text of a Google Maps sidebar card into
    (category, address, rating).  Handles the four layout variants
    Google uses.
    """
    lines = [l.strip() for l in card_text.split('\n') if l.strip()]
    ICON_RANGE = (0xE000, 0xF8FF)

    def is_icon_only(s):
        return bool(s) and all(ICON_RANGE[0] <= ord(c) <= ICON_RANGE[1] for c in s)

    def strip_icons(s):
        return re.sub(r'[\ue000-\uf8ff]', '', s).strip()

    def is_plus_code(s):
        return bool(re.match(r'^[A-Z0-9]{4}\+[A-Z0-9]{2,}', s))

    def clean_address(s):
        return re.sub(r'^[\s\u00b7\u2022\u2027\u2023\u2043\ue000-\uf8ff·•]+', '', s).strip()

    category = address = rating = ""

    for line in lines:
        if name and name.lower() in line.lower():
            continue
        if 'sponsored' in line.lower():
            continue
        if is_icon_only(line):
            continue
        if re.match(r'^\d+\.\d+', line):
            rating = re.match(r'^\d+\.\d+', line).group()
            continue
        if re.match(r'^(open|closed|closes|öppen|geschlossen|abierto)', line.lower()):
            continue

        if ' · ' in line:
            parts = line.split(' · ', 1)
            cat_raw = strip_icons(parts[0])
            addr_raw = parts[1].strip()
            if not is_plus_code(addr_raw):
                if cat_raw and not category:
                    category = cat_raw
                if addr_raw and not address:
                    address = addr_raw
            else:
                if cat_raw and not category:
                    category = cat_raw
            continue

        if line.startswith('\u00b7') or line.startswith('·') or line.startswith('•'):
            addr_raw = clean_address(line)
            if addr_raw and not is_plus_code(addr_raw) and not address:
                address = addr_raw
            continue

        cleaned = strip_icons(line)
        if cleaned and len(cleaned) > 3:
            if not category:
                category = cleaned
            elif not address and len(cleaned) > 8:
                address = cleaned

    if address:
        address = clean_address(address)
    return category, address, rating


# ═══════════════════════════════════════════════════════════════════════════════
#  BROWSER HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _launch_browser(p, log_fn):
    """Launch Chromium with stealth args. Falls back to cached local binary."""
    browser = None
    try:
        browser = await p.chromium.launch(headless=True, args=STEALTH_ARGS)
    except Exception as launch_err:
        log_fn("Default Playwright Chromium not found. Searching for locally cached fallback...")
        import glob
        user_profile = os.environ.get("USERPROFILE", "C:\\Users\\admin")
        patterns = [
            os.path.join(user_profile, "AppData", "Local", "ms-playwright",
                         "**", "chrome-headless-shell.exe"),
            os.path.join(user_profile, "AppData", "Local", "ms-playwright",
                         "**", "chrome.exe"),
        ]
        found = []
        for pat in patterns:
            found.extend(glob.glob(pat, recursive=True))
        if found:
            log_fn(f"Using fallback browser: {found[0]}")
            try:
                browser = await p.chromium.launch(
                    headless=True, executable_path=found[0], args=STEALTH_ARGS
                )
            except Exception as inner:
                log_fn(f"Failed to launch fallback: {inner}")
        if not browser:
            raise launch_err
    return browser


async def _new_stealth_context(browser, log_fn):
    """Create a new browser context with randomised viewport + playwright-stealth."""
    vp_w = random.randint(1260, 1380)
    vp_h = random.randint(790, 850)
    context = await browser.new_context(
        locale="en-US",
        user_agent=CHROME_UA,           # single consistent UA — NO rotation
        viewport={"width": vp_w, "height": vp_h},
        java_script_enabled=True,
        bypass_csp=True,
    )
    try:
        # playwright-stealth v2.x API: Stealth class with apply_stealth_async
        from playwright_stealth import Stealth
        await Stealth().apply_stealth_async(context)
        log_fn("[stealth] playwright-stealth v2 applied (full fingerprint masking).")
    except (ImportError, AttributeError):
        try:
            # Fallback: playwright-stealth v1.x API
            from playwright_stealth import stealth_async
            await stealth_async(context)
            log_fn("[stealth] playwright-stealth v1 applied (full fingerprint masking).")
        except ImportError:
            log_fn("[stealth] playwright-stealth not installed — using manual patches.")
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
                window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(p) {
                    if (p === 37445) return 'Intel Inc.';
                    if (p === 37446) return 'Intel Iris OpenGL Engine';
                    return getParameter.call(this, p);
                };
            """)
    return context



# ═══════════════════════════════════════════════════════════════════════════════
#  CORE SCRAPER  (single query — called by both modes)
# ═══════════════════════════════════════════════════════════════════════════════

async def scrape_google_maps(
    query: str,
    limit: int = 20,
    log_callback=None,
    csv_output: str = None,          # if set, write rows immediately to this CSV
    seen_websites: set = None,       # global dedup set (shared across calls)
    leads_written_so_far: list = None,  # mutable counter list [int] for macro-throttle
    require_website: bool = True,    # skip leads that have no website
    require_email: bool = False,     # skip leads where email could not be found
) -> list:
    """
    Scrape Google Maps for `query`, up to `limit` results.

    Returns a list of lead dicts.
    If csv_output is provided, each lead is written to disk immediately
    (append mode) so data survives crashes.

    Filters:
      require_website  – drop leads with no website (default True)
      require_email    – drop leads with no email  (default False)
    """
    def log(msg):
        print(msg)
        if log_callback:
            log_callback(msg)

    if seen_websites is None:
        seen_websites = set()
    if leads_written_so_far is None:
        leads_written_so_far = [0]

    # ── Macro-throttle counter (shared across batches in campaign mode) ───────
    # Every 20 leads processed globally, rest 60–100 seconds.
    MACRO_PAUSE_EVERY = 20

    actual_limit = 100000 if limit < 0 else limit
    results = []

    async with async_playwright() as p:
        browser = await _launch_browser(p, log)
        context = await _new_stealth_context(browser, log)
        page = await context.new_page()

        # Navigate to search results
        # urlencode properly so commas in the query don't break the URL
        from urllib.parse import quote_plus
        search_url = f"https://www.google.com/maps/search/{quote_plus(query)}"
        log(f"Searching Google Maps for: {query}")
        log(f"URL: {search_url}")
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
        except Exception as e:
            log(f"Navigation warning (continuing): {e}")

        # Accept cookies / consent dialog if present — Google shows many variants
        consent_selectors = [
            'button:has-text("Accept all")',
            'button:has-text("Reject all")',
            'button:has-text("I agree")',
            'button:has-text("Agree")',
            'button:has-text("Accept")',
            'button[aria-label*="Accept"]',
            'button[jsname="higCR"]',   # Google consent button jsname
            'form[action*="consent"] button',
        ]
        for csel in consent_selectors:
            try:
                btn = page.locator(csel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    log(f"  Dismissed consent dialog: {csel}")
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                pass

        # Check if we were redirected to a consent/captcha page
        current_url = page.url
        if "consent.google" in current_url or "accounts.google" in current_url:
            log(f"WARNING: Redirected to consent/login page: {current_url}")
            log("  Attempting to navigate back to Maps...")
            try:
                await page.go_back()
                await page.wait_for_timeout(3000)
            except Exception:
                pass

        # Wait for results to render (longer wait for reliability)
        await page.wait_for_timeout(10000)

        # Find the results feed — Google Maps changes class names frequently
        feed = None
        feed_selectors = [
            'div[role="feed"]',
            'div.m6QErb[aria-label^="Results for"]',
            'div.m6QErb',
            'div[aria-label^="Results for"]',
            '.DxyBCb',
            'div[jstcache] div[role="feed"]',
        ]
        for sel in feed_selectors:
            try:
                candidate = page.locator(sel).first
                if await candidate.count() > 0:
                    await candidate.wait_for(state="visible", timeout=5000)
                    feed = candidate
                    log(f"Found results feed: {sel}")
                    break
            except Exception:
                pass

        if not feed:
            log("WARNING: Could not find results feed container — will try scrolling page directly.")
            # Log the page title so we can diagnose what Google returned
            try:
                title = await page.title()
                log(f"  Page title: {title}")
                final_url = page.url
                log(f"  Final URL: {final_url}")
            except Exception:
                pass

        if limit >= 30 or limit < 0:
            limit_desc = "limitless" if limit < 0 else str(limit)
            log(f"⚠ Scraping {limit_desc} results — extended delays active. Will take longer.")

        # ── Scroll to load cards (human-like variable speed) ──────────────────
        # Try multiple card selectors — Google changes these class names
        CARD_SELECTORS = [
            "a.hfpxzc",           # classic selector
            "a[href*='/maps/place/']",   # stable href-based fallback
            ".Nv2PK a[href*='/maps/']",  # result card link
            "div[jsaction] a[data-cid]", # data-cid attribute
        ]

        def get_card_locator():
            """Return the first card selector that has matches."""
            # We'll try each in the scroll loop
            return page.locator("a.hfpxzc, a[href*='/maps/place/']")

        previously_counted = 0
        no_change_streak = 0
        while no_change_streak < 6:
            scroll_px = random.randint(600, 1200)
            if feed:
                await feed.evaluate(f"(node) => node.scrollBy(0, {scroll_px})")
            else:
                await page.mouse.wheel(0, scroll_px)

            base_wait = 2800 if (0 < limit < 20) else 3500
            await page.wait_for_timeout(base_wait + random.randint(-400, 600))

            current_count = await get_card_locator().count()
            if current_count == previously_counted:
                no_change_streak += 1
            else:
                no_change_streak = 0
            previously_counted = current_count
            log(f"Loaded {current_count} results so far...")

            if limit > 0 and current_count >= limit:
                break

        total_items = await get_card_locator().count()
        num_to_process = min(total_items, actual_limit)
        log(f"Processing {num_to_process} results...")

        # ── Extract card data (NO clicking) ──────────────────────────────────
        raw_cards = []
        cards_locator = get_card_locator()
        for i in range(num_to_process):
            try:
                item = cards_locator.nth(i)
                name = await item.get_attribute("aria-label") or f"Business {i+1}"
                href = await item.get_attribute("href") or ""

                if any(ad in href for ad in ["aclk", "googleadservices", "adurl", "doubleclick"]):
                    log(f"  [{i+1}] Skipping sponsored ad: {name}")
                    continue

                card_text = ""
                try:
                    parent = page.locator(f'xpath=(//a[@class="hfpxzc"])[{i+1}]/..')
                    if await parent.count() > 0:
                        card_text = await parent.first.inner_text()
                except Exception:
                    pass

                category, address, rating = parse_card_text(card_text, name)
                raw_cards.append({
                    "name": name, "href": href,
                    "address": address, "category": category, "rating": rating,
                })
                log(f"  [{i+1}/{num_to_process}] {name} — {address or '(no address in card)'}")
            except Exception as e:
                log(f"  [{i+1}] Error reading card: {e}")

        # ── Visit each place URL to get website + phone ───────────────────────
        # Deep-reading pause: every DEEP_PAUSE_EVERY pages, stall 10-18s
        DEEP_PAUSE_EVERY = random.randint(7, 12)   # unpredictable per run
        pages_visited = 0

        for card in raw_cards:
            name    = card["name"]
            href    = card["href"]
            address = card["address"]
            website = ""
            phone   = ""

            if href and "/maps/place/" in href:
                log(f"  Checking place URL: {name}")
                try:
                    await page.goto(href, wait_until="commit", timeout=40000)

                    # Adaptive micro-delay (optimized for speed)
                    if 0 < limit <= 20:
                        wait_ms = random.randint(1500, 2500)
                    elif 0 < limit <= 35:
                        wait_ms = random.randint(2000, 3500)
                    else:
                        wait_ms = random.randint(3000, 5000)
                    await page.wait_for_timeout(wait_ms)

                    # Random mouse glance
                    await page.mouse.move(
                        random.randint(200, 900), random.randint(100, 600)
                    )

                    pages_visited += 1

                    # ── Deep reading pause (IP velocity mitigation - optimized) ──
                    if pages_visited % DEEP_PAUSE_EVERY == 0:
                        reading_pause_s = random.randint(10, 18)
                        log(f"  🔍 [human sim] Reading pause: {reading_pause_s}s...")
                        elapsed = 0
                        while elapsed < reading_pause_s * 1000:
                            chunk = random.randint(4000, 8000)
                            await page.mouse.wheel(0, random.randint(100, 400))
                            await page.wait_for_timeout(chunk)
                            elapsed += chunk

                    # Extract website link
                    for web_sel in [
                        'a[data-item-id="authority"]',
                        'a[aria-label*="Website"]',
                        'a[aria-label*="website"]',
                        'a[aria-label*="Site"]',
                    ]:
                        loc = page.locator(web_sel).first
                        if await loc.count() > 0:
                            raw = await loc.get_attribute("href") or ""
                            if (raw.startswith("http")
                                    and "google.com" not in raw
                                    and not any(s in raw for s in SOCIAL_MEDIA_DOMAINS)):
                                website = raw.strip()
                                break

                    # Extract phone number
                    for ph_sel in [
                        'button[data-item-id^="phone:"]',
                        'button[aria-label*="Phone:"]',
                        'button[aria-label*="Call"]',
                        '[data-item-id^="phone:"]',
                    ]:
                        loc = page.locator(ph_sel).first
                        if await loc.count() > 0:
                            phone = await loc.inner_text() or ""
                            if not phone:
                                aria = await loc.get_attribute("aria-label") or ""
                                phone = re.sub(r'(Phone:|Call|call)', '', aria).strip()
                            phone = phone.replace('\n', '').strip()
                            if phone:
                                break

                except Exception as e:
                    log(f"  Could not navigate to place URL: {e}")

            # ── WEBSITE FILTER ────────────────────────────────────────────────
            if require_website and not website:
                log(f"  [skip] {name} — no website found (require_website=True)")
                continue

            log(f"[OK] {name}" + (f" | {website}" if website else "") + (f" | {phone}" if phone else ""))

            # ── GLOBAL DEDUPLICATION (campaign mode) ─────────────────────────
            # Skip this lead if we already scraped this website in a prior city.
            normalized_site = website.lower().rstrip("/") if website else ""
            if normalized_site and normalized_site in seen_websites:
                log(f"  [skip] Already scraped {website} in a previous location.")
                continue
            if normalized_site:
                seen_websites.add(normalized_site)

            # ── Email extraction ──────────────────────────────────────────────
            email = ""
            fallback_email = ""
            # Try Maps panel first (fast, no extra HTTP request)
            try:
                gm_html = await page.content()
                gm_emails = extract_emails(gm_html)
                if gm_emails:
                    for e in gm_emails:
                        if not e.startswith(GENERIC_EMAIL_PREFIXES):
                            email = e
                            log(f"  Found personal email in Maps panel: {email}")
                            break
                        if not fallback_email:
                            fallback_email = e
                    if not email and fallback_email:
                        log(f"  Found generic email in Maps panel (will keep searching): {fallback_email}")
            except Exception:
                pass

            # Crawl business website if no personal email yet
            if not email and website:
                log(f"  Crawling for personal email: {website}")
                crawled_email = await crawl_for_email(website, log)
                if crawled_email:
                    if not crawled_email.startswith(GENERIC_EMAIL_PREFIXES):
                        email = crawled_email
                        log(f"  Found personal email: {email}")
                    elif not fallback_email:
                        fallback_email = crawled_email
                        log(f"  Found generic email: {fallback_email}")

            if not email and fallback_email:
                email = fallback_email
                log(f"  Using generic fallback email: {email}")

            # ── EMAIL FILTER ──────────────────────────────────────────────────
            if require_email and not email:
                log(f"  [skip] {name} — no email found (require_email=True)")
                continue

            lead = {
                "company name": name,
                "website": website,
                "phone": phone,
                "address": address,
                "email": email,
            }
            results.append(lead)

            # ── REAL-TIME DISK WRITE ──────────────────────────────────────────
            # Write each lead immediately so data survives crashes.
            if csv_output:
                _write_lead_to_csv(lead, csv_output)

            leads_written_so_far[0] += 1

            # ── MACRO-THROTTLE (every 20 leads globally, rest 60-100s) ────────
            if leads_written_so_far[0] % MACRO_PAUSE_EVERY == 0:
                macro_s = random.randint(60, 100)
                log(f"\n⏸ [macro-throttle] Processed {leads_written_so_far[0]} leads total. "
                    f"Resting {macro_s}s to reset IP trust score...\n")
                await asyncio.sleep(macro_s)

        await browser.close()
    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  CSV HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_csv_header(csv_path: str):
    """Create the CSV file with headers if it doesn't already exist."""
    p = Path(csv_path)
    if not p.exists() or p.stat().st_size == 0:
        with open(csv_path, mode="w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()
        print(f"[CSV] Created output file: {csv_path}")


def _write_lead_to_csv(lead: dict, csv_path: str):
    """Append a single lead row to the CSV immediately (crash-safe)."""
    with open(csv_path, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writerow({k: lead.get(k, "") for k in CSV_HEADERS})


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTOMATED CAMPAIGN RUNNER  (the orchestrator)
# ═══════════════════════════════════════════════════════════════════════════════

async def automated_campaign_runner(
    niche: str,
    locations_str: str,
    total_limit: int,
    csv_output: str = "outreachos_campaign.csv",
    log_callback=None,
    require_website: bool = True,
    require_email: bool = False,
):
    """
    Fire-and-forget orchestrator that:
      1. Splits total_limit evenly across all locations.
      2. Scrapes each city with a fresh browser context (memory flush).
      3. Writes every lead to disk the moment it is found (crash-safe).
      4. Maintains global deduplication across all cities.
      5. Macro-throttles every 20 leads (60-100s pause).
      6. City cooldown of 3-5 minutes between locations (IP trust reset).
    """
    def log(msg):
        ts = _time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        if log_callback:
            log_callback(line)

    locations = [loc.strip() for loc in locations_str.split(',') if loc.strip()]
    if not locations:
        log("ERROR: No locations provided.")
        return []

    is_limitless = (total_limit <= 0)

    if is_limitless:
        log("=" * 60)
        log(f"🚀 AUTOMATED CAMPAIGN STARTING (LIMITLESS MODE)")
        log(f"   Niche     : {niche}")
        log(f"   Locations : {', '.join(locations)}")
        log(f"   Total     : Limitless")
        log(f"   Output    : {csv_output}")
        log(f"   Safe est. : Dependent on results")
        log("=" * 60)
    else:
        base_per_loc = total_limit // len(locations)
        remainder    = total_limit % len(locations)
        log("=" * 60)
        log(f"🚀 AUTOMATED CAMPAIGN STARTING")
        log(f"   Niche     : {niche}")
        log(f"   Locations : {', '.join(locations)}")
        log(f"   Total     : {total_limit} leads  ({base_per_loc}/city + {remainder} remainder)")
        log(f"   Output    : {csv_output}")
        log(f"   Safe est. : ~{int(total_limit * 24 / 60)} minutes")
        log("=" * 60)

    _ensure_csv_header(csv_output)

    # Global state shared across all city batches
    seen_websites = set()           # deduplication across cities

    # Load existing websites from the CSV output to prevent duplicates if resuming/restarting
    if csv_output and os.path.exists(csv_output):
        try:
            with open(csv_output, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    site = row.get("website", "")
                    if site:
                        normalized = site.lower().rstrip("/")
                        seen_websites.add(normalized)
            log(f"Loaded {len(seen_websites)} existing websites from {csv_output} for global deduplication.")
        except Exception as csv_err:
            log(f"Warning: Could not parse existing CSV: {csv_err}")

    leads_written = [0]             # mutable counter for macro-throttle

    all_results = []

    for i, location in enumerate(locations):
        if is_limitless:
            loc_limit = 100000
        else:
            remaining_locations = len(locations) - i
            remaining_target = total_limit - len(all_results)
            if remaining_target <= 0:
                log(f"Target of {total_limit} leads reached. Ending campaign early.")
                break
            loc_limit = (remaining_target + remaining_locations - 1) // remaining_locations

        log(f"\n{'=' * 60}")
        log(f"📍 Batch {i+1}/{len(locations)}: '{location}'  (target: {'Limitless' if is_limitless else loc_limit} leads)")
        log(f"{'=' * 60}")

        batch_leads = []
        niche_list = [n.strip() for n in niche.split(',') if n.strip()]

        for n_idx, single_niche in enumerate(niche_list):
            if is_limitless:
                remaining_limit = -1
            else:
                remaining_loc_limit = loc_limit - len(batch_leads)
                remaining_global_limit = total_limit - len(all_results) - len(batch_leads)
                remaining_limit = min(remaining_loc_limit, remaining_global_limit)
                if remaining_limit <= 0:
                    break

            query = f"{single_niche} {location}"
            limit_str = "Limitless" if is_limitless else f"need {remaining_limit} more leads for this location"
            log(f"\n🔍 Searching niche {n_idx+1}/{len(niche_list)}: '{query}' ({limit_str})")

            try:
                batch = await scrape_google_maps(
                    query       = query,
                    limit       = remaining_limit,
                    log_callback= log_callback,
                    csv_output  = csv_output,
                    seen_websites    = seen_websites,
                    leads_written_so_far = leads_written,
                    require_website  = require_website,
                    require_email    = require_email,
                )
                batch_leads.extend(batch)
            except Exception as e:
                log(f"❌ Error in niche '{single_niche}' for location '{location}': {e}")

            # Cooldown between niches within the same location to look natural (e.g. 5-10 seconds)
            if n_idx < len(niche_list) - 1:
                if not is_limitless and len(batch_leads) >= loc_limit:
                    continue
                niche_pause = random.uniform(5.0, 10.0)
                log(f"💤 Niche cooldown: {niche_pause:.1f}s before next niche...")
                await asyncio.sleep(niche_pause)

        all_results.extend(batch_leads)
        log(f"✅ Batch {i+1} done — {len(batch_leads)} leads scraped for '{location}' "
            f"({leads_written[0]} total saved to disk so far)")

        # ── City cooldown (60–90 seconds) — browser is already closed inside scrape_google_maps
        if i < len(locations) - 1:
            city_pause = random.uniform(60.0, 90.0)
            log(f"\n💤 City cooldown: {city_pause / 60:.1f} min before '{locations[i+1]}'...")
            await asyncio.sleep(city_pause)

    log(f"\n{'=' * 60}")
    log(f"🎉 CAMPAIGN COMPLETE")
    log(f"   Total unique leads : {leads_written[0]}")
    log(f"   Saved to           : {csv_output}")
    log(f"{'=' * 60}")
    return all_results


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Google Maps Lead Scraper — single query OR automated campaign"
    )

    # ── Campaign mode flags ───────────────────────────────────────────────────
    parser.add_argument(
        "--campaign", action="store_true",
        help="Run in automated campaign mode (multiple locations)"
    )
    parser.add_argument("--niche",     type=str, help="Business niche, e.g. 'HVAC'")
    parser.add_argument(
        "--locations", type=str,
        help="Comma-separated list of locations, e.g. 'Buckhead GA, Roswell GA'"
    )
    parser.add_argument("--limit",  type=int, default=100, help="Total leads to collect")
    parser.add_argument(
        "--output", type=str, default="outreachos_campaign.csv",
        help="Output CSV file path"
    )
    # ── Filter flags ──────────────────────────────────────────────────────────
    parser.add_argument(
        "--require-website", dest="require_website",
        action="store_true", default=True,
        help="(default) Only keep leads that have a website (skip those without)"
    )
    parser.add_argument(
        "--no-require-website", dest="require_website",
        action="store_false",
        help="Include leads even if they have no website"
    )
    parser.add_argument(
        "--require-email", dest="require_email",
        action="store_true", default=False,
        help="Only keep leads where an email address was found"
    )
    parser.add_argument(
        "--no-require-email", dest="require_email",
        action="store_false",
        help="(default) Keep leads even if no email was found"
    )

    # ── Single-query mode (positional, backwards-compatible) ──────────────────
    parser.add_argument("query",  nargs="?", help="Search query for single-query mode")
    parser.add_argument("single_limit", nargs="?", type=int, default=20,
                        help="Result limit for single-query mode")

    args = parser.parse_args()

    # ── Campaign mode ─────────────────────────────────────────────────────────
    if args.campaign:
        if not args.niche or not args.locations:
            print("ERROR: --campaign requires --niche and --locations")
            print("Example:")
            print('  python google_maps_scraper.py --campaign \\')
            print('      --niche "HVAC" \\')
            print('      --locations "Buckhead GA, Roswell GA, Alpharetta GA" \\')
            print('      --limit 150 \\')
            print('      --output leads.csv')
            sys.exit(1)

        asyncio.run(automated_campaign_runner(
            niche            = args.niche,
            locations_str    = args.locations,
            total_limit      = args.limit,
            csv_output       = args.output,
            require_website  = args.require_website,
            require_email    = args.require_email,
        ))

    # ── Single-query mode (original behaviour) ────────────────────────────────
    else:
        if not args.query:
            print("Usage:")
            print("  Single query : python google_maps_scraper.py 'HVAC Atlanta' 25")
            print("  Campaign     : python google_maps_scraper.py --campaign --niche HVAC "
                  "--locations 'Atlanta GA, Roswell GA' --limit 100")
            sys.exit(1)

        query  = args.query
        limit  = args.single_limit
        output = args.output  # default: outreachos_campaign.csv
        if output == "outreachos_campaign.csv":
            output = "scraped_leads.csv"  # backwards-compatible default

        print(f"Starting single-query scrape for '{query}' (limit={limit})...")
        print(f"  Filters: require_website={args.require_website}, require_email={args.require_email}")

        _ensure_csv_header(output)
        results = asyncio.run(scrape_google_maps(
            query           = query,
            limit           = limit,
            csv_output      = output,
            require_website = args.require_website,
            require_email   = args.require_email,
        ))
        print(f"\nScraping complete! Saved {len(results)} leads to {output}")
