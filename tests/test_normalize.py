"""Unit tests for the field normalisers.

The important ones are not "does it lowercase an email" but the two claims the
pipeline makes about the real files:

  * the dash/slash date convention is a property of the data, not a guess
  * the lakh-vs-rupee CTC ranges do not overlap, so the < 1000 rule is safe

Both are asserted against data/*.csv, so if a future file breaks the assumption
the suite fails instead of the data quietly going wrong.
"""
import csv
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import config, normalize
from pipeline.normalize import ERROR, INFO, WARN


def categories(norm):
    return set(p["category"] for p in norm.problems)


class TestPhone(unittest.TestCase):
    def test_all_four_spellings_reduce_to_the_same_number(self):
        for raw in ("+919000000254", "919000000254", "09000000254",
                    "+91-9000000254", "9000000254", " 9000000254 ",
                    "00919000000254", "+91 90000 00254"):
            self.assertEqual(normalize.normalize_phone(raw).value, "9000000254",
                             "failed for %r" % raw)

    def test_transitive_bridge_survives_normalisation(self):
        # This exact pair is what links source1 to source3 for Nikhil Chopra.
        self.assertEqual(normalize.normalize_phone("09000000103").value,
                         normalize.normalize_phone("+91-9000000103").value)

    def test_too_short_is_rejected_not_padded(self):
        result = normalize.normalize_phone("12345")
        self.assertIsNone(result.value)
        self.assertEqual(categories(result), {"invalid_format"})

    def test_non_mobile_prefix_is_kept_but_flagged(self):
        result = normalize.normalize_phone("1234567890")
        self.assertEqual(result.value, "1234567890")
        self.assertIn("suspicious_value", categories(result))

    def test_empty_is_null_with_a_warning_not_an_exception(self):
        for raw in (None, "", "   "):
            result = normalize.normalize_phone(raw)
            self.assertIsNone(result.value)
            self.assertEqual(categories(result), {"missing_value"})


class TestEmail(unittest.TestCase):
    def test_uppercase_is_folded(self):
        result = normalize.normalize_email("ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG")
        self.assertEqual(result.value, "isha.chopra95@mailtest.example.org")
        self.assertIn("inconsistent_casing", categories(result))

    def test_alias_is_reported_but_not_rewritten(self):
        result = normalize.normalize_email("alt.nikhil.chopra70@example.com")
        # The value is NOT silently rewritten to the non-alias address: the
        # merge is justified by the phone number, not by this resemblance.
        self.assertEqual(result.value, "alt.nikhil.chopra70@example.com")
        self.assertEqual(result.extra["alias_of"], "nikhil.chopra70@example.com")
        self.assertIn("alias_email", categories(result))

    def test_garbage_is_rejected(self):
        for raw in ("not-an-email", "a@b", "@example.com", "two@@at.com"):
            result = normalize.normalize_email(raw)
            self.assertIsNone(result.value, "accepted %r" % raw)


class TestName(unittest.TestCase):
    def test_casing_is_normalised_for_display(self):
        self.assertEqual(normalize.normalize_name("RITU SHARMA").value, "Ritu Sharma")
        self.assertEqual(normalize.normalize_name("ritu sharma").value, "Ritu Sharma")

    def test_initial_is_preserved_and_flagged(self):
        result = normalize.normalize_name("R. Verma")
        self.assertEqual(result.value, "R. Verma")
        self.assertIn("abbreviated_name", categories(result))
        self.assertTrue(result.extra["initials_only"])

    def test_match_key_ignores_case_and_punctuation(self):
        self.assertEqual(normalize.normalize_name("MANISH BHATIA").extra["key"],
                         normalize.normalize_name("Manish Bhatia ").extra["key"])

    def test_initial_compatibility(self):
        tokens = lambda s: normalize.normalize_name(s).extra["tokens"]
        self.assertTrue(normalize.names_compatible(tokens("R. Verma"), tokens("Rohit Verma")))
        self.assertFalse(normalize.names_compatible(tokens("R. Verma"), tokens("Kavya Verma")))
        # Different length -> not compatible, rather than a fuzzy guess.
        self.assertFalse(normalize.names_compatible(tokens("Rohit Verma"), tokens("Rohit Kumar Verma")))


class TestCity(unittest.TestCase):
    def test_aliases_and_casing_collapse(self):
        for raw in ("Bengaluru", "bangalore", "BANGALORE", "Bengaluru "):
            self.assertEqual(normalize.normalize_city(raw).value, "Bengaluru")
        for raw in ("GURGAON", "gurugram ", "Gurugram"):
            self.assertEqual(normalize.normalize_city(raw).value, "Gurugram")

    def test_region_label_is_flagged_not_silently_upgraded(self):
        result = normalize.normalize_city("Delhi NCR")
        self.assertEqual(result.value, "Delhi")
        self.assertTrue(result.extra["region_only"])
        self.assertIn("precision_loss", categories(result))

        precise = normalize.normalize_city("New Delhi")
        self.assertEqual(precise.value, "Delhi")
        self.assertFalse(precise.extra["region_only"])

    def test_unknown_city_is_kept_not_dropped(self):
        result = normalize.normalize_city("Kochi")
        self.assertEqual(result.value, "Kochi")
        self.assertIn("unmapped_value", categories(result))


class TestDates(unittest.TestCase):
    def test_the_four_formats(self):
        self.assertEqual(normalize.normalize_date("2026-08-08").value, "2026-08-08")
        self.assertEqual(normalize.normalize_date("24-07-2026").value, "2026-07-24")
        self.assertEqual(normalize.normalize_date("07/13/2026").value, "2026-07-13")
        self.assertEqual(normalize.normalize_date("7 Jul 2026").value, "2026-07-07")

    def test_value_beats_convention_when_a_component_exceeds_12(self):
        # 13 cannot be a month, whatever the separator says.
        self.assertEqual(normalize.normalize_date("07/13/2026").value, "2026-07-13")
        self.assertEqual(normalize.normalize_date("24-07-2026").value, "2026-07-24")

    def test_undecidable_dates_are_flagged(self):
        result = normalize.normalize_date("07/03/2026")
        self.assertEqual(result.value, "2026-07-03")     # slash -> mm/dd
        self.assertTrue(result.extra["ambiguous"])
        result = normalize.normalize_date("03-07-2026")
        self.assertEqual(result.value, "2026-07-03")     # dash -> dd-mm
        self.assertTrue(result.extra["ambiguous"])

    def test_future_dates_are_kept_and_flagged(self):
        result = normalize.normalize_date("22-08-2026")   # ingest date 2026-08-18
        self.assertEqual(result.value, "2026-08-22")
        self.assertTrue(result.extra["is_future"])
        self.assertIn("future_date", categories(result))

    def test_impossible_dates_are_rejected(self):
        for raw in ("32-01-2026", "2026-02-30", "13/13/2026"):
            self.assertIsNone(normalize.normalize_date(raw).value, "accepted %r" % raw)

    def test_date_separator_convention_holds_on_real_data(self):
        """The claim: dash means dd-mm and slash means mm/dd in source1.

        Proven, not assumed: for every dash date, any component > 12 must be in
        position 1; for every slash date, in position 2. If both conventions
        appeared under one separator this assertion fails and the parser needs a
        different rule.
        """
        dash_day_first = slash_month_first = 0
        with io.open(config.SOURCES["naukri"], encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                value = (row.get("Applied Date") or "").strip()
                if "-" in value and not value.startswith("20"):
                    a, b, _ = value.split("-")
                    self.assertFalse(int(b) > 12, "dash date %r has month > 12" % value)
                    dash_day_first += int(a) > 12
                elif "/" in value:
                    a, b, _ = value.split("/")
                    self.assertFalse(int(a) > 12, "slash date %r has month > 12" % value)
                    slash_month_first += int(b) > 12

        # Both conventions must actually be *witnessed* in the file, otherwise
        # the test is vacuously true and proves nothing.
        self.assertGreater(dash_day_first, 0, "no dash date proves dd-mm")
        self.assertGreater(slash_month_first, 0, "no slash date proves mm/dd")


class TestMoney(unittest.TestCase):
    def test_lakhs_are_expanded(self):
        result = normalize.normalize_ctc("4.2")
        self.assertEqual(result.value, 420000.0)
        self.assertEqual(result.extra["unit"], "lakh_per_annum")
        self.assertIn("mixed_units", categories(result))

    def test_absolute_rupees_are_left_alone(self):
        result = normalize.normalize_ctc("417964")
        self.assertEqual(result.value, 417964.0)
        self.assertEqual(result.extra["unit"], "absolute_inr")

    def test_ctc_unit_ranges_do_not_overlap_in_real_data(self):
        """The < 1000 threshold is only safe if no value is ambiguous."""
        lakhs, absolutes = [], []
        with io.open(config.SOURCES["naukri"], encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                raw = (row.get("Current CTC") or "").strip()
                if not raw:
                    continue
                value = float(raw)
                (lakhs if value < config.CTC_LAKH_THRESHOLD else absolutes).append(value)
        self.assertTrue(lakhs and absolutes, "file should contain both units")
        # A clear gap between the two populations, not a fuzzy boundary.
        self.assertLess(max(lakhs) * 100, min(absolutes),
                        "lakh and rupee ranges are too close for a threshold rule")


class TestRate(unittest.TestCase):
    def test_hourly(self):
        result = normalize.normalize_rate("1415/hr")
        self.assertEqual(result.extra["hourly"], 1415.0)
        self.assertEqual(result.extra["monthly"], 1415.0 * config.BILLABLE_HOURS_PER_MONTH)
        self.assertEqual(result.extra["basis"], "hourly")

    def test_monthly_with_k_suffix(self):
        result = normalize.normalize_rate("15k/month")
        self.assertEqual(result.extra["monthly"], 15000.0)
        self.assertEqual(result.extra["basis"], "monthly")

    def test_basis_records_what_was_actually_quoted(self):
        # Both numbers are always populated, but only one of them was stated by
        # the worker. Anything comparing rates has to know which.
        self.assertEqual(normalize.normalize_rate("403/hr").extra["basis"], "hourly")
        self.assertEqual(normalize.normalize_rate("72k/month").extra["basis"], "monthly")

    def test_unparseable_rate_is_null(self):
        self.assertIsNone(normalize.normalize_rate("negotiable").value)


class TestSmallScalars(unittest.TestCase):
    def test_boolean_spellings(self):
        for raw in ("Y", "y", "yes", "Yes", "YES", "true", "1"):
            self.assertIs(normalize.normalize_bool(raw).value, True, raw)
        for raw in ("N", "n", "No", "no", "false", "0"):
            self.assertIs(normalize.normalize_bool(raw).value, False, raw)

    def test_unknown_boolean_is_null_not_false(self):
        # "maybe" is not False. Guessing here would invent a verification status.
        result = normalize.normalize_bool("maybe")
        self.assertIsNone(result.value)

    def test_status_enum(self):
        for raw in ("Active", "active", "ACTIVE"):
            self.assertEqual(normalize.normalize_status(raw).value, "active")
        self.assertEqual(normalize.normalize_status("Inactive").value, "inactive")
        self.assertEqual(normalize.normalize_status("paused").value, "paused")
        self.assertIsNone(normalize.normalize_status("retired").value)


class TestSkills(unittest.TestCase):
    def test_casing_variants_collapse_to_one_skill(self):
        a = normalize.normalize_skills("REST APIs, MongoDB, SQL")
        b = normalize.normalize_skills("rest apis, mongodb, sql")
        self.assertEqual(a.value, b.value)

    def test_duplicates_within_one_list_are_removed(self):
        result = normalize.normalize_skills("SQL, sql, SQL ")
        self.assertEqual(result.value, ["SQL"])

    def test_unknown_skill_is_kept_verbatim(self):
        result = normalize.normalize_skills("Rust")
        self.assertEqual(result.value, ["Rust"])
        self.assertIn("unmapped_value", categories(result))


if __name__ == "__main__":
    unittest.main(verbosity=2)
