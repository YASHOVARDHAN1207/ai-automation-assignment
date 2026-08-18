"""Endpoints the Task 2 n8n flows talk to.

Registered as a Flask blueprint on the same app as Task 3, so one process serves
both and n8n has a single base URL.

The design rule here is that **the flow is not trusted**. n8n is a convenient
place to orchestrate steps and a terrible place to enforce correctness: an LLM
node will happily return "Automation Heavy!!" or a category nobody defined, and a
webhook can be called by anything. So:

  * a category is rejected unless it is in the allowlist, exactly
  * confidence must parse as a number in [0, 1]
  * the duplicate checker re-normalises every incoming value through
    pipeline.normalize rather than believing what the Code node sent
  * every write is recorded in automation_events, so "the flow ran" is a
    checkable claim rather than an assertion
"""
import json
from collections import Counter, OrderedDict
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request

from app import db as app_db
from pipeline import normalize

bp = Blueprint("automation", __name__)

# The closed vocabulary the LLM must choose from. Kept small on purpose: a
# free-text category from a language model is not a category, it is a suggestion,
# and it makes every downstream GROUP BY meaningless.
SKILL_CATEGORIES = OrderedDict([
    ("automation-heavy", "n8n, Zapier, LangChain, scripted integrations, LLM plumbing"),
    ("web-dev", "React, JavaScript and front-end work"),
    ("data", "SQL, Pandas, analytics, reporting, data wrangling"),
    ("backend-api", "FastAPI, REST APIs, MongoDB/MySQL service work, Docker"),
    ("qa-automation", "Selenium, web scraping, test automation"),
    ("generalist", "no single area dominates the skill list"),
])

CLASSIFIER_INSTRUCTIONS = (
    "You are tagging contractors in a staffing database by their dominant skill "
    "area. Choose exactly one category from the allowed list. Judge by what the "
    "person would be hired to do, not by the longest sublist. Return strict JSON "
    "with keys: category, confidence (0-1), rationale (max 20 words), key_skills "
    "(the 1-3 skills that decided it). If the skills genuinely span areas with no "
    "clear winner, answer generalist with a confidence below 0.6 rather than "
    "picking a favourite."
)


def _conn():
    return app_db.connect(current_app.config["DB_PATH"])


def _log_event(conn, flow, event_type, summary, payload):
    conn.execute(
        "INSERT INTO automation_events (created_at, flow, event_type, summary, "
        "payload) VALUES (?, ?, ?, ?, ?)",
        (datetime.now().isoformat(timespec="seconds"), flow, event_type, summary,
         json.dumps(payload, ensure_ascii=False)))


# ---------------------------------------------------------------------------
# Flow 1 - LLM skill categorisation
# ---------------------------------------------------------------------------

@bp.route("/api/people/untagged", methods=["GET"])
def untagged_people():
    """People with skills but no skill category yet.

    The flow polls this, so it must be idempotent: once a person is tagged they
    stop appearing, which means a re-run costs nothing and a crashed run simply
    resumes. That is the difference between an automation you can re-run and one
    you have to babysit.
    """
    try:
        limit = max(1, min(int(request.args.get("limit", 20)), 200))
    except ValueError:
        limit = 20

    conn = _conn()
    try:
        rows = conn.execute("""
            SELECT p.person_id, p.full_name, p.email, p.phone, p.city,
                   p.experience_years, p.gig_status, p.rate_monthly_inr,
                   COALESCE(p.email, p.phone) AS person_key,
                   (SELECT GROUP_CONCAT(s.skill_name, ', ')
                      FROM person_skills ps JOIN skills s ON s.skill_id = ps.skill_id
                     WHERE ps.person_id = p.person_id) AS skills
              FROM people p
             WHERE COALESCE(p.email, p.phone) IS NOT NULL
               AND COALESCE(p.email, p.phone) NOT IN
                   (SELECT person_key FROM person_skill_categories)
               AND EXISTS (SELECT 1 FROM person_skills ps
                            WHERE ps.person_id = p.person_id)
             ORDER BY p.person_id
             LIMIT ?
        """, (limit,)).fetchall()

        remaining = conn.execute("""
            SELECT COUNT(*) FROM people p
             WHERE COALESCE(p.email, p.phone) IS NOT NULL
               AND COALESCE(p.email, p.phone) NOT IN
                   (SELECT person_key FROM person_skill_categories)
               AND EXISTS (SELECT 1 FROM person_skills ps
                            WHERE ps.person_id = p.person_id)
        """).fetchone()[0]
    finally:
        conn.close()

    people = []
    for row in rows:
        item = {k: row[k] for k in row.keys()}
        item["skills"] = [s.strip() for s in (item.get("skills") or "").split(",") if s.strip()]
        people.append(item)

    return jsonify({
        "count": len(people),
        "remaining_after_this_batch": max(0, remaining - len(people)),
        "allowed_categories": list(SKILL_CATEGORIES.keys()),
        "category_definitions": SKILL_CATEGORIES,
        "instructions": CLASSIFIER_INSTRUCTIONS,
        "people": people,
    })


@bp.route("/api/people/<int:person_id>/category", methods=["POST"])
def set_person_category(person_id):
    """Write back one LLM verdict, after checking it."""
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    category = (payload.get("category") or "").strip().lower()

    if category not in SKILL_CATEGORIES:
        # Refusing here is the point. An unrecognised category written into the
        # table would silently break every report built on it, and "the LLM said
        # so" is not a schema.
        return jsonify({
            "ok": False,
            "error": "category %r is not in the allowed list" % payload.get("category"),
            "allowed_categories": list(SKILL_CATEGORIES.keys()),
        }), 400

    confidence = payload.get("confidence")
    try:
        confidence = None if confidence in (None, "") else float(confidence)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "confidence must be a number"}), 400
    if confidence is not None and not 0.0 <= confidence <= 1.0:
        return jsonify({"ok": False, "error": "confidence must be between 0 and 1"}), 400

    key_skills = payload.get("key_skills")
    if isinstance(key_skills, (list, tuple)):
        key_skills = ", ".join(str(s) for s in key_skills)

    conn = _conn()
    try:
        person = conn.execute(
            "SELECT person_id, full_name, COALESCE(email, phone) AS person_key "
            "FROM people WHERE person_id = ?", (person_id,)).fetchone()
        if person is None:
            return jsonify({"ok": False, "error": "person %d not found" % person_id}), 404
        if not person["person_key"]:
            return jsonify({"ok": False, "error":
                            "person %d has neither email nor phone, so there is no "
                            "durable key to attach a tag to" % person_id}), 409

        with conn:
            conn.execute("""
                INSERT INTO person_skill_categories
                       (person_key, person_id, category, confidence, rationale,
                        key_skills, model, tagged_by, tagged_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(person_key) DO UPDATE SET
                    person_id = excluded.person_id,
                    category = excluded.category,
                    confidence = excluded.confidence,
                    rationale = excluded.rationale,
                    key_skills = excluded.key_skills,
                    model = excluded.model,
                    tagged_by = excluded.tagged_by,
                    tagged_at = excluded.tagged_at
            """, (person["person_key"], person_id, category, confidence,
                  (payload.get("rationale") or "")[:500], key_skills,
                  payload.get("model"), payload.get("tagged_by") or "n8n",
                  datetime.now().isoformat(timespec="seconds")))
            _log_event(conn, payload.get("tagged_by") or "n8n", "person_tagged",
                       "%s -> %s" % (person["full_name"], category),
                       {"person_id": person_id, "category": category,
                        "confidence": confidence, "model": payload.get("model")})
    finally:
        conn.close()

    return jsonify({"ok": True, "person_id": person_id, "category": category,
                    "confidence": confidence}), 200


@bp.route("/api/categories", methods=["GET"])
def category_summary():
    conn = _conn()
    try:
        rows = conn.execute("""
            SELECT category, COUNT(*) AS people, ROUND(AVG(confidence), 3) AS avg_confidence
              FROM person_skill_categories GROUP BY category ORDER BY people DESC
        """).fetchall()
        tagged = conn.execute("SELECT COUNT(*) FROM person_skill_categories").fetchone()[0]
        low = conn.execute(
            "SELECT person_id, category, confidence, rationale FROM "
            "person_skill_categories WHERE confidence < 0.6 ORDER BY confidence").fetchall()
    finally:
        conn.close()
    return jsonify({
        "tagged": tagged,
        "by_category": [{k: r[k] for k in r.keys()} for r in rows],
        "low_confidence": [{k: r[k] for k in r.keys()} for r in low],
    })


# ---------------------------------------------------------------------------
# Flow 2 - duplicate detection for an incoming file
# ---------------------------------------------------------------------------

def _classify_incoming(conn, row):
    """Decide whether one incoming row is a duplicate, new, or a review case.

    Mirrors the pipeline's matching order deliberately: exact email, then exact
    phone, then name+city as a *review* signal only - never as a merge. Anything
    else and the automation would contradict the database it is guarding.
    """
    name = normalize.normalize_name(row.get("name") or row.get("full_name"))
    email = normalize.normalize_email(row.get("email"))
    phone = normalize.normalize_phone(row.get("phone"))
    city = normalize.normalize_city(row.get("city")) if row.get("city") else normalize.Norm(None)

    warnings = []
    for norm in (name, email, phone, city):
        for problem in norm.problems:
            if problem["severity"] != "info":
                warnings.append(problem["description"])

    result = OrderedDict([
        ("input", row),
        ("normalised", {"name": name.value, "email": email.value,
                        "phone": phone.value, "city": city.value}),
        ("status", "new"),
        ("matched_on", None),
        ("person_id", None),
        ("person_name", None),
        ("warnings", warnings),
    ])

    match = None
    if email.value:
        match = conn.execute(
            "SELECT person_id, full_name, city, sources FROM people WHERE email = ?",
            (email.value,)).fetchone()
        if match:
            result["matched_on"] = "email"
    if match is None and phone.value:
        match = conn.execute(
            "SELECT person_id, full_name, city, sources FROM people WHERE phone = ?",
            (phone.value,)).fetchone()
        if match:
            result["matched_on"] = "phone"

    if match is not None:
        result.update(status="duplicate", person_id=match["person_id"],
                      person_name=match["full_name"])
        result["existing_sources"] = match["sources"]
        return result

    if name.value and city.value:
        candidates = conn.execute(
            "SELECT person_id, full_name, email, phone FROM people "
            "WHERE LOWER(full_name) = ? AND city = ?",
            (name.value.lower(), city.value)).fetchall()
        if len(candidates) == 1:
            result.update(status="needs_review", matched_on="name_city",
                          person_id=candidates[0]["person_id"],
                          person_name=candidates[0]["full_name"])
            result["warnings"].append(
                "same name and city as person #%d but no shared email or phone - "
                "not treated as a duplicate, because three people in this database "
                "share the name 'Arjun Mehta' in Noida"
                % candidates[0]["person_id"])
        elif len(candidates) > 1:
            result.update(status="needs_review", matched_on="name_city_ambiguous")
            result["warnings"].append(
                "%d existing people share this name and city; a name match cannot "
                "identify anyone" % len(candidates))

    # A worker with no phone in the database can never be matched on phone, so a
    # "clean miss" here is not proof of a new person.
    if result["status"] == "new" and not email.value and phone.value is None:
        result["status"] = "needs_review"
        result["warnings"].append(
            "neither email nor phone could be parsed, so no duplicate check was "
            "possible")

    return result


@bp.route("/api/match/check", methods=["POST"])
def check_batch():
    """Check a batch of incoming rows against the merged database.

    Called by the duplicate-alert flow with whatever it parsed out of a new CSV.
    Returns a per-row verdict plus a summary the flow branches on.
    """
    payload = request.get_json(silent=True) or {}
    rows = payload.get("rows")
    if rows is None and isinstance(payload, list):
        rows = payload
    if not isinstance(rows, list):
        return jsonify({"ok": False,
                        "error": "expected {\"rows\": [...]} with one object per "
                                 "incoming record"}), 400
    if len(rows) > 1000:
        return jsonify({"ok": False, "error": "batch limited to 1000 rows"}), 413

    source_label = payload.get("source") or "unknown upload"
    conn = _conn()
    try:
        results = [_classify_incoming(conn, row if isinstance(row, dict) else {})
                   for row in rows]
        counts = Counter(r["status"] for r in results)

        # An automation that emails a person is a side effect, so the check for
        # undeliverable addresses lives here rather than in the flow: see
        # docs/DATA_ISSUES.md #25 - every address in this dataset is on an RFC
        # 2606 reserved domain.
        reserved = [r for r in results if r["normalised"]["email"]
                    and r["normalised"]["email"].split("@")[-1].startswith("example.")]

        summary = ("%d row(s): %d duplicate, %d new, %d need review"
                   % (len(results), counts.get("duplicate", 0), counts.get("new", 0),
                      counts.get("needs_review", 0)))
        with conn:
            _log_event(conn, "duplicate-alert", "batch_checked", summary,
                       {"source": source_label, "counts": dict(counts),
                        "duplicates": [
                            {"input": r["input"], "person_id": r["person_id"],
                             "matched_on": r["matched_on"]}
                            for r in results if r["status"] == "duplicate"]})
    finally:
        conn.close()

    return jsonify({
        "ok": True,
        "source": source_label,
        "checked": len(results),
        "duplicate_count": counts.get("duplicate", 0),
        "new_count": counts.get("new", 0),
        "review_count": counts.get("needs_review", 0),
        "has_duplicates": counts.get("duplicate", 0) > 0,
        "needs_attention": counts.get("duplicate", 0) + counts.get("needs_review", 0) > 0,
        "undeliverable_email_count": len(reserved),
        "summary": summary,
        "results": results,
    })


@bp.route("/api/automation/events", methods=["GET"])
def automation_events():
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM automation_events ORDER BY event_id DESC LIMIT ?",
            (min(int(request.args.get("limit", 50)), 500),)).fetchall()
    finally:
        conn.close()
    events = []
    for row in rows:
        item = {k: row[k] for k in row.keys()}
        try:
            item["payload"] = json.loads(item["payload"] or "null")
        except ValueError:
            pass
        events.append(item)
    return jsonify({"count": len(events), "events": events})
