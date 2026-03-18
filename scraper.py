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
import random
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

# Rotating User-Agent pool — different fingerprint per request
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 OPR/106.0.0.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0",
]

_BROWSER_USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/121.0.6167.66 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.6167.101 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.280 Mobile Safari/537.36",
]


def _get_headers():
    """Get request headers with a random User-Agent."""
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }


# Keep a static copy for session default
HEADERS = _get_headers()


def _random_delay(min_s=0.5, max_s=2.0):
    """Random delay to avoid detection patterns."""
    time.sleep(random.uniform(min_s, max_s))


def _retry_request(method, url, max_retries=2, **kwargs):
    """Make an HTTP request with retry + exponential backoff on 403/429/5xx."""
    kwargs.setdefault("timeout", TIMEOUT)
    original_headers = kwargs.pop("headers", {}) or {}
    for attempt in range(max_retries + 1):
        try:
            req_headers = dict(original_headers)
            req_headers["User-Agent"] = random.choice(_USER_AGENTS)
            kwargs["headers"] = req_headers
            resp = method(url, **kwargs)
            if resp.status_code in (403, 429, 503) and attempt < max_retries:
                wait = (2 ** attempt) + random.uniform(0.5, 1.5)
                time.sleep(wait)
                continue
            return resp
        except Exception:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise
    return None

TIMEOUT = 15
MAX_WORKERS = 25  # Full mode — 15 browser pool, extra workers handle non-browser tasks
MAX_WORKERS_FAST = 20  # Fast mode — no browser, lightweight HTTP only
MAX_CRAWL_FULL = 15  # Reduced from 50 — diminishing returns past 15 pages

PAGESPEED_API_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PAGESPEED_API_KEY = os.environ.get("PAGESPEED_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# =============================================================================
# BROWSER POOL — Reuse Playwright browsers across sites instead of launching fresh
# =============================================================================
import atexit
import queue
import threading

class BrowserPool:
    """Pool of reusable Playwright browser instances for concurrent scraping.

    Each browser is created with its own Playwright instance on the calling thread.
    Browsers are reused across requests but must be used from the thread that acquires them.
    """

    def __init__(self, size=4):
        self._size = size
        self._pool = queue.Queue()
        self._playwright_instances = []
        self._lock = threading.Lock()
        self._initialized = False

    def _init_pool(self):
        """Lazily initialize browser pool on first use."""
        with self._lock:
            if self._initialized:
                return
            if not HAS_PLAYWRIGHT:
                self._initialized = True
                return
            for _ in range(self._size):
                try:
                    p = sync_playwright().start()
                    browser = p.chromium.launch(headless=True)
                    self._playwright_instances.append(p)
                    self._pool.put(browser)
                except Exception:
                    break
            self._initialized = True

    def acquire(self, timeout=30):
        """Get a browser from the pool. Blocks if all are in use."""
        self._init_pool()
        try:
            browser = self._pool.get(timeout=timeout)
            # Health check — verify browser is still connected
            try:
                browser.contexts  # Will throw if browser crashed
                return browser
            except Exception:
                # Browser is dead, try to get another
                return self._pool.get(timeout=5) if not self._pool.empty() else None
        except queue.Empty:
            return None

    def release(self, browser):
        """Return a browser to the pool."""
        if browser:
            try:
                browser.contexts  # Verify still alive before returning
                self._pool.put(browser)
            except Exception:
                pass  # Discard dead browsers

    def shutdown(self):
        """Close all browsers and Playwright instances. Suppresses exit errors."""
        import warnings
        warnings.filterwarnings("ignore")
        while not self._pool.empty():
            try:
                browser = self._pool.get_nowait()
                browser.close()
            except Exception:
                pass
        for p in self._playwright_instances:
            try:
                p.stop()
            except Exception:
                pass
        self._playwright_instances.clear()
        self._initialized = False


# Global browser pool — shared across all scrape_website() calls
_browser_pool = BrowserPool(size=15)
atexit.register(_browser_pool.shutdown)

# Shared requests.Session for connection pooling (DNS cache + keep-alive)
_http_session = requests.Session()
_http_session.headers.update(_get_headers())


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
    """Fetch a page with requests (no JS). Uses retry + rotating UA."""
    try:
        resp = _retry_request(_http_session.get, url, timeout=timeout, allow_redirects=True)
        if resp is None:
            return None, None
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
    """Fetch a page with headless Chromium (renders JS). Uses browser pool for reuse."""
    if not HAS_PLAYWRIGHT:
        return fetch_page_simple(url)

    browser = _browser_pool.acquire(timeout=30)
    if not browser:
        return fetch_page_simple(url)

    context = None
    try:
        context = browser.new_context(
            viewport={"width": 390, "height": 844},  # Mobile viewport
            user_agent=random.choice(_BROWSER_USER_AGENTS),
        )
        page = context.new_page()

        start = time.time()
        resp = page.goto(url, wait_until="networkidle", timeout=timeout)
        page.wait_for_timeout(2000)
        elapsed = time.time() - start

        content = page.content()
        final_url = page.url
        status = resp.status if resp else 200
        headers = {}
        if resp:
            headers = {h["name"]: h["value"] for h in resp.headers_array()} if hasattr(resp, "headers_array") else {}

        mobile_cta_visible = False
        try:
            cta_el = page.query_selector('a[href*="book"], a[href*="schedule"], a[href*="calendly"], a[href*="call"], button:has-text("Book"), button:has-text("Schedule"), button:has-text("Get Started")')
            if cta_el and cta_el.is_visible():
                box = cta_el.bounding_box()
                if box and box["y"] < 844:
                    mobile_cta_visible = True
        except Exception:
            pass

        context.close()
        context = None

        result = BrowserResult(final_url, content, status, elapsed, headers)
        result.mobile_cta_visible = mobile_cta_visible
        result.mobile_load_time = round(elapsed, 2)

        soup = BeautifulSoup(content, "html.parser")
        return result, soup
    except Exception:
        return fetch_page_simple(url)
    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            _browser_pool.release(browser)
        except Exception:
            pass


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
        resp = _http_session.get(PAGESPEED_API_URL, params=params, timeout=60)
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


def get_external_links(soup, base_url):
    """Get all external (cross-domain) links — reveals multi-domain funnels."""
    external = []
    base_parsed = urlparse(base_url)
    base_domain = base_parsed.netloc.replace("www.", "")
    seen_domains = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        ext_domain = parsed.netloc.replace("www.", "")

        # Skip same domain, empty, anchors, mailto, tel
        if not ext_domain or ext_domain == base_domain:
            continue
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        # Skip common non-funnel externals
        skip_domains = [
            "facebook.com", "instagram.com", "twitter.com", "x.com",
            "linkedin.com", "youtube.com", "youtu.be", "tiktok.com",
            "pinterest.com", "apple.com", "spotify.com", "google.com",
            "trustpilot.com", "g2.com",
        ]
        if any(sd in ext_domain for sd in skip_domains):
            continue

        text = a.get_text(strip=True)
        external.append({
            "text": text[:80],
            "url": full,
            "domain": ext_domain,
        })
        seen_domains.add(ext_domain)

    return external, list(seen_domains)


def detect_secondary_funnels(external_links, external_domains):
    """Identify if prospect has a separate funnel/sales domain."""
    data = {
        "has_secondary_domain": False,
        "secondary_domains": [],
        "secondary_funnel_platform": "None",
        "secondary_domain_links": [],
    }

    funnel_platforms = {
        "ClickFunnels": ["clickfunnels.com", "myclickfunnels.com"],
        "Kajabi": ["kajabi.com", "mykajabi.com"],
        "Kartra": ["kartra.com"],
        "ThriveCart": ["thrivecart.com"],
        "Teachable": ["teachable.com"],
        "Podia": ["podia.com"],
        "Gumroad": ["gumroad.com"],
        "Skool": ["skool.com"],
        "Stan.store": ["stan.store"],
    }

    # Filter out common non-funnel domains (payment, analytics, CDN, social, etc.)
    ignore_domains = {
        "stripe.com", "paypal.com", "square.com", "gstatic.com", "googleapis.com",
        "google.com", "googletagmanager.com", "google-analytics.com", "doubleclick.net",
        "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
        "youtube.com", "tiktok.com", "pinterest.com", "vimeo.com",
        "cloudflare.com", "cdn.jsdelivr.net", "unpkg.com", "fonts.googleapis.com",
        "gravatar.com", "wp.com", "wordpress.com", "wordpress.org",
        "mailchimp.com", "convertkit.com", "hubspot.com", "aweber.com",
        "intercom.io", "crisp.chat", "drift.com", "zendesk.com",
        "hotjar.com", "clarity.ms", "segment.com", "mixpanel.com",
        "sentry.io", "bugsnag.com", "newrelic.com",
        "calendly.com", "acuityscheduling.com", "tidycal.com",
        "typeform.com", "jotform.com", "wufoo.com",
    }

    # Filter to meaningful external domains
    meaningful_domains = [d for d in external_domains if not any(d.endswith(ig) for ig in ignore_domains)]

    if meaningful_domains:
        data["has_secondary_domain"] = True
        data["secondary_domains"] = meaningful_domains[:10]
        data["secondary_domain_links"] = [l for l in external_links if l.get("domain") in meaningful_domains][:10]

        # Check if any external links go to known funnel platforms
        all_urls = " ".join(l["url"] for l in external_links)
        for platform, domains in funnel_platforms.items():
            for d in domains:
                if d in all_urls:
                    data["secondary_funnel_platform"] = platform
                    break

    return data


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
    KNOWN_BOOKING_DOMAINS = [
        "calendly.com", "acuityscheduling.com", "squareup.com",
        "cal.com", "typeform.com", "meetings.hubspot.com",
        "savvycal.com", "tidycal.com", "oncehub.com",
        "scheduleonce.com", "dubsado.com", "booklikeaboss.com",
        "youcanbook.me",
    ]

    if booking_links:
        test_url = booking_links[0]["url"]
        parsed_test = urlparse(test_url)
        test_domain = parsed_test.netloc.replace("www.", "")

        # Known booking tools often block HEAD requests — trust the link exists
        is_known_tool = any(d in test_domain for d in KNOWN_BOOKING_DOMAINS)

        try:
            # Try HEAD first
            r = _http_session.head(test_url, timeout=10, allow_redirects=True)
            if r.status_code < 400:
                data["booking_cta_works"] = "Yes"
            elif is_known_tool and r.status_code in (400, 403, 405):
                # Known tools often block HEAD — retry with GET
                try:
                    r2 = _http_session.get(test_url, timeout=10, allow_redirects=True)
                    if r2.status_code < 400:
                        data["booking_cta_works"] = "Yes"
                    else:
                        data["booking_cta_works"] = f"Likely works (known tool, HTTP {r2.status_code} on GET — verify manually)"
                except Exception:
                    data["booking_cta_works"] = f"Likely works (known tool, connection issue — verify manually)"
            else:
                data["booking_cta_works"] = f"Possibly broken (HTTP {r.status_code} — verify manually)"
        except Exception:
            if is_known_tool:
                data["booking_cta_works"] = "Likely works (known tool, connection blocked — verify manually)"
            else:
                data["booking_cta_works"] = "Could not verify (connection failed)"

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
            resp = _http_session.get(sitemap_url, timeout=8)
            if resp.status_code == 200 and ("<?xml" in resp.text[:200].lower() or "<urlset" in resp.text[:500].lower() or "<sitemapindex" in resp.text[:500].lower()):
                sitemap_soup = BeautifulSoup(resp.text, "html.parser")

                # Handle sitemap index — follow first child
                sitemaps = sitemap_soup.find_all("sitemap")
                if sitemaps:
                    child_loc = sitemaps[0].find("loc")
                    if child_loc:
                        try:
                            child_resp = _http_session.get(child_loc.get_text(strip=True), timeout=8)
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
        head_resp = _http_session.head(url, timeout=8, allow_redirects=True)
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
                    r = _http_session.get(test_url, timeout=5)
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
        "cta_unique_actions": 0,
        "cta_list": [],
        "cta_destinations": [],
        "cta_unique_destinations": 0,
        "cta_destination_mismatch": False,
        "cta_mismatch_hook": "",
        "booking_page_no_context": False,
        "technical_issues_summary": [],
    }

    # SSL check
    final_url = resp.url
    data["has_ssl"] = final_url.startswith("https://")
    if not data["has_ssl"]:
        data["ssl_issues"] = "Site not using HTTPS"
        data["technical_issues_summary"].append("Missing SSL/HTTPS")

    # Check a sample of links for broken ones — in parallel
    check_urls = []
    for link in links[:20]:
        link_url = link["url"]
        if link_url.startswith("mailto:") or link_url.startswith("tel:") or link_url.startswith("#"):
            continue
        check_urls.append(link)
        if len(check_urls) >= 10:
            break

    def _check_link(link):
        try:
            r = _http_session.head(link["url"], timeout=5, allow_redirects=True)
            if r.status_code >= 400:
                return {"url": link["url"], "text": link["text"][:50], "status": r.status_code}
        except Exception:
            return {"url": link["url"], "text": link["text"][:50], "status": "timeout/error"}
        return None

    with ThreadPoolExecutor(max_workers=5) as executor:
        for result in executor.map(_check_link, check_urls):
            if result:
                data["broken_links"].append(result)

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

    # Conflicting CTAs — deduplicate by destination URL, not just text
    cta_patterns = re.compile(
        r"book|schedule|apply|enroll|sign.?up|get.?started|join|download|"
        r"buy.?now|purchase|subscribe|register|contact|free.?call",
        re.I,
    )
    cta_by_destination = {}  # url -> set of button texts
    cta_texts = set()
    for el in soup.find_all(["a", "button"]):
        el_text = el.get_text(strip=True)
        if cta_patterns.search(el_text) and len(el_text) < 60:
            cta_texts.add(el_text)
            href = el.get("href", "")
            if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                dest = urljoin(resp.url, href).rstrip("/").split("?")[0]  # normalize
                if dest not in cta_by_destination:
                    cta_by_destination[dest] = set()
                cta_by_destination[dest].add(el_text)

    unique_destinations = len(cta_by_destination)
    unique_texts = len(cta_texts)

    data["cta_count"] = unique_texts
    data["cta_unique_actions"] = unique_destinations
    data["cta_list"] = list(cta_texts)[:15]

    # Only flag as conflicting if there are 3+ DIFFERENT destinations
    if unique_destinations >= 4:
        data["conflicting_ctas"] = True
        data["technical_issues_summary"].append(
            f"{unique_texts} CTAs pointing to {unique_destinations} different destinations"
        )
    elif unique_destinations >= 2 and unique_texts >= 5:
        data["conflicting_ctas"] = "Mild"
        data["technical_issues_summary"].append(
            f"{unique_texts} CTA buttons but only {unique_destinations} unique destinations — reinforcement, not confusion"
        )

    # CTA destination tracking (Enhancement 5)
    cta_destinations = []
    destination_roots = set()
    for el in soup.find_all(["a", "button"]):
        el_text = el.get_text(strip=True)
        href = el.get("href", "")
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        if cta_patterns.search(el_text) and len(el_text) < 60:
            full_url = href if href.startswith("http") else urljoin(resp.url, href)
            try:
                parsed_dest = urlparse(full_url)
                domain = parsed_dest.netloc.replace("www.", "")
                root = f"{domain}{parsed_dest.path.rstrip('/').split('/')[0] if parsed_dest.path else ''}"
                destination_roots.add(root)
                cta_destinations.append({"text": el_text, "url": full_url, "domain": domain})
            except Exception:
                pass

    data["cta_destinations"] = cta_destinations[:15]
    data["cta_unique_destinations"] = len(destination_roots)
    if len(destination_roots) >= 3:
        data["cta_destination_mismatch"] = True
        dest_summary = ", ".join(sorted(destination_roots)[:5])
        data["cta_mismatch_hook"] = (
            f"Your homepage sends visitors to {len(destination_roots)} different places — "
            f"{dest_summary}. That decision fatigue is splitting your conversion."
        )
        data["technical_issues_summary"].append(f"CTAs point to {len(destination_roots)} different destinations")

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
# ENHANCEMENT 1: ANTI-SIGNAL DETECTION (Automation Platforms)
# =============================================================================

def detect_automation_platforms(soup, html):
    """Detect GoHighLevel, ClickFunnels, Kajabi, etc. — prospects who already solved booking."""
    data = {
        "has_gohighlevel": False,
        "has_clickfunnels": False,
        "has_kajabi": False,
        "has_kartra": False,
        "has_keap_infusionsoft": False,
        "has_full_automation_platform": False,
        "automation_platform_name": "",
    }

    platforms = {
        "has_gohighlevel": [r"gohighlevel\.com", r"msgsndr\.com", r"highlevel", r"\.myfunnels\.com", r"leadconnectorhq\.com", r"app\.ghl\."],
        "has_clickfunnels": [r"clickfunnels\.com", r"cf-pages", r"cfimg\.com", r"clickfunnels2\.com"],
        "has_kajabi": [r"kajabi\.com", r"kajabi-storefronts", r"checkout\.kajabi"],
        "has_kartra": [r"kartra\.com", r"app\.kartra"],
        "has_keap_infusionsoft": [r"keap\.com", r"infusionsoft\.com", r"keap-app"],
    }

    platform_names = {
        "has_gohighlevel": "GoHighLevel",
        "has_clickfunnels": "ClickFunnels",
        "has_kajabi": "Kajabi",
        "has_kartra": "Kartra",
        "has_keap_infusionsoft": "Keap/Infusionsoft",
    }

    for key, patterns in platforms.items():
        for pattern in patterns:
            if re.search(pattern, html, re.I):
                data[key] = True
                if not data["has_full_automation_platform"]:
                    data["has_full_automation_platform"] = True
                    data["automation_platform_name"] = platform_names[key]
                break

    return data


# =============================================================================
# ENHANCEMENT 7: SCHEMA.ORG / STRUCTURED DATA GAP
# =============================================================================

def check_structured_data(soup):
    """Check for Schema.org structured data markup."""
    data = {
        "has_schema_markup": False,
        "schema_types_found": [],
        "has_business_schema": False,
        "has_faq_schema": False,
        "has_review_schema": False,
        "has_service_schema": False,
        "schema_gap_hook": "",
    }

    business_types = {"LocalBusiness", "ProfessionalService", "Organization", "Person",
                      "EducationalOrganization", "MedicalBusiness", "Physician", "Dentist"}
    review_types = {"AggregateRating", "Review"}
    service_types = {"Service", "Product", "Offer", "Course"}

    ld_scripts = soup.find_all("script", {"type": "application/ld+json"})
    all_types = set()

    for script in ld_scripts:
        try:
            content = script.string or ""
            parsed = json.loads(content)

            def extract_types(obj):
                if isinstance(obj, dict):
                    t = obj.get("@type", "")
                    if isinstance(t, list):
                        all_types.update(t)
                    elif t:
                        all_types.add(t)
                    for v in obj.values():
                        extract_types(v)
                elif isinstance(obj, list):
                    for item in obj:
                        extract_types(item)

            extract_types(parsed)
        except (json.JSONDecodeError, TypeError):
            continue

    if all_types:
        data["has_schema_markup"] = True
        data["schema_types_found"] = sorted(all_types)
        data["has_business_schema"] = bool(all_types & business_types)
        data["has_faq_schema"] = "FAQPage" in all_types
        data["has_review_schema"] = bool(all_types & review_types)
        data["has_service_schema"] = bool(all_types & service_types)

    return data


# =============================================================================
# ENHANCEMENT 6: REDIRECT CHAIN DEPTH ON BOOKING CTAs
# =============================================================================

def check_booking_redirects(booking_data):
    """Check how many redirects the booking CTA goes through."""
    data = {
        "booking_redirect_count": 0,
        "booking_redirect_chain": [],
        "booking_redirect_excessive": False,
        "booking_redirect_hook": "",
    }

    links = booking_data.get("booking_links", [])
    if not links:
        return data

    target_url = links[0].get("url", "")
    if not target_url or target_url.startswith("#"):
        return data

    try:
        resp = _http_session.get(target_url, timeout=10, allow_redirects=True)
        chain = [r.url for r in resp.history] + [resp.url]
        data["booking_redirect_count"] = len(resp.history)
        data["booking_redirect_chain"] = chain

        if len(resp.history) >= 2:
            data["booking_redirect_excessive"] = True
            count = len(resp.history)
            data["booking_redirect_hook"] = (
                f"Your 'Book a Call' link goes through {count} redirects before reaching "
                f"your scheduling page — that adds ~{count}s of dead loading time where visitors drop off."
            )
    except Exception:
        pass

    return data


# =============================================================================
# ENHANCEMENT 4: FORM FIELD COUNTER
# =============================================================================

def count_form_fields(soup, html, subpages, base_url):
    """Count form fields across homepage and key subpages to detect high-friction forms."""
    data = {
        "forms_found": 0,
        "longest_form_fields": 0,
        "longest_form_url": "",
        "longest_form_type": "",
        "forms_detail": [],
        "high_friction_form": False,
        "form_friction_hook": "",
    }

    def _analyze_forms(page_soup, page_url):
        """Analyze all forms on a page and return list of form info dicts."""
        results = []
        for form in page_soup.find_all("form"):
            inputs = form.find_all("input")
            field_count = 0
            for inp in inputs:
                input_type = (inp.get("type") or "text").lower()
                if input_type not in ("hidden", "submit", "button", "reset", "image"):
                    field_count += 1
            field_count += len(form.find_all("select"))
            field_count += len(form.find_all("textarea"))

            if field_count == 0:
                continue

            # Determine form type
            form_text = (form.get("action", "") + " " + form.get("id", "") + " " +
                         " ".join(form.get("class", [])) + " " + form.get_text(" ", strip=True)[:200]).lower()
            if any(kw in form_text for kw in ["contact", "message", "inquiry", "get in touch"]):
                form_type = "contact"
            elif any(kw in form_text for kw in ["apply", "application"]):
                form_type = "application"
            elif any(kw in form_text for kw in ["intake", "onboard"]):
                form_type = "intake"
            elif any(kw in form_text for kw in ["book", "schedule", "calendar"]):
                form_type = "booking"
            else:
                form_type = "unknown"

            results.append({
                "url": page_url,
                "field_count": field_count,
                "form_type": form_type,
            })
        return results

    # Analyze homepage forms
    all_forms = _analyze_forms(soup, base_url)

    # Check key subpages
    form_pages = ["/apply", "/application", "/intake", "/contact", "/work-with-me", "/get-started"]
    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    pages_checked = 0
    for path in form_pages:
        if pages_checked >= 5:
            break
        page_url = f"{base}{path}"
        try:
            _, page_soup = fetch_page_simple(page_url, timeout=8)
            if page_soup:
                all_forms.extend(_analyze_forms(page_soup, page_url))
                pages_checked += 1
        except Exception:
            continue

    # Also check subpages from find_subpages
    for sp_type, sp_url in subpages.items():
        if pages_checked >= 5:
            break
        if any(kw in sp_type for kw in ["apply", "contact", "intake", "work"]):
            try:
                _, page_soup = fetch_page_simple(sp_url, timeout=8)
                if page_soup:
                    all_forms.extend(_analyze_forms(page_soup, sp_url))
                    pages_checked += 1
            except Exception:
                continue

    data["forms_found"] = len(all_forms)
    data["forms_detail"] = all_forms

    if all_forms:
        longest = max(all_forms, key=lambda f: f["field_count"])
        data["longest_form_fields"] = longest["field_count"]
        data["longest_form_url"] = longest["url"]
        data["longest_form_type"] = longest["form_type"]

        if longest["field_count"] >= 8:
            data["high_friction_form"] = True
            ft = longest["form_type"]
            fc = longest["field_count"]
            if fc >= 12:
                data["form_friction_hook"] = (
                    f"Your {ft} form asks for {fc} pieces of information before someone "
                    f"can even talk to you — I'd estimate that's filtering out 40-60% of interested prospects."
                )
            else:
                data["form_friction_hook"] = (
                    f"Your {ft} form has {fc} fields — completion rates typically drop "
                    f"~10% per field after 5. That's a lot of interested leads abandoning before they finish."
                )

    return data


# =============================================================================
# ENHANCEMENT 3: BOOKING PAGE BRANDING CHECK
# =============================================================================

def check_booking_page_branding(booking_data, base_url):
    """Check if the booking page (Calendly, etc.) has custom branding or is fully default."""
    data = {
        "booking_page_fetched": False,
        "booking_page_url": "",
        "booking_page_is_branded": False,
        "booking_page_has_description": False,
        "booking_page_has_photo": False,
        "booking_page_has_custom_colors": False,
        "booking_page_word_count": 0,
        "booking_page_branding_score": "",
        "booking_page_hook": "",
    }

    tool = booking_data.get("booking_tool", "")
    if not tool or tool in ("None detected", "Contact form"):
        return data

    links = booking_data.get("booking_links", [])
    if not links:
        return data

    # Find the first external booking link
    booking_url = ""
    for link in links:
        href = link.get("url", "")
        if any(re.search(pat, href, re.I) for pats in BOOKING_TOOLS.values() for pat in pats):
            booking_url = href
            break

    if not booking_url:
        return data

    data["booking_page_url"] = booking_url

    try:
        _, bp_soup = fetch_page_simple(booking_url, timeout=10)
        if not bp_soup:
            return data

        data["booking_page_fetched"] = True
        bp_text = bp_soup.get_text(separator=" ", strip=True)
        data["booking_page_word_count"] = len(bp_text.split())

        # Check for description (>20 words of content)
        if data["booking_page_word_count"] > 20:
            data["booking_page_has_description"] = True

        # Check for profile photo/avatar
        for img in bp_soup.find_all("img"):
            alt = (img.get("alt", "") or "").lower()
            src = (img.get("src", "") or "").lower()
            if any(kw in alt for kw in ["photo", "avatar", "profile", "headshot", "portrait"]):
                data["booking_page_has_photo"] = True
                break
            if any(kw in src for kw in ["avatar", "profile", "photo", "headshot"]):
                data["booking_page_has_photo"] = True
                break

        # Check for custom colors (inline styles with color values)
        bp_html = str(bp_soup)
        color_matches = re.findall(r'(?:background-color|color)\s*:\s*#[0-9a-fA-F]{3,6}', bp_html)
        if len(color_matches) >= 2:
            data["booking_page_has_custom_colors"] = True

        # Branding score
        brand_signals = sum([
            data["booking_page_has_description"],
            data["booking_page_has_photo"],
            data["booking_page_has_custom_colors"],
        ])

        if brand_signals == 0 and data["booking_page_word_count"] < 20:
            data["booking_page_branding_score"] = "default"
            data["booking_page_hook"] = (
                f"Your {tool} page is completely default — no photo, no description of what the "
                f"call covers. Visitors land on a blank scheduling widget with zero context about "
                f"why they should show up."
            )
        elif brand_signals <= 2:
            data["booking_page_branding_score"] = "partially_branded"
            missing = []
            if not data["booking_page_has_photo"]:
                missing.append("a profile photo")
            if not data["booking_page_has_description"]:
                missing.append("a description of the call")
            if not data["booking_page_has_custom_colors"]:
                missing.append("custom branding colors")
            missing_str = " and ".join(missing)
            data["booking_page_hook"] = (
                f"Your booking page has some customization but is still missing {missing_str} — "
                f"that gap between your polished site and a bare scheduling page costs you show-ups."
            )
        else:
            data["booking_page_branding_score"] = "well_branded"

        data["booking_page_is_branded"] = brand_signals >= 2

    except Exception:
        pass

    return data


# =============================================================================
# ENHANCEMENT 2: CONFIRMATION/THANK-YOU PAGE QUALITY AUDIT
# =============================================================================

def audit_confirmation_pages(crawl_data, base_url):
    """Audit confirmation/thank-you pages for quality — thin pages drive no-shows."""
    data = {
        "confirmation_pages_found": 0,
        "confirmation_pages": [],
        "worst_confirmation_quality": "none_found",
        "confirmation_hook": "",
    }

    hidden_pages = crawl_data.get("crawl_hidden_pages", [])
    if not hidden_pages:
        return data

    confirm_pages = []
    for page in hidden_pages:
        page_url = page if isinstance(page, str) else page.get("url", "")
        if not page_url:
            continue
        if re.search(r"thank|confirm|success|welcome", page_url, re.I):
            confirm_pages.append(page_url)

    # Also manually check common confirmation paths
    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    for path in ["/thank-you", "/thanks", "/confirmation", "/success", "/welcome", "/booking-confirmed"]:
        test_url = f"{base}{path}"
        if test_url not in confirm_pages:
            try:
                resp = _http_session.head(test_url, timeout=5, allow_redirects=True)
                if resp.status_code == 200:
                    confirm_pages.append(test_url)
            except Exception:
                continue

    if not confirm_pages:
        return data

    data["confirmation_pages_found"] = len(confirm_pages)
    worst_quality = "good"

    for page_url in confirm_pages[:3]:  # Check up to 3
        page_info = {
            "url": page_url,
            "word_count": 0,
            "has_video": False,
            "has_what_to_expect": False,
            "has_calendar_add": False,
            "has_next_steps": False,
            "has_branding": False,
            "quality_score": "",
        }

        try:
            _, page_soup = fetch_page_simple(page_url, timeout=8)
            if not page_soup:
                continue

            page_text = page_soup.get_text(separator=" ", strip=True)
            page_html = str(page_soup)
            page_info["word_count"] = len(page_text.split())

            # Video check
            if page_soup.find("video") or re.search(r'<iframe[^>]*(?:youtube|vimeo|wistia|loom)', page_html, re.I):
                page_info["has_video"] = True

            # What to expect
            if re.search(r"what to expect|before your call|prepare for|here.?s what", page_text, re.I):
                page_info["has_what_to_expect"] = True

            # Calendar add
            if re.search(r"\.ics|add to calendar|google\.com/calendar|outlook\.com", page_html, re.I):
                page_info["has_calendar_add"] = True

            # Next steps
            if re.search(r"next step|what happens next|here.?s what to do", page_text, re.I):
                page_info["has_next_steps"] = True

            # Branding
            logos = page_soup.find_all("img", class_=re.compile(r"logo", re.I))
            if logos or page_soup.find("img", alt=re.compile(r"logo", re.I)):
                page_info["has_branding"] = True

            # Quality scoring
            enhancements = sum([
                page_info["has_video"],
                page_info["has_what_to_expect"],
                page_info["has_next_steps"],
                page_info["has_calendar_add"],
            ])

            if page_info["word_count"] < 50 and enhancements == 0:
                page_info["quality_score"] = "generic"
            elif page_info["word_count"] > 150 and enhancements >= 2:
                page_info["quality_score"] = "good"
            else:
                page_info["quality_score"] = "basic"

            # Track worst quality
            quality_rank = {"generic": 0, "basic": 1, "good": 2}
            if quality_rank.get(page_info["quality_score"], 2) < quality_rank.get(worst_quality, 2):
                worst_quality = page_info["quality_score"]

        except Exception:
            continue

        data["confirmation_pages"].append(page_info)

    data["worst_confirmation_quality"] = worst_quality

    # Hook generation
    if worst_quality == "generic":
        worst_page = next((p for p in data["confirmation_pages"] if p["quality_score"] == "generic"), None)
        wc = worst_page["word_count"] if worst_page else 0
        data["confirmation_hook"] = (
            f"Your confirmation page after booking is just {wc} words with no context about "
            f"what the call is for — that's where 30-50% of no-shows start."
        )
    elif worst_quality == "basic":
        data["confirmation_hook"] = (
            "Your post-booking page is missing a pre-call sequence — no video, no 'what to expect', "
            "no prep steps. That gap drives no-shows."
        )

    return data


# =============================================================================
# FULL SITE CRAWL
# =============================================================================

MAX_CRAWL_PAGES = 15  # Reduced from 50 — 95% of signals found in first 15 pages

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
            browser = _browser_pool.acquire(timeout=15)
            if browser:
                context = None
                try:
                    context = browser.new_context(user_agent=random.choice(_USER_AGENTS))
                    page = context.new_page()
                    page.goto(ad_library_url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(5000)

                    content = page.content()
                    page_text = page.inner_text("body") if page.query_selector("body") else ""

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
                        ad_cards = page.query_selector_all('[class*="ad"], [data-testid*="ad"]')
                        if ad_cards and len(ad_cards) > 0:
                            data["fb_ads_found"] = True
                            data["fb_ads_count"] = len(ad_cards)
                            data["fb_ads_status"] = f"~{len(ad_cards)} ad(s) detected"
                        else:
                            data["fb_ads_status"] = "Could not determine (page structure changed)"

                    if data["fb_ads_found"]:
                        ad_elements = page.query_selector_all('[class*="ad-card"], [class*="_7jyr"]')
                        for i, el in enumerate(ad_elements[:3]):
                            try:
                                ad_text = el.inner_text()[:200]
                                data["fb_ads_details"].append(ad_text)
                            except Exception:
                                pass

                    context.close()
                    context = None
                except Exception:
                    data["fb_ads_status"] = "Check failed (browser error)"
                finally:
                    try:
                        if context:
                            context.close()
                    except Exception:
                        pass
                    try:
                        _browser_pool.release(browser)
                    except Exception:
                        pass
            else:
                data["fb_ads_status"] = "Browser pool busy"
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

    browser = _browser_pool.acquire(timeout=15)
    if not browser:
        data["social_profiles"] = {p: {"handle": h, "url": f"https://{p}.com/{h}"} for p, h in found_profiles.items()}
        data["social_summary"] = f"Found {len(found_profiles)} profile(s), browser pool busy"
        return data

    context = None
    try:
        context = browser.new_context(
            user_agent=random.choice(_BROWSER_USER_AGENTS),
        )

        for platform, handle in found_profiles.items():
            profile = {"handle": handle, "followers": None, "last_post": None, "posts_visible": 0}
            _random_delay(1.0, 2.5)  # Delay between social platforms to avoid detection

            try:
                page = context.new_page()

                if platform == "instagram":
                    page.goto(f"https://www.instagram.com/{handle}/", wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(3000)
                    ig_text = page.content()
                    follower_match = re.search(r"([\d,.]+[KkMm]?)\s*(?:Followers|followers)", ig_text)
                    if follower_match:
                        profile["followers"] = follower_match.group(1)
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

            # Parse follower count to number (runs on both success and failure)
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

        context.close()
        context = None

    except Exception:
        data["social_summary"] = "Social scraping failed"
    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            _browser_pool.release(browser)
        except Exception:
            pass

    if data.get("social_summary") == "Social scraping failed":
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
        "classification": "",
        "icp_fit": "",
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

    # New enhancement fields
    summary_parts.append(f"Automation platform (anti-signal): {intel.get('antisignal_automation_platform_name', 'None')} (disqualified: {intel.get('antisignal_has_full_automation_platform', False)})")
    summary_parts.append(f"Confirmation page quality: {intel.get('confirmation_worst_confirmation_quality', 'Not checked')}")
    summary_parts.append(f"Booking page branding: {intel.get('bookingbrand_booking_page_branding_score', 'Not checked')}")
    summary_parts.append(f"Longest form fields: {intel.get('forms_longest_form_fields', 'N/A')} fields")
    summary_parts.append(f"CTA destination mismatch: {intel.get('gaps_cta_destination_mismatch', False)} ({intel.get('gaps_cta_unique_destinations', 0)} unique destinations)")
    summary_parts.append(f"Booking redirect hops: {intel.get('redirects_booking_redirect_count', 0)}")
    summary_parts.append(f"Schema markup: {intel.get('schema_has_business_schema', 'N/A')}")

    # Add external domain and confidence data to summary
    summary_parts.append(f"External domains linked: {intel.get('external_domains', [])}")
    summary_parts.append(f"Secondary funnel platform: {intel.get('funnel_secondary_funnel_platform', 'None')}")
    summary_parts.append(f"Has secondary domain: {intel.get('funnel_has_secondary_domain', False)}")
    summary_parts.append(f"DATA CONFIDENCE: PageSpeed=HIGH, Booking link status=LOW (automated check, may be wrong), FB ads=HIGH, Social followers=MEDIUM")

    site_data = "\n".join(summary_parts)

    prompt = f"""You are a coaching business analyst for KairosCal.io, which builds booking infrastructure for coaches who sell via discovery calls.

WEBSITE AUDIT DATA:
{site_data}

STEP 1 — BUSINESS MODEL CLASSIFICATION (required before any analysis):
Classify this business into ONE of these categories:
A) SOLO COACH NEEDING CALLS — sells high-ticket offers via discovery/strategy calls, booking infrastructure is critical to revenue
B) COURSE/MEMBERSHIP SELLER — primarily sells via self-serve checkout (ThriveCart, Kajabi, Teachable, etc.), booking calls are secondary or optional
C) AGENCY/SERVICE PROVIDER — provides done-for-you services to others (not coaching), booking infra is not the core need
D) COACH-OF-COACHES — trains other coaches, likely has sophisticated existing infrastructure
E) TOO EARLY / TOO SMALL — insufficient signals to determine, or clearly under $5K/month

State your classification and ONE sentence explaining why.

If classification is B, C, D, or E: set ICP_FIT to "NO" and skip the outreach hooks. Just provide the audit summary and score.

If classification is A: set ICP_FIT to "YES" and proceed with full analysis.

STEP 2 — Respond with EXACTLY this format (keep each section to 2-4 sentences max):

CLASSIFICATION:
[Letter) Category — one sentence reason]

ICP_FIT:
[YES or NO]

AUDIT SUMMARY:
[Brief assessment of the coaching business's online presence, what they do well, and overall impression]

POSITIONING GAPS:
[Specific weaknesses in their website, booking flow, offer clarity, or marketing that are costing them clients]

OUTREACH HOOKS:
[Only if ICP_FIT is YES: 3 specific, personalized cold email hooks based on the gaps found. IMPORTANT:
- Only reference observations you are CONFIDENT about (not booking link status — those are unreliable from automated scraping)
- Prioritize hooks from: PageSpeed scores (reliable), missing confirmation pages, form friction, ad spend without clear booking path
- Do NOT generate hooks about "broken booking links" unless you see strong evidence beyond a single HTTP status code
- Frame as revenue problems, not design problems
- Consider: automation platform anti-signals, confirmation page quality, booking page branding gaps, high-friction forms, CTA destination mismatches, booking redirect chains, and missing schema markup]

OVERALL SCORE:
[X/10 — Score the website's ACTUAL quality as a coaching business, NOT outreach potential. Use this rubric:
- Booking infrastructure (0-2 pts): Has booking tool? CTA above fold? Low friction? Works properly?
- Site performance (0-2 pts): PageSpeed 70+? Fast mobile load? No major technical issues?
- Offer clarity (0-2 pts): Clear pricing? Defined target client? Compelling offer description?
- Social proof & authority (0-2 pts): Testimonials? Social following? Media mentions? Active content?
- Professionalism (0-2 pts): No broken links? Good branding? Schema markup? Confirmation pages?
Add up the points. A 10/10 = world-class coaching site. A 1/10 = barely functional.
State the score and ONE sentence justifying it by referencing the specific data points above.]"""

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
            "classification": r"CLASSIFICATION:\s*\n(.*?)(?=\nICP_FIT:|\Z)",
            "icp_fit": r"ICP_FIT:\s*\n(.*?)(?=\nAUDIT SUMMARY:|\Z)",
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

def scrape_website(url, fast=False):
    """Scrape a website against the full checklist.

    fast=True: skips Playwright browser, FB ads, social media, full crawl.
               Uses requests only. ~10-15s per site instead of ~60s.
               Best for batch processing 100+ leads.
    """
    url = normalize_url(url)
    if not url:
        return {"scrape_status": "no_url"}

    # Stagger concurrent requests to avoid burst patterns
    _random_delay(0.3, 1.5)

    intel = {"website_url": url, "scrape_mode": "fast" if fast else "full"}

    # Fetch homepage — skip browser in fast mode
    if fast:
        resp, soup = fetch_page_simple(url)
    else:
        resp, soup = fetch_homepage(url)
    if not soup:
        intel["scrape_status"] = "failed"
        return intel

    intel["scrape_status"] = "success"
    intel["final_url"] = resp.url
    intel["rendered_with"] = "requests" if fast else ("headless_browser" if HAS_PLAYWRIGHT else "requests")
    html = str(soup)
    text = soup.get_text(separator=" ", strip=True)
    links = get_all_links(soup, url)
    subpages = find_subpages(soup, url)

    # If input URL is a subpage, also fetch the root domain for context
    parsed_input = urlparse(url)
    root_url = f"{parsed_input.scheme}://{parsed_input.netloc}/"
    is_subpage = parsed_input.path.strip("/") != ""

    if is_subpage and not fast:
        intel["input_is_subpage"] = True
        intel["root_domain_url"] = root_url
        root_resp, root_soup = fetch_page_simple(root_url)
        if root_soup:
            root_html = str(root_soup)
            root_text = root_soup.get_text(separator=" ", strip=True)
            root_links = get_all_links(root_soup, root_url)
            # Merge root domain links into our link pool
            links.extend(root_links)
            # Discover subpages from root too
            root_subpages = find_subpages(root_soup, root_url)
            for k, v in root_subpages.items():
                if k not in subpages:
                    subpages[k] = v
            # Merge root HTML for pattern detection
            html = html + "\n" + root_html
            text = text + " " + root_text
    else:
        intel["input_is_subpage"] = False

    intel["subpages_found"] = list(subpages.keys())

    # Discover external domains — scan homepage + all internal links found on homepage
    all_external_links = []
    all_external_domains = set()

    # Scan homepage
    homepage_ext_links, homepage_ext_domains = get_external_links(soup, url)
    all_external_links.extend(homepage_ext_links)
    all_external_domains.update(homepage_ext_domains)

    # Build list of ALL unique internal pages to scan (from links + subpages)
    scanned_urls = {url.rstrip("/")}
    pages_to_scan = []

    # Add named subpages (about, pricing, etc.)
    for subpage_key, subpage_url in subpages.items():
        normalized = subpage_url.rstrip("/")
        if normalized not in scanned_urls:
            scanned_urls.add(normalized)
            pages_to_scan.append(subpage_url)

    # Add ALL internal links from the homepage (catches /tiny-challenge-book, /apply, etc.)
    for link in links:
        link_url = link["url"].rstrip("/")
        # Skip anchors, files, and already-seen URLs
        if link_url in scanned_urls:
            continue
        parsed_link = urlparse(link_url)
        # Skip non-page resources
        if parsed_link.path.endswith((".pdf", ".jpg", ".png", ".gif", ".css", ".js", ".svg", ".ico", ".xml")):
            continue
        # Skip homepage itself
        if not parsed_link.path.strip("/"):
            continue
        scanned_urls.add(link_url)
        pages_to_scan.append(link["url"])

    # Cap pages to scan — fewer in fast mode
    pages_to_scan = pages_to_scan[:3 if fast else 10]

    # Scan internal pages for external links — in parallel
    def _scan_page_ext(page_url):
        try:
            _, page_soup = fetch_page_simple(page_url, timeout=8)
            if page_soup:
                return get_external_links(page_soup, page_url)
        except Exception:
            pass
        return [], []

    with ThreadPoolExecutor(max_workers=8) as executor:
        ext_futures = {executor.submit(_scan_page_ext, pu): pu for pu in pages_to_scan}
        for future in as_completed(ext_futures):
            try:
                page_ext_links, page_ext_domains = future.result()
                all_external_links.extend(page_ext_links)
                all_external_domains.update(page_ext_domains)
            except Exception:
                continue

    # Deduplicate external links by URL
    seen_urls = set()
    deduped_external_links = []
    for link in all_external_links:
        if link["url"] not in seen_urls:
            seen_urls.add(link["url"])
            deduped_external_links.append(link)

    external_links = deduped_external_links
    external_domains = list(all_external_domains)

    intel["external_domains"] = external_domains
    intel["external_link_count"] = len(external_links)

    secondary = detect_secondary_funnels(external_links, external_domains)
    for k, v in secondary.items():
        intel[f"funnel_{k}"] = v

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

    # 6. Technical gaps (includes Enhancement 5: CTA destination mismatch)
    tech_gaps = analyze_technical_gaps(soup, text, html, resp, links, booking)
    for k, v in tech_gaps.items():
        intel[f"gaps_{k}"] = v

    # 7. Automation platform detection (Enhancement 1: anti-signal)
    # Scan homepage first
    autoplatform = detect_automation_platforms(soup, html)

    # Also scan external domain pages for anti-signals — in parallel (skip in fast mode)
    if not fast and not autoplatform["has_full_automation_platform"] and external_links:
        ext_domains_checked = set()
        ext_urls_to_scan = []
        for ext_link in external_links[:10]:
            ext_domain = ext_link.get("domain", "")
            if ext_domain not in ext_domains_checked:
                ext_domains_checked.add(ext_domain)
                ext_urls_to_scan.append((ext_link["url"], ext_domain))

        def _scan_ext_antisignal(url_domain):
            ext_url, ext_domain = url_domain
            try:
                _, ext_soup = fetch_page_simple(ext_url, timeout=8)
                if ext_soup:
                    ext_html = str(ext_soup)
                    result = detect_automation_platforms(ext_soup, ext_html)
                    if result["has_full_automation_platform"]:
                        result["detected_on_domain"] = ext_domain
                        return result
            except Exception:
                pass
            return None

        with ThreadPoolExecutor(max_workers=5) as executor:
            for result in executor.map(_scan_ext_antisignal, ext_urls_to_scan):
                if result:
                    autoplatform = result
                    break

    for k, v in autoplatform.items():
        intel[f"antisignal_{k}"] = v

    # 8. Schema.org structured data (Enhancement 7)
    schema = check_structured_data(soup)
    for k, v in schema.items():
        intel[f"schema_{k}"] = v

    # ---- Slow steps (network/browser calls) — ALL run in parallel ----
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

    def _run_form_fields():
        return ("form_fields", count_form_fields(soup, html, subpages, url))

    def _run_booking_brand():
        return ("booking_brand", check_booking_page_branding(booking, url))

    def _run_booking_redirects():
        return ("booking_redirects", check_booking_redirects(booking))

    if fast:
        # Fast mode: only PageSpeed + timing network (no browser needed)
        # Skip: FB ads (Playwright), social (Playwright), full crawl (50 pages)
        slow_tasks = [_run_pagespeed, _run_timing_network, _run_booking_redirects]
    else:
        # Full mode: everything
        slow_tasks = [
            _run_pagespeed, _run_fb_ads, _run_crawl, _run_social,
            _run_timing_network, _run_form_fields, _run_booking_brand,
            _run_booking_redirects,
        ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fn) for fn in slow_tasks]
        for future in as_completed(futures):
            try:
                key, result = future.result()
                slow_results[key] = result
            except Exception:
                pass

    # Merge form fields, booking brand, booking redirects
    for k, v in slow_results.get("form_fields", {}).items():
        intel[f"forms_{k}"] = v
    for k, v in slow_results.get("booking_brand", {}).items():
        intel[f"bookingbrand_{k}"] = v
    for k, v in slow_results.get("booking_redirects", {}).items():
        intel[f"redirects_{k}"] = v

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

    # Confirmation page quality audit (Enhancement 2 — depends on crawl data)
    if crawl:
        conf_audit = audit_confirmation_pages(crawl, url)
        for k, v in conf_audit.items():
            intel[f"confirmation_{k}"] = v

    # Schema gap hook (needs audience data from above)
    if not intel.get("schema_has_business_schema") and (
        intel.get("audience_social_proof_testimonials") or intel.get("audience_social_proof_media_mentions")
    ):
        intel["schema_schema_gap_hook"] = (
            "Your site has no structured data markup — Google can't properly identify your business, "
            "your reviews, or your services. That's invisible SEO credibility you're leaving on the table."
        )

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

    # Confidence assessment for key fields
    intel["_confidence"] = {
        "booking_cta_works": "LOW" if "verify manually" in str(intel.get("booking_booking_cta_works", "")) else "HIGH",
        "pagespeed": "HIGH" if intel.get("pagespeed_score_performance") is not None else "NONE",
        "fb_ads": "HIGH" if intel.get("fbads_fb_ads_found") else "MEDIUM",
        "social_followers": "MEDIUM",
        "business_model": "LOW",
        "booking_tool_detection": "HIGH",
        "anti_signal_detection": "HIGH",
    }

    # ---- AI analysis (must run last — needs all other data) ----
    if HAS_CLAUDE and ANTHROPIC_API_KEY:
        ai = generate_ai_analysis(intel)
        for k, v in ai.items():
            intel[f"ai_{k}"] = v

    return intel


# =============================================================================
# CSV PROCESSING (CLI usage)
# =============================================================================

def process_csv(input_path, output_path, fast=False):
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

    mode_label = "FAST" if fast else "FULL"
    workers = MAX_WORKERS_FAST if fast else MAX_WORKERS
    print(f"Found website column: '{website_col}'")
    print(f"Processing {len(df)} leads in {mode_label} mode ({workers} concurrent)...\n")

    results = []

    def process_row(idx, row):
        url = str(row[website_col]).strip()
        if not url or url.lower() in ("nan", "none", ""):
            return idx, {"scrape_status": "no_url"}
        print(f"  [{idx+1}/{len(df)}] Scraping {url}...")
        return idx, scrape_website(url, fast=fast)

    with ThreadPoolExecutor(max_workers=workers) as executor:
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
        print("Usage: python3 scraper.py <input.csv> [output.csv] [--fast]")
        print("  --fast  Skip browser, FB ads, social, crawl (10x faster for large batches)")
        sys.exit(1)

    fast_mode = "--fast" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--fast"]
    input_file = args[0]
    output_file = args[1] if len(args) > 1 else input_file.replace(".csv", "_enriched.csv")
    process_csv(input_file, output_file, fast=fast_mode)
