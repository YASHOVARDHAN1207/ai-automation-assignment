-- ---------------------------------------------------------------------------
-- ConsultBae unified people database (SQLite)
--
-- Shape of the design:
--
--   people          one row per human. The "golden record".
--   source_records  one row per physical CSV line, with the original values
--                   kept verbatim as JSON and a FK back to the person it fed.
--                   Nothing is ever thrown away, so any merge can be undone
--                   and any number can be traced to the line it came from.
--   skills          canonical skill vocabulary (source1 Title Case and source2
--                   lowercase collapse into one row here).
--   person_skills   many-to-many, with the file each claim came from.
--   data_issues     the Task 4 report, as queryable data rather than prose.
--   field_conflicts every case where two files disagreed about the same field
--                   for the same person, and which value won and why.
--   merge_review    pairs the matcher deliberately refused to merge.
--   load_runs       one row per pipeline execution.
--
-- SQLite was chosen over MySQL/Postgres because the whole deliverable has to
-- run on a reviewer's laptop with `python -m pipeline.run` and no service to
-- start. The schema itself is portable: no SQLite-only types are used.
-- ---------------------------------------------------------------------------

PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS person_skills;
DROP TABLE IF EXISTS skills;
DROP TABLE IF EXISTS field_conflicts;
DROP TABLE IF EXISTS merge_review;
DROP TABLE IF EXISTS data_issues;
DROP TABLE IF EXISTS source_records;
DROP TABLE IF EXISTS people;
DROP TABLE IF EXISTS load_runs;

CREATE TABLE load_runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    ingest_date       TEXT NOT NULL,        -- "today" used for future-date checks
    source_rows_read  INTEGER,
    source_rows_used  INTEGER,
    people_created    INTEGER,
    issues_logged     INTEGER,
    pipeline_version  TEXT
);

CREATE TABLE people (
    person_id               INTEGER PRIMARY KEY,
    full_name               TEXT NOT NULL,
    email                   TEXT,
    phone                   TEXT,
    city                    TEXT,
    city_region             TEXT,
    city_is_region_guess    INTEGER NOT NULL DEFAULT 0,

    -- from source1 (applicant funnel)
    experience_years        REAL,
    ctc_annual_inr          REAL,
    ctc_unit_detected       TEXT,            -- absolute_inr | lakh_per_annum
    ctc_out_of_range        INTEGER NOT NULL DEFAULT 0,
    applied_date            TEXT,            -- ISO-8601
    applied_date_ambiguous  INTEGER NOT NULL DEFAULT 0,
    applied_date_is_future  INTEGER NOT NULL DEFAULT 0,

    -- from source2 (gig bench)
    rate_hourly_inr         REAL,
    rate_monthly_inr        REAL,
    rate_basis_raw          TEXT,            -- hourly | monthly | daily (as quoted)
    rate_out_of_range       INTEGER NOT NULL DEFAULT 0,
    gig_status              TEXT,            -- active | inactive | paused

    -- from source3 (CBNexus CRM)
    is_verified             INTEGER,
    projects_completed      INTEGER,

    -- provenance
    sources                 TEXT NOT NULL,
    source_count            INTEGER NOT NULL,
    record_count            INTEGER NOT NULL,
    first_seen_source       TEXT,
    match_methods           TEXT NOT NULL,
    match_confidence        REAL NOT NULL,
    load_run_id             INTEGER REFERENCES load_runs(run_id),
    created_at              TEXT NOT NULL,

    CHECK (gig_status IS NULL OR gig_status IN ('active', 'inactive', 'paused')),
    CHECK (match_confidence > 0 AND match_confidence <= 1)
);

-- A phone or email may legitimately be NULL, but when present it must identify
-- exactly one person: that is the whole promise of the merge step.
CREATE UNIQUE INDEX idx_people_email ON people(email) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX idx_people_phone ON people(phone) WHERE phone IS NOT NULL;
CREATE INDEX idx_people_city ON people(city);
CREATE INDEX idx_people_status ON people(gig_status);

CREATE TABLE source_records (
    source_record_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name       TEXT NOT NULL,
    source_row_number INTEGER NOT NULL,      -- 1-based line number in the CSV
    person_id         INTEGER REFERENCES people(person_id),
    was_used          INTEGER NOT NULL,      -- 0 = rejected (see data_issues)
    repairs           TEXT,                  -- structural fixes applied
    raw_json          TEXT NOT NULL,         -- the row exactly as it arrived
    load_run_id       INTEGER REFERENCES load_runs(run_id),
    UNIQUE (source_name, source_row_number, load_run_id)
);

CREATE INDEX idx_source_records_person ON source_records(person_id);

CREATE TABLE skills (
    skill_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name  TEXT NOT NULL UNIQUE
);

CREATE TABLE person_skills (
    person_id  INTEGER NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
    skill_id   INTEGER NOT NULL REFERENCES skills(skill_id),
    sources    TEXT NOT NULL,                -- which file(s) claimed this skill
    PRIMARY KEY (person_id, skill_id)
);

CREATE TABLE data_issues (
    issue_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    load_run_id       INTEGER REFERENCES load_runs(run_id),
    severity          TEXT NOT NULL,         -- error | warn | info
    category          TEXT NOT NULL,
    source_name       TEXT,
    source_row_number INTEGER,
    field             TEXT,
    raw_value         TEXT,
    entity            TEXT,                  -- who the row was about, if known
    description       TEXT NOT NULL,
    action_taken      TEXT NOT NULL,
    CHECK (severity IN ('error', 'warn', 'info'))
);

CREATE INDEX idx_issues_category ON data_issues(category);
CREATE INDEX idx_issues_severity ON data_issues(severity);

CREATE TABLE field_conflicts (
    conflict_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    load_run_id      INTEGER REFERENCES load_runs(run_id),
    person_id        INTEGER REFERENCES people(person_id),
    field            TEXT NOT NULL,
    chosen_value     TEXT,
    chosen_source    TEXT,
    rejected_value   TEXT,
    rejected_source  TEXT,
    rule             TEXT NOT NULL           -- why the winner won
);

CREATE TABLE merge_review (
    review_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    load_run_id   INTEGER REFERENCES load_runs(run_id),
    reason        TEXT NOT NULL,
    name_key      TEXT,
    city          TEXT,
    cluster_root  TEXT,
    cluster_size  INTEGER,
    score         REAL,
    detail        TEXT NOT NULL,
    resolved      INTEGER NOT NULL DEFAULT 0
);

-- Convenience view: one flat row per person with their skills inlined.
DROP VIEW IF EXISTS v_people_full;
CREATE VIEW v_people_full AS
SELECT p.*,
       (SELECT GROUP_CONCAT(s.skill_name, ', ')
          FROM person_skills ps JOIN skills s ON s.skill_id = ps.skill_id
         WHERE ps.person_id = p.person_id) AS skills
  FROM people p;
