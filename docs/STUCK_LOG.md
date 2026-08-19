# Stuck log

The three places this actually cost me time, what I searched, what I tried, and
what I rejected. In rough order of how long they took.

---

## 1. "Loudness in dB" turned out not to be a question with one answer

**Where I got stuck.** The assignment asks for *loudness (dB)*. I had never
measured audio before. My first implementation computed RMS in dBFS, printed
`-21.07 dB`, and I moved on — it looked done.

Then I tested it against a second clip: the same speech with three seconds of
silence appended. RMS dropped by ~3 dB. Nothing about the recording sounded
quieter. A number that changes when you add silence cannot be used to compare two
workers' submissions, which is the only reason to store it.

**What I searched.** "audio loudness vs RMS", "dBFS vs LUFS", "why is RMS not
perceived loudness", then ITU-R BS.1770 once the term LUFS appeared, then
"K-weighting filter coefficients 48kHz", and finally pyloudnorm's source to check
how the two biquads are derived for arbitrary sample rates.

**What I asked AI.** Two things. First, to explain the difference between peak,
RMS and LUFS in terms of what question each answers — that framing is what made
the design obvious (store all three; they are not competing). Second, to walk me
through the gating rules in BS.1770, because the spec's own wording about the
relative gate took me two reads.

**What I rejected.**

- **Just installing `pyloudnorm`.** It would have been two lines. I rejected it
  because I could not have defended a single number it produced, and the
  assignment says I have to. Writing the gating out is ~30 lines and I now know
  what the −0.691 offset is for.
- **Hardcoding the 48 kHz coefficients** from the spec, which is what most blog
  posts show. The browser records at whatever the device gives — 44.1 kHz on this
  laptop, 16 kHz on some Android hardware — and applying 48 kHz coefficients to
  44.1 kHz audio is quietly wrong. The filters are re-derived per sample rate
  through the bilinear transform instead.
- **Trusting my own implementation without a reference.** BS.1770 defines a
  calibration point: a full-scale 997 Hz sine is −3.01 LUFS. Mine returns −3.01 at
  48 kHz, −3.01 at 44.1 kHz, −2.97 at 16 kHz. That is the test I would want if
  someone handed me this code, so it is in the suite.

**How I got unstuck**: by realising the question was "what makes two recordings
comparable", not "which formula converts amplitude to dB".

---

## 2. My SNR estimator confidently called a perfect recording "very noisy"

**Where I got stuck.** The noise estimate worked on speech-like audio and then
reported `estimated_snr_db: 0.0` and `"very noisy"` for a pure 440 Hz test tone —
a signal with no noise in it at all.

**What was actually wrong.** The estimator splits 50 ms frames into "signal" and
"background" using the frame energy distribution, and takes the noise floor as
the 10th percentile. For speech that works, because speech is loud-quiet-loud. A
steady tone has *one* energy level, so the 10th percentile equals the signal
level, no frame clears the threshold, the "speech" set comes out empty, and my
code fell through to `snr = 0.0`.

**What I searched.** "estimate SNR from a single audio file without a clean
reference", "voice activity detection energy threshold", "noise floor percentile
method". The useful finding was that every simple method assumes the signal is
non-stationary — none of them can separate signal from noise in a steady tone,
because there is no information to do it with.

**What I rejected.**

- **Returning 0 dB, or falling back to `rms - noise_floor`.** Both produce a
  number that looks like a measurement and is not one. `0.0` reading as "very
  noisy" is worse than useless: it is a confident lie about a clean file, and it
  would have docked the quality score.
- **A cleverer estimator** (spectral flatness, or a real VAD). Probably better,
  and out of scope for a 48-hour assignment — the honest cheap fix was to detect
  the case and abstain.

**What I did.** When the frame dynamic range is under 6 dB, `estimated_snr_db` is
`NULL`, `snr_note` explains why, and the quality score is *not* penalised —
unknown is not the same as bad.

**Then a test caught a second bug in the fix.** With the abstain rule in place I
wrote a test asserting a noisy clip scores below a clean one. It failed: the
noisy clip abstained too. My threshold for classifying a frame as signal was a
fixed *floor + 10 dB*, so at ~9 dB SNR nothing ever cleared it — the estimator was
silent about exactly the recordings most worth flagging. The split point now
adapts to half the measured spread (capped at 15 dB), which works down to ~6 dB.
I would not have found that by hand-testing; the test found it because I had
written down what I expected to be true.

---

## 3. `person_id` was deterministic, which I had confused with stable

**Where I got stuck.** In Task 1 I made `person_id` deterministic on purpose:
people are sorted by identity before ids are assigned, so re-running the pipeline
on the same input gives byte-identical ids. There is a test for it. I leaned on
that when the audio app needed a foreign key.

While designing the table for the LLM skill tags in Task 2, I asked what happens
to a tag when a rebuild adds a person. And the answer was not what my test said.

**What I actually did.** Rather than reason about it, I measured — copied the
database, inserted one app contact with phone `9000000001`, rebuilt, and diffed
the two `people` tables on phone number:

```
was  now  full_name    phone
56   57   Arjun Mehta  9000000272
```

One new contact, one existing person's id changed. Deterministic across re-runs of
*the same input* is not the same as stable across *changes to the input* — ids are
handed out by sort position, so a new row shifts everyone after it. My test only
ever proved the first property, and I had read it as proving the second.

An LLM-assigned category keyed on `person_id` would therefore have re-attached
itself to a different human after any rebuild. Silently. That is the kind of bug
that gets found six months later when someone asks why a React developer is
tagged `qa-automation`.

**What I rejected.**

- **Making ids genuinely immutable** by hashing the identity key. It works, but it
  gives up small readable integers everywhere and rewrites Task 1 for a problem
  that only affects two side tables.
- **A real foreign key with `ON UPDATE CASCADE`.** SQLite does not renumber rows —
  the rebuild drops and re-inserts — so there is no update to cascade. Worse, with
  `PRAGMA foreign_keys=ON`, `DROP TABLE` performs an implicit `DELETE FROM`, so a
  child row pointing at `people` would make `make pipeline` fail outright once any
  submission existed.

**What I did.** The durable tables (`audio_submissions`,
`person_skill_categories`) key on the natural identity — email, or phone when
there is no email — and carry `person_id` only as a convenience column that
`pipeline.load.relink_person_references` recomputes after every load. The test for
it deliberately inserts a contact that *does* shift ids and then asserts every tag
still points at the person it was written for; it also asserts that at least one id
moved, so the test cannot pass by accident.

---

## Smaller things, for completeness

- **`07/03/2026` vs `03-07-2026`.** I was about to pick a convention and note the
  ambiguity. Instead I checked whether the file decides it: every dash date with a
  component > 12 has it first, every slash date has it second. The separators
  carry two different conventions consistently, which is what you get when two
  upstream systems export in different locales. That is now a test that fails if a
  future file breaks the assumption — and it asserts both conventions are actually
  witnessed, so it cannot pass vacuously.
- **The scrambled row in source2.** My first instinct was to special-case row 20.
  I replaced it with a field-type scorer over the six cyclic rotations. I
  deliberately did *not* try all 720 permutations: with six columns, plenty score
  well by accident (name and city are both "a short word"), so an exhaustive
  search invents damage that is not there.
- **macOS ate port 5000.** Flask reported it was serving on 5000; every request
  returned non-JSON. AirPlay Receiver holds that port and answers first. Ten
  minutes, one `PORT=5055`, and a note in the Makefile so nobody else loses them.
- **`NOT NULL constraint failed: people.rate_out_of_range`** on the first load. The
  boolean flag columns are `NOT NULL DEFAULT 0`, but inserting an explicit `None`
  overrides a default. Fixed by coercing flags to 0 — and deliberately *not*
  coercing `is_verified`, where unknown is a genuine third state (the person is
  absent from the CRM).
