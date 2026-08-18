"""Database access for the audio app.

Deliberately reuses `pipeline.normalize` for the phone number instead of writing
a second phone parser. The whole point of Task 1 was that `+91-9000000103` and
`09000000103` are one person; if this app normalised phones differently, a
worker who types their number with a country code would fail to match the CRM
record that the pipeline worked so hard to link.
"""
import hashlib
import io
import json
import os
import sqlite3
from datetime import datetime

from pipeline import config as pipeline_config
from pipeline import normalize

SUBMISSION_FIELDS = [
    "duration_seconds", "sample_rate_hz", "sample_rate_khz", "channels",
    "bit_depth", "codec", "container_format", "bitrate_kbps", "pcm_bitrate_kbps",
    "peak_dbfs", "rms_dbfs", "crest_factor_db", "loudness_lufs",
    "loudness_range_lu", "noise_floor_dbfs", "estimated_snr_db", "snr_note",
    "frame_dynamic_range_db", "speech_ratio_pct", "silence_pct", "clipping_pct",
    "dc_offset", "quality_score", "quality_label", "file_size_bytes",
    "analysis_ok", "analysis_note", "analysis_backend",
]


def connect(db_path=None):
    conn = sqlite3.connect(db_path or pipeline_config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(db_path=None):
    """Create the Task 3 tables if the pipeline has not already done it.

    Lets the app start against a database built before Task 3 existed.
    """
    conn = connect(db_path)
    try:
        with conn:
            with io.open(pipeline_config.AUDIO_SCHEMA_PATH, encoding="utf-8") as fh:
                conn.executescript(fh.read())
    finally:
        conn.close()


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(131072), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# person resolution
# ---------------------------------------------------------------------------

def resolve_person(conn, name, phone_raw):
    """Find or create the person behind a submission.

    Returns (person_id, phone_normalised, method, problems).

    method is one of:
      matched_existing  the normalised phone already belongs to a person
      created_contact   a new contact, appended to app_contacts (a durable
                        source) and materialised into people
      unlinked          the phone could not be parsed, so no identity claim is
                        made - the submission is still stored
    """
    norm = normalize.normalize_phone(phone_raw)
    phone = norm.value
    if phone is None:
        return None, None, "unlinked", norm.problems

    row = conn.execute("SELECT person_id FROM people WHERE phone = ?", (phone,)).fetchone()
    if row:
        return row["person_id"], phone, "matched_existing", norm.problems

    now = datetime.now().isoformat(timespec="seconds")
    name_norm = normalize.normalize_name(name)
    display = name_norm.value or name

    # app_contacts is the durable record: the pipeline re-reads it as a fourth
    # source on every rebuild, so this person survives `make pipeline`.
    conn.execute(
        "INSERT OR IGNORE INTO app_contacts (created_at, full_name, phone, "
        "phone_raw, origin) VALUES (?, ?, ?, ?, 'audio_app')",
        (now, display, phone, phone_raw))

    # people is derived, but the app still needs a person_id right now, so the
    # row is materialised here too. The rebuild will recreate it from
    # app_contacts with a deterministic id, and relink_person_references will
    # re-point this submission at it.
    cur = conn.execute(
        "INSERT INTO people (full_name, phone, sources, source_count, "
        "record_count, first_seen_source, match_methods, match_confidence, "
        "load_run_id, created_at) VALUES (?, ?, ?, 1, 1, ?, 'single_source', "
        "1.0, NULL, ?)",
        (display, phone, pipeline_config.SOURCE_LABELS["audio_app"],
         pipeline_config.SOURCE_LABELS["audio_app"], now))
    return cur.lastrowid, phone, "created_contact", name_norm.problems + norm.problems


# ---------------------------------------------------------------------------
# submissions
# ---------------------------------------------------------------------------

def insert_submission(conn, name, phone_raw, stored_filename, original_filename,
                      mime_type, capture_mode, analysis, person_id, phone,
                      link_method, sha256):
    duplicate = conn.execute(
        "SELECT submission_id FROM audio_submissions WHERE sha256 = ? "
        "ORDER BY submission_id LIMIT 1", (sha256,)).fetchone()

    columns = ["submitted_at", "submitted_name", "submitted_phone_raw", "phone",
               "person_id", "person_link_method", "capture_mode",
               "original_filename", "stored_filename", "mime_type", "sha256",
               "duplicate_of", "quality_reasons"] + SUBMISSION_FIELDS
    values = [
        datetime.now().isoformat(timespec="seconds"), name, phone_raw, phone,
        person_id, link_method, capture_mode, original_filename, stored_filename,
        mime_type, sha256, duplicate["submission_id"] if duplicate else None,
        json.dumps(analysis.get("quality_reasons") or []),
    ] + [analysis.get(field) for field in SUBMISSION_FIELDS]

    cur = conn.execute(
        "INSERT INTO audio_submissions (%s) VALUES (%s)" % (
            ", ".join(columns), ", ".join(["?"] * len(columns))), values)
    return cur.lastrowid, (duplicate["submission_id"] if duplicate else None)


def list_submissions(conn, limit=200):
    rows = conn.execute(
        "SELECT * FROM v_submissions ORDER BY submission_id DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for row in rows:
        item = {k: row[k] for k in row.keys()}
        try:
            item["quality_reasons"] = json.loads(item.get("quality_reasons") or "[]")
        except ValueError:
            item["quality_reasons"] = []
        out.append(item)
    return out


def get_submission(conn, submission_id):
    row = conn.execute("SELECT * FROM v_submissions WHERE submission_id = ?",
                       (submission_id,)).fetchone()
    if not row:
        return None
    item = {k: row[k] for k in row.keys()}
    try:
        item["quality_reasons"] = json.loads(item.get("quality_reasons") or "[]")
    except ValueError:
        item["quality_reasons"] = []
    return item


def stats(conn):
    row = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(analysis_ok) AS analysed,
               SUM(person_id IS NOT NULL) AS linked,
               SUM(duplicate_of IS NOT NULL) AS duplicates,
               ROUND(AVG(duration_seconds), 2) AS avg_duration,
               ROUND(AVG(quality_score), 1) AS avg_quality,
               ROUND(SUM(file_size_bytes) / 1048576.0, 2) AS total_mb
          FROM audio_submissions
    """).fetchone()
    return {k: row[k] for k in row.keys()}
