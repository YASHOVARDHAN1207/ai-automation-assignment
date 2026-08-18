-- ---------------------------------------------------------------------------
-- Task 2 tables: what the n8n flows write back.
--
-- Additive and never dropped, for the same reason as db/schema_audio.sql:
-- `people` is derived and rebuilt from source on every pipeline run, so
-- anything an automation writes into it would be deleted by the next
-- `make pipeline`.
--
-- Note the key choice. These tables are keyed on `person_key` - the person's
-- email, or their phone when they have no email - NOT on person_id.
--
-- person_id is deterministic for a given set of inputs, but it is NOT stable
-- when the inputs change: people are sorted by identity before ids are handed
-- out, so inserting one new contact shifts the ids of everyone that sorts after
-- them. Measured: adding a single app contact moved Arjun Mehta from #56 to #57.
-- An LLM-assigned skill category keyed on person_id would therefore attach
-- itself to the wrong human after a rebuild - silently. Email and phone are the
-- durable identity, so they are the key, and person_id is a convenience column
-- refreshed by pipeline.load.relink_person_references after every load.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS person_skill_categories (
    category_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_key      TEXT NOT NULL UNIQUE,   -- email, else phone: the durable id
    person_id       INTEGER,                -- soft link, refreshed after a rebuild
    category        TEXT NOT NULL,
    confidence      REAL,
    rationale       TEXT,
    key_skills      TEXT,                   -- the skills the model leaned on
    model           TEXT,
    tagged_by       TEXT,                   -- which flow wrote this
    tagged_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_categories_person ON person_skill_categories(person_id);
CREATE INDEX IF NOT EXISTS idx_categories_category ON person_skill_categories(category);

-- An audit trail of everything the automations did. Without this, "the flow ran"
-- is an unverifiable claim, and a duplicate alert that fired at 3am is
-- unreconstructable.
CREATE TABLE IF NOT EXISTS automation_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    flow        TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    summary     TEXT,
    payload     TEXT                        -- JSON
);

CREATE INDEX IF NOT EXISTS idx_events_flow ON automation_events(flow, created_at);

DROP VIEW IF EXISTS v_people_categorised;
CREATE VIEW v_people_categorised AS
SELECT p.person_id, p.full_name, p.email, p.phone, p.city, p.gig_status,
       c.category, c.confidence, c.rationale, c.key_skills, c.model, c.tagged_at,
       (SELECT GROUP_CONCAT(s.skill_name, ', ')
          FROM person_skills ps JOIN skills s ON s.skill_id = ps.skill_id
         WHERE ps.person_id = p.person_id) AS skills
  FROM people p
  LEFT JOIN person_skill_categories c
         ON c.person_key = COALESCE(p.email, p.phone);
