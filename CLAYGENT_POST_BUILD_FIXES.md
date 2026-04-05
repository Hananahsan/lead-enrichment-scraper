# CLAYGENT POST-BUILD FIXES — Claude Code Implementation Spec
**Priority:** Must apply before running on any real leads  
**Files to modify:** `claygent.py` + `scraper.py`  
**Risk level:** Low — surgical changes, no architecture modifications

---

## Fix 1: Soup Mutation Bug (CRITICAL)

### Problem

`_clean_page_content()` in `claygent.py` (line ~252) calls `tag.decompose()` on script/style/noscript/iframe/svg tags. When the homepage `soup` object is passed directly (line ~404), this DESTROYS those tags from the shared soup that `scrape_website()` is still using.

Other slow tasks running in parallel (especially `count_form_fields()` which receives the same `soup`) will get a mutated soup with missing script/style tags. This is a race condition that can produce incorrect form field counts and potentially crash parallel functions.

### File: `claygent.py`

### Location: `_clean_page_content()` function (line ~246)

### Current code:

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
```

### Why this works:

`BeautifulSoup(str(soup_or_html), "html.parser")` serializes the soup back to HTML string and re-parses it into a new, independent BeautifulSoup tree. All subsequent `decompose()` calls operate on this copy, leaving the original soup untouched. The cost is negligible — the homepage HTML is already in memory, and re-parsing a ~50-100KB string takes <10ms.

---

## Fix 2: `--no-ai` Flag Gap (HIGH)

### Problem

In full mode (non-fast), `scraper.py` always runs Claygent if the API key is set (line ~3448). There's no way to do a full enrichment run (browser, FB ads, social, crawl) WITHOUT Claygent except by unsetting the API key. The `no_ai` parameter only gates Claygent in fast mode.

This matters for:
- Testing scraper changes without burning API credits
- Cost control on exploratory batches
- A/B comparing Claygent-enriched vs regex-only output

### File: `scraper.py`

### Location 1: Full mode slow tasks (line ~3448)

### Current code:

```python
        if HAS_CLAYGENT and HAS_CLAUDE and ANTHROPIC_API_KEY:
            slow_tasks.append(_run_claygent)
```

### Replace with:

```python
        if not no_ai and HAS_CLAYGENT and HAS_CLAUDE and ANTHROPIC_API_KEY:
            slow_tasks.append(_run_claygent)
```

### Location 2: CLI flags (line ~3693-3701)

### Current code:

```python
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scraper.py <input.csv> [output.csv] [--fast] [--fast-no-ai]")
        print("  --fast        Skip browser, FB ads, social, crawl (10x faster for large batches)")
        print("  --fast-no-ai  Same as --fast but also skip Claygent AI (regex only, cheapest mode)")
        sys.exit(1)

    fast_mode = "--fast" in sys.argv or "--fast-no-ai" in sys.argv
    no_ai_mode = "--fast-no-ai" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
```

### Replace with:

```python
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scraper.py <input.csv> [output.csv] [--fast] [--no-ai] [--fast-no-ai]")
        print("  --fast        Skip browser, FB ads, social, crawl. Claygent runs with homepage only.")
        print("  --no-ai       Skip Claygent AI in any mode (full or fast). Regex only, zero API cost.")
        print("  --fast-no-ai  Same as --fast --no-ai combined.")
        sys.exit(1)

    fast_mode = "--fast" in sys.argv or "--fast-no-ai" in sys.argv
    no_ai_mode = "--no-ai" in sys.argv or "--fast-no-ai" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
```

### Why this works:

Now `--no-ai` works as a standalone flag in any mode:
- `python scraper.py leads.csv` → Full mode + Claygent (default)
- `python scraper.py leads.csv --no-ai` → Full mode, no Claygent (browser + FB ads + crawl, but regex-only intelligence)
- `python scraper.py leads.csv --fast` → Fast mode + Claygent with homepage only
- `python scraper.py leads.csv --fast --no-ai` → Fast mode, no Claygent
- `python scraper.py leads.csv --fast-no-ai` → Same as above (shorthand)

---

## Fix 3: Navigator Trigger Stubs (MEDIUM — prep for Phase 2)

### Problem

`run_claygent()` has `navigator_was_triggered = False` hardcoded but no trigger condition logic. When Phase 2 is built, the Navigator function needs to be called from specific conditions. Adding the trigger stubs now (with the actual Navigator call commented out) ensures Phase 2 integration is clean.

### File: `claygent.py`

### Location: `run_claygent()` function, between Step 2 (AI analysis) and Step 4 (conflict detection)

### Current code (line ~813-816):

```python
        # Step 4: Conflict detection (only if AI succeeded)
        navigator_was_triggered = False
        if not ai_results.get("ai_parse_error"):
```

### Replace with:

```python
        # Step 3: Navigator trigger evaluation (Phase 2 — stubs only)
        navigator_was_triggered = False
        should_trigger_navigator = False

        if not ai_results.get("ai_parse_error"):
            # Trigger condition 1: AI found no booking tool but suspects false negative
            if (ai_results.get("booking_tool") in (None, "None")
                    and ai_results.get("booking_false_negative_risk")):
                should_trigger_navigator = True
                logger.info(f"Navigator trigger: false_negative_risk for {url}")

            # Trigger condition 2: Low AI certainty on booking or positioning
            if ai_results.get("ai_certainty_booking", 1.0) < 0.5:
                should_trigger_navigator = True
                logger.info(f"Navigator trigger: low booking certainty for {url}")

            # Trigger condition 3: SPA/React framework detected
            html_lower = html.lower() if html else ""
            if any(marker in html_lower for marker in ["__next", "reactdom", "_next/static", "react-root"]):
                should_trigger_navigator = True
                logger.info(f"Navigator trigger: SPA framework detected for {url}")

            # Phase 2: Uncomment when _navigator_browse is implemented
            # if should_trigger_navigator:
            #     from scraper import _run_playwright_task
            #     nav_result = _run_playwright_task("_claygent_navigator_browse", url)
            #     if nav_result and nav_result.get("content"):
            #         nav_content_data = _process_navigator_content(nav_result, url)
            #         second_pass = run_ai_analysis(nav_content_data, url)
            #         if not second_pass.get("ai_parse_error"):
            #             # Merge: second pass overrides first where confidence is higher
            #             for key in second_pass:
            #                 if key.startswith("ai_certainty_"):
            #                     if (second_pass.get(key, 0) or 0) > (ai_results.get(key, 0) or 0):
            #                         ai_results.update({k: v for k, v in second_pass.items()
            #                                          if not k.startswith("ai_")})
            #             navigator_was_triggered = True

        # Step 4: Conflict detection (only if AI succeeded)
        if not ai_results.get("ai_parse_error"):
```

### Also add to the metadata section (line ~830):

### Current:

```python
        ai_results["claygent_navigator_triggered"] = navigator_was_triggered
```

### Replace with:

```python
        ai_results["claygent_navigator_triggered"] = navigator_was_triggered
        ai_results["claygent_navigator_should_trigger"] = should_trigger_navigator
```

### Why this works:

The trigger conditions are evaluated and logged but don't execute any Navigator code. When Phase 2 is built, uncomment the Navigator block and it's immediately functional. The `claygent_navigator_should_trigger` field in the output CSV tells you how many leads WOULD have triggered Navigator — useful for deciding when to prioritize Phase 2.

---

## Fix 4: Add Legacy Fallback Comment (LOW)

### File: `scraper.py`

### Location: `generate_ai_analysis()` function (line ~2930)

### Add this comment at the top of the function:

```python
def generate_ai_analysis(intel):
    """LEGACY FALLBACK — Use Claude Haiku to generate audit summary with outreach hooks.
    
    This function is the pre-Claygent AI analysis. It reads a SUMMARY of already-extracted
    regex fields (not raw page content). It only runs when Claygent fails or is disabled.
    
    When Claygent succeeds, this function is NOT called — Claygent's Sonnet-based analysis
    replaces all fields this function would produce (ai_classification, ai_icp_fit, 
    ai_audit_summary, ai_positioning_gaps, ai_outreach_hooks, ai_overall_score).
    
    Do NOT delete this function — it serves as the graceful degradation path.
    """
    data = {
```

---

## Verification After Fixes

Run these checks to confirm all fixes are applied correctly:

1. **Fix 1 (soup mutation):** Add a temporary assertion in `scrape_website()` after the slow tasks complete:
   ```python
   # Temp check: verify soup wasn't mutated by Claygent
   assert soup.find("script") is not None or len(soup.find_all("script")) == 0, "Soup mutated!"
   ```
   Run on 1 lead. If it passes, remove the assertion.

2. **Fix 2 (--no-ai flag):** Run:
   ```bash
   python scraper.py test.csv output.csv --no-ai
   ```
   Verify output CSV has NO `enriched_claygent_*` columns but DOES have all `enriched_booking_*` and `enriched_offer_*` columns (promoted from regex).

3. **Fix 3 (navigator stubs):** Run on 5 leads in full mode. Check output CSV for `enriched_claygent_navigator_should_trigger` column. At least 1 lead should have `True` (confirms trigger evaluation is working). All `enriched_claygent_navigator_triggered` should be `False` (confirms Navigator isn't actually running yet).

4. **Fix 4 (comment):** Visual check only — confirm the docstring is present at top of `generate_ai_analysis()`.
