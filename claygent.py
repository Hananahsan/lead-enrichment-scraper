"""
Claygent — AI-First Website Intelligence Engine for KairosCal.io

Reads raw page content with Claude Sonnet to extract structured coaching site
intelligence. Runs as a slow task inside scraper.py's parallel pipeline.

Architecture:
  collect_content() → run_ai_analysis() → detect_conflicts() → merged intel dict

Design rules:
  - NEVER re-fetches the homepage (receives soup/html from scrape_website)
  - Reuses _http_session and fetch_page_simple() from scraper.py for subpages
  - Uses anthropic Python SDK (not raw HTTP)
  - All threading is sync (no asyncio)
  - Navigator (Phase 2) uses _run_playwright_task subprocess pattern
"""

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

try:
    import anthropic
except ImportError:
    anthropic = None

logger = logging.getLogger("claygent")

CLAUDE_MODEL = "claude-sonnet-4-20250514"  # Pinned — NOT "latest"
MAX_CONTENT_CHARS = 50_000  # ~12,500 tokens — ~$0.07/lead realtime, ~$0.035/lead batch
MAX_SUBPAGES = 6  # Cap subpage fetches

# Rate limit Sonnet calls across all concurrent leads (Audit Issue 1)
_api_semaphore = threading.Semaphore(5)


# ---------------------------------------------------------------------------
# System prompt and mega-prompt (Section 3 of build plan)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a precise website intelligence agent for KairosCal.io, a company that builds AI-powered booking infrastructure for coaches and consultants.

Your job: analyze coaching/consulting websites and return structured data for outreach qualification.

RULES:
1. Respond ONLY with valid JSON. No markdown, no preamble, no explanation.
2. If you cannot determine a field, return null — do NOT guess or fabricate.
3. Base ALL findings on the actual page content provided. Never infer from the URL alone.
4. For booking detection: look carefully for Squarespace built-in scheduling, Google Calendar Appointments, and tools loaded via JavaScript that may not appear as traditional URLs.
5. For positioning: read the ACTUAL homepage headline and about page language. "Life coach" as primary identity is a hard disqualifier. "Executive coach" is ideal.
6. For pricing: distinguish between "pricing hidden behind apply/call" (HIGH-TICKET signal, positive) and "no pricing found anywhere" (unclear signal, neutral).
7. For observation hooks: ALWAYS use timeline hook format. A timeline hook combines a specific observation with a compressed achievement window — it tells the prospect what's wrong AND how fast a peer fixed it with a specific result. Timeline hooks achieve 2.3x higher reply rates than plain problem statements. Never output a hook that only describes a problem without the timeline/peer/velocity component.
8. For secondary observations: NEVER repeat the same data point used in the primary observation_hook. If the primary hook uses PageSpeed/load time, the secondary MUST use a different category (form friction, testimonials, CTAs, etc.). Each observation must cite the specific number from the TECHNICAL DATA section."""

MEGA_PROMPT_TEMPLATE = """Analyze this coaching/consulting website and return a single JSON object.

=== ICP DEFINITION (who we're looking for) ===
- Revenue: $10K-$50K/month high-ticket coaches
- Offer: 1-on-1 coaching, group programs, consulting at $2,000+ per client
- Booking: Basic Calendly, Acuity, contact form, or broken booking flow
- Team: Solo or 1-10 people, no internal dev team
- Location: English-speaking markets (US, UK, Canada, Australia)

=== HARD DISQUALIFIERS (score RED, do not email) ===
- Full automation platform: GoHighLevel, ClickFunnels, Kajabi, Kartra, Keap
- Franchise coaches: ActionCOACH, BNI
- "Life coach" as PRIMARY identity on homepage headline
- DEI/equity/social justice coaching as primary service
- Coach training / coaching-for-coaches as primary business
- Wellness/fitness/spiritual as core modality (somatic, breathwork, energy healing)
- Low-ticket (under $500) with no call-based sales model
- Course-only sellers with no booking component

=== SOFT DISQUALIFIERS (score YELLOW, flag for review) ===
- Career transition/burnout coaching with no business component
- "Personal mastery" / "self-discovery" as primary positioning
- Group coaching priced under $1,500
- Mixed life/executive coaching where life coaching dominates

=== CAMPAIGN BUCKETS ===
A: Slow Site -- page loads >3s on mobile
B: Generic Calendly -- default Calendly/Acuity, no branding, no pre-call sequence
C: No Booking -- contact form only, no scheduling tool (VERIFY CAREFULLY -- Squarespace scheduling is often invisible)
D: Ad Spend Leak -- running paid ads to basic booking page
E: New Launch -- recently redesigned or launched site
F: Multi Coach Platform -- multiple coaches, shared intake
G: Post Booking Gap -- has booking tool but no confirmation/nurture sequence
H: Brand Credential Gap -- generic/unbranded booking page despite polished site
I: Multiple Audiences -- serves 2+ distinct buyer types on one path

=== BUILD SIZE ===
- Launchpad $4,500: Simple 1-3 page site, basic automation needed
- Growth Engine $9,000: Multi-page site, full infrastructure overhaul
- Empire $18,000: Complex multi-offer business, team of 3+

=== PAGE METADATA ===
URL: {url}
Platform: {platform}
Nav Items: {nav_items_formatted}
Booking-Related Links: {booking_links_formatted}

{technical_data_section}

=== PAGE CONTENT ===
{combined_content}

=== RETURN THIS EXACT JSON SCHEMA ===
{{
  "booking_tool": "Calendly|Acuity|Cal.com|Squarespace Scheduling|Google Calendar|HubSpot|TidyCal|SavvyCal|OnceHub|Dubsado|YouCanBookMe|Custom Form|Contact Form Only|None",
  "booking_url": "direct URL or null",
  "booking_branded": true/false,
  "booking_pre_call_sequence": true/false,
  "booking_cta_above_fold": true/false,
  "booking_friction_score": 1-10,
  "booking_false_negative_risk": true/false,
  "booking_notes": "brief explanation",

  "offer_primary": "description of primary coaching offer",
  "offer_secondary": ["other offers"],
  "offer_requires_booking": true/false,
  "offer_target_audience": "who they serve",
  "price_visible": true/false,
  "price_range": "$X-$Y or 'Hidden (apply/call)' or 'Not found'",
  "price_meets_2k_threshold": true/false/null,

  "positioning": "business_outcome|personal_outcome|mixed",
  "positioning_detail": "one sentence explaining the classification",
  "primary_identity": "e.g., executive coach, business coach, life coach, leadership coach",

  "icp_score": "GREEN|YELLOW|RED",
  "icp_score_numeric": 0-100,
  "icp_classification": "A) SOLO COACH NEEDING CALLS|B) COURSE/MEMBERSHIP|C) AGENCY|D) COACH-OF-COACHES|E) TOO EARLY",
  "icp_classification_reason": "one sentence",
  "icp_fit": "YES|NO",
  "disqualifiers": ["list any found, empty array if none"],

  "campaign_bucket": "A|B|C|D|E|F|G|H|I",
  "campaign_reason": "one sentence why this bucket",
  "build_size_estimate": "Launchpad $4,500|Growth Engine $9,000|Empire $18,000",

  "observation_hook": "The ONE specific cold email hook for Email 1. MUST use TIMELINE HOOK format: [specific observation about THIS site] + [compressed achievement window with peer reference]. Structure: '[Observation] — a coaching client [with similar situation] [achieved specific result] [in specific timeframe].' Example: '{{{{companyName}}}} takes 4.2s to load on mobile — a coaching client with similar speed issues cut their cost-per-booked-call by 35% in the first 30 days.' The observation must be verifiable (the prospect can check it). The peer reference adds velocity and proof. NEVER write a plain problem statement without the timeline component.",
  "secondary_observations": ["1-2 backup observations for Email 2. Each must reference a DIFFERENT data point from the primary hook. Priority order (use the FIRST that applies with actual data): 1) PageSpeed score or load time, 2) form field count / friction, 3) testimonial count vs display, 4) CTA count or destination mismatch, 5) content recency dates, 6) social follower count vs engagement, 7) mobile layout issues, 8) redirect chain hops, 9) missing schema markup. Each observation must include the SPECIFIC number or data point, not a generic statement."],

  "audit_summary": "2-3 sentence assessment of the business and its online presence",
  "positioning_gaps": "specific weaknesses costing them clients",

  "ai_certainty_booking": 0.0-1.0,
  "ai_certainty_positioning": 0.0-1.0,
  "ai_certainty_pricing": 0.0-1.0,
  "ai_certainty_icp": 0.0-1.0
}}"""


# ---------------------------------------------------------------------------
# Platform detection markers (mirrors scraper.py detect_automation_platforms
# plus additional CMS detection)
# ---------------------------------------------------------------------------

_PLATFORM_MARKERS = {
    "Squarespace": [r"squarespace", r"static\.squarespace", r"sqs-"],
    "Webflow": [r"webflow\.com", r"w-webflow"],
    "WordPress": [r"wp-content", r"wp-includes"],
    "Wix": [r"wix\.com"],
    "Shopify": [r"shopify\.com", r"cdn\.shopify"],
    "Kajabi": [r"kajabi\.com", r"kajabi-storefronts", r"checkout\.kajabi"],
    "ClickFunnels": [r"clickfunnels\.com", r"cf-pages", r"cfimg\.com", r"clickfunnels2\.com"],
    "GoHighLevel": [r"gohighlevel\.com", r"msgsndr\.com", r"highlevel", r"\.myfunnels\.com", r"leadconnectorhq\.com", r"app\.ghl\."],
    "Kartra": [r"kartra\.com", r"app\.kartra"],
    "Keap": [r"keap\.com", r"infusionsoft\.com", r"keap-app"],
}

_BOOKING_KEYWORDS = [
    "book", "schedule", "calendar", "consult", "discovery", "call",
    "appointment", "meeting", "session", "calendly", "acuity",
    "cal.com", "hubspot", "tidycal", "savvycal", "oncehub", "youcanbook",
]


# ---------------------------------------------------------------------------
# 2.2  Content Collector
# ---------------------------------------------------------------------------

def _detect_platform(soup, html):
    """Detect the website platform/CMS from HTML markers and meta tags."""
    html_lower = html.lower()

    # Check meta generator tag first
    gen_tag = soup.find("meta", attrs={"name": "generator"})
    if gen_tag:
        gen_content = (gen_tag.get("content", "") or "").lower()
        for platform, _ in _PLATFORM_MARKERS.items():
            if platform.lower() in gen_content:
                return platform

    # Check HTML markers
    for platform, patterns in _PLATFORM_MARKERS.items():
        for pattern in patterns:
            if re.search(pattern, html_lower):
                return platform

    return None


def _extract_nav_items(soup):
    """Extract all <a> tags inside <nav> elements."""
    nav_items = []
    for nav in soup.find_all("nav"):
        for a_tag in nav.find_all("a", href=True):
            text = a_tag.get_text(strip=True)
            if text:
                nav_items.append({"text": text, "href": a_tag["href"]})
    return nav_items


def _extract_booking_links(soup, html):
    """Extract all links where href or text matches booking keywords."""
    booking_links = []
    seen_hrefs = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].lower()
        text = a_tag.get_text(strip=True).lower()
        combined = href + " " + text

        if any(kw in combined for kw in _BOOKING_KEYWORDS):
            if href not in seen_hrefs:
                seen_hrefs.add(href)
                booking_links.append({
                    "text": a_tag.get_text(strip=True),
                    "href": a_tag["href"],
                })

    return booking_links


def _clean_page_content(soup_or_html, title=None):
    """Clean a page's content for AI consumption.

    Removes scripts/styles, preserves heading structure, link text with hrefs,
    and list items. Deduplicates adjacent identical lines.
    """
    from bs4 import BeautifulSoup

    # ALWAYS parse fresh — never mutate the original soup object.
    # The homepage soup is shared with other parallel tasks (form_fields, etc.)
    # and decompose() would destroy tags they need.
    if isinstance(soup_or_html, str):
        soup = BeautifulSoup(soup_or_html, "html.parser")
    else:
        soup = BeautifulSoup(str(soup_or_html), "html.parser")

    # Remove noise tags (safe — operating on a copy)
    for tag_name in ["script", "style", "noscript", "iframe", "svg"]:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    lines = []

    for element in soup.descendants:
        if hasattr(element, "name") and element.name:
            # Heading structure
            if element.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                heading_text = element.get_text(strip=True)
                if heading_text:
                    prefix = f"[{element.name.upper()}]"
                    lines.append(f"{prefix} {heading_text}")

            # Links with href
            elif element.name == "a" and element.get("href"):
                link_text = element.get_text(strip=True)
                href = element["href"]
                if link_text and href and not href.startswith("#"):
                    lines.append(f"{link_text} ({href})")

            # List items
            elif element.name == "li":
                li_text = element.get_text(strip=True)
                if li_text:
                    lines.append(f"- {li_text}")

            # Paragraphs and divs — only direct text
            elif element.name in ("p", "div", "span", "td", "th", "blockquote"):
                # Get only direct text children to avoid duplication
                direct_text = ""
                for child in element.children:
                    if isinstance(child, str):
                        direct_text += child.strip() + " "
                direct_text = direct_text.strip()
                if len(direct_text) > 20:  # Skip very short fragments
                    lines.append(direct_text)

    # Deduplicate adjacent identical lines
    deduped = []
    for line in lines:
        line = line.strip()
        if line and (not deduped or line != deduped[-1]):
            deduped.append(line)

    # Strip excessive whitespace
    content = "\n".join(deduped)
    content = re.sub(r"\n{3,}", "\n\n", content)

    return content


def _discover_subpage_urls(nav_items, subpages, base_url):
    """Build prioritized list of subpage URLs to fetch.

    Priority order per the build plan:
      1. pricing / invest / packages
      2. services / programs / coaching / work-with-me
      3. about
      4. First booking-related link
      5. book / schedule / discovery-call / free-consultation
      6. contact
    """
    urls_to_fetch = []
    seen = set()

    def _add(url, label):
        if url and len(urls_to_fetch) < MAX_SUBPAGES:
            full = urljoin(base_url, url)
            parsed = urlparse(full)
            base_parsed = urlparse(base_url)
            # Only same-domain pages
            if parsed.netloc == base_parsed.netloc or not parsed.netloc:
                norm = parsed._replace(fragment="").geturl()
                if norm not in seen:
                    seen.add(norm)
                    urls_to_fetch.append((full, label))

    # 1. Pricing
    if subpages.get("pricing"):
        _add(subpages["pricing"], "pricing")
    else:
        for item in nav_items:
            href = item["href"].lower()
            if any(kw in href for kw in ["/pricing", "/invest", "/packages"]):
                _add(item["href"], "pricing")
                break

    # 2. Services
    if subpages.get("services"):
        _add(subpages["services"], "services")
    else:
        for item in nav_items:
            href = item["href"].lower()
            if any(kw in href for kw in ["/services", "/programs", "/coaching", "/work-with-me"]):
                _add(item["href"], "services")
                break

    # 3. About
    if subpages.get("about"):
        _add(subpages["about"], "about")

    # 4. First booking-related nav link
    for item in nav_items:
        href_lower = item["href"].lower()
        text_lower = item["text"].lower()
        combined = href_lower + " " + text_lower
        if any(kw in combined for kw in _BOOKING_KEYWORDS):
            _add(item["href"], "booking")
            break

    # 5. Common booking paths
    for path in ["/book", "/schedule", "/discovery-call", "/free-consultation"]:
        if len(urls_to_fetch) >= MAX_SUBPAGES:
            break
        _add(path, "booking_path")

    # 6. Contact
    if subpages.get("contact"):
        _add(subpages["contact"], "contact")

    return urls_to_fetch


def collect_content(soup, html, url, subpages, fast=False):
    """Collect and clean page content for AI analysis.

    Takes the already-fetched homepage soup and enriches with key subpage content.
    NEVER re-fetches the homepage.

    Args:
        soup: BeautifulSoup object from scrape_website() (already in memory)
        html: raw HTML string from scrape_website() (already in memory)
        url: base URL
        subpages: dict from find_subpages() (already in memory)
        fast: if True, skip subpage fetching (homepage only)

    Returns:
        dict with combined_content, platform, nav_items, booking_links, etc.
    """
    # Lazy import to avoid circular import at module level
    from scraper import fetch_page_simple

    # Extract metadata from soup
    platform = _detect_platform(soup, html)
    nav_items = _extract_nav_items(soup)
    booking_links = _extract_booking_links(soup, html)

    # Clean homepage content
    title_tag = soup.find("title")
    homepage_title = title_tag.get_text(strip=True) if title_tag else ""
    homepage_content = _clean_page_content(soup, homepage_title)

    sections = [f"=== HOMEPAGE ===\nTitle: {homepage_title}\n{homepage_content}"]
    subpages_fetched = []

    # Fetch subpages (unless fast mode)
    if not fast:
        urls_to_fetch = _discover_subpage_urls(nav_items, subpages, url)

        def _fetch_subpage(url_label):
            sub_url, label = url_label
            try:
                resp, sub_soup = fetch_page_simple(sub_url, timeout=8, browser_fallback=False)
                if sub_soup:
                    sub_title_tag = sub_soup.find("title")
                    sub_title = sub_title_tag.get_text(strip=True) if sub_title_tag else ""
                    content = _clean_page_content(sub_soup, sub_title)
                    parsed_path = urlparse(sub_url).path or sub_url
                    return (sub_url, label, sub_title, content, parsed_path)
            except Exception as e:
                logger.debug(f"Failed to fetch subpage {sub_url}: {e}")
            return None

        if urls_to_fetch:
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {executor.submit(_fetch_subpage, ul): ul for ul in urls_to_fetch}
                for future in as_completed(futures, timeout=30):
                    try:
                        result = future.result(timeout=10)
                        if result:
                            sub_url, label, sub_title, content, path = result
                            sections.append(f"=== {path.upper()} ===\nTitle: {sub_title}\n{content}")
                            subpages_fetched.append(sub_url)
                    except Exception:
                        pass

    # Combine into one text block
    combined = "\n\n".join(sections)

    # Truncate to MAX_CONTENT_CHARS
    if len(combined) > MAX_CONTENT_CHARS:
        combined = combined[:MAX_CONTENT_CHARS] + "\n\n[Content truncated at 50,000 characters]"

    return {
        "combined_content": combined,
        "platform": platform,
        "nav_items": nav_items,
        "booking_links": booking_links,
        "subpages_fetched": subpages_fetched,
        "content_length": len(combined),
    }


# ---------------------------------------------------------------------------
# 2.3  AI Engine
# ---------------------------------------------------------------------------

def _build_tech_section(intel_snapshot):
    """Build the TECHNICAL DATA section from an intel snapshot dict.

    Shared by run_ai_analysis() and prepare_batch_request() to avoid divergence.
    """
    if not intel_snapshot:
        return ""
    tech_parts = []
    ps_perf = intel_snapshot.get("pagespeed_score_performance")
    if ps_perf is not None:
        tech_parts.append(f"PageSpeed Mobile Performance Score: {ps_perf}/100")
    ps_seo = intel_snapshot.get("pagespeed_score_seo")
    if ps_seo is not None:
        tech_parts.append(f"PageSpeed SEO Score: {ps_seo}/100")
    lcp = intel_snapshot.get("pagespeed_lcp_seconds")
    if lcp is not None:
        tech_parts.append(f"Largest Contentful Paint (mobile): {lcp} seconds")
    fcp = intel_snapshot.get("pagespeed_fcp_seconds")
    if fcp is not None:
        tech_parts.append(f"First Contentful Paint (mobile): {fcp} seconds")
    tbt = intel_snapshot.get("pagespeed_tbt_ms")
    if tbt is not None:
        tech_parts.append(f"Total Blocking Time: {tbt}ms")
    speed_idx = intel_snapshot.get("pagespeed_speed_index")
    if speed_idx is not None:
        tech_parts.append(f"Speed Index: {speed_idx}s")
    mobile_time = intel_snapshot.get("mobile_load_time_seconds")
    if mobile_time is not None:
        tech_parts.append(f"Mobile Load Time (browser measured): {mobile_time}s")
    mobile_cta = intel_snapshot.get("mobile_cta_visible")
    if mobile_cta is not None:
        tech_parts.append(f"Mobile CTA Above Fold: {'Yes' if mobile_cta else 'No'}")
    if intel_snapshot.get("fbads_fb_ads_found"):
        tech_parts.append(f"Facebook Ads Active: {intel_snapshot.get('fbads_fb_ads_count', 0)} ads running")
    social = intel_snapshot.get("social_total_followers")
    if social:
        tech_parts.append(f"Total Social Followers: {social}")
        social_sum = intel_snapshot.get("social_summary", "")
        if social_sum:
            tech_parts.append(f"Social Breakdown: {social_sum}")
    testimonials = intel_snapshot.get("testimonial_count")
    if testimonials:
        tech_parts.append(f"Testimonials Found On Site: {testimonials}")
    prices = intel_snapshot.get("prices_found")
    if prices:
        tech_parts.append(f"Prices Found: {prices}")
    form_fields = intel_snapshot.get("longest_form_fields")
    if form_fields:
        tech_parts.append(f"Longest Form: {form_fields} fields")
    brand_score = intel_snapshot.get("booking_branding_score")
    if brand_score:
        tech_parts.append(f"Booking Page Branding Score: {brand_score}")
    redirects = intel_snapshot.get("booking_redirect_hops")
    if redirects:
        tech_parts.append(f"Booking Link Redirect Hops: {redirects}")
    if tech_parts:
        return "=== TECHNICAL DATA (real measurements — use exact numbers in hooks) ===\n" + "\n".join(tech_parts)
    return ""


def run_ai_analysis(content_data, url, intel_snapshot=None):
    """Send combined page content to Claude Sonnet with the mega-prompt.

    Args:
        content_data: dict from collect_content()
        url: base URL
        intel_snapshot: optional dict of already-collected intel (PageSpeed, FB ads, etc.)

    Returns:
        dict with all parsed fields + metadata (ai_raw, ai_tokens_in, etc.)
    """
    # Lazy import
    from scraper import ANTHROPIC_API_KEY

    if anthropic is None or not ANTHROPIC_API_KEY:
        return {"ai_parse_error": True, "ai_error": "anthropic SDK or API key not available"}

    # Format nav items and booking links for the prompt
    nav_formatted = "\n".join(
        f"  - {item['text']}: {item['href']}"
        for item in (content_data.get("nav_items") or [])[:15]
    ) or "  (none found)"

    booking_formatted = "\n".join(
        f"  - {item['text']}: {item['href']}"
        for item in (content_data.get("booking_links") or [])[:10]
    ) or "  (none found)"

    tech_section = _build_tech_section(intel_snapshot)

    # Build the user message.
    # Escape braces in user-controlled content to prevent format string injection
    _safe = lambda s: str(s).replace("{", "{{").replace("}", "}}")
    user_message = MEGA_PROMPT_TEMPLATE.format(
        url=_safe(url),
        platform=_safe(content_data.get("platform") or "Unknown"),
        nav_items_formatted=_safe(nav_formatted),
        booking_links_formatted=_safe(booking_formatted),
        technical_data_section=_safe(tech_section),
        combined_content=_safe(content_data.get("combined_content", "")),
    )

    # Call Claude Sonnet with retry logic
    result = {
        "ai_raw": "",
        "ai_tokens_in": 0,
        "ai_tokens_out": 0,
        "ai_cost_usd": 0.0,
        "ai_model": CLAUDE_MODEL,
        "ai_parse_error": False,
    }

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            _api_semaphore.acquire()
            try:
                client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                message = client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=2048,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_message}],
                )
            finally:
                _api_semaphore.release()

            response_text = message.content[0].text
            result["ai_raw"] = response_text
            result["ai_tokens_in"] = message.usage.input_tokens
            result["ai_tokens_out"] = message.usage.output_tokens
            result["ai_cost_usd"] = round(
                (message.usage.input_tokens * 3 / 1_000_000)
                + (message.usage.output_tokens * 15 / 1_000_000),
                4,
            )

            # Parse JSON response
            parsed = _parse_ai_response(response_text)
            if parsed is not None:
                result.update(parsed)
            else:
                result["ai_parse_error"] = True
                logger.warning(f"Failed to parse AI response for {url}")

            return result

        except anthropic.RateLimitError:
            if attempt < max_retries:
                wait = 2 ** attempt
                logger.warning(f"Rate limit hit for {url}, retrying in {wait}s (attempt {attempt + 1})")
                time.sleep(wait)
                continue
            result["ai_parse_error"] = True
            result["ai_error"] = "Rate limit exceeded after retries"
            return result

        except anthropic.APIStatusError as e:
            if e.status_code >= 500 and attempt < max_retries:
                wait = 2 ** attempt
                logger.warning(f"API 5xx for {url}, retrying in {wait}s (attempt {attempt + 1})")
                time.sleep(wait)
                continue
            result["ai_parse_error"] = True
            result["ai_error"] = f"API error: {str(e)[:200]}"
            return result

        except anthropic.APIConnectionError:
            if attempt < 1:
                logger.warning(f"Connection error for {url}, retrying in 3s")
                time.sleep(3)
                continue
            result["ai_parse_error"] = True
            result["ai_error"] = "API connection failed after retry"
            return result

        except Exception as e:
            result["ai_parse_error"] = True
            result["ai_error"] = f"Unexpected error: {str(e)[:200]}"
            return result

    return result


def _parse_ai_response(text):
    """Parse Claude's JSON response, handling markdown fences and extraction."""
    # Strip markdown code fences
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    cleaned = cleaned.strip()

    # Try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try extracting outermost {...} block (non-greedy to avoid spanning
    # multiple objects if Claude accidentally returns more than one)
    match = re.search(r"\{[\s\S]*?\}", cleaned)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            # Non-greedy may have stopped too early on nested braces.
            # Fall back to greedy match for the full JSON.
            match2 = re.search(r"\{[\s\S]*\}", cleaned)
            if match2:
                try:
                    return json.loads(match2.group())
                except json.JSONDecodeError:
                    pass

    return None


# ---------------------------------------------------------------------------
# 2.4  Conflict Detector
# ---------------------------------------------------------------------------

# Known booking tool URL patterns where regex is authoritative
_BOOKING_URL_PATTERNS = {
    "Calendly": r"calendly\.com",
    "Acuity": r"acuityscheduling\.com|squareup\.com/appointments",
    "Cal.com": r"cal\.com",
    "HubSpot": r"meetings\.hubspot\.com",
    "TidyCal": r"tidycal\.com",
    "SavvyCal": r"savvycal\.com",
    "OnceHub": r"oncehub\.com|scheduleonce\.com",
    "Dubsado": r"dubsado\.com",
    "YouCanBookMe": r"youcanbook\.me",
}

# Automation platforms that are HARD anti-signals — regex always wins
_HARD_ANTISIGNAL_PLATFORMS = {"GoHighLevel", "ClickFunnels", "Kajabi", "Kartra", "Keap", "Keap/Infusionsoft"}


def detect_conflicts(ai_results, regex_results):
    """Compare AI output to regex output and produce the final merged intel dict.

    AI is the primary source for all fields. Regex wins on:
    1. Specific booking tool URL patterns found in HTML
    2. Automation platform anti-signals (GoHighLevel, ClickFunnels, etc.)

    Args:
        ai_results: dict from run_ai_analysis()
        regex_results: dict with keys like regex_booking_*, regex_offer_*, etc.

    Returns:
        dict with primary fields (AI), validation fields (regex), confidence,
        and conflict descriptions.
    """
    merged = {}
    conflicts = []

    # --- BOOKING TOOL ---
    ai_booking = ai_results.get("booking_tool") or "None"
    regex_booking = regex_results.get("regex_booking_booking_tool", "None detected")
    regex_booking_links = regex_results.get("regex_booking_booking_links", "")

    # Check if regex found a specific URL pattern
    regex_found_specific_url = False
    regex_specific_tool = None
    booking_links_str = str(regex_booking_links).lower()
    for tool_name, pattern in _BOOKING_URL_PATTERNS.items():
        if re.search(pattern, booking_links_str, re.I):
            regex_found_specific_url = True
            regex_specific_tool = tool_name
            break

    if ai_booking != "None" and regex_booking != "None detected":
        # Both found something
        if ai_booking.lower() == regex_booking.lower() or (regex_specific_tool and ai_booking == regex_specific_tool):
            conflicts.append(f"booking_tool: AGREE — both found {ai_booking}")
            merged["confidence_booking"] = "HIGH"
        else:
            conflicts.append(f"booking_tool: CONFLICT — AI={ai_booking}, regex={regex_booking}")
            merged["confidence_booking"] = "LOW"
    elif ai_booking != "None" and regex_booking == "None detected":
        # AI found, regex missed
        conflicts.append(f"booking_tool: AI_UPGRADE — AI found {ai_booking}, regex missed")
        merged["confidence_booking"] = "MEDIUM_HIGH"
    elif ai_booking == "None" and regex_found_specific_url:
        # Regex found specific URL, AI missed — regex wins (AI_DOWNGRADE)
        conflicts.append(f"booking_tool: AI_DOWNGRADE — regex found {regex_specific_tool} URL, AI missed")
        merged["confidence_booking"] = "LOW"
        # Override AI: use regex finding
        ai_results["booking_tool"] = regex_specific_tool
        ai_results["booking_notes"] = (ai_results.get("booking_notes") or "") + f" [Overridden: regex detected {regex_specific_tool} URL in HTML]"
    else:
        # Neither found anything
        conflicts.append("booking_tool: AGREE — neither found a booking tool")
        merged["confidence_booking"] = "HIGH"

    # --- PRICING ---
    ai_price = ai_results.get("price_range") or "Not found"
    regex_price = regex_results.get("regex_offer_price_range", "Not visible on site")

    if ai_price != "Not found" and regex_price not in ("Not visible on site", "Not found", ""):
        conflicts.append(f"pricing: AGREE — both found pricing")
        merged["confidence_pricing"] = "HIGH"
    elif ai_price != "Not found" and regex_price in ("Not visible on site", "Not found", ""):
        conflicts.append(f"pricing: AI_UPGRADE — AI found '{ai_price}', regex missed")
        merged["confidence_pricing"] = "MEDIUM_HIGH"
    elif ai_price == "Not found" and regex_price not in ("Not visible on site", "Not found", ""):
        conflicts.append(f"pricing: CONFLICT — AI found nothing, regex found '{regex_price}'")
        merged["confidence_pricing"] = "LOW"
    else:
        conflicts.append("pricing: AGREE — neither found pricing")
        merged["confidence_pricing"] = "HIGH"

    # --- AUTOMATION PLATFORM (anti-signal — regex ALWAYS wins) ---
    regex_has_platform = regex_results.get("antisignal_has_full_automation_platform", False)
    regex_platform_name = regex_results.get("antisignal_automation_platform_name", "")
    ai_disqualifiers = ai_results.get("disqualifiers") or []

    if regex_has_platform:
        # Regex detected a hard anti-signal — ensure it's in disqualifiers
        platform_in_ai = any(
            regex_platform_name.lower() in d.lower()
            for d in ai_disqualifiers
            if isinstance(d, str)
        )
        if not platform_in_ai:
            conflicts.append(f"automation_platform: AI_DOWNGRADE — regex found {regex_platform_name}, AI missed it")
            # Force the disqualifier
            if isinstance(ai_results.get("disqualifiers"), list):
                ai_results["disqualifiers"].append(f"Full automation platform: {regex_platform_name}")
            else:
                ai_results["disqualifiers"] = [f"Full automation platform: {regex_platform_name}"]
            # Force RED score
            ai_results["icp_score"] = "RED"
            ai_results["icp_fit"] = "NO"
            merged["confidence_automation"] = "HIGH"
        else:
            conflicts.append(f"automation_platform: AGREE — both detected {regex_platform_name}")
            merged["confidence_automation"] = "HIGH"

    # --- CONTACT FORM cross-check ---
    regex_has_form = regex_results.get("regex_booking_has_contact_form", False)
    if ai_booking == "None" and regex_has_form:
        conflicts.append("contact_form: NOTE — AI found no booking tool but regex detected a contact form")

    # --- OFFER TYPE (supplementary) ---
    ai_offer = ai_results.get("offer_primary") or ""
    regex_offer = regex_results.get("regex_offer_offer_type", "")
    if ai_offer and regex_offer:
        conflicts.append(f"offer: CROSS_CHECK — AI='{ai_offer[:60]}', regex='{regex_offer[:60]}'")

    # --- AUDIENCE SIGNALS cross-check ---
    for signal_key, signal_name in [
        ("regex_audience_has_podcast", "podcast"),
        ("regex_audience_has_youtube", "YouTube"),
        ("regex_audience_has_active_blog", "active blog"),
    ]:
        regex_val = regex_results.get(signal_key, False)
        if regex_val and isinstance(regex_val, bool):
            conflicts.append(f"audience_{signal_name}: regex detected {signal_name}")

    # Positioning confidence — always MEDIUM_HIGH (no regex equivalent)
    merged["confidence_positioning"] = "MEDIUM_HIGH"

    # Store conflicts
    merged["confidence_conflicts"] = conflicts

    return merged


# ---------------------------------------------------------------------------
# 2.5  Navigator — Phase 2 (JS-heavy page interaction)
# ---------------------------------------------------------------------------

_NAV_USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/132.0.6834.78 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.79 Mobile Safari/537.36",
]


def _navigator_browse(url):
    """Standalone Playwright function for subprocess execution.

    Called via: _run_playwright_task("_claygent_navigator_browse", url)

    5-step coaching-site action sequence:
    1. Load homepage with JS rendering
    2. Scroll to bottom (lazy-loaded widgets, footer CTAs)
    3. Click nav items matching service/coaching pages
    4. Click booking CTAs (booking pages behind interactions)
    5. Try hamburger/mobile menu if nothing else found

    Returns dict with extracted content from all visited pages.
    """
    import random

    result = {
        "pages_visited": [],
        "content": {},
        "errors": [],
        "actions_completed": 0,
        "booking_page_found": False,
        "booking_page_url": None,
    }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result["errors"].append("Playwright not installed")
        return result

    pw = None
    browser = None
    context = None

    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            user_agent=random.choice(_NAV_USER_AGENTS),
        )
        page = context.new_page()

        # Step 1: Load homepage
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            result["pages_visited"].append(url)
            result["content"]["homepage"] = page.content()
            result["actions_completed"] += 1
        except Exception as e:
            result["errors"].append(f"Homepage load: {str(e)[:100]}")
            return result

        # Step 2: Scroll to bottom
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)
            bottom_content = page.content()
            if len(bottom_content) > len(result["content"].get("homepage", "")) + 500:
                result["content"]["after_scroll"] = bottom_content
            result["actions_completed"] += 1
        except Exception as e:
            result["errors"].append(f"Scroll: {str(e)[:100]}")

        # Step 3: Click nav items matching services/coaching pages
        nav_targets = [
            "Services", "Programs", "Work With Me", "Coaching",
            "Our Services", "What We Do", "Offerings", "Packages",
        ]
        nav_found = False
        for target in nav_targets:
            if nav_found:
                break
            try:
                locator = page.get_by_text(target, exact=False).first
                if locator.is_visible(timeout=2000):
                    locator.click(timeout=3000)
                    page.wait_for_load_state("networkidle", timeout=8000)
                    page.wait_for_timeout(1500)
                    result["content"][f"nav_{target.lower().replace(' ', '_')}"] = page.content()
                    result["pages_visited"].append(page.url)
                    result["actions_completed"] += 1
                    nav_found = True
                    try:
                        page.go_back(wait_until="networkidle", timeout=8000)
                        page.wait_for_timeout(1000)
                    except Exception:
                        try:
                            page.goto(url, wait_until="networkidle", timeout=15000)
                        except Exception:
                            pass
            except Exception:
                continue

        # Step 4: Click booking CTAs
        cta_targets = [
            "Book a Call", "Book Now", "Book a Session",
            "Book a Discovery Call", "Schedule a Call", "Schedule Now",
            "Free Consultation", "Free Discovery Call", "Free Strategy Call",
            "Get Started", "Apply Now", "Apply Here",
            "Let's Talk", "Let's Connect", "Work With Me", "Start Here",
        ]
        for target in cta_targets:
            try:
                locator = page.get_by_text(target, exact=False).first
                if locator.is_visible(timeout=2000):
                    href = locator.get_attribute("href") or ""
                    booking_external = any(
                        d in href.lower() for d in [
                            "calendly.com", "acuityscheduling.com", "cal.com",
                            "meetings.hubspot.com", "tidycal.com", "savvycal.com",
                        ]
                    )
                    if booking_external:
                        result["booking_page_found"] = True
                        result["booking_page_url"] = href
                        result["actions_completed"] += 1
                        break
                    locator.click(timeout=3000)
                    page.wait_for_load_state("networkidle", timeout=8000)
                    page.wait_for_timeout(2000)
                    result["content"]["booking_page"] = page.content()
                    result["pages_visited"].append(page.url)
                    result["booking_page_found"] = True
                    result["booking_page_url"] = page.url
                    result["actions_completed"] += 1
                    break
            except Exception:
                continue

        # Step 5: Try hamburger/mobile menu if nothing found
        if not nav_found and not result["booking_page_found"]:
            hamburger_selectors = [
                "button[aria-label*='menu' i]",
                "button[aria-label*='nav' i]",
                ".hamburger", ".menu-toggle", ".nav-toggle",
                "[data-toggle='collapse']",
                "button.w-nav-button",
            ]
            for selector in hamburger_selectors:
                try:
                    ham = page.query_selector(selector)
                    if ham and ham.is_visible():
                        ham.click()
                        page.wait_for_timeout(1500)
                        for cta in ["Book", "Schedule", "Discovery", "Consult"]:
                            try:
                                loc = page.get_by_text(cta, exact=False).first
                                if loc.is_visible(timeout=1000):
                                    href = loc.get_attribute("href") or ""
                                    if href and href != "#":
                                        result["booking_page_url"] = href
                                        result["booking_page_found"] = True
                                        result["actions_completed"] += 1
                                        try:
                                            loc.click(timeout=3000)
                                            page.wait_for_load_state(
                                                "networkidle", timeout=8000
                                            )
                                            result["content"]["booking_page"] = page.content()
                                            result["pages_visited"].append(page.url)
                                        except Exception:
                                            pass
                                        break
                            except Exception:
                                continue
                        break
                except Exception:
                    continue

        return result

    except Exception as e:
        result["errors"].append(f"Navigator error: {str(e)[:200]}")
        return result
    finally:
        try:
            context and context.close()
        except Exception:
            pass
        try:
            browser and browser.close()
        except Exception:
            pass
        try:
            pw and pw.stop()
        except Exception:
            pass


def _process_navigator_content(nav_result, url, original_content_data):
    """Clean Navigator's raw HTML and combine with original content for second AI pass."""
    if not nav_result or not nav_result.get("content"):
        return None

    sections = []
    for page_key, page_html in nav_result["content"].items():
        if not page_html or page_key == "homepage":
            continue
        try:
            cleaned = _clean_page_content(page_html)
            if cleaned and len(cleaned) > 100:
                sections.append(f"=== NAVIGATOR: {page_key.upper()} ===\n{cleaned}")
        except Exception:
            continue

    if not sections:
        return None

    nav_text = "\n\n".join(sections)
    original_text = original_content_data.get("combined_content", "")
    combined = (
        original_text
        + "\n\n=== ADDITIONAL CONTENT DISCOVERED BY BROWSER NAVIGATION ===\n\n"
        + nav_text
    )

    if len(combined) > MAX_CONTENT_CHARS:
        combined = combined[:MAX_CONTENT_CHARS] + "\n\n[Content truncated]"

    updated = dict(original_content_data)
    updated["combined_content"] = combined
    updated["content_length"] = len(combined)
    updated["navigator_pages"] = list(nav_result["content"].keys())

    if nav_result.get("booking_page_found"):
        booking_links = list(updated.get("booking_links", []))
        booking_links.append({
            "text": "Navigator-discovered booking",
            "href": nav_result.get("booking_page_url", ""),
        })
        updated["booking_links"] = booking_links

    return updated


def _merge_navigator_results(first_pass, second_pass):
    """Merge second AI pass into first. Second overrides where it found more."""
    # Booking: override if second found a tool and first didn't
    if (first_pass.get("booking_tool") in (None, "None")
            and second_pass.get("booking_tool") not in (None, "None")):
        for key in ("booking_tool", "booking_url", "booking_branded",
                     "booking_pre_call_sequence", "booking_cta_above_fold",
                     "booking_friction_score", "booking_false_negative_risk",
                     "booking_notes"):
            if key in second_pass:
                first_pass[key] = second_pass[key]

    # Certainty: take the higher value
    for key in ("ai_certainty_booking", "ai_certainty_positioning",
                "ai_certainty_pricing", "ai_certainty_icp"):
        first_val = first_pass.get(key) or 0
        second_val = second_pass.get(key) or 0
        if second_val > first_val:
            first_pass[key] = second_val

    # Hooks: override if second pass has higher overall certainty
    first_avg = sum(
        first_pass.get(k, 0) or 0
        for k in ("ai_certainty_booking", "ai_certainty_positioning")
    ) / 2
    second_avg = sum(
        second_pass.get(k, 0) or 0
        for k in ("ai_certainty_booking", "ai_certainty_positioning")
    ) / 2
    if second_avg > first_avg:
        for key in ("observation_hook", "secondary_observations",
                     "campaign_bucket", "campaign_reason"):
            if second_pass.get(key):
                first_pass[key] = second_pass[key]

    # Track cumulative cost
    first_pass["ai_tokens_in"] = (
        (first_pass.get("ai_tokens_in", 0) or 0)
        + (second_pass.get("ai_tokens_in", 0) or 0)
    )
    first_pass["ai_tokens_out"] = (
        (first_pass.get("ai_tokens_out", 0) or 0)
        + (second_pass.get("ai_tokens_out", 0) or 0)
    )
    first_pass["ai_cost_usd"] = (
        (first_pass.get("ai_cost_usd", 0) or 0)
        + (second_pass.get("ai_cost_usd", 0) or 0)
    )


# ---------------------------------------------------------------------------
# 2.6  Main Entry Point
# ---------------------------------------------------------------------------

def claygent_collect(url, soup, html, subpages, fast=False):
    """Phase 1: Collect and clean page content. Runs in parallel with slow tasks.

    This is pure HTTP fetching + HTML cleaning — no API calls, no AI.
    Returns content_data dict that the Phase 2 AI analysis will use.
    """
    try:
        content_data = collect_content(soup, html, url, subpages, fast=fast)
        logger.info(
            f"Claygent content collected for {url}: "
            f"{content_data['content_length']} chars, "
            f"{len(content_data['subpages_fetched'])} subpages"
        )
        return ("claygent_content", content_data)
    except Exception as e:
        logger.error(f"Claygent content collection failed for {url}: {e}")
        return ("claygent_content", None)


def run_claygent(url, soup, html, intel, subpages, content_data=None, fast=False):
    """Phase 2: AI analysis + conflict detection. Runs AFTER all slow tasks merge.

    When called with content_data (pre-collected in Phase 1), skips collection.
    When called without content_data, does both collection and analysis.

    This function has access to the FULL intel dict including PageSpeed scores,
    FB ads, social followers, crawl data, form friction — everything needed to
    write observation hooks with real numbers instead of guesses.

    Args:
        url: base URL
        soup: BeautifulSoup object (already fetched)
        html: raw HTML string (already fetched)
        intel: COMPLETE intel dict with ALL slow task data already merged
        subpages: dict from find_subpages()
        content_data: pre-collected content from claygent_collect() or None
        fast: if True, homepage-only content

    Returns:
        dict of Claygent results (called directly, not as slow task tuple)
    """
    start_time = time.time()

    try:
        # Step 1: Collect content (if not pre-collected)
        if content_data is None:
            content_data = collect_content(soup, html, url, subpages, fast=fast)
            logger.info(
                f"Claygent content collected for {url}: "
                f"{content_data['content_length']} chars, "
                f"{len(content_data['subpages_fetched'])} subpages"
            )

        # Step 2: Build FULL intel snapshot — now has all slow task data
        intel_snapshot = {}
        # PageSpeed (real Lighthouse data — critical for Campaign A hooks)
        for k in ("pagespeed_score_performance", "pagespeed_score_seo",
                   "pagespeed_score_accessibility", "pagespeed_lcp_seconds",
                   "pagespeed_fcp_seconds", "pagespeed_cls", "pagespeed_tbt_ms",
                   "pagespeed_speed_index"):
            if intel.get(k) is not None:
                intel_snapshot[k] = intel[k]
        # Facebook ads
        if intel.get("fbads_fb_ads_found"):
            intel_snapshot["fbads_fb_ads_found"] = True
            intel_snapshot["fbads_fb_ads_count"] = intel.get("fbads_fb_ads_count", 0)
        # Social followers
        if intel.get("social_social_total_followers"):
            intel_snapshot["social_total_followers"] = intel["social_social_total_followers"]
            intel_snapshot["social_summary"] = intel.get("social_social_summary", "")
        # Crawl data
        if intel.get("crawl_total_testimonials"):
            intel_snapshot["testimonial_count"] = intel["crawl_total_testimonials"]
        if intel.get("crawl_all_prices"):
            intel_snapshot["prices_found"] = intel["crawl_all_prices"]
        # Mobile data from browser
        if intel.get("mobile_load_time_seconds"):
            intel_snapshot["mobile_load_time_seconds"] = intel["mobile_load_time_seconds"]
        if intel.get("mobile_cta_visible_above_fold") is not None:
            intel_snapshot["mobile_cta_visible"] = intel["mobile_cta_visible_above_fold"]
        # Form friction
        if intel.get("forms_longest_form_fields"):
            intel_snapshot["longest_form_fields"] = intel["forms_longest_form_fields"]
        # Booking branding
        if intel.get("bookingbrand_booking_page_branding_score"):
            intel_snapshot["booking_branding_score"] = intel["bookingbrand_booking_page_branding_score"]
        # Redirect hops
        if intel.get("redirects_booking_redirect_count"):
            intel_snapshot["booking_redirect_hops"] = intel["redirects_booking_redirect_count"]

        # Step 3: Run AI analysis with full data
        ai_results = run_ai_analysis(content_data, url, intel_snapshot=intel_snapshot or None)

        # Step 4: Navigator trigger evaluation + execution
        navigator_was_triggered = False
        should_trigger_navigator = False

        if not ai_results.get("ai_parse_error"):
            # Trigger condition 1: AI found no booking but suspects false negative
            if (ai_results.get("booking_tool") in (None, "None")
                    and ai_results.get("booking_false_negative_risk")):
                should_trigger_navigator = True
                logger.info(f"Navigator trigger: false_negative_risk for {url}")

            # Trigger condition 2: Low certainty on booking or positioning
            if (ai_results.get("ai_certainty_booking") or 1.0) < 0.5:
                should_trigger_navigator = True
                logger.info(f"Navigator trigger: low booking certainty for {url}")

            # Trigger condition 3: SPA/React framework detected
            html_lower = html.lower() if html else ""
            if any(marker in html_lower for marker in
                   ["__next", "reactdom", "_next/static", "react-root"]):
                should_trigger_navigator = True
                logger.info(f"Navigator trigger: SPA framework detected for {url}")

            # Trigger condition 4: AI and regex CONFLICT on booking tool
            regex_booking = intel.get("regex_booking_booking_tool", "None detected")
            if (ai_results.get("booking_tool") in (None, "None")
                    and regex_booking not in ("None detected", "Contact form")):
                should_trigger_navigator = True
                logger.info(f"Navigator trigger: AI/regex booking conflict for {url}")

            # Execute Navigator if triggered
            if should_trigger_navigator:
                try:
                    from scraper import _run_playwright_task
                    nav_result = _run_playwright_task(
                        "_claygent_navigator_browse", url
                    )
                    if nav_result and nav_result.get("content"):
                        nav_content_data = _process_navigator_content(
                            nav_result, url, content_data
                        )
                        if nav_content_data:
                            second_pass = run_ai_analysis(
                                nav_content_data, url,
                                intel_snapshot=intel_snapshot
                            )
                            if not second_pass.get("ai_parse_error"):
                                _merge_navigator_results(ai_results, second_pass)
                                navigator_was_triggered = True
                                logger.info(
                                    f"Navigator second pass completed for {url}"
                                )
                except Exception as nav_err:
                    logger.warning(f"Navigator failed for {url}: {nav_err}")

        # Step 5: Build regex_results dict for conflict detection
        regex_results = {
            k: v for k, v in intel.items()
            if k.startswith(("regex_", "antisignal_"))
        }

        # Step 6: Conflict detection
        if not ai_results.get("ai_parse_error"):
            conflict_data = detect_conflicts(ai_results, regex_results)
            ai_results.update(conflict_data)
        else:
            ai_results["confidence_booking"] = "NONE"
            ai_results["confidence_pricing"] = "NONE"
            ai_results["confidence_positioning"] = "NONE"
            ai_results["confidence_conflicts"] = [
                "AI parse failed — no conflict detection"
            ]

        # Step 7: Metadata
        ai_results["claygent_status"] = (
            "success" if not ai_results.get("ai_parse_error") else "partial"
        )
        ai_results["claygent_tokens_in"] = ai_results.get("ai_tokens_in", 0)
        ai_results["claygent_tokens_out"] = ai_results.get("ai_tokens_out", 0)
        ai_results["claygent_cost_usd"] = ai_results.get("ai_cost_usd", 0.0)
        ai_results["claygent_navigator_triggered"] = navigator_was_triggered
        ai_results["claygent_navigator_should_trigger"] = should_trigger_navigator
        ai_results["claygent_second_pass"] = navigator_was_triggered
        ai_results["claygent_processing_seconds"] = round(
            time.time() - start_time, 2
        )
        ai_results["claygent_platform"] = content_data.get("platform")
        ai_results["claygent_content_length"] = content_data.get(
            "content_length", 0
        )
        ai_results["claygent_subpages_fetched"] = content_data.get(
            "subpages_fetched", []
        )
        ai_results["claygent_had_pagespeed"] = (
            "pagespeed_score_performance" in intel_snapshot
        )
        ai_results["claygent_had_fb_ads"] = "fbads_fb_ads_found" in intel_snapshot

        return ai_results

    except Exception as e:
        logger.error(f"Claygent failed for {url}: {e}")
        return {
            "claygent_error": str(e)[:200],
            "claygent_status": "failed",
            "claygent_processing_seconds": round(time.time() - start_time, 2),
        }


# ---------------------------------------------------------------------------
# 3.0  Batch Processing API (50% cost savings)
# ---------------------------------------------------------------------------

def prepare_batch_request(content_data, url, intel_snapshot=None):
    """Build the prompt for a single lead WITHOUT calling the API.

    Returns the system prompt and user message that would be sent to Claude,
    ready to be included in a Message Batches API request.

    Args:
        content_data: dict from collect_content()
        url: base URL
        intel_snapshot: optional dict of already-collected intel

    Returns:
        dict with "system", "user_message" keys, or None if inputs are invalid.
    """
    if not content_data or not content_data.get("combined_content"):
        return None

    nav_formatted = "\n".join(
        f"  - {item['text']}: {item['href']}"
        for item in (content_data.get("nav_items") or [])[:15]
    ) or "  (none found)"

    booking_formatted = "\n".join(
        f"  - {item['text']}: {item['href']}"
        for item in (content_data.get("booking_links") or [])[:10]
    ) or "  (none found)"

    tech_section = _build_tech_section(intel_snapshot)

    _safe = lambda s: str(s).replace("{", "{{").replace("}", "}}")
    user_message = MEGA_PROMPT_TEMPLATE.format(
        url=_safe(url),
        platform=_safe(content_data.get("platform") or "Unknown"),
        nav_items_formatted=_safe(nav_formatted),
        booking_links_formatted=_safe(booking_formatted),
        technical_data_section=_safe(tech_section),
        combined_content=_safe(content_data.get("combined_content", "")),
    )

    return {
        "system": SYSTEM_PROMPT,
        "user_message": user_message,
    }


def build_intel_snapshot(intel):
    """Extract the intel_snapshot dict from a full intel dict.

    Same extraction logic as run_claygent() Step 2, factored out for reuse.
    """
    snapshot = {}
    for k in ("pagespeed_score_performance", "pagespeed_score_seo",
               "pagespeed_score_accessibility", "pagespeed_lcp_seconds",
               "pagespeed_fcp_seconds", "pagespeed_cls", "pagespeed_tbt_ms",
               "pagespeed_speed_index"):
        if intel.get(k) is not None:
            snapshot[k] = intel[k]
    if intel.get("fbads_fb_ads_found"):
        snapshot["fbads_fb_ads_found"] = True
        snapshot["fbads_fb_ads_count"] = intel.get("fbads_fb_ads_count", 0)
    if intel.get("social_social_total_followers"):
        snapshot["social_total_followers"] = intel["social_social_total_followers"]
        snapshot["social_summary"] = intel.get("social_social_summary", "")
    if intel.get("crawl_total_testimonials"):
        snapshot["testimonial_count"] = intel["crawl_total_testimonials"]
    if intel.get("crawl_all_prices"):
        snapshot["prices_found"] = intel["crawl_all_prices"]
    if intel.get("mobile_load_time_seconds"):
        snapshot["mobile_load_time_seconds"] = intel["mobile_load_time_seconds"]
    if intel.get("mobile_cta_visible_above_fold") is not None:
        snapshot["mobile_cta_visible"] = intel["mobile_cta_visible_above_fold"]
    if intel.get("forms_longest_form_fields"):
        snapshot["longest_form_fields"] = intel["forms_longest_form_fields"]
    if intel.get("bookingbrand_booking_page_branding_score"):
        snapshot["booking_branding_score"] = intel["bookingbrand_booking_page_branding_score"]
    if intel.get("redirects_booking_redirect_count"):
        snapshot["booking_redirect_hops"] = intel["redirects_booking_redirect_count"]
    return snapshot


def submit_batch(requests_list):
    """Submit a list of prepared requests to the Anthropic Message Batches API.

    Args:
        requests_list: list of dicts, each with:
            - "custom_id": unique identifier (e.g., row index as string)
            - "system": system prompt string
            - "user_message": user message string

    Returns:
        batch object from the API, or None on failure.
    """
    from scraper import ANTHROPIC_API_KEY

    if anthropic is None or not ANTHROPIC_API_KEY:
        logger.error("Cannot submit batch: anthropic SDK or API key not available")
        return []

    if not requests_list:
        logger.warning("Empty requests list — nothing to batch")
        return []

    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    batch_requests = []
    for req in requests_list:
        batch_requests.append(
            Request(
                custom_id=str(req["custom_id"]),
                params=MessageCreateParamsNonStreaming(
                    model=CLAUDE_MODEL,
                    max_tokens=2048,
                    system=[
                        {
                            "type": "text",
                            "text": req["system"],
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": req["user_message"]}],
                ),
            )
        )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Batches API supports up to 100,000 requests or 256 MB
    # Split into chunks of 10,000 if needed (safety margin)
    CHUNK_SIZE = 10_000
    batches = []
    for i in range(0, len(batch_requests), CHUNK_SIZE):
        chunk = batch_requests[i:i + CHUNK_SIZE]
        try:
            batch = client.messages.batches.create(requests=chunk)
            batches.append(batch)
            logger.info(
                f"Batch {len(batches)} submitted: {batch.id} "
                f"with {len(chunk)} requests"
            )
        except Exception as e:
            logger.error(f"Batch submission failed: {e}")
    return batches


def poll_batch(batch_id, callback=None, poll_interval=15, max_wait_seconds=86400):
    """Poll a batch until it completes or times out.

    Args:
        batch_id: the batch ID string
        callback: optional function(batch) called on each poll with current status
        poll_interval: seconds between polls (default 15)
        max_wait_seconds: maximum wait time before giving up (default 24h)

    Returns:
        final batch object when processing has ended.

    Raises:
        TimeoutError: if polling exceeds max_wait_seconds
    """
    from scraper import ANTHROPIC_API_KEY

    if anthropic is None or not ANTHROPIC_API_KEY:
        raise RuntimeError("anthropic SDK or API key not available for polling")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    start_time = time.time()

    while True:
        try:
            batch = client.messages.batches.retrieve(batch_id)
        except Exception as e:
            logger.error(f"Failed to retrieve batch {batch_id}: {e}")
            raise

        if callback:
            callback(batch)

        if batch.processing_status == "ended":
            return batch
        if batch.processing_status in ("canceling",):
            # Still transitioning — keep polling
            pass

        elapsed = time.time() - start_time
        if elapsed > max_wait_seconds:
            logger.error(f"Batch {batch_id} polling timed out after {elapsed:.0f}s")
            raise TimeoutError(
                f"Batch polling exceeded {max_wait_seconds}s limit"
            )

        time.sleep(poll_interval)


def retrieve_batch_results(batch_id):
    """Stream and collect all results from a completed batch.

    Args:
        batch_id: the batch ID string

    Returns:
        dict mapping custom_id → parsed AI result dict (same format as
        run_ai_analysis() output).
    """
    from scraper import ANTHROPIC_API_KEY

    if anthropic is None or not ANTHROPIC_API_KEY:
        raise RuntimeError("anthropic SDK or API key not available for retrieval")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    results = {}

    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id

        if result.result.type == "succeeded":
            message = result.result.message
            if not message.content:
                results[custom_id] = {
                    "ai_parse_error": True,
                    "ai_error": "Empty response from batch API",
                    "ai_batch_mode": True,
                }
                continue
            response_text = message.content[0].text if hasattr(message.content[0], "text") else str(message.content[0])
            tokens_in = getattr(message.usage, "input_tokens", 0)
            tokens_out = getattr(message.usage, "output_tokens", 0)

            ai_result = {
                "ai_raw": response_text,
                "ai_tokens_in": tokens_in,
                "ai_tokens_out": tokens_out,
                "ai_cost_usd": round(
                    (tokens_in * 1.5 / 1_000_000)  # Batch pricing: $1.50/MTok input
                    + (tokens_out * 7.5 / 1_000_000),  # Batch pricing: $7.50/MTok output
                    4,
                ),
                "ai_model": CLAUDE_MODEL,
                "ai_parse_error": False,
                "ai_batch_mode": True,
            }

            parsed = _parse_ai_response(response_text)
            if parsed is not None:
                ai_result.update(parsed)
            else:
                ai_result["ai_parse_error"] = True
                logger.warning(f"Failed to parse batch AI response for {custom_id}")

            results[custom_id] = ai_result

        elif result.result.type == "errored":
            error_msg = "Unknown batch error"
            if hasattr(result.result, "error") and result.result.error:
                error_msg = str(result.result.error)[:200]
            results[custom_id] = {
                "ai_parse_error": True,
                "ai_error": f"Batch error: {error_msg}",
                "ai_batch_mode": True,
            }

        elif result.result.type == "expired":
            results[custom_id] = {
                "ai_parse_error": True,
                "ai_error": "Batch request expired (24h timeout)",
                "ai_batch_mode": True,
            }

        elif result.result.type == "canceled":
            results[custom_id] = {
                "ai_parse_error": True,
                "ai_error": "Batch request was canceled",
                "ai_batch_mode": True,
            }

    return results


def apply_batch_results_to_intel(intel, ai_results, content_data):
    """Merge batch AI results into an intel dict + run conflict detection.

    This does the same post-processing as run_claygent() Steps 5-7,
    but without Navigator re-analysis (batch mode skips Navigator).

    Args:
        intel: the full intel dict from scrape_website(no_ai=True)
        ai_results: dict from retrieve_batch_results() for this lead
        content_data: the content_data dict from Phase 1

    Returns:
        updated intel dict with AI fields merged.
    """
    if ai_results.get("ai_parse_error"):
        # AI failed — attach what we have and return
        for k, v in ai_results.items():
            intel[k] = v
        intel["claygent_status"] = "batch_failed"
        return intel

    # Conflict detection (same as run_claygent Step 5-6)
    regex_results = {
        k: v for k, v in intel.items()
        if k.startswith(("regex_", "antisignal_"))
    }
    conflict_data = detect_conflicts(ai_results, regex_results)
    ai_results.update(conflict_data)

    # Metadata (same as run_claygent Step 7)
    ai_results["claygent_status"] = "batch_success"
    ai_results["claygent_tokens_in"] = ai_results.get("ai_tokens_in", 0)
    ai_results["claygent_tokens_out"] = ai_results.get("ai_tokens_out", 0)
    ai_results["claygent_cost_usd"] = ai_results.get("ai_cost_usd", 0.0)
    ai_results["claygent_navigator_triggered"] = False
    ai_results["claygent_navigator_should_trigger"] = False
    ai_results["claygent_second_pass"] = False
    ai_results["claygent_processing_seconds"] = 0  # Batch — no per-lead timing
    ai_results["claygent_had_pagespeed"] = intel.get("pagespeed_score_performance") is not None
    ai_results["claygent_had_fb_ads"] = bool(intel.get("fbads_fb_ads_found"))
    if content_data:
        ai_results["claygent_platform"] = content_data.get("platform")
        ai_results["claygent_content_length"] = content_data.get("content_length", 0)
        ai_results["claygent_subpages_fetched"] = content_data.get("subpages_fetched", [])

    # Merge AI fields into intel as primary (same as scraper.py line 3670-3695)
    for k, v in ai_results.items():
        intel[k] = v

    # Backwards compatibility mappings
    intel["ai_classification"] = intel.get("icp_classification", "")
    intel["ai_icp_fit"] = intel.get("icp_fit", "")
    intel["ai_audit_summary"] = intel.get("audit_summary", "")
    intel["ai_positioning_gaps"] = intel.get("positioning_gaps", "")
    intel["ai_outreach_hooks"] = intel.get("observation_hook") or ""
    if intel.get("secondary_observations"):
        secondary = intel["secondary_observations"]
        if isinstance(secondary, list):
            hooks = intel["ai_outreach_hooks"]
            joined = " | ".join(str(s) for s in secondary if s)
            intel["ai_outreach_hooks"] = (hooks + " | " + joined) if hooks else joined
    numeric = intel.get("icp_score_numeric", 0)
    if isinstance(numeric, (int, float)):
        intel["ai_overall_score"] = f"{round(numeric / 10, 1)}/10"
    else:
        intel["ai_overall_score"] = "N/A"
    intel["offer_offer_type"] = intel.get("offer_primary", "")
    intel["offer_price_range"] = intel.get("price_range", "")
    intel["offer_target_client"] = intel.get("offer_target_audience", "")
    intel["offer_sales_model"] = "Application-based" if intel.get("offer_requires_booking") else "Unknown"
    intel["booking_booking_tool"] = intel.get("booking_tool", "")
    intel["offer_solo_or_multi_coach"] = intel.get("regex_team_solo_or_multi_coach", "Unknown")

    return intel
