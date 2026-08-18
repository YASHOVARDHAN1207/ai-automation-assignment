"""Tests for identity resolution.

These use synthetic records rather than the real CSVs, so each guard is tested
in isolation - including the cases the real data does not happen to contain
(e.g. a name+city pair spanning two clusters that hold contradictory phones).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import match
from pipeline.extract import SourceRecord
from pipeline.issues import IssueLog


def record(source, row, name, email=None, phone=None, city="Noida", skills=None):
    """Build a SourceRecord the way extract.* would, without touching a file."""
    rec = SourceRecord(source, row, {"name": name})
    tokens = name.lower().replace(".", "").split()
    rec.fields = {
        "full_name": name,
        "name_key": " ".join(tokens),
        "name_tokens": tokens,
        "name_is_abbreviated": any(len(t) == 1 for t in tokens),
        "email": email,
        "phone": phone,
        "city": city,
        "city_region": None,
        "city_is_region_guess": False,
    }
    rec.skills = skills or []
    return rec


def resolve(records):
    log = IssueLog()
    people, conflicts, reviews, uf = match.resolve(records, log)
    return people, conflicts, reviews, log


class TestStrongPasses(unittest.TestCase):
    def test_email_merges_across_sources(self):
        people, _, _, _ = resolve([
            record("naukri", 2, "Karan Bhatia", email="k@x.com", phone="9000000211"),
            record("gig", 4, "Karan Bhatia", email="K@X.COM".lower()),
        ])
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["source_count"], 2)

    def test_phone_merges_when_emails_differ(self):
        """The Nikhil Chopra case: two emails, one phone, one person."""
        people, conflicts, _, _ = resolve([
            record("naukri", 27, "Nikhil Chopra", email="alt.n@x.com", phone="9000000103"),
            record("naukri", 37, "Nikhil Chopra", email="n@x.com", phone="9000000103"),
        ])
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["match_methods"], "exact_phone")

    def test_transitive_join_through_the_hub_file(self):
        """source2 (email only) and source3 (phone only) share nothing, but both
        touch source1, so all three must land on one person."""
        people, _, _, _ = resolve([
            record("gig", 4, "Isha Chopra", email="i@x.com"),
            record("naukri", 9, "Isha Chopra", email="i@x.com", phone="9000000138"),
            record("cbnexus", 18, "Isha Chopra", phone="9000000138"),
        ])
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["source_count"], 3)
        self.assertEqual(people[0]["match_methods"], "exact_email,exact_phone")
        self.assertEqual(people[0]["match_confidence"], 1.0)

    def test_different_people_stay_separate(self):
        people, _, _, _ = resolve([
            record("naukri", 2, "A One", email="a@x.com", phone="9000000001"),
            record("naukri", 3, "B Two", email="b@x.com", phone="9000000002"),
        ])
        self.assertEqual(len(people), 2)


class TestWeakPassGuards(unittest.TestCase):
    def test_weak_merge_fires_when_no_strong_key_could_ever_match(self):
        """source2 has the email, source3 has the phone, neither has both."""
        people, _, reviews, log = resolve([
            record("gig", 21, "Divya Chopra", email="d@x.com"),
            record("cbnexus", 30, "Divya Chopra", phone="9000000111"),
        ])
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["match_methods"], "name_city_weak")
        self.assertEqual(people[0]["match_confidence"], 0.6)
        self.assertEqual(reviews, [])
        # Both keys survive onto the golden record.
        self.assertEqual(people[0]["email"], "d@x.com")
        self.assertEqual(people[0]["phone"], "9000000111")

    def test_g2_three_clusters_with_one_name_are_never_merged(self):
        """The Arjun Mehta case. Three people, one name, one city."""
        people, _, reviews, log = resolve([
            record("gig", 18, "Arjun Mehta", email="a77@x.com"),
            record("naukri", 20, "Arjun Mehta", email="a9@x.com", phone="9000000131"),
            record("cbnexus", 5, "Arjun Mehta", phone="9000000131"),
            record("cbnexus", 28, "Arjun Mehta", phone="9000000272"),
        ])
        # 3 people: the naukri+cbnexus pair merges on phone, the other two stand alone.
        self.assertEqual(len(people), 3)
        self.assertEqual(len(reviews), 3)
        self.assertTrue(all(r["reason"] == "ambiguous_name_city" for r in reviews))
        self.assertIn("ambiguous_identity",
                      set(r["category"] for r in log.rows))

    def test_g3_contradictory_phones_beat_a_name_match(self):
        """Two Deepak Nairs in one city with different phones: not one person."""
        people, _, reviews, _ = resolve([
            record("cbnexus", 25, "Deepak Nair", phone="9000000296", city="Pune"),
            record("gig", 32, "Deepak Nair", email="d57@x.com", city="Pune"),
            record("naukri", 33, "Deepak Nair", email="d57@x.com", phone="9000000999", city="Pune"),
        ])
        self.assertEqual(len(people), 2)
        self.assertEqual([r["reason"] for r in reviews],
                         ["same_name_conflicting_strong_key"])

    def test_same_name_different_city_is_not_merged(self):
        people, _, _, log = resolve([
            record("gig", 32, "Deepak Nair", email="d57@x.com", city="Delhi"),
            record("cbnexus", 25, "Deepak Nair", phone="9000000296", city="Bengaluru"),
        ])
        self.assertEqual(len(people), 2)
        self.assertIn("homonym", set(r["category"] for r in log.rows))

    def test_weak_pass_is_order_independent(self):
        """A merge decided early must not invalidate the roots later iterations
        were reasoning about."""
        base = [
            record("gig", 1, "Aa Bb", email="ab@x.com", city="Pune"),
            record("cbnexus", 2, "Aa Bb", phone="9000000001", city="Pune"),
            record("gig", 3, "Cc Dd", email="cd@x.com", city="Pune"),
            record("cbnexus", 4, "Cc Dd", phone="9000000002", city="Pune"),
        ]
        forward, _, _, _ = resolve(list(base))
        backward, _, _, _ = resolve(list(reversed(base)))
        self.assertEqual(len(forward), 2)
        self.assertEqual(len(backward), 2)
        self.assertEqual([p["full_name"] for p in forward],
                         [p["full_name"] for p in backward])


class TestGoldenRecord(unittest.TestCase):
    def test_full_name_prefers_the_spelled_out_variant(self):
        people, conflicts, _, _ = resolve([
            record("naukri", 25, "R. Verma", email="rv@x.com", phone="9000000294"),
            record("naukri", 31, "Rohit Verma", email="rv@x.com", phone="9000000294"),
        ])
        self.assertEqual(people[0]["full_name"], "Rohit Verma")
        self.assertEqual([(c["field"], c["rejected_value"]) for c in conflicts],
                         [("full_name", "R. Verma")])

    def test_city_is_decided_by_majority(self):
        people, conflicts, _, _ = resolve([
            record("naukri", 2, "X Y", email="x@x.com", phone="9000000001", city="Pune"),
            record("gig", 3, "X Y", email="x@x.com", city="Noida"),
            record("cbnexus", 4, "X Y", phone="9000000001", city="Noida"),
        ])
        self.assertEqual(people[0]["city"], "Noida")
        self.assertEqual([c["rejected_value"] for c in conflicts], ["Pune"])

    def test_skills_are_unioned_with_provenance(self):
        people, _, _, _ = resolve([
            record("naukri", 2, "X Y", email="x@x.com", skills=["SQL", "Python"]),
            record("gig", 3, "X Y", email="x@x.com", skills=["SQL", "Docker"]),
        ])
        skills = people[0]["_skills"]
        self.assertEqual(sorted(skills), ["Docker", "Python", "SQL"])
        self.assertEqual(len(skills["SQL"]), 2)      # claimed by both files
        self.assertEqual(len(skills["Docker"]), 1)

    def test_person_ids_are_stable_across_input_order(self):
        rows = [
            record("naukri", 2, "Bb Cc", email="b@x.com", phone="9000000002"),
            record("naukri", 3, "Aa Bb", email="a@x.com", phone="9000000001"),
        ]
        first, _, _, _ = resolve(list(rows))
        second, _, _, _ = resolve(list(reversed(rows)))
        self.assertEqual({p["email"]: p["person_id"] for p in first},
                         {p["email"]: p["person_id"] for p in second})


if __name__ == "__main__":
    unittest.main(verbosity=2)
