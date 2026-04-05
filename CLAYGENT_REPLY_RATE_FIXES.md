# CLAYGENT REPLY-RATE FIXES — Claude Code Implementation Spec
**Priority:** All 5 items directly impact cold email reply rates  
**Files to modify:** `claygent.py` + `scraper.py`  
**Build order:** Items 1-4 in one pass, Item 5 (Navigator) as separate build  
**READ BOTH FILES COMPLETELY BEFORE STARTING**

---

## Item 1: Soup Mutation Bug Fix (30 minutes)

### Problem
`_clean_page_content()` in `claygent.py` calls `tag.decompose()` on script/style tags. When the homepage `soup` object is passed directly, it destroys those tags from the shared soup that `scrape_website()` is still using. Other parallel tasks (`count_form_fields()`, `check_booking_page_branding()`) read the same soup and get corrupted data. Wrong form field counts → wrong E2 observations → weaker follow-up emails.

### File: `claygent.py`
### Function: `_clean_page_content()`

### Find this code:
```python
def _clean_page_content(soup_or_html, title=None):
    from bs4 import BeautifulSoup

    if isinstance(soup_or_html, str):
        soup = BeautifulSoup(soup_or_html, "html.parser")
    else:
        soup = soup_or_html

    # Remove noise tags
    for tag_name in ["script", "style", "noscript", "iframe", "svg"]:
        for tag in soup.find_all(tag_name):
            tag.decompose()
```

### Replace with:
```python
def _clean_page_content(soup_or_html, title=None):
    from bs4 import BeautifulSoup

    # ALWAYS re-parse into a fresh copy. The homepage soup is shared with
    # parallel tasks (form_fields, booking_brand, etc.) — decompose() on the
    # original would destroy tags those tasks still need.
    if isinstance(soup_or_html, str):
        soup = BeautifulSoup(soup_or_html, "html.parser")
    else:
        soup = BeautifulSoup(str(soup_or_html), "html.parser")

    # Remove noise tags (safe — operating on an independent copy)
    for tag_name in ["script", "style", "noscript", "iframe", "svg"]:
        for tag in soup.find_all(tag_name):
            tag.decompose()
```

---

## Item 2: Timeline Hook Format in Mega-Prompt (1 hour)

### Problem
The current mega-prompt asks for "one specific, verifiable observation framed as a REVENUE problem." This produces problem hooks ("Your site takes 4s to load"), not timeline hooks ("Your site takes 4s to load — a coaching client with similar issues cut cost-per-call by 35% in the first 30 days"). KairosCal's own A/B data shows timeline hooks get 2.3x higher reply rates and 3.4x more meetings than problem hooks. This is the single highest-leverage change in this spec.

### File: `claygent.py`
### Location: `MEGA_PROMPT_TEMPLATE` string, the observation_hook and secondary_observations field descriptions

### Find this text inside the JSON schema in MEGA_PROMPT_TEMPLATE:
```
  "observation_hook": "The ONE specific, verifiable observation for the Email 1 cold email hook. Must be framed as a REVENUE problem. Must reference something specific to THIS site.",
  "secondary_observations": ["1-2 backup observations for Email 2"],
```

### Replace with:
```
  "observation_hook": "The ONE specific cold email hook for Email 1. MUST use TIMELINE HOOK format: [specific observation about THIS site] + [compressed achievement window with peer reference]. Structure: '[Observation] — a coaching client [with similar situation] [achieved specific result] [in specific timeframe].' Example: '{{companyName}} takes 4.2s to load on mobile — a coaching client with similar speed issues cut their cost-per-booked-call by 35% in the first 30 days.' The observation must be verifiable (the prospect can check it). The peer reference adds velocity and proof. NEVER write a plain problem statement without the timeline component.",
  "secondary_observations": ["1-2 backup observations for Email 2. Each must reference a DIFFERENT data point from the primary hook. Priority order (use the FIRST that applies with actual data): 1) PageSpeed score or load time, 2) form field count / friction, 3) testimonial count vs display, 4) CTA count or destination mismatch, 5) content recency dates, 6) social follower count vs engagement, 7) mobile layout issues, 8) redirect chain hops, 9) missing schema markup. Each observation must include the SPECIFIC number or data point, not a generic statement."],
```

### ALSO: Add timeline hook instruction to the system prompt section

### Find this text in SYSTEM_PROMPT:
```
6. For pricing: distinguish between "pricing hidden behind apply/call" (HIGH-TICKET signal, positive) and "no pricing found anywhere" (unclear signal, neutral).
```

### Add after it (append these two lines before the closing triple-quote):
```
7. For observation hooks: ALWAYS use timeline hook format. A timeline hook combines a specific observation with a compressed achievement window — it tells the prospect what's wrong AND how fast a peer fixed it with a specific result. Timeline hooks achieve 2.3x higher reply rates than plain problem statements. Never output a hook that only describes a problem without the timeline/peer/velocity component.
8. For secondary observations: NEVER repeat the same data point used in the primary observation_hook. If the primary hook uses PageSpeed/load time, the secondary MUST use a different category (form friction, testimonials, CTAs, etc.). Each observation must cite the specific number from the TECHNICAL DATA section.
```

---

## Item 3: Feed PageSpeed + All Slow Task Data to Claygent Sequentially (2-3 hours)

### Problem
Claygent currently runs IN PARALLEL with PageSpeed API, Facebook Ad Library, crawl, and social scraping. It usually finishes first (5-8 seconds vs 10-20 seconds for crawl). This means when Claygent generates the observation_hook, it does NOT have actual Lighthouse scores, Facebook ad counts, social follower data, or crawl results. For Campaign A (Slow Site) leads, the hook says something vague about speed instead of "your site scores 34/100 on mobile PageSpeed." The specific number is dramatically more compelling because the prospect can verify it.

### Architecture Change
Split Claygent into two phases:
1. **Content Collection** (subpage fetching) runs AS a parallel slow task — takes 2-5 seconds
2. **AI Analysis** (Sonnet API call) runs AFTER all slow tasks complete and merge — gets full intel dict with all data

Subpage fetching doesn't add latency. Only the API call (5-8 seconds) adds time after the parallel block.

### File: `claygent.py`

### Add this NEW function BEFORE `run_claygent()`:

```python
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
```

### Replace the ENTIRE `run_claygent()` function with:

```python
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
```

### Also update `run_ai_analysis()` tech section formatting:

### Find this code in `run_ai_analysis()`:
```python
    # Build optional technical data section
    tech_section = ""
    if intel_snapshot:
        tech_parts = []
        ps_perf = intel_snapshot.get("pagespeed_score_performance")
        if ps_perf is not None:
            tech_parts.append(f"PageSpeed Performance: {ps_perf}/100")
        ps_seo = intel_snapshot.get("pagespeed_score_seo")
        if ps_seo is not None:
            tech_parts.append(f"PageSpeed SEO: {ps_seo}/100")
        if intel_snapshot.get("fbads_fb_ads_found"):
            tech_parts.append(f"Facebook Ads: {intel_snapshot.get('fbads_fb_ads_count', 0)} active ads found")
        social_followers = intel_snapshot.get("social_social_total_followers")
        if social_followers:
            tech_parts.append(f"Social Followers: {social_followers}")
        if tech_parts:
            tech_section = "=== TECHNICAL DATA ===\n" + "\n".join(tech_parts)
```

### Replace with:
```python
    # Build technical data section from the FULL intel snapshot.
    # Claygent now runs AFTER all slow tasks — this contains real data.
    tech_section = ""
    if intel_snapshot:
        tech_parts = []
        # PageSpeed (real Lighthouse — critical for Campaign A hooks)
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
        # Facebook ads
        if intel_snapshot.get("fbads_fb_ads_found"):
            tech_parts.append(f"Facebook Ads Active: {intel_snapshot.get('fbads_fb_ads_count', 0)} ads running")
        # Social
        social = intel_snapshot.get("social_total_followers")
        if social:
            tech_parts.append(f"Total Social Followers: {social}")
            social_sum = intel_snapshot.get("social_summary", "")
            if social_sum:
                tech_parts.append(f"Social Breakdown: {social_sum}")
        # Crawl data
        testimonials = intel_snapshot.get("testimonial_count")
        if testimonials:
            tech_parts.append(f"Testimonials Found On Site: {testimonials}")
        prices = intel_snapshot.get("prices_found")
        if prices:
            tech_parts.append(f"Prices Found: {prices}")
        # Form friction
        form_fields = intel_snapshot.get("longest_form_fields")
        if form_fields:
            tech_parts.append(f"Longest Form: {form_fields} fields")
        # Booking branding
        brand_score = intel_snapshot.get("booking_branding_score")
        if brand_score:
            tech_parts.append(f"Booking Page Branding Score: {brand_score}")
        # Redirects
        redirects = intel_snapshot.get("booking_redirect_hops")
        if redirects:
            tech_parts.append(f"Booking Link Redirect Hops: {redirects}")
        if tech_parts:
            tech_section = "=== TECHNICAL DATA (real measurements — use exact numbers in hooks) ===\n" + "\n".join(tech_parts)
```

### File: `scraper.py` — restructure the slow tasks and merge

### Update the import at top of scraper.py:

Find:
```python
try:
    from claygent import run_claygent
    HAS_CLAYGENT = True
except ImportError:
    HAS_CLAYGENT = False
```

Replace with:
```python
try:
    from claygent import run_claygent, claygent_collect
    HAS_CLAYGENT = True
except ImportError:
    HAS_CLAYGENT = False
```

### Replace the Claygent slow task definition:

Find:
```python
    def _run_claygent():
        """AI-first intelligence — reads raw pages with Claude Sonnet."""
        return run_claygent(url, soup, html, intel, subpages, fast=fast)
```

Replace with:
```python
    def _run_claygent_collect():
        """Claygent Phase 1: Collect subpage content in parallel (no API call)."""
        return claygent_collect(url, soup, html, subpages, fast=fast)
```

### Replace BOTH slow_tasks Claygent entries:

Find (fast mode block):
```python
        if not no_ai and HAS_CLAYGENT and HAS_CLAUDE and ANTHROPIC_API_KEY:
            slow_tasks.append(_run_claygent)
```

Replace with:
```python
        if not no_ai and HAS_CLAYGENT and HAS_CLAUDE and ANTHROPIC_API_KEY:
            slow_tasks.append(_run_claygent_collect)
```

Find (full mode block — the other occurrence):
```python
        if not no_ai and HAS_CLAYGENT and HAS_CLAUDE and ANTHROPIC_API_KEY:
            slow_tasks.append(_run_claygent)
```

Replace with:
```python
        if not no_ai and HAS_CLAYGENT and HAS_CLAUDE and ANTHROPIC_API_KEY:
            slow_tasks.append(_run_claygent_collect)
```

### Replace the Claygent merge section:

Find:
```python
    # ---- Merge Claygent AI results (primary intelligence layer) ----
    claygent = slow_results.get("claygent", {})
    if claygent and claygent.get("claygent_status") != "failed":
```

Replace with:
```python
    # ---- Claygent Phase 2: AI Analysis (runs AFTER all slow tasks merge) ----
    # Claygent now has the FULL intel dict: PageSpeed scores, FB ads, social
    # followers, crawl data, form friction — real numbers for observation hooks.
    claygent_content = slow_results.get("claygent_content")
    claygent = {}

    if (not no_ai and HAS_CLAYGENT and HAS_CLAUDE and ANTHROPIC_API_KEY
            and claygent_content is not None):
        try:
            claygent = run_claygent(
                url, soup, html, intel, subpages,
                content_data=claygent_content,
                fast=fast,
            )
        except Exception as cg_err:
            logger.error(f"Claygent analysis failed for {url}: {cg_err}")
            claygent = {"claygent_status": "failed", "claygent_error": str(cg_err)[:200]}

    if claygent and claygent.get("claygent_status") != "failed":
```

**IMPORTANT:** Everything AFTER `if claygent and claygent.get("claygent_status") != "failed":` stays EXACTLY as-is. Do not modify the backwards compatibility aliases, graceful degradation, or Haiku fallback sections.

---

## Item 4: E2 Observation Priority Order (already handled by Item 2)

Item 2's replacement text for `secondary_observations` includes the full priority list and the "different category" rule. Item 2's system prompt addition (instruction #8) reinforces cross-email diversity. No additional changes needed.

---

## Item 5: Navigator Phase 2 — Full Build (1-2 days)

### Problem
10-20% of coaching sites hide booking tools behind JavaScript interactions: dropdown nav menus, React routes, modal popups, lazy-loaded widgets. Without Navigator, these leads get wrong campaign assignments and wrong observation hooks. The prospect reads an email claiming "there's no way to book on your site" — when there IS. That's an instant credibility kill.

### Architecture
Navigator uses `_run_playwright_task()` subprocess pattern (same as FB ads and social scraping). It does NOT acquire from `_browser_pool` — avoids deadlock. It's called from within `run_claygent()` AFTER the first AI pass, when trigger conditions are met. The trigger conditions and call site are already wired in Item 3's rewrite of `run_claygent()`.

Item 5 builds three functions that Item 3's code calls:
1. `_navigator_browse(url)` — the Playwright browsing function (runs in subprocess)
2. `_process_navigator_content(nav_result, url, original_content_data)` — cleans Navigator output
3. `_merge_navigator_results(first_pass, second_pass)` — merges second AI pass into first

### File: `claygent.py`
### Add these functions AFTER the conflict detector and BEFORE `claygent_collect()`:

```python
# ---------------------------------------------------------------------------
# 2.5  Navigator — Phase 2 (JS-heavy page interaction)
# ---------------------------------------------------------------------------

_NAV_USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/121.0.6167.66 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.6167.101 Mobile Safari/537.36",
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
    import time as _time

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
```

### File: `scraper.py`
### Add Navigator wrapper (AFTER `_run_playwright_task` definition, BEFORE `scrape_website()`):

```python
def _claygent_navigator_browse(url):
    """Wrapper for claygent Navigator — used by _run_playwright_task subprocess.

    _run_playwright_task imports from scraper.py, so Navigator needs this wrapper
    that imports from claygent.py. The subprocess gets a clean process.
    """
    from claygent import _navigator_browse
    return _navigator_browse(url)
```

---

## Verification Checklist

### Item 1 (Soup Mutation):
- [ ] Run on 1 lead with forms. `forms_longest_form_fields` should be non-zero.

### Item 2 (Timeline Hooks):
- [ ] Run on 3 leads. Every `observation_hook` must contain BOTH a specific observation AND a peer reference with timeframe. No plain problem statements.

### Item 3 (PageSpeed Sequential):
- [ ] Run on 1 slow site. Check `claygent_had_pagespeed` = True.
- [ ] Campaign A hook should reference actual PageSpeed score or LCP seconds.
- [ ] Total time: ~25-35 seconds per lead (15-25s parallel + 5-10s Claygent API).

### Item 4 (E2 Priority):
- [ ] Run on 5 leads. `secondary_observations` each reference a specific number. No lead has same data category in both hook and secondary.

### Item 5 (Navigator):
- [ ] Run on a React/SPA coaching site. `claygent_navigator_triggered` = True.
- [ ] Navigator completes in <30 seconds per lead.
- [ ] On 20-lead batch: triggers on 3-5 leads (15-25%).
- [ ] Does NOT increase `_browser_pool` acquire count (uses subprocess).
