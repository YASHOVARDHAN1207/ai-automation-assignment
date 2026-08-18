# How people are matched across the three files

The assignment's one-line warning — *"no single ID field is common across all
files"* — understates it. No *field* is common across all files either.

| | name | email | phone |
| --- | --- | --- | --- |
| `source1_naukri_applicants.csv` | ✅ | ✅ | ✅ |
| `source2_gig_workers.csv` | ✅ | ✅ | ❌ |
| `source3_cbnexus_contacts.csv` | ✅ | ❌ | ✅ |

So source2 ∩ source3 = {name, city}, and name is not an identifier — this data
has three people called "Arjun Mehta" in the same city.

## Why union-find

The only reliable path from a gig worker to a CRM contact runs *through* the
applicant file:

```
source2 row  ──email──►  source1 row  ──phone──►  source3 row
(no phone)                (has both)               (no email)
```

Matching has to be **transitive**: A links to B on email, B links to C on phone,
therefore A, B and C are one person even though A and C share nothing. That is
the definition of a connected component, so the matcher is a union-find
(disjoint-set) over record IDs. Pairwise "is A the same as B" comparison cannot
express it without re-scanning after every merge.

15 of the 56 people are assembled this way — found in all three files, joined by
a chain no single comparison would find.

Two implementation details that matter:

- The union always keeps the lexicographically smaller root, so cluster identity
  is **deterministic** regardless of the order rows arrive in. Combined with
  sorting people by a stable key before assigning `person_id`, re-running the
  pipeline on the same input produces byte-identical IDs. Task 3 stores
  `person_id` as a foreign key, so IDs that shuffled on every rebuild would
  silently corrupt the audio submissions.
- Every union records the method that caused it, so `people.match_methods` can
  say `exact_email,exact_phone` or `name_city_weak` and consumers can filter on
  it.

## The three passes

| Pass | Key | Confidence | Result |
| ---- | --- | ---------- | ------ |
| 1 | normalised email | 1.0 | auto-merge |
| 2 | normalised 10-digit phone | 1.0 | auto-merge |
| 3 | name + canonical city | 0.6 | merge only under guards, else review queue |

Normalisation has to happen **before** matching or the strong passes silently
fail:

- `ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG` and `isha.chopra95@mailtest.example.org`
  are one person, so email is lowercased.
- `09000000103` (source1) and `+91-9000000103` (source3) are one phone, so both
  reduce to `9000000103`.

Get either wrong and the transitive bridge in the diagram above breaks — which is
why the phone normaliser has its own unit tests rather than being trusted.

`match_confidence` on a person is the **minimum** confidence of the links used to
build it, not an average. A person assembled from one strong and one weak link is
only as trustworthy as its weakest join.

## Pass 3 and its three guards

Pass 3 exists because 5 people appear only in source2 and source3 and would
otherwise stay permanently split. It is also the pass that can destroy data, so
it fires only when all three guards hold.

**G1 — not already merged.** The two records must be in different clusters after
the strong passes.

**G2 — the name must be unambiguous.** The (name, city) pair must point at
exactly **two** clusters. Three or more means the name identifies nobody:

| "Arjun Mehta", Noida | files | phone | email |
| --- | --- | --- | --- |
| person #5 | source2 | — | `arjun.mehta77@mailtest.example.org` |
| person #6 | source1 + source3 | `9000000131` | `arjun.mehta9@example.in` |
| person #56 | source3 | `9000000272` | — |

All three go to `merge_review` and none are merged. A pipeline that merged them
would report a *lower, cleaner* people count while having deleted two people's
project history — the failure mode that looks like success.

**G3 — no contradictory strong key.** If both clusters have an email and the
emails differ, or both have a phone and the phones differ, that is positive
evidence of two humans and it overrides the name match. This is what keeps the
two `Deepak Nair`s apart.

Note the asymmetry: a *missing* key is not evidence of anything, but two
*different* keys are proof. G3 only fires on contradiction, never on absence.

### What pass 3 actually produced

| person | files | why no strong key could ever have matched them |
| --- | --- | --- |
| Divya Chopra (Noida) | source2 + source3 | source2 has her email, source3 has her phone, neither file has both |
| Karan Chopra (Pune) | source2 + source3 | same |
| Manish Bhatia (Noida) | source2 + source3 | same |
| Vikram Mehta (Pune) | source2 + source3 | same |

4 merges applied at confidence 0.6, 3 records queued for a human, 0 wrong
merges. Each of those 4 is a genuine gap in the source schemas rather than a
guess about similar-looking strings.

### Where this would need more work at real scale

Name + city is a blocking key that happens to be sufficient for 56 people. At
50,000 it would not be:

- Common name + big city (e.g. "Rahul Sharma" + Delhi) would collide constantly,
  and G2 would refuse almost every merge — correct, but useless.
- The next step would be scoring rather than a boolean: fuzzy name distance,
  skill-list overlap (already available and already shown to agree perfectly
  across sources for correctly-matched people), rate band, and applied-date
  proximity, combined into a score with a review band in the middle.
- The `merge_review` table is deliberately shaped for that: it already carries a
  `score` column and a `resolved` flag.

## Choosing between values that disagree

Once rows are one person, shared fields need one winner. Every loser is written
to `field_conflicts` with the rule that decided it, so no overwrite is silent.

| Field | Rule | Why |
| --- | --- | --- |
| `full_name` | prefer a fully spelled name over an initial, then the longest | `Rohit Verma` beats `R. Verma`; "first row wins" would have kept the degraded one |
| `email` | prefer the primary (non-alias) address, then source trust | `nikhil.chopra70@` beats `alt.nikhil.chopra70@` |
| `phone` | source trust order | only source1 and source3 have phones and they agree wherever both exist |
| `city` | majority vote, then prefer a city name over a region label, then source trust | see below |
| everything else | only one file supplies it, so no conflict is possible | recorded in `EXCLUSIVE_FIELDS` |

Source trust order is `naukri > cbnexus > gig`:

1. **source1** is a self-submitted application form — the candidate typed it
   themselves, so it is the most deliberate.
2. **source3** is a staff-maintained CRM — curated, but second-hand.
3. **source2** is an ops sheet, and its `location` column is the least curated of
   the three (it is the only file with trailing-whitespace *and* casing variants
   in the same column).

`city_region` and `city_is_region_guess` are **derived from the winning city**
rather than voted on independently. Picking them separately allowed a person to
end up with city `Delhi` while carrying a region-guess flag inherited from a row
that said `Delhi NCR` — the two fields have to be answered by the same row.

## Verifying the merges are actually right

There is no ground truth to check against, so the pipeline uses corroboration:

- **Skill agreement.** Every person present in both source1 and source2 lists
  exactly the same skills in both files once casing is normalised. Wrongly
  merged people would not agree on their skill lists, so this is independent
  evidence that the email/phone joins are sound. It is asserted in the test
  suite, not just observed once.
- **Uniqueness constraints.** `people.email` and `people.phone` carry unique
  indexes. If matching ever left the same email on two people, the load would
  fail loudly instead of producing a quietly duplicated table.
- **Reversibility.** `source_records` keeps all 103 original rows as JSON with a
  foreign key to the person each fed, so any merge can be inspected or undone
  after the fact.
