# Task 5 — 5,000 gig workers, one weekend

What breaks first, and what I would change before launch. Numbers below are
measured on this implementation, not estimated.

## The measurements that matter

| | measured |
| --- | --- |
| 30 s recording, 44.1 kHz 16-bit mono WAV | **2.52 MB** |
| same audio as Opus @ 24 kbps | **137 KB** — 18.5× smaller |
| server-side analysis of a 30 s clip | **0.70 s of CPU** (pure-Python K-weighting filter) |
| SQLite write throughput, 8 concurrent writers | ~2,200 serialised inserts/s |
| full pipeline rebuild at today's 103 rows | 0.02 s, and it does **not** block a concurrent insert |

Load assumption: 5,000 workers × ~1.5 clips = **7,500 uploads**, and a weekend
launch is not uniform — roughly half will arrive in one 4-hour window after
whatever message goes out. That is ~16 uploads/minute average with peaks around
5× the mean, so **~1.3 uploads/second at peak**.

## What breaks first

**1. The upload path, and it breaks exactly at the peak.** Analysis runs
synchronously inside the POST handler on Flask's single-threaded development
server. At 0.70 s of CPU per clip, one worker process tops out near 1.4
requests/second *with zero overhead* — the same order as the 1.3/s peak. So it
does not fail comfortably early or survive comfortably; it saturates right where
the load lands, and every queued upload holds a mobile connection open while it
waits. This is the first thing that falls over and the least visible in testing,
because with one tester it is always fast.

**2. Bandwidth on the worker's phone, not on the server.** 2.52 MB over a weak
mobile connection, with no resumability and no client-side retry, means a
worker's recording is lost when the upload drops at 80% — and they have to say
the whole thing again. For gig workers on metered connections this, not server
capacity, is what determines the completion rate. The WAV choice that made
server-side analysis dependency-free (see [AUDIO_APP.md](AUDIO_APP.md)) is
exactly what hurts here. It was the right call for a laptop demo and the wrong
one for 5,000 phones.

**3. Storage volume follows from that.** 7,500 WAV clips ≈ **18.9 GB**; the same
clips as Opus ≈ **1.0 GB**. On S3 at list price that is about $0.44/month versus
$0.02 — the storage bill is not the problem. The problem is that 18.9 GB is sitting
on one box's local disk next to the SQLite file, with no backup and no lifecycle
policy, and `app/uploads/` is in `.gitignore` so nothing is protecting it.

**4. Duplicates are only half-solved.** Identical bytes are caught by sha256. But a
worker who taps *record* twice and says the same sentence produces different
bytes, and nothing stops them — there is no per-phone submission limit and no
"you have already submitted" state in the UI. At 5,000 people the duplicate rate
will be driven by nervous re-recording, which the current check cannot see.

**5. Not SQLite — and I checked.** The instinct is to blame it, but the measured
write throughput is ~2,200 inserts/second serialised, which is three orders of
magnitude above the peak, and the rebuild holds no meaningful lock at this size.
SQLite fails for a different reason: it is one file on one machine, so the moment
you want a second app server to absorb the peak, it is over. The constraint is
horizontal scaling, not throughput.

**6. Anyone can POST.** There is no auth on `/api/submissions` and no rate limit.
One bored person with `curl` can fill the disk with 25 MB uploads.

## What I would change before launch, in this order

1. **Take analysis out of the request path.** Store the file, write the row with
   the properties still pending, return immediately, and analyse in a worker
   queue. Serve the app with gunicorn behind nginx rather than the dev server.
   This alone converts the peak from a failure into a few minutes of lag, and
   `analysis_ok` already exists as the flag for "not measured yet".
2. **Record Opus instead of WAV**, and accept the ffmpeg dependency on the
   server — a deliberate reversal of the Task 3 decision, made because 18.5×
   less data over a mobile connection is worth more at 5,000 phones than a
   dependency-free server is. Keep the WAV path as the fallback for browsers that
   cannot encode Opus.
3. **Upload straight to object storage** with a presigned URL and resumable/multipart
   transfer, so the app never proxies bytes and a dropped connection resumes
   instead of restarting. Hold the recording in the browser until the upload is
   acknowledged, so a failure never costs the worker their recording.
4. **One signed link per worker**, plus a per-phone rate limit. This turns
   duplicates from a data-cleaning problem into a UX problem — the second visit
   shows "you already submitted on Saturday, replace it?" — and it fixes the open
   endpoint at the same time.
5. **Postgres instead of SQLite**, because step 1 means more than one process and
   step 3 means more than one box. The schema was written to be portable for this
   reason; no SQLite-only types are used.

Two things I would **not** change: the matching logic, which is the part that took
the thinking and does not care about volume; and the rule that a bad input gets
flagged rather than dropped. At 5,000 submissions the flagged pile is exactly the
work queue you want, and a pipeline that silently discarded 3% of it would be
much harder to debug on Monday.

## What I would watch in the first hour

Upload success rate (client-side, not server-side — the failures never reach the
server), p95 analysis lag, the drop-off between page load and successful submit,
the distribution of `quality_score`, and the per-phone submission count. If the
quality distribution is bimodal, the microphone-permission flow is failing
silently for one class of device, and that is invisible in every server metric.
