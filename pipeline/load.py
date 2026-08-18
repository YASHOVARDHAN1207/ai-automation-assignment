"""Write the resolved people, their provenance and the issue log into SQLite.

The whole load runs inside one transaction: either the database ends up
completely rebuilt or it is left exactly as it was. A half-merged people table
is worse than no people table.
"""
import io
import json
import os
import sqlite3
from datetime import datetime

from . import config

PIPELINE_VERSION = "1.0.0"


def connect(db_path=None):
    path = db_path or config.DB_PATH
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn, schema_path=None):
    with io.open(schema_path or config.SCHEMA_PATH, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())


def apply_additive_schemas(conn, paths=None):
    """Create the Task 3 and Task 2 tables if they are missing.

    Safe to run on every load: everything in these files is
    CREATE ... IF NOT EXISTS, so submissions, app_contacts, LLM tags and the
    automation audit trail all survive a pipeline rebuild untouched.
    """
    for path in (paths or (config.AUDIO_SCHEMA_PATH, config.AUTOMATION_SCHEMA_PATH)):
        if os.path.exists(path):
            with io.open(path, "r", encoding="utf-8") as fh:
                conn.executescript(fh.read())


def _table_exists(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,)).fetchone())


def relink_person_references(conn):
    """Re-point the soft person_id links after `people` has been rebuilt.

    person_id is never a foreign key in the app/automation tables. Two reasons:

    1. With PRAGMA foreign_keys=ON, SQLite's DROP TABLE runs an implicit
       DELETE FROM, so a child row pointing at `people` would make the rebuild
       fail outright.
    2. person_id is deterministic for a given input set but shifts when the
       input set changes, because ids are handed out in identity-sorted order.
       Adding one contact moved Arjun Mehta from #56 to #57.

    So the durable keys are phone and email, and the ids are recomputed here.
    Returns {table: rows_linked}.
    """
    linked = {}

    if _table_exists(conn, "audio_submissions"):
        conn.execute("""
            UPDATE audio_submissions
               SET person_id = (SELECT p.person_id FROM people p
                                 WHERE p.phone = audio_submissions.phone)
             WHERE phone IS NOT NULL
        """)
        linked["audio_submissions"] = conn.execute(
            "SELECT COUNT(*) FROM audio_submissions "
            "WHERE person_id IS NOT NULL").fetchone()[0]

    if _table_exists(conn, "person_skill_categories"):
        conn.execute("""
            UPDATE person_skill_categories
               SET person_id = (SELECT p.person_id FROM people p
                                 WHERE p.email = person_skill_categories.person_key
                                    OR p.phone = person_skill_categories.person_key)
        """)
        linked["person_skill_categories"] = conn.execute(
            "SELECT COUNT(*) FROM person_skill_categories "
            "WHERE person_id IS NOT NULL").fetchone()[0]

    return linked


PEOPLE_COLUMNS = [
    "person_id", "full_name", "email", "phone", "city", "city_region",
    "city_is_region_guess", "experience_years", "ctc_annual_inr",
    "ctc_unit_detected", "ctc_out_of_range", "applied_date",
    "applied_date_ambiguous", "applied_date_is_future", "rate_hourly_inr",
    "rate_monthly_inr", "rate_basis_raw", "rate_out_of_range", "gig_status",
    "is_verified", "projects_completed", "sources", "source_count",
    "record_count", "first_seen_source", "match_methods", "match_confidence",
]


# Boolean flag columns are NOT NULL in the schema: "we did not detect a
# problem" and "there is no problem" are the same statement, so a missing flag
# means 0 rather than unknown. is_verified is deliberately NOT in this set -
# there, unknown really is a third state (the person is absent from the CRM).
FLAG_COLUMNS = {
    "city_is_region_guess", "ctc_out_of_range", "applied_date_ambiguous",
    "applied_date_is_future", "rate_out_of_range",
}


def _as_db_value(column, value):
    if value is None:
        return 0 if column in FLAG_COLUMNS else None
    if isinstance(value, bool):
        return int(value)
    return value


def load(records, people, conflicts, reviews, log, db_path=None, schema_path=None):
    """Rebuild the database from scratch. Returns the run_id."""
    conn = connect(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with conn:
            create_schema(conn, schema_path)

            cur = conn.execute(
                "INSERT INTO load_runs (started_at, ingest_date, pipeline_version) "
                "VALUES (?, ?, ?)", (now, config.INGEST_DATE, PIPELINE_VERSION))
            run_id = cur.lastrowid

            # --- people ---------------------------------------------------
            placeholders = ", ".join(["?"] * (len(PEOPLE_COLUMNS) + 2))
            conn.executemany(
                "INSERT INTO people (%s, load_run_id, created_at) VALUES (%s)" % (
                    ", ".join(PEOPLE_COLUMNS), placeholders),
                [
                    tuple(_as_db_value(col, person.get(col)) for col in PEOPLE_COLUMNS)
                    + (run_id, now)
                    for person in people
                ])

            # --- skills ---------------------------------------------------
            all_skills = []
            for person in people:
                all_skills.extend(person["_skills"].keys())
            for skill in sorted(set(all_skills)):
                conn.execute("INSERT OR IGNORE INTO skills (skill_name) VALUES (?)", (skill,))
            skill_ids = {row["skill_name"]: row["skill_id"]
                         for row in conn.execute("SELECT skill_id, skill_name FROM skills")}

            conn.executemany(
                "INSERT OR IGNORE INTO person_skills (person_id, skill_id, sources) "
                "VALUES (?, ?, ?)",
                [
                    (person["person_id"], skill_ids[skill], ", ".join(sources))
                    for person in people
                    for skill, sources in person["_skills"].items()
                ])

            # --- source records (provenance) ------------------------------
            person_by_uid = {}
            for person in people:
                for member in person["_members"]:
                    person_by_uid[member.uid] = person["person_id"]

            conn.executemany(
                "INSERT INTO source_records (source_name, source_row_number, "
                "person_id, was_used, repairs, raw_json, load_run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (config.SOURCE_LABELS[rec.source], rec.row_number,
                     person_by_uid.get(rec.uid), int(rec.usable),
                     ",".join(rec.repairs) or None, rec.raw_json(), run_id)
                    for rec in records
                ])

            # --- conflicts ------------------------------------------------
            person_id_by_ref = {person["_ref"]: person["person_id"] for person in people}
            conn.executemany(
                "INSERT INTO field_conflicts (load_run_id, person_id, field, "
                "chosen_value, chosen_source, rejected_value, rejected_source, rule) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (run_id, person_id_by_ref.get(c["person_ref"]), c["field"],
                     c["chosen_value"], c["chosen_source"], c["rejected_value"],
                     c["rejected_source"], c["rule"])
                    for c in conflicts
                ])

            # --- merge review queue ---------------------------------------
            conn.executemany(
                "INSERT INTO merge_review (load_run_id, reason, name_key, city, "
                "cluster_root, cluster_size, score, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (run_id, r["reason"], r["name_key"], r["city"],
                     str(r["cluster_root"]), r["cluster_size"], r["score"], r["detail"])
                    for r in reviews
                ])

            # --- issue log ------------------------------------------------
            conn.executemany(
                "INSERT INTO data_issues (load_run_id, severity, category, "
                "source_name, source_row_number, field, raw_value, entity, "
                "description, action_taken) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (run_id, row["severity"], row["category"], row["source"],
                     row["row_number"], row["field"], row["raw_value"],
                     row["entity"], row["description"], row["action_taken"])
                    for row in log.sorted_rows()
                ])

            # --- Task 2 / Task 3 tables: created if absent, never dropped --
            apply_additive_schemas(conn)
            for table, count in sorted(relink_person_references(conn).items()):
                if not count:
                    continue
                conn.execute(
                    "INSERT INTO data_issues (load_run_id, severity, category, "
                    "description, action_taken) VALUES (?, 'info', "
                    "'person_references_relinked', ?, ?)",
                    (run_id,
                     "%d row(s) in %s were re-pointed at the rebuilt people rows"
                     % (count, table),
                     "person_id is a soft link recomputed from email/phone after "
                     "every rebuild: a real foreign key would make DROP TABLE "
                     "people fail, and person_id shifts when the input set "
                     "changes, so it is not a durable key"))

            conn.execute(
                "UPDATE load_runs SET finished_at = ?, source_rows_read = ?, "
                "source_rows_used = ?, people_created = ?, issues_logged = ? "
                "WHERE run_id = ?",
                (datetime.now().isoformat(timespec="seconds"), len(records),
                 sum(1 for r in records if r.usable), len(people), len(log), run_id))
    finally:
        conn.close()
    return run_id
