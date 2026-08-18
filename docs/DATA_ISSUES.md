# Task 4 — Data issues report

Every problem found in the three files, and what the pipeline does about it.

Nothing in this document is hand-counted. Every number comes from
`db/consultbae.db`, and the exhaustive backing tables are regenerated with:

```bash
python3 -m pipeline.run --report
```

which writes [`DATA_ISSUES_APPENDIX.md`](DATA_ISSUES_APPENDIX.md) (every issue,
in full) plus CSV/JSON exports in `reports/`.

**400 issues logged across 103 rows: 2 `error`, 53 `warn`, 345 `info`.**

Severity means:

| | |
| --- | --- |
| `error` | the row or field could not be trusted as written; the pipeline had to repair or reject it |
| `warn` | loaded, but with a flag on the record — a human should look |
| `info` | cosmetic inconsistency, standardised silently (but still logged) |

A guiding rule, applied everywhere below: **the pipeline flags rather than
fixes anything it cannot prove.** Deleting a suspicious row makes a report look
clean and makes the data worse.

---

## A. Structural problems — the shape of the file is wrong

These are handled before any field is parsed, because a row whose columns are in
the wrong order will otherwise produce six bogus "invalid format" errors and
then get thrown away for the wrong reason.

### 1. No identifier is shared by all three files `error`-class design problem

Not just "no common ID" — no *field* is even present in all three:

| | name | email | phone |
| --- | --- | --- | --- |
| source1 | ✅ | ✅ | ✅ |
| source2 | ✅ | ✅ | ❌ no phone column |
| source3 | ✅ | ❌ no email column | ✅ |

source2 and source3 therefore share **no key at all**. They can only be joined
transitively through source1.

**Action:** identity resolution is a union-find over three passes (email, phone,
then guarded name+city) so that `source2 → source1 → source3` composes into one
person. Full reasoning in [MATCHING.md](MATCHING.md).

### 2. `source2` row 12 — completely empty row `warn`

```
,,,,,
```

**Action:** skipped, logged. It carries no person, and letting it through would
create a nameless record that inflates every count.

### 3. `source2` row 20 — columns rotated out of place `error`

```csv
"react, javascript, mysql",ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG,Isha Chopra,1406/hr,Pune,active
```

The skill list is sitting in `email_id`, the email in `worker_name`, and so on —
the whole row is shifted one position. This is what an off-by-one write in an
export script produces.

**Action:** detected by scoring every column against a field-type validator
(does this look like an email? a rate? a status? a skill list?). As written the
row scores **0.0/6**; rotated left by one it scores **6.0/6**, so it is rotated
back and then parsed normally. Only cyclic rotations are tried — with six
columns there are 720 permutations and plenty would score well by accident, so
an exhaustive search would invent damage that is not there. The repair is
recorded in `source_records.repairs`.

Note what this is *not*: `if row_number == 20`. The check is generic, so the
same class of damage is caught in the next file too.

### 4. `source3` row 16 — the header repeated inside the data `error`

```csv
Name,Phone Number,City,Verified,Projects Completed
```

**Action:** dropped. Left alone it becomes a person named **"Name"** living in
city **"City"** with `Verified = "Verified"` — a record that looks plausible in a
count and poisons every aggregate downstream.

### 5. Permanent match-key gaps `warn`

After merging, **11 of 56 people have no phone** and **1 has no email** — not
because parsing failed, but because the file they came from has no such column.

**Action:** recorded as a known ceiling on future incremental loads. A new CSV
keyed on phone will silently create duplicates for those 11 people, which is
exactly why the Task 2 duplicate-alert flow falls back to name+city and raises a
review item rather than trusting a clean miss.

---

## B. Duplicates

### 6. `source1` rows 25 & 31 — same person, name abbreviated in one row `warn`

| row | name | email | phone |
| --- | --- | --- | --- |
| 25 | `R. Verma` | `rohit.verma13@mailtest.example.org` | `9000000294` |
| 31 | `Rohit Verma` | `rohit.verma13@mailtest.example.org` | `9000000294` |

**Action:** merged on the exact email. The display name is chosen by a rule that
prefers a fully spelled given name over an initial, so the person is stored as
**Rohit Verma**; `R. Verma` is kept in `field_conflicts` with the reason. Sorting
the two rows alphabetically or taking "the first one" would have kept the
degraded name.

### 7. `source1` rows 27 & 37 — same person, two different email addresses `warn`

| row | name | email | phone |
| --- | --- | --- | --- |
| 27 | `Nikhil Chopra` | `alt.nikhil.chopra70@example.com` | `09000000103` |
| 37 | `Nikhil Chopra` | `nikhil.chopra70@example.com` | `09000000103` |

This is the case that punishes email-only matching: the emails differ, so an
email-keyed dedupe leaves two people.

**Action:** merged on the exact **phone**, which is identical. The `alt.` prefix
is *noticed* and logged as an `alias_email` observation, but it is deliberately
**not** used as a merge key — "this address looks like a variant of that one" is
a hunch, and the phone match is proof. The primary (non-alias) address wins the
`email` field.

### 8. `source2` rows 7 & 20 — the same person twice `warn`

Once the rotation in row 20 is repaired, it is byte-for-byte identical to row 7.

**Action:** both rows retained in `source_records` for audit, collapsed into one
person. Logged as both `scrambled_columns` and `exact_duplicate_row` — the
scramble is *why* a naive loader would have missed the duplicate.

### 9. 29 people appear in more than one file

15 in all three files, 14 in two.

**Action:** collapsed from 103 CSV rows into **56 people**. Every one of the 47
absorbed rows is still in `source_records` with its original JSON and a foreign
key to the person it fed, so any merge can be audited or reversed.

---

## C. Identity traps — where merging goes wrong

### 10. Three different people named "Arjun Mehta", all in Noida `warn`

| person | files | phone | email |
| --- | --- | --- | --- |
| #5 | source2 | — | `arjun.mehta77@mailtest.example.org` |
| #6 | source1 + source3 | `9000000131` | `arjun.mehta9@example.in` |
| #56 | source3 | `9000000272` | — |

Matching on name + city — which is the only key #5 and #56 share with anything —
would fuse all three into one person and delete two people's project history.

**Action:** **not merged.** The weak pass refuses to fire when a (name, city)
pair points at three or more clusters, and all three go to the `merge_review`
table for a human. This is the single most important decision in the pipeline:
the "clean" outcome here is the wrong one.

### 11. Two different people named "Deepak Nair" `info`

`deepak.nair44@example.com` (Bengaluru, in all three files) and
`deepak.nair57@example.in` (Delhi, source2 only).

**Action:** kept separate. Different city *and* different email; conflicting
strong keys override a name match.

---

## D. Format inconsistency — same meaning, many spellings

All silently standardised, all logged. These are `info` because no information
is at risk — but leaving any of them alone splits one person into two records,
which is why they matter.

### 12. Phone numbers — four spellings, 47 rows `info`

`+919000000254` · `9000000237` · `09000000287` · `+91-9000000131`

**Action:** reduced to a bare 10-digit national number (strip non-digits, drop
`00`/`+91` country code and trunk `0`, then require 10 digits starting 6–9).
Without this, source1's `09000000103` and source3's `+91-9000000103` never match
and the transitive join through source1 collapses.

### 13. Email casing — 10 rows in `source2` `info`

`ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG` vs `isha.chopra95@mailtest.example.org`

**Action:** lowercased. Strictly, RFC 5321 says the local part *is*
case-sensitive; no real provider treats it that way, and honouring the spec here
would split Isha Chopra into two people. The trade-off is noted in the code.

### 14. Name casing — 9 rows in `source3` `info`

`RITU SHARMA`, `MANISH BHATIA`, `SAHIL MALHOTRA` …

**Action:** re-cased to Title Case for display; matching uses a
punctuation-stripped lowercase key so casing never affects a merge.

### 15. Cities — 60 value variants + 14 trailing-space rows `info`

`Bengaluru` / `bangalore` / `Bangalore` · `GURGAON` / `Gurugram` / `gurugram␣` ·
`new delhi` / `New Delhi` / `Delhi` · `Noida␣` / `NOIDA`

**Action:** mapped through one canonical city list in `config.py`. Trailing
whitespace is a separate logged category because `"Noida "` and `"Noida"` are a
different bug from `"NOIDA"` — one is a data-entry artefact, the other a
convention difference.

### 16. `Verified` — five spellings of a boolean, 18 rows `info`

`Y` · `yes` · `Yes` · `N` · `No`

**Action:** stored as `0/1`. Unrecognised values would be rejected to NULL, not
guessed — there were none.

### 17. `status` — four spellings across three states, 22 rows `info`

`Active` / `active` / `ACTIVE` / `Inactive` / `paused`

**Action:** lowercased into an enum, enforced by a `CHECK` constraint in the
schema so bad values can never enter the table later.

### 18. Skill lists — different casing per file, 31 rows `info`

source1 writes `REST APIs`, source2 writes `rest apis`.

**Action:** canonicalised through one skill map, so both become a single row in
`skills` instead of two. Skills are stored many-to-many with the file each claim
came from (`person_skills.sources`).

Worth noting as a **positive** finding: after canonicalisation, every person
present in both source1 and source2 lists *exactly* the same skills in both
files. That is corroborating evidence that the email/phone merges are correct —
wrongly merged people would not agree on their skill lists.

### 19. Applied dates — four formats in one column, 33 rows `info`

`24-07-2026` · `2026-08-08` · `07/13/2026` · `7 Jul 2026`

**Action:** four parsers. The interesting part is the convention:

- dash dates → `dd-mm-yyyy`
- slash dates → `mm/dd/yyyy`

That is **not a guess.** Across the whole column, every dash date with a
component > 12 has it in position 1 (`24-07`, `21-08`, `28-07` …) and every slash
date with a component > 12 has it in position 2 (`07/13`, `08/19`, `08/21` …).
The two separators carry two different conventions *consistently*, which is what
you get when one upstream system exports Indian format and another exports US
format. `tests/test_normalize.py::test_date_separator_convention_holds_on_real_data`
asserts this property against the real file, so if a future file breaks the
assumption the test suite fails instead of the dates quietly being wrong.

---

## E. Value and unit problems — the number itself is wrong

### 20. `Current CTC` holds two different units in one column `warn` ×21

21 of the 42 rows are absolute rupees (`417964`, `1195422`), the other 21 are
lakhs per annum (`4.2`, `11.9`). Reading the column as one unit makes Amit
Agarwal earn ₹4.20 a year.

**Action:** values below 1000 are read as lakhs and multiplied by 1e5. This is
safe because the two ranges do not overlap anywhere in the file — the smallest
absolute value is `327287` and the largest lakh value is `11.9`, so no value has
two competing readings. `people.ctc_unit_detected` records which reading was
applied, so the conversion is auditable rather than baked in.

### 21. `rate` holds two different units — and the unit predicts the number `warn`

17 rows quote an hourly rate (`1415/hr`), 14 quote a monthly one (`15k/month`).

Converting at 160 billable hours/month is the obvious move, and it produces a
result that should stop you:

| quoted as | median monthly equivalent |
| --- | --- |
| per hour | **₹140,800** |
| per month | **₹55,500** |

A **2.5× gap between two groups doing the same work.** If both columns meant the
same thing, the medians would be similar. They are not, so `1415/hr` and
`15k/month` are not two spellings of one quantity — either the hourly cohort is
part-time, or the column mixes two pricing concepts (a billable rate vs a
retainer).

**Action:** the pipeline **refuses to pick one truth.** Both `rate_hourly_inr`
and `rate_monthly_inr` are stored, `rate_basis_raw` records which one the worker
actually quoted, and the 160 h/month figure lives in `config.py` labelled as an
assumption. This is a question for whoever owns the column upstream, not
something a merge script should silently decide. 7 further rows are flagged as
being >3× off the corpus median.

### 22. Eight dates that are genuinely undecidable `warn`

`01-08-2026` · `03-07-2026` · `02-06-2026` · `07/03/2026` ×3 · `07/12/2026` ·
`08/11/2026` — both components ≤ 12, so the value alone cannot say which is the
month.

**Action:** parsed with the separator convention proven in #19, and flagged with
`applied_date_ambiguous = 1` so nobody builds a "applications per day" chart on
them without knowing.

### 23. Five applied dates in the future `warn`

`21-08-2026`, `22-08-2026`, `08/19/2026`, `2026-08-19`, `08/21/2026` — all after
the ingest date of `2026-08-18`. An application cannot be submitted in the
future.

**Action:** kept (rejecting them would lose five candidates) and flagged
`applied_date_is_future = 1`. Most likely a day/month swap at source — note that
four of the five are within days of the cutoff, which is the signature of a
timezone or export-boundary bug rather than random corruption. The ingest date is
configurable (`CONSULTBAE_INGEST_DATE`) so this check is deterministic and does
not change meaning when the pipeline is re-run next year.

### 24. "Delhi NCR" is a region, not a city `warn` ×3

source1 rows 18 and 36, source3 row 7.

**Action:** mapped to `Delhi` but flagged `city_is_region_guess = 1`, because
Gurugram and Noida are *also* inside Delhi NCR — collapsing the label into
"Delhi" claims precision the source never had. `city_region` is derived from the
winning city rather than picked independently, so a person can never end up with
city `Delhi` flagged as a region guess from a row that said `Delhi NCR`.

### 25. 100% of email addresses are undeliverable `info`

All 55 emails sit on `example.com`, `example.in` and `mailtest.example.org` —
domains reserved for documentation and testing by RFC 2606. Nothing sent to them
will ever arrive.

**Action:** loaded as-is (the assignment says the data is fictional), but
flagged, because this is the check that stops an automation from firing 56
bounces on day one. The Task 2 duplicate-alert flow refuses to send to a
reserved domain.

### 26. Eight contacts have 10+ completed projects but are marked unverified `warn`

Vikram Saxena (15), Sneha Chopra (14), Tanvi Gupta (14), Priya Saxena (12),
Arjun Mishra (10), Karan Chopra (10), Neha Bhatia (10), Rahul Chopra (10).

**Action:** loaded as given — a merge script does not get to decide who is
verified. Reported because `Verified` and `Projects Completed` disagreeing this
often is a *process* bug (verification being skipped after the first project),
not a bad cell, and it is invisible until you look at the two columns together.

### 27. CTC that does not fit the stated experience `info` ×3

Nikhil Chopra: ₹780,000 against 0.8 years. Kavya Mehta: ₹240,000 against 5.1
years. Priya Nair: ₹366,311 against 5.6 years.

**Action:** flagged, not corrected. Either the CTC unit was misread upstream or
the experience figure is wrong; both readings are plausible from the row alone,
so guessing would be inventing data. Detected by comparing against the corpus
median rather than a hardcoded band, so the check survives new data.

### 28. Rate outliers, 7 rows `info`

Kavya Verma (₹237k/month equiv), Deepak Nair (₹234k), Varun Jain (₹226k), Isha
Chopra (₹225k) at the top; Isha Kapoor (₹15k), Pooja Kapoor (₹21k), Vikram Mehta
(₹22k) at the bottom.

**Action:** flagged as >3× off the median in either direction. Note that the
split falls exactly along the unit boundary from #21 — every high outlier quoted
hourly, every low one quoted monthly. The outliers are a symptom of the unit
problem, not seven independent typos.

---

## What was deliberately *not* done

| Tempting fix | Why it was rejected |
| --- | --- |
| Merge `alt.nikhil.chopra70@` with `nikhil.chopra70@` because the addresses look related | A prefix resemblance is a hunch. The identical phone number is proof, and it produces the same merge — so use the proof. Applied to real data, `alt.` stripping would eventually merge two genuinely different people. |
| Merge the three "Arjun Mehta" records to get a cleaner people count | It would destroy two people's history. A lower headline number with 3 review items is the correct output. |
| Pick one unit for `rate` and convert everything to it | The unit correlates with the magnitude (#21), so any single conversion is knowingly wrong for one of the two cohorts. Store both, flag it, escalate. |
| Delete rows with future applied dates or implausible CTC | Those are real candidates with one bad field. Flag the field, keep the person. |
| Fold `Delhi NCR` into `Delhi` silently | Gurugram and Noida are in NCR too. The label is less precise than a city name and is stored as such. |
| Drop the 47 duplicate CSV rows after merging | They are the audit trail. `source_records` keeps every original row as JSON, which is what makes an incorrect merge recoverable instead of permanent. |
