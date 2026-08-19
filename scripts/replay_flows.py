"""Replay the two n8n flows against a running app, without n8n.

    python3 scripts/replay_flows.py                 # both flows
    python3 scripts/replay_flows.py --flow tagging
    python3 scripts/replay_flows.py --flow duplicates

**This is a test harness, not the Task 2 deliverable.** The deliverable is the
workflow JSON in automation/n8n/, and the video shows it running inside n8n. This
script exists because a flow is only as good as the API contract underneath it,
and that contract deserves to be checkable in one command instead of by clicking
through a canvas. It makes exactly the same HTTP calls, in the same order, with
the same prompt.

The LLM step calls the real Anthropic API when ANTHROPIC_API_KEY is set. Without
a key it falls back to a deterministic rule-based classifier so the write-back
path is still exercisable offline - clearly labelled in the output and in the
`model` column, because a stub result must never be mistaken for a model result.
"""
import argparse
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("CONSULTBAE_BASE_URL", "http://127.0.0.1:5055")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_CSV = os.path.join(ROOT, "automation", "sample_incoming", "new_applicants_batch.csv")

# Column aliases, mirroring the Code node in 02_duplicate_alert_on_new_csv.json.
ALIASES = {
    "name": ["name", "full name", "full_name", "worker_name", "candidate", "contact name"],
    "email": ["email", "email_id", "email address", "e-mail", "mail"],
    "phone": ["phone", "phone number", "phone_number", "mobile", "contact", "contact number"],
    "city": ["city", "location", "base city", "current city"],
}


def request_json(url, payload=None, method=None, headers=None, timeout=60):
    data = None
    all_headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        all_headers["Content-Type"] = "application/json"
    all_headers.update(headers or {})

    req = urllib.request.Request(url, data=data, headers=all_headers,
                                method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.getcode(), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"error": body[:400]}
    except urllib.error.URLError as exc:
        raise SystemExit("cannot reach %s (%s).\nStart the app first: make app" % (url, exc.reason))


# ---------------------------------------------------------------------------
# Flow 1 - LLM skill tagging
# ---------------------------------------------------------------------------

def call_claude(person, batch, api_key):
    """The same request the HTTP Request node in the workflow makes."""
    system = (
        batch["instructions"] + "\n\nAllowed categories:\n"
        + "\n".join("- %s: %s" % (key, meaning)
                    for key, meaning in batch["category_definitions"].items())
        + "\n\nRespond with JSON only. No prose, no code fences."
    )
    payload = {
        "model": MODEL,
        "max_tokens": 300,
        "temperature": 0,
        "system": system,
        "messages": [{
            "role": "user",
            "content": ("Contractor: %s\nCity: %s\nYears of experience: %s\nSkills: %s" % (
                person["full_name"], person.get("city") or "unknown",
                person.get("experience_years") if person.get("experience_years") is not None else "unknown",
                ", ".join(person["skills"]))),
        }],
    }
    status, body = request_json(ANTHROPIC_URL, payload, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    })
    if status != 200:
        return None, "api_error: %s" % json.dumps(body)[:200]

    text = "".join(block.get("text", "") for block in body.get("content", [])
                   if block.get("type") == "text").strip()
    cleaned = text.lstrip("`").replace("json\n", "", 1).rstrip("`").strip()
    try:
        return json.loads(cleaned), body.get("model") or MODEL
    except ValueError:
        return None, "unparseable_json: %s" % text[:200]


# Offline stand-in. Weights are crude on purpose: this is a placeholder for a
# model, not a competing classifier, and it says so in the output.
STUB_WEIGHTS = {
    "automation-heavy": {"n8n": 3, "Zapier": 3, "LangChain": 2},
    "web-dev": {"React": 3, "JavaScript": 2},
    "data": {"Pandas": 3, "SQL": 2, "MySQL": 1, "MongoDB": 1},
    "backend-api": {"FastAPI": 3, "REST APIs": 2, "Docker": 2, "MongoDB": 1, "MySQL": 1},
    "qa-automation": {"Selenium": 3, "Web Scraping": 2},
}


def stub_classify(person, allowed):
    scores = {}
    for category, weights in STUB_WEIGHTS.items():
        total = sum(weight for skill, weight in weights.items() if skill in person["skills"])
        if total:
            scores[category] = total
    if not scores:
        return {"category": "generalist", "confidence": 0.4,
                "rationale": "no scoring skill matched", "key_skills": []}

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, top_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if top_score == runner_up:
        return {"category": "generalist", "confidence": 0.45,
                "rationale": "two areas tied", "key_skills": person["skills"][:3]}
    confidence = min(0.95, 0.55 + 0.1 * (top_score - runner_up))
    key = [skill for skill in person["skills"] if skill in STUB_WEIGHTS[top]]
    return {"category": top if top in allowed else "generalist",
            "confidence": round(confidence, 2),
            "rationale": "dominant skills: %s" % ", ".join(key[:3]),
            "key_skills": key[:3]}


def flow_tagging(base, limit, dry_run):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    print("\n=== Flow 1: LLM skill tagging ===")
    print("  classifier: %s" % (
        "Anthropic %s (live)" % MODEL if api_key else
        "OFFLINE STUB (set ANTHROPIC_API_KEY to use the real model)"))

    status, batch = request_json("%s/api/people/untagged?limit=%d" % (base, limit))
    if status != 200:
        raise SystemExit("GET /api/people/untagged failed: %s" % batch)

    print("  %d untagged people in this batch, %d remaining after it"
          % (batch["count"], batch["remaining_after_this_batch"]))
    if not batch["count"]:
        print("  nothing to do")
        return

    written = skipped = 0
    for person in batch["people"]:
        if api_key:
            verdict, model = call_claude(person, batch, api_key)
            if verdict is None:
                print("  SKIP  #%-3d %-18s %s" % (person["person_id"], person["full_name"], model))
                skipped += 1
                continue
        else:
            verdict = stub_classify(person, batch["allowed_categories"])
            model = "offline-stub (not a model)"

        category = str(verdict.get("category", "")).strip().lower()
        if category not in batch["allowed_categories"]:
            print("  SKIP  #%-3d %-18s category not allowed: %r"
                  % (person["person_id"], person["full_name"], verdict.get("category")))
            skipped += 1
            continue

        print("  %-14s #%-3d %-18s %-16s conf %-5s %s" % (
            "would write" if dry_run else "write", person["person_id"],
            person["full_name"], category, verdict.get("confidence"),
            ", ".join(person["skills"][:4])))

        if dry_run:
            continue

        status, response = request_json(
            "%s/api/people/%d/category" % (base, person["person_id"]),
            {"category": category, "confidence": verdict.get("confidence"),
             "rationale": verdict.get("rationale"),
             "key_skills": verdict.get("key_skills"), "model": model,
             "tagged_by": "scripts/replay_flows.py"})
        if status == 200:
            written += 1
        else:
            print("        write-back rejected (%d): %s" % (status, response.get("error")))
            skipped += 1

    print("  wrote %d, skipped %d" % (written, skipped))
    status, summary = request_json("%s/api/categories" % base)
    if status == 200:
        print("  category spread now:")
        for row in summary["by_category"]:
            print("    %-16s %-3d people   avg confidence %s"
                  % (row["category"], row["people"], row["avg_confidence"]))
        if summary["low_confidence"]:
            print("  %d low-confidence tag(s) for a human to check"
                  % len(summary["low_confidence"]))


# ---------------------------------------------------------------------------
# Flow 2 - duplicate alert
# ---------------------------------------------------------------------------

def map_csv(path):
    """Mirror of the workflow's Code node: alias the columns, drop junk rows."""
    rows, skipped = [], []
    with io.open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for index, raw in enumerate(reader, start=2):
            values = [(v or "").strip() for v in raw.values()]
            if all(v == "" for v in values):
                skipped.append({"line": index, "reason": "blank row"})
                continue
            if all((k or "").strip().lower() == (v or "").strip().lower()
                   for k, v in raw.items()):
                skipped.append({"line": index, "reason": "repeated header row"})
                continue

            def pick(keys):
                for key, value in raw.items():
                    if (key or "").strip().lower() in keys and (value or "").strip():
                        return value.strip()
                return ""

            row = {"name": pick(ALIASES["name"]), "email": pick(ALIASES["email"]),
                   "phone": pick(ALIASES["phone"]), "city": pick(ALIASES["city"]),
                   "source_line": index}
            if not (row["name"] or row["email"] or row["phone"]):
                skipped.append({"line": index, "reason": "no name, email or phone"})
                continue
            rows.append(row)
    return rows, skipped


def flow_duplicates(base, csv_path):
    print("\n=== Flow 2: duplicate alert on a new CSV ===")
    print("  file: %s" % os.path.relpath(csv_path, ROOT))

    rows, skipped = map_csv(csv_path)
    print("  parsed %d row(s), dropped %d before the API call:" % (len(rows), len(skipped)))
    for item in skipped:
        print("    line %d - %s" % (item["line"], item["reason"]))

    status, result = request_json("%s/api/match/check" % base,
                                 {"source": os.path.basename(csv_path), "rows": rows})
    if status != 200:
        raise SystemExit("POST /api/match/check failed: %s" % result)

    print("\n  %s" % result["summary"])
    for row in result["results"]:
        marker = {"duplicate": "DUP ", "needs_review": "?   ", "new": "new "}[row["status"]]
        target = (" -> person #%d %s (on %s)" % (row["person_id"], row["person_name"],
                                                 row["matched_on"])
                  if row["person_id"] else "")
        print("  %s line %-3d %-16s%s" % (marker, row["input"]["source_line"],
                                          row["normalised"]["name"] or "(no name)", target))
        for warning in row["warnings"]:
            print("        ! %s" % warning)

    print("\n  needs_attention = %s -> the IF node would %s"
          % (result["needs_attention"],
             "send the alert" if result["needs_attention"] else "take the all-clear branch"))
    if result["undeliverable_email_count"]:
        print("  %d address(es) on RFC 2606 reserved domains, so the alert says so "
              "instead of emailing them" % result["undeliverable_email_count"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=DEFAULT_BASE, help="app base URL")
    parser.add_argument("--flow", choices=["tagging", "duplicates", "both"], default="both")
    parser.add_argument("--limit", type=int, default=60, help="people per tagging batch")
    parser.add_argument("--csv", default=SAMPLE_CSV, help="CSV to feed the duplicate flow")
    parser.add_argument("--dry-run", action="store_true",
                        help="classify but do not write anything back")
    args = parser.parse_args(argv)

    status, health = request_json("%s/healthz" % args.base)
    if status != 200:
        raise SystemExit("app is not healthy: %s" % health)
    print("app: %s  (%d people, %d submissions)"
          % (args.base, health["people"], health["submissions"]))

    if args.flow in ("tagging", "both"):
        flow_tagging(args.base, args.limit, args.dry_run)
    if args.flow in ("duplicates", "both"):
        flow_duplicates(args.base, args.csv)
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
