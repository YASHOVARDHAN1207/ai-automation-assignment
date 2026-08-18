"""Read the three CSVs, repair structural damage, normalise every field.

Two levels of damage are handled here:

1. **Structural** - things wrong with the *shape* of a row: a fully blank line,
   a header repeated in the middle of the data, a row whose values are rotated
   out of their columns. These must be fixed before field parsing, because a
   scrambled row would otherwise produce six wrong "invalid format" errors and
   then be thrown away.

2. **Field level** - delegated to pipeline.normalize.

Output is a list of SourceRecord, which is what pipeline.match consumes.
"""
import csv
import json
import os
import re

from . import config, normalize
from .normalize import ERROR, INFO, WARN


class SourceRecord(object):
    """One physical CSV row after cleaning, before identity resolution."""

    __slots__ = ("source", "row_number", "raw", "fields", "skills", "usable", "repairs")

    def __init__(self, source, row_number, raw):
        self.source = source
        self.row_number = row_number
        self.raw = raw
        self.fields = {}
        self.skills = []
        self.usable = True
        self.repairs = []

    @property
    def uid(self):
        return "%s:%d" % (self.source, self.row_number)

    @property
    def email(self):
        return self.fields.get("email")

    @property
    def phone(self):
        return self.fields.get("phone")

    @property
    def name_key(self):
        return self.fields.get("name_key")

    def raw_json(self):
        return json.dumps(self.raw, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# structural repair
# ---------------------------------------------------------------------------

_EMAILISH = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_RATEISH = re.compile(r"^\d+(\.\d+)?\s*(k|l|lac|lakh)?\s*/\s*(hr|hour|hourly|month|mo|monthly|day|daily)$", re.I)
_NAMEISH = re.compile(r"^[A-Za-z][A-Za-z.'\- ]{1,60}$")


def _looks_like_email(v):
    return 1.0 if _EMAILISH.match(v or "") else 0.0


def _looks_like_rate(v):
    return 1.0 if _RATEISH.match((v or "").strip()) else 0.0


def _looks_like_status(v):
    return 1.0 if (v or "").strip().lower() in config.GIG_STATUS_CANON else 0.0


def _looks_like_city(v):
    return 1.0 if (v or "").strip().lower() in config.CITY_CANON else 0.0


def _looks_like_name(v):
    text = (v or "").strip()
    if not _NAMEISH.match(text) or "@" in text:
        return 0.0
    # A skill list also matches "name-ish" once commas are gone, so penalise
    # anything that contains a comma or a known skill token.
    if "," in (v or ""):
        return 0.0
    return 1.0 if 1 <= len(text.split()) <= 4 else 0.3


def _looks_like_skills(v):
    parts = [p.strip().lower() for p in (v or "").split(",") if p.strip()]
    if not parts:
        return 0.0
    hits = sum(1 for p in parts if p in config.SKILL_CANON)
    return hits / float(len(parts))


# Per-column validators for source2, in header order.
GIG_VALIDATORS = [
    ("email_id", _looks_like_email),
    ("worker_name", _looks_like_name),
    ("rate", _looks_like_rate),
    ("location", _looks_like_city),
    ("status", _looks_like_status),
    ("skill_tags", _looks_like_skills),
]


def _score_assignment(values, validators):
    return sum(check(values[i]) for i, (_, check) in enumerate(validators))


def repair_rotation(values, validators):
    """Try every cyclic rotation of a row's values and keep the best-scoring one.

    Only *rotations* are tried, not all permutations. With six columns there are
    720 permutations and plenty of them would score well by accident (name and
    city are both "a short word"), so an exhaustive search invents damage that
    is not there. A rotation is what an off-by-one write in an export script
    actually produces, there are only six of them, and a rotation only wins here
    if it beats the original by a clear margin.

    Returns (repaired_values, shift, before_score, after_score).
    """
    n = len(values)
    base = _score_assignment(values, validators)
    best, best_shift, best_score = values, 0, base
    for shift in range(1, n):
        rotated = values[shift:] + values[:shift]
        score = _score_assignment(rotated, validators)
        if score > best_score + 1.0:      # must be a decisive win, not a tie
            best, best_shift, best_score = rotated, shift, score
    return best, best_shift, base, best_score


# ---------------------------------------------------------------------------
# shared row-level guards
# ---------------------------------------------------------------------------

def _is_blank_row(values):
    return all(normalize.is_blank(v) for v in values)


def _is_header_repeat(values, header):
    if len(values) != len(header):
        return False
    return [normalize.squash(v).lower() for v in values] == \
           [normalize.squash(h).lower() for h in header]


def _read_rows(path):
    """Yield (row_number, values). row_number is the 1-based line in the file,
    so it matches what a human sees when they open the CSV."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        for idx, values in enumerate(reader, start=1):
            yield idx, values


# ---------------------------------------------------------------------------
# source 1 - naukri applicants
# ---------------------------------------------------------------------------

def extract_naukri(log):
    source = "naukri"
    path = config.SOURCES[source]
    records = []
    header = None

    for row_number, values in _read_rows(path):
        if header is None:
            header = values
            continue
        if _is_blank_row(values):
            log.add("blank_row", "Row %d is completely empty" % row_number,
                    "Skipped; it carries no person", WARN, source, row_number)
            continue
        if _is_header_repeat(values, header):
            log.add("repeated_header", "Row %d repeats the header row" % row_number,
                    "Skipped; it is a file-concatenation artefact, not a person",
                    WARN, source, row_number)
            continue
        if len(values) != len(header):
            log.add("column_count_mismatch",
                    "Row %d has %d fields, header has %d" % (row_number, len(values), len(header)),
                    "Padded/truncated to the header width and flagged", WARN, source, row_number)
            values = (values + [""] * len(header))[:len(header)]

        raw = dict(zip(header, values))
        rec = SourceRecord(source, row_number, raw)

        name = normalize.normalize_name(raw.get("Full Name"), "Full Name")
        email = normalize.normalize_email(raw.get("Email"), "Email")
        phone = normalize.normalize_phone(raw.get("Phone"), "Phone")
        city = normalize.normalize_city(raw.get("City"), "City")
        exp = normalize.normalize_float(raw.get("Experience (Years)"), "Experience (Years)",
                                        config.EXPERIENCE_MIN, config.EXPERIENCE_MAX)
        ctc = normalize.normalize_ctc(raw.get("Current CTC"), "Current CTC")
        applied = normalize.normalize_date(raw.get("Applied Date"), "Applied Date")
        skills = normalize.normalize_skills(raw.get("Skills"), "Skills")

        for norm in (name, email, phone, city, exp, ctc, applied, skills):
            log.extend(norm.problems, source, row_number, entity=name.value)

        if name.value is None and email.value is None and phone.value is None:
            rec.usable = False
            log.add("unidentifiable_row",
                    "Row %d has no name, no valid email and no valid phone" % row_number,
                    "Rejected; there is nothing to merge on", ERROR, source, row_number)
            records.append(rec)
            continue

        rec.fields = {
            "full_name": name.value,
            "name_key": name.extra.get("key"),
            "name_tokens": name.extra.get("tokens"),
            "name_is_abbreviated": bool(name.extra.get("initials_only")),
            "email": email.value,
            "email_alias_of": email.extra.get("alias_of"),
            "phone": phone.value,
            "city": city.value,
            "city_region": city.extra.get("region"),
            "city_is_region_guess": bool(city.extra.get("region_only")),
            "experience_years": exp.value,
            "ctc_annual_inr": ctc.value,
            "ctc_unit_detected": ctc.extra.get("unit"),
            "ctc_out_of_range": bool(ctc.extra.get("out_of_range")),
            "applied_date": applied.value,
            "applied_date_ambiguous": bool(applied.extra.get("ambiguous")),
            "applied_date_is_future": bool(applied.extra.get("is_future")),
        }
        rec.skills = skills.value
        records.append(rec)

    return records


# ---------------------------------------------------------------------------
# source 2 - gig workers
# ---------------------------------------------------------------------------

def extract_gig(log):
    source = "gig"
    path = config.SOURCES[source]
    records = []
    header = None

    for row_number, values in _read_rows(path):
        if header is None:
            header = values
            continue
        if _is_blank_row(values):
            log.add("blank_row",
                    "Row %d is a fully empty record (',,,,,')" % row_number,
                    "Skipped; no field carries any value", WARN, source, row_number)
            continue
        if _is_header_repeat(values, header):
            log.add("repeated_header", "Row %d repeats the header row" % row_number,
                    "Skipped; file-concatenation artefact", WARN, source, row_number)
            continue
        if len(values) != len(header):
            log.add("column_count_mismatch",
                    "Row %d has %d fields, header has %d" % (row_number, len(values), len(header)),
                    "Padded/truncated to the header width and flagged", WARN, source, row_number)
            values = (values + [""] * len(header))[:len(header)]

        repaired, shift, before, after = repair_rotation(values, GIG_VALIDATORS)
        if shift:
            log.add("scrambled_columns",
                    "Row %d has its values rotated out of their columns - the "
                    "skill list sits in email_id and the email sits in "
                    "worker_name (field-type score %.1f/6 as written)" % (row_number, before),
                    "Rotated the row left by %d position(s), which scores %.1f/6, "
                    "then parsed it normally. Detected by scoring each column "
                    "against a field-type validator rather than by hardcoding "
                    "the row number" % (shift, after),
                    ERROR, source, row_number, raw_value=" | ".join(values))
        values = repaired

        raw = dict(zip(header, values))
        rec = SourceRecord(source, row_number, raw)
        if shift:
            rec.repairs.append("rotated_columns_left_%d" % shift)

        email = normalize.normalize_email(raw.get("email_id"), "email_id")
        name = normalize.normalize_name(raw.get("worker_name"), "worker_name")
        rate = normalize.normalize_rate(raw.get("rate"), "rate")
        city = normalize.normalize_city(raw.get("location"), "location")
        status = normalize.normalize_status(raw.get("status"), "status")
        skills = normalize.normalize_skills(raw.get("skill_tags"), "skill_tags")

        for norm in (email, name, rate, city, status, skills):
            log.extend(norm.problems, source, row_number, entity=name.value)

        if name.value is None and email.value is None:
            rec.usable = False
            log.add("unidentifiable_row",
                    "Row %d has neither a usable name nor a usable email" % row_number,
                    "Rejected; this source carries no phone so there is no third "
                    "key to fall back on", ERROR, source, row_number)
            records.append(rec)
            continue

        rec.fields = {
            "full_name": name.value,
            "name_key": name.extra.get("key"),
            "name_tokens": name.extra.get("tokens"),
            "name_is_abbreviated": bool(name.extra.get("initials_only")),
            "email": email.value,
            "email_alias_of": email.extra.get("alias_of"),
            "phone": None,                      # this source has no phone column
            "city": city.value,
            "city_region": city.extra.get("region"),
            "city_is_region_guess": bool(city.extra.get("region_only")),
            "rate_hourly_inr": rate.extra.get("hourly"),
            "rate_monthly_inr": rate.extra.get("monthly"),
            "rate_basis_raw": rate.extra.get("basis"),
            "rate_out_of_range": bool(rate.extra.get("out_of_range")),
            "gig_status": status.value,
        }
        rec.skills = skills.value
        records.append(rec)

    return records


# ---------------------------------------------------------------------------
# source 3 - CBNexus contacts
# ---------------------------------------------------------------------------

def extract_cbnexus(log):
    source = "cbnexus"
    path = config.SOURCES[source]
    records = []
    header = None

    for row_number, values in _read_rows(path):
        if header is None:
            header = values
            continue
        if _is_blank_row(values):
            log.add("blank_row", "Row %d is completely empty" % row_number,
                    "Skipped", WARN, source, row_number)
            continue
        if _is_header_repeat(values, header):
            log.add("repeated_header",
                    "Row %d repeats the header line verbatim in the middle of "
                    "the data" % row_number,
                    "Skipped. Left unhandled it would have become a person named "
                    "'Name' in city 'City', which is the kind of record that "
                    "quietly poisons every downstream count",
                    ERROR, source, row_number, raw_value=",".join(values))
            continue
        if len(values) != len(header):
            log.add("column_count_mismatch",
                    "Row %d has %d fields, header has %d" % (row_number, len(values), len(header)),
                    "Padded/truncated to the header width and flagged", WARN, source, row_number)
            values = (values + [""] * len(header))[:len(header)]

        raw = dict(zip(header, values))
        rec = SourceRecord(source, row_number, raw)

        name = normalize.normalize_name(raw.get("Name"), "Name")
        phone = normalize.normalize_phone(raw.get("Phone Number"), "Phone Number")
        city = normalize.normalize_city(raw.get("City"), "City")
        verified = normalize.normalize_bool(raw.get("Verified"), "Verified")
        projects = normalize.normalize_int(raw.get("Projects Completed"),
                                           "Projects Completed", 0, 1000)

        for norm in (name, phone, city, verified, projects):
            log.extend(norm.problems, source, row_number, entity=name.value)

        if name.value is None and phone.value is None:
            rec.usable = False
            log.add("unidentifiable_row",
                    "Row %d has neither a usable name nor a usable phone" % row_number,
                    "Rejected; this source carries no email", ERROR, source, row_number)
            records.append(rec)
            continue

        rec.fields = {
            "full_name": name.value,
            "name_key": name.extra.get("key"),
            "name_tokens": name.extra.get("tokens"),
            "name_is_abbreviated": bool(name.extra.get("initials_only")),
            "email": None,                      # this source has no email column
            "phone": phone.value,
            "city": city.value,
            "city_region": city.extra.get("region"),
            "city_is_region_guess": bool(city.extra.get("region_only")),
            "is_verified": verified.value,
            "projects_completed": projects.value,
        }
        records.append(rec)

    return records


# ---------------------------------------------------------------------------
# intra-source duplicate detection
# ---------------------------------------------------------------------------

def flag_intra_source_duplicates(records, log):
    """Report rows that duplicate another row *within the same file*.

    These are reported, not deleted: the identity resolver collapses them
    anyway, and reporting them separately is what makes the "same person twice
    in one file" class of problem visible in the Task 4 report.
    """
    by_email, by_phone, by_payload = {}, {}, {}
    for rec in records:
        if not rec.usable:
            continue
        key = (rec.source, rec.email)
        if rec.email:
            first = by_email.get(key)
            if first is not None:
                log.add("duplicate_within_source",
                        "Row %d repeats email %s already seen on row %d of the "
                        "same file" % (rec.row_number, rec.email, first.row_number),
                        "Both rows kept as source_records and collapsed into one "
                        "person by identity resolution",
                        WARN, rec.source, rec.row_number, "email", rec.email,
                        entity=rec.fields.get("full_name"))
            else:
                by_email[key] = rec

        if rec.phone:
            pkey = (rec.source, rec.phone)
            first = by_phone.get(pkey)
            if first is not None:
                same_email = first.email == rec.email
                log.add("duplicate_within_source",
                        "Row %d repeats phone %s already seen on row %d of the "
                        "same file%s" % (
                            rec.row_number, rec.phone, first.row_number,
                            "" if same_email else
                            " under a different email (%s vs %s)" % (first.email, rec.email)),
                        "Both rows kept as source_records and collapsed into one "
                        "person by identity resolution; the phone is what proves "
                        "they are the same human when the emails differ",
                        WARN, rec.source, rec.row_number, "phone", rec.phone,
                        entity=rec.fields.get("full_name"))
            else:
                by_phone[pkey] = rec

        payload = (rec.source, rec.raw_json())
        first = by_payload.get(payload)
        if first is not None:
            log.add("exact_duplicate_row",
                    "Row %d is byte-for-byte identical to row %d" % (rec.row_number, first.row_number),
                    "Kept one; the copy is retained in source_records for audit",
                    WARN, rec.source, rec.row_number,
                    entity=rec.fields.get("full_name"))
        else:
            by_payload[payload] = rec


# ---------------------------------------------------------------------------
# source 4 - contacts created by the Task 3 audio app
# ---------------------------------------------------------------------------

def extract_app_contacts(log, db_path=None):
    """Read the app_contacts table as if it were a fourth CSV.

    A gig worker who submits audio without existing in any of the three files is
    a real person the business now knows about. Writing them straight into
    `people` would not survive the next rebuild, because `people` is derived. So
    the app appends to app_contacts and the pipeline treats it as a source -
    which means these contacts go through exactly the same normalisation and the
    same matching passes as the CSV rows, with no special-casing.

    Returns [] when the table does not exist yet (i.e. before the app has ever
    run), so Task 1 keeps working standalone.
    """
    import sqlite3

    path = db_path or config.DB_PATH
    if not os.path.exists(path):
        return []

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_contacts'"
        ).fetchone()
        if not exists:
            return []
        rows = conn.execute(
            "SELECT * FROM app_contacts ORDER BY contact_id").fetchall()
    finally:
        conn.close()

    source = "audio_app"
    records = []
    for row in rows:
        raw = {k: row[k] for k in row.keys()}
        rec = SourceRecord(source, row["contact_id"], raw)

        name = normalize.normalize_name(row["full_name"], "full_name")
        phone = normalize.normalize_phone(row["phone_raw"] or row["phone"], "phone")
        city = (normalize.normalize_city(row["city"], "city")
                if row["city"] else normalize.Norm(None))

        for norm in (name, phone, city):
            log.extend(norm.problems, source, row["contact_id"], entity=name.value)

        if name.value is None or phone.value is None:
            rec.usable = False
            log.add("unidentifiable_row",
                    "app_contact %d has no usable name or phone" % row["contact_id"],
                    "Rejected", ERROR, source, row["contact_id"])
            records.append(rec)
            continue

        rec.fields = {
            "full_name": name.value,
            "name_key": name.extra.get("key"),
            "name_tokens": name.extra.get("tokens"),
            "name_is_abbreviated": bool(name.extra.get("initials_only")),
            "email": None,
            "phone": phone.value,
            "city": city.value,
            "city_region": city.extra.get("region"),
            "city_is_region_guess": bool(city.extra.get("region_only")),
        }
        records.append(rec)

    if records:
        log.add("app_contacts_ingested",
                "%d contact(s) created by the audio app were re-ingested as a "
                "fourth source" % len(records),
                "Merged through the same email/phone/name passes as the CSV rows. "
                "This is what makes an app submission survive a pipeline rebuild "
                "without the app ever writing to a derived table",
                INFO, source, None)
    return records


def extract_all(log, db_path=None):
    records = []
    records.extend(extract_naukri(log))
    records.extend(extract_gig(log))
    records.extend(extract_cbnexus(log))
    records.extend(extract_app_contacts(log, db_path=db_path))
    flag_intra_source_duplicates(records, log)
    return records
