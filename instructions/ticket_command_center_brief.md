# Vome Ticket Command Center — Project Brief
**Version 1.0 — April 2026**
**Feed this document into your Claude Code project as CLAUDE.md**

---

## Table of Contents

1. [What We're Building and Why](#1-what-were-building-and-why)
2. [Architecture Decision (Final)](#2-architecture-decision-final)
3. [The Dashboard — Core View](#3-the-dashboard--core-view)
4. [Ticket Detail — Thread View](#4-ticket-detail--thread-view)
5. [The Draft Reply System](#5-the-draft-reply-system)
6. [Action Flows — Every Possible State](#6-action-flows--every-possible-state)
7. [Status Sync Logic — Zoho ↔ ClickUp](#7-status-sync-logic--zoho--clickup)
8. [Engineer Comment Integration](#8-engineer-comment-integration)
9. [Priority Scoring Algorithm](#9-priority-scoring-algorithm)
10. [API Endpoints to Build](#10-api-endpoints-to-build)
11. [File Structure](#11-file-structure)
12. [Environment & Config Reference](#12-environment--config-reference)
13. [What Not to Touch](#13-what-not-to-touch)

---

## 1. What We're Building and Why

### The Problem

Vome runs support with a lean team: Sam (CEO, primary reviewer), two engineers (Sanjay — frontend, OnlyG — backend), and Ron (sales/field). The current support workflow requires Sam to context-switch between three separate tools to handle a single ticket:

- **Zoho Desk** — read the thread, draft a reply, update the ticket status
- **ClickUp** — update assignee, change task status, add notes
- **Slack** — communicate with engineers, post updates

This is unsustainable. Every ticket resolution requires 6–10 manual steps across three UIs. The result: tickets sit longer than they should, engineers get pinged about things that aren't ready for them, and Sam spends more time managing tools than managing outcomes.

### What We're Building

A **Ticket Command Center** — a single-page internal web application that replaces Zoho Desk as Sam's primary interface for managing support. It lives inside the existing FastAPI intelligence layer (already deployed on Railway) and is only accessible to the Vome team.

**Zoho Desk becomes the database.** It stores ticket threads, email history, client communications, and audit trail. Sam no longer needs to open it directly.

**The Command Center becomes the interface.** It shows a prioritized, enriched view of every ticket that needs attention, with every possible action available in one or two clicks. Drafting, assigning, sending, closing, requesting info — all from one screen, all synced to both Zoho and ClickUp simultaneously.

### What This Enables

- Sam opens one URL in the morning and knows exactly what needs his attention, in order of importance
- Drafting a reply takes 30 seconds instead of 3 minutes
- Assigning to an engineer updates Zoho owner AND ClickUp assignee in one click
- Tickets waiting on a client response disappear from the engineer queue automatically
- When engineers move a task to "needs review" or "waiting on client" in ClickUp, it immediately surfaces in the Command Center with their notes as context for drafting the right follow-up
- Zoho Desk `support.vomevolunteer.com` remains live but exclusively for clients to track their existing ticket status — never for Sam to manage work

---

## 2. Architecture Decision (Final)

```
COMMAND CENTER FRONTEND (React SPA)
  served by FastAPI on Railway
  accessible at: ops.vomevolunteer.com (or internal URL)
  auth: simple token-based, team-only access
        ↕
FASTAPI INTELLIGENCE LAYER (existing app on Railway)
  new endpoints added alongside existing webhooks
  reads: Zoho Desk MCP, ClickUp REST API, PostgreSQL thread map
  writes: Zoho Desk (status, owner, draft replies), ClickUp (assignee, status)
  calls: Claude API for draft generation (existing pattern)
        ↕
ZOHO DESK — ticket database, email transport, client portal
CLICKUP — engineering task queue
POSTGRESQL — thread map, event dedup (existing schema)
SLACK — existing notifications (unchanged)
```

### What Is Not Changing

- All existing webhook endpoints (`/webhook/zoho-ticket`, `/webhook/zoho-update`, `/webhook/clickup-status`) remain exactly as-is
- The existing `agent.py` classification pipeline is unchanged
- The existing Slack notifications continue to fire
- The PostgreSQL schema is not altered (add columns only if needed, never remove)
- `system_prompt.md` and `response_templates.md` loading at runtime — unchanged
- The Zoho customer portal at `support.vomevolunteer.com` stays live for clients

---

## 3. The Dashboard — Core View

### What Sam Sees When He Opens the App

A single-page prioritized ticket queue. Every ticket that requires his attention — meaning Zoho status is **New**, **Processing**, or **Final Review**, OR ClickUp status is **Needs Review** or **Waiting on Client** — appears as a card. Sorted by composite priority score (see Section 9).

### Ticket Card — Information Displayed

Each card shows everything Sam needs to make a decision without opening anything:

```
┌─────────────────────────────────────────────────────────────┐
│ [P1 — Urgent] [Enterprise] [Needs review]        #6960  CU↗ │
│                                                              │
│ University of Maryland Medical Center                        │
│ Unread chat message indicator not surfacing the message      │
│                                                              │
│ Laurel sees 1 unread in Chat nav but can't find which        │
│ message is unread. Clear repro steps. Sanjay assigned.       │
│                                                              │
│ [Chat] [Web]  ⏱ 2 days ago  Assigned: Sanjay               │
│                                                              │
│ ─────────────────────────────────────────────────────────── │
│ [Draft reply ↗] [View thread ↗] [Assign Sanjay ↗]          │
│ [Assign OnlyG ↗] [Request info ↗] [Park ↗] [Close ↗]       │
└─────────────────────────────────────────────────────────────┘
```

**Priority badge** — P1 (red left border), P2 (amber left border), P3 (neutral left border)

**Tier badge** — Enterprise (blue), Pro (green), Recruit (gray), Unknown (gray)

**Status badge** — New (red), Processing (amber), Final Review (purple), Needs Review (red), Waiting on Client (amber)

**ARR** — shown if known from CRM, e.g. `$2,640/yr`

**Ticket number** — Zoho ticket number, e.g. `#6904`

**CU link** — direct link to ClickUp task (opens in new tab)

**Org name + subject** — pulled from Zoho ticket title

**2-line summary** — Claude-generated summary of the thread, stored in PostgreSQL thread map on ticket creation

**Missing info callout** — amber warning bar if the ticket is flagged as incomplete, showing exactly what's missing, e.g. `⚠ Need: affected volunteer email + which form/opportunity`

**Engineer note callout** — if a ClickUp comment from Sanjay or OnlyG exists, shown as a blue bar: `💬 Sanjay: "Upload button is absent in Step 8 config — need to know if admin disabled it or if it's a code issue"`

**Module tag** — e.g. `Admin Scheduling`, `Forms`, `Sequences`

**Platform tag** — `Web`, `Mobile`, `Both`

**Age** — time since last activity

**Assignee** — current assignee, shown in red if unassigned

### Filter Tabs

```
[All active] [P1 only] [Bugs] [Needs review] [Waiting on client] 
[Final review] [Resolved today] [Unassigned]
```

- Default view is **All active** — excludes resolved/closed tickets
- **P1 only** — urgent priority tickets regardless of status
- **Needs review** — ClickUp status = needs review OR Zoho status = new/processing with incomplete info
- **Waiting on client** — ClickUp status = waiting on client, Zoho status = on hold
- **Final review** — Zoho status = final review (bug resolved, needs resolution reply drafted)
- **Unassigned** — no ClickUp assignee set

### Summary Stats Row

```
[ 11 need response ]  [ 8 needs review ]  [ 4 waiting on client ]  [ 3 resolved today ]
```

---

## 4. Ticket Detail — Thread View

When Sam clicks **View thread** on any card, a slide-in panel (right side, ~50% width) opens without leaving the dashboard. The panel shows the full Zoho Desk conversation thread inline — no redirect to Zoho.

### Thread Panel Contents

```
┌──────────────────────────────────────────────┐
│ Rising Starr Horse Rescue — #6957        [✕] │
│ Bonnie Troy · bonnie@risingstarrhorserescue.org│
│ Enterprise · $1,440/yr · Sanjay assigned      │
│ ─────────────────────────────────────────────│
│ [Thread] [ClickUp notes] [Actions]           │
│ ─────────────────────────────────────────────│
│                                              │
│  Apr 10, 09:44 — Bonnie Troy (client)        │
│  "Good Morning, this person is having        │
│  problems signing into VOME, Can you         │
│  please help?"                               │
│  📎 1 attachment                             │
│                                              │
│  Apr 10, 09:45 — Support Agent (outbound)    │
│  "Hi Bonnie, thanks for reaching out..."     │
│                                              │
│ ─────────────────────────────────────────────│
│ [Draft reply] [Request info] [Close] [Park]  │
└──────────────────────────────────────────────┘
```

**Three tabs in the panel:**

1. **Thread** — the full Zoho conversation, rendered cleanly. Inbound messages (client) left-aligned with client name. Outbound (team) right-aligned. Internal notes shown with gray background labeled "Internal note." Attachments shown as clickable links that open in new tab.

2. **ClickUp notes** — pulls all comments from the ClickUp task. Shows engineer name, timestamp, and comment text. This is where Sanjay/OnlyG leave notes about what they found, what they need, or what they fixed.

3. **Actions** — a clean panel with all available actions for this specific ticket, context-aware based on current state (see Section 6).

### Attachment Handling

If a thread message has attachments, show a paperclip indicator and filename. Clicking opens the Zoho attachment URL in a new tab. The agent should note in the thread view if an attachment is present but has not been reviewed (i.e., the ClickUp task description says "attachment included — likely contains important context").

---

## 5. The Draft Reply System

This is the core intelligence feature. Every draft is generated by Claude using the full ticket context and the existing `response_templates.md`. Sam never writes a reply from scratch.

### Draft Generation Flow

When Sam clicks **Draft reply** (from the card or the thread panel):

1. The system fetches the full Zoho thread (all conversations)
2. Fetches ClickUp task description and all comments
3. Constructs a prompt for Claude with:
   - Full thread history
   - CRM context (org, tier, ARR)
   - ClickUp task status and engineer comments
   - The action type Sam is taking (reply, request info, resolution, closure)
   - All 20 response templates from `response_templates.md`
   - Instruction: match tone to tier, draft in the client's language (French detection), never use em-dashes, sign as "Sam | Vome support"
4. Returns a draft reply shown in an editable text area

### Draft UI

```
┌──────────────────────────────────────────────────┐
│ Draft reply — Rising Starr #6957                 │
│                                                  │
│ Context: Bonnie forwarded truncated volunteer    │
│ login issue. Attachment present. No vol. email.  │
│                                                  │
│ ┌────────────────────────────────────────────┐   │
│ │ Hi Bonnie,                                 │   │
│ │                                            │   │
│ │ Thanks for flagging this. To look into     │   │
│ │ the login issue for your volunteer, could  │   │
│ │ you share their email address? That will   │   │
│ │ let us pull up their account directly.     │   │
│ │                                            │   │
│ │ Best,                                      │   │
│ │ Sam | Vome support                         │   │
│ │ support.vomevolunteer.com                  │   │
│ └────────────────────────────────────────────┘   │
│                                                  │
│ [Redraft ↗] [Edit manually] [Send] [Discard]    │
│                                                  │
│ Status on send: [ Waiting on client ▾ ]          │
│ ClickUp on send: [ Close temporarily ▾ ]         │
└──────────────────────────────────────────────────┘
```

**Redraft button** — calls Claude again with the same context plus a note: "Previous draft was not right. Regenerate with more [friendly / specific / concise] tone." Sam can type a redraft instruction in plain English: "Ask specifically about the passport upload, not login" and the redraft incorporates it.

**Edit manually** — makes the text area directly editable. Sam can type or paste changes.

**Status on send** — dropdown showing what Zoho status to set when the reply is sent. Pre-populated intelligently based on action type (see Section 6). Options: Open, Processing, On Hold, Final Review, Closed.

**ClickUp on send** — dropdown showing what to do with the ClickUp task when the reply is sent. Pre-populated based on action type. Options: Leave as-is, Close temporarily, Move to In Progress, Move to Waiting on Client, Mark Done.

**Send button** — sends the reply via Zoho Desk draftsReply API, sets Zoho ticket status, updates ClickUp task status and assignee as configured, posts an internal note to Zoho confirming the action taken.

### Draft Types

Claude generates different draft types based on context. The system auto-detects which type is needed, but Sam can override:

| Draft type | When used | Zoho status after send | ClickUp after send |
|-----------|-----------|----------------------|-------------------|
| **Acknowledge + looking into it** | New ticket, assigned to engineer | Processing | In Progress |
| **Request more info** | Ticket is incomplete — missing user email, module, etc. | On Hold | Close temporarily |
| **Resolution sent** | Bug fixed, ClickUp task moved to On Prod, resolution drafted | Final Review | Done |
| **Admin action required** | Issue needs the org admin to take action (re-open seq step, fix config) | Processing | Waiting on Client |
| **Close — no action needed** | User education issue, already resolved, duplicate | Closed | Done |
| **Escalation acknowledgment** | P1 bug, Enterprise tier, need to set expectation of urgency | Processing | In Progress (keep assigned) |

---

## 6. Action Flows — Every Possible State

This section defines exactly what happens for every action Sam can take, including all side effects across Zoho and ClickUp.

### Flow A — New ticket, enough info, assign to engineer

**Trigger:** Sam reviews a new ticket card. The issue is clear and reproducible. He clicks "Assign Sanjay."

**Steps:**
1. Draft an "acknowledge + looking into it" reply (auto-generated, shown for review)
2. Sam approves and clicks Send
3. Zoho: ticket status → `Processing`, ticket owner → Sanjay's agent ID (`569440000023159001`)
4. ClickUp: task assignee → Sanjay (`4434086`), task status → `In Progress`
5. Slack: post to `#vome-support-engineering` — "Ticket #XXXX assigned to Sanjay: [subject] — [Zoho link] [ClickUp link]"
6. PostgreSQL: update `ticket_threads` row — status, assignee, last action timestamp
7. Card disappears from "All active" view (ticket is now in engineer's hands, no Sam action needed until engineer updates)

### Flow B — New ticket, incomplete info, request more info

**Trigger:** Sam reviews a ticket. Missing volunteer email, module unclear, or issue too vague. He clicks "Request info."

**Steps:**
1. Claude generates a targeted info-request draft based on what's specifically missing (from the completeness check in the thread map — or re-run if needed)
2. Draft is shown in the panel. Sam reviews, optionally edits, clicks Send
3. Zoho: ticket status → `On Hold` (pauses SLA clock), ticket owner → Sam's agent ID (he owns it until info arrives)
4. ClickUp: task status → `Waiting on Client` (or close temporarily if the task is not yet worth engineers seeing)
5. PostgreSQL: flag `pending_info: true` on the thread map row
6. Card moves to "Waiting on client" filter in the dashboard
7. When client replies: existing `/webhook/zoho-update` fires → agent processes reply → posts internal note → card reappears in "All active" with updated context

**Note on "close temporarily":** When awaiting client info, the ClickUp task should be set to a status that removes it from the engineer's active view. Use `Waiting on Client` status in ClickUp — engineers filter this out by default. The task is NOT deleted. It reactivates when the webhook fires and the agent detects a client reply.

### Flow C — Engineer moves ClickUp to "needs review"

**Trigger:** Sanjay or OnlyG has investigated the issue and needs more information from the client to proceed. They update the ClickUp task to `Needs Review` and leave a comment explaining what they found and what they need.

**Steps (automated — no Sam action needed to surface this):**
1. ClickUp webhook fires → `/webhook/clickup-status` detects status = `needs review`
2. Agent fetches ClickUp task comments to get the engineer's note
3. Agent fetches the full Zoho ticket thread
4. Agent generates a draft reply that:
   - Is warm and friendly to the client
   - Explains what the team looked into
   - Asks specifically for what the engineer noted they need
   - Does NOT expose internal engineering language to the client
5. Ticket card appears in Sam's dashboard under "Needs review" with:
   - Engineer's comment shown as a blue callout: "💬 Sanjay: [their note]"
   - The generated draft pre-loaded and ready to review
6. Sam opens the card, reviews the draft, optionally redrafts, clicks Send
7. Zoho: status → `On Hold`, ClickUp: status → `Waiting on Client`

**This is the key workflow.** Engineers update ClickUp, Sam handles client communication. The two lanes stay clean.

### Flow D — Engineer moves ClickUp to "waiting on client"

Same as Flow C. The distinction between "needs review" and "waiting on client" from the engineer's perspective is subtle — treat them identically in the dashboard. Both mean: engineer has done what they can for now, client input is needed, Sam needs to draft and send the follow-up.

### Flow E — Bug fixed, ClickUp moves to "on prod"

**Trigger:** Engineer moves ClickUp task to `On Prod`.

**Steps (existing flow — do not change):**
1. `/webhook/clickup-status` fires (existing handler)
2. Existing `on_prod_handler.py` generates resolution draft
3. Posts to Slack thread with confirm/send/cancel
4. Sam confirms in Slack → reply sent to client
5. Zoho: status → `Final Review` (awaiting client confirmation that it's fixed)
6. ClickUp: status → `On Prod` (stays there until closed)

**Dashboard behavior:** After resolution is sent, ticket appears in "Final review" filter with a "Close ticket" action. When Sam clicks Close: Zoho → `Closed`, ClickUp → `Done`.

### Flow F — Close a ticket (no further action needed)

**Trigger:** Ticket is resolved (client confirmed), duplicate, user education issue, or otherwise needs no more attention.

**Steps:**
1. Claude generates a brief, warm closure note if the last outbound message didn't already close the loop
2. Sam reviews, optionally sends the closure note
3. Zoho: ticket status → `Closed`
4. ClickUp: task status → `Done`, resolution field set to `Completed`
5. PostgreSQL: thread map row updated, `status: closed`
6. Card removed from all active views, available in "Resolved today" filter for audit

### Flow G — Park a ticket

**Trigger:** Ticket needs attention but not today — waiting on something external, low priority, or Sam needs to follow up manually later.

**Steps:**
1. Sam clicks "Park" — a modal asks for optional note and optional wake date
2. Zoho: ticket status → `On Hold`
3. ClickUp: task status → `Sleeping`, Wake Date field set if provided
4. Card moves to a "Parked" state — not visible in "All active," visible in a future "Parked" filter

---

## 7. Status Sync Logic — Zoho ↔ ClickUp

This is the authoritative mapping of what statuses mean and how they stay in sync. **This is the source of truth for all sync logic in the codebase.**

### Status Mapping Table

| Situation | Zoho Desk Status | ClickUp Status | Who Owns It |
|-----------|-----------------|----------------|-------------|
| New ticket just arrived | New | Queued | Sam |
| Acknowledged, engineer assigned | Processing | In Progress | Engineer |
| Needs more info from client | On Hold | Waiting on Client | Sam (watching) |
| Engineer investigating, update coming | Processing | In Progress | Engineer |
| Engineer needs client info | On Hold | Needs Review → Waiting on Client | Sam (to draft) |
| Bug fixed, on production | Final Review | On Prod | Sam (to close loop) |
| Client confirmed fix | Closed | Done | — |
| Ticket parked / sleeping | On Hold | Sleeping | Sam (on wake date) |
| Ticket closed — no action needed | Closed | Done | — |
| Ticket closed — declined feature | Closed | Done (resolution: Declined) | — |

### Sync Rules

**Zoho → ClickUp (via webhooks, existing):**
- New Zoho ticket → creates ClickUp task (existing `agent.py`)
- Client reply to On Hold ticket → sets ClickUp back to Queued/Needs Review (to be implemented in `/webhook/zoho-update`)

**ClickUp → Zoho (via webhooks, existing + additions):**
- ClickUp `On Prod` → triggers resolution draft (existing `on_prod_handler.py`)
- ClickUp `Needs Review` or `Waiting on Client` → surfaces in dashboard as needing Sam's attention (new dashboard query)
- ClickUp `Done` → does NOT automatically close Zoho ticket (Sam closes from dashboard after confirming)

**Dashboard → Both (new, via Command Center actions):**
- All actions taken from the Command Center update both systems simultaneously
- The Command Center is the only place Sam takes action — never Zoho Desk directly, never ClickUp directly (except engineers updating their task status/comments)

### The "Waiting on Client" Rule (Critical)

When a ticket is waiting on client input:
- Zoho status: `On Hold` — this pauses the SLA clock and signals to the client portal that we're waiting on them
- ClickUp status: `Waiting on Client` — this removes the task from the engineer's active sprint view
- Engineers should NOT see tickets in `Waiting on Client` status as part of their workload
- The task reactivates automatically when the client replies (webhook fires, agent re-processes, card reappears in Sam's dashboard)

---

## 8. Engineer Comment Integration

Engineers (Sanjay, OnlyG) communicate what they've found by leaving comments on ClickUp tasks. They do not send emails or Slack messages about ticket status — ClickUp is their communication channel for ticket updates.

### How Comments Are Used

When the Command Center fetches a ticket for display, it also fetches all ClickUp task comments. Engineer comments are:

1. **Displayed as a callout in the ticket card** — shown as a blue bar with the engineer's name: `💬 Sanjay: "Upload button absent in Step 8 — may be a config issue on IMCA's end or a code regression. Need to know when this started happening."`

2. **Fed into the draft reply prompt** as context — Claude reads the engineer's note when generating a draft and uses it to inform what to ask the client, but translates it out of engineering language into friendly client-facing language.

3. **NOT shown to the client** — engineer comments are internal ClickUp notes only. They inform the draft but never appear in outbound messages.

### Comment Format Guidance for Engineers

Engineers should leave comments in this format (document this for them separately):

```
STATUS: [what I found]
NEED: [what I need from the client to proceed]
NOTES: [any other relevant context]
```

Example:
```
STATUS: Confirmed the upload button is absent in Step 8 of the sequence 
        when the document type is set to "Passport." Driver's license 
        type shows the button correctly.
NEED: When did this start? Did it ever work? Any recent changes to 
      the sequence configuration by the org admin?
NOTES: Likely a regression from the forms update — checking git history
```

The draft system will use the `NEED:` section to generate the client-facing question, but phrase it warmly.

---

## 9. Priority Scoring Algorithm

Every ticket gets a composite priority score at query time. Cards are sorted by this score descending. The formula:

```python
def compute_priority_score(ticket: dict) -> int:
    score = 0
    
    # Base score from ClickUp auto_score field (0-100)
    score += ticket.get("auto_score", 0)
    
    # Tier weight
    tier_weights = {
        "Ultimate": 50,
        "Enterprise": 40,
        "Pro": 25,
        "Recruit": 10,
        "Prospect": 5,
        "Volunteer": 0,
        "Unknown": 5
    }
    score += tier_weights.get(ticket.get("tier", "Unknown"), 5)
    
    # ARR weight (normalized — every $1,000 ARR = 5 points, max 50)
    arr = ticket.get("arr_dollars", 0) or 0
    score += min(50, int(arr / 1000) * 5)
    
    # Priority level from ClickUp
    priority_weights = {"urgent": 40, "high": 25, "normal": 10, "low": 0}
    score += priority_weights.get(ticket.get("priority"), 0)
    
    # Status urgency
    status_weights = {
        "new": 20,           # New ticket — needs immediate triage
        "processing": 5,     # Being handled
        "needs_review": 15,  # Engineer flagged — needs Sam's draft
        "final_review": 10,  # Resolution sent — needs closure confirmation
        "waiting": 0         # Waiting on client — no action needed yet
    }
    score += status_weights.get(ticket.get("zoho_status_normalized"), 0)
    
    # Age penalty — tickets sitting too long get a bump
    days_since_update = ticket.get("days_since_update", 0)
    if days_since_update > 7:
        score += 20
    elif days_since_update > 3:
        score += 10
    
    # P1 override — always at the top regardless of other factors
    if ticket.get("priority") == "urgent":
        score += 200
    
    return score
```

---

## 10. API Endpoints to Build

All new endpoints are added to the existing FastAPI app alongside the existing webhooks. They are prefixed `/ops/` to namespace them away from the existing webhook routes.

### Authentication

All `/ops/` endpoints require a simple bearer token passed in the `Authorization` header. Token is set as an environment variable `OPS_TOKEN`. This is an internal tool — no OAuth needed.

```python
def verify_ops_token(authorization: str = Header(...)):
    if authorization != f"Bearer {os.getenv('OPS_TOKEN')}":
        raise HTTPException(status_code=401)
```

### Endpoint List

---

#### `GET /ops/tickets`

Returns the full prioritized ticket queue for the dashboard.

**Query params:**
- `filter` — `all` | `p1` | `bugs` | `needs_review` | `waiting` | `final_review` | `resolved` | `unassigned` (default: `all`)
- `limit` — integer, default 50

**Logic:**
1. Query Zoho Desk for tickets with status: New, Processing, On Hold, Final Review (resolved=false)
2. Cross-reference PostgreSQL `ticket_threads` table for ClickUp task IDs, CRM data, auto scores
3. Fetch ClickUp tasks in `needs_review` and `waiting_on_client` status (these may not be in Zoho's active statuses)
4. Merge and deduplicate by Zoho ticket ID
5. For each ticket: fetch latest ClickUp comment (engineer note) if status is needs_review or waiting_on_client
6. Run `compute_priority_score()` on each
7. Sort descending by score
8. Return paginated list

**Response shape (per ticket):**
```json
{
  "zoho_ticket_id": "569440000037491211",
  "zoho_ticket_number": "6957",
  "zoho_status": "on_hold",
  "zoho_link": "https://desk.zoho.com/...",
  "clickup_task_id": "868j7adc0",
  "clickup_status": "waiting_on_client",
  "clickup_link": "https://app.clickup.com/t/868j7adc0",
  "subject": "Rising Starr Horse Rescue — Bonnie forwarded volunteer login issue",
  "summary": "Bonnie forwarded a truncated volunteer login complaint. Attachment present but volunteer name/email unknown.",
  "org_name": "Rising Starr Horse Rescue",
  "tier": "Enterprise",
  "arr_dollars": 1440,
  "priority": "normal",
  "p_level": "P3",
  "module": "Access / Authentication",
  "platform": "Web",
  "assignee_name": "Sanjay Jangid",
  "assignee_clickup_id": 4434086,
  "assignee_zoho_id": "569440000023159001",
  "contact_email": "bonnie@risingstarrhorserescue.org",
  "missing_info": "Need: affected volunteer email + name",
  "engineer_comment": "Sanjay: Forwarded email body truncated — attachment may have full content",
  "days_since_update": 3,
  "priority_score": 87,
  "language": "en",
  "has_attachment": true,
  "resolved": false
}
```

---

#### `GET /ops/ticket/{zoho_ticket_id}/thread`

Returns the full conversation thread for a single ticket (for the thread panel).

**Logic:**
1. Call Zoho Desk `getTicketConversations` API
2. Format each thread entry with: direction (inbound/outbound/internal), author name, timestamp, content (HTML stripped to plain text), attachment URLs
3. Fetch ClickUp task comments separately
4. Return both

**Response shape:**
```json
{
  "zoho_ticket_id": "...",
  "threads": [
    {
      "id": "...",
      "direction": "inbound",
      "author": "Bonnie Troy",
      "author_email": "bonnie@risingstarrhorserescue.org",
      "timestamp": "2026-04-10T09:44:43Z",
      "content": "Good Morning, this person is having problems signing into VOME...",
      "is_internal": false,
      "attachments": [{"name": "screenshot.png", "url": "..."}]
    }
  ],
  "clickup_comments": [
    {
      "author": "Sanjay Jangid",
      "timestamp": "2026-04-10T11:00:00Z",
      "text": "Forwarded email body was truncated in the webhook..."
    }
  ]
}
```

---

#### `POST /ops/ticket/{zoho_ticket_id}/draft`

Generates a Claude draft reply for a ticket.

**Request body:**
```json
{
  "draft_type": "request_info | acknowledge | resolution | close | admin_action",
  "redraft_instruction": "optional — plain English instruction for redraft",
  "engineer_note": "optional override — if Sam wants to reference a specific note"
}
```

**Logic:**
1. Fetch full Zoho thread (via `getTicketConversations`)
2. Fetch ClickUp task description + all comments
3. Fetch ticket from PostgreSQL thread map (tier, ARR, CRM data)
4. Load `system_prompt.md` and `response_templates.md` (existing pattern)
5. Construct Claude prompt with:
   - All thread content
   - CRM context
   - Engineer comments
   - Draft type instruction
   - Redraft instruction if provided
   - Rules: match client language (French if detected), never em-dashes, sign as "Sam | Vome support", be warm and specific
6. Return generated draft

**Response:**
```json
{
  "draft": "Hi Bonnie,\n\nThanks for flagging this...",
  "suggested_zoho_status": "on_hold",
  "suggested_clickup_action": "close_temporarily",
  "draft_type": "request_info",
  "language_detected": "en"
}
```

---

#### `POST /ops/ticket/{zoho_ticket_id}/send`

Sends a reply and syncs status across Zoho + ClickUp.

**Request body:**
```json
{
  "content": "the reply text to send",
  "zoho_status_after": "on_hold | processing | final_review | closed",
  "clickup_action": "leave | close_temporarily | in_progress | waiting_on_client | done",
  "assignee_clickup_id": 4434086,
  "assignee_zoho_id": "569440000023159001"
}
```

**Logic:**
1. Call Zoho `draftsReply` API to save draft, then send OR use `sendReply` API directly
2. Call Zoho `updateTicket` API to set status and owner
3. Call ClickUp REST API to update task status
4. If new assignee: update ClickUp assignee + Zoho ticket owner
5. Post internal note to Zoho confirming what was done: "Sent [draft_type] reply via Command Center. Zoho → [status]. ClickUp → [status]."
6. Update PostgreSQL `ticket_threads` row: status, assignee, last action, last action timestamp
7. Optionally post to Slack `#vome-agent-log` for audit trail

**Response:**
```json
{
  "success": true,
  "zoho_thread_id": "...",
  "zoho_new_status": "on_hold",
  "clickup_new_status": "waiting_on_client",
  "message": "Reply sent. Zoho → On Hold. ClickUp → Waiting on Client."
}
```

---

#### `POST /ops/ticket/{zoho_ticket_id}/assign`

Assigns a ticket to an engineer without sending a reply.

**Request body:**
```json
{
  "engineer": "sanjay | onlyg",
  "send_ack": true
}
```

**Logic:**
1. Resolve engineer to IDs from the team config
2. Update Zoho ticket owner
3. Update ClickUp task assignee
4. If `send_ack: true`: auto-generate and send a brief "we're looking into it" reply
5. Update PostgreSQL

---

#### `POST /ops/ticket/{zoho_ticket_id}/close`

Closes a ticket completely.

**Request body:**
```json
{
  "send_closure_note": true,
  "resolution": "completed | declined | duplicate",
  "closure_message": "optional custom message — if null, auto-generate"
}
```

**Logic:**
1. If `send_closure_note: true` and no message provided: Claude generates a brief warm closure note
2. Send the note via Zoho
3. Zoho ticket status → `Closed`
4. ClickUp task status → `Done`, Resolution field → mapped option ID
5. Update PostgreSQL

---

#### `POST /ops/ticket/{zoho_ticket_id}/park`

Parks a ticket.

**Request body:**
```json
{
  "note": "optional internal note",
  "wake_date": "2026-05-01"
}
```

**Logic:**
1. Zoho status → `On Hold`
2. ClickUp status → `Sleeping`, Wake Date field set if provided
3. Internal note added to Zoho: "Parked by Sam via Command Center. Wake: [date if set]"
4. PostgreSQL: flag parked, store wake date

---

## 11. File Structure

```
vome-intelligence/               ← existing FastAPI repo
├── main.py                      ← existing — add /ops/ router import
├── agent.py                     ← existing — do not modify
├── clickup_tasks.py             ← existing — do not modify
├── slack.py                     ← existing — do not modify
├── slack_ticket_brief.py        ← existing — do not modify
├── slack_reply_handler.py       ← existing — do not modify
├── slack_digest.py              ← existing — do not modify
├── field_feedback.py            ← existing — do not modify
├── on_prod_handler.py           ← existing — do not modify
├── database.py                  ← existing — do not modify schema
├── system_prompt.md             ← existing — do not modify
├── response_templates.md        ← existing — do not modify
│
├── ops/                         ← NEW DIRECTORY
│   ├── __init__.py
│   ├── router.py                ← FastAPI router, mounts all /ops/ endpoints
│   ├── auth.py                  ← OPS_TOKEN bearer auth dependency
│   ├── tickets.py               ← GET /ops/tickets logic
│   ├── thread.py                ← GET /ops/ticket/{id}/thread logic
│   ├── draft.py                 ← POST /ops/ticket/{id}/draft — Claude draft gen
│   ├── send.py                  ← POST /ops/ticket/{id}/send — send + sync
│   ├── assign.py                ← POST /ops/ticket/{id}/assign
│   ├── close.py                 ← POST /ops/ticket/{id}/close
│   ├── park.py                  ← POST /ops/ticket/{id}/park
│   ├── scoring.py               ← compute_priority_score() function
│   └── zoho_sync.py             ← shared helpers: update Zoho status, owner
│
├── frontend/                    ← NEW DIRECTORY — React SPA
│   ├── index.html
│   ├── src/
│   │   ├── main.jsx
│   │   ├── App.jsx
│   │   ├── components/
│   │   │   ├── Dashboard.jsx        ← main ticket queue view
│   │   │   ├── TicketCard.jsx       ← individual ticket card
│   │   │   ├── ThreadPanel.jsx      ← slide-in thread view
│   │   │   ├── DraftPanel.jsx       ← draft review + send panel
│   │   │   ├── FilterBar.jsx        ← filter tabs + stats
│   │   │   └── ActionButtons.jsx    ← context-aware action buttons
│   │   ├── hooks/
│   │   │   ├── useTickets.js        ← fetches /ops/tickets, polling
│   │   │   ├── useThread.js         ← fetches thread for open ticket
│   │   │   └── useDraft.js          ← draft generation + redraft state
│   │   └── api.js                   ← all fetch calls to /ops/ endpoints
│   └── vite.config.js
│
└── Procfile                     ← existing — add static file serving for /frontend/dist
```

### Adding the Router to main.py

```python
# Add to existing main.py imports
from ops.router import ops_router

# Add after existing app setup
app.include_router(ops_router, prefix="/ops")

# Serve React frontend (add after router)
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="frontend/dist", html=True), name="frontend")
```

---

## 12. Environment & Config Reference

All values below are already in `.env` on Railway. Do not hardcode any of them.

### Zoho
```
ZOHO_ORG_ID=736165782
ZOHO_FROM_EMAIL=support@vomevolunteer.zohodesk.com
ZOHO_SUPPORT_AGENT_ID=569440000000139001   ← Support Agent (bot)
```

### Team Agent IDs (Zoho)
```
SAM_ZOHO_AGENT_ID=569440000000139001
ONLYG_ZOHO_AGENT_ID=569440000023160001
SANJAY_ZOHO_AGENT_ID=569440000023159001
RON_ZOHO_AGENT_ID=569440000000192003
```

### Team IDs (ClickUp)
```
SAM_CLICKUP_ID=3691763
ONLYG_CLICKUP_ID=49257687
SANJAY_CLICKUP_ID=4434086
RON_CLICKUP_ID=4434980
```

### ClickUp Lists
```
CLICKUP_PRIORITY_QUEUE_ID=901113386257
CLICKUP_RAW_INTAKE_ID=901113386484
CLICKUP_ACCEPTED_BACKLOG_ID=901113389889
CLICKUP_DONE_ID=901113386518
```

### ClickUp Custom Field IDs (for status sync writes)
```
CLICKUP_FIELD_TYPE=e0e439f5-397d-432d-addd-e90fbf50cd30
CLICKUP_FIELD_PLATFORM=5f1ff65b-18fc-49db-89aa-2c1f355ec1e7
CLICKUP_FIELD_MODULE=3f111d48-e92a-4d5e-92d9-e193c80b20cc
CLICKUP_FIELD_HIGHEST_TIER=be348a1d-6a63-4da8-83bb-9038b24264ff
CLICKUP_FIELD_REQUESTING_CLIENTS=e2de3bd0-6ad9-4b31-bb09-104f6bef383d
CLICKUP_FIELD_COMBINED_ARR=29c41859-f24b-4143-9af4-a34202205641
CLICKUP_FIELD_AUTO_SCORE=fd77f978-eca8-499e-bc3c-dc1bf4b8181e
CLICKUP_FIELD_ZOHO_TICKET_LINK=4776215b-c725-4d79-8f20-c16f0f0145ac
CLICKUP_FIELD_RESOLUTION=63ef3458-cfa6-4a0b-ae44-18858cd555f0
CLICKUP_FIELD_WAKE_DATE=701fdb31-1341-426a-be88-23d2e10edfec
```

### ClickUp Resolution Option IDs
```
CLICKUP_RESOLUTION_COMPLETED=c51c9782-0bca-4c40-a4fe-a272427dc347
CLICKUP_RESOLUTION_DECLINED=8600a963-ab55-430f-a86f-2b1d0f911156
CLICKUP_RESOLUTION_SLEEPING=560c14b1-70bb-4387-8d21-941d0543873c
CLICKUP_RESOLUTION_DUPLICATE=4ad0a7eb-5fb6-4ef1-af0c-0188c7d24a3e
```

### ClickUp Status Names (exact strings used in API calls)
```
queued
in progress
needs review
waiting on client
on dev
on prod
sleeping
done
```

### Slack Channels
```
SLACK_CHANNEL_ENGINEERING=C0ALJPCAE93      ← #vome-support-engineering
SLACK_CHANNEL_FIELD_FEEDBACK=C0AL6NTJP8F  ← #vome-field-feedback
SLACK_CHANNEL_FEATURE_REQUESTS=C0ALL4VPWK0 ← #vome-feature-requests
SLACK_CHANNEL_AGENT_LOG=C0AMGELBDB2       ← #vome-agent-log
SLACK_TICKETS_CHANNEL=C0AMTJA2UTE         ← #vome-tickets
```

### New Env Vars to Add
```
OPS_TOKEN=<generate a secure random token for dashboard auth>
```

### PostgreSQL — ticket_threads table (existing columns, do not remove)
```
ticket_id         VARCHAR — Zoho ticket ID
ticket_number     VARCHAR — Zoho ticket number (#6957)
subject           VARCHAR
channel_id        VARCHAR — Slack channel ID
thread_ts         VARCHAR — Slack thread timestamp
status            VARCHAR — current status
clickup_task_id   VARCHAR
classification    VARCHAR
crm_data          JSONB
pending_send      BOOLEAN
pending_draft     TEXT
close_after_send  BOOLEAN
```

**Columns to ADD (migrations only, no removals):**
```sql
ALTER TABLE ticket_threads ADD COLUMN IF NOT EXISTS zoho_assignee_id VARCHAR;
ALTER TABLE ticket_threads ADD COLUMN IF NOT EXISTS clickup_assignee_id INTEGER;
ALTER TABLE ticket_threads ADD COLUMN IF NOT EXISTS priority_score INTEGER;
ALTER TABLE ticket_threads ADD COLUMN IF NOT EXISTS missing_info TEXT;
ALTER TABLE ticket_threads ADD COLUMN IF NOT EXISTS engineer_note TEXT;
ALTER TABLE ticket_threads ADD COLUMN IF NOT EXISTS pending_info BOOLEAN DEFAULT FALSE;
ALTER TABLE ticket_threads ADD COLUMN IF NOT EXISTS parked BOOLEAN DEFAULT FALSE;
ALTER TABLE ticket_threads ADD COLUMN IF NOT EXISTS wake_date DATE;
ALTER TABLE ticket_threads ADD COLUMN IF NOT EXISTS last_action VARCHAR;
ALTER TABLE ticket_threads ADD COLUMN IF NOT EXISTS last_action_at TIMESTAMP;
```

---

## 13. What Not to Touch

The following files and systems must not be modified. They are working correctly and any change risks breaking the live intake pipeline.

**Files — read only:**
- `main.py` — only additive changes (import ops router, mount static files)
- `agent.py` — do not modify any existing functions
- `clickup_tasks.py` — do not modify
- `slack.py`, `slack_ticket_brief.py`, `slack_reply_handler.py`, `slack_digest.py` — do not modify
- `field_feedback.py` — do not modify
- `on_prod_handler.py` — do not modify
- `system_prompt.md` — do not modify
- `response_templates.md` — do not modify

**Database — additive only:**
- Never drop or rename columns in `ticket_threads` or `processed_events`
- Always use `ADD COLUMN IF NOT EXISTS` for new columns

**Webhook routes — do not change:**
- `POST /webhook/zoho-ticket`
- `POST /webhook/zoho-update`
- `POST /webhook/clickup-status`
- `POST /webhook/slack-events`

**Zoho customer portal:**
- `support.vomevolunteer.com` stays live — clients use it to view ticket status
- The "New Ticket" button in the portal should be suppressed (configure via Zoho Help Center settings — not a code change)

---

## Build Order for Claude Code

### Phase 1 — Backend (ops/ directory)

```
1. ops/auth.py              — OPS_TOKEN bearer auth
2. ops/scoring.py           — compute_priority_score() function
3. ops/zoho_sync.py         — shared Zoho update helpers
4. ops/tickets.py           — GET /ops/tickets (the main query)
5. ops/thread.py            — GET /ops/ticket/{id}/thread
6. ops/draft.py             — POST /ops/ticket/{id}/draft (Claude call)
7. ops/send.py              — POST /ops/ticket/{id}/send (the critical one)
8. ops/assign.py            — POST /ops/ticket/{id}/assign
9. ops/close.py             — POST /ops/ticket/{id}/close
10. ops/park.py             — POST /ops/ticket/{id}/park
11. ops/router.py           — mount all above endpoints
12. main.py                 — add router import + static file serving
13. database.py             — run ALTER TABLE migrations
```

### Phase 2 — Frontend (frontend/ directory)

```
1. Set up Vite + React project in frontend/
2. api.js                   — all fetch calls to /ops/ endpoints, auth header
3. FilterBar.jsx            — stats row + filter tabs
4. TicketCard.jsx           — full card with all fields and action buttons
5. Dashboard.jsx            — main view, polling for ticket updates
6. ThreadPanel.jsx          — slide-in thread reader
7. DraftPanel.jsx           — draft review, edit, redraft, send
8. ActionButtons.jsx        — context-aware buttons based on ticket state
9. App.jsx + main.jsx       — wire everything together
```

### Phase 3 — Integration + Polish

```
1. End-to-end test: open ticket → view thread → draft reply → send → verify Zoho + ClickUp updated
2. Test all action flows (A through G from Section 6)
3. Polling: dashboard auto-refreshes every 60 seconds
4. Add loading states and error handling to all async operations
5. Deploy: build frontend (npm run build → /frontend/dist), Railway redeploy
```

---

*This document is the authoritative spec. When in doubt, refer to Section 7 (status sync) and Section 6 (action flows) — they define the system's behavior. All other implementation details are flexible.*