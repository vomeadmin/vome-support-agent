# Vome Support Widget — Project Brief for Claude

## What we're building
A native support intake chat widget embedded inside the Vome Django 
application, backed by the existing FastAPI intelligence layer. The goal 
is to replace the current blank Zoho Desk submission form with a guided, 
authenticated chat experience that extracts complete ticket information 
before anything reaches engineering.

## The core problem we're solving
Support tickets arrive with insufficient context — no affected user email, 
no module, no screenshot. Engineers (Sanjay, OnlyG) waste time chasing 
information instead of fixing bugs. ~30% of tickets are answerable via 
self-service but still reach the engineering queue.

## Architecture decision (final)
- Django app handles (django-chats): chat UI widget management, screen capture, S3 uploads, 
  session context extraction, rendering responses
- FastAPI app handles: all Claude logic, KB search, completeness scoring, 
  Zoho ticket creation, ClickUp routing, Slack notifications
- They communicate via: POST /chat/intake (one clean interface)
- Django never touches Claude directly
- FastAPI never touches websockets or Django auth directly

## What Django passes to FastAPI on each turn
{
  "message": "user's message",
  "session_context": {
    "user_email": "...",
    "user_role": "admin|volunteer",
    "org_name": "...",
    "org_id": "...",
    "tier": "Enterprise|Pro|Recruit",
    "current_page": "/admin/scheduling",
    "platform": "web"
  },
  "conversation_history": [...previous turns],
  "attachments": ["s3_url_if_any"]
}

## What FastAPI returns
{
  "reply": "message to show user",
  "status": "collecting|deflecting|confirming|complete",
  "kb_article": { "title": "...", "url": "...", "days_stale": 45 } | null,
  "ticket_created": false,
  "ticket_id": null
}

## KB freshness logic
- < 90 days since modifiedTime: suggest confidently
- 90–365 days: suggest with caveat ("last updated X days ago")
- > 365 days: don't deflect, flag in ClickUp for KB refresh
- No article found + issue seen 3+ times: create ClickUp task 
  "KB article needed — [topic]"

## Completeness gate (in FastAPI, before ClickUp task creation)
Required fields to proceed to ticket creation:
- affected_user_email (or confirmed it's the admin themselves)
- module (mapped to ClickUp module field IDs in CONTEXT.md)
- description of behavior (what happened vs. what was expected)
- platform (web/mobile/both)
Missing any 2+: ask targeted follow-up, don't create ClickUp task yet

## Screen capture
- Django frontend uses getDisplayMedia() for screenshots
- MediaRecorder API for screen recordings
- Uploaded to existing S3 bucket before ticket submission
- URL passed in attachments[] field to FastAPI
- FastAPI attaches to Zoho ticket on creation

## Zoho Desk relationship
- support.vomevolunteer.com: stays live, ticket STATUS tracking only
- "New Ticket" button: suppressed via Zoho portal customization
- Email fallback (support@vomevolunteer.zohodesk.com): still works, 
  but completeness gate added to agent.py before ClickUp task creation
- All ticket creation still goes through existing Zoho MCP calls

## Files to know about
- support-agent — core ticket processing (add completeness gate here)
- support-agent  — loaded at runtime, don't hardcode
- support-agent  — 20 templates, loaded into prompt
- support-agent  — full system context (read this)
- /django-chats — existing Django chat app, websocket infrastructure

## Team
- Sam (you): CEO, full-stack, primary reviewer
- OnlyG (ID: 49257687): lead backend — assign backend/FastAPI tasks
- Sanjay (ID: 4434086): frontend — assign React/Django widget tasks

## What not to touch
- Existing webhook endpoints (/webhook/zoho-ticket, /webhook/zoho-update, 
  /webhook/clickup-status) — these work, don't break them
- Existing ClickUp field IDs and custom field mappings — in CONTEXT.md
- Existing Slack channel routing logic
- PostgreSQL schema — don't alter ticket_threads or processed_events tables