# Vome Support Agent — System Context

*Last updated: 2026-03-20*

## What this system is

An AI-powered support operations layer for Vome, a volunteer management CRM serving nonprofits, universities, and corporate organizations. The agent processes incoming Zoho Desk tickets, Slack field feedback, and ClickUp status changes automatically before any human sees them.

The agent never sends anything directly to clients. All client-facing responses are posted as internal notes or drafts in Zoho Desk for human review and approval.

---

## Tech stack

- **Python + FastAPI** — webhook server ([main.py](main.py))
- **Anthropic API** — claude-sonnet-4-20250514 for classification and drafting
- **Zoho Desk MCP** — read tickets, post internal notes, save draft replies
- **Zoho CRM MCP** — contact/account lookup, deal/ARR enrichment
- **ClickUp REST API** — task creation and management (direct HTTP, not MCP)
- **Slack SDK** — team notifications, ticket briefs, field feedback, daily digest
- **PostgreSQL** — thread map persistence, event deduplication (via SQLAlchemy)
- **APScheduler** — daily digest cron job (18:00 America/Montreal)
- **Railway** — hosting (Procfile: `uvicorn main:app`)

---

## Current working components

### Zoho Desk ticket processing (Phase 1 — complete)
- **Webhook:** `POST /webhook/zoho-ticket` — new ticket intake
- **Webhook:** `POST /webhook/zoho-update` — client reply reprocessing
- Fetches full ticket details + conversation threads from Zoho Desk
- Detects attachments across ticket body and thread messages
- Detects French language content and drafts in matching language
- CRM enrichment: contact lookup, account name, tier, ARR from Closed Won deals
- CRM fallback: if CRM auth fails, falls back to Zoho Desk contact/account search
- Claude processes ticket with full system prompt + response templates
- Posts structured internal note to Zoho (draft response + agent analysis)
- On client reply: detects client vs team replies, reprocesses, saves draft reply

### ClickUp task creation (Phase 2 — complete)
- Parses Claude's structured analysis (classification, module, platform, priority, auto score)
- Routes to correct list: Priority Queue (bugs), Raw Intake (features), Accepted Backlog (non-urgent UX)
- Populates all custom fields: Type, Platform, Module, Source, Highest Tier, Requesting Clients, Combined ARR, Auto Score, Zoho Ticket Link
- Auto-assigns to engineer based on classification (frontend → Sanjay, backend → OnlyG)
- Builds structured task title: `[Account] — [Subject] — [P1/P2/P3]`

### Slack integration (Phase 3 — complete)
- **#vome-tickets** — ticket briefs posted for every new Zoho ticket, with classification summary, CRM enrichment, links, and suggested owner
- **#vome-tickets reply handling** — Sam replies in plain English to take action (send drafts, create ClickUp tasks, park tickets, add notes, close tickets, request new drafts)
- **#vome-field-feedback** — Conversational Claude agent. Any team member (Ron, Sam, etc.) posts in natural language. Agent uses tool_use to create, update, or delete ClickUp tasks. Fetches full thread history for context on replies. Always responds with what it understood, what it did, and a link to the task.
- **Daily digest** — posted at 18:00 Montreal time with handled/parked/open/on-prod-pending counts and engineer task loads

### ClickUp status webhook (Phase 5 — complete)
- **Webhook:** `POST /webhook/clickup-status` — listens for taskStatusUpdated events
- When task moves to ON PROD: fetches Zoho ticket, generates resolution draft with Claude, posts to Slack thread with confirm/send/cancel options

### ON PROD flow (complete)
- Finds existing Slack thread for the ticket or creates a new one
- Generates context-aware resolution draft based on full ticket history
- Stores pending draft in database; team confirms via Slack reply before sending

### Database (complete)
- PostgreSQL with two tables: `ticket_threads` (thread map) and `processed_events` (Slack event dedup)
- Thread map stores: ticket ID, number, subject, channel, status, ClickUp task ID, classification, CRM data, pending send/draft, close-after-send flag
- Auto-migrates schema on startup

### ClickUp migration (Phase 6 — complete)
- 92 tasks migrated from VOMEDev to VOME Operations space (64 main triage + 28 recovered from testing containers)

### Response templates
- 20 proven templates in [response_templates.md](response_templates.md) loaded into system prompt at runtime
- Cover common scenarios: registration, login, auth bypass, password reset, email mismatch, deletion, payment, hours not showing, invite not received, etc.
- Claude matches templates to ticket context and personalizes with client details

---

## What is not yet built

- **RAG layer** (Phase 4) — ChromaDB is in requirements.txt but not implemented. Historical ticket search for recurrence intelligence and draft reference is not active.
- **Sleeping item wake monitoring** — system prompt describes wake conditions but no automated scheduler checks Wake Date fields or recurrence triggers yet
- **Weekly feature request digest** — daily digest exists but weekly feature summary for Sam is not implemented
- **Resolution custom field option IDs** — Completed/Declined/Sleeping/Duplicate option IDs still need to be fetched from ClickUp

---

## The team

| Name | Role | ClickUp ID | Zoho Agent ID | Slack |
|------|------|-----------|--------------|-------|
| **Sam** | CEO, Full-Stack Engineer, Primary Reviewer | 3691763 | 569440000000139001 | U01B9FQNSBU |
| **OnlyG** | Lead Backend Engineer, Full-Stack | 49257687 | 569440000023160001 | — |
| **Sanjay** | Frontend Engineer (Web + Mobile) | 4434086 | 569440000023159001 | — |
| **Ron** | Sales, Field Feedback via Slack | 4434980 | 569440000000192003 | — |

---

## Zoho configuration

- **Org ID:** 736165782
- **From address:** support@vomevolunteer.zohodesk.com
- **MCP endpoint:** Zoho MCP server (URL + token in .env)
- **CRM:** Active via MCP — searches Contacts by email, fetches related Deals for ARR. Falls back to Desk contact search on CRM auth failure.
- **Team emails** (excluded from client reply detection): admin@vomevolunteer.com, sam@vomevolunteer.com, s.fagen@vomevolunteer.com, r.segev@vomevolunteer.com

---

## Slack channels

| Channel | Env var | Channel ID | Purpose |
|---------|---------|-----------|---------|
| #vome-support-engineering | SLACK_CHANNEL_ENGINEERING | C0ALJPCAE93 | OnlyG + Sanjay — P1 escalations, timing requests |
| #vome-field-feedback | SLACK_CHANNEL_FIELD_FEEDBACK | C0AL6NTJP8F | Ron + Sam — field feedback intake |
| #vome-feature-requests | SLACK_CHANNEL_FEATURE_REQUESTS | C0ALL4VPWK0 | Sam — scored feature request pings |
| #vome-agent-log | SLACK_CHANNEL_AGENT_LOG | C0AMGELBDB2 | Sam — audit trail of all agent actions |
| #vome-tickets | SLACK_TICKETS_CHANNEL | C0AMTJA2UTE | Primary ticket brief channel, reply handling |

---

## ClickUp configuration

- **Team ID:** 3604276
- **Space:** VOME Operations (90114113004)

### Folder / List structure

```
FOLDER: Master Queue
  LIST: Priority Queue         901113386257
  LIST: Done                   901113386518

FOLDER: Feature Requests
  LIST: Raw Intake             901113386484
  LIST: Accepted Backlog       901113389889
  LIST: Sleeping               901113389897
  LIST: Declined               901113389900
```

### Custom field IDs

**Type:** `e0e439f5-397d-432d-addd-e90fbf50cd30`
| Option | ID |
|--------|----|
| Bug | `d9c82e67-c46b-48d1-95f7-9c1f5b2fc2df` |
| Feature | `41a1ea4e-eec9-418d-a684-3c17cdd8dd67` |
| UX | `da749879-7b3a-4fd5-a1cb-c85fcb719569` |
| Improvement | `9864f852-39cc-481c-aafc-c2f2ebdba30b` |
| Investigation | `f9bd67bb-5b85-49fb-bd4a-21295f01cf5a` |

**Platform:** `5f1ff65b-18fc-49db-89aa-2c1f355ec1e7`
| Option | ID |
|--------|----|
| Web | `946c8214-6a65-4e63-a437-d98415dc1439` |
| Mobile | `070470c3-c248-4d64-8ceb-8c95df82506b` |
| Both | `2d69c526-e58f-4486-bc6a-168cd812f0bf` |

**Module:** `3f111d48-e92a-4d5e-92d9-e193c80b20cc`
| Option | ID |
|--------|----|
| Volunteer Homepage | `1fd64528-970a-48da-8881-9a0fb4ac96f4` |
| Reserve Schedule | `197109a7-2974-4210-94f1-a1e97990830f` |
| Opportunities | `af0b8949-5281-4655-8326-c77dcfb2ecf7` |
| Sequences | `04d7e808-d94f-48bb-b5eb-f567e6cf41ca` |
| Forms | `aa6f6c17-7260-44fd-a862-99daaf7d77c0` |
| Admin Dashboard | `b13da71b-46ab-45c6-82ab-fa37a18fc0b3` |
| Admin Scheduling | `f36d0e31-2f2b-4044-bb5c-fb4fef06cb74` |
| Admin Settings | `d9d9051c-d733-4ac0-8607-1a75153b021b` |
| Admin Permissions | `bf68973f-f858-4083-9a60-7dc014b6e1f3` |
| Sites | `5f5cc57b-6259-4bab-88dd-64b3a34036f1` |
| Groups | `36178405-828f-4471-80a4-cedb2eb0be59` |
| Categories | `f4f5021b-d528-41c1-b832-87a67e5ba0ae` |
| Hour Tracking | `53f02923-e8a5-4c30-8a42-c2bddaa75778` |
| Kiosk | `d92abcaa-e2a3-46f6-bb99-2025aa3984d3` |
| Email Communications | `c2c3a5ae-e8db-4da5-9ab3-736c3a76b66f` |
| Chat | `a4d5a4bd-049b-4dfa-b6c2-2843661faad4` |
| Reports | `95a6e4bf-eaa0-4cd4-87ed-9298a37da0c6` |
| KPI Dashboards | `ef0db184-315f-4420-bccb-bb7dee73e7a1` |
| Integrations | `938a5549-70dd-40f2-9db2-4176d10b4221` |
| Access / Authentication | `fd457e45-e25c-4910-871f-fe67bf5391d3` |
| Other | `cbe38d18-9d9f-4e49-abf5-5101f09349ff` |

**Source:** `857e1262-cb5c-4c22-b8da-770a8fcfa82e`
| Option | ID |
|--------|----|
| Migration | `21681d2f-d0b7-4eb5-9fce-5f41600ffe6f` |
| Zoho Ticket | `9b678f29-3b49-4842-9305-ada436cfc0b3` |
| Field Feedback | `ef5fcb3c-c27d-443c-bef2-32be1521baf1` |
| Internal | `ea82838e-f5ee-4cc6-b5d3-da4ef9052343` |
| Roadmap | `0a60ef2b-bb3c-4023-a21a-ad1375c84ef8` |

**Tags:** `291fd0e7-42e3-4376-bacd-2b54a6c1d48c`
| Option | ID |
|--------|----|
| Notification | `2eaf6034-ae01-42da-ace7-5832cfc3e44e` |
| Performance | `427c7f85-ee11-4cd0-99d2-10dcb67eb4c0` |
| Security | `a580d60c-889a-4e5e-a199-d89dc5bb2001` |
| Data | `0bf3c2d8-2ac2-44c5-b214-0acfbfd94fd3` |
| Regression | `743fb482-4c9c-4e64-a33f-2d8437c7b9f7` |

**Other fields:**
| Field | ID |
|-------|----|
| Highest Tier | `be348a1d-6a63-4da8-83bb-9038b24264ff` |
| Requesting Clients | `e2de3bd0-6ad9-4b31-bb09-104f6bef383d` |
| Combined ARR | `29c41859-f24b-4143-9af4-a34202205641` |
| Auto Score | `fd77f978-eca8-499e-bc3c-dc1bf4b8181e` |
| Sprint Batch | `97b38eb3-4416-40c9-9e27-fd26b0174849` |
| Wake Date | `701fdb31-1341-426a-be88-23d2e10edfec` |
| Release Note | `49f6daf4-1eba-4ec9-9102-f5140a9f81c5` |
| Notified Client | `479a95ce-5129-42d7-8fc6-48fcccc2ce7e` |
| Design Spec | `723d8e39-a6b1-40b3-9154-0d64f843313d` |
| Zoho Ticket Link | `4776215b-c725-4d79-8f20-c16f0f0145ac` |
| Resolution | `63ef3458-cfa6-4a0b-ae44-18858cd555f0` |

---

## Workflow summary

### New Zoho ticket arrives
1. Zoho fires webhook to `/webhook/zoho-ticket`
2. Agent fetches full ticket + conversations from Zoho Desk
3. Detects attachments and language
4. Looks up contact in Zoho CRM (email → account → tier → ARR)
5. Claude classifies: type, module, platform, priority, auto score, timing, suggested owner
6. Posts internal note to Zoho (draft response + agent analysis)
7. Posts ticket brief to #vome-tickets in Slack
8. Stores thread mapping in PostgreSQL

### Client replies to existing ticket
1. Zoho fires webhook to `/webhook/zoho-update`
2. Agent checks if latest message is a client reply (not team, not bot)
3. Reprocesses with full thread context
4. Saves draft reply via Zoho draftsReply API
5. Posts update as internal note for audit trail

### Sam replies in #vome-tickets thread
1. Slack events webhook routes to `slack_reply_handler`
2. Supports commands: send draft, create ClickUp task, park, close, add note, request new draft, custom send
3. All actions update database thread status

### Team posts in #vome-field-feedback
1. Any message in #vome-field-feedback triggers `field_feedback.py`
2. Agent loads CONTEXT.md + full thread history and sends to Claude with tool_use
3. Claude decides what to do: create task, update task, delete task, ask follow-up, or just acknowledge
4. Agent executes tool calls (ClickUp create/update/delete/get) and sends results back to Claude
5. Claude produces a final response: what it understood, what it did, link to ClickUp task
6. Agent posts response as thread reply
7. On subsequent thread replies: full thread history is fetched so Claude has conversation context
8. Works for Ron, Sam, or any team member — not restricted to one person

### ClickUp task hits ON PROD
1. ClickUp fires webhook to `/webhook/clickup-status`
2. Agent reads Zoho Ticket Link from task, fetches ticket + conversations
3. Claude generates resolution draft
4. Posts to Slack thread with confirm/send/cancel options
5. On confirm: sends resolution to client via Zoho

### Daily digest (18:00 Montreal)
1. APScheduler triggers `send_daily_digest`
2. Queries all threads from database
3. Posts summary: handled, parked, open, on-prod-pending, engineer task counts

---

## Key behaviour rules

- Agent NEVER sends directly to clients
- All client responses post as internal notes or drafts in Zoho for human review
- Only trigger for resolution draft: ClickUp task moves to ON PROD status
- system_prompt.md is loaded at runtime as the agent's instructions — never hardcode its contents
- response_templates.md is appended to system prompt at runtime
- Sam is always the support identity ("Sam | Vome support") regardless of who reviews
- Never use em-dashes in any client-facing response
- Draft in the same language the client used (French detection built in)
- Tickets with attachments are never classified as Unclear

---

## File reference

| File | Purpose |
|------|---------|
| [main.py](main.py) | FastAPI app, webhook endpoints, scheduler |
| [agent.py](agent.py) | Core ticket processing, Zoho MCP calls, CRM enrichment, Claude calls |
| [clickup_tasks.py](clickup_tasks.py) | ClickUp task creation via REST API |
| [slack.py](slack.py) | Slack posting helpers for each channel |
| [slack_ticket_brief.py](slack_ticket_brief.py) | Ticket brief formatter + poster for #vome-tickets |
| [slack_reply_handler.py](slack_reply_handler.py) | Sam's reply handling in #vome-tickets threads |
| [slack_digest.py](slack_digest.py) | Daily end-of-day digest |
| [field_feedback.py](field_feedback.py) | Ron's field feedback processing from #vome-field-feedback |
| [on_prod_handler.py](on_prod_handler.py) | ON PROD flow: resolution draft + Slack notification |
| [database.py](database.py) | PostgreSQL thread map + event deduplication |
| [system_prompt.md](system_prompt.md) | Agent instructions loaded at runtime (v2.0) |
| [response_templates.md](response_templates.md) | 20 proven response templates for common scenarios |
| [Procfile](Procfile) | Railway deployment config |
