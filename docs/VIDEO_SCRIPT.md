# Screen recording plan — 6 minutes

The brief: run the pipeline, show the audio app working end to end, walk through
the 2–3 hardest decisions. Voice required, face not.

Rehearse once. The temptation is to narrate every file; the score is for
*judgment*, so ~2 minutes is demo and ~4 minutes is reasoning.

## Before you hit record

```bash
make clean && make report        # fresh db, fresh reports
make venv                        # if .venv is missing
```

- Terminal at a readable font size, window ~120 columns.
- Tabs open: the app on `:5055`, `/submissions`, n8n with both flows imported.
- `docs/DATA_ISSUES.md` and `automation/n8n/README.md` open but not on screen yet.
- Mic checked. Do not record the first take.

## 0:00–0:35 — What this is

> "Three files, no shared ID, and no field that even exists in all three. 103
> rows in, 56 people out. I'll run it, show the audio app, then spend most of the
> time on the three decisions I'd defend in an interview."

Show the three CSVs side by side for five seconds — specifically the header rows,
so the missing-phone and missing-email columns are visible.

## 0:35–1:30 — Task 1 + Task 4: run the pipeline

```bash
python3 -m pipeline.run --report
```

Point at four lines of the summary and nothing else:

- `103 rows read → 56 people`
- `400 data issues logged (2 error / 53 warn / 345 info)`
- `merges left for a human: 3`
- the four `name_city_weak` merges at confidence 0.6

> "Every one of those 400 issues records what the value was, what I did about it,
> and why — that's Task 4, and it falls out of the pipeline rather than being a
> separate audit."

Then one query, not a tour of the schema:

```bash
sqlite3 -header -column db/consultbae.db \
  "SELECT person_id, full_name, phone, sources, match_methods FROM people WHERE full_name = 'Arjun Mehta';"
```

## 1:30–3:00 — Decision 1: the merge that should NOT happen

This is the most important 90 seconds of the video.

> "Three different people called Arjun Mehta, all in Noida. Two of them only
> exist in one file each, so name and city is the only key they share with
> anything. Merge on that and my people count goes *down* — the output looks
> cleaner and I've just deleted two people's work history."

Show `merge_review`, then the guard in `pipeline/match.py` — the `len(roots) > 2`
check — and say the rule out loud: a name+city merge only fires when it points at
exactly two clusters and those clusters hold no contradictory email or phone.

> "Four merges did fire — each one a source2+source3 pair where source2 has the
> email, source3 has the phone, and neither file has both. Those are real gaps in
> the schemas. Three records went to a human instead. Zero wrong merges."

Mention the transitive point briefly: source2 and source3 share no key at all, so
they're joined *through* source1 — which is why matching is a union-find and not
pairwise comparison.

## 3:00–4:15 — Task 3: the audio app, end to end

Record ~8 seconds of yourself talking, submit, and let the result render.

> "Name, phone, record. It found me in the merged database on the phone number —
> I typed it with a `+91` and spaces, and it matched the record the pipeline
> stored as ten digits, because the app imports the same normaliser."

Read three numbers off the result card, then the decision:

> "Duration, sample rate, bitrate, and loudness three ways. Peak tells you if it
> clipped, RMS tells you how hot it is, and LUFS tells you how loud it *sounds* —
> that's the only one that compares two recordings fairly, because it gates out
> silence. I implemented it from the ITU spec, and I check it against the spec's
> own calibration point: a full-scale 997 Hz sine has to read −3.01 LUFS."

```bash
.venv/bin/python -m unittest tests.test_audio_analysis.TestLoudness -v
```

Then `/submissions` for the list view with the play button.

If time is tight, cut the SNR story here — it is in the stuck log.

## 4:15–5:20 — Task 2: n8n

Show the canvas of `01_llm_skill_tagging`, then execute it.

> "Polls for untagged people, one Claude call each at temperature zero, and the
> Code node validates the answer before anything is written."

Then the point of the whole flow:

```bash
curl -X POST http://127.0.0.1:5055/api/people/1/category \
     -H 'Content-Type: application/json' \
     -d '{"category":"Automation Heavy!!","confidence":0.9}'
```

> "400. The flow validates, and the *server* validates again, because anything
> can POST to that endpoint. A free-text category from a language model isn't a
> category, it's a suggestion — and one unrecognised value in that column
> silently breaks every report built on it."

Then flow 02 with the sample CSV, and point at the two rows it refuses to call
duplicates: the ambiguous Arjun Mehta, and the name+city-only Meera Bhatia.

> "Same rule as the pipeline. The automation can't be more confident than the
> database it's guarding."

## 5:20–6:00 — Decision 3: the bug I found by measuring

> "`person_id` is deterministic — same input, same ids, and there's a test for it.
> I leaned on that, then asked what happens to an LLM tag when a rebuild adds one
> person. Instead of reasoning about it I measured: copied the database, added one
> contact, rebuilt, diffed. One existing person's id moved from 56 to 57."

> "Deterministic across re-runs isn't the same as stable across input changes, and
> my test only proved the first one. Tags keyed on `person_id` would have
> re-attached to a different human, silently. So the durable tables key on email
> or phone, and the loader recomputes `person_id` after every rebuild."

Close on what you would change first, not on a summary:

> "Before launching this to 5,000 workers, the first thing I'd fix is the upload
> path — analysis runs inside the request at 0.7 seconds of CPU per clip, which
> saturates a single worker at almost exactly the peak I'd expect. That's in
> SCALE_5000.md, with the measurements."

## Do not

- Read the README aloud.
- Walk through directory structure.
- Apologise for anything. State a limitation once and move on.
- Say "the AI wrote this". Say what the decision was and why.
