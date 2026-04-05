# WORKFLOW GAPS — Claude Code Implementation Spec
**Version:** 1.0 | March 2026  
**Files to modify:** `app.py` + `index.html` + `scraper.py` (minor)  
**New file:** None — all additions are in existing files  
**Goal:** Bridge the gap between "enrichment complete" and "emails loaded in Instantly"

---

## READ FIRST — These Are Workflow Speed Improvements, Not Reply Rate Fixes

The reply-rate spec (`CLAYGENT_REPLY_RATE_FIXES.md`) handles data quality. This spec handles the manual time between enrichment output and email sending. Three gaps:

1. **Outreach-ready research briefs** — Generates the exact research brief format the outreach-master skill expects as input for email writing. Saves 30-60 minutes of manual reformatting per batch.
2. **Instantly-ready export** — Generates a CSV formatted for direct Instantly import with campaign-ready custom variables. Saves 20-30 minutes of CSV manipulation per batch.
3. **Batch quality summary** — Replaces Phase 1.5 manual verification with a visual dashboard showing GREEN/YELLOW/RED breakdown, conflicts, and Navigator flags. Saves 30-60 minutes of manual site-by-site checking.

---

## GAP 1: Outreach-Ready Research Briefs

### 1.1 What It Does

After a batch enrichment completes, generates a downloadable text file containing a structured research brief per GREEN lead. The brief matches the EXACT format expected by the outreach-master Claude skill (Mode 1: RESEARCH output), so the user can paste it directly into Claude Chat to trigger email writing.

### 1.2 The Brief Format (from outreach-master SKILL.md)

Each GREEN lead produces this text block:

```
PROSPECT: [Full Name] — [Company/Brand Name]
WEBSITE: [URL]
SCORE: [GREEN/YELLOW/RED] — [one-line justification]

OFFER: [Core offer, price point if visible]
BOOKING: [Tool used, friction score X/10, key issue]
SIGNAL: [Timing signal + strength level]
OBSERVATION: [The ONE specific thing for Email 1]
SECONDARY: [1-2 backup observations for Email 2]
BUILD SIZE: [Tier + price + brief rationale]
CAMPAIGN: [A/B/C/D/E/F/G/H/I + campaign name]

NOTES: [Disqualifiers spotted, unusual setup, confidence conflicts]
---
```

### 1.3 Backend — Add to app.py

**Add a new endpoint:**

```python
@app.route("/briefs/<job_id>")
def download_briefs(job_id):
    """Generate outreach-ready research briefs for GREEN leads."""
    if job_id not in jobs or jobs[job_id].get("status") != "done":
        return jsonify({"error": "Job not done yet"}), 404

    output_path = jobs[job_id].get("output_path")
    if not output_path or not os.path.exists(output_path):
        return jsonify({"error": "Output file not found"}), 404

    df = pd.read_csv(output_path)

    # Campaign bucket letter → full name mapping
    campaign_names = {
        "A": "A: Slow Site", "B": "B: Generic Calendly", "C": "C: No Booking",
        "D": "D: Ad Spend Leak", "E": "E: New Launch", "F": "F: Multi Coach Platform",
        "G": "G: Post Booking Gap", "H": "H: Brand Credential Gap", "I": "I: Multiple Audiences",
    }

    briefs = []
    green_count = 0
    yellow_count = 0
    red_count = 0

    for _, row in df.iterrows():
        # Determine ICP score — Claygent primary, legacy fallback
        icp_score = (
            _get(row, "enriched_icp_score")
            or _extract_icp_from_classification(_get(row, "enriched_ai_classification"))
            or "UNKNOWN"
        ).strip().upper()

        if icp_score == "GREEN":
            green_count += 1
        elif icp_score == "YELLOW":
            yellow_count += 1
        elif icp_score == "RED":
            red_count += 1
            continue  # Skip RED leads — no brief generated
        else:
            continue

        # Only generate briefs for GREEN leads
        if icp_score != "GREEN":
            continue

        # Find name and company from Apollo columns (input CSV)
        first_name = _get(row, "First Name", "") or _get(row, "first_name", "")
        last_name = _get(row, "Last Name", "") or _get(row, "last_name", "")
        full_name = f"{first_name} {last_name}".strip() or "Unknown"
        company = (
            _get(row, "Company", "")
            or _get(row, "Organization Name", "")
            or _get(row, "company", "")
            or _get(row, "enriched_offer_primary", "")
            or "Unknown"
        )
        website = _get(row, "Website", "") or _get(row, "website", "") or _get(row, "enriched_website_url", "")

        # Claygent fields (primary), with legacy fallbacks
        offer = _get(row, "enriched_offer_primary") or _get(row, "enriched_offer_offer_type", "Not determined")
        price = _get(row, "enriched_price_range") or _get(row, "enriched_offer_price_range", "Not visible")
        booking_tool = _get(row, "enriched_booking_tool") or _get(row, "enriched_booking_booking_tool", "Unknown")
        friction = _get(row, "enriched_booking_friction_score", "N/A")
        booking_notes = _get(row, "enriched_booking_notes", "")
        observation = _get(row, "enriched_observation_hook", "")
        secondary = _get(row, "enriched_secondary_observations", "")
        build_size = _get(row, "enriched_build_size_estimate", "Growth Engine $9,000")
        campaign_raw = _get(row, "enriched_campaign_bucket", "")
        campaign_reason = _get(row, "enriched_campaign_reason", "")
        classification = _get(row, "enriched_icp_classification") or _get(row, "enriched_ai_classification", "")
        positioning = _get(row, "enriched_positioning", "")
        disqualifiers = _get(row, "enriched_disqualifiers", "")
        conflicts = _get(row, "enriched_confidence_conflicts", "")
        confidence_booking = _get(row, "enriched_confidence_booking", "")

        # Format campaign
        campaign_letter = str(campaign_raw).strip().upper()[:1] if campaign_raw else ""
        campaign_full = campaign_names.get(campaign_letter, campaign_raw or "Not assigned")

        # Format signal/timing
        signal = ""
        if _get(row, "enriched_fbads_fb_ads_found") in (True, "True", "true"):
            ad_count = _get(row, "enriched_fbads_fb_ads_count", 0)
            signal = f"Running {ad_count} Facebook ads — VERY HIGH"
        elif _get(row, "enriched_timing_now_enrolling_banner") in (True, "True", "true"):
            signal = "Currently enrolling — HIGH"
        elif _get(row, "enriched_timing_site_looks_recent") and "actively" in str(_get(row, "enriched_timing_site_looks_recent")).lower():
            signal = "Actively maintained site — MODERATE"
        else:
            signal = "No strong timing signal — check LinkedIn"

        # Format secondary observations
        if secondary:
            try:
                secondary_list = json.loads(secondary) if isinstance(secondary, str) and secondary.startswith("[") else [secondary]
                secondary_formatted = "; ".join(str(s) for s in secondary_list)
            except (json.JSONDecodeError, TypeError):
                secondary_formatted = str(secondary)
        else:
            secondary_formatted = "Check PageSpeed data + form field count"

        # Format booking line
        booking_line = f"{booking_tool}, friction {friction}/10"
        if booking_notes:
            booking_line += f" — {booking_notes[:100]}"

        # Format notes
        notes_parts = []
        if positioning:
            notes_parts.append(f"Positioning: {positioning}")
        if disqualifiers and disqualifiers not in ("[]", ""):
            notes_parts.append(f"Disqualifiers: {disqualifiers}")
        if conflicts and conflicts not in ("[]", ""):
            notes_parts.append(f"Conflicts: {conflicts[:200]}")
        if confidence_booking:
            notes_parts.append(f"Booking confidence: {confidence_booking}")
        notes_line = "; ".join(notes_parts) if notes_parts else "None"

        brief = f"""PROSPECT: {full_name} — {company}
WEBSITE: {website}
SCORE: {icp_score} — {classification}

OFFER: {offer}, {price}
BOOKING: {booking_line}
SIGNAL: {signal}
OBSERVATION: {observation}
SECONDARY: {secondary_formatted}
BUILD SIZE: {build_size}
CAMPAIGN: {campaign_full}{' — ' + campaign_reason if campaign_reason else ''}

NOTES: {notes_line}
---"""
        briefs.append(brief)

    # Build header
    header = f"""KAIROSCAL OUTREACH BRIEFS
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
Batch: {len(df)} total leads | {green_count} GREEN | {yellow_count} YELLOW | {red_count} RED
Briefs below: {len(briefs)} GREEN leads ready for email writing

Paste each brief into Claude (outreach-master skill) to generate the V7 email sequence.
{'='*60}

"""
    content = header + "\n\n".join(briefs)

    # Save as text file
    briefs_path = os.path.join(app.config["OUTPUT_FOLDER"], f"{job_id}_briefs.txt")
    with open(briefs_path, "w") as f:
        f.write(content)

    original_name = jobs[job_id].get("original_name", "leads")
    date_str = datetime.now().strftime("%Y-%m-%d")
    return send_file(
        briefs_path,
        as_attachment=True,
        download_name=f"{original_name}_outreach_briefs_{date_str}.txt",
        mimetype="text/plain",
    )


def _get(row, key, default=""):
    """Safely get a value from a DataFrame row, handling NaN/None."""
    val = row.get(key)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    return val


def _extract_icp_from_classification(classification_text):
    """Extract YES/NO/GREEN/RED from legacy ai_classification text."""
    if not classification_text:
        return None
    text = str(classification_text).upper()
    if "A)" in text or "SOLO COACH" in text:
        return "GREEN"
    if any(x in text for x in ["B)", "C)", "D)", "E)"]):
        return "RED"
    return None
```

**Add `_get` and `_extract_icp_from_classification` as module-level helper functions** in `app.py`, before the route definitions. They're used by multiple endpoints.

### 1.4 Verification

- [ ] Run a 10-lead batch. Download briefs. Each GREEN lead should produce a brief matching the format exactly.
- [ ] Paste one brief into Claude with outreach-master skill active. It should trigger Mode 2 (EMAIL) and write an E1 immediately without asking for more context.
- [ ] RED leads should NOT appear in the briefs file.
- [ ] The header should show correct GREEN/YELLOW/RED counts.

---

## GAP 2: Instantly-Ready Export

### 2.1 What It Does

After batch enrichment, generates a CSV formatted for direct Instantly import — one row per GREEN lead, with Instantly's expected columns and KairosCal's custom variables pre-filled. The user downloads this CSV and uploads it directly to an Instantly campaign. Zero manual reformatting.

### 2.2 Instantly Import Column Format

Instantly expects these columns:

**Required by Instantly:**
- `email` — The lead's email address
- `first_name` — First name for {{firstName}} variable
- `last_name` — Last name
- `company_name` — Company name (cleaned: no Inc., LLC, ®)

**Custom variables for KairosCal V7 email templates:**
- `{{companyName}}` — Cleaned company name for email templates
- `{{firstName}}` — First name
- `{{observation}}` — The E1 observation hook (timeline format)
- `{{secondary_obs}}` — E2 backup observation
- `{{loadTime}}` — Mobile load time in seconds (for Campaign A hooks)
- `{{pageSpeedScore}}` — PageSpeed performance score (for Campaign A hooks)
- `{{bookingTool}}` — Current booking tool name (for Campaign B/G hooks)
- `{{campaignBucket}}` — Campaign letter for internal tracking
- `{{buildSize}}` — Build size estimate
- `{{costFrame}}` — Will be filled per-lead during email writing (left blank as placeholder)

### 2.3 Backend — Add to app.py

**Add a new endpoint:**

```python
@app.route("/instantly/<job_id>")
def download_instantly(job_id):
    """Generate Instantly-ready import CSV for GREEN leads."""
    if job_id not in jobs or jobs[job_id].get("status") != "done":
        return jsonify({"error": "Job not done yet"}), 404

    output_path = jobs[job_id].get("output_path")
    if not output_path or not os.path.exists(output_path):
        return jsonify({"error": "Output file not found"}), 404

    df = pd.read_csv(output_path)

    rows = []
    for _, row in df.iterrows():
        # Only GREEN leads go to Instantly
        icp_score = (
            _get(row, "enriched_icp_score")
            or _extract_icp_from_classification(_get(row, "enriched_ai_classification"))
            or ""
        ).strip().upper()

        if icp_score != "GREEN":
            continue

        # Skip if no email
        email = _get(row, "Email") or _get(row, "email") or _get(row, "Email Address") or ""
        if not email or "@" not in str(email):
            continue

        # Extract fields from input CSV (Apollo columns)
        first_name = _get(row, "First Name") or _get(row, "first_name", "")
        last_name = _get(row, "Last Name") or _get(row, "last_name", "")
        company_raw = (
            _get(row, "Company")
            or _get(row, "Organization Name")
            or _get(row, "company", "")
        )

        # Clean company name — drop Inc., LLC, ®, etc.
        company_clean = _clean_company_name(company_raw)

        # Claygent enrichment fields
        observation = _get(row, "enriched_observation_hook", "")
        secondary = _get(row, "enriched_secondary_observations", "")
        campaign_bucket = _get(row, "enriched_campaign_bucket", "")
        build_size = _get(row, "enriched_build_size_estimate", "")
        booking_tool = _get(row, "enriched_booking_tool") or _get(row, "enriched_booking_booking_tool", "")

        # PageSpeed data for Campaign A hooks
        load_time = _get(row, "enriched_pagespeed_lcp_seconds") or _get(row, "enriched_mobile_load_time_seconds", "")
        ps_score = _get(row, "enriched_pagespeed_score_performance", "")

        # Format secondary observations
        if secondary:
            try:
                sec_list = json.loads(secondary) if isinstance(secondary, str) and secondary.startswith("[") else [secondary]
                secondary_formatted = sec_list[0] if sec_list else ""
            except (json.JSONDecodeError, TypeError):
                secondary_formatted = str(secondary)
        else:
            secondary_formatted = ""

        rows.append({
            "email": str(email).strip(),
            "first_name": str(first_name).strip(),
            "last_name": str(last_name).strip(),
            "company_name": company_clean,
            "{{companyName}}": company_clean,
            "{{firstName}}": str(first_name).strip(),
            "{{observation}}": str(observation).strip(),
            "{{secondary_obs}}": str(secondary_formatted).strip(),
            "{{loadTime}}": str(load_time).strip() if load_time else "",
            "{{pageSpeedScore}}": str(ps_score).strip() if ps_score else "",
            "{{bookingTool}}": str(booking_tool).strip(),
            "{{campaignBucket}}": str(campaign_bucket).strip(),
            "{{buildSize}}": str(build_size).strip(),
            "{{costFrame}}": "",  # Placeholder — filled during email writing
        })

    if not rows:
        return jsonify({"error": "No GREEN leads with email addresses found"}), 404

    instantly_df = pd.DataFrame(rows)
    instantly_path = os.path.join(app.config["OUTPUT_FOLDER"], f"{job_id}_instantly.csv")
    instantly_df.to_csv(instantly_path, index=False)

    original_name = jobs[job_id].get("original_name", "leads")
    date_str = datetime.now().strftime("%Y-%m-%d")
    return send_file(
        instantly_path,
        as_attachment=True,
        download_name=f"{original_name}_instantly_import_{date_str}.csv",
        mimetype="text/csv",
    )


def _clean_company_name(name):
    """Clean company name for email templates.
    Drops Inc., LLC, ®, etc. Tests possessives won't break.
    """
    if not name:
        return ""
    name = str(name).strip()
    # Remove common suffixes
    suffixes = [
        r",?\s*Inc\.?$", r",?\s*LLC\.?$", r",?\s*Ltd\.?$", r",?\s*LTD\.?$",
        r",?\s*Co\.?$", r",?\s*Corp\.?$", r",?\s*Corporation$",
        r",?\s*Limited$", r",?\s*PLC$", r",?\s*LP$",
        r"\s*®$", r"\s*™$",
    ]
    import re as _re
    for pattern in suffixes:
        name = _re.sub(pattern, "", name, flags=_re.IGNORECASE)
    name = name.strip().rstrip(",").strip()

    # Fix ampersand edge cases
    if name.endswith(" &") or name.endswith(" &amp;"):
        name = name.rsplit("&", 1)[0].strip()

    return name
```

### 2.4 Verification

- [ ] Run a 10-lead batch. Download Instantly CSV. Verify:
  - Only GREEN leads with email addresses appear
  - `company_name` has no "Inc.", "LLC", "®" suffixes
  - `{{observation}}` contains the timeline hook from Claygent
  - `{{loadTime}}` and `{{pageSpeedScore}}` are populated for leads with PageSpeed data
  - `{{campaignBucket}}` matches the enriched CSV's campaign bucket
- [ ] Upload the CSV to Instantly (test campaign, don't send). Verify all custom variables resolve correctly in email preview.

---

## GAP 3: Batch Quality Summary in Web UI

### 3.1 What It Does

Replaces Phase 1.5 manual verification. When a batch completes, the "Done" step shows a quality dashboard instead of just success/fail/skipped counts. The dashboard shows:

- GREEN / YELLOW / RED breakdown
- Campaign bucket distribution
- Leads with AI-regex conflicts (the ones that used to need manual checking)
- Leads flagged as `booking_false_negative_risk` (Navigator candidates)
- Total Claygent cost
- Download buttons for: enriched CSV, outreach briefs, Instantly import

### 3.2 Backend — Modify app.py

**Modify `run_scrape_job()` to collect batch quality stats after all leads complete.**

**Find the section after the CSV is saved (around line 119-122):**

```python
        output_df.to_csv(output_path, index=False)

        jobs[job_id]["status"] = "done"
        jobs[job_id]["output_path"] = output_path
```

**Replace with:**

```python
        output_df.to_csv(output_path, index=False)

        # ---- Batch quality summary ----
        quality = _compute_batch_quality(results)
        jobs[job_id]["status"] = "done"
        jobs[job_id]["output_path"] = output_path
        jobs[job_id]["quality"] = quality
```

**Add this function to app.py (module level):**

```python
def _compute_batch_quality(results):
    """Compute batch quality summary from enrichment results.
    Returns a dict for the web UI to render.
    """
    quality = {
        "icp_green": 0,
        "icp_yellow": 0,
        "icp_red": 0,
        "icp_unknown": 0,
        "campaigns": {},       # {"A": 3, "B": 5, ...}
        "conflicts": [],       # [{lead, conflict_type, detail}]
        "false_negatives": [],  # [{lead, booking_tool_ai, booking_tool_regex}]
        "claygent_cost": 0.0,
        "claygent_ran": 0,
        "navigator_should_trigger": 0,
        "navigator_triggered": 0,
        "avg_confidence_booking": "",
        "green_leads": [],     # [{name, company, url, campaign, build_size, score}]
    }

    confidence_booking_counts = {"HIGH": 0, "MEDIUM_HIGH": 0, "LOW": 0, "NONE": 0}

    for idx, intel in results:
        if not isinstance(intel, dict):
            continue

        # ICP score
        icp = str(intel.get("enriched_icp_score") or intel.get("enriched_ai_icp_fit") or "").strip().upper()
        if icp == "GREEN" or icp == "YES":
            quality["icp_green"] += 1
        elif icp == "YELLOW":
            quality["icp_yellow"] += 1
        elif icp == "RED" or icp == "NO":
            quality["icp_red"] += 1
        else:
            # Try to extract from classification text
            classification = str(intel.get("enriched_icp_classification") or intel.get("enriched_ai_classification") or "")
            if "A)" in classification or "SOLO COACH" in classification.upper():
                quality["icp_green"] += 1
            elif any(x in classification for x in ["B)", "C)", "D)", "E)"]):
                quality["icp_red"] += 1
            else:
                quality["icp_unknown"] += 1

        # Campaign distribution
        campaign = str(intel.get("enriched_campaign_bucket", "") or "").strip().upper()[:1]
        if campaign and campaign in "ABCDEFGHI":
            quality["campaigns"][campaign] = quality["campaigns"].get(campaign, 0) + 1

        # Conflicts
        conflicts_raw = intel.get("enriched_confidence_conflicts")
        if conflicts_raw:
            try:
                conflict_list = json.loads(conflicts_raw) if isinstance(conflicts_raw, str) else conflicts_raw
                if isinstance(conflict_list, list):
                    for c in conflict_list:
                        c_str = str(c)
                        if "CONFLICT" in c_str or "AI_DOWNGRADE" in c_str:
                            url = intel.get("enriched_website_url", f"Lead #{idx}")
                            quality["conflicts"].append({
                                "lead": url,
                                "detail": c_str[:200],
                            })
            except (json.JSONDecodeError, TypeError):
                pass

        # False negative risk
        if intel.get("enriched_booking_false_negative_risk") in (True, "True", "true"):
            quality["false_negatives"].append({
                "lead": intel.get("enriched_website_url", f"Lead #{idx}"),
                "booking_ai": intel.get("enriched_booking_tool", ""),
                "booking_regex": intel.get("enriched_regex_booking_booking_tool", ""),
            })

        # Claygent stats
        cost = intel.get("enriched_claygent_cost_usd")
        if isinstance(cost, (int, float)):
            quality["claygent_cost"] += cost
        if intel.get("enriched_claygent_status") in ("success", "partial"):
            quality["claygent_ran"] += 1
        if intel.get("enriched_claygent_navigator_should_trigger") in (True, "True", "true"):
            quality["navigator_should_trigger"] += 1
        if intel.get("enriched_claygent_navigator_triggered") in (True, "True", "true"):
            quality["navigator_triggered"] += 1

        # Confidence booking
        cb = str(intel.get("enriched_confidence_booking", "")).upper()
        if cb in confidence_booking_counts:
            confidence_booking_counts[cb] += 1

        # Collect GREEN lead summaries
        if icp in ("GREEN", "YES"):
            quality["green_leads"].append({
                "name": f"{intel.get('enriched_first_name', '')} {intel.get('enriched_last_name', '')}".strip() or intel.get("enriched_website_url", ""),
                "company": intel.get("enriched_company", "") or intel.get("enriched_offer_primary", "")[:40],
                "url": intel.get("enriched_website_url", ""),
                "campaign": intel.get("enriched_campaign_bucket", ""),
                "build_size": intel.get("enriched_build_size_estimate", ""),
                "score": intel.get("enriched_icp_score_numeric", ""),
            })

    quality["claygent_cost"] = round(quality["claygent_cost"], 4)

    # Summarize confidence
    total_conf = sum(confidence_booking_counts.values())
    if total_conf > 0:
        dominant = max(confidence_booking_counts, key=confidence_booking_counts.get)
        quality["avg_confidence_booking"] = f"{dominant} ({confidence_booking_counts[dominant]}/{total_conf})"

    # Cap conflict/false_negative lists for JSON size
    quality["conflicts"] = quality["conflicts"][:20]
    quality["false_negatives"] = quality["false_negatives"][:20]
    quality["green_leads"] = quality["green_leads"][:50]

    return quality
```

**IMPORTANT:** The `results` list passed to `_compute_batch_quality` needs the raw data with `enriched_` prefixes. Modify the call site — `results` as built in `run_scrape_job()` contains `(idx, intel_data)` tuples where `intel_data` is already flattened with `enriched_` prefixes. So pass the `results_dict` items. Update the call:

```python
        # After building results list and saving CSV:
        quality = _compute_batch_quality(list(results_dict.items()))
```

Wait — looking at the code more carefully, `results_dict` stores `{idx: flattened_intel}` where values are already flattened with `enriched_` prefix (from `_flatten_intel`). But `_flatten_intel` converts lists/dicts to JSON strings. So `enriched_confidence_conflicts` will be a JSON string, not a list. The `_compute_batch_quality` function handles this with the `json.loads()` in the conflicts section. Correct.

Also need to handle that `results_dict` values don't have the original input CSV columns (first_name, company, etc.) — those are only in the original `df`. So `green_leads` summary might not have names. That's acceptable — use the website URL as the identifier.

### 3.3 Modify Status Endpoint

**In app.py, modify the `/status/<job_id>` endpoint** to include quality data when the job is done:

The current endpoint:

```python
@app.route("/status/<job_id>")
def status(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(jobs[job_id])
```

This already works — it returns all keys in the job dict, which now includes `quality`. The frontend just needs to read `data.quality` when `data.status === 'done'`. No backend change needed here.

### 3.4 Frontend — Modify index.html Done Step

**Find the "Step 4: Done" section (around line 461-476):**

```html
        <!-- Step 4: Done -->
        <div id="step-done" class="hidden">
            <div class="card">
                <div class="done-icon"><svg ...></svg></div>
                <div class="done-text"><h2>Enrichment Complete</h2><p id="done-summary"></p></div>
                <div class="stats-grid">
                    <div class="stat-card success"><div class="number" id="done-success">0</div><div class="label">Scraped</div></div>
                    <div class="stat-card failed"><div class="number" id="done-failed">0</div><div class="label">Failed</div></div>
                    <div class="stat-card skipped"><div class="number" id="done-skipped">0</div><div class="label">No URL</div></div>
                </div>
                <div class="btn-row" style="justify-content: center;">
                    <a class="btn btn-success" id="download-btn">Download Enriched CSV</a>
                    <button class="btn btn-outline" id="new-btn">Start New Enrichment</button>
                </div>
            </div>
        </div>
```

**Replace the entire done step with:**

```html
        <!-- Step 4: Done -->
        <div id="step-done" class="hidden">
            <div class="card">
                <div class="done-icon"><svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5" /></svg></div>
                <div class="done-text"><h2>Enrichment Complete</h2><p id="done-summary"></p></div>

                <!-- ICP Breakdown -->
                <div class="stats-grid" style="grid-template-columns: repeat(4, 1fr); margin-bottom: 16px;">
                    <div class="stat-card success"><div class="number" id="done-green">0</div><div class="label">GREEN</div></div>
                    <div class="stat-card" style="background:#422006;"><div class="number" style="color:#facc15;" id="done-yellow">0</div><div class="label">YELLOW</div></div>
                    <div class="stat-card failed"><div class="number" id="done-red">0</div><div class="label">RED</div></div>
                    <div class="stat-card"><div class="number" id="done-unknown">0</div><div class="label">Unknown</div></div>
                </div>

                <!-- Scrape Stats -->
                <div class="stats-grid" style="margin-bottom: 16px;">
                    <div class="stat-card success"><div class="number" id="done-success">0</div><div class="label">Scraped</div></div>
                    <div class="stat-card failed"><div class="number" id="done-failed">0</div><div class="label">Failed</div></div>
                    <div class="stat-card skipped"><div class="number" id="done-skipped">0</div><div class="label">No URL</div></div>
                </div>

                <!-- Quality Details (expandable) -->
                <div id="quality-details" class="hidden" style="margin-bottom: 16px;">
                    <!-- Campaign Distribution -->
                    <div id="quality-campaigns" class="hidden" style="background:#111113; border:1px solid #1e1e22; border-radius:10px; padding:16px; margin-bottom:10px;">
                        <h3 style="font-size:13px; color:#93c5fd; margin-bottom:10px;">Campaign Distribution</h3>
                        <div id="quality-campaign-bars"></div>
                    </div>

                    <!-- Conflicts -->
                    <div id="quality-conflicts" class="hidden" style="background:#111113; border:1px solid #1e1e22; border-radius:10px; padding:16px; margin-bottom:10px;">
                        <h3 style="font-size:13px; color:#f87171; margin-bottom:10px;">AI vs Regex Conflicts <span id="conflict-count" style="color:#52525b; font-weight:400;"></span></h3>
                        <div id="quality-conflict-list" style="font-size:12px; color:#a1a1aa; max-height:200px; overflow-y:auto;"></div>
                    </div>

                    <!-- False Negative Risks -->
                    <div id="quality-false-neg" class="hidden" style="background:#111113; border:1px solid #1e1e22; border-radius:10px; padding:16px; margin-bottom:10px;">
                        <h3 style="font-size:13px; color:#facc15; margin-bottom:10px;">Booking False Negative Risk <span id="false-neg-count" style="color:#52525b; font-weight:400;"></span></h3>
                        <div id="quality-false-neg-list" style="font-size:12px; color:#a1a1aa; max-height:150px; overflow-y:auto;"></div>
                    </div>

                    <!-- Claygent Stats -->
                    <div id="quality-claygent" class="hidden" style="background:#111113; border:1px solid #1e1e22; border-radius:10px; padding:16px; margin-bottom:10px;">
                        <h3 style="font-size:13px; color:#3b82f6; margin-bottom:10px;">Claygent AI Stats</h3>
                        <div id="quality-claygent-stats" style="font-size:12px; color:#a1a1aa;"></div>
                    </div>

                    <!-- GREEN Lead Summary Table -->
                    <div id="quality-green-leads" class="hidden" style="background:#111113; border:1px solid #1e1e22; border-radius:10px; padding:16px;">
                        <h3 style="font-size:13px; color:#4ade80; margin-bottom:10px;">GREEN Leads Ready for Outreach</h3>
                        <div class="table-wrap" style="max-height:300px; overflow-y:auto;">
                            <table id="green-leads-table"><thead><tr>
                                <th>URL</th><th>Campaign</th><th>Build Size</th><th>Score</th>
                            </tr></thead><tbody id="green-leads-body"></tbody></table>
                        </div>
                    </div>
                </div>

                <div style="text-align:center; margin-bottom:16px;">
                    <button class="btn btn-outline btn-sm" id="toggle-quality-btn" style="display:none;">Show Quality Details</button>
                </div>

                <!-- Download Buttons -->
                <div class="btn-row" style="justify-content: center; flex-wrap: wrap;">
                    <a class="btn btn-success" id="download-btn">Download Enriched CSV</a>
                    <a class="btn btn-primary" id="download-briefs-btn" style="display:none;">Download Outreach Briefs</a>
                    <a class="btn btn-outline" id="download-instantly-btn" style="display:none;">Download Instantly Import</a>
                    <button class="btn btn-outline" id="new-btn">Start New Enrichment</button>
                </div>
            </div>
        </div>
```

### 3.5 Frontend — Modify pollStatus() JS

**Find the done handler inside `pollStatus()` (around line 585):**

```javascript
if (data.status === 'done') { clearInterval(pollInterval); $('#done-success').textContent = data.stats.success || 0; $('#done-failed').textContent = data.stats.failed || 0; $('#done-skipped').textContent = data.stats.no_url || 0; $('#done-summary').textContent = `${data.stats.success || 0} of ${data.total || 0} leads enriched.`; $('#download-btn').href = `/download/${currentJobId}`; showStep('done'); }
```

**Replace with:**

```javascript
            if (data.status === 'done') {
                clearInterval(pollInterval);
                $('#done-success').textContent = data.stats.success || 0;
                $('#done-failed').textContent = data.stats.failed || 0;
                $('#done-skipped').textContent = data.stats.no_url || 0;
                $('#done-summary').textContent = `${data.stats.success || 0} of ${data.total || 0} leads enriched.`;
                $('#download-btn').href = `/download/${currentJobId}`;

                // Quality summary
                const q = data.quality;
                if (q) {
                    // ICP counts
                    $('#done-green').textContent = q.icp_green || 0;
                    $('#done-yellow').textContent = q.icp_yellow || 0;
                    $('#done-red').textContent = q.icp_red || 0;
                    $('#done-unknown').textContent = q.icp_unknown || 0;

                    const hasDetails = (q.icp_green > 0) || (q.conflicts && q.conflicts.length > 0) || (q.false_negatives && q.false_negatives.length > 0);
                    if (hasDetails) {
                        const toggleBtn = $('#toggle-quality-btn');
                        toggleBtn.style.display = '';
                        toggleBtn.addEventListener('click', function() {
                            const el = $('#quality-details');
                            const hidden = el.classList.toggle('hidden');
                            this.textContent = hidden ? 'Show Quality Details' : 'Hide Quality Details';
                        });
                    }

                    // Campaign distribution
                    if (q.campaigns && Object.keys(q.campaigns).length > 0) {
                        $('#quality-campaigns').classList.remove('hidden');
                        const campaignNames = {A:'Slow Site',B:'Generic Calendly',C:'No Booking',D:'Ad Spend Leak',E:'New Launch',F:'Multi Coach',G:'Post Booking Gap',H:'Brand Gap',I:'Multiple Audiences'};
                        const maxCount = Math.max(...Object.values(q.campaigns));
                        let barsHtml = '';
                        for (const [letter, count] of Object.entries(q.campaigns).sort((a,b) => b[1] - a[1])) {
                            const pct = Math.round((count / maxCount) * 100);
                            const name = campaignNames[letter] || letter;
                            barsHtml += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
                                <span style="color:#93c5fd;font-size:11px;font-weight:700;width:20px;">${letter}</span>
                                <div style="flex:1;height:18px;background:#18181b;border-radius:4px;overflow:hidden;">
                                    <div style="height:100%;width:${pct}%;background:linear-gradient(90deg,#1e3a5f,#2563eb);border-radius:4px;"></div>
                                </div>
                                <span style="color:#d4d4d8;font-size:11px;min-width:60px;">${count} — ${name}</span>
                            </div>`;
                        }
                        $('#quality-campaign-bars').innerHTML = barsHtml;
                    }

                    // Conflicts
                    if (q.conflicts && q.conflicts.length > 0) {
                        $('#quality-conflicts').classList.remove('hidden');
                        $('#conflict-count').textContent = `(${q.conflicts.length} found)`;
                        let conflictHtml = '';
                        for (const c of q.conflicts) {
                            conflictHtml += `<div style="padding:4px 0;border-bottom:1px solid #141416;">
                                <span style="color:#60a5fa;">${esc(c.lead)}</span><br>
                                <span style="color:#71717a;">${esc(c.detail)}</span>
                            </div>`;
                        }
                        $('#quality-conflict-list').innerHTML = conflictHtml;
                    }

                    // False negatives
                    if (q.false_negatives && q.false_negatives.length > 0) {
                        $('#quality-false-neg').classList.remove('hidden');
                        $('#false-neg-count').textContent = `(${q.false_negatives.length} leads)`;
                        let fnHtml = '';
                        for (const fn of q.false_negatives) {
                            fnHtml += `<div style="padding:4px 0;border-bottom:1px solid #141416;">
                                <span style="color:#60a5fa;">${esc(fn.lead)}</span>
                                — AI: ${esc(fn.booking_ai || 'None')}, Regex: ${esc(fn.booking_regex || 'None detected')}
                            </div>`;
                        }
                        $('#quality-false-neg-list').innerHTML = fnHtml;
                    }

                    // Claygent stats
                    if (q.claygent_ran > 0) {
                        $('#quality-claygent').classList.remove('hidden');
                        $('#quality-claygent-stats').innerHTML = `
                            Leads analyzed: ${q.claygent_ran} · 
                            Total cost: $${q.claygent_cost.toFixed(4)} · 
                            Navigator would-trigger: ${q.navigator_should_trigger} · 
                            Navigator triggered: ${q.navigator_triggered} · 
                            Booking confidence: ${esc(q.avg_confidence_booking)}
                        `;
                    }

                    // GREEN leads table
                    if (q.green_leads && q.green_leads.length > 0) {
                        $('#quality-green-leads').classList.remove('hidden');
                        let tbody = '';
                        for (const gl of q.green_leads) {
                            const shortUrl = gl.url ? gl.url.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '') : '-';
                            tbody += `<tr>
                                <td>${gl.url ? `<a href="${safeUrl(gl.url)}" target="_blank" rel="noopener" style="color:#60a5fa;text-decoration:none;">${esc(shortUrl)}</a>` : '-'}</td>
                                <td>${esc(gl.campaign || '-')}</td>
                                <td>${esc(gl.build_size ? gl.build_size.split(' ')[0] : '-')}</td>
                                <td>${gl.score ? esc(gl.score) + '/100' : '-'}</td>
                            </tr>`;
                        }
                        $('#green-leads-body').innerHTML = tbody;
                    }

                    // Show download buttons for briefs and instantly when GREEN leads exist
                    if (q.icp_green > 0) {
                        const briefsBtn = $('#download-briefs-btn');
                        briefsBtn.href = `/briefs/${currentJobId}`;
                        briefsBtn.style.display = '';

                        const instantlyBtn = $('#download-instantly-btn');
                        instantlyBtn.href = `/instantly/${currentJobId}`;
                        instantlyBtn.style.display = '';
                    }
                }

                showStep('done');
            }
```

### 3.6 Verification

- [ ] Run a 10-lead batch with mix of GREEN/YELLOW/RED leads. The done screen should show:
  - ICP breakdown row (GREEN count, YELLOW count, RED count, Unknown count)
  - "Show Quality Details" button visible
  - Clicking it reveals: campaign bars, conflicts, false negatives, Claygent stats, GREEN leads table
- [ ] Campaign distribution bars should sum to the total number of leads with campaign assignments
- [ ] Conflicts section only shows CONFLICT and AI_DOWNGRADE entries (not AGREE or AI_UPGRADE)
- [ ] "Download Outreach Briefs" button appears when GREEN > 0, and downloads a .txt file
- [ ] "Download Instantly Import" button appears when GREEN > 0, and downloads a .csv file
- [ ] GREEN leads table shows clickable URLs, campaign bucket, and build size
- [ ] Run with `--no-ai` mode: quality section should show Unknown for all ICP counts (no Claygent ran). Briefs and Instantly buttons should NOT appear (no GREEN scoring without AI).

---

## FULL FILE SUMMARY

| File | Changes |
|------|---------|
| `app.py` | Add `/briefs/<job_id>` endpoint, `/instantly/<job_id>` endpoint, `_compute_batch_quality()` function, `_get()` helper, `_extract_icp_from_classification()` helper, `_clean_company_name()` helper. Modify `run_scrape_job()` to call `_compute_batch_quality()`. |
| `index.html` | Replace "Step 4: Done" HTML with quality dashboard. Replace `pollStatus()` done handler with quality-aware rendering. |
| `scraper.py` | No changes for these gaps — all modifications are in the web layer. |
| `claygent.py` | No changes for these gaps. |
