# Help centre gap loop

How a question we answered badly becomes an article, and how to check it
is working.

This sits next to the two pipelines in `KNOWLEDGE_PIPELINE.md`. Those
teach the agent what it knows. This one decides what we should write
down that nobody has written down yet.

---

## The problem it fixes

Two things detect help centre holes, and until this loop existed they
both had the same blind spot.

`kb_search.check_and_create_kb_task()` counts questions with no matching
article and files a ClickUp task on the third occurrence in 30 days. It
had exactly two call sites, `intake.py:874` and `intake.py:927`. Both are
the Vic widget.

So a client who asked Vic counted. A client who emailed support, whose
ticket an engineer then triaged to `user education`, did not. That second
path is the stronger signal: an engineer has read the thread and
concluded the product works fine and the client misunderstood it. That is
the definition of a help centre hole, and it was invisible to the thing
that detects help centre holes.

The second problem is granularity. The counter keys on an exact
fingerprint, so `cant-find-shift-button` and `where-do-i-add-a-shift`
are two rows that each sit at two occurrences forever, and neither
reaches three. One article would serve both.

---

## The loop

```
  Vic widget  ──────────────┐
  (intake.py)               │
                            ├──>  kb_deflection_log  ──>  monthly pass  ──>  ClickUp task
  ClickUp "user education"  │        (one row per          (cluster,          (with the
  (clickup_user_education_  │         signal)               check KB,          article
   handler.py)  ────────────┘                               draft)             drafted)
```

### 1. Recording a signal

`kb_gap.record_user_education_gap()` is called from
`handle_user_education()` in `clickup_user_education_handler.py`, at step
7c.

**Where it sits, and why.** After the outbound guard and after the
duplicate guard, immediately before the send in step 8.

- A guard-blocked draft returns above this point. The outbound guard
  fires when a dev has mislabelled an open bug as user education, which
  is not a help centre gap, so it must not be counted as one.
- A held duplicate also returns above it. We already sent that
  explanation once, so the question was answered.
- Everything reaching step 7c is either auto-sent (step 8) or handed to
  Slack for a manual send (step 9). Both are genuine user education, and
  both count.

It makes one fast-model call to reduce the ticket to a topic slug, checks
the live KB index for coverage, and writes one row. It is wrapped in
`try/except` at the call site: this is instrumentation hanging off a
handler that emails clients, so it must never be the reason a client does
not get a reply.

### 2. The table

`kb_deflection_log` gained six columns (see `database.py`). Older rows
predate them, which is why readers `COALESCE(source, 'vic_widget')`
rather than treating NULL as a third source.

| Column | Why |
| --- | --- |
| `source` | `vic_widget` or `user_education`. Lets the report say where a topic is coming from. |
| `question` | The underlying question in one sentence. The fingerprint alone is too terse to cluster well. |
| `module` | Which part of Vome. Used for grouping and for the ClickUp task. |
| `kb_covered` | Whether a fresh article already existed at the time. |
| `zoho_ticket_id`, `clickup_task_id` | Traceability back to the source ticket. |

`kb_covered` is the one worth understanding. A topic that keeps
generating signals **while an article already exists** is a findability
or clarity problem, not a missing article. The two need different work,
and the monthly pass files them with different titles (`KB rewrite` vs
`KB article needed`).

### 3. The monthly pass

`kb_gap.run_monthly_kb_gap_pass()`, first Tuesday of the month at 09:00
ET. It claims the run in `sweeper_runs` (key `kb_gap_YYYY_MM`) so a
restart near the trigger cannot double-file.

1. Load every signal in the last 90 days, grouped by fingerprint.
2. Load closed ClickUp tasks the weekly knowledge scan already flagged
   with `kb_article_candidate` or `resolution_type = user_education`.
   These carry `client_facing_explanation`, which is the answer already
   written in Sam's voice with the internal detail stripped out. Nothing
   had ever read those fields.
3. One Claude pass groups the fingerprints into topics.
4. For each topic over the threshold, check KB coverage, draft the
   article, file a ClickUp task with the draft in the body.
5. Post one Slack summary to `SLACK_CHANNEL_AGENT_LOG`.

**Counts are recomputed from the table, never taken from the model.**
`_cluster_signals` sums the real per-fingerprint counts and drops any
fingerprint or task id the model invented. The count decides what gets
filed, so it comes from Postgres.

### 4. Why it stops at a ClickUp task

Nothing in this module writes to Zoho Desk. An article is public the
moment it is published and nothing here is good enough to publish
unread.

The draft prompt is told to write `TODO: confirm` rather than invent a
menu path, because a confident wrong instruction in a help centre article
is worse than a visible hole. A human fills those in and publishes.

---

## Knobs

| Constant / env var | Default | What it does |
| --- | --- | --- |
| `MIN_SIGNALS_FOR_TOPIC` | 3 | Below this it is one client's confusion, not a pattern. |
| `CLUSTER_WINDOW_DAYS` | 90 | Long enough for a slow-burn topic, short enough that an article we wrote stops the topic recurring. |
| `COVERAGE_STALE_DAYS` | 365 | Matches the caveat boundary in `kb_search.score_article`. |
| `max_topics` | 5 | Tasks filed per pass. Five is a month of article writing. Filing twenty means nobody writes any. |
| `KB_GAP_DRY_RUN` | unset | `true` puts the scheduled pass in report-only mode. |
| `KB_GAP_HOUR` / `KB_GAP_MINUTE` | 9 / 0 | Trigger time. |

---

## Checking it

```bash
# Cluster and report WITHOUT filing anything. Do this first after any
# change to the clustering or drafting prompt.
curl -X POST "$HOST/kb-gap/cluster?dry_run=true"

# What the last run did, and what is currently filed
curl "$HOST/kb-gap/status"

# Run for real, wider window, more topics
curl -X POST "$HOST/kb-gap/cluster?window=180&max=8"
```

Locally, `python kb_gap.py --dry-run` does the same thing and prints the
JSON.

There is no undo beyond deleting the ClickUp tasks by hand, which is why
`dry_run` exists and why `max_topics` is low.

What to look at:

- **Signals recorded but no topics filed.** Expected early on. Every
  topic needs three signals in 90 days, and the user education source
  only started producing rows when this shipped.
- **The same topic filed twice.** Should be impossible: `kb_gap_topics`
  is checked before filing and only a topic marked `published` is
  eligible again. If it happens, the two `topic_key` slugs differed, so
  tighten the clustering prompt.
- **A topic filed as `KB rewrite` that should be `KB article needed`.**
  The coverage check matched a loosely related article. `_kb_coverage`
  takes the single top FTS hit, which is deliberately blunt.

---

## The answer key, before you trust any of this

Do not let the monthly pass drive the roadmap until it has been scored
against you.

Label ten closed `user education` tickets by hand: genuine FAQ hole, or
one-off, or a product bug wearing a user education costume. Then run the
pass over that period and compare. Article-worthiness is exactly the kind
of fuzzy judgment where a model's labels drift from a human's, and the
cost of getting it wrong is a quarter of article writing aimed at the
wrong topics.

Tune until it agrees with you on 8 of 10. Only then treat the ranked
topic list as roadmap input.
