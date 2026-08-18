"""Identity resolution: decide which rows across the three files are one human.

No field is shared by all three files:

    source1  name, email, phone
    source2  name, email          (no phone column)
    source3  name,        phone   (no email column)

So source2 and source3 can never be joined directly. They are joined
*transitively* through source1, which holds both keys - source1 acts as the hub
that links a gig-worker email to a CRM phone number. That is the whole trick of
Task 1, and it is why a union-find (connected components) is the right structure
rather than pairwise merging: the link source2 -> source1 -> source3 has to
compose.

Matching runs in three passes, strongest first:

    1. exact normalised email     -> strong, auto-merge
    2. exact normalised phone     -> strong, auto-merge
    3. name + city                -> weak, auto-merge ONLY under the guards in
                                     `weak_pass`, otherwise queued for human
                                     review in the merge_review table

Pass 3 exists because five people appear only in source2 and source3, which
share no key at all. Pass 3 is also where a naive pipeline destroys data: this
dataset contains three different humans called "Arjun Mehta", all in Noida.
Blindly merging on name+city would fuse them into one person. The guards catch
that and it stays a review item instead.
"""
from collections import Counter, OrderedDict, defaultdict

from . import config
from .normalize import ERROR, INFO, WARN, names_compatible

STRONG = "strong"
WEAK = "weak"

CONFIDENCE = {"exact_email": 1.0, "exact_phone": 1.0, "name_city_weak": 0.6}


class UnionFind(object):
    def __init__(self):
        self.parent = {}
        self.links = defaultdict(list)   # root -> [(a, b, method)]

    def add(self, item):
        self.parent.setdefault(item, item)

    def find(self, item):
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:      # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a, b, method):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        # Keep the lexicographically smaller root so cluster identity is
        # deterministic across runs regardless of input order.
        if rb < ra:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.links[ra].extend(self.links.pop(rb, []))
        self.links[ra].append((a, b, method))
        return True

    def clusters(self):
        groups = defaultdict(list)
        for item in self.parent:
            groups[self.find(item)].append(item)
        return groups


# ---------------------------------------------------------------------------
# passes
# ---------------------------------------------------------------------------

def strong_passes(records, uf, log):
    by_uid = {r.uid: r for r in records}
    for rec in records:
        if rec.usable:
            uf.add(rec.uid)

    for key_name, method in (("email", "exact_email"), ("phone", "exact_phone")):
        buckets = defaultdict(list)
        for rec in records:
            if not rec.usable:
                continue
            value = rec.fields.get(key_name)
            if value:
                buckets[value].append(rec)
        for value, group in buckets.items():
            if len(group) < 2:
                continue
            anchor = group[0]
            for other in group[1:]:
                if uf.union(anchor.uid, other.uid, method):
                    if anchor.source != other.source:
                        log.add("cross_source_match",
                                "Same %s %s appears in %s row %d and %s row %d" % (
                                    key_name, value,
                                    config.SOURCE_LABELS[anchor.source], anchor.row_number,
                                    config.SOURCE_LABELS[other.source], other.row_number),
                                "Merged into one person on an exact %s match" % key_name,
                                INFO, other.source, other.row_number, key_name, value,
                                entity=other.fields.get("full_name"))
    return by_uid


def weak_pass(records, uf, log):
    """name + city matching, with three guards.

    A weak merge is allowed only when ALL of these hold:

      G1 the two records are in different clusters after the strong passes
      G2 the shared (name, city) points at exactly two clusters in the whole
         corpus - if a name+city combination covers three clusters we cannot
         know which two belong together, so none of them are merged
      G3 the two clusters do not hold contradictory strong keys: two different
         emails, or two different phones, is positive proof of two people

    Returns a list of review candidates that were rejected.
    """
    reviews = []
    usable = [r for r in records if r.usable and r.name_key and r.fields.get("city")]

    # Snapshot the post-strong-pass state ONCE. Every decision below is taken
    # against this snapshot and the unions are applied afterwards, so a merge
    # made early in the loop cannot invalidate the roots the later iterations
    # were reasoning about.
    cluster_members = uf.clusters()
    by_uid = {r.uid: r for r in records}
    representative = {}
    index = defaultdict(set)
    for rec in usable:
        root = uf.find(rec.uid)
        index[(rec.name_key, rec.fields["city"])].add(root)
        representative.setdefault(root, rec)

    merges = []

    def cluster_keys(root, field):
        return set(
            by_uid[uid].fields.get(field)
            for uid in cluster_members.get(root, [])
            if by_uid[uid].fields.get(field)
        )

    for (name_key, city), roots in sorted(index.items()):
        if len(roots) < 2:
            continue

        roots = sorted(roots)
        if len(roots) > 2:                                          # G2
            for root in roots:
                reviews.append(OrderedDict([
                    ("reason", "ambiguous_name_city"),
                    ("name_key", name_key),
                    ("city", city),
                    ("cluster_root", root),
                    ("cluster_size", len(cluster_members.get(root, []))),
                    ("score", 0.0),
                    ("detail",
                     "%d distinct clusters share the name %r in %s, so a "
                     "name+city merge cannot tell them apart" % (len(roots), name_key, city)),
                ]))
            log.add("ambiguous_identity",
                    "%d separate people share the name %r in %s" % (len(roots), name_key, city),
                    "NOT merged. Queued in the merge_review table for a human. "
                    "Merging them would have silently invented one person out of "
                    "%d and destroyed the others' history" % len(roots),
                    WARN, None, None, "full_name", name_key)
            continue

        a, b = roots
        emails_a, emails_b = cluster_keys(a, "email"), cluster_keys(b, "email")
        phones_a, phones_b = cluster_keys(a, "phone"), cluster_keys(b, "phone")

        if (emails_a and emails_b and not (emails_a & emails_b)) or \
           (phones_a and phones_b and not (phones_a & phones_b)):   # G3
            reviews.append(OrderedDict([
                ("reason", "same_name_conflicting_strong_key"),
                ("name_key", name_key),
                ("city", city),
                ("cluster_root", "%s | %s" % (a, b)),
                ("cluster_size", len(cluster_members.get(a, [])) + len(cluster_members.get(b, []))),
                ("score", 0.0),
                ("detail",
                 "Same name and city but different %s (%s vs %s) - that is "
                 "evidence of two people, not one" % (
                     "emails" if emails_a and emails_b else "phones",
                     ",".join(sorted(emails_a or phones_a)),
                     ",".join(sorted(emails_b or phones_b)))),
            ]))
            log.add("homonym_kept_separate",
                    "Two records share the name %r in %s but carry different "
                    "contact keys" % (name_key, city),
                    "Kept as two people. Conflicting email/phone is proof of two "
                    "humans, so the name collision is ignored",
                    INFO, None, None, "full_name", name_key)
            continue

        merges.append((representative[a], representative[b], name_key))

    # Apply the decisions taken against the snapshot.
    for rec_a, rec_b, name_key in merges:
        uf.union(rec_a.uid, rec_b.uid, "name_city_weak")
        log.add("weak_match_applied",
                "%s (%s row %d) and %s (%s row %d) share a name and city and "
                "have no key in common - source2 has no phone column and "
                "source3 has no email column, so there is no strong key that "
                "could ever link them" % (
                    rec_a.fields.get("full_name"), config.SOURCE_LABELS[rec_a.source], rec_a.row_number,
                    rec_b.fields.get("full_name"), config.SOURCE_LABELS[rec_b.source], rec_b.row_number),
                "Merged at confidence %.1f (vs 1.0 for an exact-key match) and "
                "recorded in people.match_methods, so anything built on top can "
                "choose to ignore weak merges" % CONFIDENCE["name_city_weak"],
                WARN, rec_b.source, rec_b.row_number, "full_name", name_key,
                entity=rec_b.fields.get("full_name"))

    # Report every remaining name collision - not merged, but worth knowing
    # about. Recomputed from the post-merge state, not the snapshot.
    final_members = uf.clusters()
    by_name = defaultdict(set)
    for rec in usable:
        by_name[rec.name_key].add(uf.find(rec.uid))
    for name_key, roots in sorted(by_name.items()):
        if len(roots) > 1:
            cities = sorted(set(
                by_uid[uid].fields.get("city")
                for root in roots for uid in final_members.get(root, [])
                if by_uid[uid].fields.get("city")))
            log.add("homonym",
                    "The name %r belongs to %d different people (cities: %s)" % (
                        name_key, len(roots), ", ".join(cities)),
                    "Left as %d separate people. This is why name is never used "
                    "as a match key on its own" % len(roots),
                    INFO, None, None, "full_name", name_key)

    return reviews


# ---------------------------------------------------------------------------
# golden record construction
# ---------------------------------------------------------------------------

# Fields only one source can supply -> no cross-source conflict is possible.
EXCLUSIVE_FIELDS = {
    "naukri": ["experience_years", "ctc_annual_inr", "ctc_unit_detected",
               "ctc_out_of_range", "applied_date", "applied_date_ambiguous",
               "applied_date_is_future"],
    "gig": ["rate_hourly_inr", "rate_monthly_inr", "rate_basis_raw",
            "rate_out_of_range", "gig_status"],
    "cbnexus": ["is_verified", "projects_completed"],
}

# Fields more than one source can supply -> a winner has to be chosen and the
# loser recorded. city_region and city_is_region_guess are deliberately NOT in
# this list: they are properties *of the chosen city*, so picking them
# independently could label a person's city "Delhi" while flagging it as a
# region guess sourced from a row that said "Delhi NCR". They are derived from
# the winning city instead (see build_people).
SHARED_FIELDS = ["full_name", "email", "phone", "city"]


def _name_score(rec):
    """Higher is a better display name: full given names beat initials."""
    tokens = rec.fields.get("name_tokens") or []
    return (
        0 if rec.fields.get("name_is_abbreviated") else 1,
        len(tokens),
        len(rec.fields.get("full_name") or ""),
    )


def _pick_shared(field, members, conflicts, person_ref):
    """Choose one value for a field several sources disagree about."""
    candidates = [(rec, rec.fields.get(field)) for rec in members
                  if rec.fields.get(field) not in (None, "")]
    if not candidates:
        return None

    distinct = OrderedDict()
    for rec, value in candidates:
        distinct.setdefault(value, []).append(rec)

    if field == "full_name":
        chosen = max(candidates, key=lambda cv: _name_score(cv[0]))[1]
        rule = "prefer the fully spelled name over an initial, then the longest"
    elif field == "email":
        # Prefer a non-alias address, then source priority.
        chosen = min(
            candidates,
            key=lambda cv: (
                1 if cv[0].fields.get("email_alias_of") else 0,
                config.FIELD_SOURCE_PRIORITY.index(cv[0].source),
            ))[1]
        rule = "prefer the primary (non-alias) address, then source priority"
    elif field == "city":
        # Majority vote, then prefer a real city over a region label, then
        # source priority.
        votes = Counter(v for _, v in candidates)
        top = max(votes.values())
        tied = [v for v in distinct if votes[v] == top]
        if len(tied) == 1:
            chosen, rule = tied[0], "majority vote across sources (%d/%d)" % (top, len(candidates))
        else:
            def city_rank(value):
                recs = distinct[value]
                region_guess = all(r.fields.get("city_is_region_guess") for r in recs)
                priority = min(config.FIELD_SOURCE_PRIORITY.index(r.source) for r in recs)
                return (1 if region_guess else 0, priority)
            chosen = min(tied, key=city_rank)
            rule = ("tie broken by preferring a city name over a region label, "
                    "then by source trust order %s" % " > ".join(config.FIELD_SOURCE_PRIORITY))
    else:
        chosen = min(candidates,
                     key=lambda cv: config.FIELD_SOURCE_PRIORITY.index(cv[0].source))[1]
        rule = "source trust order %s" % " > ".join(config.FIELD_SOURCE_PRIORITY)

    if len(distinct) > 1:
        chosen_sources = sorted(set(r.source for r in distinct[chosen]))
        for value, recs in distinct.items():
            if value == chosen:
                continue
            conflicts.append(OrderedDict([
                ("person_ref", person_ref),
                ("field", field),
                ("chosen_value", str(chosen)),
                ("chosen_source", ",".join(config.SOURCE_LABELS[s] for s in chosen_sources)),
                ("rejected_value", str(value)),
                ("rejected_source", ",".join(
                    config.SOURCE_LABELS[s] for s in sorted(set(r.source for r in recs)))),
                ("rule", rule),
            ]))
    return chosen


def build_people(records, uf, log):
    """Collapse clusters into golden records.

    Returns (people, conflicts). Each person is an OrderedDict ready for the
    loader, with `_members` carrying the SourceRecords behind it.
    """
    by_uid = {r.uid: r for r in records}
    clusters = uf.clusters()
    conflicts = []
    people = []

    for root, uids in clusters.items():
        members = sorted((by_uid[uid] for uid in uids),
                         key=lambda r: (config.FIELD_SOURCE_PRIORITY.index(r.source), r.row_number))
        ref = members[0].uid
        person = OrderedDict()

        for field in SHARED_FIELDS:
            person[field] = _pick_shared(field, members, conflicts, ref)

        # Derived from the winning city so the two can never contradict it.
        chosen_city = person.get("city")
        city_donors = [r for r in members if r.fields.get("city") == chosen_city]
        person["city_region"] = config.CITY_REGION.get(chosen_city)
        person["city_is_region_guess"] = bool(
            city_donors and all(r.fields.get("city_is_region_guess") for r in city_donors))

        for source, fields in EXCLUSIVE_FIELDS.items():
            same_source = [r for r in members if r.source == source]
            for field in fields:
                values = [r.fields.get(field) for r in same_source
                          if r.fields.get(field) not in (None, "")]
                if not values:
                    person[field] = None
                    continue
                person[field] = values[0]
                for other in values[1:]:
                    if other != values[0]:
                        conflicts.append(OrderedDict([
                            ("person_ref", ref),
                            ("field", field),
                            ("chosen_value", str(values[0])),
                            ("chosen_source", config.SOURCE_LABELS[source] + " (first row)"),
                            ("rejected_value", str(other)),
                            ("rejected_source", config.SOURCE_LABELS[source] + " (later row)"),
                            ("rule", "two rows of the same file disagree; kept the "
                                     "earlier row and logged the difference"),
                        ]))

        methods = sorted(set(m for _, _, m in uf.links.get(root, [])))
        person["source_count"] = len(set(r.source for r in members))
        person["record_count"] = len(members)
        person["sources"] = ",".join(sorted(set(config.SOURCE_LABELS[r.source] for r in members)))
        person["match_methods"] = ",".join(methods) if methods else "single_source"
        person["match_confidence"] = min([CONFIDENCE[m] for m in methods]) if methods else 1.0
        person["first_seen_source"] = config.SOURCE_LABELS[members[0].source]
        person["_members"] = members
        person["_skills"] = _merge_skills(members)
        person["_ref"] = ref
        people.append(person)

    # Deterministic ordering -> stable person_id across re-runs, which matters
    # because the audio app (Task 3) stores person_id as a foreign key.
    people.sort(key=lambda p: (
        (p.get("email") or "~"), (p.get("phone") or "~"),
        (p.get("full_name") or "~"), p["_ref"]))
    for index, person in enumerate(people, start=1):
        person["person_id"] = index

    for person in people:
        if person["source_count"] == 3:
            log.add("three_way_match",
                    "%s was found in all three files" % person["full_name"],
                    "Collapsed %d rows into one person via %s" % (
                        person["record_count"], person["match_methods"]),
                    INFO, None, None, None, None, entity=person["full_name"])

    return people, conflicts


def _merge_skills(members):
    """Union the two skill columns, remembering which file each came from."""
    out = OrderedDict()
    for rec in members:
        for skill in rec.skills:
            out.setdefault(skill, set()).add(config.SOURCE_LABELS[rec.source])
    return OrderedDict((skill, sorted(sources)) for skill, sources in out.items())


def resolve(records, log):
    uf = UnionFind()
    strong_passes(records, uf, log)
    reviews = weak_pass(records, uf, log)
    people, conflicts = build_people(records, uf, log)
    return people, conflicts, reviews, uf
