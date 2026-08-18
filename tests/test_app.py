"""Integration tests for the Task 3 app, through Flask's test client.

The one that matters most is
test_an_app_created_contact_survives_a_pipeline_rebuild. `people` is a derived
table that the pipeline drops and rebuilds, so the obvious implementation - have
the app INSERT into `people` - loses every walk-in worker on the next
`make pipeline`, silently. These tests pin the design that avoids it.
"""
import io
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import flask                                   # noqa: F401
    import numpy as np
    HAVE_DEPS = True
except ImportError:                                # pragma: no cover
    HAVE_DEPS = False

if HAVE_DEPS:
    from app import audio_analysis, db as app_db, server
    from pipeline import run as pipeline_run


def wav_bytes(seconds=1.5, rate=44100, freq=440.0, amplitude=0.4):
    t = np.arange(int(rate * seconds)) / float(rate)
    samples = (amplitude * np.sin(2 * math.pi * freq * t))
    raw = (samples * 32767.0).astype("<i2").tobytes()
    return audio_analysis.wav_header(rate, 1, 16, len(raw)) + raw


@unittest.skipUnless(HAVE_DEPS, "flask and numpy are required for the app tests")
class AppTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.mkdtemp(prefix="cb-app-")
        cls.db_path = os.path.join(cls.workdir, "test.db")
        cls.uploads = os.path.join(cls.workdir, "uploads")
        os.makedirs(cls.uploads)
        pipeline_run.run(db_path=cls.db_path, verbose=False)
        app_db.ensure_schema(cls.db_path)

        server.app.config.update(TESTING=True, DB_PATH=cls.db_path,
                                 UPLOAD_DIR=cls.uploads)
        cls.client = server.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def submit(self, name="Priya Singh", phone="+91 90000 00287",
               filename="clip.wav", data=None, capture_mode="browser_recording"):
        payload = {
            "name": name,
            "phone": phone,
            "capture_mode": capture_mode,
            "audio": (io.BytesIO(data if data is not None else wav_bytes()), filename),
        }
        response = self.client.post("/api/submissions", data=payload,
                                    content_type="multipart/form-data")
        return response, json.loads(response.data.decode("utf-8"))

    def conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


class TestViews(AppTestCase):
    def test_the_two_required_views_render(self):
        for path in ("/", "/submissions"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
        body = self.client.get("/").data.decode("utf-8")
        self.assertIn('name="name"', body)      # name field
        self.assertIn('name="phone"', body)     # phone field
        self.assertIn("btn-record", body)       # browser recording
        self.assertIn('type="file"', body)      # or upload

    def test_healthz_reports_the_database(self):
        payload = json.loads(self.client.get("/healthz").data.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertGreater(payload["people"], 50)


class TestSubmission(AppTestCase):
    def test_a_submission_is_stored_with_its_audio_properties(self):
        response, payload = self.submit()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(payload["ok"])

        submission = payload["submission"]
        self.assertEqual(submission["analysis_ok"], 1)
        self.assertAlmostEqual(submission["duration_seconds"], 1.5, places=1)
        self.assertEqual(submission["sample_rate_khz"], 44.1)
        self.assertEqual(submission["bitrate_kbps"], 705.6)
        self.assertAlmostEqual(submission["peak_dbfs"], -7.96, delta=0.2)
        self.assertIsNotNone(submission["loudness_lufs"])
        self.assertIsNotNone(submission["quality_score"])
        self.assertEqual(submission["analysis_backend"], "stdlib wave")

        # And the file really is on disk and servable.
        stored = os.path.join(self.uploads, submission["stored_filename"])
        self.assertTrue(os.path.exists(stored))
        audio = self.client.get("/media/%s" % submission["stored_filename"])
        self.assertEqual(audio.status_code, 200)
        audio.close()          # send_from_directory holds the file open

    def test_a_messy_phone_still_matches_the_merged_person(self):
        """'+91 90000 00287' must find the person the pipeline stored as
        '9000000287'. This is why the app reuses pipeline.normalize."""
        _, payload = self.submit(phone="+91 90000 00287")
        self.assertEqual(payload["person_link_method"], "matched_existing")
        row = self.conn().execute(
            "SELECT full_name, city FROM people WHERE person_id = ?",
            (payload["person_id"],)).fetchone()
        self.assertEqual(row["full_name"], "Priya Singh")

        # Every spelling of the same number lands on the same person.
        for spelling in ("9000000287", "09000000287", "+919000000287", "91-9000000287"):
            _, other = self.submit(phone=spelling)
            self.assertEqual(other["person_id"], payload["person_id"], spelling)

    def test_an_unknown_phone_creates_a_durable_contact(self):
        _, payload = self.submit(name="ravi kumar", phone="09000000771")
        self.assertEqual(payload["person_link_method"], "created_contact")

        conn = self.conn()
        contact = conn.execute(
            "SELECT * FROM app_contacts WHERE phone = '9000000771'").fetchone()
        self.assertIsNotNone(contact)
        self.assertEqual(contact["full_name"], "Ravi Kumar")   # re-cased for display
        person = conn.execute("SELECT * FROM people WHERE person_id = ?",
                              (payload["person_id"],)).fetchone()
        self.assertEqual(person["phone"], "9000000771")

    def test_identical_audio_is_flagged_as_a_duplicate(self):
        data = wav_bytes(seconds=1.0, freq=523.0)
        _, first = self.submit(phone="9000000254", data=data)
        _, second = self.submit(phone="9000000254", data=data)
        self.assertIsNone(first["duplicate_of"])
        self.assertEqual(second["duplicate_of"], first["submission_id"])

    def test_an_unparseable_phone_stores_the_audio_without_claiming_an_identity(self):
        _, payload = self.submit(name="Test User", phone="not-a-number")
        self.assertEqual(payload["person_link_method"], "unlinked")
        self.assertIsNone(payload["person_id"])
        # The recording is still kept - losing it would be the worse failure.
        self.assertEqual(payload["submission"]["analysis_ok"], 1)

    def test_a_file_whose_audio_cannot_be_read_is_still_recorded(self):
        _, payload = self.submit(phone="9000000237", data=b"not audio at all",
                                 filename="broken.wav")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["submission"]["analysis_ok"], 0)
        self.assertTrue(payload["submission"]["analysis_note"])


class TestValidation(AppTestCase):
    def test_name_and_phone_are_required(self):
        response, payload = self.submit(name="")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Name", payload["error"])

        response, payload = self.submit(phone="")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Phone", payload["error"])

    def test_a_non_audio_extension_is_rejected(self):
        response, payload = self.submit(filename="resume.pdf")
        self.assertEqual(response.status_code, 400)
        self.assertIn("not accepted", payload["error"])

    def test_an_empty_file_is_rejected(self):
        response, payload = self.submit(data=b"")
        self.assertEqual(response.status_code, 400)
        self.assertIn("empty", payload["error"])

    def test_path_traversal_in_media_is_refused(self):
        response = self.client.get("/media/../../db/consultbae.db")
        self.assertIn(response.status_code, (400, 404))


class TestApi(AppTestCase):
    def test_list_and_detail_endpoints(self):
        _, payload = self.submit(phone="9000000113")
        listing = json.loads(self.client.get("/api/submissions").data.decode("utf-8"))
        self.assertGreater(listing["count"], 0)
        self.assertIn("audio_url", listing["submissions"][0])
        self.assertIn("stats", listing)

        detail = json.loads(self.client.get(
            "/api/submissions/%d" % payload["submission_id"]).data.decode("utf-8"))
        self.assertEqual(detail["submission_id"], payload["submission_id"])
        self.assertEqual(self.client.get("/api/submissions/999999").status_code, 404)


class TestRebuildSurvival(AppTestCase):
    def test_an_app_created_contact_survives_a_pipeline_rebuild(self):
        """The whole reason app_contacts exists.

        `people` is derived and gets dropped on every run. A worker who walks in
        through the app must still be there afterwards, and their submission must
        still point at them.
        """
        _, payload = self.submit(name="Anita Desai", phone="9000000888")
        self.assertEqual(payload["person_link_method"], "created_contact")
        submission_id = payload["submission_id"]

        # Rebuild from the CSVs, exactly as `make pipeline` does.
        pipeline_run.run(db_path=self.db_path, verbose=False)

        conn = self.conn()
        person = conn.execute(
            "SELECT * FROM people WHERE phone = '9000000888'").fetchone()
        self.assertIsNotNone(person, "the app-created person was wiped by the rebuild")
        self.assertEqual(person["full_name"], "Anita Desai")
        self.assertEqual(person["sources"], "app_contacts (audio app)")

        # The submission was re-pointed at the rebuilt person row.
        submission = conn.execute(
            "SELECT * FROM audio_submissions WHERE submission_id = ?",
            (submission_id,)).fetchone()
        self.assertEqual(submission["person_id"], person["person_id"])

        # And the audio file itself is untouched.
        self.assertTrue(os.path.exists(
            os.path.join(self.uploads, submission["stored_filename"])))

    def test_submissions_are_never_dropped_by_a_rebuild(self):
        before = self.conn().execute(
            "SELECT COUNT(*) FROM audio_submissions").fetchone()[0]
        self.assertGreater(before, 0)
        pipeline_run.run(db_path=self.db_path, verbose=False)
        after = self.conn().execute(
            "SELECT COUNT(*) FROM audio_submissions").fetchone()[0]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
