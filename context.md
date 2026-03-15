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

## ClickUp space
VOME Operations — brand new space, 
do not touch VOMEDev space

Folders:
- Support & Bugs (Inbox/In Progress/
  On Dev/On Prod/Closed)
- Feature Requests (Raw Intake/Under Review/
  Roadmap Backlog/Sleeping/Archived)
- Design Queue
- Current Sprint
- CEO Dashboard

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