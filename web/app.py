"""Web server behind Stash: fetches posts and hands their pictures and videos to the phone.

Jobs live in memory, so run a single process with threads (see the Dockerfile).
Finished files are temporary and removed after FILE_LIFETIME seconds.
"""
import mimetypes
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from stash_core import PinError, download_post, find_links  # noqa: E402

FILE_LIFETIME = 30 * 60
MAX_LINKS_PER_REQUEST = 10
RATE_LIMIT = 30  # links per client...
RATE_WINDOW = 10 * 60  # ...per this many seconds
SIMULTANEOUS_DOWNLOADS = 2
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
FILES_ROOT = tempfile.mkdtemp(prefix="stash-")

app = Flask(__name__, static_folder=None)
if os.environ.get("TRUST_PROXY"):  # Set only when running behind a hosting proxy.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

jobs = {}
jobs_lock = threading.Lock()
recent_requests = defaultdict(deque)
pool = ThreadPoolExecutor(max_workers=SIMULTANEOUS_DOWNLOADS)


def describe(path):
    ext = os.path.splitext(path)[1].lower()
    return {
        "path": path,
        "name": os.path.basename(path),
        "size": os.path.getsize(path),
        "kind": "video" if ext in VIDEO_EXTENSIONS else "picture",
    }


def run_job(job_id, link, folder):
    def progress(fraction):
        with jobs_lock:
            jobs[job_id]["progress"] = fraction

    with jobs_lock:
        jobs[job_id]["state"] = "working"
    try:
        paths, _ = download_post(link, folder, progress)
        update = {"state": "ready", "files": [describe(p) for p in paths], "progress": 1.0}
    except PinError as error:
        update = {"state": "failed", "error": str(error)}
    except Exception:
        app.logger.exception("Unexpected failure for %s", link)
        update = {"state": "failed", "error": "Something went wrong. Please try again."}
    with jobs_lock:
        jobs[job_id].update(update)


def remove_expired_jobs():
    while True:
        time.sleep(60)
        cutoff = time.time() - FILE_LIFETIME
        with jobs_lock:
            expired = [(i, j["folder"]) for i, j in jobs.items()
                       if j["created"] < cutoff and j["state"] in ("ready", "failed")]
            for job_id, _ in expired:
                del jobs[job_id]
        for _, folder in expired:
            shutil.rmtree(folder, ignore_errors=True)


threading.Thread(target=remove_expired_jobs, daemon=True).start()


def over_rate_limit(client, count):
    now = time.time()
    with jobs_lock:
        history = recent_requests[client]
        while history and history[0] < now - RATE_WINDOW:
            history.popleft()
        if len(history) + count > RATE_LIMIT:
            return True
        history.extend([now] * count)
    return False


def public_view(job_id, job):
    return {
        "id": job_id,
        "link": job["link"],
        "state": job["state"],
        "progress": job["progress"],
        "files": [{k: f[k] for k in ("name", "size", "kind")} for f in job.get("files", [])],
        "error": job.get("error"),
    }


@app.post("/api/jobs")
def create_jobs():
    data = request.get_json(silent=True) or {}
    links = find_links(str(data.get("text", "")))
    if not links:
        return jsonify(error="No supported link found. Paste a Pinterest, Instagram or X link."), 400
    if len(links) > MAX_LINKS_PER_REQUEST:
        return jsonify(error=f"Please send {MAX_LINKS_PER_REQUEST} links or fewer at a time."), 400
    if over_rate_limit(request.remote_addr, len(links)):
        return jsonify(error="Too many downloads right now. Please wait a few minutes."), 429

    created = []
    for link in links:
        job_id = secrets.token_urlsafe(16)
        folder = tempfile.mkdtemp(dir=FILES_ROOT)
        job = {"link": link, "state": "queued", "progress": 0.0, "created": time.time(), "folder": folder}
        with jobs_lock:
            jobs[job_id] = job
        pool.submit(run_job, job_id, link, folder)
        created.append(public_view(job_id, job))
    return jsonify(jobs=created), 201


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            abort(404)
        return jsonify(public_view(job_id, job))


@app.get("/api/jobs/<job_id>/file/<int:index>")
def job_file(job_id, index):
    with jobs_lock:
        job = jobs.get(job_id)
        files = job.get("files", []) if job else []
        entry = files[index] if 0 <= index < len(files) else None
    if not entry or not os.path.exists(entry["path"]):
        abort(404)
    inline = request.args.get("inline") == "1"
    mimetype = mimetypes.guess_type(entry["name"])[0] or "application/octet-stream"
    return send_file(entry["path"], mimetype=mimetype, as_attachment=not inline,
                     download_name=entry["name"], conditional=True, max_age=0)


@app.get("/healthz")
def health():
    return "ok"


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html", max_age=0)


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(STATIC, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/icons/<path:name>")
def icons(name):
    return send_from_directory(os.path.join(STATIC, "icons"), name)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8765)), threaded=True)
