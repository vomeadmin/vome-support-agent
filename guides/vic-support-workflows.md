# Vic Support Workflows — Reference & Change Log (June 2026)

This document explains how the Vome support agent ("Vic") automates ticket
handling, and **why** each piece works the way it does. It captures the
June 2026 build-out that turned several human-reviewed draft flows into
sender-aware auto-send flows, centralized status/signature handling, and wired
the new ClickUp status board.

Audience: a future engineer or assistant continuing this work. It assumes no
prior context from the chat in which these changes were made.

---

## 1. The big picture

The agent is a FastAPI app (`main.py`) that reacts to webhooks from **Zoho Desk**
(the client-facing helpdesk), **ClickUp** (the engineering task board), and
**Slack** (the team's control surface). It uses Claude to classify tickets,
draft client replies, and make a few narrow "should we send this?" judgments.
Zoho is the client system of record; ClickUp is the engineering system of
record; Slack is the real-time human layer.

There are **two parallel reply paths** in the codebase — important context:

- **Webhook + Slack path** (`main.py` → handlers → `slack_reply_handler.py`):
  the live production flow. Everything in this document is about this path.
- **`/ops` Command Center** (`ops/` package, a React SPA + REST API): a separate
  dashboard/draft path. It was **not** migrated in this build-out and still
  references the old status vocabulary — see [§9 Open follow-ups](#9-open-follow-ups).

### The sender model (the core idea)

Client emails are signed by one of two "senders":

- **Vic** — the agent itself. Used for the **two auto-send categories** that go
  out **without human review**:
  1. **On-prod resolution replies** (engineer marks a fix live → client is told
     it's resolved).
  2. **Engineer-triggered needs-client-info requests** (engineer needs detail
     from the client → Vic asks for it).
- **Sam** — signed `Sam | Vome team`. Used for **everything a human reviews
  before it goes out** (new-ticket drafts, client-reply drafts, clarifying or
  feature replies in a sprint).

Effectively: *if it auto-sends, it's from Vic; if a human reviews it, it's from
Sam.* A small set of pre-existing automated emails keep their legacy
signatures (see [§4](#4-signatures-signaturespy)).

---

## 2. The ClickUp status board (source of the triggers)

The Priority Queue list (ClickUp space `90114113004`, list `901113386257`) uses
these statuses. Engineers move tasks between them; several drive webhook
automation:

| Status | Who sets it | What it triggers |
|---|---|---|
| `queued` | system / resurface | — |
| `in progress` | engineer | — |
| `needs client info` | engineer | **Auto-sends a Vic info request** → parks |
| `escalated` | engineer | **Posts an escalation card to #escalated-tickets** |
| `on dev` | engineer | — |
| `awaiting client response` | the agent (park state) | — (auto-resurfaces on client reply) |
| `sleeping` / `declined` | engineer / Sam | — |
| `on prod` | engineer | **Auto-sends a Vic resolution → closes ticket** |
| `Closed` | the agent | — |

> **Critical history:** the board was renamed to this vocabulary
> (`needs client info`, `escalated`, and a new `awaiting client response`
> column were added; the old `waiting on client` and `needs review` were
> removed) **before** the webhook code was updated. So for a while, setting
> `needs client info` or `escalated` triggered **nothing** — `main.py` was still
> matching the dead `waiting on client` / `needs review` names. Part of this
> build-out was rewiring those triggers. If you rename a board status again,
> you must update the matching constant in `status_constants.py` **and** the
> branch in `main.py`.

---

## 3. Status vocabulary — `status_constants.py`

All status strings were centralized into one module so a rename can't silently
miss a casing variant scattered across files. Five vocabularies live here; do
not conflate them:

1. **`CU_*`** — ClickUp status *names*, canonical lowercase, for **inbound
   matching**. Compare with `normalize_status(incoming) == CU_xxx`.
   Key ones: `CU_ON_PROD`, `CU_NEEDS_CLIENT_INFO` (`"needs client info"`),
   `CU_AWAITING_CLIENT` (`"awaiting client response"`), `CU_ESCALATED`
   (`"escalated"`), `CU_QUEUED`, `CU_IN_PROGRESS`, `CU_ON_DEV`.
2. **`CU_WRITE_*`** — exact strings we *send* to the ClickUp API. Casing is
   intentionally preserved per call site (ClickUp matches case-insensitively,
   but we keep the historical bytes). e.g. `CU_WRITE_CLOSED_TITLE = "Closed"`.
3. **`THREAD_*`** — values for our own `ticket_threads.status` column in
   PostgreSQL (internal state, not visible to anyone). e.g.
   `THREAD_WAITING_CLIENT = "waiting-client"` (the internal "parked" marker),
   `THREAD_ON_PROD_SENT`, `THREAD_ESCALATED`, `THREAD_CLOSED`.
4. **`ZOHO_*`** — Zoho Desk status display names we write, e.g.
   `ZOHO_AWAITING_CLIENT_RESPONSE = "Awaiting Client Response"`,
   `ZOHO_CLOSED`, `ZOHO_FINAL_REVIEW`. Plus **`ZNORM_*`** — the normalized
   lowercase keys used only by the dashboard scorer.
5. **`ACTION_*`** — Command Center action keys (`/ops` path only).

`normalize_status(value)` lowercases, unifies separators (`_`/`-`/spaces → one
space), and strips emoji/punctuation — so `"On Prod"`, `"on_prod"`, and
`"on prod ✅"` all resolve to `CU_ON_PROD`. **Inbound webhook matching in
`main.py` uses this normalizer.** (Note: `ops/tickets.py` deliberately does *not*
normalize — it matches raw lowercase — which is one reason the `/ops` dashboard
needs its own cleanup pass.)

---

## 4. Signatures — `signatures.py`

One sender-keyed source for the client-facing closing block.

- `signature(sender, lang="en")` → full closing block. Senders:
  - `vic` → `Best,\n\nVic\nsupport.vomevolunteer.com` (FR uses `Cordialement,`)
  - `sam` → `Best,\n\nSam | Vome team\nsupport.vomevolunteer.com` (FR `Cordialement,`)
  - `legacy_vome_team` → `Best,\n\nVome team\n…` — preserves the old auto-ack /
    completeness-gate signature byte-for-byte.
  - `legacy_sam_support` → `Best,\n\nSam | Vome support\n…` — legacy.
- `signature_name(sender)` → just the name line (for prompt instructions that
  phrase the sign-off their own way).
- `sign_message(text, sender, lang)` → the workhorse for **model-generated
  drafts**. It:
  1. **Strips any trailing sign-off the model emitted** (defends against
     double-signing — closing words, name lines, the domain line, separators).
  2. Appends the correct signature.
  3. For **compound** output (a `DRAFT RESPONSE` section followed by an
     `AGENT ANALYSIS` section), it places the signature at the **end of the
     draft body, before the analysis** — so the client-facing portion is signed
     correctly and the internal analysis is untouched.

### Why the prompt was changed

`system_prompt.md` previously baked `Vome team` / `Sam | Vome support` closings
into the voice guidelines and every example. Because **one prompt feeds drafts
that need different signers**, the prompt now instructs the model to **write the
body only — no closing or signature** ("a signature is appended automatically").
The signature is then attached in code per sender. This is what makes one set of
voice rules able to produce both Vic and Sam emails.

`system_prompt.md` also gained a **`## SIGNATURES AND AUTO-SEND`** section that
documents: the two Vic auto-send categories; that everything else is
Sam-reviewed; the **No-Confirmation Rule** (see [§6](#6-needs-client-info-auto-send));
the **ClickUp status vocabulary**; and that **feature requests stay with Sam and
are never auto-acknowledged** (what's asked for may already exist).

### Which sites are signed how (wiring)

| Call site | Sender |
|---|---|
| `agent.process_ticket` (sprint analysis/draft) | `sam` |
| `agent.process_ticket_update` (client-reply draft) | `sam` |
| `on_prod_handler._generate_resolution_draft` | `vic` |
| `clickup_waiting_client_handler._generate_need_info_message` | `vic` |
| auto-ack templates, completeness-gate reply | `legacy_vome_team` (unchanged) |
| auth-bypass replies | raw legacy format (unchanged) |
| `ops/draft.py` (Command Center) | still model-emitted `Sam | Vome support` — **not migrated** |

---

## 5. On-prod auto-send — `on_prod_handler.py`

**Trigger:** ClickUp task → `on prod` (engineer ships the fix).
**Entry point:** `handle_on_prod(task_id, engineer_name)`.

### Flow
1. Post a brief alert to `#eng-alerts`.
2. Extract the linked Zoho ticket (the `Zoho Ticket Link` custom field). No
   linked ticket → stop after the eng-alert.
3. Set Zoho status → `Final Review` (interim).
4. Fetch the Zoho ticket + full conversation thread.
5. **Pre-send review** — `_assess_resolution_state(...)` (a Claude call) reads
   the thread and decides whether a "it's fixed" email is still needed.
6. **If already resolved** → **do not send.** Close the Zoho ticket + close the
   ClickUp task, and post a "closed, no email — already resolved" record to
   Slack.
7. **Otherwise** → generate a **surface-level** Vic resolution draft, **auto-send
   it to the client**, close Zoho (`Closed`) + ClickUp (`Closed`), and post a
   "sent (Vic)" record showing exactly what went out.
8. **Fallback:** if it can't auto-send (no contact email, empty draft, or a send
   error) it degrades to the **old Slack-review flow** (posts the draft with
   `confirm`/`send`/`cancel`, leaves Zoho in `Final Review`) — so nothing is
   ever silently dropped.

### Why the pre-send review exists
Sam sometimes replies to a client out-of-band (e.g. an urgent Slack thread)
and forgets to flip the ClickUp task. If an engineer later marks it `on prod`,
a naive auto-send would email a **near-duplicate** "it's fixed" note. The review
reads the thread first: if a prior team reply already confirmed the fix — or the
client themselves confirmed it's working — Vic skips the email and just closes.
The "fixed" email is deliberately **basic and non-technical** (an update was
made, it should be resolved, please check and let us know) — no cause, no fix
details.

### Safety / tie-breakers
- **Defaults to send** when the review is uncertain or errors (a missed update
  is worse than a rare redundant note; duplicates only happen when the review is
  *confident* a confirmation already went out).
- **Loop-safe:** the outbound email isn't a client reply (so the resulting
  `zoho-update` webhook is ignored), and closing the ClickUp task fires a
  `Closed` status webhook that matches no trigger branch. `main.py` dedups
  ClickUp webhooks for 5 minutes to absorb retries.

---

## 6. Needs-client-info auto-send — `clickup_waiting_client_handler.py`

**Trigger:** ClickUp task → `needs client info` (engineer needs detail from the
client). **Entry point:** `handle_needs_client_info(task_id, engineer_name)`.

> The filename is legacy (`clickup_waiting_client_handler.py`) from when this
> status was called "waiting on client"; the behavior is the needs-client-info
> flow.

### Flow
1. Pull **engineer notes** = the ClickUp task description + comments. These tell
   Vic *what information to ask for*.
2. Fetch the Zoho ticket + thread.
3. **Pre-send review** — `_assess_info_request_state(...)` (Claude) returns one
   of:
   - **`send`** — the needed info hasn't been requested yet → generate the Vic
     request, **auto-send** it, then **park**: ClickUp → `awaiting client
     response`, Zoho → `Awaiting Client Response`, tag the ticket, post a record.
   - **`skip_already_asked`** — we already asked and are still waiting → **no
     duplicate**; just park.
   - **`skip_already_answered`** — the client already provided it → **re-queue**
     the task to `queued` for the engineer; no email.
4. **Fallback:** can't auto-send → Slack review draft (`confirm`/`send`/`cancel`).

### The No-Confirmation Rule
A needs-client-info request must **never confirm, diagnose, or describe the bug.**
Vic reads the engineer's note only to learn *what to ask*, then translates it
into a neutral, client-friendly request. It must not echo internal findings,
root-cause theories, or whether something is in fact broken. (This rule lives in
`system_prompt.md` under `SIGNATURES AND AUTO-SEND` and is reinforced in the
draft prompt.)

### Park + resurface lifecycle
- Parked tasks sit at `awaiting client response` (a ClickUp "done"-group column
  Sam created so it can be hidden from the active dev view). Internally the
  thread is marked `waiting-client`.
- When the client replies, the Zoho update webhook runs `process_ticket_update`,
  which (for a `waiting-client` thread) **re-queues the task to `queued`** and
  **logs the client's reply as a ClickUp comment** ("Client replied: …") so the
  engineer sees it.

### Why "default to send"
Same logic as on-prod: a blocked engineer (we failed to ask) is worse than a
rare redundant ask, so the review only skips when confident.

---

## 6A. No-action client replies (courtesy acks) — `process_ticket_update`

**Trigger:** a genuine inbound client reply on `/webhook/zoho-update` (same
`_is_client_reply` filter). **No email is ever sent on this path** — it only
realigns the Zoho status to mirror the linked ClickUp task, so courtesy
messages ("thanks!") stop sitting on the New/Processing dashboard.

> **Critical fix (June 2026): the update webhook now gates on event type.**
> `/webhook/zoho-update` fires for **every** ticket change, not just replies —
> including a status/assignee edit Sam makes by hand. `process_ticket_update`
> re-derives "the client replied" from the **latest conversation entry**, so a
> manual change to a ticket whose last message is from the client was mistaken
> for a reply and had its status **restored** — fighting Sam in a
> `Closed → Processing` loop every time he closed it (the "Support Agent" actor
> in the history is the shared `admin@` account the app posts as, so both sides
> of the flip look like the same user). `main.py` now calls
> `process_ticket_update` **only** when `is_zoho_reply_event(event_type, id,
> ticketId)` is true — i.e. a thread-add (eventType contains "thread", or the
> reply ID is in `id` while the real ticket ID is in `ticketId`). A bare field
> update runs **only** `sync_zoho_to_clickup` (Zoho→ClickUp, which never writes
> the Zoho status). Covered by `test_closed_reply_guard.py`.

### Ordering (critical)
The no-action check runs **before** the existing classify / resurface / draft
logic in `process_ticket_update`. So a courtesy reply on an
`awaiting client response` task is intercepted and **never resurfaces the task**
and **never creates a draft**. Action-needed replies fall straight through to
the unchanged existing behavior.

### Detection (precision over recall)
1. **OOO / autoresponder guard** (`_looks_like_auto_reply`): an out-of-office is
   *not* treated as a reply at all → the update returns early, ticket untouched.
2. **Attachment guard**: any attachment on the reply → action-needed.
3. **Action-signal keyword guard** (`_ACTION_SIGNAL_RE`): any obvious signal
   (`?`, `but`, `also`, `still`, `not working`, `doesn't`, `error`,
   `how/when/why`, `can you`, `help`, `again`, …) forces action-needed even if
   the model disagrees.
4. **Classifier** (`_classify_no_action_reply`, a cheap Haiku call): returns
   no-action **only** for pure acks ("thanks", "got it", "ok", "will do",
   "sounds good", "I'll check later", "appreciate it", closing pleasantries).
   Defaults to action-needed on uncertainty or error.

A false "action-needed" is safe (it just runs the normal flow); a false
"no-action" could wrongly move a status — so every guard biases toward
action-needed.

> **Critical fix (June 2026): the classifiers were running blind.** All of the
> above guards read the reply text from `latest.get("content")` — but Zoho's
> conversations **list** endpoint returns reply threads with a `summary` field
> and **no `content`** (only *comments* carry `content`). So every client reply
> was judged as empty text / `"(no text content)"`. The keyword guard never
> matched, and the ack classifier defaulted toward "no new info." On a *resolved*
> ticket this silently **re-closed** a "it's still broken, please re-check" reply
> (real incident: ticket #7905). Two changes fixed it:
> 1. `_extract_reply_text(latest)` now falls back to `summary` (and strips HTML),
>    so the guards and classifiers see the words the client actually wrote — the
>    same content→summary fallback `_format_conversations` already used.
> 2. **Re-closing a closed ticket now requires `_is_confident_ack`** (in
>    `process_ticket_update`), not a bare ack classification. A closed-ticket
>    reply re-closes **only** when there is readable text, **no** attachments,
>    **no** action signal, **and** the model agrees it's a pure ack. Any doubt →
>    the ticket is **re-opened** and reprocessed so a human sees it. Per Sam: be
>    highly confident to auto-close, otherwise the reply won't be seen. Covered
>    by `test_closed_reply_guard.py`.

### What happens on a no-action reply (`_handle_no_action_reply`)
- **No draft, no resurface, no email.** ClickUp is left exactly as-is.
- **Escalated → skip entirely** (leave in Processing for Sam). Matches both
  `escalated` and legacy `needs review` via `normalize_status`.
- Otherwise read the **live** ClickUp status (`_get_clickup_status`) and set the
  Zoho status to mirror it — **status only, the Zoho owner is never touched** —
  via the plain-string `_set_zoho_ticket_status` helper (never the object form,
  which silently no-ops):

  | ClickUp status (new / legacy) | Zoho status |
  |---|---|
  | `closed` / `done` | Closed |
  | `on prod` | Closed |
  | `awaiting client response` / `waiting on client` | Awaiting Client Response |
  | `queued` / `in progress` / `on dev` | In Progress |
  | (no linked ClickUp task) | Closed |
  | anything else (`sleeping`, `needs client info`, …) | left unchanged |

- Every auto-handled reply is logged to **#vome-agent-log**
  (`SLACK_CHANNEL_AGENT_LOG`): ticket ID, reply text, the ClickUp state read,
  and the Zoho status set — so misclassifications are easy to catch.

### Why
Clients routinely reply "thanks!" after a ticket is effectively done. Those used
to linger in New/Processing — or worse, resurface an awaiting-client task or
generate a needless draft. Now they silently realign Zoho to whatever ClickUp
says (the source of truth for engineering state) and get logged, with no
client-facing email. It is the read-only counterpart to the manual Zoho→ClickUp
sync in §8.

---

## 7. Escalations — `clickup_needs_review_handler.py`

**Trigger:** ClickUp task → `escalated`. **Entry point:**
`handle_escalated(task_id, engineer_name)`.

> Filename is legacy (was "needs review"); behavior is the escalation flow.

Posts a **structured escalation card** to the dedicated **#escalated-tickets**
Slack channel in real time, then marks the thread `escalated`. The card shows:
ticket #, task title, who escalated, account / tier / ARR, current assignee,
complexity, and Zoho + ClickUp links — with the channel thread as the discussion
space.

- Channel is read from `SLACK_CHANNEL_ESCALATIONS`, defaulting to `C0BB3JCT51A`
  (the "escalated tickets" channel). Override via env to move it.
- **Why a dedicated channel:** escalations are time-sensitive (engineer stuck,
  or — once built — a high-value client thread turning negative). A dedicated,
  escalations-only channel with Sam + both engineers keeps signal high and
  discussion in one place; ClickUp stays the system of record.
- The card is posted **without an @-mention** by default (the dedicated channel
  is already the signal). Adding `@Sam` / `@assignee` is a small change if wanted.

> **Note:** "escalated" today is **engineer-triggered only**. The SOP's
> *automatic* escalation (an Enterprise/Ultimate or >$1,500 CAD ARR account whose
> thread turns urgent/negative) is **not built** — see [§9](#9-open-follow-ups).

---

## 8. Zoho → ClickUp status sync — `agent.sync_zoho_to_clickup`

Runs on every `/webhook/zoho-update`. Keeps the ClickUp task in sync when a Zoho
ticket's status changes (e.g. Sam changes it by hand):

| Zoho status | ClickUp task | Thread |
|---|---|---|
| `Closed` / `Resolved` | → closed | `closed` |
| `Awaiting Client Response` | → `awaiting client response` (parked) | `waiting-client` |
| reassigned to Sam/Ron, or unassigned | → closed | — |

The `Awaiting Client Response` branch is checked **before** the assignee rules,
so a Sam-owned ticket set to awaiting is **parked, not closed** (the assignee
rule would otherwise close any non-engineer-owned task). Setting the thread to
`waiting-client` means a later client reply auto-resurfaces it to `queued` (same
machinery as §6).

This is the reverse direction of the auto-send flows: those push Zoho status
*from* the agent; this pulls a manual Zoho status change *into* ClickUp.

---

## 9. Configuration & setup

### Environment variables (key ones)
- `ANTHROPIC_API_KEY`, `ZOHO_DESK_MCP_URL`, `ZOHO_CRM_MCP_URL`, `ZOHO_ORG_ID`
  (the org is `736165782`).
- `CLICKUP_API_TOKEN`, `SLACK_BOT_TOKEN`.
- `ZOHO_FROM_ADDRESS` — the address client emails are sent from.
- Slack channels: `SLACK_CHANNEL_SUPPORT_FINAL_REVIEW`,
  `SLACK_CHANNEL_VOME_TICKETS`, `SLACK_CHANNEL_FINISHED_TASKS`,
  `SLACK_CHANNEL_ESCALATIONS` (defaults to `C0BB3JCT51A`), etc.

### Webhooks
- **Zoho Desk** → `/webhook/zoho-ticket` (new ticket) and `/webhook/zoho-update`
  (updates, incl. status changes and client replies).
- **ClickUp** → `/webhook/clickup-status` (must subscribe to both
  `taskStatusUpdated` and `taskAssigneeUpdated`).
- **Slack** events → `/webhook/slack-events`.

### Slack app
No app/manifest/scope changes are needed to add a channel. The bot already uses
`chat:write`. To post to a **new** channel, the bot just needs to be a **member**
— `/invite` it into the channel. (A `not_in_channel` error in logs means it
wasn't invited.) This is the only manual step required for #escalated-tickets.

### ClickUp board
The Priority Queue must contain the status columns listed in [§2](#2-the-clickup-status-board-source-of-the-triggers).
If you rename one, update `status_constants.py` + the matching branch in
`main.py`.

---

## 10. How it was validated

Changes were dry-run against **real tickets** via the live Zoho/ClickUp data
(reading only — no emails sent, no statuses changed):

- **On-prod:** verified it **suppresses** a duplicate when the client already
  confirmed the fix, and **sends** a clean confirmation when only a generic ack
  had been sent.
- **Needs-client-info:** verified it **skips** (no duplicate) on tickets where
  repro steps / details had already been requested twice and were still pending
  (incl. a French thread), and **sends** a targeted question where none had been
  asked. Surfaced that the quality of the request tracks the quality of the
  engineer's note.
- `signatures.sign_message` was unit-tested for clean EN/FR drafts, the
  double-sign guard, and compound (draft + analysis) placement.
- **No-action replies (§6A):** the OOO guard, the action-signal keyword guard,
  and the ClickUp→Zoho mapping were validated against the real board statuses
  (incl. the escalated-skip and "unmapped → leave unchanged" cases). The
  classifier itself is a live Haiku call, backstopped by the deterministic
  guards and a default-to-action-needed.

All touched modules compile (`py_compile`) and pass `pyflakes` (no undefined
names / no new unused imports; remaining lint is pre-existing style).

---

## 11. Open follow-ups

1. **`/ops` Command Center is on the old vocabulary.** `ops/tickets.py` (dashboard
   filters), `ops/send.py` (status writes), and `ops/draft.py` still reference
   `waiting on client` / `needs review`, which no longer exist on the board. The
   dashboard won't categorize awaiting/escalated tasks correctly and
   `ops/send.py` could write an invalid status. Needs a dedicated migration to
   `needs client info` / `awaiting client response` / `escalated`.
2. **`ops/draft.py` still relies on a model-emitted signature** (`Sam | Vome
   support`) rather than `sign_message`. Migrate it to the programmatic signer.
3. **Automatic escalation is not built.** Escalated is engineer-triggered only.
   The SOP's rule — auto-escalate + ping Sam when an Enterprise/Ultimate or
   >$1,500 CAD ARR account's thread turns urgent/negative — needs: a
   sentiment/urgency signal (none exists today; there is no sentiment scoring),
   the tier/ARR available at send time (CRM enrichment is on the thread record),
   and **currency handling** (ARR is treated as a bare number today; CAD vs USD
   is not considered).
4. **Auto-acknowledgment survival is undecided.** The new-ticket auto-ack still
   sends on engineer-assigned tickets (legacy `Vome team` signature). Whether it
   stays is an open product decision. **Update (June 2026):** it is now
   **suppressed on agent-created tickets** — when Sam opens a ticket by hand in
   Zoho Desk on a client's behalf, no auto-ack fires so he controls the first
   reply. Detection is `_is_agent_created(source_type)` in `agent.py`, keyed on
   Zoho's `source.type == "SYSTEM"` (client-submitted email/web tickets carry
   their channel's type instead). The guard sits on the auto-ack call in
   `process_ticket`; the Slack draft, classification, and engineer assignment
   still run as normal.
5. **Optional @-mentions** on escalation cards (and possibly on-prod/needs-info
   records) if the dedicated channel isn't enough signal.

---

## 12. File map (what changed in this build-out)

| File | Role |
|---|---|
| `status_constants.py` | Central status vocabulary + `normalize_status()`. Added `CU_NEEDS_CLIENT_INFO`, `CU_AWAITING_CLIENT`, `CU_ESCALATED`, `THREAD_ESCALATED`. |
| `signatures.py` | Sender model + `sign_message()` (strip-then-append, compound-aware). |
| `system_prompt.md` | Removed baked-in signatures; added `SIGNATURES AND AUTO-SEND`, status vocabulary, No-Confirmation Rule, feature-request rule. |
| `main.py` | Webhook triggers rewired: `on prod` → on-prod; `needs client info` → needs-client-info; `escalated` → escalations. |
| `agent.py` | Programmatic Sam signature on sprint drafts; `sync_zoho_to_clickup` gained the `Awaiting Client Response` branch; resurface logs client reply as a ClickUp comment (`_add_clickup_comment`); **no-action courtesy-reply handler** (OOO guard + keyword guard + Haiku classifier → mirror Zoho to ClickUp, no email/draft/resurface) running before the resurface/draft logic. |
| `on_prod_handler.py` | Auto-send + close (Zoho + ClickUp) + pre-send resolution review + Slack record; Slack-review fallback. |
| `clickup_waiting_client_handler.py` | Needs-client-info auto-send + park + pre-send review (send/skip-asked/skip-answered) + fallback. |
| `clickup_needs_review_handler.py` | Escalation card → #escalated-tickets; trigger rewired to `escalated`. |
| `slack_reply_handler.py` | Manual-confirm path now writes `awaiting client response` (was the dead `WAITING ON CLIENT`). |

---

*Last updated: June 2026. If you change a workflow, update the matching section
here and the file map so this stays the single source of truth for the why.*
