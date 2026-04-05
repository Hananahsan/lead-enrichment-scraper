"""
Apollo Lead Enrichment - Web App
"""

import os
import uuid
import json
import threading
from datetime import datetime

from flask import Flask, render_template, request, jsonify, send_file
from concurrent.futures import ThreadPoolExecutor, as_completed
from scraper import scrape_website, normalize_url, HAS_PLAYWRIGHT, HAS_CLAUDE, HAS_CLAYGENT, PAGESPEED_API_KEY, ANTHROPIC_API_KEY, MAX_WORKERS, MAX_WORKERS_FAST
try:
    from claygent import (
        submit_batch, poll_batch, retrieve_batch_results,
        apply_batch_results_to_intel,
    )
    HAS_BATCH = HAS_CLAYGENT and HAS_CLAUDE
except ImportError:
    HAS_BATCH = False

import pandas as pd

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = os.path.join("/tmp", "scraper_uploads")
app.config["OUTPUT_FOLDER"] = os.path.join("/tmp", "scraper_outputs")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["OUTPUT_FOLDER"], exist_ok=True)

# In-memory job tracking
jobs = {}
_jobs_lock = threading.Lock()
_MAX_JOBS = 200  # Evict oldest jobs when limit reached


def _evict_old_jobs():
    """Remove oldest completed jobs if we exceed _MAX_JOBS. Call under _jobs_lock."""
    if len(jobs) <= _MAX_JOBS:
        return
    # Sort by started_at, remove oldest completed jobs first
    completed = [
        (jid, j.get("started_at", ""))
        for jid, j in jobs.items()
        if j.get("status") in ("done", "error", "cancelled")
    ]
    completed.sort(key=lambda x: x[1])
    to_remove = len(jobs) - _MAX_JOBS
    for jid, _ in completed[:to_remove]:
        del jobs[jid]

# Session-wide usage tracking (resets on server restart)
_usage = {
    "total_tokens_in": 0,
    "total_tokens_out": 0,
    "total_cost_usd": 0.0,
    "total_leads_analyzed": 0,
    "total_jobs": 0,
    "batch_savings_usd": 0.0,  # How much saved by using batch vs realtime
    "jobs_history": [],  # Last 50 jobs: [{job_id, mode, leads, cost, timestamp}]
}
_usage_lock = threading.Lock()


def find_website_column(columns):
    for col in columns:
        if any(kw in col.lower() for kw in ["website", "url", "domain", "web"]):
            return col
    return None


def _flatten_intel(intel):
    """Flatten intel dict for CSV output."""
    flat = {}
    for key, value in intel.items():
        if key.startswith("_"):
            continue  # Skip internal keys (_batch_request, _claygent_content, etc.)
        if isinstance(value, (list, dict)):
            flat[f"enriched_{key}"] = json.dumps(value)
        else:
            flat[f"enriched_{key}"] = value
    return flat


def _track_job_usage(job_id, results_dict, mode):
    """Extract token/cost data from completed results and update session usage."""
    job_tokens_in = 0
    job_tokens_out = 0
    job_cost = 0.0
    leads_with_ai = 0

    for idx, intel in results_dict.items():
        if not isinstance(intel, dict):
            continue
        # Results are already flattened with enriched_ prefix.
        # Use 'is None' checks — 'or' would treat 0 as falsy.
        t_in = intel.get("enriched_claygent_tokens_in")
        if t_in is None:
            t_in = intel.get("enriched_ai_tokens_in", 0)
        t_out = intel.get("enriched_claygent_tokens_out")
        if t_out is None:
            t_out = intel.get("enriched_ai_tokens_out", 0)
        cost = intel.get("enriched_claygent_cost_usd")
        if cost is None:
            cost = intel.get("enriched_ai_cost_usd", 0)
        if isinstance(t_in, (int, float)):
            job_tokens_in += t_in
        if isinstance(t_out, (int, float)):
            job_tokens_out += t_out
        if isinstance(cost, (int, float)) and cost > 0:
            job_cost += cost
            leads_with_ai += 1

    # Calculate batch savings (batch costs 50% of realtime)
    is_batch = "batch" in str(mode)
    batch_savings = job_cost if is_batch else 0.0  # Saved same amount as spent

    with _usage_lock:
        _usage["total_tokens_in"] += job_tokens_in
        _usage["total_tokens_out"] += job_tokens_out
        _usage["total_cost_usd"] += job_cost
        _usage["total_leads_analyzed"] += leads_with_ai
        _usage["total_jobs"] += 1
        _usage["batch_savings_usd"] += batch_savings
        _usage["jobs_history"].append({
            "job_id": job_id,
            "mode": mode,
            "leads": leads_with_ai,
            "tokens_in": job_tokens_in,
            "tokens_out": job_tokens_out,
            "cost_usd": round(job_cost, 4),
            "batch_savings_usd": round(batch_savings, 4),
            "timestamp": datetime.now().isoformat(),
        })
        # Keep only last 50 jobs
        if len(_usage["jobs_history"]) > 50:
            _usage["jobs_history"] = _usage["jobs_history"][-50:]

    # Also store on the job itself for the status endpoint
    with _jobs_lock:
        jobs[job_id]["usage"] = {
            "tokens_in": job_tokens_in,
            "tokens_out": job_tokens_out,
            "cost_usd": round(job_cost, 4),
            "leads_with_ai": leads_with_ai,
            "batch_savings_usd": round(batch_savings, 4),
        }


def _update_job(job_id, **kwargs):
    """Thread-safe helper to update job fields."""
    with _jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)
            if kwargs.get("status") in ("done", "error", "cancelled"):
                _evict_old_jobs()


def run_scrape_job(job_id, input_path, fast=False, no_ai=False):
    """Run scraping in background thread. fast=True uses parallel processing."""
    try:
        df = pd.read_csv(input_path)
        website_col = find_website_column(df.columns)

        if not website_col:
            _update_job(job_id, status="error",
                        error=f"No website column found. Columns: {list(df.columns)}")
            return

        total = len(df)
        _update_job(job_id, total=total, website_col=website_col,
                    mode="fast" if fast else "full")

        # Build list of (index, url) pairs
        url_tasks = []
        skip_indices = set()
        for idx, row in df.iterrows():
            url = str(row[website_col]).strip()
            if not url or url.lower() in ("nan", "none", ""):
                skip_indices.add(idx)
            else:
                url_tasks.append((idx, url))

        # Pre-fill results dict with blanks for skipped rows
        results_dict = {}
        for idx in skip_indices:
            results_dict[idx] = {"enriched_scrape_status": "no_url"}

        workers = MAX_WORKERS_FAST if fast else MAX_WORKERS

        def _scrape_one(idx, url):
            return idx, scrape_website(url, fast=fast, no_ai=no_ai)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for idx, url in url_tasks:
                if jobs[job_id].get("cancelled"):
                    break
                fut = executor.submit(_scrape_one, idx, url)
                futures[fut] = (idx, url)

            for fut in as_completed(futures):
                if jobs[job_id].get("cancelled"):
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                idx, url = futures[fut]
                try:
                    _, intel = fut.result()
                    results_dict[idx] = _flatten_intel(intel)
                    status = intel.get("scrape_status", "unknown")
                    with _jobs_lock:
                        jobs[job_id]["stats"][status] = jobs[job_id]["stats"].get(status, 0) + 1
                except Exception as e:
                    results_dict[idx] = {"enriched_scrape_status": "error", "enriched_error": str(e)}
                    with _jobs_lock:
                        jobs[job_id]["stats"]["error"] = jobs[job_id]["stats"].get("error", 0) + 1

                with _jobs_lock:
                    jobs[job_id]["current"] = len(results_dict)
                    jobs[job_id]["current_url"] = url

        # Reassemble in original row order
        results = [results_dict.get(i, {}) for i in range(total)]

        enriched_df = pd.DataFrame(results)
        output_df = pd.concat([df.reset_index(drop=True), enriched_df], axis=1)
        output_path = os.path.join(app.config["OUTPUT_FOLDER"], f"{job_id}.csv")
        output_df.to_csv(output_path, index=False)

        # Batch quality summary
        quality = _compute_batch_quality(list(results_dict.items()))
        _update_job(job_id, status="done", output_path=output_path,
                    quality=quality)

        # Track usage
        with _jobs_lock:
            mode = jobs[job_id].get("mode", "full")
        _track_job_usage(job_id, results_dict, mode)

    except Exception as e:
        _update_job(job_id, status="error", error=str(e))


def run_scrape_job_batch(job_id, input_path, fast=False):
    """Run scraping with Anthropic Batch API for 50% AI cost savings.

    Two-phase approach:
      Phase 1: Scrape all URLs with defer_ai=True (parallel, no API calls)
      Phase 2: Submit all AI prompts as one batch, poll, merge results

    Batch processing trades latency (~minutes-to-hour wait) for cost savings.
    """
    try:
        df = pd.read_csv(input_path)
        website_col = find_website_column(df.columns)

        if not website_col:
            _update_job(job_id, status="error",
                        error=f"No website column found. Columns: {list(df.columns)}")
            return

        total = len(df)
        _update_job(job_id, total=total, website_col=website_col)

        # Build list of (index, url) pairs
        url_tasks = []
        skip_indices = set()
        for idx, row in df.iterrows():
            url = str(row[website_col]).strip()
            if not url or url.lower() in ("nan", "none", ""):
                skip_indices.add(idx)
            else:
                url_tasks.append((idx, url))

        # Pre-fill results for skipped rows
        results_dict = {}
        for idx in skip_indices:
            results_dict[idx] = {"enriched_scrape_status": "no_url"}

        # ---- Phase 1: Scrape all URLs with defer_ai=True ----
        _update_job(job_id, batch_phase="scraping")
        intel_cache = {}  # idx → full intel dict (with _batch_request)
        workers = MAX_WORKERS_FAST if fast else MAX_WORKERS

        def _scrape_one_deferred(idx, url):
            return idx, scrape_website(url, fast=fast, defer_ai=True)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for idx, url in url_tasks:
                if jobs[job_id].get("cancelled"):
                    break
                fut = executor.submit(_scrape_one_deferred, idx, url)
                futures[fut] = (idx, url)

            for fut in as_completed(futures):
                if jobs[job_id].get("cancelled"):
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                idx, url = futures[fut]
                try:
                    _, intel = fut.result()
                    intel_cache[idx] = intel
                    status = intel.get("scrape_status", "unknown")
                    with _jobs_lock:
                        jobs[job_id]["stats"][status] = jobs[job_id]["stats"].get(status, 0) + 1
                except Exception as e:
                    intel_cache[idx] = {"scrape_status": "error", "error": str(e)}
                    with _jobs_lock:
                        jobs[job_id]["stats"]["error"] = jobs[job_id]["stats"].get("error", 0) + 1

                with _jobs_lock:
                    jobs[job_id]["current"] = len(intel_cache) + len(skip_indices)
                    jobs[job_id]["current_url"] = url

        if jobs[job_id].get("cancelled"):
            _update_job(job_id, status="cancelled")
            return

        # ---- Phase 2: Build and submit the batch ----
        _update_job(job_id, batch_phase="submitting")

        batch_requests = []
        batch_idx_map = {}  # custom_id → idx

        for idx, intel in intel_cache.items():
            batch_req = intel.pop("_batch_request", None)
            # Keep _claygent_content — we extract it later for apply_batch_results_to_intel
            if batch_req:
                custom_id = f"lead_{idx}"
                batch_requests.append({
                    "custom_id": custom_id,
                    "system": batch_req["system"],
                    "user_message": batch_req["user_message"],
                })
                batch_idx_map[custom_id] = idx

        if not batch_requests:
            # No AI-eligible leads — just output what we have
            _update_job(job_id, batch_phase="done")
            for idx, intel in intel_cache.items():
                intel.pop("_claygent_content", None)
                results_dict[idx] = _flatten_intel(intel)
            _finalize_batch_job(job_id, df, results_dict, total)
            return

        # Re-extract content_data from intel_cache before we flatten
        # (we need it for apply_batch_results_to_intel)
        content_cache = {}
        for idx, intel in intel_cache.items():
            content_cache[idx] = intel.pop("_claygent_content", None)

        batches = submit_batch(batch_requests)
        if not batches:
            # Batch submission failed — fall back to non-AI results
            _update_job(job_id, batch_phase="failed")
            for idx, intel in intel_cache.items():
                results_dict[idx] = _flatten_intel(intel)
            _finalize_batch_job(job_id, df, results_dict, total)
            return

        if len(batches) > 1:
            import logging as _logging
            _logging.getLogger("claygent").warning(
                f"Large job split into {len(batches)} batches — "
                "polling all sequentially"
            )

        _update_job(job_id, batch_phase="processing",
                    batch_requests_count=len(batch_requests),
                    batch_ids=[b.id for b in batches])

        # ---- Phase 3: Poll each batch until complete ----
        def _on_poll(b):
            with _jobs_lock:
                counts = b.request_counts
                jobs[job_id]["batch_succeeded"] = counts.succeeded
                jobs[job_id]["batch_errored"] = counts.errored
                jobs[job_id]["batch_processing"] = counts.processing
                jobs[job_id]["batch_expired"] = counts.expired

        for batch_obj in batches:
            try:
                poll_batch(batch_obj.id, callback=_on_poll, poll_interval=15)
            except TimeoutError:
                _update_job(job_id, batch_phase="timeout", status="error",
                            error=f"Batch {batch_obj.id} timed out")
                # Still try to retrieve partial results below

        # ---- Phase 4: Retrieve results from all batches and merge ----
        _update_job(job_id, batch_phase="merging")

        ai_results_map = {}
        for batch_obj in batches:
            try:
                batch_results = retrieve_batch_results(batch_obj.id)
                ai_results_map.update(batch_results)
            except Exception as e:
                import logging as _logging
                _logging.getLogger("claygent").error(
                    f"Failed to retrieve results for batch {batch_obj.id}: {e}"
                )

        for custom_id, ai_result in ai_results_map.items():
            idx = batch_idx_map.get(custom_id)
            if idx is None:
                continue
            intel = intel_cache.get(idx, {})
            content_data = content_cache.get(idx)
            apply_batch_results_to_intel(intel, ai_result, content_data)
            intel_cache[idx] = intel

        # Build final results (including leads that had no batch request)
        for idx, intel in intel_cache.items():
            results_dict[idx] = _flatten_intel(intel)

        _finalize_batch_job(job_id, df, results_dict, total)

    except Exception as e:
        _update_job(job_id, status="error", error=str(e))


def _finalize_batch_job(job_id, df, results_dict, total):
    """Write CSV output and mark the batch job as done."""
    results = [results_dict.get(i, {}) for i in range(total)]
    enriched_df = pd.DataFrame(results)
    output_df = pd.concat([df.reset_index(drop=True), enriched_df], axis=1)
    output_path = os.path.join(app.config["OUTPUT_FOLDER"], f"{job_id}.csv")
    output_df.to_csv(output_path, index=False)

    quality = _compute_batch_quality(list(results_dict.items()))
    _update_job(job_id, status="done", output_path=output_path,
                quality=quality, batch_phase="done")

    with _jobs_lock:
        mode = jobs[job_id].get("mode", "batch")
    _track_job_usage(job_id, results_dict, mode)


def _get(row, key, default=""):
    """Safely get a value from a DataFrame row, handling NaN/None."""
    val = row.get(key)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    return val


def _extract_icp_from_classification(classification_text):
    """Extract GREEN/RED from legacy ai_classification text."""
    if not classification_text:
        return None
    text = str(classification_text).upper()
    if "A)" in text or "SOLO COACH" in text:
        return "GREEN"
    if any(x in text for x in ["B)", "C)", "D)", "E)"]):
        return "RED"
    return None


def _compute_batch_quality(results_items):
    """Compute batch quality summary from enrichment results."""
    quality = {
        "icp_green": 0, "icp_yellow": 0, "icp_red": 0, "icp_unknown": 0,
        "campaigns": {},
        "conflicts": [],
        "false_negatives": [],
        "claygent_cost": 0.0,
        "claygent_ran": 0,
        "navigator_should_trigger": 0,
        "navigator_triggered": 0,
        "avg_confidence_booking": "",
        "green_leads": [],
    }

    confidence_booking_counts = {"HIGH": 0, "MEDIUM_HIGH": 0, "LOW": 0, "NONE": 0}

    for idx, intel in results_items:
        if not isinstance(intel, dict):
            continue

        icp = str(intel.get("enriched_icp_score") or intel.get("enriched_ai_icp_fit") or "").strip().upper()
        if icp == "GREEN" or icp == "YES":
            quality["icp_green"] += 1
        elif icp == "YELLOW":
            quality["icp_yellow"] += 1
        elif icp == "RED" or icp == "NO":
            quality["icp_red"] += 1
        else:
            classification = str(intel.get("enriched_icp_classification") or intel.get("enriched_ai_classification") or "")
            if "A)" in classification or "SOLO COACH" in classification.upper():
                quality["icp_green"] += 1
                icp = "GREEN"
            elif any(x in classification for x in ["B)", "C)", "D)", "E)"]):
                quality["icp_red"] += 1
                icp = "RED"
            else:
                quality["icp_unknown"] += 1

        campaign = str(intel.get("enriched_campaign_bucket", "") or "").strip().upper()[:1]
        if campaign and campaign in "ABCDEFGHI":
            quality["campaigns"][campaign] = quality["campaigns"].get(campaign, 0) + 1

        conflicts_raw = intel.get("enriched_confidence_conflicts")
        if conflicts_raw:
            try:
                conflict_list = json.loads(conflicts_raw) if isinstance(conflicts_raw, str) else conflicts_raw
                if isinstance(conflict_list, list):
                    for c in conflict_list:
                        c_str = str(c)
                        if "CONFLICT" in c_str or "AI_DOWNGRADE" in c_str:
                            url = intel.get("enriched_website_url", f"Lead #{idx}")
                            quality["conflicts"].append({"lead": url, "detail": c_str[:200]})
            except (json.JSONDecodeError, TypeError):
                pass

        if intel.get("enriched_booking_false_negative_risk") in (True, "True", "true"):
            quality["false_negatives"].append({
                "lead": intel.get("enriched_website_url", f"Lead #{idx}"),
                "booking_ai": intel.get("enriched_booking_tool", ""),
                "booking_regex": intel.get("enriched_regex_booking_booking_tool", ""),
            })

        cost = intel.get("enriched_claygent_cost_usd")
        if isinstance(cost, (int, float)):
            quality["claygent_cost"] += cost
        if intel.get("enriched_claygent_status") in ("success", "partial"):
            quality["claygent_ran"] += 1
        if intel.get("enriched_claygent_navigator_should_trigger") in (True, "True", "true"):
            quality["navigator_should_trigger"] += 1
        if intel.get("enriched_claygent_navigator_triggered") in (True, "True", "true"):
            quality["navigator_triggered"] += 1

        cb = str(intel.get("enriched_confidence_booking", "")).upper()
        if cb in confidence_booking_counts:
            confidence_booking_counts[cb] += 1

        if icp in ("GREEN", "YES"):
            quality["green_leads"].append({
                "url": intel.get("enriched_website_url", ""),
                "campaign": intel.get("enriched_campaign_bucket", ""),
                "build_size": intel.get("enriched_build_size_estimate", ""),
                "score": intel.get("enriched_icp_score_numeric", ""),
            })

    quality["claygent_cost"] = round(quality["claygent_cost"], 4)
    total_conf = sum(confidence_booking_counts.values())
    if total_conf > 0:
        dominant = max(confidence_booking_counts, key=confidence_booking_counts.get)
        quality["avg_confidence_booking"] = f"{dominant} ({confidence_booking_counts[dominant]}/{total_conf})"

    quality["conflicts"] = quality["conflicts"][:20]
    quality["false_negatives"] = quality["false_negatives"][:20]
    quality["green_leads"] = quality["green_leads"][:50]

    return quality


def _clean_company_name(name):
    """Clean company name for email templates. Drops Inc., LLC, etc."""
    if not name:
        return ""
    name = str(name).strip()
    import re as _re
    suffixes = [
        r",?\s*Inc\.?$", r",?\s*LLC\.?$", r",?\s*Ltd\.?$", r",?\s*LTD\.?$",
        r",?\s*Co\.?$", r",?\s*Corp\.?$", r",?\s*Corporation$",
        r",?\s*Limited$", r",?\s*PLC$", r",?\s*LP$",
        r"\s*®$", r"\s*™$",
    ]
    for pattern in suffixes:
        name = _re.sub(pattern, "", name, flags=_re.IGNORECASE)
    name = name.strip().rstrip(",").strip()
    if name.endswith(" &") or name.endswith(" &amp;"):
        name = name.rsplit("&", 1)[0].strip()
    return name


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename or not file.filename.endswith(".csv"):
        return jsonify({"error": "Please upload a CSV file"}), 400

    job_id = str(uuid.uuid4())[:8]
    original_name = file.filename.rsplit(".csv", 1)[0]  # Strip .csv extension
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], f"{job_id}.csv")
    file.save(filepath)

    # Preview
    try:
        df = pd.read_csv(filepath)
    except Exception as e:
        os.remove(filepath)
        return jsonify({"error": f"Could not parse CSV: {str(e)[:200]}"}), 400

    website_col = find_website_column(df.columns)

    # Store original filename for download naming
    jobs[job_id] = {"original_name": original_name}

    return jsonify({
        "job_id": job_id,
        "rows": len(df),
        "columns": list(df.columns),
        "website_column": website_col,
        "preview": df.head(5).fillna("").to_dict(orient="records"),
    })


@app.route("/scrape-url", methods=["POST"])
def scrape_url():
    """Scrape a single URL and return results as JSON + downloadable CSV."""
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    mode = data.get("mode", "full")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    fast = mode in ("fast", "fast-no-ai")
    no_ai = mode in ("no-ai", "fast-no-ai")

    job_id = str(uuid.uuid4())[:8]
    # Extract domain for download filename — add scheme if missing
    from urllib.parse import urlparse as _urlparse
    _parsed = _urlparse(url if "://" in url else f"https://{url}")
    domain = (_parsed.netloc or _parsed.path.split("/")[0]).replace("www.", "").replace(".", "_")
    domain = domain or "single_url"
    jobs[job_id] = {
        "status": "running",
        "total": 1,
        "current": 0,
        "current_url": url,
        "stats": {},
        "original_name": domain,
        "mode": mode,
        "started_at": datetime.now().isoformat(),
    }

    def run_single(job_id, url):
        try:
            intel = scrape_website(url, fast=fast, no_ai=no_ai)
            status = intel.get("scrape_status", "unknown")
            _update_job(job_id, current=1, **{"stats": {status: 1}})

            flat = _flatten_intel(intel)

            output_path = os.path.join(app.config["OUTPUT_FOLDER"], f"{job_id}.csv")
            pd.DataFrame([flat]).to_csv(output_path, index=False)

            _update_job(job_id, status="done", output_path=output_path,
                        results=intel)

            _track_job_usage(job_id, {0: flat}, mode)
        except Exception as e:
            _update_job(job_id, status="error", error=str(e))

    thread = threading.Thread(target=run_single, args=(job_id, url))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id, "status": "started"})


@app.route("/start/<job_id>", methods=["POST"])
def start(job_id):
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], f"{job_id}.csv")
    if not os.path.exists(filepath):
        return jsonify({"error": "Job not found"}), 404

    if job_id in jobs and jobs[job_id].get("status") == "running":
        return jsonify({"error": "Job already running"}), 409

    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "fast")
    fast = mode in ("fast", "fast-no-ai", "batch", "batch-fast")
    no_ai = mode in ("no-ai", "fast-no-ai")
    use_batch = mode in ("batch", "batch-fast")
    original_name = jobs.get(job_id, {}).get("original_name", "leads")

    jobs[job_id] = {
        "status": "running",
        "total": 0,
        "current": 0,
        "current_url": "",
        "stats": {},
        "mode": mode,
        "original_name": original_name,
        "started_at": datetime.now().isoformat(),
    }

    if use_batch and HAS_BATCH:
        batch_fast = mode == "batch-fast"
        thread = threading.Thread(
            target=run_scrape_job_batch,
            args=(job_id, filepath, batch_fast),
        )
    else:
        if use_batch and not HAS_BATCH:
            _update_job(job_id, mode="fast" if fast else "full",
                        batch_fallback=True)
        thread = threading.Thread(target=run_scrape_job, args=(job_id, filepath, fast, no_ai))

    thread.daemon = True
    thread.start()

    return jsonify({
        "status": "started",
        "mode": mode,
        "batch": use_batch and HAS_BATCH,
    })


@app.route("/status/<job_id>")
def status(job_id):
    with _jobs_lock:
        if job_id not in jobs:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(dict(jobs[job_id]))


@app.route("/usage")
def usage():
    """Return session-wide usage stats (tokens, cost, savings)."""
    with _usage_lock:
        return jsonify({
            "tokens_in": _usage["total_tokens_in"],
            "tokens_out": _usage["total_tokens_out"],
            "cost_usd": round(_usage["total_cost_usd"], 4),
            "leads_analyzed": _usage["total_leads_analyzed"],
            "total_jobs": _usage["total_jobs"],
            "batch_savings_usd": round(_usage["batch_savings_usd"], 4),
            "realtime_equivalent_usd": round(
                _usage["total_cost_usd"] + _usage["batch_savings_usd"], 4
            ),
            "jobs_history": _usage["jobs_history"][-10:],  # Last 10 for UI
        })


_claude_health_cache = {"ok": None, "detail": "", "checked_at": 0}


def _check_claude_health():
    """Check Claude API connectivity with 60-second cache."""
    import time as _time
    now = _time.time()
    if _claude_health_cache["ok"] is not None and now - _claude_health_cache["checked_at"] < 60:
        return _claude_health_cache["ok"], _claude_health_cache["detail"]

    ok = HAS_CLAUDE and bool(ANTHROPIC_API_KEY)
    detail = "Not installed"
    if HAS_CLAUDE and not ANTHROPIC_API_KEY:
        detail = "Package installed but no API key — set ANTHROPIC_API_KEY"
        ok = False
    elif ok:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            client.models.list(limit=1)
            detail = "Connected and working"
        except Exception as e:
            err = str(e)
            if "credit balance" in err.lower():
                detail = "Connected but insufficient credits"
                ok = False
            elif "invalid" in err.lower() or "auth" in err.lower():
                detail = "Invalid API key"
                ok = False
            else:
                detail = f"Error: {err[:100]}"
                ok = False

    _claude_health_cache["ok"] = ok
    _claude_health_cache["detail"] = detail
    _claude_health_cache["checked_at"] = now
    return ok, detail


@app.route("/health")
def health():
    """Check which services/integrations are connected and working."""
    checks = {}

    # 1. Playwright (headless browser)
    checks["playwright"] = {
        "name": "Playwright (Headless Browser)",
        "connected": HAS_PLAYWRIGHT,
        "detail": "Installed" if HAS_PLAYWRIGHT else "Not installed — using requests fallback",
    }

    # 2. PageSpeed API
    ps_ok = bool(PAGESPEED_API_KEY)
    checks["pagespeed"] = {
        "name": "Google PageSpeed Insights",
        "connected": ps_ok,
        "detail": "API key configured" if ps_ok else "No API key — set PAGESPEED_API_KEY",
    }

    # 3. Anthropic / Claude API (cached for 60s to avoid API spam)
    claude_ok, claude_detail = _check_claude_health()

    checks["claude"] = {
        "name": "Claude AI Analysis",
        "connected": claude_ok,
        "detail": claude_detail,
    }

    # 4. Claygent AI (Sonnet intelligence layer)
    claygent_ok = HAS_CLAYGENT and claude_ok
    claygent_detail = "Not installed"
    if HAS_CLAYGENT and claude_ok:
        claygent_detail = "Ready (Sonnet-based AI analysis)"
    elif HAS_CLAYGENT and not claude_ok:
        claygent_detail = "Module loaded but Claude API not connected"
    elif not HAS_CLAYGENT:
        claygent_detail = "claygent.py not found or import error"

    checks["claygent"] = {
        "name": "Claygent AI",
        "connected": claygent_ok,
        "detail": claygent_detail,
    }

    # 5. Batch API (50% cost savings)
    batch_ok = HAS_BATCH and claude_ok
    batch_detail = "Not available"
    if batch_ok:
        batch_detail = "Ready — use mode 'batch' or 'batch-fast' for 50% AI cost savings"
    elif not HAS_BATCH:
        batch_detail = "Requires Claygent + Claude SDK"
    elif not claude_ok:
        batch_detail = "Claude API not connected"

    checks["batch_api"] = {
        "name": "Anthropic Batch API",
        "connected": batch_ok,
        "detail": batch_detail,
    }

    # 6. Facebook Ad Library (just needs Playwright)
    checks["facebook_ads"] = {
        "name": "Facebook Ad Library",
        "connected": HAS_PLAYWRIGHT,
        "detail": "Available (uses Playwright)" if HAS_PLAYWRIGHT else "Unavailable — needs Playwright",
    }

    # 7. Social Media Scraping (needs Playwright)
    checks["social_media"] = {
        "name": "Social Media Scraping",
        "connected": HAS_PLAYWRIGHT,
        "detail": "Available (uses Playwright)" if HAS_PLAYWRIGHT else "Unavailable — needs Playwright",
    }

    all_ok = all(c["connected"] for c in checks.values())

    return jsonify({
        "status": "all_connected" if all_ok else "partial",
        "checks": checks,
    })


@app.route("/download/<job_id>")
def download(job_id):
    if job_id not in jobs or jobs[job_id].get("status") != "done":
        return jsonify({"error": "File not ready"}), 404
    original_name = jobs[job_id].get("original_name", "leads")
    date_str = datetime.now().strftime("%Y-%m-%d")
    mode = jobs[job_id].get("mode", "full")
    download_name = f"{original_name}_enriched_{mode}_{date_str}.csv"
    return send_file(
        jobs[job_id]["output_path"],
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/briefs/<job_id>")
def download_briefs(job_id):
    """Generate outreach-ready research briefs for GREEN leads."""
    if job_id not in jobs or jobs[job_id].get("status") != "done":
        return jsonify({"error": "Job not done yet"}), 404

    output_path = jobs[job_id].get("output_path")
    if not output_path or not os.path.exists(output_path):
        return jsonify({"error": "Output file not found"}), 404

    df = pd.read_csv(output_path)

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
        icp_score = (
            _get(row, "enriched_icp_score")
            or _extract_icp_from_classification(_get(row, "enriched_icp_classification") or _get(row, "enriched_ai_classification"))
            or "UNKNOWN"
        ).strip().upper()

        if icp_score == "GREEN":
            green_count += 1
        elif icp_score == "YELLOW":
            yellow_count += 1
        elif icp_score == "RED":
            red_count += 1
            continue
        else:
            continue

        if icp_score != "GREEN":
            continue

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

        campaign_letter = str(campaign_raw).strip().upper()[:1] if campaign_raw else ""
        campaign_full = campaign_names.get(campaign_letter, campaign_raw or "Not assigned")

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

        if secondary:
            try:
                secondary_list = json.loads(secondary) if isinstance(secondary, str) and secondary.startswith("[") else [secondary]
                secondary_formatted = "; ".join(str(s) for s in secondary_list)
            except (json.JSONDecodeError, TypeError):
                secondary_formatted = str(secondary)
        else:
            secondary_formatted = "Check PageSpeed data + form field count"

        booking_line = f"{booking_tool}, friction {friction}/10"
        if booking_notes:
            booking_line += f" — {booking_notes[:100]}"

        notes_parts = []
        if positioning:
            notes_parts.append(f"Positioning: {positioning}")
        if disqualifiers and disqualifiers not in ("[]", ""):
            notes_parts.append(f"Disqualifiers: {disqualifiers}")
        if conflicts and conflicts not in ("[]", ""):
            notes_parts.append(f"Conflicts: {str(conflicts)[:200]}")
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

    header = f"""KAIROSCAL OUTREACH BRIEFS
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
Batch: {len(df)} total leads | {green_count} GREEN | {yellow_count} YELLOW | {red_count} RED
Briefs below: {len(briefs)} GREEN leads ready for email writing

Paste each brief into Claude (outreach-master skill) to generate the V7 email sequence.
{'='*60}

"""
    content = header + "\n\n".join(briefs)

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
        icp_score = (
            _get(row, "enriched_icp_score")
            or _extract_icp_from_classification(_get(row, "enriched_icp_classification") or _get(row, "enriched_ai_classification"))
            or ""
        ).strip().upper()

        if icp_score != "GREEN":
            continue

        email = _get(row, "Email") or _get(row, "email") or _get(row, "Email Address") or ""
        if not email or "@" not in str(email):
            continue

        first_name = _get(row, "First Name") or _get(row, "first_name", "")
        last_name = _get(row, "Last Name") or _get(row, "last_name", "")
        company_raw = (
            _get(row, "Company")
            or _get(row, "Organization Name")
            or _get(row, "company", "")
        )
        company_clean = _clean_company_name(company_raw)

        observation = _get(row, "enriched_observation_hook", "")
        secondary = _get(row, "enriched_secondary_observations", "")
        campaign_bucket = _get(row, "enriched_campaign_bucket", "")
        build_size = _get(row, "enriched_build_size_estimate", "")
        booking_tool = _get(row, "enriched_booking_tool") or _get(row, "enriched_booking_booking_tool", "")

        load_time = _get(row, "enriched_pagespeed_lcp_seconds") or _get(row, "enriched_mobile_load_time_seconds", "")
        ps_score = _get(row, "enriched_pagespeed_score_performance", "")

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
            "{{costFrame}}": "",
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


if __name__ == "__main__":
    app.run(debug=True, port=5001)
