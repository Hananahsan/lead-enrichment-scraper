"""
Apollo Lead Enrichment - Web App
"""

import os
import uuid
import json
import threading
from datetime import datetime

from flask import Flask, render_template, request, jsonify, send_file
from scraper import scrape_website, normalize_url

import pandas as pd

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(__file__), "uploads")
app.config["OUTPUT_FOLDER"] = os.path.join(os.path.dirname(__file__), "outputs")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max

# In-memory job tracking
jobs = {}


def find_website_column(columns):
    for col in columns:
        if any(kw in col.lower() for kw in ["website", "url", "domain", "web"]):
            return col
    return None


def run_scrape_job(job_id, input_path):
    """Run scraping in background thread."""
    try:
        df = pd.read_csv(input_path)
        website_col = find_website_column(df.columns)

        if not website_col:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = f"No website column found. Columns: {list(df.columns)}"
            return

        jobs[job_id]["total"] = len(df)
        jobs[job_id]["website_col"] = website_col
        results = []

        for idx, row in df.iterrows():
            if jobs[job_id].get("cancelled"):
                break

            url = str(row[website_col]).strip()
            jobs[job_id]["current"] = idx + 1
            jobs[job_id]["current_url"] = url if url.lower() not in ("nan", "none", "") else "—"

            if not url or url.lower() in ("nan", "none", ""):
                results.append({"enriched_scrape_status": "no_url"})
                continue

            intel = scrape_website(url)
            flat = {}
            for key, value in intel.items():
                if isinstance(value, (list, dict)):
                    flat[f"enriched_{key}"] = json.dumps(value)
                else:
                    flat[f"enriched_{key}"] = value
            results.append(flat)

            # Update stats
            status = intel.get("scrape_status", "unknown")
            jobs[job_id]["stats"][status] = jobs[job_id]["stats"].get(status, 0) + 1

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
    df = pd.read_csv(filepath)
    website_col = find_website_column(df.columns)

    return jsonify({
        "job_id": job_id,
        "rows": len(df),
        "columns": list(df.columns),
        "website_column": website_col,
        "preview": df.head(5).fillna("").to_dict(orient="records"),
    })


@app.route("/start/<job_id>", methods=["POST"])
def start(job_id):
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], f"{job_id}.csv")
    if not os.path.exists(filepath):
        return jsonify({"error": "Job not found"}), 404

    jobs[job_id] = {
        "status": "running",
        "total": 0,
        "current": 0,
        "current_url": "",
        "stats": {},
        "started_at": datetime.now().isoformat(),
    }

    thread = threading.Thread(target=run_scrape_job, args=(job_id, filepath))
    thread.daemon = True
    thread.start()

    return jsonify({"status": "started"})


@app.route("/status/<job_id>")
def status(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(jobs[job_id])


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
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["OUTPUT_FOLDER"], exist_ok=True)
    app.run(debug=True, port=5001)
