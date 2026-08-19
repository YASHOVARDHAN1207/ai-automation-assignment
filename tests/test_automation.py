"""Tests for the endpoints the n8n flows depend on.

The flows themselves live in automation/n8n/*.json and are exercised in the video.
What is tested here is the contract underneath them, because that is where the
correctness has to live: a Code node in n8n cannot be unit-tested, and an LLM
node will eventually return something absurd.

The two that matter most:

  test_a_bogus_category_from_the_llm_is_refused - the flow validates the model's
  answer, but so does the server, because anything can POST to that endpoint.

  test_tags_survive_a_rebuild_that_shifts_person_ids - person_id is not stable
  when the input set changes, so a tag keyed on it would silently reattach itself
  to a different human.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import flask                                    # noqa: F401
    HAVE_FLASK = True
except ImportError:                                 # pragma: no cover
    HAVE_FLASK = False

if HAVE_FLASK:
    from app import db as app_db, server
    from app.automation import SKILL_CATEGORIES
    from pipeline import run as pipeline_run


@unittest.skipUnless(HAVE_FLASK, "flask is required for the automation tests")
class AutomationTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="cb-auto-")
        self.db_path = os.path.join(self.workdir, "test.db")
        pipeline_run.run(db_path=self.db_path, verbose=False)
        app_db.ensure_schema(self.db_path)
        server.app.config.update(TESTING=True, DB_PATH=self.db_path,
                                 UPLOAD_DIR=os.path.join(self.workdir, "uploads"))
        self.client = server.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def get(self, path):
        response = self.client.get(path)
        return response.status_code, json.loads(response.data.decode("utf-8"))

    def post(self, path, payload):
        response = self.client.post(path, json=payload)
        return response.status_code, json.loads(response.data.decode("utf-8"))

    def conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


class TestUntaggedQueue(AutomationTestCase):
    def test_returns_people_with_skills_and_a_prompt_contract(self):
        status, payload = self.get("/api/people/untagged?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 5)
        self.assertGreater(payload["remaining_after_this_batch"], 0)
        self.assertEqual(payload["allowed_categories"], list(SKILL_CATEGORIES.keys()))
        self.assertTrue(payload["instructions"])
        for person in payload["people"]:
            self.assertTrue(person["skills"], "a person with no skills cannot be tagged")
            self.assertTrue(person["person_key"])

    def test_the_queue_is_idempotent_so_a_crashed_run_can_just_resume(self):
        _, before = self.get("/api/people/untagged?limit=3")
        first = before["people"][0]
        self.post("/api/people/%d/category" % first["person_id"],
                  {"category": "data", "confidence": 0.9})
        _, after = self.get("/api/people/untagged?limit=3")
        self.assertNotIn(first["person_id"], [p["person_id"] for p in after["people"]])

    def test_limit_is_clamped(self):
        _, payload = self.get("/api/people/untagged?limit=99999")
        self.assertLessEqual(payload["count"], 200)
        _, payload = self.get("/api/people/untagged?limit=not-a-number")
        self.assertEqual(payload["count"], 20)


class TestWriteBackValidation(AutomationTestCase):
    def test_a_valid_verdict_is_stored(self):
        status, payload = self.post("/api/people/1/category", {
            "category": "automation-heavy", "confidence": 0.82,
            "rationale": "n8n and Zapier dominate", "key_skills": ["n8n", "Zapier"],
            "model": "claude-sonnet-5", "tagged_by": "test"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        row = self.conn().execute(
            "SELECT * FROM person_skill_categories WHERE person_id = 1").fetchone()
        self.assertEqual(row["category"], "automation-heavy")
        self.assertEqual(row["confidence"], 0.82)
        self.assertEqual(row["key_skills"], "n8n, Zapier")
        self.assertTrue(row["person_key"])

    def test_a_bogus_category_from_the_llm_is_refused(self):
        """An LLM will eventually answer 'Automation Heavy!!' or invent a label.

        Writing that in would silently break every GROUP BY built on the column,
        so the server rejects anything outside the closed vocabulary - even though
        the flow already checks. 'The model said so' is not a schema.
        """
        for bad in ("Automation Heavy!!", "automation heavy", "AUTOMATION-HEAVY ",
                    "devops", "", None, "generalist; web-dev"):
            status, payload = self.post("/api/people/1/category",
                                        {"category": bad, "confidence": 0.9})
            if bad == "AUTOMATION-HEAVY ":
                # Case and surrounding whitespace are normalised, not rejected -
                # that is a formatting difference, not a different category.
                self.assertEqual(status, 200, "%r should normalise" % bad)
                continue
            self.assertEqual(status, 400, "%r was accepted" % bad)
            self.assertIn("allowed_categories", payload)

        self.assertEqual(self.conn().execute(
            "SELECT COUNT(*) FROM person_skill_categories "
            "WHERE category NOT IN (%s)" % ",".join("?" * len(SKILL_CATEGORIES)),
            tuple(SKILL_CATEGORIES)).fetchone()[0], 0)

    def test_confidence_must_be_a_number_between_zero_and_one(self):
        for bad in (5, -0.5, "high", 1.01):
            status, _ = self.post("/api/people/2/category",
                                  {"category": "data", "confidence": bad})
            self.assertEqual(status, 400, "%r was accepted" % bad)
        # Missing confidence is allowed: not every classifier reports one.
        status, _ = self.post("/api/people/2/category", {"category": "data"})
        self.assertEqual(status, 200)

    def test_retagging_updates_in_place_instead_of_duplicating(self):
        self.post("/api/people/3/category", {"category": "data", "confidence": 0.5})
        self.post("/api/people/3/category", {"category": "web-dev", "confidence": 0.9})
        rows = self.conn().execute(
            "SELECT * FROM person_skill_categories WHERE person_id = 3").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "web-dev")

    def test_unknown_person_is_a_404(self):
        status, _ = self.post("/api/people/999999/category", {"category": "data"})
        self.assertEqual(status, 404)

    def test_every_write_is_recorded_in_the_audit_trail(self):
        self.post("/api/people/4/category",
                  {"category": "qa-automation", "confidence": 0.7, "tagged_by": "test-flow"})
        row = self.conn().execute(
            "SELECT * FROM automation_events WHERE event_type = 'person_tagged' "
            "ORDER BY event_id DESC LIMIT 1").fetchone()
        self.assertEqual(row["flow"], "test-flow")
        self.assertIn("qa-automation", row["summary"])


class TestTagDurability(AutomationTestCase):
    def test_tags_survive_a_rebuild_that_shifts_person_ids(self):
        """person_id is deterministic for one input set, not stable across two.

        Adding a contact whose phone sorts early pushes everyone after it down by
        one. A tag keyed on person_id would then describe a different human. This
        test adds such a contact and asserts the tag stayed with its person.
        """
        # Tag the phone-only people specifically: they sort after everyone with
        # an email, so they are exactly the ones a new phone-keyed contact
        # displaces. They are taken straight from the table rather than from
        # /api/people/untagged, because that queue only offers people who have
        # skills and source3 (the phone-only file) has no skills column.
        phone_only = self.conn().execute(
            "SELECT person_id FROM people WHERE email IS NULL AND phone IS NOT NULL "
            "ORDER BY phone").fetchall()
        self.assertTrue(phone_only, "expected people with a phone but no email")
        for row in phone_only:
            status, _ = self.post("/api/people/%d/category" % row["person_id"],
                                  {"category": "generalist", "confidence": 0.5})
            self.assertEqual(status, 200)

        before = {row["person_key"]: row["person_id"] for row in self.conn().execute(
            "SELECT person_key, person_id FROM person_skill_categories")}

        # A new contact with a very low phone number, inserted the way the audio
        # app inserts one.
        conn = self.conn()
        with conn:
            conn.execute(
                "INSERT INTO app_contacts (created_at, full_name, phone, phone_raw) "
                "VALUES ('2026-08-18', 'Aaa Shifter', '9000000002', '9000000002')")
        conn.close()

        pipeline_run.run(db_path=self.db_path, verbose=False)

        after = {}
        for row in self.conn().execute(
                "SELECT c.person_key, c.person_id, c.category, p.email, p.phone "
                "FROM person_skill_categories c LEFT JOIN people p USING (person_id)"):
            after[row["person_key"]] = row
            # The tag still points at the person it was written for.
            self.assertIn(row["person_key"], (row["email"], row["phone"]),
                          "tag for %s drifted onto a different person" % row["person_key"])

        self.assertEqual(set(before), set(after))
        self.assertEqual(self.conn().execute(
            "SELECT COUNT(*) FROM person_skill_categories "
            "WHERE person_id IS NULL").fetchone()[0], 0)

        # And at least one id really did move, or the test proves nothing.
        moved = [key for key in before if before[key] != after[key]["person_id"]]
        self.assertTrue(moved, "no id shifted, so this test did not exercise the risk")


class TestDuplicateCheck(AutomationTestCase):
    def check(self, rows, source="test.csv"):
        return self.post("/api/match/check", {"source": source, "rows": rows})

    def test_a_duplicate_is_caught_on_email_even_when_shouted(self):
        status, payload = self.check([
            {"name": "Tanvi Gupta", "email": "TANVI.GUPTA31@EXAMPLE.COM", "city": "Bengaluru"}])
        self.assertEqual(status, 200)
        self.assertEqual(payload["duplicate_count"], 1)
        self.assertEqual(payload["results"][0]["matched_on"], "email")
        self.assertTrue(payload["has_duplicates"])

    def test_a_duplicate_is_caught_on_a_messily_written_phone(self):
        _, payload = self.check([{"name": "Rohit Nair", "phone": "+91-9000000268"}])
        self.assertEqual(payload["duplicate_count"], 1)
        self.assertEqual(payload["results"][0]["matched_on"], "phone")

    def test_a_genuinely_new_person_is_new(self):
        _, payload = self.check([
            {"name": "Lakshmi Iyer", "email": "lakshmi.iyer@example.com",
             "phone": "9000000901", "city": "Chennai"}])
        self.assertEqual(payload["new_count"], 1)
        self.assertFalse(payload["has_duplicates"])

    def test_an_ambiguous_name_is_never_reported_as_a_duplicate(self):
        """Three Arjun Mehtas live in Noida. A name+city hit is a question."""
        _, payload = self.check([{"name": "Arjun Mehta", "city": "Noida"}])
        result = payload["results"][0]
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["matched_on"], "name_city_ambiguous")
        self.assertEqual(payload["duplicate_count"], 0)
        self.assertTrue(payload["needs_attention"])

    def test_a_single_name_city_hit_is_review_not_duplicate(self):
        _, payload = self.check([{"name": "Meera Bhatia", "city": "Delhi NCR"}])
        result = payload["results"][0]
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["matched_on"], "name_city")
        self.assertIsNotNone(result["person_id"])

    def test_a_row_with_no_usable_key_is_review_not_a_clean_pass(self):
        _, payload = self.check([{"name": "Zoya Khan", "phone": "not-a-number"}])
        self.assertEqual(payload["results"][0]["status"], "needs_review")
        self.assertEqual(payload["review_count"], 1)

    def test_reserved_email_domains_are_counted(self):
        _, payload = self.check([
            {"name": "Tanvi Gupta", "email": "tanvi.gupta31@example.com"}])
        self.assertEqual(payload["undeliverable_email_count"], 1)

    def test_a_malformed_body_is_rejected(self):
        response = self.client.post("/api/match/check", json={"rows": "not a list"})
        self.assertEqual(response.status_code, 400)

    def test_the_batch_is_recorded_for_later_reconstruction(self):
        self.check([{"name": "Tanvi Gupta", "email": "tanvi.gupta31@example.com"}],
                   source="monday-upload.csv")
        row = self.conn().execute(
            "SELECT * FROM automation_events WHERE event_type = 'batch_checked' "
            "ORDER BY event_id DESC LIMIT 1").fetchone()
        self.assertEqual(row["flow"], "duplicate-alert")
        payload = json.loads(row["payload"])
        self.assertEqual(payload["source"], "monday-upload.csv")
        self.assertEqual(payload["counts"]["duplicate"], 1)

    def test_the_whole_sample_file_behaves_as_documented(self):
        """End-to-end over automation/sample_incoming/new_applicants_batch.csv,
        via the same column mapping the workflow's Code node applies."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
        import replay_flows

        csv_path = replay_flows.SAMPLE_CSV
        rows, skipped = replay_flows.map_csv(csv_path)
        self.assertEqual(sorted(item["reason"] for item in skipped),
                         ["blank row", "repeated header row"])

        _, payload = self.check(rows, source="new_applicants_batch.csv")
        self.assertEqual(payload["checked"], 7)
        self.assertEqual(payload["duplicate_count"], 3)
        self.assertEqual(payload["review_count"], 2)
        self.assertEqual(payload["new_count"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
