"""End-to-end test against the real CSVs, into a throwaway database.

This is the regression net for the claims made in the README and in
docs/DATA_ISSUES.md. If a refactor changes how many people come out, or lets an
"Arjun Mehta" merge happen, these fail.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import config, extract, run as run_module
from pipeline.extract import GIG_VALIDATORS, repair_rotation


class TestPipelineEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handle, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(cls.db_path)
        cls.result = run_module.run(db_path=cls.db_path, verbose=False)
        cls.conn = sqlite3.connect(cls.db_path)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        if os.path.exists(cls.db_path):
            os.unlink(cls.db_path)

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, *args):
        row = self.conn.execute(sql, args).fetchone()
        return row[0] if row else None

    # --- shape ------------------------------------------------------------

    def test_row_and_person_counts(self):
        self.assertEqual(self.result["rows_read"], 103)
        self.assertEqual(self.result["people"], 56)
        self.assertEqual(self.one("SELECT COUNT(*) FROM people"), 56)
        self.assertEqual(self.one("SELECT COUNT(*) FROM source_records"), 103)

    def test_every_used_row_is_attached_to_a_person(self):
        orphans = self.one(
            "SELECT COUNT(*) FROM source_records WHERE was_used = 1 AND person_id IS NULL")
        self.assertEqual(orphans, 0)

    def test_source_coverage(self):
        counts = {r["source_count"]: r["n"] for r in self.q(
            "SELECT source_count, COUNT(*) n FROM people GROUP BY source_count")}
        self.assertEqual(counts, {1: 27, 2: 14, 3: 15})

    # --- the promise of the merge step ------------------------------------

    def test_no_email_or_phone_belongs_to_two_people(self):
        """The unique indexes enforce this at write time; assert it explicitly so
        the guarantee is documented, not incidental."""
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM (SELECT email FROM people WHERE email IS NOT NULL "
            "GROUP BY email HAVING COUNT(*) > 1)"), 0)
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM (SELECT phone FROM people WHERE phone IS NOT NULL "
            "GROUP BY phone HAVING COUNT(*) > 1)"), 0)

    def test_the_three_arjun_mehtas_are_still_three_people(self):
        rows = self.q("SELECT * FROM people WHERE full_name = 'Arjun Mehta'")
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            self.one("SELECT COUNT(*) FROM merge_review WHERE reason = 'ambiguous_name_city'"), 3)

    def test_the_two_deepak_nairs_are_still_two_people(self):
        rows = self.q("SELECT city FROM people WHERE full_name = 'Deepak Nair' ORDER BY city")
        self.assertEqual([r["city"] for r in rows], ["Bengaluru", "Delhi"])

    def test_rohit_verma_merged_and_kept_the_better_name(self):
        row = self.q("SELECT * FROM people WHERE phone = '9000000294'")
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["full_name"], "Rohit Verma")
        self.assertEqual(row[0]["record_count"], 2)

    def test_nikhil_chopra_merged_on_phone_despite_two_emails(self):
        row = self.q("SELECT * FROM people WHERE phone = '9000000103'")
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["email"], "nikhil.chopra70@example.com")
        self.assertEqual(row[0]["match_methods"], "exact_phone")

    def test_isha_chopra_absorbed_all_four_rows_including_the_scrambled_one(self):
        row = self.q("SELECT * FROM people WHERE email = 'isha.chopra95@mailtest.example.org'")
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["record_count"], 4)
        self.assertEqual(row[0]["source_count"], 3)

    def test_exactly_four_weak_merges_and_they_are_all_source2_plus_source3(self):
        rows = self.q("SELECT * FROM people WHERE match_methods = 'name_city_weak'")
        self.assertEqual(sorted(r["full_name"] for r in rows),
                         ["Divya Chopra", "Karan Chopra", "Manish Bhatia", "Vikram Mehta"])
        for row in rows:
            self.assertEqual(row["match_confidence"], 0.6)
            self.assertEqual(row["sources"],
                             "source2_gig_workers.csv,source3_cbnexus_contacts.csv")
            # Each one gained a key it could not have had from either file alone.
            self.assertIsNotNone(row["email"])
            self.assertIsNotNone(row["phone"])

    # --- structural repairs -----------------------------------------------

    def test_scrambled_row_was_detected_generically(self):
        """No row number is hardcoded in extract.py - re-run the detector on the
        raw values and confirm it is the scoring that finds the damage."""
        scrambled = ["react, javascript, mysql", "ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG",
                     "Isha Chopra", "1406/hr", "Pune", "active"]
        repaired, shift, before, after = repair_rotation(scrambled, GIG_VALIDATORS)
        self.assertEqual(shift, 1)
        self.assertEqual(before, 0.0)
        self.assertEqual(after, 6.0)
        self.assertEqual(repaired[0], "ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG")

    def test_a_correctly_ordered_row_is_left_alone(self):
        clean = ["varun.jain29@example.com", "Varun Jain", "1415/hr", "Pune", "Active",
                 "n8n, web scraping, fastapi, mysql, pandas, mongodb"]
        repaired, shift, _, _ = repair_rotation(clean, GIG_VALIDATORS)
        self.assertEqual(shift, 0)
        self.assertEqual(repaired, clean)

    def test_repeated_header_never_became_a_person(self):
        self.assertEqual(self.one("SELECT COUNT(*) FROM people WHERE full_name = 'Name'"), 0)
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM data_issues WHERE category = 'repeated_header'"), 1)

    def test_blank_row_never_became_a_person(self):
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM data_issues WHERE category = 'blank_row'"), 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM people WHERE full_name IS NULL"), 0)

    # --- value normalisation ----------------------------------------------

    def test_all_phones_are_ten_digits(self):
        bad = self.q("SELECT phone FROM people WHERE phone IS NOT NULL "
                     "AND (LENGTH(phone) != 10 OR phone GLOB '*[^0-9]*')")
        self.assertEqual(bad, [])

    def test_all_emails_are_lowercase(self):
        self.assertEqual(self.q(
            "SELECT email FROM people WHERE email IS NOT NULL AND email != LOWER(email)"), [])

    def test_cities_are_canonical(self):
        cities = set(r["city"] for r in self.q("SELECT DISTINCT city FROM people"))
        self.assertEqual(cities, {"Bengaluru", "Delhi", "Gurugram", "Noida", "Pune"})

    def test_no_ctc_left_in_lakhs(self):
        """A value under 1000 in this column would mean the unit fix was skipped."""
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM people WHERE ctc_annual_inr IS NOT NULL "
            "AND ctc_annual_inr < 1000"), 0)
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM people WHERE ctc_unit_detected = 'lakh_per_annum'"), 19)

    def test_dates_are_iso_and_future_ones_are_flagged(self):
        self.assertEqual(self.q(
            "SELECT applied_date FROM people WHERE applied_date IS NOT NULL "
            "AND applied_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"), [])
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM people WHERE applied_date_is_future = 1"), 5)
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM people WHERE applied_date > ?", config.INGEST_DATE),
            self.one("SELECT COUNT(*) FROM people WHERE applied_date_is_future = 1"))

    def test_gig_status_is_a_clean_enum(self):
        values = set(r["gig_status"] for r in self.q(
            "SELECT DISTINCT gig_status FROM people WHERE gig_status IS NOT NULL"))
        self.assertEqual(values, {"active", "inactive", "paused"})

    def test_both_rate_figures_are_present_whenever_either_is(self):
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM people WHERE (rate_hourly_inr IS NULL) "
            "!= (rate_monthly_inr IS NULL)"), 0)
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM people WHERE rate_basis_raw IS NOT NULL "
            "AND rate_basis_raw NOT IN ('hourly', 'monthly', 'daily')"), 0)

    def test_city_region_flag_agrees_with_the_city(self):
        """The bug this guards: city 'Delhi' carrying a region-guess flag that
        came from a different row saying 'Delhi NCR'."""
        for row in self.q("SELECT city, city_region FROM people"):
            self.assertEqual(row["city_region"], config.CITY_REGION.get(row["city"]))

    # --- corroboration ----------------------------------------------------

    def test_skills_agree_between_sources_for_merged_people(self):
        """Independent evidence the merges are right: wrongly merged people would
        not list identical skills in two different files."""
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM data_issues WHERE category = 'cross_source_disagreement'"), 0)
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM data_issues WHERE category = 'cross_source_agreement'"), 1)

    def test_every_issue_records_what_was_done_about_it(self):
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM data_issues WHERE action_taken IS NULL "
            "OR TRIM(action_taken) = ''"), 0)

    def test_issue_severities_are_within_the_expected_envelope(self):
        counts = {r["severity"]: r["n"] for r in self.q(
            "SELECT severity, COUNT(*) n FROM data_issues GROUP BY severity")}
        self.assertEqual(counts["error"], 2)          # scrambled row + repeated header
        self.assertGreaterEqual(counts["warn"], 50)
        self.assertGreater(counts["info"], 300)

    def test_a_rerun_produces_identical_person_ids(self):
        handle, second_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(second_path)
        try:
            run_module.run(db_path=second_path, verbose=False)
            other = sqlite3.connect(second_path)
            try:
                first = self.q("SELECT person_id, full_name, email, phone FROM people "
                               "ORDER BY person_id")
                again = other.execute("SELECT person_id, full_name, email, phone FROM people "
                                      "ORDER BY person_id").fetchall()
                self.assertEqual([tuple(r) for r in first], [tuple(r) for r in again])
            finally:
                other.close()
        finally:
            if os.path.exists(second_path):
                os.unlink(second_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
