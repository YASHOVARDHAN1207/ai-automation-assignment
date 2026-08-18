# Task 3 — mini audio collection app

```bash
make venv        # Flask + numpy into .venv
make app         # http://127.0.0.1:5055
```

Two views, as asked:

| | |
| --- | --- |
| `/` | name + phone, record in the browser **or** upload a file |
| `/submissions` | every submission with a play button and the extracted properties |

Plus a JSON API over the same data (`/api/submissions`), which the Task 2 n8n
flows consume, and `/healthz`.

> Port 5055, not 5000: on macOS the AirPlay Receiver squats on 5000 and answers
> HTTP requests before Flask ever sees them. That cost me ten confusing minutes
> — see the stuck log in the README.

## What gets extracted

Required by the assignment: **duration, sample rate (kHz), bitrate, loudness
(dB)**. Bonus: **a noise/quality estimate**. All of it lands in
`audio_submissions` and is shown in both views.

| Field | Meaning |
| --- | --- |
| `duration_seconds` | from the decoded sample count, not the container header |
| `sample_rate_hz` / `sample_rate_khz` | |
| `channels`, `bit_depth`, `codec`, `container_format` | |
| `bitrate_kbps` | container bitrate when stated, else file size × 8 ÷ duration |
| `pcm_bitrate_kbps` | uncompressed equivalent — `rate × bits × channels` |
| `peak_dbfs` | did it clip |
| `rms_dbfs` | how hot the signal is |
| `loudness_lufs` | how loud it *sounds*, gated, ITU-R BS.1770 |
| `loudness_range_lu` | spread of the gated blocks — a dynamics proxy |
| `crest_factor_db` | peak minus RMS |
| `noise_floor_dbfs` | 10th percentile of 50 ms frame energies |
| `estimated_snr_db` | signal frames vs background frames, or `NULL` |
| `frame_dynamic_range_db` | p95 − p10 of frame energies |
| `speech_ratio_pct`, `silence_pct` | |
| `clipping_pct`, `dc_offset` | |
| `quality_score` (0–100), `quality_label`, `quality_reasons` | |

### Loudness is reported three ways, on purpose

"Loudness in dB" is not one number:

- **Peak dBFS** answers *did it clip*.
- **RMS dBFS** answers *how hot is the signal*.
- **LUFS** answers *how loud does this sound to a human* — and it is the only one
  that compares two different recordings fairly, because it weights frequencies
  the way an ear does and gates out silence. A quiet clip with one loud cough has
  a high peak and a low LUFS.

The LUFS implementation is written out rather than imported, so the gating is
visible: K-weighting (a +4 dB shelf above ~1.7 kHz modelling the head, plus a
38 Hz high-pass), 400 ms blocks overlapping 75%, an absolute gate at −70 LUFS,
then a relative gate 10 LU below the ungated mean.

**It is verified against the spec's own calibration point.** BS.1770 defines that
a full-scale 997 Hz sine reads −3.01 LUFS. This implementation returns:

| sample rate | measured |
| --- | --- |
| 48 000 Hz | −3.01 LUFS |
| 44 100 Hz | −3.01 LUFS |
| 16 000 Hz | −2.97 LUFS |

That one number validates the entire chain — both filters, the gating and the
−0.691 offset. If any coefficient were wrong it would miss. The published
coefficients are specified at 48 kHz only, so the filters are re-derived through
the bilinear transform for whatever rate the browser actually recorded at, which
is why the 44.1 and 16 kHz rows are right too.
(`tests/test_audio_analysis.py::test_lufs_matches_the_bs1770_calibration_point`)

A second test asserts the property that makes LUFS worth the effort: padding a
clip with three seconds of silence changes the LUFS reading by <0.5 dB while
dragging plain RMS down by >2 dB.

### The SNR estimator abstains instead of guessing

SNR is estimated by splitting 50 ms frames into "signal" and "background", which
only means anything if the frame energies are genuinely bimodal — speech is
loud-quiet-loud, so they are. A **stationary** signal (a test tone, continuous
hiss, a hum) has one flat energy level, so any split puts every frame on one
side and the naive result is 0 dB — which reads as *"very noisy"* about what may
be a pristine recording.

So when the frame dynamic range is under 6 dB, `estimated_snr_db` is `NULL` and
`snr_note` says why, and the quality score is **not** penalised. Unknown is not
the same as bad; penalising a measurement that was never taken is how a scoring
function starts lying.

The split point itself adapts to the measured spread (half of it, capped at
15 dB) rather than sitting at a fixed floor + 10 dB. The fixed threshold was the
first implementation and it failed a test: at ~9 dB SNR nothing cleared the bar,
so it abstained on exactly the noisy clips most worth flagging.

## Three decisions worth arguing about

### 1. The browser records WAV, not WebM/Opus

`MediaRecorder` — the obvious API — produces WebM/Opus. Opus cannot be decoded in
pure Python, so **every loudness number would depend on ffmpeg being installed
next to the server.** Instead the recorder pulls raw Float32 frames off a Web
Audio node and writes the 44-byte PCM WAV header in JavaScript
(`app/static/recorder.js`), so the server reads the waveform with the standard
library's `wave` module and nothing else.

Cost, stated honestly: WAV is roughly 10× the size of Opus. Fine for a 10-second
clip, and it is the first thing that breaks at 5,000 workers — see
[SCALE_5000.md](SCALE_5000.md).

Uploaded files of any format are still accepted and routed through ffmpeg when it
is present. When it is not, the submission is stored with container metadata only
and `analysis_note` explains what is missing.

### 2. A submission is never rejected because analysis failed

If the waveform cannot be decoded — unknown codec, truncated file, no ffmpeg —
the row is still written with `analysis_ok = 0` and a note. Losing a gig
worker's recording because the server lacked a codec is the worse failure, and
the analysis can be re-run later from the stored file. There is a test for a
file that is not audio at all.

### 3. The app does not write to `people`

`people` is a **derived** table: `db/schema.sql` drops and rebuilds it from the
three CSVs on every pipeline run. The obvious implementation — have the app
`INSERT INTO people` — loses every walk-in worker on the next `make pipeline`,
silently.

So the app writes to tables the pipeline never drops, and **becomes a fourth
source**:

```
                        app_contacts ─┐
source1_naukri.csv ──┐                │
source2_gig.csv ─────┼──► the same email/phone/name matching passes ──► people
source3_cbnexus.csv ─┘                │
                                      └─► re-read on every rebuild
```

- **Matched phone** → the submission links to the existing person. `+91 90000
  00287` finds the person the pipeline stored as `9000000287`, because the app
  imports `pipeline.normalize` rather than writing a second phone parser. If it
  had its own, a worker typing a country code would fail to match the CRM record
  the pipeline worked to link.
- **Unknown phone** → a row in `app_contacts`, which `pipeline.extract` reads as
  a fourth source and re-materialises into `people` through the normal matching
  passes. No special-casing, and the person survives every rebuild.
- **Unparseable phone** → the audio is stored with no identity claim.

`audio_submissions.person_id` is deliberately **not** a foreign key. With
`PRAGMA foreign_keys=ON`, SQLite's `DROP TABLE` performs an implicit
`DELETE FROM`, so a child row pointing at `people` would make the next rebuild
fail outright. The durable join key is `phone`, and the pipeline re-points
`person_id` after every rebuild
(`pipeline.load.relink_person_references`). This is also the reason `person_id`
was made deterministic back in Task 1.

Two tests pin this down:
`test_an_app_created_contact_survives_a_pipeline_rebuild` and
`test_submissions_are_never_dropped_by_a_rebuild`.

## Other details

- **Duplicate uploads** are detected by sha256 of the stored bytes and reported
  as `duplicate_of`, rather than silently double-counted. This is the same
  duplicate question the Task 2 flow answers for incoming CSVs.
- **Uploads are capped** at 25 MB, extensions are allowlisted, and files are
  stored under a generated uuid name — the original filename is kept as data,
  never used as a path.
- `/media/<name>` refuses anything containing a character outside
  `[A-Za-z0-9._-]`, so a traversal attempt cannot reach the database file.
