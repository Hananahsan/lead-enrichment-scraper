"""
Apollo Lead Enrichment Scraper
Reads a CSV of leads, scrapes their company websites, and outputs enriched data.
"""

import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 15
MAX_WORKERS = 5


def fetch_page(url, timeout=TIMEOUT):
    """Fetch a page and return (response, soup) or (None, None) on failure."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        return resp, soup
    except Exception:
        return None, None


def normalize_url(url):
    """Ensure URL has a scheme."""
    if not url:
        return None
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def extract_meta(soup):
    """Extract meta tags info."""
    meta = {}
    # Title
    if soup.title and soup.title.string:
        meta["page_title"] = soup.title.string.strip()

    # Meta description
    desc_tag = soup.find("meta", attrs={"name": "description"})
    if desc_tag and desc_tag.get("content"):
        meta["meta_description"] = desc_tag["content"].strip()

    # Meta keywords
    kw_tag = soup.find("meta", attrs={"name": "keywords"})
    if kw_tag and kw_tag.get("content"):
        meta["meta_keywords"] = kw_tag["content"].strip()

    # OG tags
    for prop in ["og:title", "og:description", "og:image", "og:type", "og:site_name"]:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            meta[prop.replace(":", "_")] = tag["content"].strip()

    return meta


def extract_social_links(soup, base_url):
    """Extract social media profile URLs."""
    social_patterns = {
        "linkedin": r"linkedin\.com",
        "twitter": r"(twitter\.com|x\.com)",
        "facebook": r"facebook\.com",
        "instagram": r"instagram\.com",
        "youtube": r"youtube\.com",
        "github": r"github\.com",
        "crunchbase": r"crunchbase\.com",
    }
    socials = {}
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        for platform, pattern in social_patterns.items():
            if platform not in socials and re.search(pattern, href, re.I):
                socials[platform] = href
    return socials


def extract_emails(soup, text):
    """Extract email addresses from page."""
    emails = set()
    # From mailto links
    for a_tag in soup.find_all("a", href=True):
        if a_tag["href"].startswith("mailto:"):
            email = a_tag["href"].replace("mailto:", "").split("?")[0].strip()
            if email:
                emails.add(email)
    # From page text
    email_pattern = r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
    emails.update(re.findall(email_pattern, text))
    # Filter out common false positives
    emails = {e for e in emails if not e.endswith((".png", ".jpg", ".gif", ".svg"))}
    return list(emails)


def extract_phone_numbers(text):
    """Extract phone numbers from page text."""
    patterns = [
        r"\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}",
        r"\+\d{1,3}[-.\s]?\d{1,4}[-.\s]?\d{1,4}[-.\s]?\d{1,9}",
    ]
    phones = set()
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for m in matches:
            cleaned = re.sub(r"[^\d+]", "", m)
            if 7 <= len(cleaned.replace("+", "")) <= 15:
                phones.add(m.strip())
    return list(phones)


def extract_tech_stack(soup, resp_headers):
    """Detect technologies from HTML and headers."""
    tech = []
    html = str(soup)

    checks = {
        "Google Analytics": [r"google-analytics\.com", r"gtag\(", r"UA-\d+"],
        "Google Tag Manager": [r"googletagmanager\.com"],
        "Facebook Pixel": [r"connect\.facebook\.net", r"fbq\("],
        "HubSpot": [r"js\.hs-scripts\.com", r"hs-analytics", r"hubspot"],
        "Salesforce": [r"salesforce\.com", r"pardot\.com"],
        "Intercom": [r"intercom\.io", r"widget\.intercom"],
        "Drift": [r"drift\.com", r"js\.driftt\.com"],
        "Zendesk": [r"zendesk\.com", r"zdassets\.com"],
        "Hotjar": [r"hotjar\.com", r"static\.hotjar"],
        "Segment": [r"segment\.com/analytics", r"cdn\.segment\.com"],
        "Mixpanel": [r"mixpanel\.com"],
        "Amplitude": [r"amplitude\.com"],
        "Stripe": [r"js\.stripe\.com", r"stripe\.com"],
        "WordPress": [r"wp-content", r"wp-includes"],
        "Shopify": [r"cdn\.shopify\.com", r"shopify\.com"],
        "React": [r"react\.production", r"__NEXT_DATA__", r"_next/"],
        "Next.js": [r"__NEXT_DATA__", r"_next/static"],
        "Vue.js": [r"vue\.js", r"vue\.min\.js", r"__vue"],
        "Angular": [r"ng-version", r"angular\.js"],
        "jQuery": [r"jquery\.min\.js", r"jquery-\d"],
        "Bootstrap": [r"bootstrap\.min\.(css|js)"],
        "Tailwind CSS": [r"tailwindcss", r"tailwind\.css"],
        "Cloudflare": [r"cloudflare"],
        "AWS": [r"amazonaws\.com"],
        "Vercel": [r"vercel"],
        "Netlify": [r"netlify"],
    }

    for tech_name, patterns in checks.items():
        for pattern in patterns:
            if re.search(pattern, html, re.I):
                tech.append(tech_name)
                break

    # Check response headers
    server = resp_headers.get("Server", "")
    if server:
        tech.append(f"Server: {server}")
    powered_by = resp_headers.get("X-Powered-By", "")
    if powered_by:
        tech.append(f"Powered-By: {powered_by}")

    return tech


def extract_company_info(soup, text):
    """Extract company description and other info from common page sections."""
    info = {}

    # Try to find "about" or "company" text in first few paragraphs
    paragraphs = soup.find_all("p")
    long_paragraphs = [p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 80]
    if long_paragraphs:
        info["site_description"] = long_paragraphs[0][:500]

    # Copyright notice (often has company legal name and year)
    copyright_pattern = r"(?:©|\(c\)|copyright)\s*(\d{4})?\s*([^.|\n]{2,80})"
    match = re.search(copyright_pattern, text, re.I)
    if match:
        info["copyright"] = match.group(0).strip()

    # Address patterns
    address_pattern = r"\d{1,5}\s[\w\s]{1,30}(?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|way|court|ct|place|pl)[\w\s,]{0,50}\d{5}"
    addr_match = re.search(address_pattern, text, re.I)
    if addr_match:
        info["address"] = addr_match.group(0).strip()

    return info


def find_subpages(soup, base_url):
    """Find links to key subpages (about, pricing, careers, contact, blog)."""
    keywords = {
        "about": r"about|who-we-are|our-story|company",
        "pricing": r"pricing|plans|packages",
        "careers": r"careers|jobs|hiring|join-us|work-with-us",
        "contact": r"contact|get-in-touch|reach-us",
        "blog": r"blog|news|insights|resources",
        "products": r"products|services|solutions|features",
        "team": r"team|people|leadership|our-team",
    }
    found = {}
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].lower()
        link_text = a_tag.get_text(strip=True).lower()
        for key, pattern in keywords.items():
            if key not in found:
                if re.search(pattern, href) or re.search(pattern, link_text):
                    full_url = urljoin(base_url, a_tag["href"])
                    parsed = urlparse(full_url)
                    base_parsed = urlparse(base_url)
                    # Only same domain
                    if parsed.netloc == base_parsed.netloc or not parsed.netloc:
                        found[key] = full_url
    return found


def scrape_subpage(url, page_type):
    """Scrape a subpage for additional intel."""
    _, soup = fetch_page(url)
    if not soup:
        return {}

    text = soup.get_text(separator=" ", strip=True)
    info = {}

    if page_type == "about":
        paragraphs = soup.find_all("p")
        long_p = [p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 80]
        if long_p:
            info["about_text"] = " ".join(long_p[:3])[:1000]

    elif page_type == "pricing":
        info["has_pricing_page"] = True
        # Look for price patterns
        prices = re.findall(r"\$[\d,]+(?:\.\d{2})?(?:/\w+)?", text)
        if prices:
            info["pricing_points"] = list(set(prices))[:10]

    elif page_type == "careers":
        info["has_careers_page"] = True
        # Count job-like listings
        job_elements = soup.find_all(["li", "div", "a"], string=re.compile(
            r"engineer|developer|designer|manager|analyst|sales|marketing", re.I
        ))
        if job_elements:
            info["job_roles_found"] = len(job_elements)

    elif page_type == "team":
        info["has_team_page"] = True
        # Count team member cards/images
        images = soup.find_all("img", alt=True)
        team_imgs = [img for img in images if len(img["alt"]) > 3 and not re.search(r"logo|icon|banner", img["alt"], re.I)]
        if team_imgs:
            info["team_size_estimate"] = len(team_imgs)

    return info


def scrape_website(url):
    """Main scraping function for a single website. Returns a dict of intel."""
    url = normalize_url(url)
    if not url:
        return {"scrape_status": "no_url"}

    intel = {"website_url": url}

    # Fetch homepage
    resp, soup = fetch_page(url)
    if not soup:
        intel["scrape_status"] = "failed"
        return intel

    intel["scrape_status"] = "success"
    intel["final_url"] = resp.url
    text = soup.get_text(separator=" ", strip=True)

    # Extract all intel from homepage
    intel.update(extract_meta(soup))
    intel["social_links"] = extract_social_links(soup, url)
    intel["emails"] = extract_emails(soup, text)
    intel["phone_numbers"] = extract_phone_numbers(text)
    intel["tech_stack"] = extract_tech_stack(soup, resp.headers)
    intel.update(extract_company_info(soup, text))

    # Find and scrape key subpages
    subpages = find_subpages(soup, url)
    intel["subpages_found"] = list(subpages.keys())

    for page_type, page_url in subpages.items():
        time.sleep(0.5)  # Be polite
        sub_intel = scrape_subpage(page_url, page_type)
        intel.update(sub_intel)

    return intel


def process_csv(input_path, output_path):
    """Read leads CSV, scrape each lead's website, write enriched CSV."""
    df = pd.read_csv(input_path)

    # Find the website column (flexible matching)
    website_col = None
    for col in df.columns:
        if any(kw in col.lower() for kw in ["website", "url", "domain", "web"]):
            website_col = col
            break

    if not website_col:
        print("Available columns:", list(df.columns))
        print("ERROR: No website/URL column found in CSV. Please ensure your CSV has a column "
              "with 'website', 'url', or 'domain' in its name.")
        sys.exit(1)

    print(f"Found website column: '{website_col}'")
    print(f"Processing {len(df)} leads...\n")

    results = []

    def process_row(idx, row):
        url = str(row[website_col]).strip()
        if not url or url.lower() in ("nan", "none", ""):
            return idx, {"scrape_status": "no_url"}
        print(f"  [{idx+1}/{len(df)}] Scraping {url}...")
        return idx, scrape_website(url)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_row, idx, row): idx
            for idx, row in df.iterrows()
        }
        for future in as_completed(futures):
            idx, intel = future.result()
            results.append((idx, intel))

    # Sort by original order
    results.sort(key=lambda x: x[0])

    # Flatten intel into columns
    enrichment_rows = []
    for idx, intel in results:
        flat = {}
        for key, value in intel.items():
            if isinstance(value, (list, dict)):
                flat[f"enriched_{key}"] = json.dumps(value)
            else:
                flat[f"enriched_{key}"] = value
        enrichment_rows.append(flat)

    enriched_df = pd.DataFrame(enrichment_rows)
    output_df = pd.concat([df.reset_index(drop=True), enriched_df], axis=1)
    output_df.to_csv(output_path, index=False)
    print(f"\nDone! Enriched data saved to: {output_path}")
    print(f"  - {sum(1 for _, i in results if i.get('scrape_status') == 'success')} successfully scraped")
    print(f"  - {sum(1 for _, i in results if i.get('scrape_status') == 'failed')} failed")
    print(f"  - {sum(1 for _, i in results if i.get('scrape_status') == 'no_url')} had no URL")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scraper.py <input.csv> [output.csv]")
        print("\nThe CSV should have a column containing website URLs")
        print("(column name should contain 'website', 'url', or 'domain')")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else input_file.replace(".csv", "_enriched.csv")

    process_csv(input_file, output_file)
