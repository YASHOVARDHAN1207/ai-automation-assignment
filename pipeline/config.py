"""Central configuration: paths, business rules and canonical vocabularies.

Every "magic" decision the pipeline makes lives here so it can be reviewed in
one place instead of being buried inside the transform code.
"""
import os

# --- paths -----------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DB_DIR = os.path.join(ROOT, "db")
REPORTS_DIR = os.path.join(ROOT, "reports")
SCHEMA_PATH = os.path.join(DB_DIR, "schema.sql")
AUDIO_SCHEMA_PATH = os.path.join(DB_DIR, "schema_audio.sql")
AUTOMATION_SCHEMA_PATH = os.path.join(DB_DIR, "schema_automation.sql")
DB_PATH = os.path.join(DB_DIR, "consultbae.db")
UPLOAD_DIR = os.path.join(ROOT, "app", "uploads")

SOURCES = {
    "naukri": os.path.join(DATA_DIR, "source1_naukri_applicants.csv"),
    "gig": os.path.join(DATA_DIR, "source2_gig_workers.csv"),
    "cbnexus": os.path.join(DATA_DIR, "source3_cbnexus_contacts.csv"),
}

# Human-readable labels used in reports. "audio_app" is not a CSV: it is the
# app_contacts table, which the Task 3 app writes to and the pipeline reads as a
# fourth source. See db/schema_audio.sql for why the app cannot write into
# `people` directly.
SOURCE_LABELS = {
    "naukri": "source1_naukri_applicants.csv",
    "gig": "source2_gig_workers.csv",
    "cbnexus": "source3_cbnexus_contacts.csv",
    "audio_app": "app_contacts (audio app)",
}

# --- business rules --------------------------------------------------------

# "Today" for the purpose of validating applied dates. Overridable so the
# future-date check is deterministic and does not silently change meaning when
# the pipeline is re-run months later.
INGEST_DATE = os.environ.get("CONSULTBAE_INGEST_DATE", "2026-08-18")

# CTC arrives in two units in the same column (see docs/DATA_ISSUES.md #4).
# Anything below this threshold is read as "lakhs per annum", anything above
# as "absolute rupees per annum".
CTC_LAKH_THRESHOLD = 1000.0
CTC_MIN_PLAUSIBLE = 50_000        # < 50k p.a. is not a real salary
CTC_MAX_PLAUSIBLE = 100_000_000   # > 10 Cr p.a. is not one of these candidates

# Gig rates arrive as either "1415/hr" or "15k/month". To compare them we need
# one assumption, stated loudly rather than hidden:
BILLABLE_HOURS_PER_MONTH = 160    # 20 working days x 8 hours

# Rate sanity band, in monthly-equivalent INR.
RATE_MIN_PLAUSIBLE_MONTHLY = 8_000
RATE_MAX_PLAUSIBLE_MONTHLY = 400_000

# Experience sanity band, in years.
EXPERIENCE_MIN = 0.0
EXPERIENCE_MAX = 50.0

# --- canonical vocabularies ------------------------------------------------

# City canonicalisation. Note the deliberate distinction: "Delhi NCR" is a
# metro *region*, not a city, and Gurugram / Noida are inside it. Collapsing
# everything NCR-ish into "Delhi" would destroy real signal, so region is
# tracked separately (see normalize.canonical_city).
CITY_CANON = {
    "bengaluru": "Bengaluru",
    "bangalore": "Bengaluru",
    "blr": "Bengaluru",
    "gurgaon": "Gurugram",
    "gurugram": "Gurugram",
    "ggn": "Gurugram",
    "noida": "Noida",
    "new delhi": "Delhi",
    "delhi": "Delhi",
    "delhi ncr": "Delhi",
    "ncr": "Delhi",
    "pune": "Pune",
    "puna": "Pune",
}

# Cities whose raw value was a region label rather than a city name. Kept so
# we never claim more precision than the source actually gave us.
CITY_REGION_ONLY = {"delhi ncr", "ncr"}

CITY_REGION = {
    "Delhi": "Delhi NCR",
    "Gurugram": "Delhi NCR",
    "Noida": "Delhi NCR",
    "Bengaluru": "Bengaluru Urban",
    "Pune": "Pune Metro",
}

# Skill canonicalisation: source1 uses Title Case, source2 lowercases
# everything, so "REST APIs" and "rest apis" are the same skill.
SKILL_CANON = {
    "n8n": "n8n",
    "langchain": "LangChain",
    "lang chain": "LangChain",
    "rest apis": "REST APIs",
    "rest api": "REST APIs",
    "restapi": "REST APIs",
    "mongodb": "MongoDB",
    "mongo db": "MongoDB",
    "mongo": "MongoDB",
    "sql": "SQL",
    "mysql": "MySQL",
    "my sql": "MySQL",
    "docker": "Docker",
    "zapier": "Zapier",
    "javascript": "JavaScript",
    "js": "JavaScript",
    "react": "React",
    "reactjs": "React",
    "react.js": "React",
    "python": "Python",
    "selenium": "Selenium",
    "web scraping": "Web Scraping",
    "webscraping": "Web Scraping",
    "scraping": "Web Scraping",
    "fastapi": "FastAPI",
    "fast api": "FastAPI",
    "pandas": "Pandas",
}

GIG_STATUS_CANON = {
    "active": "active",
    "inactive": "inactive",
    "in-active": "inactive",
    "paused": "paused",
    "on hold": "paused",
    "on-hold": "paused",
}

TRUTHY = {"y", "yes", "true", "1", "t"}
FALSY = {"n", "no", "false", "0", "f"}

# Trust order used to break ties when two sources disagree on a shared field.
# Rationale (docs/MATCHING.md): source1 is a self-submitted application form,
# so the candidate typed it themselves and it is the most deliberate. source3
# is an internal CRM maintained by staff. source2 is an ops sheet where the
# location field is the least curated of the three.
# audio_app ranks last for shared fields: a worker typing their name into a
# phone form in a hurry is less reliable than an application they filled in
# deliberately. It still wins for anything no other source has.
FIELD_SOURCE_PRIORITY = ["naukri", "cbnexus", "gig", "audio_app"]
