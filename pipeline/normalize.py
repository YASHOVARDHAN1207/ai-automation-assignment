"""Field-level normalisers.

Design rule for this module: **a normaliser never throws and never silently
drops information.** It returns a `Norm` carrying the cleaned value plus a list
of problems it noticed on the way. The caller decides what to do with them;
`pipeline.issues` turns them into the Task 4 report.

Every function here is pure (no I/O, no globals mutated) which is what makes
tests/test_normalize.py possible.
"""
import re
import unicodedata
from datetime import date, datetime

from . import config

# --- problem severities ----------------------------------------------------
ERROR = "error"      # value could not be trusted; row or field was rejected
WARN = "warn"        # value was repaired or is suspicious; kept with a flag
INFO = "info"        # cosmetic inconsistency, silently standardised


class Norm(object):
    """A normalised value plus the problems found while normalising it."""

    __slots__ = ("value", "problems", "extra")

    def __init__(self, value, problems=None, extra=None):
        self.value = value
        self.problems = problems or []
        self.extra = extra or {}

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Norm(value=%r, problems=%d)" % (self.value, len(self.problems))


def problem(category, description, action, severity=WARN, field=None, raw=None):
    return {
        "category": category,
        "description": description,
        "action": action,
        "severity": severity,
        "field": field,
        "raw_value": raw,
    }


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def squash(raw):
    """Trim, collapse internal runs of whitespace, strip zero-width junk."""
    if raw is None:
        return ""
    text = unicodedata.normalize("NFKC", str(raw))
    text = text.replace("​", "").replace("﻿", "")
    return _WS_RE.sub(" ", text).strip()


def is_blank(raw):
    return squash(raw) == ""


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------

_NAME_ALLOWED_RE = re.compile(r"^[A-Za-z][A-Za-z.'\- ]*$")


def _titlecase_token(token):
    # "R." -> "R.", "GUPTA" -> "Gupta", "o'brien" -> "O'Brien"
    if len(token) <= 2 and token.endswith("."):
        return token.upper()
    out = token[:1].upper() + token[1:].lower()
    for sep in ("'", "-"):
        if sep in out:
            out = sep.join(p[:1].upper() + p[1:] for p in out.split(sep))
    return out


def normalize_name(raw, field="name"):
    """Return a display-cased name plus a match key.

    extra["key"]     lowercase punctuation-free key used for blocking
    extra["tokens"]  token list used for initial-aware comparison
    extra["initials_only"] True when the name contains an abbreviated token
    """
    text = squash(raw)
    problems = []
    if not text:
        return Norm(None, [problem("missing_value", "Name is empty", "Row rejected: a record with no name cannot be identified", ERROR, field, raw)])

    if not _NAME_ALLOWED_RE.match(text):
        problems.append(problem(
            "suspicious_value",
            "Name contains characters that are not name-like: %r" % text,
            "Kept the value but flagged it for manual review",
            WARN, field, raw))

    if text.isupper():
        problems.append(problem(
            "inconsistent_casing", "Name stored in ALL CAPS: %r" % text,
            "Re-cased to Title Case for display", INFO, field, raw))
    elif text.islower():
        problems.append(problem(
            "inconsistent_casing", "Name stored in lowercase: %r" % text,
            "Re-cased to Title Case for display", INFO, field, raw))

    display = " ".join(_titlecase_token(t) for t in text.split(" "))
    tokens = [t for t in re.sub(r"[^a-z ]", " ", text.lower()).split(" ") if t]
    key = " ".join(tokens)
    initials_only = any(len(t) == 1 for t in tokens)
    if initials_only:
        problems.append(problem(
            "abbreviated_name",
            "Name uses an initial rather than a full given name: %r" % text,
            "Kept as-is; matching falls back to email/phone and prefers the "
            "fully spelled variant when merging",
            WARN, field, raw))

    return Norm(display, problems, {"key": key, "tokens": tokens, "initials_only": initials_only})


def names_compatible(tokens_a, tokens_b):
    """True when two token lists could name the same human.

    Handles the "R. Verma" vs "Rohit Verma" case: a single-letter token matches
    any token that starts with that letter. Deliberately conservative - it is
    only ever used to *propose* a merge, never to force one.
    """
    if not tokens_a or not tokens_b:
        return False
    if len(tokens_a) != len(tokens_b):
        return False
    for a, b in zip(tokens_a, tokens_b):
        if a == b:
            continue
        if len(a) == 1 and b.startswith(a):
            continue
        if len(b) == 1 and a.startswith(b):
            continue
        return False
    return True


# ---------------------------------------------------------------------------
# phone
# ---------------------------------------------------------------------------

def normalize_phone(raw, field="phone"):
    """Reduce any Indian mobile spelling to a bare 10-digit national number.

    Handles: +919000000254 / 919000000254 / 09000000287 / +91-9000000131 /
    00919000000131 / spaces and dashes anywhere.
    """
    text = squash(raw)
    problems = []
    if not text:
        return Norm(None, [problem("missing_value", "Phone is empty",
                                   "Left NULL; identity resolution falls back to email",
                                   WARN, field, raw)])

    digits = re.sub(r"\D", "", text)
    if not digits:
        return Norm(None, [problem("invalid_format", "Phone %r contains no digits" % text,
                                   "Left NULL", ERROR, field, raw)])

    if digits.startswith("00"):
        digits = digits[2:]
    while len(digits) > 10 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]

    if len(digits) != 10:
        return Norm(None, [problem(
            "invalid_format",
            "Phone %r reduces to %d digits, expected 10" % (text, len(digits)),
            "Left NULL and flagged; not used as a match key", ERROR, field, raw)])

    if digits[0] not in "6789":
        problems.append(problem(
            "suspicious_value",
            "Phone %s does not start with 6-9, which no Indian mobile does" % digits,
            "Kept as a match key but flagged", WARN, field, raw))

    if text != digits:
        problems.append(problem(
            "inconsistent_format",
            "Phone written as %r (country code / trunk zero / punctuation)" % text,
            "Normalised to the bare 10-digit form %s" % digits, INFO, field, raw))

    return Norm(digits, problems)


# ---------------------------------------------------------------------------
# email
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# Prefixes seen on re-registered / duplicate accounts in source1.
_ALIAS_PREFIXES = ("alt.", "alt_", "new.", "dup.")


def normalize_email(raw, field="email"):
    text = squash(raw)
    problems = []
    if not text:
        return Norm(None, [problem("missing_value", "Email is empty",
                                   "Left NULL; identity resolution falls back to phone",
                                   WARN, field, raw)])

    if " " in text:
        text = text.replace(" ", "")
        problems.append(problem("invalid_format", "Email contained spaces: %r" % raw,
                                "Spaces removed", WARN, field, raw))

    lowered = text.lower()
    if text != lowered:
        problems.append(problem(
            "inconsistent_casing", "Email stored with uppercase characters: %r" % text,
            "Lowercased - the local part is case sensitive per RFC 5321 but no "
            "real provider treats it that way, and leaving it would split one "
            "person into two records", INFO, field, raw))

    if not _EMAIL_RE.match(lowered):
        return Norm(None, [problem(
            "invalid_format", "Email %r is not a valid address" % raw,
            "Left NULL and flagged; not used as a match key", ERROR, field, raw)])

    local, _, domain = lowered.partition("@")
    alias_of = None
    for prefix in _ALIAS_PREFIXES:
        if local.startswith(prefix):
            alias_of = local[len(prefix):] + "@" + domain
            problems.append(problem(
                "alias_email",
                "Email %s looks like a secondary account for %s" % (lowered, alias_of),
                "NOT auto-merged on this hunch; the two rows are merged only "
                "because their phone numbers match exactly",
                WARN, field, raw))
            break

    return Norm(lowered, problems, {"domain": domain, "local": local, "alias_of": alias_of})


# ---------------------------------------------------------------------------
# city
# ---------------------------------------------------------------------------

def normalize_city(raw, field="city"):
    text = squash(raw)
    problems = []
    if not text:
        return Norm(None, [problem("missing_value", "City is empty", "Left NULL", WARN, field, raw)])

    raw_str = str(raw)
    if raw_str != raw_str.strip():
        problems.append(problem(
            "trailing_whitespace", "City %r has leading/trailing whitespace" % raw_str,
            "Trimmed", INFO, field, raw))

    key = text.lower()
    canon = config.CITY_CANON.get(key)
    if canon is None:
        problems.append(problem(
            "unmapped_value", "City %r is not in the canonical city list" % text,
            "Kept the Title Cased raw value so nothing is lost; add it to "
            "config.CITY_CANON to fold it into a known city",
            WARN, field, raw))
        canon = " ".join(_titlecase_token(t) for t in text.split(" "))
    elif text != canon:
        problems.append(problem(
            "inconsistent_value",
            "City written as %r, which is a casing/alias variant of %s" % (text, canon),
            "Mapped to the canonical name %s" % canon, INFO, field, raw))

    region_only = key in config.CITY_REGION_ONLY
    if region_only:
        problems.append(problem(
            "precision_loss",
            "%r names a metro region, not a city; Gurugram and Noida are also "
            "inside it" % text,
            "Recorded city=%s with city_is_region_guess=1 so the record is not "
            "presented as more precise than the source was" % canon,
            WARN, field, raw))

    return Norm(canon, problems, {
        "region": config.CITY_REGION.get(canon),
        "region_only": region_only,
    })


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DASH_RE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")
_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_TEXT_RE = re.compile(r"^(\d{1,2})[ \-]([A-Za-z]{3,9})[ \-](\d{4})$")

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
_MONTHS.update({m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)})


def _ingest_date():
    return datetime.strptime(config.INGEST_DATE, "%Y-%m-%d").date()


def normalize_date(raw, field="applied_date"):
    """Parse the four date spellings present in source1 into ISO-8601.

    The only genuinely undecidable case is a slash/dash date where both leading
    numbers are <= 12. The convention applied is:

        dd-mm-yyyy   for dash dates
        mm/dd/yyyy   for slash dates

    That is not a guess. Across source1 every dash date with a component > 12
    has it in position 1 (24-07-2026, 21-08-2026 ...) and every slash date with
    a component > 12 has it in position 2 (07/13/2026, 08/19/2026 ...). The two
    separators therefore carry two different conventions consistently, which is
    exactly what you get when one system exports Indian format and another
    exports US format. tests/test_normalize.py asserts this property against the
    real file, so if a future file breaks the assumption the suite fails instead
    of quietly mis-parsing dates.
    """
    text = squash(raw)
    problems = []
    if not text:
        return Norm(None, [problem("missing_value", "Date is empty", "Left NULL", WARN, field, raw)])

    ambiguous = False
    iso_m = _ISO_RE.match(text)
    dash_m = _DASH_RE.match(text)
    slash_m = _SLASH_RE.match(text)
    text_m = _TEXT_RE.match(text)

    if iso_m:
        assumed = "yyyy-mm-dd"
        y, mo, d = (int(g) for g in iso_m.groups())

    elif dash_m or slash_m:
        # Two numbers then a year. Which is the day is decided by the value
        # when one of them is > 12, and by the separator convention otherwise.
        assumed = "dd-mm-yyyy" if dash_m else "mm/dd/yyyy"
        a, b, y = (int(g) for g in (dash_m or slash_m).groups())
        if a > 12 and b <= 12:
            d, mo = a, b
        elif b > 12 and a <= 12:
            mo, d = a, b
        elif dash_m:
            d, mo, ambiguous = a, b, True     # dash -> day first
        else:
            mo, d, ambiguous = a, b, True     # slash -> month first

    elif text_m:
        assumed = "d Mon yyyy"
        d, y = int(text_m.group(1)), int(text_m.group(3))
        mo = _MONTHS.get(text_m.group(2).lower())
        if mo is None:
            return Norm(None, [problem(
                "invalid_format", "Unknown month name in date %r" % text,
                "Left NULL", ERROR, field, raw)])

    else:
        return Norm(None, [problem(
            "invalid_format", "Date %r matches none of the four known formats" % text,
            "Left NULL and flagged", ERROR, field, raw)])

    try:
        parsed = date(y, mo, d)
    except ValueError as exc:
        return Norm(None, [problem(
            "invalid_value", "Date %r is not a real calendar date (%s)" % (text, exc),
            "Left NULL", ERROR, field, raw)])

    if assumed and text != parsed.isoformat():
        problems.append(problem(
            "inconsistent_format",
            "Date written as %r (%s) in a column that also contains ISO dates" % (text, assumed),
            "Parsed with the %s convention and stored as %s" % (assumed, parsed.isoformat()),
            INFO, field, raw))

    if ambiguous:
        problems.append(problem(
            "ambiguous_date",
            "Date %r is undecidable from the value alone - both components are <= 12" % text,
            "Applied the separator convention (%s) proven across the rest of the "
            "column, stored %s, and set applied_date_ambiguous=1 so downstream "
            "users know not to lean on the exact day" % (assumed, parsed.isoformat()),
            WARN, field, raw))

    today = _ingest_date()
    if parsed > today:
        problems.append(problem(
            "future_date",
            "Applied date %s is after the ingest date %s - an application "
            "cannot be submitted in the future" % (parsed.isoformat(), today.isoformat()),
            "Kept the value (rejecting it would lose the row) and set "
            "applied_date_is_future=1; most likely a dd-mm vs mm-dd export bug "
            "at source",
            WARN, field, raw))

    return Norm(parsed.isoformat(), problems, {
        "ambiguous": ambiguous,
        "is_future": parsed > today,
        "format": assumed,
    })


# ---------------------------------------------------------------------------
# money
# ---------------------------------------------------------------------------

def normalize_ctc(raw, field="ctc_annual_inr"):
    """Resolve the two-units-in-one-column CTC problem.

    source1 "Current CTC" holds both absolute rupees (417964) and lakhs per
    annum (4.2) in the same column. Anything under config.CTC_LAKH_THRESHOLD is
    read as lakhs. This is safe because the two ranges do not overlap: the
    smallest absolute value in the file is 327287 and the largest lakh value is
    11.9, so there is no value where the two readings compete.
    """
    text = squash(raw)
    problems = []
    if not text:
        return Norm(None, [problem("missing_value", "CTC is empty", "Left NULL", WARN, field, raw)])

    cleaned = text.replace(",", "").replace("₹", "").replace("INR", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return Norm(None, [problem(
            "invalid_format", "CTC %r is not numeric" % text, "Left NULL", ERROR, field, raw)])

    unit = "absolute_inr"
    if value < config.CTC_LAKH_THRESHOLD:
        value = value * 100_000
        unit = "lakh_per_annum"
        problems.append(problem(
            "mixed_units",
            "CTC %r is expressed in lakhs while other rows in the same column "
            "are in absolute rupees" % text,
            "Multiplied by 1e5 -> %d INR; unit recorded in ctc_unit_detected "
            "so the conversion is auditable" % int(value),
            WARN, field, raw))

    if not (config.CTC_MIN_PLAUSIBLE <= value <= config.CTC_MAX_PLAUSIBLE):
        problems.append(problem(
            "out_of_range",
            "CTC %d INR is outside the plausible band %d-%d" % (
                int(value), config.CTC_MIN_PLAUSIBLE, config.CTC_MAX_PLAUSIBLE),
            "Kept but flagged with ctc_out_of_range=1", WARN, field, raw))

    return Norm(round(value, 2), problems, {
        "unit": unit,
        "out_of_range": not (config.CTC_MIN_PLAUSIBLE <= value <= config.CTC_MAX_PLAUSIBLE),
    })


_RATE_RE = re.compile(
    r"^(?P<num>\d+(?:\.\d+)?)\s*(?P<mult>k|l|lac|lakh)?\s*(?:/|per\s+)\s*(?P<per>hr|hour|hourly|month|mo|monthly|day|daily)$",
    re.I)

_MULT = {None: 1, "k": 1_000, "l": 100_000, "lac": 100_000, "lakh": 100_000}


def normalize_rate(raw, field="rate"):
    """Turn "1415/hr" and "15k/month" into one comparable pair of numbers.

    Both an hourly and a monthly figure are stored. Whichever one the source
    did not state is derived using config.BILLABLE_HOURS_PER_MONTH, and
    rate_basis_raw records which one was actually given so nobody mistakes the
    derived number for a quoted price.
    """
    text = squash(raw)
    problems = []
    if not text:
        return Norm(None, [problem("missing_value", "Rate is empty", "Left NULL", WARN, field, raw)])

    m = _RATE_RE.match(text.replace(" ", ""))
    if not m:
        m = _RATE_RE.match(text)
    if not m:
        return Norm(None, [problem(
            "invalid_format",
            "Rate %r matches neither the '<n>/hr' nor the '<n>k/month' pattern" % text,
            "Left NULL and flagged", ERROR, field, raw)])

    amount = float(m.group("num")) * _MULT[(m.group("mult") or "").lower() or None]
    per = m.group("per").lower()
    hours = config.BILLABLE_HOURS_PER_MONTH

    if per in ("hr", "hour", "hourly"):
        hourly, monthly, basis = amount, amount * hours, "hourly"
    elif per in ("day", "daily"):
        hourly, monthly, basis = amount / 8.0, amount * 20, "daily"
    else:
        hourly, monthly, basis = amount / float(hours), amount, "monthly"

    problems.append(problem(
        "mixed_units",
        "Rate %r is quoted per %s while other rows in the same column are "
        "quoted on a different basis" % (text, basis),
        "Stored both rate_hourly_inr=%.2f and rate_monthly_inr=%.2f, deriving "
        "the missing one at %d billable hours/month; rate_basis_raw=%s keeps "
        "the quoted basis visible" % (hourly, monthly, hours, basis),
        INFO, field, raw))

    out_of_range = not (config.RATE_MIN_PLAUSIBLE_MONTHLY <= monthly <= config.RATE_MAX_PLAUSIBLE_MONTHLY)
    if out_of_range:
        problems.append(problem(
            "out_of_range",
            "Rate %r is %d INR/month equivalent, outside the plausible band "
            "%d-%d - a 1483/hr contractor bills ~2.4L/month, which is ~16x the "
            "15k/month quoted by another worker with the same skills" % (
                text, int(monthly), config.RATE_MIN_PLAUSIBLE_MONTHLY,
                config.RATE_MAX_PLAUSIBLE_MONTHLY),
            "Kept but flagged with rate_out_of_range=1; the hourly-vs-monthly "
            "mix in this column means at least some of these are unit errors "
            "at source rather than genuine premium rates",
            WARN, field, raw))

    return Norm(round(hourly, 2), problems, {
        "hourly": round(hourly, 2),
        "monthly": round(monthly, 2),
        "basis": basis,
        "out_of_range": out_of_range,
    })


# ---------------------------------------------------------------------------
# small scalars
# ---------------------------------------------------------------------------

def normalize_float(raw, field, lo=None, hi=None):
    text = squash(raw)
    problems = []
    if not text:
        return Norm(None, [problem("missing_value", "%s is empty" % field, "Left NULL", WARN, field, raw)])
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        return Norm(None, [problem("invalid_format", "%s %r is not numeric" % (field, text),
                                   "Left NULL", ERROR, field, raw)])
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        problems.append(problem(
            "out_of_range", "%s %s is outside the plausible band %s-%s" % (field, value, lo, hi),
            "Kept but flagged", WARN, field, raw))
    return Norm(value, problems)


def normalize_int(raw, field, lo=None, hi=None):
    result = normalize_float(raw, field, lo, hi)
    if result.value is None:
        return result
    return Norm(int(round(result.value)), result.problems, result.extra)


def normalize_bool(raw, field="verified"):
    text = squash(raw).lower()
    if not text:
        return Norm(None, [problem("missing_value", "%s is empty" % field, "Left NULL", WARN, field, raw)])
    if text in config.TRUTHY:
        value = True
    elif text in config.FALSY:
        value = False
    else:
        return Norm(None, [problem(
            "unmapped_value", "%s %r is not a recognised boolean" % (field, text),
            "Left NULL and flagged", ERROR, field, raw)])
    canonical = "Y" if value else "N"
    problems = []
    if squash(raw) != canonical:
        problems.append(problem(
            "inconsistent_value",
            "Boolean %s written as %r; the same column also uses Y/N/yes/No/Yes" % (field, squash(raw)),
            "Stored as integer %d" % int(value), INFO, field, raw))
    return Norm(value, problems)


def normalize_status(raw, field="gig_status"):
    text = squash(raw)
    if not text:
        return Norm(None, [problem("missing_value", "Status is empty", "Left NULL", WARN, field, raw)])
    canon = config.GIG_STATUS_CANON.get(text.lower())
    if canon is None:
        return Norm(None, [problem(
            "unmapped_value", "Status %r is not one of active/inactive/paused" % text,
            "Left NULL and flagged", ERROR, field, raw)])
    problems = []
    if text != canon:
        problems.append(problem(
            "inconsistent_casing",
            "Status written as %r; the same column also holds %s variants" % (text, canon),
            "Lowercased to the %s enum" % canon, INFO, field, raw))
    return Norm(canon, problems)


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------

def normalize_skills(raw, field="skills"):
    """Split a comma list into canonical skill names, de-duplicated."""
    text = squash(raw)
    problems = []
    if not text:
        return Norm([], [problem("missing_value", "Skill list is empty", "Stored no skills", WARN, field, raw)])

    seen, out, unmapped, recased = [], [], [], []
    for token in text.split(","):
        item = squash(token)
        if not item:
            continue
        canon = config.SKILL_CANON.get(item.lower())
        if canon is None:
            canon = item
            unmapped.append(item)
        elif item != canon:
            recased.append(item)
        if canon.lower() not in seen:
            seen.append(canon.lower())
            out.append(canon)

    if recased:
        problems.append(problem(
            "inconsistent_casing",
            "Skills %s use a different casing than the canonical spelling" % ", ".join(sorted(set(recased))[:6]),
            "Mapped through config.SKILL_CANON so 'rest apis' and 'REST APIs' "
            "become one skill row instead of two", INFO, field, raw))
    if unmapped:
        problems.append(problem(
            "unmapped_value",
            "Skills %s are not in the canonical skill list" % ", ".join(sorted(set(unmapped))[:6]),
            "Inserted verbatim so nothing is lost; add them to "
            "config.SKILL_CANON to fold them into an existing skill", WARN, field, raw))

    return Norm(out, problems)
