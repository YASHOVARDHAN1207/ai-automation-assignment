# ConsultBae — AI Automation Assignment

Merging three messy people-databases into one, automating on top of it, and a
small audio-collection app.

| Task | What it is | Where |
| ---- | ---------- | ----- |
| 1 | Merge 3 CSVs into one clean database | `pipeline/`, `db/schema.sql` |
| 2 | Automation in a no-code tool (n8n) | [`automation/n8n/`](automation/n8n/README.md) |
| 3 | Mini audio collection app | [`app/`](docs/AUDIO_APP.md) |
| 4 | Data issues report | [docs/DATA_ISSUES.md](docs/DATA_ISSUES.md) |
| 5 | Scale write-up (stretch) | [docs/SCALE_5000.md](docs/SCALE_5000.md) |

**The stuck log the brief asks for is [docs/STUCK_LOG.md](docs/STUCK_LOG.md)**, and
the short version is [further down this page](#stuck-log).

---

## Quickstart

**Tasks 1 & 4 — the pipeline.** No dependencies; Python 3.8+ and its standard
library are enough.

```bash
python3 -m pipeline.run --report
```

That rebuilds `db/consultbae.db` from the three CSVs in `data/`, prints a
summary, and exports the data-issues report.

**Task 3 — the audio app.** Needs Flask and numpy.

```bash
make venv && make app        # then open http://127.0.0.1:5055
```

**Task 2 — the n8n flows.** Import the JSON from `automation/n8n/` and point
`CONSULTBAE_BASE_URL` at the running app; full setup in
[automation/n8n/README.md](automation/n8n/README.md). To check the API the flows
depend on without opening n8n:

```bash
python3 scripts/replay_flows.py
```

**Everything.**

```bash
make test                    # 128 tests
sqlite3 db/consultbae.db "SELECT * FROM v_people_full LIMIT 5;"
```

## Documents

| | |
| --- | --- |
| [docs/DATA_ISSUES.md](docs/DATA_ISSUES.md) | **Task 4.** All 28 problem classes found in the three files, and what was done about each |
| [docs/DATA_ISSUES_APPENDIX.md](docs/DATA_ISSUES_APPENDIX.md) | Generated from the database: every issue in full, with counts |
| [docs/MATCHING.md](docs/MATCHING.md) | How people are matched, and where the matching would break at 50k rows |
| [docs/AUDIO_APP.md](docs/AUDIO_APP.md) | **Task 3.** What is measured, how, and the three decisions worth arguing about |
| [automation/n8n/README.md](automation/n8n/README.md) | **Task 2.** Both flows, node by node, and why each guard exists |
| [docs/SCALE_5000.md](docs/SCALE_5000.md) | **Task 5.** What breaks first at 5,000 workers, with measurements |
| [docs/STUCK_LOG.md](docs/STUCK_LOG.md) | Where I got stuck, what I searched, what I rejected |

## What the run produces

```
CSV rows read           : 103
CSV rows usable         : 103
people after merge      : 56
rows collapsed away     : 47
data issues logged      : 400  (error 2 / warn 53 / info 345)
field conflicts         : 2
merges left for a human : 3
```

15 people appear in all three files, 14 in two, 27 in only one.

## The matching problem

There is no shared ID, and worse, no field is even *present* in all three
files:

| | name | email | phone |
| --- | --- | --- | --- |
| `source1_naukri_applicants.csv` | ✅ | ✅ | ✅ |
| `source2_gig_workers.csv` | ✅ | ✅ | ❌ |
| `source3_cbnexus_contacts.csv` | ✅ | ❌ | ✅ |

So source2 and source3 share **no key at all** and can never be matched
directly. They get linked *through* source1, which is the only file holding
both an email and a phone:

```
source2 (email) ──┐
                  ├──► source1 ──┐
source3 (phone) ──┘              └──► one person
```

That transitivity is why matching is implemented as a union-find over three
passes rather than as pairwise comparison:

| Pass | Key | Strength | Merges |
| ---- | --- | -------- | ------ |
| 1 | normalised email | strong, auto-merge | conf 1.0 |
| 2 | normalised 10-digit phone | strong, auto-merge | conf 1.0 |
| 3 | name + canonical city | weak, guarded | conf 0.6 |

`match_confidence` and `match_methods` are stored on every person, so anything
built on top of this database can decide to trust only the strong merges.

### Pass 3 is where a naive pipeline destroys data

Five people exist only in source2 and source3, so without a name-based pass
they stay split forever. But this dataset also contains **three different
humans called "Arjun Mehta", all in Noida**:

| | source | phone | email |
| --- | --- | --- | --- |
| person #5 | source2 | — | `arjun.mehta77@mailtest.example.org` |
| person #6 | source1 + source3 | 9000000131 | `arjun.mehta9@example.in` |
| person #56 | source3 | 9000000272 | — |

Matching on name + city would have fused all three into one person and deleted
two people's work history. So the weak pass only fires under three guards:

- **G1** the two records are not already in the same cluster
- **G2** the (name, city) pair points at exactly **two** clusters. Three
  clusters means the name cannot identify anybody, so nothing is merged and all
  of them go to the `merge_review` table instead.
- **G3** the two clusters hold no *contradictory* strong key. Two different
  phone numbers is positive proof of two people, and it overrides the name
  match — that is what keeps the two `Deepak Nair`s (Bengaluru and Delhi)
  apart.

Result: 4 weak merges applied (Divya Chopra, Karan Chopra, Manish Bhatia,
Vikram Mehta — each a source2 + source3 pair with no possible strong key), 3
Arjun Mehta records left in the review queue, 0 wrong merges.

```sql
SELECT reason, name_key, city, detail FROM merge_review;
```

### Choosing between values that disagree

When two files disagree about the same field, the winner and the loser are both
recorded in `field_conflicts` with the rule that decided it — no silent
overwrites:

| person | field | chosen | rejected | rule |
| --- | --- | --- | --- | --- |
| Rohit Verma | `full_name` | `Rohit Verma` | `R. Verma` | prefer the fully spelled name over an initial |
| Nikhil Chopra | `email` | `nikhil.chopra70@…` | `alt.nikhil.chopra70@…` | prefer the primary (non-alias) address |

For `city`, the rule is majority vote across sources, tie-broken by preferring
a real city name over a region label, then by source trust order
(`naukri > cbnexus > gig` — source1 is typed by the candidate themselves,
source3 is a staff-maintained CRM, source2's location column is the least
curated).

## Database shape

```
load_runs ──┬── people ──┬── person_skills ── skills
            │            └── source_records   (one row per CSV line, raw JSON kept)
            ├── data_issues        every problem found + what was done about it
            ├── field_conflicts    every disagreement + which value won and why
            └── merge_review       merges the matcher refused to make
```

Two design decisions worth calling out:

- **Nothing is discarded.** `source_records` holds every physical CSV line with
  its original values as JSON and a foreign key to the person it fed. Any merge
  can be audited or undone, and any number in `people` can be traced back to
  the line it came from.
- **`person_id` is deterministic — but not stable, and that distinction was a
  bug.** People are sorted by identity before IDs are assigned, so re-running on
  the same input gives byte-identical IDs. It does *not* survive a change to the
  input: adding one contact shifted Arjun Mehta from #56 to #57, because IDs are
  handed out by sort position. Everything durable therefore keys on email or
  phone, and `person_id` is a convenience column the loader recomputes. Full
  story in [the stuck log](docs/STUCK_LOG.md#3-person_id-was-deterministic-which-i-had-confused-with-stable).

## Handling of messy values

28 distinct problem classes were found. Full detail with row numbers is in
[docs/DATA_ISSUES.md](docs/DATA_ISSUES.md); the summary:

| Problem | Handling |
| --- | --- |
| `+919000000254` / `09000000287` / `+91-9000000131` | reduced to a bare 10-digit national number |
| `ISHA.CHOPRA95@MAILTEST…` | lowercased, or the same person becomes two records |
| `Bengaluru` / `bangalore`, `GURGAON` / `gurugram ` | canonical city list; `Delhi NCR` is kept flagged as a *region*, not upgraded to a city |
| `4.2` and `417964` in one CTC column | < 1000 read as lakhs and multiplied by 1e5; `ctc_unit_detected` records which reading was used |
| `1415/hr` and `15k/month` in one rate column | both an hourly and a monthly figure stored, derived at 160 billable hours/month, with `rate_basis_raw` recording which one was actually quoted |
| `24-07-2026`, `2026-08-08`, `07/13/2026`, `7 Jul 2026` | four parsers; dash = `dd-mm`, slash = `mm/dd`, proven against the file rather than guessed |
| applied dates *after* the ingest date | kept, flagged `applied_date_is_future` |
| a row whose columns are rotated out of place | detected by scoring each column against a field-type validator, then rotated back |
| header line repeated inside the data | dropped (it would otherwise become a person named "Name") |

Three findings only visible across the whole column, not in any single row:

- **The `rate` unit predicts the magnitude.** Rates quoted per hour convert to a
  median of ₹140,800/month; rates quoted per month have a median of ₹55,500 — a
  2.5× gap between people doing the same work. So `1415/hr` and `15k/month` are
  not two spellings of one quantity, and the pipeline refuses to pick one truth:
  it stores both figures and records which one was actually quoted.
- **100% of the email addresses are undeliverable**, sitting on domains reserved
  for testing by RFC 2606. This is the check that stops an automation from
  firing 56 bounces on day one.
- **8 contacts have 10+ completed projects but are marked unverified** in the
  CRM — a process bug upstream, not a bad cell, and invisible until you look at
  two columns together.

## The audio app (Task 3)

Full write-up in [docs/AUDIO_APP.md](docs/AUDIO_APP.md). The short version:

- Enter name + phone, **record in the browser or upload a file**, submit. The
  server extracts duration, sample rate, bitrate, three kinds of loudness, a
  noise floor, an SNR estimate and a 0–100 quality score with reasons.
- Loudness is computed as gated **LUFS** (ITU-R BS.1770), implemented from the
  spec rather than imported, and **verified against the spec's own calibration
  point**: a full-scale 997 Hz sine must read −3.01 LUFS, and it does, at three
  different sample rates.
- The SNR estimator **abstains** rather than guessing when signal and noise
  cannot be separated (a steady tone has no bimodal frame energy), because
  reporting 0 dB there would label a pristine recording "very noisy".
- The browser records **16-bit PCM WAV**, not the WebM/Opus that `MediaRecorder`
  gives you, so the server can analyse the waveform with the Python standard
  library and no codec installed.
- The app **does not write to `people`**, because `people` is derived and gets
  dropped on every pipeline run. It appends to `app_contacts`, which the pipeline
  reads as a **fourth source** and merges through the same email/phone/name
  passes. A walk-in worker therefore survives `make pipeline`.

## The automations (Task 2)

Two n8n workflows, both connected to the merged database. Node-by-node write-up in
[automation/n8n/README.md](automation/n8n/README.md).

**`01_llm_skill_tagging.json`** (primary) — polls for untagged people, asks Claude
for one skill category at `temperature: 0`, validates the answer, writes it back.
The interesting part is what happens to a bad answer:

```bash
curl -X POST http://127.0.0.1:5055/api/people/1/category \
     -H 'Content-Type: application/json' \
     -d '{"category":"Automation Heavy!!","confidence":0.9}'
# 400 {"error":"category 'Automation Heavy!!' is not in the allowed list", ...}
```

The Code node validates the model's JSON *and* the server validates it again,
because anything can POST to that endpoint. A free-text category from a language
model is a suggestion, not a category — one unrecognised value silently breaks
every report built on that column. A failed or unparseable answer leaves the
person untagged rather than writing a fallback guess, and since the queue endpoint
is idempotent the next scheduled run simply retries them.

**`02_duplicate_alert_on_new_csv.json`** — webhook receives a CSV, every row is
checked against the database, and duplicates raise a Slack/email alert. On the
sample file in `automation/sample_incoming/`:

```
7 row(s): 3 duplicate, 2 new, 2 need review
DUP  line 2   Tanvi Gupta      -> person #47 (on email)      shouted email, +91 spacing
DUP  line 3   Rohit Nair       -> person #40 (on phone)      +91- prefix
DUP  line 4   Sahil Malhotra   -> person #42 (on email)
?    line 7   Arjun Mehta                                    3 people share this name+city
?    line 8   Meera Bhatia     -> person #23 (on name_city)  no shared key: review, not merge
new  line 9   Lakshmi Iyer                                   'Chennai' is not in the city list
new  line 10  Zoya Khan                                      phone 'not-a-number' unparseable
dropped before the API call: line 5 repeated header, line 6 blank
```

Note lines 7 and 8: the alert applies **the same guards as the merge pipeline** and
refuses to call a name+city hit a duplicate. An automation cannot be more
confident than the database it is guarding. No matching logic lives in the Code
node either — it maps column aliases and stops, because a second copy of
"the same person" inside n8n is the copy nobody tests.

## Tests

```bash
make test        # 128 tests
```

The pipeline tests need no dependencies at all; the app and automation tests skip
cleanly if Flask and numpy are absent.

Three worth singling out, because they assert things that are easy to get wrong
and easy to believe you got right:

| Test | What it proves |
| --- | --- |
| `test_lufs_matches_the_bs1770_calibration_point` | the loudness implementation returns the spec's own reference value (−3.01 LUFS for a full-scale 997 Hz sine) at three sample rates |
| `test_tags_survive_a_rebuild_that_shifts_person_ids` | it inserts a contact that *does* shift IDs, then asserts every tag still points at the person it was written for — and that an ID really moved, so it cannot pass by accident |
| `test_date_separator_convention_holds_on_real_data` | the `dd-mm` vs `mm/dd` rule is a property of the file, not a guess, and both conventions are actually witnessed |

## Stuck log

The full version, with what I searched and what I rejected, is
[docs/STUCK_LOG.md](docs/STUCK_LOG.md). The three that cost real time:

**1. "Loudness in dB" is not one question.** My first version computed RMS dBFS
and looked finished. Then appending three seconds of silence to a clip dropped the
reading by 3 dB — nothing sounded quieter. A number that moves when you add
silence cannot compare two workers' submissions. That sent me to ITU-R BS.1770 and
LUFS. I rejected `pip install pyloudnorm` (two lines, but I could not have
defended a single number it produced) and rejected hardcoding the published 48 kHz
filter coefficients, since the browser records at 44.1 or 16 kHz — the filters are
re-derived per sample rate instead. Then I checked my work against the spec's
calibration point rather than trusting it.

**2. My SNR estimator called a perfect recording "very noisy".** A pure test tone
returned `0.0 dB` and `"very noisy"`. The estimator splits frames into signal and
background by energy, which requires the signal to be non-stationary — a steady
tone has one energy level, so the "signal" set came out empty and the code fell
through to zero. I rejected returning 0 (a confident lie about a clean file) and
rejected building a real VAD (out of scope): it now **abstains**, with a note, and
does not dock the score. Then a test caught a *second* bug in that fix — my fixed
`floor + 10 dB` threshold meant genuinely noisy clips abstained too, which is
exactly backwards. The split point now adapts to half the measured spread.

**3. Deterministic is not stable.** I made `person_id` deterministic in Task 1 and
wrote a test for it, then leaned on that for a foreign key. Designing the LLM tag
table, I asked what happens when a rebuild adds a person — and instead of
reasoning, I measured: copied the DB, inserted one contact, rebuilt, diffed. Arjun
Mehta moved from #56 to #57. My test had only ever proved *same input → same IDs*,
and I had read it as *IDs never change*. A tag keyed on `person_id` would have
silently re-attached to a different human. Durable tables now key on email or
phone; the loader recomputes `person_id`.

## A note on AI use

Claude was used throughout, as the brief permits. Where it earned its keep: as a
tutor on audio measurement, a domain I had not worked in before. Where I did not
take its output as given: every decision in this repo that involves a trade-off is
written down with the alternative I rejected, because those are the parts I have
to defend on a call — and two of the three stuck-log entries are bugs found by
testing an assumption rather than by accepting the first thing that worked.

Two of them are worth singling out, because they assert *properties of the data*
rather than of the code:

- `test_date_separator_convention_holds_on_real_data` proves that dash dates are
  `dd-mm` and slash dates are `mm/dd` in source1, by checking that no dash date
  has a month > 12 and no slash date has a day in the first position. It also
  asserts both conventions are actually witnessed, so the test cannot pass
  vacuously. The date parser is built on that proof rather than on a guess.
- `test_ctc_unit_ranges_do_not_overlap_in_real_data` proves the `< 1000 means
  lakhs` rule is safe, by asserting a clear gap between the two populations.

If a future file breaks either assumption, the suite fails instead of the data
quietly going wrong.

## Layout

```
data/            the three source CSVs, untouched
db/schema.sql    the target schema, commented with the reasoning
pipeline/
  config.py      every threshold, canonical list and trust order in one place
  normalize.py   pure field-level normalisers, each returning value + problems
  extract.py     CSV reading, structural repair, per-source field mapping
  match.py       union-find identity resolution + golden-record construction
  load.py        transactional SQLite writer
  run.py         CLI
docs/            assignment PDF, and the written reports (later commits)
```
