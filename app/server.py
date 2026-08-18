"""Task 3 - mini audio collection app.

    python -m app.server            # http://127.0.0.1:5000

Two views, as the assignment asks:

    /             enter name + phone, record in the browser or upload a file
    /submissions  every submission, with a play button and the extracted
                  properties

Plus a small JSON API over the same data, which Task 2's n8n flows consume.

Design notes worth defending:

* The browser records **16-bit PCM WAV**, not the WebM/Opus that MediaRecorder
  produces by default. Opus cannot be decoded in pure Python, so a WebM upload
  would make loudness analysis depend on ffmpeg being installed on the server.
  Encoding WAV client-side means the core flow works with nothing but Flask and
  numpy. Uploads of any format are still accepted and routed through ffmpeg when
  it is present.
* A submission is never rejected because its audio could not be analysed. The
  row is written with `analysis_ok = 0` and a note saying why. Losing a gig
  worker's recording because the server lacked a codec is the worse outcome.
* Files are stored on disk with a generated name and the sha256 recorded, so a
  re-upload of identical bytes is detected as a duplicate rather than silently
  double-counted.
"""
import os
import re
import sys
import uuid

from flask import (Flask, jsonify, render_template, request, send_from_directory,
                   url_for)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import audio_analysis, db                                  # noqa: E402
from app.automation import bp as automation_bp                      # noqa: E402
from pipeline import config as pipeline_config                      # noqa: E402

MAX_UPLOAD_BYTES = 25 * 1024 * 1024        # 25 MB
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".webm",
                      ".flac", ".mp4", ".3gp", ".amr"}
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["DB_PATH"] = os.environ.get("CONSULTBAE_DB", pipeline_config.DB_PATH)
app.config["UPLOAD_DIR"] = os.environ.get("CONSULTBAE_UPLOADS", pipeline_config.UPLOAD_DIR)

# Task 2 endpoints (/api/people/untagged, /api/match/check, ...) live on the same
# app so n8n has one base URL to point at.
app.register_blueprint(automation_bp)


def upload_dir():
    path = app.config["UPLOAD_DIR"]
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------

@app.route("/")
def record_view():
    conn = db.connect(app.config["DB_PATH"])
    try:
        return render_template("record.html", stats=db.stats(conn),
                               has_ffmpeg=bool(audio_analysis.FFMPEG))
    finally:
        conn.close()


@app.route("/submissions")
def submissions_view():
    conn = db.connect(app.config["DB_PATH"])
    try:
        return render_template("submissions.html",
                               submissions=db.list_submissions(conn),
                               stats=db.stats(conn))
    finally:
        conn.close()


@app.route("/media/<path:filename>")
def media(filename):
    # Serve only from the upload directory, and only names this app generated.
    if SAFE_NAME_RE.search(filename):
        return jsonify({"error": "bad filename"}), 400
    return send_from_directory(upload_dir(), filename, conditional=True)


# ---------------------------------------------------------------------------
# api
# ---------------------------------------------------------------------------

@app.route("/api/submissions", methods=["POST"])
def create_submission():
    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    capture_mode = request.form.get("capture_mode") or "file_upload"

    if not name:
        return jsonify({"ok": False, "error": "Name is required"}), 400
    if not phone:
        return jsonify({"ok": False, "error": "Phone number is required"}), 400

    upload = request.files.get("audio")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "error": "No audio was attached"}), 400

    original = upload.filename
    extension = os.path.splitext(original)[1].lower() or ".wav"
    if extension not in ALLOWED_EXTENSIONS:
        return jsonify({
            "ok": False,
            "error": "%s files are not accepted. Allowed: %s" % (
                extension, ", ".join(sorted(ALLOWED_EXTENSIONS))),
        }), 400

    stored_name = "%s%s" % (uuid.uuid4().hex, extension)
    stored_path = os.path.join(upload_dir(), stored_name)
    upload.save(stored_path)

    if os.path.getsize(stored_path) == 0:
        os.unlink(stored_path)
        return jsonify({"ok": False, "error": "The uploaded file was empty"}), 400

    # Analysis must never take the submission down with it.
    try:
        analysis = audio_analysis.analyse(stored_path)
    except Exception as exc:                                  # noqa: BLE001
        analysis = {"analysis_ok": 0,
                    "analysis_note": "analysis crashed: %s" % exc,
                    "file_size_bytes": os.path.getsize(stored_path)}

    conn = db.connect(app.config["DB_PATH"])
    try:
        with conn:
            person_id, phone_norm, link_method, _ = db.resolve_person(conn, name, phone)
            submission_id, duplicate_of = db.insert_submission(
                conn, name=name, phone_raw=phone, stored_filename=stored_name,
                original_filename=original, mime_type=upload.mimetype,
                capture_mode=capture_mode, analysis=analysis, person_id=person_id,
                phone=phone_norm, link_method=link_method,
                sha256=db.sha256_of(stored_path))
        record = db.get_submission(conn, submission_id)
    finally:
        conn.close()

    record["audio_url"] = url_for("media", filename=stored_name)
    return jsonify({
        "ok": True,
        "submission_id": submission_id,
        "person_id": person_id,
        "person_link_method": link_method,
        "duplicate_of": duplicate_of,
        "submission": record,
    }), 201


@app.route("/api/submissions", methods=["GET"])
def api_list_submissions():
    conn = db.connect(app.config["DB_PATH"])
    try:
        rows = db.list_submissions(conn, limit=int(request.args.get("limit", 200)))
        for row in rows:
            row["audio_url"] = url_for("media", filename=row["stored_filename"])
        return jsonify({"count": len(rows), "submissions": rows,
                        "stats": db.stats(conn)})
    finally:
        conn.close()


@app.route("/api/submissions/<int:submission_id>", methods=["GET"])
def api_get_submission(submission_id):
    conn = db.connect(app.config["DB_PATH"])
    try:
        record = db.get_submission(conn, submission_id)
    finally:
        conn.close()
    if record is None:
        return jsonify({"error": "not found"}), 404
    record["audio_url"] = url_for("media", filename=record["stored_filename"])
    return jsonify(record)


@app.route("/healthz")
def healthz():
    conn = db.connect(app.config["DB_PATH"])
    try:
        people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        submissions = conn.execute("SELECT COUNT(*) FROM audio_submissions").fetchone()[0]
    finally:
        conn.close()
    return jsonify({
        "ok": True,
        "database": app.config["DB_PATH"],
        "people": people,
        "submissions": submissions,
        "ffmpeg": bool(audio_analysis.FFMPEG),
        "ffprobe": bool(audio_analysis.FFPROBE),
    })


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"ok": False,
                    "error": "Audio must be under %d MB" % (MAX_UPLOAD_BYTES // 1048576)}), 413


def main():
    if not os.path.exists(app.config["DB_PATH"]):
        print("No database at %s - run `python3 -m pipeline.run` first."
              % app.config["DB_PATH"])
        return 1
    db.ensure_schema(app.config["DB_PATH"])
    upload_dir()
    port = int(os.environ.get("PORT", 5000))
    print("audio app on http://127.0.0.1:%d   (submissions: /submissions)" % port)
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
