# Automation roadmap

What we automate next, and the rule that decides what actually ships.

Written 2026-09-14, after closing the help centre gap loop
(`KB_GAP_LOOP.md`).

---

## The one rule

**An automation ships if it runs on a schedule somewhere that executes
and reports to a channel a human already reads. Otherwise it does not
exist.**

We have direct evidence for this in our own repos.

`support-agent` works. Seven scheduled jobs, deployed, posting to Slack,
handling real tickets end to end. It gets improved continuously because
its output lands in front of someone every day.

`sales-agent` does not. Built 2026-02-27, code-complete: weekly Apollo
prospecting, Zoho sync, lead scoring, three-touch email generation, an
approval gate, Instantly dispatch, Zoho writeback, and a Next.js approval
dashboard. It has never run. All four tables hold zero rows, the sqlite
file has not been touched since 2026-02-28, and the repo is not in git.

The difference is not code quality or ambition. It is that one of them
has a daily reader and the other one has a folder.

Four months after `sales-agent`, `sdr-agent/nico` restated the same idea
as a design doc. That is the failure mode to watch for: writing the thing
a third time is easier than operating it once.

So the first question for any new automation is not "what should it do".
It is "where does it run, and who sees the output tomorrow morning".

---

## The ticket shape

Every automation task, and every code task handed to a worker, gets
written in this shape before anyone starts. Borrowed from Oren, trimmed
to what we actually need.

```
DELIVER    One paragraph. What exists at the end that does not exist now.
DONE       The observable check. A command that passes, a row that
           appears, a Slack message that lands.
BUDGET     The files the worker may read. Five or so, plus "ask before
           opening more". Too tight and it stops every time; too loose
           and it wanders.
STOP       What to come back and ask about instead of deciding alone.
           Anything outward facing goes here by default.
CLOSE      Recon first, stop for approval, then build, then the diff.
```

The recon-then-stop step is the part that pays. In a codebase with this
many surfaces (forms, scheduling, sequences, kiosk, mobile, Salesforce,
Zapier), a worker that starts editing before it has mapped what it
touches will break a surface it did not know existed.

---

## What is done

**Help centre gap loop.** See `KB_GAP_LOOP.md`. User education tickets
now reach the gap counter, signals cluster into topics monthly, and each
topic arrives as a ClickUp task with the article already drafted.

Outstanding before it drives anything: the ten-ticket answer key. Do not
treat the ranked topic list as roadmap input until the clusterer agrees
with a human on 8 of 10.

---

## Next, in order

### 1. The answer key harness (small, unblocks everything)

A reusable way to score any classifier we run against labels a human
wrote. We now have three in production making fuzzy judgment calls:
ticket classification in `intake.py`, `training_value` and
`kb_article_candidate` in `clickup_knowledge.py`, and the new topic
clustering in `kb_gap.py`. None of them has ever been scored against a
human.

Shape: a table of hand-labelled examples, a runner that replays them, a
report of agreement. Reuse the existing `analyzed_*` tables as the
example store rather than inventing a new one.

Why first: it is the cheapest thing on this list and every later item
gets more trustworthy because it exists.

### 2. Revive lead gen, do not rebuild it

Not a new build. Two existing attempts get reconciled:

- `sales-agent/backend` has the working plumbing. Keep it.
- `sdr-agent/nico` has the correct thinking. Its README already
  diagnoses why attempt 1 was wrong: the ICP was hardcoded and guessed,
  and one real Zoho pull disproved the guesses.

Two gaps to close, both of which are actual requirements and neither of
which exists in either attempt:

- **Suppression.** `job_apollo_prospecting` dedups on `apollo_id`
  against local sqlite only. It never checks whether the org is already
  in Zoho as a customer, an open deal, or closed lost. Nico's
  suppression list design is the right version and is unimplemented.
- **Per-lead campaign routing.** `send_approved_drafts(session,
  campaign_name=None)` takes one campaign for the whole batch. Deciding
  which campaign a lead belongs in does not exist.

**First ticket is not code.** It is: where does this run, and who reads
the proposed-leads message tomorrow. Answer that, then port Nico's
CRM-grounded ICP onto the `sales-agent` pipeline.

Nothing enters an outbound sequence without a human click for the first
month. We already learned this the expensive way: ticket #8945 sent a
model's refusal text to a client, which is why `outbound_guard.py`
exists. Outbound to prospects deserves the same guard, at minimum.

### 3. Weekly numbers pull

One table, same columns every week, in one place: new MRR, expansion,
churn, trials started, trials converted, pipeline by stage. Stripe plus
Zoho CRM.

This is the smallest automation on the list with the largest change in
how the week feels, because it turns "is ARR growing" from a feeling
into a Monday table. It also has an obvious reader and an obvious
channel, so it passes the one rule without any thought.

### 4. PR review skill

Turn the review checklist into a skill a worker runs on every PR before
a human looks: the five things actually looked for, with the exact
commands. Output is one line per finding, severity tagged, no praise.

Score it with the harness from item 1 against ten PRs already reviewed.
The same file doubles as the SOP a new engineer reads.

### 5. Open and close ritual

Only after the items above have been running long enough to trust. The
ritual reads the support queue, the numbers table, the gap report and
the PR digest, and lays out the day. It is worth little until those
inputs are reliable, and it is the thing most likely to be built first
out of enthusiasm and then ignored.

---

## What we are deliberately not doing

**A separate disposable-worker layer.** Oren's setup runs a chief of
staff that hands tickets to disposable Codex sessions, because he had no
execution substrate. We have one, and it is better: a deployed Python
service with a scheduler, Postgres, and Slack, ClickUp and Zoho already
wired in. Recurring work belongs in `support-agent` as a handler.
Disposable sessions are for code work, not for operations.

**A vector database or a memory product.** Markdown in git plus Postgres
FTS is doing the job. `kb_sync` already indexes article bodies with
language-aware stemming and it is enough.

**Auto-publishing anything outward facing.** Articles stop at a ClickUp
task. Outbound stops at an approval click. Client replies already stop at
`outbound_guard`. Every one of these is cheap to relax later and
expensive to unwind after it misfires once.

---

## Size discipline

Everything grows unless something forces it to shrink.

Every file an agent loads at startup gets a cap. Skills: 600 words. This
document: 1200. `guides/vic-support-workflows.md` is currently 36KB,
which is not a skill, it is a document nobody re-reads, and it should be
split into the procedures that are actually loaded on demand.

When a procedure is used, the last step is to propose a shortening.
