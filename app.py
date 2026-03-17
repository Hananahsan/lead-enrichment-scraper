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
from scraper import scrape_website, normalize_url, HAS_PLAYWRIGHT, HAS_CLAUDE, PAGESPEED_API_KEY, ANTHROPIC_API_KEY, MAX_WORKERS, MAX_WORKERS_FAST

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


def find_website_column(columns):
    for col in columns:
        if any(kw in col.lower() for kw in ["website", "url", "domain", "web"]):
            return col
    return None


def _flatten_intel(intel):
    """Flatten intel dict for CSV output."""
    flat = {}
    for key, value in intel.items():
        if isinstance(value, (list, dict)):
            flat[f"enriched_{key}"] = json.dumps(value)
        else:
            flat[f"enriched_{key}"] = value
    return flat


def run_scrape_job(job_id, input_path, fast=False):
    """Run scraping in background thread. fast=True uses parallel processing."""
    try:
        df = pd.read_csv(input_path)
        website_col = find_website_column(df.columns)

        if not website_col:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = f"No website column found. Columns: {list(df.columns)}"
            return

        total = len(df)
        jobs[job_id]["total"] = total
        jobs[job_id]["website_col"] = website_col
        jobs[job_id]["mode"] = "fast" if fast else "full"

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
            return idx, scrape_website(url, fast=fast)

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

        jobs[job_id]["status"] = "done"
        jobs[job_id]["output_path"] = output_path

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename.endswith(".csv"):
        return jsonify({"error": "Please upload a CSV file"}), 400

    job_id = str(uuid.uuid4())[:8]
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], f"{job_id}.csv")
    file.save(filepath)

    # Preview
    try:
        df = pd.read_csv(filepath)
    except Exception as e:
        os.remove(filepath)
        return jsonify({"error": f"Could not parse CSV: {str(e)[:200]}"}), 400

    website_col = find_website_column(df.columns)

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
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "running",
        "total": 1,
        "current": 0,
        "current_url": url,
        "stats": {},
        "started_at": datetime.now().isoformat(),
    }

    def run_single(job_id, url):
        try:
            intel = scrape_website(url)
            status = intel.get("scrape_status", "unknown")
            jobs[job_id]["current"] = 1
            jobs[job_id]["stats"][status] = 1

            # Save as CSV
            flat = {}
            for key, value in intel.items():
                if isinstance(value, (list, dict)):
                    flat[f"enriched_{key}"] = json.dumps(value)
                else:
                    flat[f"enriched_{key}"] = value

            output_path = os.path.join(app.config["OUTPUT_FOLDER"], f"{job_id}.csv")
            pd.DataFrame([flat]).to_csv(output_path, index=False)

            jobs[job_id]["status"] = "done"
            jobs[job_id]["output_path"] = output_path
            jobs[job_id]["results"] = intel
        except Exception as e:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)

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
    fast = data.get("mode", "fast") == "fast"  # Default to fast for CSV batches

    jobs[job_id] = {
        "status": "running",
        "total": 0,
        "current": 0,
        "current_url": "",
        "stats": {},
        "mode": "fast" if fast else "full",
        "started_at": datetime.now().isoformat(),
    }

    thread = threading.Thread(target=run_scrape_job, args=(job_id, filepath, fast))
    thread.daemon = True
    thread.start()

    return jsonify({"status": "started", "mode": "fast" if fast else "full"})


@app.route("/status/<job_id>")
def status(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(jobs[job_id])


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

    # 3. Anthropic / Claude API
    claude_ok = HAS_CLAUDE and bool(ANTHROPIC_API_KEY)
    claude_detail = "Not installed"
    if HAS_CLAUDE and not ANTHROPIC_API_KEY:
        claude_detail = "Package installed but no API key — set ANTHROPIC_API_KEY"
    elif claude_ok:
        # Quick validation: try to ping the API
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            client.models.list(limit=1)
            claude_detail = "Connected and working"
        except Exception as e:
            err = str(e)
            if "credit balance" in err.lower():
                claude_detail = "Connected but insufficient credits"
                claude_ok = False
            elif "invalid" in err.lower() or "auth" in err.lower():
                claude_detail = "Invalid API key"
                claude_ok = False
            else:
                claude_detail = f"Error: {err[:100]}"
                claude_ok = False

    checks["claude"] = {
        "name": "Claude AI Analysis",
        "connected": claude_ok,
        "detail": claude_detail,
    }

    # 4. Facebook Ad Library (just needs Playwright)
    checks["facebook_ads"] = {
        "name": "Facebook Ad Library",
        "connected": HAS_PLAYWRIGHT,
        "detail": "Available (uses Playwright)" if HAS_PLAYWRIGHT else "Unavailable — needs Playwright",
    }

    # 5. Social Media Scraping (needs Playwright)
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
    if job_id not in jobs or jobs[job_id]["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(
        jobs[job_id]["output_path"],
        as_attachment=True,
        download_name=f"enriched_leads_{job_id}.csv",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
