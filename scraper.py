"""
Apollo Lead Enrichment Scraper — Coaching Site Audit
Scrapes websites against a 6-category checklist:
  1. Booking infrastructure
  2. Site performance
  3. Offer details
  4. Audience signals
  5. Timing/recency signals
  6. Technical gaps
"""

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
from datetime import datetime
from pathlib import Path

# Load .env file if it exists
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip())

import warnings
import pandas as pd
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from dateutil import parser as dateutil_parser

try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

try:
    import whois
    HAS_WHOIS = True
except ImportError:
    HAS_WHOIS = False

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    import anthropic
    HAS_CLAUDE = True
except ImportError:
    HAS_CLAUDE = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 15
MAX_WORKERS = 3  # Lower for headless browser to avoid resource issues

PAGESPEED_API_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PAGESPEED_API_KEY = os.environ.get("PAGESPEED_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

BOOKING_TOOLS = {
    "Calendly": [r"calendly\.com"],
    "Acuity": [r"acuityscheduling\.com", r"squareup\.com/appointments"],
    "Cal.com": [r"cal\.com"],
    "Typeform": [r"typeform\.com"],
    "HubSpot Meetings": [r"meetings\.hubspot\.com"],
    "Savvycal": [r"savvycal\.com"],
    "TidyCal": [r"tidycal\.com"],
    "OnceHub": [r"oncehub\.com", r"scheduleonce\.com"],
    "Dubsado": [r"dubsado\.com"],
    "Book Like A Boss": [r"booklikeaboss\.com"],
    "YouCanBookMe": [r"youcanbook\.me"],
}


def fetch_page_simple(url, timeout=TIMEOUT):
    """Fetch a page with requests (no JS). Returns (response, soup) or (None, None)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        return resp, soup
    except Exception:
        return None, None


class BrowserResult:
    """Mimics requests.Response for compatibility."""
    def __init__(self, url, content, status_code, elapsed_seconds, headers=None):
        self.url = url
        self.text = content
        self.content = content.encode("utf-8")
        self.status_code = status_code
        self.elapsed = type("Elapsed", (), {"total_seconds": lambda self: elapsed_seconds})()
        self.headers = headers or {}


def fetch_page_browser(url, timeout=30000):
    """Fetch a page with headless Chromium (renders JS). Returns (response, soup) or (None, None)."""
    if not HAS_PLAYWRIGHT:
        return fetch_page_simple(url)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 390, "height": 844},  # Mobile viewport
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            )
            page = context.new_page()

            start = time.time()
            resp = page.goto(url, wait_until="networkidle", timeout=timeout)
            # Wait a bit more for lazy-loaded content
            page.wait_for_timeout(2000)
            elapsed = time.time() - start

            content = page.content()
            final_url = page.url
            status = resp.status if resp else 200
            headers = {}
            if resp:
                headers = {h["name"]: h["value"] for h in resp.headers_array()} if hasattr(resp, "headers_array") else {}

            # Check for mobile CTA visibility
            mobile_cta_visible = False
            try:
                cta_el = page.query_selector('a[href*="book"], a[href*="schedule"], a[href*="calendly"], a[href*="call"], button:has-text("Book"), button:has-text("Schedule"), button:has-text("Get Started")')
                if cta_el and cta_el.is_visible():
                    box = cta_el.bounding_box()
                    if box and box["y"] < 844:  # Within mobile viewport
                        mobile_cta_visible = True
            except Exception:
                pass

            browser.close()

            result = BrowserResult(final_url, content, status, elapsed, headers)
            result.mobile_cta_visible = mobile_cta_visible
            result.mobile_load_time = round(elapsed, 2)

            soup = BeautifulSoup(content, "html.parser")
            return result, soup
    except Exception as e:
        # Fallback to simple fetch
        return fetch_page_simple(url)


def fetch_page(url, timeout=TIMEOUT):
    """Fetch a page — uses headless browser for main page, requests for subpages."""
    return fetch_page_simple(url, timeout)


def fetch_homepage(url):
    """Fetch homepage with headless browser for full JS rendering."""
    return fetch_page_browser(url)


# =============================================================================
# GOOGLE PAGESPEED INSIGHTS
# =============================================================================

def get_pagespeed_insights(url):
    """Get real Lighthouse scores from Google PageSpeed Insights API (free, no key needed)."""
    data = {
        "pagespeed_score_performance": None,
        "pagespeed_score_accessibility": None,
        "pagespeed_score_seo": None,
        "pagespeed_score_best_practices": None,
        "pagespeed_lcp_seconds": None,
        "pagespeed_fid_ms": None,
        "pagespeed_cls": None,
        "pagespeed_fcp_seconds": None,
        "pagespeed_speed_index": None,
        "pagespeed_tbt_ms": None,
        "pagespeed_opportunities": [],
    }

    try:
        # Run for mobile strategy
        params = {
            "url": url,
            "strategy": "mobile",
            "category": ["performance", "accessibility", "seo", "best-practices"],
        }
        if PAGESPEED_API_KEY:
            params["key"] = PAGESPEED_API_KEY
        resp = requests.get(PAGESPEED_API_URL, params=params, timeout=60)
        if resp.status_code != 200:
            return data

        result = resp.json()
        lighthouse = result.get("lighthouseResult", {})
        categories = lighthouse.get("categories", {})
        audits = lighthouse.get("audits", {})

        # Scores (0-100)
        for cat_key, field_key in [
            ("performance", "pagespeed_score_performance"),
            ("accessibility", "pagespeed_score_accessibility"),
            ("seo", "pagespeed_score_seo"),
            ("best-practices", "pagespeed_score_best_practices"),
        ]:
            cat = categories.get(cat_key, {})
            if cat.get("score") is not None:
                data[field_key] = round(cat["score"] * 100)

        # Core Web Vitals
        if "largest-contentful-paint" in audits:
            val = audits["largest-contentful-paint"].get("numericValue")
            if val is not None:
                data["pagespeed_lcp_seconds"] = round(val / 1000, 2)

        if "max-potential-fid" in audits:
            val = audits["max-potential-fid"].get("numericValue")
            if val is not None:
                data["pagespeed_fid_ms"] = round(val)

        if "cumulative-layout-shift" in audits:
            val = audits["cumulative-layout-shift"].get("numericValue")
            if val is not None:
                data["pagespeed_cls"] = round(val, 3)

        if "first-contentful-paint" in audits:
            val = audits["first-contentful-paint"].get("numericValue")
            if val is not None:
                data["pagespeed_fcp_seconds"] = round(val / 1000, 2)

        if "speed-index" in audits:
            val = audits["speed-index"].get("numericValue")
            if val is not None:
                data["pagespeed_speed_index"] = round(val / 1000, 2)

        if "total-blocking-time" in audits:
            val = audits["total-blocking-time"].get("numericValue")
            if val is not None:
                data["pagespeed_tbt_ms"] = round(val)

        # Top opportunities for improvement
        for audit_key, audit_data in audits.items():
            if (audit_data.get("score") is not None
                    and audit_data["score"] < 0.9
                    and audit_data.get("details", {}).get("type") == "opportunity"):
                savings = audit_data.get("details", {}).get("overallSavingsMs", 0)
                if savings > 100:
                    data["pagespeed_opportunities"].append({
                        "issue": audit_data.get("title", audit_key),
                        "savings_ms": round(savings),
                    })

        data["pagespeed_opportunities"] = sorted(
            data["pagespeed_opportunities"],
            key=lambda x: x["savings_ms"],
            reverse=True,
        )[:5]

    except Exception:
        pass

    return data


def normalize_url(url):
    if not url:
        return None
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def get_all_links(soup, base_url):
    """Get all same-domain links from a page."""
    links = []
    base_parsed = urlparse(base_url)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.netloc == base_parsed.netloc or not parsed.netloc:
            links.append({
                "url": full,
                "href": href,
                "text": a.get_text(strip=True),
                "element": a,
            })
    return links


# =============================================================================
# 1. BOOKING INFRASTRUCTURE
# =============================================================================

def analyze_booking(soup, html, links, base_url):
    """Checklist category 1: Booking infrastructure."""
    data = {
        "booking_tool": "None detected",
        "booking_tool_branded": "",
        "booking_links": [],
        "booking_cta_above_fold": False,
        "pre_call_sequence_visible": False,
        "clicks_to_book": "Unknown",
        "booking_cta_works": "Unknown",
        "has_contact_form": False,
    }

    # Detect booking tool
    for tool, patterns in BOOKING_TOOLS.items():
        for pattern in patterns:
            if re.search(pattern, html, re.I):
                data["booking_tool"] = tool
                break
        if data["booking_tool"] != "None detected":
            break

    # Check for generic contact form
    forms = soup.find_all("form")
    for form in forms:
        form_html = str(form).lower()
        if any(kw in form_html for kw in ["contact", "message", "inquiry", "get in touch", "name", "email"]):
            data["has_contact_form"] = True
            if data["booking_tool"] == "None detected":
                data["booking_tool"] = "Contact form"
            break

    # Find booking/CTA links
    cta_keywords = re.compile(
        r"book|schedule|call|consult|discovery|strategy.?session|free.?call|"
        r"get.?started|apply|work.?with|let.?s.?talk|chat.?with|speak.?with",
        re.I,
    )
    booking_links = []
    for link in links:
        text = link["text"]
        href = link["href"]
        if cta_keywords.search(text) or cta_keywords.search(href):
            booking_links.append({"text": text[:80], "url": link["url"]})

    # Also check buttons
    for btn in soup.find_all(["button", "a"], class_=True):
        classes = " ".join(btn.get("class", []))
        text = btn.get_text(strip=True)
        if cta_keywords.search(text) or any(kw in classes.lower() for kw in ["cta", "btn-primary", "book", "schedule"]):
            href = btn.get("href", "")
            if href and href not in [b["url"] for b in booking_links]:
                booking_links.append({"text": text[:80], "url": urljoin(base_url, href)})

    data["booking_links"] = booking_links[:10]

    # Check if CTA is likely above the fold (in first 20 elements)
    first_elements = soup.find_all(["a", "button"], limit=30)
    for el in first_elements:
        text = el.get_text(strip=True)
        if cta_keywords.search(text):
            data["booking_cta_above_fold"] = True
            break

    # Check for branded vs generic booking
    if data["booking_tool"] not in ("None detected", "Contact form"):
        # Look for custom domain or embedded booking
        tool_lower = data["booking_tool"].lower()
        embedded = soup.find("iframe", src=re.compile(tool_lower, re.I))
        if embedded:
            data["booking_tool_branded"] = "Embedded on site"
        else:
            data["booking_tool_branded"] = "External link (not embedded)"

    # Pre-call sequence indicators
    precall_keywords = re.compile(
        r"confirmation|what.?to.?expect|before.?your.?call|prepare.?for|"
        r"reminder|welcome.?video|pre.?call|intake.?form",
        re.I,
    )
    if precall_keywords.search(html):
        data["pre_call_sequence_visible"] = True

    # Clicks to book estimate
    if booking_links:
        # Check if any booking link is on homepage directly
        for bl in booking_links:
            for tool_patterns in BOOKING_TOOLS.values():
                for pat in tool_patterns:
                    if re.search(pat, bl["url"], re.I):
                        data["clicks_to_book"] = "1 (direct link on homepage)"
                        break
        if data["clicks_to_book"] == "Unknown":
            data["clicks_to_book"] = "1-2 (CTA found on homepage)"
    else:
        data["clicks_to_book"] = "3+ (no obvious booking CTA on homepage)"

    # Verify booking links work
    if booking_links:
        test_url = booking_links[0]["url"]
        try:
            r = requests.head(test_url, headers=HEADERS, timeout=10, allow_redirects=True)
            if r.status_code < 400:
                data["booking_cta_works"] = "Yes"
            else:
                data["booking_cta_works"] = f"Broken (HTTP {r.status_code})"
        except Exception:
            data["booking_cta_works"] = "Broken (connection failed)"

    return data


# =============================================================================
# 1b. PAID ADS DETECTION
# =============================================================================

def analyze_paid_ads(soup, html):
    """Gap 1: Detect paid ad signals from website source."""
    data = {
        "has_facebook_pixel": False,
        "has_google_ads_tag": False,
        "has_linkedin_pixel": False,
        "has_retargeting_pixels": False,
        "utm_parameters_found": False,
        "likely_running_paid": False,
        "ad_details": [],
    }

    # Facebook / Meta Pixel
    if re.search(r"fbq\(|facebook\.com/tr|_fbp|connect\.facebook\.net|fbevents\.js", html, re.I):
        data["has_facebook_pixel"] = True
        data["ad_details"].append("Facebook/Meta Pixel detected")

    # Google Ads / gtag
    if re.search(r"googleads|gads|google_conversion|AW-\d+|ads\.google|adservice\.google", html, re.I):
        data["has_google_ads_tag"] = True
        data["ad_details"].append("Google Ads tag detected")

    # LinkedIn Insight Tag
    if re.search(r"snap\.licdn\.com|_linkedin_partner_id|linkedin\.com/px", html, re.I):
        data["has_linkedin_pixel"] = True
        data["ad_details"].append("LinkedIn Insight Tag detected")

    # Retargeting / analytics pixels
    retargeting = []
    if re.search(r"hotjar\.com|static\.hotjar", html, re.I):
        retargeting.append("Hotjar")
    if re.search(r"clarity\.ms", html, re.I):
        retargeting.append("Microsoft Clarity")
    if re.search(r"hs-scripts\.com|hs-analytics", html, re.I):
        retargeting.append("HubSpot tracking")
    if re.search(r"cdn\.segment\.com|analytics\.js", html, re.I):
        retargeting.append("Segment")
    if re.search(r"crisp\.chat", html, re.I):
        retargeting.append("Crisp")
    if re.search(r"intercom\.io", html, re.I):
        retargeting.append("Intercom")
    if retargeting:
        data["has_retargeting_pixels"] = True
        data["ad_details"].append(f"Retargeting/analytics: {', '.join(retargeting)}")

    # UTM parameters in internal links
    if re.search(r"utm_source|utm_medium=paid|utm_campaign", html, re.I):
        data["utm_parameters_found"] = True
        data["ad_details"].append("UTM parameters found in links")

    # Likely running paid
    data["likely_running_paid"] = any([
        data["has_facebook_pixel"],
        data["has_google_ads_tag"],
        data["has_linkedin_pixel"],
        data["utm_parameters_found"],
    ])

    return data


# =============================================================================
# 2. SITE PERFORMANCE
# =============================================================================

def analyze_performance(resp, soup, url):
    """Checklist category 2: Site performance."""
    data = {
        "load_time_seconds": None,
        "mobile_viewport_set": False,
        "page_size_kb": None,
        "image_count": 0,
        "large_images_detected": False,
        "mobile_layout_issues": [],
    }

    # Load time (approximate from response time)
    data["load_time_seconds"] = round(resp.elapsed.total_seconds(), 2)

    # Page size
    content_length = len(resp.content)
    data["page_size_kb"] = round(content_length / 1024, 1)

    # Mobile viewport
    viewport = soup.find("meta", attrs={"name": "viewport"})
    data["mobile_viewport_set"] = viewport is not None

    # Image analysis
    images = soup.find_all("img")
    data["image_count"] = len(images)

    # Check for responsive images
    non_responsive = 0
    for img in images:
        if not img.get("srcset") and not img.get("loading"):
            non_responsive += 1
    if non_responsive > 5:
        data["mobile_layout_issues"].append(f"{non_responsive} images without lazy loading or srcset")

    # Fixed width elements (mobile issue)
    html = str(soup)
    fixed_width = len(re.findall(r'width:\s*\d{4,}px', html))
    if fixed_width:
        data["mobile_layout_issues"].append(f"{fixed_width} elements with fixed width >999px")

    # Horizontal scroll risk
    if re.search(r'overflow-x:\s*hidden', html):
        data["mobile_layout_issues"].append("overflow-x:hidden used (may hide content on mobile)")

    if not data["mobile_layout_issues"]:
        data["mobile_layout_issues"] = ["No obvious issues detected"]

    return data


# =============================================================================
# 3. OFFER DETAILS
# =============================================================================

def analyze_offer(soup, text, html, subpages, base_url):
    """Checklist category 3: Offer details (enhanced with Gap 3 fixes)."""
    data = {
        "offer_type": [],
        "price_points": [],
        "price_range": "",
        "target_client": [],
        "solo_or_multi_coach": "Unknown",
        "offer_description": "",
        "pricing_page_exists": False,
        "pricing_page_url": "",
        "application_required": False,
        "sales_model": "Unknown",
    }

    # Scrape additional pages for offer info: /pricing, /work-with-me, /services, /programs
    all_text = text
    offer_page_keywords = {
        "pricing": r"pricing|invest|investment",
        "services": r"services|programs|offerings|work-with-me|work-with|coaching|packages",
    }
    for page_key in ["pricing", "services"]:
        page_url = subpages.get(page_key)
        if page_url:
            _, page_soup = fetch_page(page_url)
            if page_soup:
                page_text = page_soup.get_text(separator=" ", strip=True)
                all_text += " " + page_text
                if page_key == "pricing":
                    data["pricing_page_exists"] = True
                    data["pricing_page_url"] = page_url

    # Also look for pricing-like pages not yet found
    if not data["pricing_page_exists"]:
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].lower()
            link_text = a_tag.get_text(strip=True).lower()
            if re.search(r"pricing|invest|packages|work.?with", href + " " + link_text):
                full_url = urljoin(base_url, a_tag["href"])
                parsed = urlparse(full_url)
                base_parsed = urlparse(base_url)
                if parsed.netloc == base_parsed.netloc or not parsed.netloc:
                    data["pricing_page_exists"] = True
                    data["pricing_page_url"] = full_url
                    _, ps = fetch_page(full_url)
                    if ps:
                        all_text += " " + ps.get_text(separator=" ", strip=True)
                    break

    full_text = all_text.lower()

    # Detect offer types (expanded patterns from Gap 3)
    offer_patterns = {
        "1-on-1 Coaching": r"1.?on.?1|1:1|one.?on.?one|individual.?coaching|private.?coaching|personal.?coaching",
        "Group Program": r"group.?program|group.?coaching|cohort|group.?session",
        "Mastermind": r"mastermind|membership.?community",
        "Course": r"online.?course|self.?paced|video.?course|digital.?course|module|lesson",
        "Course + Coaching Hybrid": r"course.*coaching|coaching.*course|program.*support|program.*calls",
        "Workshop": r"workshop|bootcamp|intensive|immersive",
        "Membership": r"membership|community|monthly.?access|inner.?circle",
        "Retreat": r"retreat",
        "VIP Day": r"vip.?day|vip.?intensive|half.?day|full.?day",
        "Corporate Training": r"corporate.?training|team.?training|leadership.?development|organizational",
        "Consulting/Advisory": r"consulting|advisory|fractional|retainer",
        "Speaking": r"keynote|speaking|book.?me.?to.?speak",
    }

    for offer, pattern in offer_patterns.items():
        if re.search(pattern, full_text):
            data["offer_type"].append(offer)

    if not data["offer_type"]:
        data["offer_type"] = ["Not clearly stated"]

    # Price detection — scan ALL text from all pages
    prices = re.findall(r"\$[\d,]+(?:\.\d{2})?(?:\s*[/-]\s*\w+)?", all_text)
    if prices:
        data["price_points"] = list(set(prices))[:10]
        amounts = []
        for p in prices:
            num = re.search(r"[\d,]+", p)
            if num:
                amounts.append(int(num.group().replace(",", "")))
        if amounts:
            max_price = max(amounts)
            if max_price >= 2000:
                data["price_range"] = "High-ticket ($2K+)"
            elif max_price >= 500:
                data["price_range"] = "Mid-ticket ($500-$2K)"
            else:
                data["price_range"] = "Low-ticket (under $500)"
    else:
        # Check for indirect price signals in testimonials
        price_signals = re.findall(r"(?:invested|roi|5.?figure|6.?figure|five.?figure|six.?figure|\$\d)", full_text)
        if price_signals:
            data["price_range"] = "High-ticket (implied from testimonials)"
        else:
            data["price_range"] = "Not visible on site"

    # Application-based vs self-serve (Gap 3)
    apply_patterns = re.compile(r"apply.?now|application|book.?a.?discovery|schedule.?a.?consult|book.?a.?call|strategy.?call|qualify", re.I)
    selfserve_patterns = re.compile(r"buy.?now|add.?to.?cart|enroll.?now|purchase|checkout|instant.?access", re.I)
    contact_only = re.compile(r"contact.?us|get.?in.?touch|reach.?out|send.?a.?message", re.I)

    has_apply = apply_patterns.search(all_text)
    has_selfserve = selfserve_patterns.search(all_text)
    has_contact = contact_only.search(all_text)

    if has_apply:
        data["application_required"] = True
        data["sales_model"] = "Application-based"
    elif has_selfserve:
        data["sales_model"] = "Self-serve checkout"
    elif has_contact:
        data["sales_model"] = "Contact form only"
    else:
        data["sales_model"] = "Book a call" if re.search(r"book|schedule|call", full_text) else "Unknown"

    # Target client detection
    audience_patterns = {
        "Executives": r"executive|c-suite|ceo|cfo|cto|senior.?leader",
        "Entrepreneurs": r"entrepreneur|founder|business.?owner|startup|solopreneur",
        "Women Leaders": r"women.?leader|female.?founder|women.?in|her\b.*business|she\b.*lead",
        "Corporate Teams": r"corporate|team|organization|enterprise|company",
        "Career Professionals": r"career|professional|job|mid.?career|transition",
        "Coaches/Consultants": r"coach(?:es|ing)?.*coach|consultant.*grow|help.?coaches|coach.?training",
        "Health/Wellness": r"health|wellness|fitness|nutrition|mindset|burnout",
        "Sales Professionals": r"sales.?team|sales.?leader|revenue|quota",
        "Creatives": r"creative|artist|designer|writer|content.?creator",
        "Parents": r"parent|mom|dad|family",
    }

    for audience, pattern in audience_patterns.items():
        if re.search(pattern, full_text):
            data["target_client"].append(audience)

    if not data["target_client"]:
        data["target_client"] = ["Not clearly defined"]

    # Offer description from meta or first long paragraph
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        data["offer_description"] = meta_desc["content"].strip()[:300]
    else:
        paragraphs = soup.find_all("p")
        for p in paragraphs:
            ptext = p.get_text(strip=True)
            if len(ptext) > 80:
                data["offer_description"] = ptext[:300]
                break

    return data


# =============================================================================
# 3b. SOLO VS MULTI-COACH (enhanced - Gap 2)
# =============================================================================

def analyze_solo_vs_multi(soup, text, html, subpages, base_url):
    """Gap 2: Enhanced solo vs multi-coach detection."""
    data = {
        "solo_or_multi_coach": "Unknown",
        "team_page_exists": False,
        "team_coach_count": 0,
        "team_coach_names": [],
        "copy_uses_i_my": False,
        "copy_uses_we_our": False,
    }

    # Check pronoun usage on homepage
    # Count "I/my" vs "we/our" in body copy (not nav/footer)
    body = soup.find("main") or soup.find("body")
    body_text = body.get_text(separator=" ", strip=True) if body else text

    i_my_count = len(re.findall(r"\b(?:I|my|me|myself)\b", body_text))
    we_our_count = len(re.findall(r"\b(?:we|our|us|ourselves)\b", body_text))

    data["copy_uses_i_my"] = i_my_count > 3
    data["copy_uses_we_our"] = we_our_count > 3

    # Scrape team/about page
    team_urls = []
    for key in ["team", "about"]:
        if key in subpages:
            team_urls.append(subpages[key])

    # Also look for /our-coaches, /meet-the-team, etc.
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].lower()
        if re.search(r"our.?coaches|meet.?the.?team|our.?team|facilitator", href):
            full_url = urljoin(base_url, a_tag["href"])
            if full_url not in team_urls:
                team_urls.append(full_url)

    coach_names = []
    for team_url in team_urls[:3]:
        _, team_soup = fetch_page(team_url)
        if not team_soup:
            continue
        data["team_page_exists"] = True

        # Look for person headshots with names
        # Method 1: img alt tags with person names
        for img in team_soup.find_all("img", alt=True):
            alt = img["alt"].strip()
            # Name pattern: 2-3 capitalized words, no common non-name words
            if re.match(r"^[A-Z][a-z]+ [A-Z][a-z]+", alt) and not re.search(r"logo|icon|banner|hero|stock|image", alt, re.I):
                coach_names.append(alt)

        # Method 2: headings near "coach" or "team" sections
        for heading in team_soup.find_all(["h2", "h3", "h4"]):
            h_text = heading.get_text(strip=True)
            if re.match(r"^[A-Z][a-z]+ [A-Z][a-z]+", h_text) and len(h_text) < 40:
                # Check if near coach/team context
                parent = heading.parent
                if parent:
                    parent_text = parent.get_text().lower()
                    if re.search(r"coach|team|facilitator|trainer|consultant|advisor", parent_text):
                        if h_text not in coach_names:
                            coach_names.append(h_text)

        # Method 3: JSON-LD Person schema
        for script in team_soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(script.string)
                if isinstance(ld, list):
                    for item in ld:
                        if item.get("@type") == "Person" and item.get("name"):
                            if item["name"] not in coach_names:
                                coach_names.append(item["name"])
                elif isinstance(ld, dict):
                    if ld.get("@type") == "Person" and ld.get("name"):
                        if ld["name"] not in coach_names:
                            coach_names.append(ld["name"])
            except (json.JSONDecodeError, TypeError):
                pass

    data["team_coach_names"] = coach_names[:20]
    data["team_coach_count"] = len(coach_names)

    # Check booking page for coach selection
    booking_has_choice = False
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        for tool_patterns in BOOKING_TOOLS.values():
            for pat in tool_patterns:
                if re.search(pat, href, re.I):
                    _, bsoup = fetch_page(urljoin(base_url, href))
                    if bsoup:
                        bt = bsoup.get_text(separator=" ", strip=True).lower()
                        if re.search(r"select.?(?:your|a).?coach|choose.?(?:your|a).?coach|pick.?(?:your|a)", bt):
                            booking_has_choice = True
                    break

    # Determine solo vs multi
    if data["team_coach_count"] >= 2 or booking_has_choice:
        data["solo_or_multi_coach"] = "Multi-coach"
    elif data["team_coach_count"] == 1 and data["copy_uses_i_my"]:
        data["solo_or_multi_coach"] = "Solo"
    elif data["copy_uses_i_my"] and not data["copy_uses_we_our"]:
        data["solo_or_multi_coach"] = "Solo"
    elif data["copy_uses_i_my"] and data["copy_uses_we_our"]:
        data["solo_or_multi_coach"] = "Looks solo but uses 'we'"
    elif data["copy_uses_we_our"] and not data["copy_uses_i_my"]:
        if data["team_page_exists"]:
            data["solo_or_multi_coach"] = "Multi-coach"
        else:
            data["solo_or_multi_coach"] = "Uses 'we' (likely solo positioning as company)"
    else:
        data["solo_or_multi_coach"] = "Unknown"

    return data


# =============================================================================
# 4. AUDIENCE SIGNALS
# =============================================================================

def analyze_audience(soup, text, html, links, subpages):
    """Checklist category 4: Audience signals."""
    data = {
        "serves_multiple_audiences": False,
        "audience_segments": [],
        "has_active_blog": False,
        "latest_blog_date": "",
        "has_podcast": False,
        "has_youtube": False,
        "social_proof_testimonials": False,
        "testimonial_count": 0,
        "social_proof_logos": False,
        "social_proof_media_mentions": False,
        "social_proof_details": [],
    }

    # Multiple audience detection
    audience_pages = []
    for link in links:
        lt = link["text"].lower()
        href = link["href"].lower()
        if re.search(r"for.?(executive|entrepreneur|individual|corporate|team|women|leader|coach)", lt + " " + href, re.I):
            audience_pages.append(link["text"][:60])

    if len(audience_pages) >= 2:
        data["serves_multiple_audiences"] = True
        data["audience_segments"] = audience_pages[:5]

    # Also check if text mentions serving different groups
    multi_pattern = re.compile(
        r"(for.?individuals.?and.?(?:teams|corporate|organizations))|"
        r"(whether.?you.?re.?a.?.*or.?a)|"
        r"(for.?both.?.*and)",
        re.I,
    )
    if multi_pattern.search(text):
        data["serves_multiple_audiences"] = True

    # Blog detection
    blog_subpage = subpages.get("blog")
    if blog_subpage:
        _, blog_soup = fetch_page(blog_subpage)
        if blog_soup:
            blog_text = blog_soup.get_text(separator=" ", strip=True)
            # Look for dates
            date_patterns = [
                r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}",
                r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}",
                r"\d{4}-\d{2}-\d{2}",
            ]
            dates_found = []
            for dp in date_patterns:
                dates_found.extend(re.findall(dp, blog_text))

            if dates_found:
                data["has_active_blog"] = True
                data["latest_blog_date"] = dates_found[0]
            else:
                # Has blog page but can't find dates
                data["has_active_blog"] = True
                data["latest_blog_date"] = "Dates not found"
    else:
        # Check homepage for blog links
        for link in links:
            if re.search(r"blog|article|post|insight|resource", link["href"], re.I):
                data["has_active_blog"] = True
                break

    # Podcast detection
    podcast_patterns = r"podcast|episode|listen.?now|apple.?podcast|spotify.*podcast|anchor\.fm|buzzsprout"
    if re.search(podcast_patterns, html, re.I):
        data["has_podcast"] = True

    # YouTube detection
    if re.search(r"youtube\.com|youtu\.be", html, re.I):
        data["has_youtube"] = True

    # Testimonials
    testimonial_patterns = re.compile(
        r"testimonial|review|what.?(client|people|they).?say|success.?stor|"
        r"client.?result|transformation|case.?stud",
        re.I,
    )

    # Look for testimonial sections
    if testimonial_patterns.search(html):
        data["social_proof_testimonials"] = True

    # Count blockquotes and testimonial-like elements
    blockquotes = soup.find_all("blockquote")
    testimonial_divs = soup.find_all(["div", "section"], class_=re.compile(r"testimonial|review|quote", re.I))
    data["testimonial_count"] = max(len(blockquotes), len(testimonial_divs))

    # Look for quote marks as testimonial indicator
    quote_elements = soup.find_all(string=re.compile(r'^["\u201c].*["\u201d]$'))
    if quote_elements:
        data["testimonial_count"] = max(data["testimonial_count"], len(quote_elements))
        data["social_proof_testimonials"] = True

    # Logo bar / "as seen in"
    logo_patterns = re.compile(
        r"as.?seen.?in|featured.?in|trusted.?by|as.?featured|partner|client.?logo|"
        r"worked.?with|companies.?we|brands.?we",
        re.I,
    )
    if logo_patterns.search(html):
        data["social_proof_logos"] = True
        data["social_proof_details"].append("Logo bar / 'featured in' section found")

    # Media mentions
    media_patterns = re.compile(
        r"forbes|inc\.com|entrepreneur\.com|fast.?company|harvard.?business|"
        r"wall.?street|new.?york.?times|cnn|bbc|ted.?talk|tedx|huffington|"
        r"business.?insider|usa.?today|nbc|abc|cbs|fox.?news",
        re.I,
    )
    if media_patterns.search(html):
        data["social_proof_media_mentions"] = True
        # Find which ones
        for media in ["Forbes", "Inc", "Entrepreneur", "Fast Company", "Harvard Business Review",
                       "Wall Street Journal", "NYT", "CNN", "BBC", "TEDx", "HuffPost",
                       "Business Insider", "USA Today"]:
            if re.search(re.escape(media), html, re.I):
                data["social_proof_details"].append(f"Mentioned: {media}")

    return data


# =============================================================================
# 5. TIMING / RECENCY SIGNALS
# =============================================================================

def analyze_timing(soup, text, html):
    """Checklist category 5: Timing/recency signals (local HTML analysis)."""
    data = {
        "copyright_year": "",
        "site_looks_recent": "Unknown",
        "now_enrolling_banner": False,
        "new_program_signals": [],
        "hiring_indicators": False,
        "design_assessment": "",
        # Enhanced fields
        "newest_content_date": "",
        "content_dates_found": [],
        "upcoming_events": [],
        "press_mentions": [],
    }

    now = datetime.now()
    current_year = now.year

    # ---- Copyright year ----
    copyright_match = re.search(r"(?:©|\(c\)|copyright)\s*(\d{4})", text, re.I)
    if copyright_match:
        year = int(copyright_match.group(1))
        data["copyright_year"] = str(year)
    else:
        data["copyright_year"] = "Not found"

    # ---- Extract real dates from content ----
    found_dates = []

    # 1. <time> elements with datetime attr
    for time_el in soup.find_all("time"):
        dt_str = time_el.get("datetime", "") or time_el.get_text(strip=True)
        if dt_str:
            try:
                dt = dateutil_parser.parse(dt_str, fuzzy=True, dayfirst=False)
                if 2015 <= dt.year <= current_year + 2:
                    found_dates.append(dt)
            except (ValueError, OverflowError):
                pass

    # 2. Schema.org datePublished / dateModified
    for meta in soup.find_all("meta"):
        prop = meta.get("property", "") or meta.get("itemprop", "")
        if prop in ("datePublished", "dateModified", "article:published_time", "article:modified_time"):
            try:
                dt = dateutil_parser.parse(meta.get("content", ""), fuzzy=True, dayfirst=False)
                if 2015 <= dt.year <= current_year + 2:
                    found_dates.append(dt)
            except (ValueError, OverflowError):
                pass

    # 3. Common blog date class patterns
    date_selectors = [
        "[class*='date']", "[class*='publish']", "[class*='posted']",
        "[class*='entry-date']", "[class*='post-meta']", "[class*='blog-date']",
        "[itemprop='datePublished']", "[itemprop='dateCreated']",
    ]
    for selector in date_selectors:
        for el in soup.select(selector)[:10]:
            txt = el.get_text(strip=True)
            if txt and len(txt) < 80:
                try:
                    dt = dateutil_parser.parse(txt, fuzzy=True, dayfirst=False)
                    if 2015 <= dt.year <= current_year + 2:
                        found_dates.append(dt)
                except (ValueError, OverflowError):
                    pass

    # 4. Regex for inline dates near blog/article context
    date_patterns = [
        r"(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b)",
        r"(\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\b)",
        r"(\b\d{4}-\d{2}-\d{2}\b)",
        r"(\b\d{1,2}/\d{1,2}/\d{4}\b)",
    ]
    for pattern in date_patterns:
        for match in re.finditer(pattern, text, re.I):
            try:
                dt = dateutil_parser.parse(match.group(1), fuzzy=True, dayfirst=False)
                if 2015 <= dt.year <= current_year + 2:
                    found_dates.append(dt)
            except (ValueError, OverflowError):
                pass

    # Deduplicate and sort dates
    unique_dates = sorted(set(d.strftime("%Y-%m-%d") for d in found_dates), reverse=True)
    data["content_dates_found"] = unique_dates[:10]
    if unique_dates:
        data["newest_content_date"] = unique_dates[0]

    # ---- Upcoming events (future dates near event keywords) ----
    event_keywords = re.compile(
        r"webinar|workshop|summit|masterclass|live.?session|conference|"
        r"bootcamp|retreat|virtual.?event|group.?call|q\s*&\s*a|office.?hours",
        re.I,
    )
    for date_obj in found_dates:
        if date_obj.date() >= now.date():
            # Find surrounding context
            date_str = date_obj.strftime("%b %d, %Y")
            # Search for event keywords near this date in text
            for match in event_keywords.finditer(text):
                start = max(0, match.start() - 200)
                end = min(len(text), match.end() + 200)
                context = text[start:end]
                if date_obj.strftime("%Y") in context or date_obj.strftime("%B") in context or date_obj.strftime("%b") in context:
                    event_name = match.group(0).strip()
                    event_entry = f"{date_str} — {event_name}"
                    if event_entry not in data["upcoming_events"]:
                        data["upcoming_events"].append(event_entry)

    # Also detect future dates standalone
    for date_obj in found_dates:
        if date_obj.date() > now.date():
            date_str = date_obj.strftime("%b %d, %Y")
            entry = f"{date_str} — upcoming date found"
            if not any(date_str in e for e in data["upcoming_events"]):
                data["upcoming_events"].append(entry)

    data["upcoming_events"] = data["upcoming_events"][:5]

    # ---- Enrolling / launch banners ----
    enroll_patterns = re.compile(
        r"now.?enrolling|doors.?open|enrollment.?open|join.?now|launching.?soon|"
        r"new.?cohort|next.?cohort|applications.?open|waitlist|sign.?up.?now|"
        r"limited.?spots|spots.?remaining|seats.?left|early.?bird",
        re.I,
    )
    if enroll_patterns.search(text):
        data["now_enrolling_banner"] = True
        matches = enroll_patterns.findall(text)
        data["new_program_signals"].extend(m.strip() for m in matches[:5])

    # New program signals
    new_patterns = re.compile(
        r"new.?program|just.?launched|brand.?new|introducing|coming.?soon|"
        r"beta|founding.?member|charter.?member|pilot.?program",
        re.I,
    )
    if new_patterns.search(text):
        matches = new_patterns.findall(text)
        data["new_program_signals"].extend(m.strip() for m in matches[:5])
    data["new_program_signals"] = list(set(data["new_program_signals"]))

    # ---- Press mentions with dates ----
    press_keywords = re.compile(
        r"(?:featured\s+(?:in|on)|as\s+seen\s+(?:in|on)|press|media|interview|podcast|article|"
        r"published\s+(?:in|on)|quoted\s+(?:in|on)|appeared\s+(?:in|on))",
        re.I,
    )
    press_outlets = re.compile(
        r"Forbes|Inc\b|Entrepreneur|Business\s*Insider|Fast\s*Company|NYT|New\s*York\s*Times|"
        r"Wall\s*Street\s*Journal|WSJ|BBC|CNN|CNBC|TechCrunch|Huffington|Medium|"
        r"USA\s*Today|Reuters|Bloomberg|Yahoo|GQ|Vogue|Cosmopolitan|TIME",
        re.I,
    )
    mentions = []
    for match in press_keywords.finditer(text):
        start = max(0, match.start() - 50)
        end = min(len(text), match.end() + 150)
        context = text[start:end].strip()
        # Try to find a year in this context
        year_match = re.search(r"\b(20[12]\d)\b", context)
        # Try to find an outlet name
        outlet_match = press_outlets.search(context)
        if outlet_match:
            outlet = outlet_match.group(0)
            year = year_match.group(1) if year_match else ""
            entry = f"{year} — {outlet}" if year else outlet
            if entry not in mentions:
                mentions.append(entry)

    # Also scan for outlet names directly
    for match in press_outlets.finditer(text):
        start = max(0, match.start() - 80)
        end = min(len(text), match.end() + 80)
        context = text[start:end]
        outlet = match.group(0)
        year_match = re.search(r"\b(20[12]\d)\b", context)
        year = year_match.group(1) if year_match else ""
        entry = f"{year} — {outlet}" if year else outlet
        if entry not in mentions:
            mentions.append(entry)

    data["press_mentions"] = mentions[:10]

    # ---- Hiring indicators (more precise) ----
    hiring_patterns = re.compile(
        r"we.?re.?hiring|join.?our.?team|open.?position|job.?opening|"
        r"now.?hiring|careers?\s+page|view.?open.?roles",
        re.I,
    )
    if hiring_patterns.search(text):
        data["hiring_indicators"] = True
    # Also check for careers/jobs links
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        if any(kw in href for kw in ["/careers", "/jobs", "greenhouse.io", "lever.co", "workable.com"]):
            data["hiring_indicators"] = True
            break

    # ---- Design assessment ----
    design_signals = []
    if re.search(r"tailwind|_next/|webflow|framer|squarespace|wix|shopify", html, re.I):
        if re.search(r"_next/", html):
            design_signals.append("Next.js")
        if re.search(r"webflow", html, re.I):
            design_signals.append("Webflow")
        if re.search(r"framer", html, re.I):
            design_signals.append("Framer")
        if re.search(r"squarespace", html, re.I):
            design_signals.append("Squarespace")
        if re.search(r"wix", html, re.I):
            design_signals.append("Wix")
        if re.search(r"shopify", html, re.I):
            design_signals.append("Shopify")
        if re.search(r"tailwind", html, re.I):
            design_signals.append("Tailwind CSS")
    if re.search(r"wordpress|wp-content", html, re.I):
        design_signals.append("WordPress")
    if re.search(r"jquery-1\.|bootstrap-[23]", html, re.I):
        design_signals.append("Older framework version")
    if soup.find("meta", attrs={"name": "viewport"}):
        design_signals.append("Responsive")
    if re.search(r"@keyframes|gsap|aos|framer-motion", html, re.I):
        design_signals.append("Animations")

    data["design_assessment"] = ", ".join(design_signals) if design_signals else "Basic HTML"

    # ---- Overall recency assessment (local signals only — network signals added later) ----
    recency_score = 0
    if copyright_match:
        year = int(copyright_match.group(1))
        if year >= current_year:
            recency_score += 2
        elif year >= current_year - 1:
            recency_score += 1
    if data["newest_content_date"]:
        try:
            newest = dateutil_parser.parse(data["newest_content_date"])
            days_ago = (now - newest).days
            if days_ago <= 30:
                recency_score += 3
            elif days_ago <= 90:
                recency_score += 2
            elif days_ago <= 365:
                recency_score += 1
        except (ValueError, OverflowError):
            pass
    if data["upcoming_events"]:
        recency_score += 2
    if data["now_enrolling_banner"]:
        recency_score += 1

    if recency_score >= 4:
        data["site_looks_recent"] = "Yes — actively maintained"
    elif recency_score >= 2:
        data["site_looks_recent"] = "Likely recent"
    elif recency_score >= 1:
        data["site_looks_recent"] = "Possibly outdated"
    else:
        data["site_looks_recent"] = "No recency signals found"

    return data


def analyze_timing_network(url, soup):
    """Timing/recency signals that require network calls (runs in parallel)."""
    data = {
        "sitemap_last_updated": "",
        "sitemap_page_count": 0,
        "http_last_modified": "",
        "domain_age_years": "",
        "domain_created": "",
        "rss_latest_date": "",
        "rss_feed_url": "",
        "rss_post_count": 0,
    }

    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # ---- 1. Sitemap.xml ----
    sitemap_urls_to_try = [
        f"{base}/sitemap.xml",
        f"{base}/sitemap_index.xml",
        f"{base}/sitemap/sitemap.xml",
        f"{base}/wp-sitemap.xml",
    ]
    for sitemap_url in sitemap_urls_to_try:
        try:
            resp = requests.get(sitemap_url, headers=HEADERS, timeout=8)
            if resp.status_code == 200 and "<?xml" in resp.text[:200].lower() or "<urlset" in resp.text[:500].lower() or "<sitemapindex" in resp.text[:500].lower():
                sitemap_soup = BeautifulSoup(resp.text, "html.parser")

                # Handle sitemap index — follow first child
                sitemaps = sitemap_soup.find_all("sitemap")
                if sitemaps:
                    child_loc = sitemaps[0].find("loc")
                    if child_loc:
                        try:
                            child_resp = requests.get(child_loc.get_text(strip=True), headers=HEADERS, timeout=8)
                            if child_resp.status_code == 200:
                                sitemap_soup = BeautifulSoup(child_resp.text, "html.parser")
                        except Exception:
                            pass

                urls = sitemap_soup.find_all("url")
                data["sitemap_page_count"] = len(urls)

                lastmods = []
                for loc in sitemap_soup.find_all("lastmod"):
                    try:
                        dt = dateutil_parser.parse(loc.get_text(strip=True))
                        lastmods.append(dt)
                    except (ValueError, OverflowError):
                        pass

                if lastmods:
                    newest = max(lastmods)
                    data["sitemap_last_updated"] = newest.strftime("%Y-%m-%d")

                break  # Found a working sitemap
        except Exception:
            continue

    # ---- 2. HTTP Last-Modified header ----
    try:
        head_resp = requests.head(url, headers=HEADERS, timeout=8, allow_redirects=True)
        last_mod = head_resp.headers.get("Last-Modified", "")
        if last_mod:
            try:
                dt = dateutil_parser.parse(last_mod)
                data["http_last_modified"] = dt.strftime("%Y-%m-%d")
            except (ValueError, OverflowError):
                data["http_last_modified"] = last_mod
    except Exception:
        pass

    # ---- 3. Domain WHOIS age ----
    if HAS_WHOIS:
        try:
            domain = parsed.netloc.replace("www.", "")
            w = whois.whois(domain)
            creation = w.creation_date
            if isinstance(creation, list):
                creation = creation[0]
            if creation:
                # Strip timezone for comparison with naive datetime
                if hasattr(creation, 'tzinfo') and creation.tzinfo:
                    creation = creation.replace(tzinfo=None)
                data["domain_created"] = creation.strftime("%Y-%m-%d")
                age = (datetime.now() - creation).days / 365.25
                data["domain_age_years"] = round(age, 1)
        except Exception:
            pass

    # ---- 4. RSS / Atom feed discovery & parsing ----
    if HAS_FEEDPARSER:
        feed_url = ""

        # Check <link> tags in the page
        for link in soup.find_all("link", {"type": re.compile(r"rss|atom")}):
            href = link.get("href", "")
            if href:
                feed_url = href if href.startswith("http") else urljoin(url, href)
                break

        # Try common paths if no <link> tag found
        if not feed_url:
            common_feeds = ["/feed", "/rss", "/blog/feed", "/feed.xml", "/atom.xml", "/rss.xml"]
            for path in common_feeds:
                try:
                    test_url = f"{base}{path}"
                    r = requests.get(test_url, headers=HEADERS, timeout=5)
                    if r.status_code == 200 and ("<rss" in r.text[:500].lower() or "<feed" in r.text[:500].lower() or "<atom" in r.text[:500].lower()):
                        feed_url = test_url
                        break
                except Exception:
                    continue

        if feed_url:
            try:
                feed = feedparser.parse(feed_url)
                data["rss_feed_url"] = feed_url
                data["rss_post_count"] = len(feed.entries)

                if feed.entries:
                    # Get the latest entry date
                    for entry in feed.entries[:5]:
                        pub = entry.get("published") or entry.get("updated") or ""
                        if pub:
                            try:
                                dt = dateutil_parser.parse(pub)
                                if not data["rss_latest_date"] or dt.strftime("%Y-%m-%d") > data["rss_latest_date"]:
                                    data["rss_latest_date"] = dt.strftime("%Y-%m-%d")
                            except (ValueError, OverflowError):
                                pass
            except Exception:
                pass

    return data


# =============================================================================
# 6. TECHNICAL GAPS
# =============================================================================

def analyze_technical_gaps(soup, text, html, resp, links, booking_data):
    """Checklist category 6: Technical gaps (email hooks)."""
    data = {
        "broken_links": [],
        "has_ssl": False,
        "ssl_issues": "",
        "outdated_footer_year": False,
        "missing_favicon": False,
        "placeholder_text_found": False,
        "placeholder_details": [],
        "conflicting_ctas": False,
        "cta_count": 0,
        "cta_list": [],
        "booking_page_no_context": False,
        "technical_issues_summary": [],
    }

    # SSL check
    final_url = resp.url
    data["has_ssl"] = final_url.startswith("https://")
    if not data["has_ssl"]:
        data["ssl_issues"] = "Site not using HTTPS"
        data["technical_issues_summary"].append("Missing SSL/HTTPS")

    # Check a sample of links for broken ones
    checked = 0
    for link in links[:20]:
        url = link["url"]
        if url.startswith("mailto:") or url.startswith("tel:") or url.startswith("#"):
            continue
        try:
            r = requests.head(url, headers=HEADERS, timeout=8, allow_redirects=True)
            if r.status_code >= 400:
                data["broken_links"].append({"url": url, "text": link["text"][:50], "status": r.status_code})
        except Exception:
            data["broken_links"].append({"url": url, "text": link["text"][:50], "status": "timeout/error"})
        checked += 1
        if checked >= 10:
            break

    if data["broken_links"]:
        data["technical_issues_summary"].append(f"{len(data['broken_links'])} broken link(s) found")

    # Favicon
    favicon = soup.find("link", rel=re.compile(r"icon", re.I))
    if not favicon:
        data["missing_favicon"] = True
        data["technical_issues_summary"].append("Missing favicon")

    # Outdated footer year
    current_year = datetime.now().year
    footer = soup.find("footer")
    if footer:
        footer_text = footer.get_text()
        year_match = re.search(r"20\d{2}", footer_text)
        if year_match:
            year = int(year_match.group())
            if year < current_year - 1:
                data["outdated_footer_year"] = True
                data["technical_issues_summary"].append(f"Outdated footer year ({year})")

    # Placeholder text / lorem ipsum
    placeholder_patterns = [
        (r"lorem\s+ipsum", "Lorem ipsum placeholder text"),
        (r"your.?(?:company|name|title).?here", "Placeholder: 'your name/company here'"),
        (r"coming\s+soon", "Coming soon placeholder"),
        (r"under\s+construction", "Under construction"),
        (r"example\.com", "example.com reference"),
        (r"insert\s+(?:text|image|content)", "Insert content placeholder"),
    ]
    for pattern, desc in placeholder_patterns:
        if re.search(pattern, text, re.I):
            data["placeholder_text_found"] = True
            data["placeholder_details"].append(desc)
            data["technical_issues_summary"].append(desc)

    # Conflicting CTAs
    cta_patterns = re.compile(
        r"book|schedule|apply|enroll|sign.?up|get.?started|join|download|"
        r"buy.?now|purchase|subscribe|register|contact|free.?call",
        re.I,
    )
    ctas = set()
    for el in soup.find_all(["a", "button"]):
        el_text = el.get_text(strip=True)
        if cta_patterns.search(el_text) and len(el_text) < 60:
            ctas.add(el_text)

    data["cta_count"] = len(ctas)
    data["cta_list"] = list(ctas)[:15]
    if len(ctas) >= 5:
        data["conflicting_ctas"] = True
        data["technical_issues_summary"].append(f"{len(ctas)} different CTAs competing for attention")

    # Booking page context check
    if booking_data.get("booking_links"):
        first_booking = booking_data["booking_links"][0]["url"]
        # Check if booking link goes to external tool with no context
        for tool_patterns in BOOKING_TOOLS.values():
            for pat in tool_patterns:
                if re.search(pat, first_booking, re.I):
                    # It's an external booking tool - check if there's any context page before it
                    _, booking_soup = fetch_page(first_booking)
                    if booking_soup:
                        bt = booking_soup.get_text(strip=True)
                        if len(bt) < 200:
                            data["booking_page_no_context"] = True
                            data["technical_issues_summary"].append("Booking page has no context about what the call is for")
                    break

    if not data["technical_issues_summary"]:
        data["technical_issues_summary"] = ["No major technical issues found"]

    return data


# =============================================================================
# FULL SITE CRAWL
# =============================================================================

MAX_CRAWL_PAGES = 50

def crawl_site(base_url, soup, max_pages=MAX_CRAWL_PAGES):
    """Crawl up to max_pages internal pages and aggregate all data."""
    base_parsed = urlparse(base_url)
    visited = {base_url}
    to_visit = []
    page_data = []

    # Collect all internal links from homepage
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"])
        parsed = urlparse(full)
        clean = parsed._replace(fragment="", query="").geturl()
        if (parsed.netloc == base_parsed.netloc
                and clean not in visited
                and not re.search(r"\.(pdf|jpg|jpeg|png|gif|svg|zip|mp4|mp3|css|js)$", parsed.path, re.I)):
            to_visit.append(clean)
            visited.add(clean)

    # Crawl pages
    crawled = 0
    all_text = soup.get_text(separator=" ", strip=True)
    all_emails = set()
    all_phones = set()
    all_prices = []
    all_testimonials = 0
    all_pages_info = [{"url": base_url, "title": soup.title.string.strip() if soup.title and soup.title.string else ""}]
    hidden_pages = []

    def crawl_page(page_url):
        _, page_soup = fetch_page_simple(page_url, timeout=10)
        if not page_soup:
            return None
        return {
            "url": page_url,
            "soup": page_soup,
            "text": page_soup.get_text(separator=" ", strip=True),
            "html": str(page_soup),
            "title": page_soup.title.string.strip() if page_soup.title and page_soup.title.string else "",
        }

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(crawl_page, url): url for url in to_visit[:max_pages]}
        for future in as_completed(futures):
            result = future.result()
            if not result:
                continue
            crawled += 1
            page_soup = result["soup"]
            page_text = result["text"]
            page_html = result["html"]
            page_url = result["url"]

            all_pages_info.append({"url": page_url, "title": result["title"]})
            all_text += " " + page_text

            # Collect emails
            for a_tag in page_soup.find_all("a", href=True):
                if a_tag["href"].startswith("mailto:"):
                    email = a_tag["href"].replace("mailto:", "").split("?")[0].strip()
                    if email:
                        all_emails.add(email)
            email_pattern = r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
            for e in re.findall(email_pattern, page_text):
                if not e.endswith((".png", ".jpg", ".gif", ".svg")):
                    all_emails.add(e)

            # Collect phones
            phone_matches = re.findall(r"\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", page_text)
            for m in phone_matches:
                cleaned = re.sub(r"[^\d+]", "", m)
                if 7 <= len(cleaned.replace("+", "")) <= 15:
                    all_phones.add(m.strip())

            # Collect prices
            prices = re.findall(r"\$[\d,]+(?:\.\d{2})?(?:\s*[/-]\s*\w+)?", page_text)
            all_prices.extend(prices)

            # Count testimonials
            blockquotes = page_soup.find_all("blockquote")
            testimonial_divs = page_soup.find_all(["div", "section"], class_=re.compile(r"testimonial|review|quote", re.I))
            all_testimonials += max(len(blockquotes), len(testimonial_divs))

            # Find hidden/interesting pages
            path = urlparse(page_url).path.lower()
            if re.search(r"thank|confirm|success|welcome|onboard|intake|apply|application|quiz|assessment", path):
                hidden_pages.append({"url": page_url, "title": result["title"], "type": "hidden/interesting"})

            # Discover more links from this page
            for a in page_soup.find_all("a", href=True):
                full = urljoin(page_url, a["href"])
                parsed = urlparse(full)
                clean = parsed._replace(fragment="", query="").geturl()
                if (parsed.netloc == base_parsed.netloc
                        and clean not in visited
                        and not re.search(r"\.(pdf|jpg|jpeg|png|gif|svg|zip|mp4|mp3|css|js)$", parsed.path, re.I)):
                    visited.add(clean)
                    # We won't crawl these extra links (already hit max), but count them

    # Aggregate price data
    unique_prices = list(set(all_prices))
    price_amounts = []
    for p in unique_prices:
        num = re.search(r"[\d,]+", p)
        if num:
            price_amounts.append(int(num.group().replace(",", "")))

    price_range = "Not visible"
    if price_amounts:
        max_p = max(price_amounts)
        if max_p >= 2000:
            price_range = "High-ticket ($2K+)"
        elif max_p >= 500:
            price_range = "Mid-ticket ($500-$2K)"
        else:
            price_range = "Low-ticket (under $500)"

    return {
        "crawl_pages_found": len(visited),
        "crawl_pages_scraped": crawled + 1,  # +1 for homepage
        "crawl_all_emails": list(all_emails),
        "crawl_all_phones": list(all_phones),
        "crawl_all_prices": unique_prices[:20],
        "crawl_price_range": price_range,
        "crawl_total_testimonials": all_testimonials,
        "crawl_hidden_pages": hidden_pages,
        "crawl_site_map": all_pages_info,
        "crawl_full_text_length": len(all_text),
    }


# =============================================================================
# FACEBOOK AD LIBRARY CHECK
# =============================================================================

def check_facebook_ads(url):
    """Check Facebook Ad Library for active ads on this domain."""
    data = {
        "fb_ads_found": False,
        "fb_ads_count": 0,
        "fb_ads_status": "Not checked",
        "fb_ads_details": [],
    }

    domain = urlparse(url).netloc.replace("www.", "")

    try:
        # Method 1: Search Facebook Ad Library page for the domain
        ad_library_url = f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL&q={domain}&search_type=keyword_unordered"

        if HAS_PLAYWRIGHT:
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.goto(ad_library_url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(5000)

                    content = page.content()
                    page_text = page.inner_text("body") if page.query_selector("body") else ""

                    # Check for ad results
                    # Look for "About X results" or ad cards
                    results_match = re.search(r"(\d+)\s*results?", page_text, re.I)
                    no_results = re.search(r"no\s*results|didn.?t\s*find|0\s*results", page_text, re.I)

                    if no_results:
                        data["fb_ads_found"] = False
                        data["fb_ads_status"] = "No active ads found"
                    elif results_match:
                        count = int(results_match.group(1))
                        if count > 0:
                            data["fb_ads_found"] = True
                            data["fb_ads_count"] = count
                            data["fb_ads_status"] = f"{count} active ad(s) found"
                        else:
                            data["fb_ads_status"] = "No active ads found"
                    else:
                        # Look for ad card elements
                        ad_cards = page.query_selector_all('[class*="ad"], [data-testid*="ad"]')
                        if ad_cards and len(ad_cards) > 0:
                            data["fb_ads_found"] = True
                            data["fb_ads_count"] = len(ad_cards)
                            data["fb_ads_status"] = f"~{len(ad_cards)} ad(s) detected"
                        else:
                            data["fb_ads_status"] = "Could not determine (page structure changed)"

                    # Try to extract ad details
                    if data["fb_ads_found"]:
                        # Get first few ad texts
                        ad_elements = page.query_selector_all('[class*="ad-card"], [class*="_7jyr"]')
                        for i, el in enumerate(ad_elements[:3]):
                            try:
                                ad_text = el.inner_text()[:200]
                                data["fb_ads_details"].append(ad_text)
                            except Exception:
                                pass

                    browser.close()
            except Exception:
                data["fb_ads_status"] = "Check failed (browser error)"
        else:
            # Without Playwright, try a simple heuristic check via the website itself
            # Check if site has Facebook Pixel (already done in ads analysis)
            data["fb_ads_status"] = "Requires headless browser (Playwright not available)"

        # Add the Ad Library link for manual checking
        data["fb_ads_library_url"] = ad_library_url

    except Exception:
        data["fb_ads_status"] = "Check failed"

    return data


# =============================================================================
# SOCIAL MEDIA PROFILE SCRAPING
# =============================================================================

def scrape_social_profiles(soup, html):
    """Scrape social media profiles found on the website for follower counts, activity, etc."""
    data = {
        "social_profiles": {},
        "social_total_followers": 0,
        "social_most_active_platform": "",
        "social_last_post_date": "",
        "social_posting_frequency": "",
        "social_summary": "",
    }

    # Extract social links from the page
    social_patterns = {
        "linkedin": r"linkedin\.com/(?:company|in)/([^/\"?\s]+)",
        "instagram": r"instagram\.com/([^/\"?\s]+)",
        "youtube": r"youtube\.com/(?:@|c/|channel/|user/)([^/\"?\s]+)",
        "twitter": r"(?:twitter\.com|x\.com)/([^/\"?\s]+)",
        "facebook": r"facebook\.com/([^/\"?\s]+)",
        "tiktok": r"tiktok\.com/@([^/\"?\s]+)",
    }

    found_profiles = {}
    for platform, pattern in social_patterns.items():
        match = re.search(pattern, html, re.I)
        if match:
            handle = match.group(1).strip("/").strip()
            if handle and handle not in ("share", "sharer", "intent", "hashtag", "search"):
                found_profiles[platform] = handle

    if not found_profiles and not HAS_PLAYWRIGHT:
        data["social_summary"] = "No social profiles found or Playwright not available"
        return data

    if not HAS_PLAYWRIGHT:
        data["social_profiles"] = {p: {"handle": h, "url": f"https://{p}.com/{h}"} for p, h in found_profiles.items()}
        data["social_summary"] = f"Found {len(found_profiles)} profile(s), headless browser needed for details"
        return data

    total_followers = 0
    platform_activity = {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )

            for platform, handle in found_profiles.items():
                profile = {"handle": handle, "followers": None, "last_post": None, "posts_visible": 0}

                try:
                    page = context.new_page()

                    if platform == "instagram":
                        page.goto(f"https://www.instagram.com/{handle}/", wait_until="domcontentloaded", timeout=15000)
                        page.wait_for_timeout(3000)
                        ig_text = page.content()
                        # Instagram meta tags often have follower count
                        follower_match = re.search(r"([\d,.]+[KkMm]?)\s*(?:Followers|followers)", ig_text)
                        if follower_match:
                            profile["followers"] = follower_match.group(1)
                        # Also try meta description
                        meta_match = re.search(r'([\d,.]+[KkMm]?)\s*Followers', ig_text)
                        if meta_match:
                            profile["followers"] = meta_match.group(1)
                        profile["url"] = f"https://www.instagram.com/{handle}/"

                    elif platform == "linkedin":
                        profile["url"] = f"https://www.linkedin.com/company/{handle}/"
                        page.goto(profile["url"], wait_until="domcontentloaded", timeout=15000)
                        page.wait_for_timeout(3000)
                        li_text = page.inner_text("body") if page.query_selector("body") else ""
                        follower_match = re.search(r"([\d,.]+[KkMm]?)\s*(?:followers|Followers)", li_text)
                        if follower_match:
                            profile["followers"] = follower_match.group(1)
                        employee_match = re.search(r"([\d,.]+[KkMm]?)\s*(?:employees|associated members)", li_text)
                        if employee_match:
                            profile["employees"] = employee_match.group(1)

                    elif platform == "youtube":
                        yt_url = f"https://www.youtube.com/@{handle}"
                        page.goto(yt_url, wait_until="domcontentloaded", timeout=15000)
                        page.wait_for_timeout(3000)
                        yt_text = page.inner_text("body") if page.query_selector("body") else ""
                        sub_match = re.search(r"([\d,.]+[KkMm]?)\s*(?:subscribers|Subscribers)", yt_text)
                        if sub_match:
                            profile["followers"] = sub_match.group(1)
                        # Try to get video count
                        video_match = re.search(r"([\d,.]+)\s*(?:videos|Videos)", yt_text)
                        if video_match:
                            profile["video_count"] = video_match.group(1)
                        profile["url"] = yt_url

                    elif platform == "twitter":
                        profile["url"] = f"https://x.com/{handle}"
                        page.goto(profile["url"], wait_until="domcontentloaded", timeout=15000)
                        page.wait_for_timeout(3000)
                        tw_text = page.inner_text("body") if page.query_selector("body") else ""
                        follower_match = re.search(r"([\d,.]+[KkMm]?)\s*(?:Followers|followers)", tw_text)
                        if follower_match:
                            profile["followers"] = follower_match.group(1)

                    elif platform == "facebook":
                        profile["url"] = f"https://www.facebook.com/{handle}"
                        page.goto(profile["url"], wait_until="domcontentloaded", timeout=15000)
                        page.wait_for_timeout(3000)
                        fb_text = page.inner_text("body") if page.query_selector("body") else ""
                        follower_match = re.search(r"([\d,.]+[KkMm]?)\s*(?:followers|people follow|likes)", fb_text)
                        if follower_match:
                            profile["followers"] = follower_match.group(1)

                    elif platform == "tiktok":
                        profile["url"] = f"https://www.tiktok.com/@{handle}"
                        page.goto(profile["url"], wait_until="domcontentloaded", timeout=15000)
                        page.wait_for_timeout(3000)
                        tt_text = page.inner_text("body") if page.query_selector("body") else ""
                        follower_match = re.search(r"([\d,.]+[KkMm]?)\s*(?:Followers|followers)", tt_text)
                        if follower_match:
                            profile["followers"] = follower_match.group(1)

                    page.close()

                except Exception:
                    profile["error"] = "Could not scrape"
                    try:
                        page.close()
                    except Exception:
                        pass

                # Parse follower count to number
                if profile.get("followers"):
                    f_str = str(profile["followers"]).replace(",", "")
                    multiplier = 1
                    if f_str.upper().endswith("K"):
                        multiplier = 1000
                        f_str = f_str[:-1]
                    elif f_str.upper().endswith("M"):
                        multiplier = 1000000
                        f_str = f_str[:-1]
                    try:
                        count = int(float(f_str) * multiplier)
                        profile["followers_numeric"] = count
                        total_followers += count
                        platform_activity[platform] = count
                    except (ValueError, TypeError):
                        pass

                data["social_profiles"][platform] = profile

            browser.close()

    except Exception:
        data["social_summary"] = "Social scraping failed"
        return data

    data["social_total_followers"] = total_followers

    if platform_activity:
        data["social_most_active_platform"] = max(platform_activity, key=platform_activity.get)

    # Summary
    profile_summaries = []
    for plat, prof in data["social_profiles"].items():
        f = prof.get("followers", "unknown")
        profile_summaries.append(f"{plat}: {f} followers")
    data["social_summary"] = "; ".join(profile_summaries) if profile_summaries else "No profiles scraped"

    return data


# =============================================================================
# AI-POWERED ANALYSIS (Claude API)
# =============================================================================

def generate_ai_analysis(intel):
    """Use Claude to generate a natural-language audit summary with outreach hooks."""
    data = {
        "audit_summary": "",
        "positioning_gaps": "",
        "outreach_hooks": "",
        "overall_score": "",
    }

    if not HAS_CLAUDE or not ANTHROPIC_API_KEY:
        data["audit_summary"] = "Claude API key not configured (set ANTHROPIC_API_KEY env var)"
        return data

    # Build a structured summary of all intel for Claude
    summary_parts = []
    summary_parts.append(f"Website: {intel.get('website_url', 'N/A')}")
    summary_parts.append(f"Booking tool: {intel.get('booking_booking_tool', 'N/A')}")
    summary_parts.append(f"CTA above fold (mobile): {intel.get('mobile_cta_visible_above_fold', 'N/A')}")
    summary_parts.append(f"Clicks to book: {intel.get('booking_clicks_to_book', 'N/A')}")
    summary_parts.append(f"Booking CTA works: {intel.get('booking_booking_cta_works', 'N/A')}")
    summary_parts.append(f"Offer type: {intel.get('offer_offer_type', 'N/A')}")
    summary_parts.append(f"Price range: {intel.get('offer_price_range', 'N/A')}")
    summary_parts.append(f"Target client: {intel.get('offer_target_client', 'N/A')}")
    summary_parts.append(f"Sales model: {intel.get('offer_sales_model', 'N/A')}")
    summary_parts.append(f"Solo/multi coach: {intel.get('offer_solo_or_multi_coach', 'N/A')}")
    summary_parts.append(f"Has active blog: {intel.get('audience_has_active_blog', 'N/A')}")
    summary_parts.append(f"Has podcast: {intel.get('audience_has_podcast', 'N/A')}")
    summary_parts.append(f"Has YouTube: {intel.get('audience_has_youtube', 'N/A')}")
    summary_parts.append(f"Testimonials count: {intel.get('audience_testimonial_count', 'N/A')}")
    summary_parts.append(f"Media mentions: {intel.get('audience_social_proof_media_mentions', 'N/A')}")
    summary_parts.append(f"Multiple audiences: {intel.get('audience_serves_multiple_audiences', 'N/A')}")
    summary_parts.append(f"Copyright year: {intel.get('timing_copyright_year', 'N/A')}")
    summary_parts.append(f"Now enrolling: {intel.get('timing_now_enrolling_banner', 'N/A')}")
    summary_parts.append(f"Hiring indicators: {intel.get('timing_hiring_indicators', 'N/A')}")
    summary_parts.append(f"Running paid ads: {intel.get('ads_likely_running_paid', 'N/A')}")
    summary_parts.append(f"Facebook ads active: {intel.get('fbads_fb_ads_found', 'N/A')} ({intel.get('fbads_fb_ads_count', 0)} ads)")
    summary_parts.append(f"PageSpeed performance: {intel.get('pagespeed_score_performance', 'N/A')}/100")
    summary_parts.append(f"PageSpeed SEO: {intel.get('pagespeed_score_seo', 'N/A')}/100")
    summary_parts.append(f"Mobile load time: {intel.get('mobile_load_time_seconds', 'N/A')}s")
    summary_parts.append(f"Technical issues: {intel.get('gaps_technical_issues_summary', 'N/A')}")
    summary_parts.append(f"Broken links: {len(intel.get('gaps_broken_links', []))}")
    summary_parts.append(f"Missing favicon: {intel.get('gaps_missing_favicon', 'N/A')}")
    summary_parts.append(f"Conflicting CTAs: {intel.get('gaps_conflicting_ctas', 'N/A')} ({intel.get('gaps_cta_count', 0)} CTAs)")
    summary_parts.append(f"Site description: {intel.get('offer_offer_description', 'N/A')}")
    summary_parts.append(f"Pages crawled: {intel.get('crawl_pages_scraped', 'N/A')}")
    summary_parts.append(f"Total prices found: {intel.get('offer_price_points', 'N/A')}")
    summary_parts.append(f"Social profiles: {intel.get('social_social_summary', 'N/A')}")
    summary_parts.append(f"Total social followers: {intel.get('social_social_total_followers', 'N/A')}")

    site_data = "\n".join(summary_parts)

    prompt = f"""You are a coaching business analyst. Analyze this website audit data for a coaching/consulting business and provide a concise, actionable report.

WEBSITE AUDIT DATA:
{site_data}

Respond with EXACTLY this format (keep each section to 2-4 sentences max):

AUDIT SUMMARY:
[Brief assessment of the coaching business's online presence, what they do well, and overall impression]

POSITIONING GAPS:
[Specific weaknesses in their website, booking flow, offer clarity, or marketing that are costing them clients]

OUTREACH HOOKS:
[3 specific, personalized cold email hooks based on the gaps found - things that would get this coach's attention because they address real problems on their site. Write these as actual email opening lines.]

OVERALL SCORE:
[Rate the site X/10 with a one-line justification]"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text

        # Parse sections
        sections = {
            "audit_summary": r"AUDIT SUMMARY:\s*\n(.*?)(?=\nPOSITIONING GAPS:|\Z)",
            "positioning_gaps": r"POSITIONING GAPS:\s*\n(.*?)(?=\nOUTREACH HOOKS:|\Z)",
            "outreach_hooks": r"OUTREACH HOOKS:\s*\n(.*?)(?=\nOVERALL SCORE:|\Z)",
            "overall_score": r"OVERALL SCORE:\s*\n(.*?)(?:\Z)",
        }

        for key, pattern in sections.items():
            match = re.search(pattern, response_text, re.S)
            if match:
                data[key] = match.group(1).strip()

        # Fallback: if parsing fails, just return the full response
        if not any(data[k] for k in sections):
            data["audit_summary"] = response_text

    except Exception as e:
        data["audit_summary"] = f"AI analysis failed: {str(e)}"

    return data


# =============================================================================
# FIND SUBPAGES
# =============================================================================

def find_subpages(soup, base_url):
    keywords = {
        "about": r"about|who-we-are|our-story|company",
        "pricing": r"pricing|plans|packages|investment",
        "services": r"services|programs|offerings|work-with-me|coaching",
        "blog": r"blog|articles|insights|resources|posts",
        "podcast": r"podcast|episodes|show",
        "contact": r"contact|get-in-touch|reach-us",
        "testimonials": r"testimonials|results|success-stories|case-studies",
        "team": r"team|about-us|our-coaches|facilitators",
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
                    if parsed.netloc == base_parsed.netloc or not parsed.netloc:
                        found[key] = full_url
    return found


# =============================================================================
# MAIN SCRAPE
# =============================================================================

def scrape_website(url):
    """Scrape a website against the full checklist using headless browser + PageSpeed."""
    url = normalize_url(url)
    if not url:
        return {"scrape_status": "no_url"}

    intel = {"website_url": url}

    # Fetch homepage with headless browser (renders JS, measures mobile)
    resp, soup = fetch_homepage(url)
    if not soup:
        intel["scrape_status"] = "failed"
        return intel

    intel["scrape_status"] = "success"
    intel["final_url"] = resp.url
    intel["rendered_with"] = "headless_browser" if HAS_PLAYWRIGHT else "requests"
    html = str(soup)
    text = soup.get_text(separator=" ", strip=True)
    links = get_all_links(soup, url)
    subpages = find_subpages(soup, url)
    intel["subpages_found"] = list(subpages.keys())

    # Browser-specific data
    if hasattr(resp, "mobile_cta_visible"):
        intel["mobile_cta_visible_above_fold"] = resp.mobile_cta_visible
        intel["mobile_load_time_seconds"] = resp.mobile_load_time

    # ---- Fast analysis (runs on already-fetched HTML, <1s each) ----
    # 1. Booking infrastructure
    booking = analyze_booking(soup, html, links, url)
    for k, v in booking.items():
        intel[f"booking_{k}"] = v
    if hasattr(resp, "mobile_cta_visible"):
        intel["booking_booking_cta_above_fold"] = resp.mobile_cta_visible

    # 1b. Paid ads detection
    ads = analyze_paid_ads(soup, html)
    for k, v in ads.items():
        intel[f"ads_{k}"] = v

    # 2. Site performance (basic from response)
    perf = analyze_performance(resp, soup, url)
    for k, v in perf.items():
        intel[f"perf_{k}"] = v

    # 3. Offer details
    offer = analyze_offer(soup, text, html, subpages, url)
    for k, v in offer.items():
        intel[f"offer_{k}"] = v

    # 3b. Solo vs multi-coach
    coach = analyze_solo_vs_multi(soup, text, html, subpages, url)
    for k, v in coach.items():
        intel[f"team_{k}"] = v
    intel["offer_solo_or_multi_coach"] = coach["solo_or_multi_coach"]

    # 4. Audience signals
    audience = analyze_audience(soup, text, html, links, subpages)
    for k, v in audience.items():
        intel[f"audience_{k}"] = v

    # 5. Timing/recency signals
    timing = analyze_timing(soup, text, html)
    for k, v in timing.items():
        intel[f"timing_{k}"] = v

    # 6. Technical gaps
    tech_gaps = analyze_technical_gaps(soup, text, html, resp, links, booking)
    for k, v in tech_gaps.items():
        intel[f"gaps_{k}"] = v

    # ---- Slow steps (network/browser calls) — run in parallel ----
    from concurrent.futures import ThreadPoolExecutor, as_completed

    slow_results = {}

    def _run_pagespeed():
        return ("pagespeed", get_pagespeed_insights(url))

    def _run_fb_ads():
        return ("fb_ads", check_facebook_ads(url))

    def _run_crawl():
        return ("crawl", crawl_site(url, soup))

    def _run_social():
        return ("social", scrape_social_profiles(soup, html))

    def _run_timing_network():
        return ("timing_network", analyze_timing_network(url, soup))

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(_run_pagespeed),
            executor.submit(_run_fb_ads),
            executor.submit(_run_crawl),
            executor.submit(_run_social),
            executor.submit(_run_timing_network),
        ]
        for future in as_completed(futures):
            try:
                key, result = future.result()
                slow_results[key] = result
            except Exception:
                pass

    # Merge PageSpeed results
    pagespeed = slow_results.get("pagespeed", {})
    for k, v in pagespeed.items():
        intel[k] = v

    # Merge Facebook Ad Library results
    fb_ads = slow_results.get("fb_ads", {})
    for k, v in fb_ads.items():
        intel[f"fbads_{k}"] = v

    # Merge crawl results + override offer data
    crawl = slow_results.get("crawl", {})
    for k, v in crawl.items():
        intel[k] = v
    if crawl.get("crawl_all_prices"):
        intel["offer_price_points"] = crawl["crawl_all_prices"]
        intel["offer_price_range"] = crawl["crawl_price_range"]
    if crawl.get("crawl_all_emails"):
        intel["all_emails_found"] = crawl["crawl_all_emails"]
    if crawl.get("crawl_all_phones"):
        intel["all_phones_found"] = crawl["crawl_all_phones"]
    if crawl.get("crawl_total_testimonials", 0) > intel.get("audience_testimonial_count", 0):
        intel["audience_testimonial_count"] = crawl["crawl_total_testimonials"]

    # Merge social media results
    social = slow_results.get("social", {})
    profiles = social.pop("social_profiles", {})
    for platform, profile_data in profiles.items():
        intel[f"social_{platform}_url"] = profile_data.get("url", "")
        intel[f"social_{platform}_followers"] = profile_data.get("followers", "Not found")
        if platform == "instagram":
            intel[f"social_{platform}_bio"] = profile_data.get("bio", "")
        if platform == "youtube":
            intel[f"social_{platform}_subscribers"] = profile_data.get("followers", "Not found")
    for k, v in social.items():
        intel[f"social_{k}"] = v

    # Merge timing network results + upgrade recency assessment
    timing_net = slow_results.get("timing_network", {})
    for k, v in timing_net.items():
        intel[f"timing_{k}"] = v

    # Upgrade site_looks_recent with network signals
    recency_boost = 0
    for date_key in ["sitemap_last_updated", "http_last_modified", "rss_latest_date"]:
        date_val = timing_net.get(date_key, "")
        if date_val:
            try:
                dt = dateutil_parser.parse(date_val)
                days = (datetime.now() - dt).days
                if days <= 30:
                    recency_boost += 3
                elif days <= 90:
                    recency_boost += 2
                elif days <= 365:
                    recency_boost += 1
            except (ValueError, OverflowError):
                pass
    if timing_net.get("domain_age_years") and timing_net["domain_age_years"] < 1:
        recency_boost += 1

    if recency_boost >= 3:
        intel["timing_site_looks_recent"] = "Yes — actively maintained"
    elif recency_boost >= 1 and intel.get("timing_site_looks_recent", "") != "Yes — actively maintained":
        intel["timing_site_looks_recent"] = "Likely recent"

    # ---- AI analysis (must run last — needs all other data) ----
    if HAS_CLAUDE and ANTHROPIC_API_KEY:
        ai = generate_ai_analysis(intel)
        for k, v in ai.items():
            intel[f"ai_{k}"] = v

    return intel


# =============================================================================
# CSV PROCESSING (CLI usage)
# =============================================================================

def process_csv(input_path, output_path):
    df = pd.read_csv(input_path)

    website_col = None
    for col in df.columns:
        if any(kw in col.lower() for kw in ["website", "url", "domain", "web"]):
            website_col = col
            break

    if not website_col:
        print("Available columns:", list(df.columns))
        print("ERROR: No website/URL column found.")
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
            idx, intel_data = future.result()
            results.append((idx, intel_data))

    results.sort(key=lambda x: x[0])

    enrichment_rows = []
    for idx, intel_data in results:
        flat = {}
        for key, value in intel_data.items():
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
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else input_file.replace(".csv", "_enriched.csv")
    process_csv(input_file, output_file)
