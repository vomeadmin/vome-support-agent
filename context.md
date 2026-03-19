# Vome Support Agent — Build Context

## What this system is
An AI-powered support operations layer for Vome,
a volunteer management CRM. The agent processes 
incoming Zoho Desk tickets automatically before 
any human sees them.

## Tech stack
- Python + FastAPI (webhook server)
- Anthropic API (claude-sonnet-4-20250514)
- Zoho Desk MCP (read tickets, write internal notes)
- ClickUp MCP (create and manage tasks)
- Slack SDK (team notifications and field feedback)
- ChromaDB (RAG layer for historical tickets)
- Railway (hosting)

## The team
- Sam — CEO, full-stack, primary reviewer
- OnlyG — Lead backend engineer
- Sanjay — Frontend engineer
- Ron — Sales, submits field feedback via Slack

## Slack channels
- #vome-support-engineering → OnlyG + Sanjay
- #vome-field-feedback → Sam + Ron
- #vome-feature-requests → Sam
- #vome-agent-log → Sam (debug/monitoring)

## CLICKUP CUSTOM FIELD IDs
Use these IDs when creating or updating 
tasks via ClickUp MCP API calls.

SPACE: VOME Operations (90114113004)

LISTS:
Priority Queue: 901113386257
Raw Intake: 901113386484
Accepted Backlog: 901113389889
Sleeping: 901113389897
Declined: 901113389900
Done: 901113386518

CUSTOM FIELDS:
Type: e0e439f5-397d-432d-addd-e90fbf50cd30
  Bug: d9c82e67-c46b-48d1-95f7-9c1f5b2fc2df
  Feature: 41a1ea4e-eec9-418d-a684-3c17cdd8dd67
  UX: da749879-7b3a-4fd5-a1cb-c85fcb719569
  Improvement: 9864f852-39cc-481c-aafc-c2f2ebdba30b
  Investigation: f9bd67bb-5b85-49fb-bd4a-21295f01cf5a

Platform: 5f1ff65b-18fc-49db-89aa-2c1f355ec1e7
  Web: 946c8214-6a65-4e63-a437-d98415dc1439
  Mobile: 070470c3-c248-4d64-8ceb-8c95df82506b
  Both: 2d69c526-e58f-4486-bc6a-168cd812f0bf

Module: 3f111d48-e92a-4d5e-92d9-e193c80b20cc
  Volunteer Homepage: 1fd64528-970a-48da-8881-9a0fb4ac96f4
  Reserve Schedule: 197109a7-2974-4210-94f1-a1e97990830f
  Opportunities: af0b8949-5281-4655-8326-c77dcfb2ecf7
  Sequences: 04d7e808-d94f-48bb-b5eb-f567e6cf41ca
  Forms: aa6f6c17-7260-44fd-a862-99daaf7d77c0
  Admin Dashboard: b13da71b-46ab-45c6-82ab-fa37a18fc0b3
  Admin Scheduling: f36d0e31-2f2b-4044-bb5c-fb4fef06cb74
  Admin Settings: d9d9051c-d733-4ac0-8607-1a75153b021b
  Admin Permissions: bf68973f-f858-4083-9a60-7dc014b6e1f3
  Sites: 5f5cc57b-6259-4bab-88dd-64b3a34036f1
  Groups: 36178405-828f-4471-80a4-cedb2eb0be59
  Categories: f4f5021b-d528-41c1-b832-87a67e5ba0ae
  Hour Tracking: 53f02923-e8a5-4c30-8a42-c2bddaa75778
  Kiosk: d92abcaa-e2a3-46f6-bb99-2025aa3984d3
  Email Communications: c2c3a5ae-e8db-4da5-9ab3-736c3a76b66f
  Chat: a4d5a4bd-049b-4dfa-b6c2-2843661faad4
  Reports: 95a6e4bf-eaa0-4cd4-87ed-9298a37da0c6
  KPI Dashboards: ef0db184-315f-4420-bccb-bb7dee73e7a1
  Integrations: 938a5549-70dd-40f2-9db2-4176d10b4221
  Access / Authentication: fd457e45-e25c-4910-871f-fe67bf5391d3
  Other: cbe38d18-9d9f-4e49-abf5-5101f09349ff

Source: 857e1262-cb5c-4c22-b8da-770a8fcfa82e
  Migration: 21681d2f-d0b7-4eb5-9fce-5f41600ffe6f
  Zoho Ticket: 9b678f29-3b49-4842-9305-ada436cfc0b3
  Field Feedback: ef5fcb3c-c27d-443c-bef2-32be1521baf1
  Internal: ea82838e-f5ee-4cc6-b5d3-da4ef9052343
  Roadmap: 0a60ef2b-bb3c-4023-a21a-ad1375c84ef8

Highest Tier: be348a1d-6a63-4da8-83bb-9038b24264ff

Requesting Clients: e2de3bd0-6ad9-4b31-bb09-104f6bef383d

Combined ARR: 29c41859-f24b-4143-9af4-a34202205641

Auto Score: fd77f978-eca8-499e-bc3c-dc1bf4b8181e

Tags: 291fd0e7-42e3-4376-bacd-2b54a6c1d48c
  Notification: 2eaf6034-ae01-42da-ace7-5832cfc3e44e
  Performance: 427c7f85-ee11-4cd0-99d2-10dcb67eb4c0
  Security: a580d60c-889a-4e5e-a199-d89dc5bb2001
  Data: 0bf3c2d8-2ac2-44c5-b214-0acfbfd94fd3
  Regression: 743fb482-4c9c-4e64-a33f-2d8437c7b9f7

Sprint Batch: 97b38eb3-4416-40c9-9e27-fd26b0174849

Wake Date: 701fdb31-1341-426a-be88-23d2e10edfec

Release Note: 49f6daf4-1eba-4ec9-9102-f5140a9f81c5

Notified Client: 479a95ce-5129-42d7-8fc6-48fcccc2ce7e

Design Spec: 723d8e39-a6b1-40b3-9154-0d64f843313d

Zoho Ticket Link: 4776215b-c725-4d79-8f20-c16f0f0145ac

Resolution: 63ef3458-cfa6-4a0b-ae44-18858cd555f0
  Completed: (fetch option ID)
  Declined: (fetch option ID)
  Sleeping: (fetch option ID)
  Duplicate: (fetch option ID)

## Key behaviour rules
- Agent NEVER sends directly to clients
- All client responses post as internal 
  notes in Zoho for human review
- Only trigger for client draft: 
  ClickUp task moves to On Prod status
- WhatsApp replaced entirely by Slack
- system_prompt.md is loaded at runtime 
  as the agent's instructions — 
  never hardcode its contents in code

## Build phases
Phase 1 — Zoho webhook + enrichment + 
          internal note (current focus)
Phase 2 — ClickUp task creation
Phase 3 — Slack integration
Phase 4 — RAG layer
Phase 5 — Railway deployment
Phase 6 — ClickUp migration