# Task 2 — the n8n flows

Two workflows, both connected to the merged database from Task 1.

| File | What it does | Assignment option |
| --- | --- | --- |
| [`01_llm_skill_tagging.json`](01_llm_skill_tagging.json) | Polls for untagged people, asks Claude for a skill category, validates the answer, writes it back | *"a flow with an LLM step that auto-tags each person's skill category and writes results back"* |
| [`02_duplicate_alert_on_new_csv.json`](02_duplicate_alert_on_new_csv.json) | Webhook receives a CSV, checks every row against the database, alerts on duplicates | *"receives a new CSV, checks it against your database, sends a duplicate alert"* |

The assignment asks for one. Both are here because they exercise opposite
directions — one writes *into* the database, one guards the door — and together
they show the API surface is real rather than shaped around a single happy path.
**01 is the primary submission.**

## Setup

1. **Start the app** (it serves the endpoints both flows call):

   ```bash
   make venv && make app        # http://127.0.0.1:5055
   ```

2. **Import the workflow**: n8n → *Workflows* → *Import from File*.

3. **Set the base URL.** Both flows read `$vars.CONSULTBAE_BASE_URL` and fall back
   to `http://host.docker.internal:5055`, which is what a Docker-hosted n8n needs
   to reach a server on the host. For a local `npx n8n`, set the variable to
   `http://127.0.0.1:5055`.

   > If n8n runs in Docker and you point it at `localhost`, it will try to reach
   > *itself*, and the HTTP node fails with `ECONNREFUSED`. This is the single
   > most common way this setup breaks.

4. **Credentials.**

   | Flow | Credential | Notes |
   | --- | --- | --- |
   | 01 | *HTTP Header Auth* — name `x-api-key`, value = your Anthropic API key | attached to the **Claude: classify skills** node |
   | 02 | Slack (optional) | the email node is an alternative; both have `continueOnFail` |

   Flow 02 needs no credentials at all if you leave both alert nodes as they are —
   the webhook response still reports the verdict.

5. **Run it.** Flow 01 has a manual trigger next to the schedule trigger, so
   *Execute Workflow* works without waiting 15 minutes. For flow 02:

   ```bash
   curl -X POST http://127.0.0.1:5678/webhook-test/consultbae/new-applicants \
        -F "file=@automation/sample_incoming/new_applicants_batch.csv"
   ```

## What flow 01 does, and why each guard is there

```
Schedule (15 min) ─┐
Manual trigger  ───┴─► GET /api/people/untagged?limit=20
                        └─► split out ─► loop (1 person at a time)
                              └─► POST api.anthropic.com/v1/messages   (claude-sonnet-5, temp 0)
                                    └─► Code: parse + VALIDATE
                                          ├─ valid ──► POST /api/people/:id/category
                                          └─ invalid ► collect for a human
                        └─(loop done)─► GET /api/categories
```

- **The queue is idempotent.** A person disappears from `/api/people/untagged`
  once tagged, so a crashed run resumes by itself and a re-run costs nothing. An
  automation you can safely re-run is worth more than one that is faster.
- **One person per LLM call.** Batching twenty into one prompt is cheaper, but a
  single malformed answer then poisons the whole batch and there is no way to
  retry just the bad one. At 5,000 people the arithmetic changes — see
  [SCALE_5000.md](../../docs/SCALE_5000.md).
- **`temperature: 0`.** Classification should not be creative. The same skills
  must produce the same category tomorrow.
- **The answer is validated, twice.** The Code node strips code fences, parses the
  JSON, checks the category against the allowlist the API itself published, and
  range-checks the confidence. Then the **server checks all of it again**, because
  the flow is not the last line of defence — anything can POST to that endpoint.
  Try it:

  ```bash
  curl -X POST http://127.0.0.1:5055/api/people/1/category \
       -H 'Content-Type: application/json' \
       -d '{"category":"Automation Heavy!!","confidence":0.9}'
  # 400 {"error":"category 'Automation Heavy!!' is not in the allowed list", ...}
  ```

  A free-text category from a language model is not a category, it is a
  suggestion. One unrecognised value in that column silently breaks every report
  built on it.
- **Failures leave the person untagged** rather than writing a fallback guess. A
  rate limit, an overload, or unparseable JSON all mean "try again in 15
  minutes", and `neverError: true` on the HTTP node makes sure a 429 reaches the
  parser instead of killing the run.
- **`generalist` is a real answer.** The prompt tells the model to answer
  `generalist` with confidence below 0.6 rather than pick a favourite when the
  skills genuinely span areas, and `/api/categories` lists every low-confidence
  tag — that is the queue a human should look at, not the whole table.
- **The raw HTTP node, not the LangChain Anthropic node**, so the workflow imports
  on a stock n8n with no community nodes installed.

## What flow 02 does

```
Webhook (CSV) ─► Extract from File ─► Code: alias columns, drop junk rows
                  └─► POST /api/match/check
                        └─► IF needs_attention
                              ├─ yes ► compose alert ─► Slack / email ─┐
                              └─ no  ► "all clear" ──────────────────► Respond to webhook
```

- **Column names are matched by alias, not exactly.** The three source files spell
  the same field four ways (`Full Name`, `worker_name`, `Name`, `name`), so an
  incoming file will too.
- **Structural junk is dropped in the flow**: a fully blank line, and a header row
  repeated inside the data. Both appear in the real source files, and the repeated
  header would otherwise become a contact called "Name" living in "City".
- **No matching logic lives in the Code node.** It maps columns and stops. Phone
  normalisation and identity matching already exist once, tested, in
  `pipeline/normalize.py`; a second copy inside n8n would give two subtly
  different definitions of "the same person", and the copy in n8n is the one
  nobody tests.
- **It branches on `needs_attention`, not on duplicates alone.** A row whose phone
  could not be parsed is not a clean pass — it is an unanswered question. Ditto a
  name+city hit: three people in this database are called "Arjun Mehta" and live
  in Noida, so a name match is a review item, never a merge.
- **The alert leads with counts and caps examples at five.** A 40-row JSON dump in
  Slack gets muted within a week.
- **`continueOnFail` on both alert nodes.** If Slack is down the webhook must
  still answer the caller; an alerting channel that takes the whole flow with it
  is worse than no alerting channel.
- **Every batch is recorded** in `automation_events`, so an alert that fired at
  3am can be reconstructed afterwards.

Running the sample file gives:

```
7 row(s): 3 duplicate, 2 new, 2 need review
DUP  line 2   Tanvi Gupta      -> person #47 (on email)     shouted email, +91 spacing
DUP  line 3   Rohit Nair       -> person #40 (on phone)     +91- prefix
DUP  line 4   Sahil Malhotra   -> person #42 (on email)
?    line 7   Arjun Mehta                                   3 people share this name+city
?    line 8   Meera Bhatia     -> person #23 (on name_city) no shared key: review, not merge
new  line 9   Lakshmi Iyer                                  'Chennai' is not in the city list
new  line 10  Zoya Khan                                     phone 'not-a-number' unparseable
dropped before the API call: line 5 repeated header, line 6 blank
```

## Checking the contract without n8n

A canvas cannot be unit-tested, so the API underneath both flows is tested
directly (`tests/test_automation.py`, 20 tests), and there is a replay harness
that makes the same calls in the same order:

```bash
python3 scripts/replay_flows.py                    # both flows
python3 scripts/replay_flows.py --flow duplicates
python3 scripts/replay_flows.py --flow tagging --dry-run
```

**This is a test harness, not the deliverable** — the deliverable is the JSON in
this folder, running in n8n, in the video. Without `ANTHROPIC_API_KEY` the replay
falls back to a crude rule-based stub so the write-back path still works offline;
it labels itself `offline-stub (not a model)` in the `model` column, so a stub
result can never be mistaken for a model result.
