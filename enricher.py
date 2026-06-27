import csv
import re
import socket
import asyncio
import aiohttp
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from urllib.robotparser import RobotFileParser

# Emojis/User Agent Configuration
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Social Media Patterns
SOCIAL_DOMAINS = ["facebook.com", "instagram.com", "twitter.com", "linkedin.com", "x.com", "youtube.com"]

# Common Contact Page Subpaths
CONTACT_KEYWORDS = ["contact", "about", "reach", "connect", "support", "info"]

# Email regex pattern
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

# Phone regex pattern (handles US and international styles)
PHONE_REGEX = re.compile(r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}')

def normalize_url(url):
    url = url.strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url

def domain_resolves(url):
    try:
        normalized = normalize_url(url)
        parsed = urlparse(normalized)
        hostname = parsed.hostname or parsed.path
        if not hostname:
            return False
        if ":" in hostname:
            hostname = hostname.split(":")[0]
        socket.gethostbyname(hostname)
        return True
    except Exception:
        return False

async def check_robots_txt(session, url):
    try:
        normalized = normalize_url(url)
        parsed = urlparse(normalized)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        async with session.get(robots_url, timeout=3) as resp:
            if resp.status == 200:
                text = await resp.text(errors='ignore')
                rp = RobotFileParser()
                rp.parse(text.splitlines())
                return rp.can_fetch("*", normalized)
    except Exception:
        pass
    return True

def extract_meta_description(soup):
    for name in ["description", "Description", "og:description"]:
        meta = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if meta and meta.get("content"):
            return meta["content"].strip()
    return ""

def extract_socials(soup):
    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        for domain in SOCIAL_DOMAINS:
            if domain in href:
                found[domain.split(".")[0]] = a["href"]
    return found

def extract_phones(text):
    matches = PHONE_REGEX.findall(text)
    # The regex might return tuples if groups are used, let's normalize
    phones = []
    for match in matches:
        if isinstance(match, tuple):
            match = "".join(match)
        match = match.strip()
        if len(re.sub(r'\D', '', match)) >= 7:
            phones.append(match)
    return list(set(phones))

async def crawl_site(session, url, options):
    """
    Crawls a single site to extract:
    - title
    - description
    - emails
    - phones
    - socials
    """
    normalized = normalize_url(url)
    
    # 1. Check DNS resolution
    if not domain_resolves(normalized):
        return {"status": "Skipped (DNS check failed)", "title": "", "description": "", "emails": [], "phones": [], "socials": {}}

    # 2. Check Robots.txt if required
    if options.get("respect_robots", True):
        allowed = await check_robots_txt(session, normalized)
        if not allowed:
            return {"status": "Skipped (Disallowed by robots.txt)", "title": "", "description": "", "emails": [], "phones": [], "socials": {}}

    result = {
        "status": "Success",
        "title": "",
        "description": "",
        "emails": [],
        "phones": [],
        "socials": {}
    }

    try:
        # Fetch Homepage
        async with session.get(normalized, timeout=8, allow_redirects=True) as resp:
            if resp.status >= 400:
                return {"status": f"Skipped (Server returned {resp.status})", "title": "", "description": "", "emails": [], "phones": [], "socials": {}}
                
            html = await resp.text(errors='replace')
            soup = BeautifulSoup(html, "html.parser")
            
            # Extract basic tags
            result["title"] = soup.title.string.strip() if soup.title else ""
            result["description"] = extract_meta_description(soup)
            
            # Extract emails, phones, socials from homepage
            homepage_text = soup.get_text()
            result["emails"] = list(set(EMAIL_REGEX.findall(html)))
            result["phones"] = extract_phones(homepage_text)
            result["socials"] = extract_socials(soup)
            
            # If emails or phones are missing, try to find a contact page link
            if not result["emails"] or not result["phones"]:
                contact_links = []
                for a in soup.find_all("a", href=True):
                    href = a["href"].lower()
                    if any(kw in href for kw in CONTACT_KEYWORDS):
                        contact_links.append(urljoin(normalized, a["href"]))
                
                # Fetch first found contact page
                if contact_links:
                    contact_url = contact_links[0]
                    try:
                        async with session.get(contact_url, timeout=6, allow_redirects=True) as c_resp:
                            if c_resp.status == 200:
                                c_html = await c_resp.text(errors='replace')
                                c_soup = BeautifulSoup(c_html, "html.parser")
                                c_text = c_soup.get_text()
                                
                                result["emails"] = list(set(result["emails"] + EMAIL_REGEX.findall(c_html)))
                                result["phones"] = list(set(result["phones"] + extract_phones(c_text)))
                                # Update socials if missing
                                c_socials = extract_socials(c_soup)
                                for k, v in c_socials.items():
                                    if k not in result["socials"]:
                                        result["socials"][k] = v
                    except Exception:
                        pass
                        
    except asyncio.TimeoutError:
        result["status"] = "Skipped (Request Timeout)"
    except Exception as e:
        result["status"] = f"Skipped (Error: {str(e)})"
        
    return result

async def enrich_csv_task(input_path, output_path, website_column, options, progress_callback):
    """
    Asynchronously processes the CSV file, crawling each website and saving results.
    """
    rows = []
    # Read the original rows
    with open(input_path, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            rows.append(dict(row))

    if not rows:
        progress_callback(0, 0, "Empty CSV file provided.")
        return

    # Add enriched fields to the output
    enriched_fields = [
        "Enriched_Email",
        "Enriched_Phone",
        "Enriched_Title",
        "Enriched_Description",
        "Enriched_Socials",
        "Crawl_Status"
    ]
    output_fieldnames = list(fieldnames)
    for field in enriched_fields:
        if field not in output_fieldnames:
            output_fieldnames.append(field)

    total = len(rows)
    headers = {
        "User-Agent": CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    
    # We use a TCPConnector with a limit of concurrent requests
    connector = aiohttp.TCPConnector(ssl=False, limit=5)
    
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        for idx, row in enumerate(rows):
            url = row.get(website_column, "").strip()
            if not url:
                row["Enriched_Email"] = ""
                row["Enriched_Phone"] = ""
                row["Enriched_Title"] = ""
                row["Enriched_Description"] = ""
                row["Enriched_Socials"] = ""
                row["Crawl_Status"] = "Skipped (Empty Website)"
                progress_callback(idx + 1, total, f"Row {idx + 1}/{total}: Skipped (Empty Website)")
                continue
            
            progress_callback(idx, total, f"Row {idx + 1}/{total}: Crawling {url}...")
            crawl_result = await crawl_site(session, url, options)
            
            # Populate row with crawled data
            emails = ", ".join(crawl_result["emails"])
            phones = ", ".join(crawl_result["phones"])
            socials = ", ".join(f"{k}: {v}" for k, v in crawl_result["socials"].items())
            
            row["Enriched_Email"] = emails
            row["Enriched_Phone"] = phones
            row["Enriched_Title"] = crawl_result["title"]
            row["Enriched_Description"] = crawl_result["description"]
            row["Enriched_Socials"] = socials
            row["Crawl_Status"] = crawl_result["status"]
            
            progress_callback(idx + 1, total, f"Row {idx + 1}/{total}: Finished {url} -> {crawl_result['status']}")
            
            # Yield control occasionally
            await asyncio.sleep(0.1)

    # Write enriched rows to output path
    with open(output_path, mode="w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=output_fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        
    progress_callback(total, total, "Enrichment completed successfully!")
