"""Dataset-level checks that no single row can reveal.

pipeline.normalize catches everything visible inside one field, and
pipeline.extract catches everything visible inside one row. This module catches
the third kind of problem: the ones that only exist when you look at the whole
column at once.

Absolute thresholds were deliberately avoided here. A hardcoded "flag any rate
above 400k/month" band tells you nothing except what the author guessed. These
checks compare each value against the distribution the data itself produced, so
they keep working when the data changes.
"""
from collections import Counter, defaultdict

from .normalize import INFO, WARN

# RFC 2606 / RFC 6761 reserve these for documentation and testing. Mail sent to
# them cannot be delivered.
RESERVED_DOMAIN_SUFFIXES = (".example.com", ".example.org", ".example.net",
                            "example.com", "example.org", "example.net",
                            "example.in", ".example", ".test", ".invalid", ".localhost")

OUTLIER_FACTOR = 3.0            # x median, in both directions
PROJECTS_TRUST_THRESHOLD = 10   # completed projects that ought to imply vetting


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def audit(people, records, log):
    _audit_reserved_domains(people, log)
    _audit_rate_distribution(people, log)
    _audit_ctc_distribution(people, log)
    _audit_trust_vs_projects(people, log)
    _audit_match_key_coverage(people, log)
    _audit_cross_source_skill_agreement(people, log)


# ---------------------------------------------------------------------------

def _audit_reserved_domains(people, log):
    domains = Counter()
    for person in people:
        if person.get("email"):
            domains[person["email"].split("@", 1)[1]] += 1
    reserved = {d: n for d, n in domains.items()
                if any(d == s or d.endswith(s) for s in RESERVED_DOMAIN_SUFFIXES)}
    if not reserved:
        return
    total = sum(domains.values())
    log.add("undeliverable_domain",
            "%d of %d email addresses (%.0f%%) sit on domains reserved for "
            "documentation and testing by RFC 2606: %s. Nothing sent to these "
            "will ever be delivered." % (
                sum(reserved.values()), total,
                100.0 * sum(reserved.values()) / total,
                ", ".join("%s (%d)" % (d, n) for d, n in sorted(reserved.items()))),
            "Loaded as-is because the assignment states the data is fictional. "
            "Flagged rather than ignored: in a real load this is the check that "
            "stops an automation from firing 56 bounces, and the duplicate-alert "
            "flow in Task 2 should refuse to send to a reserved domain.",
            INFO, None, None, "email")


def _audit_rate_distribution(people, log):
    """The finding here is not which rate is odd - it is that the unit predicts
    the magnitude, which means the two units are not two spellings of one
    number."""
    rows = [(p, p["rate_monthly_inr"], p["rate_basis_raw"]) for p in people
            if p.get("rate_monthly_inr") and p.get("rate_basis_raw")]
    if len(rows) < 6:
        return

    overall = _median([m for _, m, _ in rows])
    by_basis = defaultdict(list)
    for _, monthly, basis in rows:
        by_basis[basis].append(monthly)

    medians = {basis: _median(vals) for basis, vals in by_basis.items()}
    if len(medians) > 1:
        hourly_med = medians.get("hourly")
        monthly_med = medians.get("monthly")
        if hourly_med and monthly_med:
            log.add("unit_correlates_with_magnitude",
                    "Rates quoted per hour convert to a median of %s INR/month "
                    "while rates quoted per month have a median of %s INR/month "
                    "- a %.1fx gap between two groups doing the same work. The "
                    "quoting unit predicts the magnitude, so '1415/hr' and "
                    "'15k/month' are not two spellings of the same quantity." % (
                        "{:,}".format(int(hourly_med)), "{:,}".format(int(monthly_med)),
                        hourly_med / monthly_med),
                    "Refused to pick one truth. Both rate_hourly_inr and "
                    "rate_monthly_inr are stored, rate_basis_raw records which "
                    "one the worker actually quoted, and the 160 h/month "
                    "conversion is documented in config.py as an assumption "
                    "rather than a fact. Whoever owns this column upstream has "
                    "to answer whether the hourly cohort is part-time before "
                    "these numbers can be compared.",
                    WARN, None, None, "rate")

    if not overall:
        return
    high, low = overall * OUTLIER_FACTOR, overall / OUTLIER_FACTOR
    for person, monthly, basis in rows:
        if monthly > high or monthly < low:
            log.add("statistical_outlier",
                    "%s bills %s INR/month equivalent (quoted %s), vs a corpus "
                    "median of %s" % (
                        person["full_name"], "{:,}".format(int(monthly)), basis,
                        "{:,}".format(int(overall))),
                    "Kept. Flagged as more than %.0fx off the median so a human "
                    "can confirm the unit before this figure reaches a client "
                    "quote" % OUTLIER_FACTOR,
                    INFO, None, None, "rate", monthly, entity=person["full_name"])


def _audit_ctc_distribution(people, log):
    rows = [(p, p["ctc_annual_inr"], p.get("experience_years"))
            for p in people if p.get("ctc_annual_inr")]
    per_year = [(p, ctc / max(exp, 0.5)) for p, ctc, exp in rows if exp is not None]
    med = _median([v for _, v in per_year])
    if not med:
        return
    for person, value in per_year:
        if value > med * OUTLIER_FACTOR or value < med / OUTLIER_FACTOR:
            log.add("statistical_outlier",
                    "%s: %s INR CTC against %s years of experience is %s INR per "
                    "year of experience, vs a corpus median of %s" % (
                        person["full_name"], "{:,}".format(int(person["ctc_annual_inr"])),
                        person["experience_years"], "{:,}".format(int(value)),
                        "{:,}".format(int(med))),
                    "Kept and flagged. Either the CTC unit was misread at source "
                    "or the experience figure is wrong; both readings are "
                    "plausible from the row alone, so nothing was silently "
                    "corrected",
                    INFO, None, None, "ctc_annual_inr", person["ctc_annual_inr"],
                    entity=person["full_name"])


def _audit_trust_vs_projects(people, log):
    offenders = [p for p in people
                 if p.get("projects_completed") is not None
                 and p["projects_completed"] >= PROJECTS_TRUST_THRESHOLD
                 and p.get("is_verified") == 0]
    if not offenders:
        return
    log.add("business_rule_violation",
            "%d contacts have completed %d or more projects but are still "
            "flagged unverified in CBNexus: %s" % (
                len(offenders), PROJECTS_TRUST_THRESHOLD,
                ", ".join("%s (%d)" % (p["full_name"], p["projects_completed"])
                          for p in sorted(offenders, key=lambda x: -x["projects_completed"]))),
            "Loaded as given - the pipeline does not get to decide who is "
            "verified. Reported because 'verified' and 'projects completed' "
            "disagreeing this often means the verification step is being skipped "
            "after the first project, which is a process bug upstream rather "
            "than a bad cell.",
            WARN, None, None, "is_verified")


def _audit_match_key_coverage(people, log):
    """Who can never be matched against a future file, and why."""
    no_email = [p for p in people if not p.get("email")]
    no_phone = [p for p in people if not p.get("phone")]
    neither = [p for p in people if not p.get("email") and not p.get("phone")]

    log.add("match_key_coverage",
            "After merging, %d of %d people have no email and %d have no phone; "
            "%d have neither. That is not a parsing failure - source3 has no "
            "email column and source2 has no phone column, so a person seen "
            "only in one of those files is permanently missing one key." % (
                len(no_email), len(people), len(no_phone), len(neither)),
            "Recorded rather than patched. It sets the ceiling on what any "
            "future incremental load can match: a new CSV keyed on phone will "
            "silently create duplicates for the %d phone-less people, so the "
            "Task 2 duplicate-alert flow checks name+city as a fallback and "
            "raises a review item instead of trusting a clean miss." % len(no_phone),
            WARN, None, None, None)


def _audit_cross_source_skill_agreement(people, log):
    """Do the two skill columns agree where both exist?"""
    disagreements = []
    for person in people:
        by_source = defaultdict(set)
        for skill, sources in person["_skills"].items():
            for source in sources:
                by_source[source].add(skill)
        naukri = by_source.get("source1_naukri_applicants.csv")
        gig = by_source.get("source2_gig_workers.csv")
        if naukri and gig and naukri != gig:
            disagreements.append((person, sorted(naukri ^ gig)))

    if not disagreements:
        log.add("cross_source_agreement",
                "Every person present in both source1 and source2 lists exactly "
                "the same skills in both files, once casing is normalised "
                "('rest apis' vs 'REST APIs').",
                "Used as a positive control: it confirms the skill "
                "canonicalisation map is not silently dropping tokens, and it is "
                "corroborating evidence that the email/phone merges are correct - "
                "wrongly merged people would not agree on their skill lists.",
                INFO, None, None, "skills")
        return

    for person, diff in disagreements:
        log.add("cross_source_disagreement",
                "%s lists different skills in source1 and source2 (differ on: %s)" % (
                    person["full_name"], ", ".join(diff)),
                "Stored the union of both lists, with person_skills.sources "
                "recording which file claimed each skill",
                INFO, None, None, "skills", entity=person["full_name"])
