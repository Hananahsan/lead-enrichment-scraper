# CLAYGENT BUILD PLAN — Claude Code Implementation Spec
**Version:** 2.1 (Audit-Corrected) | March 2026  
**Target:** Modify `scraper.py` + create `claygent.py` in the same directory  
**Architecture:** AI-First with regex silent validation  

---

## 0. READ THIS FIRST

This spec modifies the existing `scraper.py` (3,400+ lines) and adds a new `claygent.py` module (~800 lines). Before making ANY changes:

1. Read `scraper.py` completely — especially `scrape_website()` (line ~3124), `generate_ai_analysis()` (line ~2930), and the slow tasks block (line ~3376)
2. Do NOT delete any existing functions — regex functions get DEMOTED, not removed
3. All new Claygent fields use NO prefix — they become the primary fields. Existing regex fields get RENAMED with `regex_` prefix
4. Backwards compatibility is mandatory — legacy field names must still appear in output CSV (see Section 7)

---

## 1. ARCHITECTURE OVERVIEW

### The Pipeline Shift

```
CURRENT FLOW:
  fetch_homepage() → 10 regex analyze_*() functions → generate_ai_analysis(Haiku reads summary) → intel dict

NEW FLOW:
  fetch_homepage() → [PARALLEL: Claygent(Sonnet reads raw pages) + PageSpeed + FB ads + crawl + social + regex_validate()] → merge + conflict detect → intel dict
```

### File Structure

```
project/
├── scraper.py          # MODIFIED — Claygent integration, regex demoted, Haiku removed
├── claygent.py         # NEW — AI engine, content collector, conflict detector, navigator
├── .env                # UNCHANGED — already has ANTHROPIC_API_KEY
└── (everything else unchanged)
```

### Key Design Rules

1. **Claygent NEVER re-fetches the homepage.** It receives `soup` and `html` from `scrape_website()`.
2. **Claygent reuses `_http_session` and `fetch_page_simple()`** for subpage fetching.
3. **Claygent uses the `anthropic` Python SDK** (already installed for Haiku). NOT raw HTTP requests.
4. **Navigator uses `_run_playwright_task()` subprocess pattern** (same as FB ads/social). NOT `_browser_pool` directly — avoids deadlock.
5. **All threading/concurrency matches scraper.py's sync model.** No asyncio. No async/await.
6. **Fast mode:** Claygent still runs but with homepage-only content (no subpage fetching). Add `--fast-no-ai` flag to skip completely.

---

## 2. claygent.py — MODULE SPECIFICATION

### 2.1 Imports and Config

```python
import json
import logging
import re
import threading
import time
from urllib.parse import urljoin, urlparse

# Import from scraper.py (same directory)
from scraper import (
    _http_session, fetch_page_simple, _run_playwright_task,
    ANTHROPIC_API_KEY, BOOKING_TOOLS, HAS_CLAUDE, HAS_PLAYWRIGHT,
    _get_headers, _USER_AGENTS, _BROWSER_USER_AGENTS
)

try:
    import anthropic
except ImportError:
    anthropic = None

logger = logging.getLogger("claygent")

CLAUDE_MODEL = "claude-sonnet-4-20250514"  # Pinned — NOT "latest"
MAX_CONTENT_CHARS = 50_000  # ~12,500 tokens — keeps cost at ~$0.04/lead
MAX_SUBPAGES = 6  # Cap subpage fetches

# CRITICAL FIX (Audit Issue 1): Rate limit Sonnet calls across all concurrent leads
_api_semaphore = threading.Semaphore(5)  # Max 5 concurrent Sonnet calls regardless of lead count
```

### 2.2 Content Collector

**Function:** `collect_content(soup, html, url, subpages, fast=False)`

**Purpose:** Takes the already-fetched homepage soup and enriches it with key subpage content for the AI to read.

**Inputs:**
- `soup` — BeautifulSoup object from `scrape_website()` (already in memory)
- `html` — raw HTML string from `scrape_website()` (already in memory)
- `url` — base URL
- `subpages` — dict from `find_subpages()` (already in memory), e.g. `{"pricing": "https://...", "services": "https://..."}`
- `fast` — if True, skip subpage fetching (use homepage only)

**Logic:**

```
1. Extract metadata from soup:
   - platform: detect from HTML markers (Squarespace, WordPress, Webflow, Wix, Kajabi, ClickFunnels, GoHighLevel, Kartra, Keap, Shopify)
     USE the same detection logic as scraper.py's detect_automation_platforms() markers, PLUS:
     - "squarespace" / "static.squarespace" / "sqs-" in html → "Squarespace"
     - "webflow.com" / "w-webflow" in html → "Webflow"
     - "wp-content" / "wp-includes" in html → "WordPress"
     - "wix.com" in html → "Wix"
     - Also check meta generator tag
   - nav_items: extract all <a> tags inside <nav> elements, return [{text, href}]
   - booking_links: extract all links where href or text matches booking keywords
     (book, schedule, calendar, consult, discovery, call, appointment, meeting, session,
      calendly, acuity, cal.com, hubspot, tidycal, savvycal, oncehub, youcanbook)

2. If NOT fast mode: Fetch up to MAX_SUBPAGES key subpages using fetch_page_simple()
   - Priority order (fetch first match for each):
     a. subpages["pricing"] or discover /pricing, /invest, /packages from nav_items
     b. subpages["services"] or discover /services, /programs, /coaching, /work-with-me from nav_items
     c. /about or subpages["about"]
     d. First booking-related link found in step 1 (the actual booking page)
     e. /book, /schedule, /discovery-call, /free-consultation (try one that exists)
     f. /contact
   - Use ThreadPoolExecutor(max_workers=4) for parallel fetching
   - 8-second timeout per page
   - IMPORTANT (Audit Issue 3): Do NOT re-fetch URLs that crawl_site() will also fetch.
     Since Claygent runs in parallel with crawl, we can't avoid ALL overlap, but cap at 6 pages
     and use common pages crawl won't prioritize (booking, pricing, work-with-me).

3. Clean each page's content:
   - Remove <script>, <style>, <noscript>, <iframe>, <svg> tags
   - Preserve heading structure: prefix h1/h2/h3/h4 text with [H1]/[H2]/etc.
   - Preserve link text with href: "Book Now (https://calendly.com/...)"  
   - Preserve list items with bullet prefix
   - Deduplicate adjacent identical lines
   - Strip excessive whitespace

4. Combine into one text block:
   === HOMEPAGE ===
   Title: {title}
   {cleaned homepage content}
   
   === /PRICING ===
   Title: {title}
   {cleaned pricing content}
   
   ... (each subpage)

5. Truncate combined content to MAX_CONTENT_CHARS (50,000 chars)

6. Return dict:
   {
       "combined_content": str,  # The text block for Claude
       "platform": str or None,  # Detected platform
       "nav_items": list[dict],  # [{text, href}]
       "booking_links": list[dict],  # [{text, href}]
       "subpages_fetched": list[str],  # URLs of pages collected
       "content_length": int,  # Character count
   }
```

### 2.3 AI Engine

**Function:** `run_ai_analysis(content_data, url, intel_snapshot=None)`

**Purpose:** Sends combined page content to Claude Sonnet with the mega-prompt. Returns structured intelligence.

**Inputs:**
- `content_data` — dict from `collect_content()`
- `url` — base URL
- `intel_snapshot` — optional dict of already-collected intel (PageSpeed scores, FB ads data, etc.) to give Claude more context. Pass None if running in parallel (data not yet available).

**Logic:**

```
1. Acquire _api_semaphore (blocks if 5 other leads are already calling Sonnet)
2. Build the user message:
   - PAGE METADATA section: url, platform, nav_items (first 15), booking_links (first 10)
   - If intel_snapshot provided: TECHNICAL DATA section with PageSpeed scores, FB ads found, social followers
   - PAGE CONTENT section: combined_content from content_data
3. Call anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create()
   - model = CLAUDE_MODEL
   - max_tokens = 2048
   - system = SYSTEM_PROMPT (see Section 3)
   - messages = [{"role": "user", "content": MEGA_PROMPT + user_message}]
4. Release _api_semaphore
5. Parse response:
   - Extract text from response.content[0].text
   - Strip markdown fences (```json ... ```)
   - json.loads() the response
   - On JSONDecodeError: try regex extraction of {...} block
   - On total failure: return {"parse_error": True, "raw_response": text}
6. Track tokens: response.usage.input_tokens, response.usage.output_tokens
7. Return dict with all parsed fields + metadata:
   {
       "ai_raw": str,  # Raw response for debugging
       "ai_tokens_in": int,
       "ai_tokens_out": int,
       "ai_cost_usd": float,  # Calculated from token counts
       "ai_model": str,
       "ai_parse_error": bool,
       ...all fields from parsed JSON (see Section 3 for schema)
   }
```

**Retry logic:**
- On anthropic.RateLimitError: wait 2^attempt seconds, retry up to 2 times
- On anthropic.APIError with status 5xx: wait 2^attempt seconds, retry up to 2 times  
- On anthropic.APIConnectionError: wait 3 seconds, retry once
- On any other exception: do not retry, return error dict

### 2.4 Conflict Detector

**Function:** `detect_conflicts(ai_results, regex_results)`

**Purpose:** Compares AI output to regex output. Determines AGREE / AI_UPGRADE / CONFLICT / AI_DOWNGRADE for overlapping fields. Produces the final merged intelligence dict.

**Inputs:**
- `ai_results` — dict from `run_ai_analysis()`
- `regex_results` — dict containing outputs from analyze_booking(), analyze_offer(), analyze_audience(), analyze_timing(), detect_automation_platforms()

**Comparison rules:**

```
BOOKING TOOL:
  ai_booking_tool vs regex_booking_tool
  - Both found same tool → AGREE (max confidence)
  - Regex = "None detected", AI found a tool → AI_UPGRADE
  - Regex found a SPECIFIC URL pattern (calendly.com, acuityscheduling.com, etc.), AI says "None" → AI_DOWNGRADE (regex wins)
  - Both found different tools → CONFLICT (flag for review)

PRICE:
  ai_price_range vs regex_price_range  
  - Both found prices → AGREE
  - Regex = "Not visible on site", AI found pricing → AI_UPGRADE
  - Regex found specific $ amounts, AI says different range → CONFLICT
  
AUTOMATION PLATFORM (anti-signal):
  ai_disqualifiers mentions GoHighLevel/ClickFunnels/Kajabi/Kartra/Keap vs regex antisignal_has_full_automation_platform
  - If regex detected it but AI missed it → AI_DOWNGRADE (regex wins — HARD DISQUALIFIER, never trust AI over regex here)
  
CONTACT FORM:
  ai says no booking tool detected, regex has_contact_form = True → Ensure AI didn't miss it
  
OFFER TYPE:
  Compare ai_offer_primary against regex_offer_type list — supplementary comparison only

AUDIENCE SIGNALS:
  Compare ai findings against regex has_podcast, has_youtube, has_active_blog — cross-check
```

**Override logic (Audit Issue 6):**

```python
# Compute EVIDENCE-BASED confidence, not AI self-reported
for field in compared_fields:
    if outcome == "AGREE":
        confidence[field] = "HIGH"  # Both sources agree
    elif outcome == "AI_UPGRADE":
        confidence[field] = "MEDIUM_HIGH"  # AI found more, can't verify with regex
    elif outcome == "CONFLICT":
        confidence[field] = "LOW"  # Disagreement — needs review
    elif outcome == "AI_DOWNGRADE":
        confidence[field] = "LOW"  # AI missed something regex found

# HARD OVERRIDE RULE: Regex ALWAYS wins on:
# 1. Specific booking tool URL patterns (regex found calendly.com in HTML)
# 2. Automation platform anti-signals (GoHighLevel, ClickFunnels, Kajabi, Kartra, Keap)
# In all other cases, AI is the primary source.
```

**Output:** Returns the final merged dict with:
- Primary fields (AI): `booking_tool`, `offer_primary`, `price_range`, `positioning`, `icp_score`, `campaign_bucket`, `observation_hook`, etc.
- Validation fields (regex): `regex_booking_tool`, `regex_price_range`, `regex_offer_type`, etc.
- Confidence: `confidence_booking`, `confidence_pricing`, `confidence_positioning`, etc. (evidence-based, NOT AI self-reported)
- Conflicts: `confidence_conflicts` — list of strings describing each comparison outcome

### 2.5 Navigator

**Function:** `run_navigator(url, trigger_reason)`

**Purpose:** Uses Playwright in a SUBPROCESS to click through JS-heavy pages and extract content behind interactions.

**CRITICAL (Audit Issue 2):** Navigator MUST use `_run_playwright_task()` (subprocess pattern), NOT `_browser_pool.acquire()`. The subprocess creates its own fresh browser, avoiding deadlock with the pool.

**When it triggers (called from `run_claygent()`):**
- AI says `booking_tool = "None"` AND `false_negative_risk = True`
- AI `confidence` on booking or positioning < 0.5
- Platform detected as React/Next.js (check for `__next`, `_next/static`, `react`, `reactDOM` in HTML)
- CONFLICT between AI and regex on booking_tool
- Hard cap: Navigator runs on max 30% of leads in any batch (tracked externally)

**Implementation:**

Create a standalone function in claygent.py that can be called via `_run_playwright_task()`:

```python
def _navigator_browse(url):
    """Standalone Playwright function for subprocess execution.
    Follows the coaching-site action sequence and returns extracted content."""
    # This function runs in its own subprocess — safe to use sync Playwright
    
    from playwright.sync_api import sync_playwright
    import random, time
    
    result = {"pages_visited": [], "content": {}, "errors": [], "actions_completed": 0}
    pw = None; browser = None; context = None
    
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            user_agent=random.choice(_BROWSER_USER_AGENTS),
        )
        page = context.new_page()
        
        # Step 1: Load homepage
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)
        result["pages_visited"].append(url)
        result["content"]["homepage"] = page.content()
        result["actions_completed"] += 1
        
        # Step 2: Scroll to bottom (catches lazy-loaded widgets)
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)
            result["content"]["after_scroll"] = page.content()
            result["actions_completed"] += 1
        except: pass
        
        # Step 3: Click nav items matching service/coaching pages
        nav_targets = ["Services", "Programs", "Work With Me", "Coaching", "About"]
        for target in nav_targets:
            try:
                locator = page.get_by_text(target, exact=False).first
                if locator.is_visible():
                    locator.click(timeout=3000)
                    page.wait_for_load_state("networkidle", timeout=8000)
                    page.wait_for_timeout(1000)
                    result["content"][f"nav_{target.lower()}"] = page.content()
                    result["pages_visited"].append(page.url)
                    result["actions_completed"] += 1
                    page.go_back(wait_until="networkidle", timeout=8000)
                    break  # Take first successful nav click
            except: continue
        
        # Step 4: Click booking CTAs
        cta_targets = ["Book a Call", "Book Now", "Schedule", "Discovery Call", 
                       "Free Consultation", "Get Started", "Apply Now", "Book a Session"]
        for target in cta_targets:
            try:
                locator = page.get_by_text(target, exact=False).first
                if locator.is_visible():
                    locator.click(timeout=3000)
                    page.wait_for_load_state("networkidle", timeout=8000)
                    page.wait_for_timeout(2000)
                    result["content"]["booking_page"] = page.content()
                    result["pages_visited"].append(page.url)
                    result["actions_completed"] += 1
                    break
            except: continue
        
        return result
        
    except Exception as e:
        result["errors"].append(str(e)[:200])
        return result
    finally:
        try: context and context.close()
        except: pass
        try: browser and browser.close()
        except: pass
        try: pw and pw.stop()
        except: pass
```

**Calling Navigator from run_claygent():**

```python
nav_result = _run_playwright_task("_navigator_browse", url)
if nav_result and nav_result.get("content"):
    # Clean and combine Navigator's content
    nav_content = clean_navigator_content(nav_result["content"])
    # Run a SECOND AI pass with the new content
    second_pass = run_ai_analysis(nav_content_data, url)
    # Merge: second pass overrides first pass where confidence is higher
```

### 2.6 Main Entry Point

**Function:** `run_claygent(url, soup, html, intel, subpages, fast=False)`

**Purpose:** Single entry point called from `scrape_website()` as a slow task. Orchestrates content collection → AI analysis → optional Navigator → conflict detection.

**Returns:** `("claygent", results_dict)` — matching the slow task return pattern.

**Logic:**

```
1. Collect content: content_data = collect_content(soup, html, url, subpages, fast)
2. Run AI analysis: ai_results = run_ai_analysis(content_data, url)
3. Check Navigator triggers:
   - If ai_results["booking_tool"] in (None, "None") AND ai_results.get("false_negative_risk"):
       → Run Navigator
   - If ai_results.get("ai_certainty_booking", 1.0) < 0.5:
       → Run Navigator
   - If platform in ("React", "Next.js") or "__next" in html or "reactDOM" in html.lower():
       → Run Navigator
   - If Navigator triggered:
       nav_result = _run_playwright_task("_navigator_browse", url)
       if new content found: run second AI pass, merge results
4. Return results dict
```

**CRITICAL (Audit Issue 9): Graceful degradation:**

```python
def run_claygent(url, soup, html, intel, subpages, fast=False):
    try:
        # ... normal Claygent logic ...
        return ("claygent", results)
    except Exception as e:
        logger.error(f"Claygent failed for {url}: {e}")
        # Return empty dict — scrape_website() will use regex-only output
        return ("claygent", {"claygent_error": str(e)[:200], "claygent_status": "failed"})
```

---

## 3. THE MEGA-PROMPT

### System Prompt

```
You are a precise website intelligence agent for KairosCal.io, a company that builds AI-powered booking infrastructure for coaches and consultants.

Your job: analyze coaching/consulting websites and return structured data for outreach qualification.

RULES:
1. Respond ONLY with valid JSON. No markdown, no preamble, no explanation.
2. If you cannot determine a field, return null — do NOT guess or fabricate.
3. Base ALL findings on the actual page content provided. Never infer from the URL alone.
4. For booking detection: look carefully for Squarespace built-in scheduling, Google Calendar Appointments, and tools loaded via JavaScript that may not appear as traditional URLs.
5. For positioning: read the ACTUAL homepage headline and about page language. "Life coach" as primary identity is a hard disqualifier. "Executive coach" is ideal.
6. For pricing: distinguish between "pricing hidden behind apply/call" (HIGH-TICKET signal, positive) and "no pricing found anywhere" (unclear signal, neutral).
```

### User Message Template

```
Analyze this coaching/consulting website and return a single JSON object.

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
A: Slow Site — page loads >3s on mobile
B: Generic Calendly — default Calendly/Acuity, no branding, no pre-call sequence
C: No Booking — contact form only, no scheduling tool (VERIFY CAREFULLY — Squarespace scheduling is often invisible)
D: Ad Spend Leak — running paid ads to basic booking page
E: New Launch — recently redesigned or launched site
F: Multi Coach Platform — multiple coaches, shared intake
G: Post Booking Gap — has booking tool but no confirmation/nurture sequence
H: Brand Credential Gap — generic/unbranded booking page despite polished site
I: Multiple Audiences — serves 2+ distinct buyer types on one path

=== BUILD SIZE ===
- Launchpad $4,500: Simple 1-3 page site, basic automation needed
- Growth Engine $9,000: Multi-page site, full infrastructure overhaul
- Empire $18,000: Complex multi-offer business, team of 3+

=== PAGE METADATA ===
URL: {url}
Platform: {platform}
Nav Items: {nav_items_formatted}
Booking-Related Links: {booking_links_formatted}

=== PAGE CONTENT ===
{combined_content}

=== RETURN THIS EXACT JSON SCHEMA ===
{
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
  
  "observation_hook": "The ONE specific, verifiable observation for the Email 1 cold email hook. Must be framed as a REVENUE problem. Must reference something specific to THIS site.",
  "secondary_observations": ["1-2 backup observations for Email 2"],
  
  "audit_summary": "2-3 sentence assessment of the business and its online presence",
  "positioning_gaps": "specific weaknesses costing them clients",
  
  "ai_certainty_booking": 0.0-1.0,
  "ai_certainty_positioning": 0.0-1.0,
  "ai_certainty_pricing": 0.0-1.0,
  "ai_certainty_icp": 0.0-1.0
}
```

---

## 4. MODIFICATIONS TO scraper.py

### 4.1 New Imports (add at top)

```python
# Add after existing imports (line ~66)
try:
    from claygent import run_claygent
    HAS_CLAYGENT = True
except ImportError:
    HAS_CLAYGENT = False
```

### 4.2 New CLI Flag (modify __main__ block)

```python
# Add --fast-no-ai flag support (line ~3613)
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scraper.py <input.csv> [output.csv] [--fast] [--fast-no-ai]")
        print("  --fast        Skip browser, FB ads, social, crawl. Claygent runs with homepage only.")
        print("  --fast-no-ai  Same as --fast but also skip Claygent AI (regex only, cheapest mode)")
        sys.exit(1)

    fast_mode = "--fast" in sys.argv or "--fast-no-ai" in sys.argv
    no_ai_mode = "--fast-no-ai" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    input_file = args[0]
    output_file = args[1] if len(args) > 1 else input_file.replace(".csv", "_enriched.csv")
    process_csv(input_file, output_file, fast=fast_mode, no_ai=no_ai_mode)
```

Update `process_csv()` to accept and pass `no_ai`:

```python
def process_csv(input_path, output_path, fast=False, no_ai=False):
    # ... existing code ...
    def process_row(idx, row):
        url = str(row[website_col]).strip()
        if not url or url.lower() in ("nan", "none", ""):
            return idx, {"scrape_status": "no_url"}
        print(f"  [{idx+1}/{len(df)}] Scraping {url}...")
        return idx, scrape_website(url, fast=fast, no_ai=no_ai)
```

### 4.3 Modify scrape_website() Signature

```python
def scrape_website(url, fast=False, no_ai=False):
```

### 4.4 Demote Regex Functions — RENAME Their Output Keys

In the "Fast analysis" section (line ~3290), change every regex output to use `regex_` prefix:

```python
# ---- Fast analysis (runs on already-fetched HTML, <1s each) ----
# These now serve as SILENT VALIDATION — AI is primary

# 1. Booking infrastructure (DEMOTED to validation)
booking = analyze_booking(soup, html, links, url)
for k, v in booking.items():
    intel[f"regex_booking_{k}"] = v  # CHANGED: was f"booking_{k}"

# 1b. Paid ads detection (STAYS PRIMARY — no AI equivalent)
ads = analyze_paid_ads(soup, html)
for k, v in ads.items():
    intel[f"ads_{k}"] = v  # UNCHANGED

# 2. Site performance (STAYS PRIMARY — AI can't measure load time)
perf = analyze_performance(resp, soup, url)
for k, v in perf.items():
    intel[f"perf_{k}"] = v  # UNCHANGED

# 3. Offer details (DEMOTED to validation)
offer = analyze_offer(soup, text, html, subpages, url)
for k, v in offer.items():
    intel[f"regex_offer_{k}"] = v  # CHANGED: was f"offer_{k}"

# 3b. Solo vs multi-coach (DEMOTED)
coach = analyze_solo_vs_multi(soup, text, html, subpages, url)
for k, v in coach.items():
    intel[f"regex_team_{k}"] = v  # CHANGED: was f"team_{k}"

# 4. Audience signals (DEMOTED to validation)
audience = analyze_audience(soup, text, html, links, subpages)
for k, v in audience.items():
    intel[f"regex_audience_{k}"] = v  # CHANGED: was f"audience_{k}"

# 5. Timing/recency signals (STAYS as-is — deterministic, no AI equivalent)
timing = analyze_timing(soup, text, html)
for k, v in timing.items():
    intel[f"timing_{k}"] = v  # UNCHANGED

# 6. Technical gaps (STAYS PRIMARY — AI can't test broken links)
tech_gaps = analyze_technical_gaps(soup, text, html, resp, links, booking)
for k, v in tech_gaps.items():
    intel[f"gaps_{k}"] = v  # UNCHANGED

# 7. Automation platform detection (STAYS PRIMARY — hard anti-signal, regex is more reliable)
# ... existing code UNCHANGED ...
for k, v in autoplatform.items():
    intel[f"antisignal_{k}"] = v  # UNCHANGED

# 8. Schema.org structured data (STAYS PRIMARY)
schema = check_structured_data(soup)
for k, v in schema.items():
    intel[f"schema_{k}"] = v  # UNCHANGED
```

### 4.5 Add Claygent as Slow Task

In the slow tasks section (line ~3376), add Claygent:

```python
# ---- Slow steps (network/browser calls) — ALL run in parallel ----
slow_results = {}

# ... existing _run_pagespeed, _run_fb_ads, _run_crawl, _run_social, etc. ...

def _run_claygent():
    """AI-first intelligence — reads raw pages with Claude Sonnet."""
    return run_claygent(url, soup, html, intel, subpages, fast=fast)

if fast:
    slow_tasks = [_run_pagespeed, _run_timing_network, _run_booking_redirects]
    if not no_ai and HAS_CLAYGENT and HAS_CLAUDE and ANTHROPIC_API_KEY:
        slow_tasks.append(_run_claygent)  # Claygent runs even in fast mode (homepage only)
else:
    slow_tasks = [
        _run_pagespeed, _run_fb_ads, _run_crawl, _run_social,
        _run_timing_network, _run_form_fields, _run_booking_brand,
        _run_booking_redirects,
    ]
    if HAS_CLAYGENT and HAS_CLAUDE and ANTHROPIC_API_KEY:
        slow_tasks.append(_run_claygent)
```

### 4.6 Merge Claygent Results

After the existing slow task merge code (line ~3500+), add Claygent merge:

```python
# ---- Merge Claygent AI results (primary intelligence layer) ----
claygent = slow_results.get("claygent", {})
if claygent and claygent.get("claygent_status") != "failed":
    # Claygent fields become PRIMARY (no prefix — these are the main fields now)
    for k, v in claygent.items():
        intel[k] = v
else:
    # GRACEFUL DEGRADATION (Audit Issue 9):
    # Claygent failed — promote regex fields back to primary names
    if not claygent or claygent.get("claygent_status") == "failed":
        # Copy regex_ fields to primary field names
        for key in list(intel.keys()):
            if key.startswith("regex_booking_"):
                primary_key = key.replace("regex_booking_", "booking_")
                intel[primary_key] = intel[key]
            elif key.startswith("regex_offer_"):
                primary_key = key.replace("regex_offer_", "offer_")
                intel[primary_key] = intel[key]
        # Run legacy Haiku analysis as fallback
        if HAS_CLAUDE and ANTHROPIC_API_KEY:
            ai = generate_ai_analysis(intel)
            for k, v in ai.items():
                intel[f"ai_{k}"] = v
```

### 4.7 Remove Haiku Call (when Claygent succeeds)

Replace the generate_ai_analysis() call at the end of scrape_website():

```python
# ---- AI analysis ----
# OLD (remove): 
# if HAS_CLAUDE and ANTHROPIC_API_KEY:
#     ai = generate_ai_analysis(intel)
#     for k, v in ai.items():
#         intel[f"ai_{k}"] = v

# NEW: Haiku only runs as fallback when Claygent failed (handled in merge section above)
# When Claygent succeeds, its fields replace all Haiku output
```

### 4.8 Backwards Compatibility Aliases (Audit Issue 5)

Add this after the Claygent merge section:

```python
# ---- Backwards compatibility: map Claygent fields to legacy field names ----
# These aliases ensure downstream processes (outreach-master skill, CLAUDE.md, batch workflow)
# that reference the old field names still work.
if claygent and claygent.get("claygent_status") != "failed":
    intel["ai_classification"] = intel.get("icp_classification", "")
    intel["ai_icp_fit"] = intel.get("icp_fit", "")
    intel["ai_audit_summary"] = intel.get("audit_summary", "")
    intel["ai_positioning_gaps"] = intel.get("positioning_gaps", "")
    intel["ai_outreach_hooks"] = intel.get("observation_hook", "")
    if intel.get("secondary_observations"):
        intel["ai_outreach_hooks"] += " | " + " | ".join(intel["secondary_observations"])
    # Map icp_score_numeric (0-100) to overall_score (X/10)
    numeric = intel.get("icp_score_numeric", 0)
    intel["ai_overall_score"] = f"{round(numeric / 10, 1)}/10"
    # Keep the old offer field names as aliases too
    intel["offer_offer_type"] = intel.get("offer_primary", "")
    intel["offer_price_range"] = intel.get("price_range", "")
    intel["offer_target_client"] = intel.get("offer_target_audience", "")
    intel["offer_sales_model"] = "Application-based" if intel.get("offer_requires_booking") else "Unknown"
    intel["booking_booking_tool"] = intel.get("booking_tool", "")
    intel["offer_solo_or_multi_coach"] = intel.get("regex_team_solo_or_multi_coach", "Unknown")
```

---

## 5. NAVIGATOR INTEGRATION

### Make _navigator_browse available for subprocess execution

In `claygent.py`, the `_navigator_browse(url)` function must be importable by `_run_playwright_task()`. Since `_run_playwright_task()` uses `from scraper import {func_name}`, and Navigator lives in `claygent.py`, you need to either:

**Option A (recommended):** Add a thin wrapper in `scraper.py`:
```python
def _claygent_navigator_browse(url):
    """Wrapper for claygent's navigator — used by _run_playwright_task subprocess."""
    from claygent import _navigator_browse
    return _navigator_browse(url)
```

Then call: `_run_playwright_task("_claygent_navigator_browse", url)`

**Option B:** Import `_navigator_browse` into scraper.py's namespace at the top.

---

## 6. COST TRACKING (Audit Issue 8)

Add to `run_claygent()` return dict:

```python
results["claygent_status"] = "success"
results["claygent_tokens_in"] = total_input_tokens
results["claygent_tokens_out"] = total_output_tokens
results["claygent_cost_usd"] = round((total_input_tokens * 3 / 1_000_000) + (total_output_tokens * 15 / 1_000_000), 4)
results["claygent_navigator_triggered"] = navigator_was_triggered
results["claygent_second_pass"] = ran_second_pass
results["claygent_processing_seconds"] = round(time.time() - start_time, 2)
```

Add batch-level cost summary at end of `process_csv()`:

```python
# After results are collected:
total_claygent_cost = sum(r.get("enriched_claygent_cost_usd", 0) for _, r in results if isinstance(r.get("enriched_claygent_cost_usd"), (int, float)))
nav_triggers = sum(1 for _, r in results if r.get("enriched_claygent_navigator_triggered"))
print(f"  - Claygent AI cost: ${total_claygent_cost:.2f}")
print(f"  - Navigator triggered: {nav_triggers}/{len(results)} leads ({nav_triggers/max(len(results),1)*100:.0f}%)")
if nav_triggers / max(len(results), 1) > 0.30:
    print(f"  ⚠️  WARNING: Navigator trigger rate above 30% — review content collector quality")
```

---

## 7. FIELD NAME REFERENCE

### Primary Fields (AI — no prefix)

These are the main fields downstream processes should read:

| Field | Source | Type | Description |
|-------|--------|------|-------------|
| `booking_tool` | AI | str | Detected booking tool name |
| `booking_url` | AI | str/null | Direct URL to booking page |
| `booking_branded` | AI | bool | Custom branding on booking page |
| `booking_pre_call_sequence` | AI | bool | Confirmation/nurture visible |
| `booking_cta_above_fold` | AI | bool | CTA visible without scroll |
| `booking_friction_score` | AI | int (1-10) | Booking friction rating |
| `booking_false_negative_risk` | AI | bool | Squarespace/GCal/SPA suspected |
| `booking_notes` | AI | str | Brief explanation |
| `offer_primary` | AI | str | Primary coaching offer |
| `offer_secondary` | AI | list | Other offers |
| `offer_requires_booking` | AI | bool | Call-based sales model |
| `offer_target_audience` | AI | str | Who they serve |
| `price_visible` | AI | bool | Pricing shown on site |
| `price_range` | AI | str | Price range or "Hidden (apply)" |
| `price_meets_2k_threshold` | AI | bool/null | Primary offer >= $2K |
| `positioning` | AI | str | business_outcome / personal_outcome / mixed |
| `positioning_detail` | AI | str | One sentence explanation |
| `primary_identity` | AI | str | Coach type label |
| `icp_score` | AI | str | GREEN / YELLOW / RED |
| `icp_score_numeric` | AI | int (0-100) | Weighted score |
| `icp_classification` | AI | str | Business model category |
| `icp_fit` | AI | str | YES / NO |
| `disqualifiers` | AI | list | Found disqualifiers |
| `campaign_bucket` | AI | str | A-I |
| `campaign_reason` | AI | str | Why this bucket |
| `build_size_estimate` | AI | str | Tier + price |
| `observation_hook` | AI | str | E1 cold email hook |
| `secondary_observations` | AI | list | E2 backup observations |
| `audit_summary` | AI | str | Business assessment |
| `positioning_gaps` | AI | str | Weaknesses costing clients |

### Validation Fields (Regex — `regex_` prefix)

| Field | Source | Description |
|-------|--------|-------------|
| `regex_booking_booking_tool` | Regex | analyze_booking() tool detection |
| `regex_booking_booking_links` | Regex | Found booking links |
| `regex_booking_has_contact_form` | Regex | Contact form detected |
| `regex_booking_pre_call_sequence_visible` | Regex | Pre-call keywords found |
| `regex_offer_offer_type` | Regex | Offer type pattern matches |
| `regex_offer_price_range` | Regex | Price regex findings |
| `regex_offer_sales_model` | Regex | Sales model detection |
| `regex_audience_has_podcast` | Regex | Podcast detected |
| `regex_audience_has_youtube` | Regex | YouTube detected |
| `regex_audience_testimonial_count` | Regex | Testimonial count |

### Confidence & Conflict Fields

| Field | Source | Description |
|-------|--------|-------------|
| `confidence_booking` | Conflict Detector | HIGH / MEDIUM_HIGH / LOW |
| `confidence_pricing` | Conflict Detector | HIGH / MEDIUM_HIGH / LOW |
| `confidence_positioning` | Conflict Detector | HIGH / MEDIUM_HIGH / LOW (always MEDIUM_HIGH — no regex equivalent) |
| `confidence_conflicts` | Conflict Detector | List of comparison outcomes |

### Backwards Compatibility Aliases (legacy names, mapped from AI fields)

| Legacy Field | Maps To |
|-------------|---------|
| `ai_classification` | `icp_classification` |
| `ai_icp_fit` | `icp_fit` |
| `ai_audit_summary` | `audit_summary` |
| `ai_positioning_gaps` | `positioning_gaps` |
| `ai_outreach_hooks` | `observation_hook` + `secondary_observations` |
| `ai_overall_score` | `icp_score_numeric` / 10 |
| `offer_offer_type` | `offer_primary` |
| `offer_price_range` | `price_range` |
| `booking_booking_tool` | `booking_tool` |

### Unchanged Fields (no modifications)

All `pagespeed_*`, `fbads_*`, `perf_*`, `ads_*`, `timing_*`, `gaps_*`, `antisignal_*`, `schema_*`, `social_*`, `forms_*`, `bookingbrand_*`, `redirects_*`, `confirmation_*`, `funnel_*`, `crawl_*` fields remain EXACTLY as they are today.

---

## 8. TESTING CHECKLIST

### Phase 1 Validation (must pass before shipping)

- [ ] **Batch 2 regression:** Run on the 5 known-failed leads. Expected results:
  - Dave → booking_tool = "Calendly" (not "None detected")
  - Laura → booking_tool = "Google Calendar" (not "None detected")  
  - Jillian → booking_tool = "Squarespace Scheduling" (not "None detected")
  - Simon Tomkins → icp_score = "RED", positioning = "personal_outcome", disqualifiers includes "life coach"
  - Peter Godard → icp_score = "RED", disqualifiers includes "DEI/equity coaching"

- [ ] **50-lead batch:** Run full enrichment. Check:
  - `claygent_status` = "success" for 90%+ of leads
  - `confidence_conflicts` shows at least 3 AI_UPGRADE outcomes (AI caught things regex missed)
  - Total `claygent_cost_usd` < $5
  - No lead takes >45 seconds total (including Claygent)

- [ ] **Fast mode:** Run with `--fast`. Check:
  - Claygent runs with homepage-only content
  - No subpage HTTP requests
  - All primary fields populated (may have lower confidence)

- [ ] **Fast-no-ai mode:** Run with `--fast-no-ai`. Check:
  - Claygent does NOT run
  - regex_ fields promoted to primary names (graceful degradation)
  - Output CSV identical to pre-Claygent format

- [ ] **API failure test:** Set ANTHROPIC_API_KEY to invalid value. Check:
  - Claygent fails gracefully
  - Regex fields promoted to primary names
  - generate_ai_analysis() Haiku runs as fallback
  - No crash, no missing fields

- [ ] **Backwards compatibility:** Compare output CSV column names against pre-Claygent output. All legacy column names must still exist (via aliases).

### Phase 2 Validation

- [ ] **Navigator triggers:** On a 50-lead batch, Navigator should trigger on 15-25%
- [ ] **Navigator subprocess:** Verify it uses `_run_playwright_task()`, NOT `_browser_pool.acquire()`
- [ ] **Navigator timeout:** Each Navigator run completes in <30 seconds
- [ ] **Content improvement:** On triggered leads, at least 50% produce new content Phase 1 missed
- [ ] **30% cap:** If >30% would trigger, extra triggers are skipped with a warning log

---

## 9. BUILD SEQUENCE

**Phase 1 (priority — build first):**
1. Create `claygent.py` with: Content Collector, AI Engine, Conflict Detector, `run_claygent()` entry point
2. Modify `scraper.py`: add import, rename regex fields to `regex_` prefix, add Claygent slow task, add merge logic, add backwards compatibility aliases, add graceful degradation, add `--fast-no-ai` flag, add cost tracking
3. Do NOT remove `generate_ai_analysis()` — keep it as fallback
4. Test against Phase 1 checklist

**Phase 2 (after Phase 1 validated):**
1. Add `_navigator_browse()` to `claygent.py`
2. Add `_claygent_navigator_browse()` wrapper to `scraper.py`
3. Wire Navigator trigger conditions into `run_claygent()`
4. Test against Phase 2 checklist

**Phase 3 (deferred):**
1. Add `ask_custom()` function to `claygent.py`
2. Add `--ask` CLI flag to `scraper.py`
3. Test custom prompts on 20-lead batch
