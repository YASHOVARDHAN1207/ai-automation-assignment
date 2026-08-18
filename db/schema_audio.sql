-- ---------------------------------------------------------------------------
-- Task 3 tables, applied on top of schema.sql.
--
-- Why these live in a separate file with IF NOT EXISTS, instead of being added
-- to schema.sql:
--
--   `people` is a DERIVED table. schema.sql drops and rebuilds it from the
--   three CSVs every time the pipeline runs. Anything the app wrote into a
--   derived table would be deleted by the next `make pipeline`, silently.
--
--   So the app writes to tables the pipeline never drops:
--
--     app_contacts       a durable SOURCE, treated by the pipeline as a fourth
--                        input file alongside the three CSVs. A worker who
--                        submits audio without already existing in the CRM is
--                        recorded here, and every rebuild re-materialises them
--                        into `people` through the same matching passes.
--     audio_submissions  the submissions themselves, with the extracted audio
--                        properties. Never derived, never dropped.
--
--   audio_submissions.person_id is deliberately NOT a foreign key. With
--   PRAGMA foreign_keys=ON, SQLite's DROP TABLE performs an implicit
--   DELETE FROM, so a child row referencing `people` would make the next
--   pipeline rebuild fail outright. Instead `phone` is the durable join key and
--   the pipeline re-points person_id after every rebuild (see
--   pipeline.load.relink_audio_submissions).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app_contacts (
    contact_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    phone         TEXT NOT NULL UNIQUE,     -- normalised 10-digit
    phone_raw     TEXT,
    city          TEXT,
    origin        TEXT NOT NULL DEFAULT 'audio_app'
);

CREATE TABLE IF NOT EXISTS audio_submissions (
    submission_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at         TEXT NOT NULL,
    submitted_name       TEXT NOT NULL,
    submitted_phone_raw  TEXT NOT NULL,
    phone                TEXT,              -- normalised; the join key to people
    person_id            INTEGER,           -- soft link, refreshed after a rebuild
    person_link_method   TEXT,              -- matched_existing | created_contact | unlinked
    capture_mode         TEXT,              -- browser_recording | file_upload
    original_filename    TEXT,
    stored_filename      TEXT NOT NULL,
    mime_type            TEXT,
    sha256               TEXT,
    duplicate_of         INTEGER,           -- an earlier submission with the same bytes

    -- extracted audio properties (Task 3 requirement)
    duration_seconds       REAL,
    sample_rate_hz         INTEGER,
    sample_rate_khz        REAL,
    channels               INTEGER,
    bit_depth              INTEGER,
    codec                  TEXT,
    container_format       TEXT,
    bitrate_kbps           REAL,
    pcm_bitrate_kbps       REAL,

    -- loudness, three ways: clipping, level, and perceived
    peak_dbfs              REAL,
    rms_dbfs               REAL,
    crest_factor_db        REAL,
    loudness_lufs          REAL,
    loudness_range_lu      REAL,

    -- noise / quality estimate (bonus)
    noise_floor_dbfs       REAL,
    estimated_snr_db       REAL,
    snr_note               TEXT,
    frame_dynamic_range_db REAL,
    speech_ratio_pct       REAL,
    silence_pct            REAL,
    clipping_pct           REAL,
    dc_offset              REAL,
    quality_score          INTEGER,
    quality_label          TEXT,
    quality_reasons        TEXT,            -- JSON array

    file_size_bytes        INTEGER,
    analysis_ok            INTEGER NOT NULL DEFAULT 0,
    analysis_note          TEXT,
    analysis_backend       TEXT
);

CREATE INDEX IF NOT EXISTS idx_audio_person ON audio_submissions(person_id);
CREATE INDEX IF NOT EXISTS idx_audio_phone ON audio_submissions(phone);
CREATE INDEX IF NOT EXISTS idx_audio_sha ON audio_submissions(sha256);

-- The list view joins through phone rather than person_id, so it stays correct
-- even in the window between a pipeline rebuild and the relink.
DROP VIEW IF EXISTS v_submissions;
CREATE VIEW v_submissions AS
SELECT s.*,
       p.person_id     AS resolved_person_id,
       p.full_name     AS person_name,
       p.city          AS person_city,
       p.gig_status    AS person_gig_status,
       p.sources       AS person_sources
  FROM audio_submissions s
  LEFT JOIN people p ON p.phone = s.phone;
