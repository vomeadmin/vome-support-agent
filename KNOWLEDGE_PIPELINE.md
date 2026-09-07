# Knowledge pipeline

How the support agent knows things, and how to check it is working.

There are two independent pipelines. Help center articles are facts about
the product. Closed work is patterns about how we handle things. Both end
up in the prompts that draft client replies.

---

## 1. Help center articles (Zoho Desk to Postgres)

**Schedule:** nightly, 02:00 ET (`run_kb_sync`, main.py)

Zoho Desk KB -> `kb_articles` in Postgres -> Postgres full-text search at
draft time. Article bodies are indexed, not just titles, so the model
paraphrases real instructions instead of guessing from a heading.

Only **Published** articles are indexed. Zoho's `getArticles` also
returns Draft and Unpublished ones, and those live at portal URLs that
404 for a client, so quoting one would hand the customer a dead link. The
filter runs at ingest and again at query time (the query-time half covers
rows indexed before the filter existed).

### The delete guard

The sync ends by deleting every row whose article id it did not see. That
is how an article deleted in Zoho leaves the index. It is also how a
partial fetch used to silently wipe entire categories: if `getArticles`
errored for a category, or an article's detail call failed, those ids
were missing from the seen set and the delete removed them.

So the fetch now tracks its own completeness (`LAST_FETCH_COMPLETE`) and
`sync_articles_to_db` refuses to run the delete unless the fetch reported
zero failures. A failed run posts to the agent log channel in Slack
rather than dying into a log line nobody reads.

### Who uses it

Every surface that drafts a client reply, via `kb_context.py`:

| Surface | Where |
| --- | --- |
| Auto-draft on a new ticket | `agent.process_ticket` |
| Slack `draft` command | `slack_reply_handler._generate_draft` |
| Slack plain-English instruction | `slack_reply_handler._generate_draft_from_instruction` |
| Command Center draft | `ops/draft.py` |
| Support widget (Vic) | `intake.py` |

### Checking it

```bash
python kb_sync.py --status     # index health: totals, languages, statuses
python kb_sync.py              # run a sync now
```

Or `GET /kb-sync/status` on the deployed app. Look at:

- `index.total` against the live article count in Zoho
- `index.by_status` (anything other than `Published` means old rows are
  still around; the next clean sync removes them)
- `last_fetch_complete` (false means the delete was skipped and recent
  articles in a failed category are missing)

---

## 2. Closed work (self-learning)

**Schedule:** weekly, Sundays 03:00 ET
(`ticket_analyzer.run_weekly_knowledge_refresh`, main.py)

Two sources feed one knowledge book:

| Source | Module | What it contributes |
| --- | --- | --- |
| Closed Zoho tickets | `ticket_analyzer.py` | how Sam actually writes: tone, phrasing, when to answer vs escalate |
| Closed ClickUp tasks | `clickup_knowledge.py` | what issues turn out to be: root cause, diagnostic questions, what the client was told |

ClickUp is the richer half. A closed task carries the engineering
diagnosis that a Zoho thread never shows.

### The flow

1. `run_full_analysis(limit=..)` analyses newly closed Zoho tickets into
   `analyzed_tickets`.
2. `run_clickup_knowledge_scan(limit=..)` mines newly closed ClickUp
   tasks into `analyzed_clickup_tasks`. Only `Closed` and
   `user education` statuses, and only tasks with enough substance to be
   worth a Claude call.
3. `generate_knowledge_book()` synthesises both into versioned rows in
   `knowledge_sections`: one per ticket category, plus `sams_voice` and
   `engineering_resolutions`.
4. `knowledge.clear_cache()` drops the read cache so live prompts pick
   the new book up immediately.

### How a section is synthesised (`knowledge_synthesis.py`)

This is the part that decides whether mining more actually helps.

The original synthesiser joined a category's summaries, cut the string
at 15,000 characters and sent that to Claude. A summary measures about
900 characters, so roughly **16 entries reached the model**, and since
`get_all_analyses` orders by `analyzed_at` ascending they were the 16
oldest-analysed. The "bug" section reported 266 source tickets and was
written from about sixteen of them. Mining more raised a counter and
changed nothing else.

Two changes fix that:

1. **`rank_and_select`** orders by `training_value` (high first), then
   recency, then round-robins across `module` so one noisy module cannot
   fill a section on its own. `MAX_ENTRIES_PER_SECTION` (400) caps how
   much any one section considers, which nothing hits today.
2. **`map_reduce_section`** batches the selected entries under the same
   15,000 character budget, extracts observations from each batch, then
   merges the notes into the final section. Coverage is no longer capped
   by one prompt window.

Categories that already fitted in a single call still take the direct
path, so small sections behave exactly as before.

Cost, measured against the real corpus shape:

| State | Calls | Runtime |
| --- | --- | --- |
| Today (665 tickets) | 44 map + 8 reduce | ~6 min |
| Fully backfilled (2,271) | 113 map + 8 reduce | ~14 min |
| Engineering (1,197 tasks) | 25 map + 1 reduce | ~3 min |

Every section carries a coverage line at the top saying what it was
built from, and `ticket_count` in `knowledge_sections` stays the full
category size so the status endpoint reports the real corpus rather than
the sampled slice.

**`sams_voice` is the exception and does not go through this.** It
aggregates deduplicated key phrases (capped at 100), follow-up questions
(50) and tone notes (80) across every analysed ticket, so it was never
prefix-truncated. It is effectively saturated at 665 tickets.

Each run is capped (`KNOWLEDGE_TICKET_LIMIT`, default 200;
`KNOWLEDGE_TASK_LIMIT`, default 150) so the historical backlog is worked
down over several weeks instead of one multi-hour run. Both passes are
resumable: anything already in the tables is skipped.

### Postgres is the store, not the filesystem

`knowledge_sections` is what gets read. The markdown under
`knowledge_book/` is a local debugging convenience only.

This matters. The old design wrote `knowledge_book/sams_voice.md` and
`intake.py` read it at import time. That could never work in production:
the dyno filesystem is ephemeral so the file was wiped on every restart,
and a prompt assembled at import could not pick up a regeneration anyway.
`knowledge.py` now reads from Postgres at request time behind a 15 minute
cache.

Note the exception: `knowledge_book/feature_catalog.md` is **not** part of
this pipeline. It is generated from landing-page strings by
`scripts/sync_landing_strings.py` and is committed to the repo, so
reading it from disk is correct.

### Checking it

```bash
python ticket_analyzer.py --weekly       # run the whole weekly pass now
python ticket_analyzer.py --limit 100    # tickets only, bounded
python ticket_analyzer.py --book-only    # re-synthesise, no new analysis
python clickup_knowledge.py --status     # what has been mined from ClickUp
python clickup_knowledge.py --limit 50   # mine a bounded batch
```

Or on the deployed app:

- `GET /knowledge-book/status`
- `POST /knowledge-book/refresh` (the full weekly pass)
- `POST /knowledge-book/clickup-scan?limit=150`

In the status payload, `live_knowledge.sections` is the one that matters.
It is what is actually being injected into prompts. Empty means nothing
has been learned yet, no matter how high the analysed counts are.

### Backfilling

The first pass has a real backlog: roughly 2,300 closed Zoho tickets and
1,300 closed ClickUp tasks. The weekly job chips away at it, but to fill
it in deliberately, run bounded batches:

```bash
python clickup_knowledge.py --limit 300
python ticket_analyzer.py --limit 300
python ticket_analyzer.py --book-only
```

Run the book generation last. It synthesises whatever is in the tables at
that moment.

---

## 3. The Setup Guide (published guidance)

**Schedule:** rebuilt with the weekly pass, or on demand.

Unlike the two pipelines above, this is not learned from closed work. It
is live editorial documentation, so `knowledge.py` labels it *published*
and tells the model it is authoritative.

The guide lives in two places and neither is complete alone:

| Where | What it holds |
| --- | --- |
| `vome-react` | The substance: 7 stages in order, 45 sections, the decisions each settles, 112 tips and warnings. Hardcoded in `setupGuideContent.js` and `translations/postlogin.js` under `sg_*` keys. |
| Zoho help center | The 45 articles the guide links to for detail. Already indexed by `kb_sync`. |

None of the first row was in Zoho, so none of it ever reached the agent,
and the second row only surfaced when a keyword search happened to
match. An admin asking "we are just getting started, where do we begin"
matched nothing and got nothing.

### The flow

1. `scripts/sync_setup_guide.py` reads the React source and emits
   `knowledge_book/setup_guide_source.{md,json}`. This mirrors
   `scripts/sync_landing_strings.py`, which does the same for landing
   page copy. Run it whenever the in-app guide changes.
2. `setup_guide.py` condenses that digest into the `setup_guide`
   section, resolving each article slug to its real help center URL from
   the index (falling back to the canonical URL shape for anything not
   yet synced).
3. `knowledge.py` carries the section on every prompt.

Deliberately a **map, not a copy**. The digest is ~80,000 characters,
which cannot ride on every draft, and the articles are already
retrievable on demand. The section's job is to let Vic place where an
admin is, warn about ordering that causes rework, and link the right
article.

```bash
python scripts/sync_setup_guide.py   # refresh the digest from vome-react
python setup_guide.py                # rebuild the section
python setup_guide.py --preview      # see what would be sent
```

Or `POST /knowledge-book/setup-guide` on the deployed app, which is cheap
and independent of ticket mining.

### Two separate article sets, easily confused

The 45 slugs referenced by `setupGuideContent.js` and the 13
`setup-decisions-*` articles in Zoho have **zero overlap**. The linked 45
are what the product actually points at. A permalink-prefix filter on
`setup-decisions` looks right and captures none of them.

---

## Gotcha: ClickUp comment blocks have no `type`

ClickUp returns a comment two ways: `comment_text` (a plain string) and
`comment` (a list of rich-text blocks). The blocks carry **no `type`
key**. Code that walked the blocks filtering on `block["type"] == "text"`
matched nothing and silently produced an empty string, which is what
happened to the Command Center's engineer notes, the thread view and the
ticket list's engineer comment.

Use `clickup_tasks.extract_comment_text(comment)`.

Do not reuse it for MCP responses. Those content blocks really do have a
`type`, and the parsers in `agent.py`, `kb_sync.py` and `main.py` are
correct as written.
