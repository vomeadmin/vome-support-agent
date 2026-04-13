# Build Order

## Phase 1 — FastAPI (vome-intelligence)
- [ ] POST /chat/intake endpoint — conversation loop with Claude
- [ ] KB search + freshness scoring against Zoho KB API  
- [ ] Completeness gate added to agent.py
- [ ] Email fallback: auto-reply for incomplete tickets, park in Pending

## Phase 2 — Django (vome-core)
- [ ] SupportWidget React component — chat bubble UI
- [ ] Screen capture: getDisplayMedia() screenshot + MediaRecorder recording
- [ ] S3 upload for capture files (hook into existing S3 setup)
- [ ] Session context extraction from Django auth + current page
- [ ] /support route or modal that renders SupportWidget

## Phase 3 — Integration
- [ ] Wire Django widget → FastAPI /chat/intake
- [ ] End-to-end test: widget → intake agent → Zoho ticket → ClickUp → Slack
- [ ] Suppress "New Ticket" on Zoho portal
- [ ] Update auto-ack template with new intake URL

## Phase 4 — KB maintenance layer
- [ ] Stale article flagging → ClickUp task creation
- [ ] "KB article needed" detection for repeated uncovered topics