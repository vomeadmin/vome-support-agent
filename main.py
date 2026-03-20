import os

from dotenv import load_dotenv

load_dotenv()

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, Response

from agent import process_ticket, process_ticket_update
from database import init_db
from field_feedback import handle_field_feedback
from on_prod_handler import handle_on_prod
from slack_reply_handler import handle_reply
from slack_digest import send_daily_digest

REQUIRED_ENV = [
    "ANTHROPIC_API_KEY",
    "ZOHO_MCP_URL",
    "ZOHO_MCP_TOKEN",
    "ZOHO_ORG_ID",
]

def _check_env():
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        for v in missing:
            print(f"WARNING: missing required env variable: {v}")
        print("The agent may fail on requests that need these variables.")
    else:
        print("Startup: all required env variables present")
    mcp_url = os.environ.get("ZOHO_MCP_URL", "")
    if not mcp_url.startswith("http"):
        print("ERROR: ZOHO_MCP_URL not configured -- Zoho API calls will fail")


def _extract_zoho_payload(raw_body: bytes) -> dict:
    """Extract the inner payload dict from Zoho's webhook format."""
    print(f"[RAW PAYLOAD] {raw_body.decode('utf-8', errors='replace')[:3000]}")

    data = json.loads(raw_body)

    # Zoho sends a list wrapper
    if isinstance(data, list):
        print(f"Payload is a list with {len(data)} item(s) -- using first")
        entry = data[0] if data else {}
    else:
        entry = data

    # The actual ticket data is nested under "payload"
    ticket = entry.get("payload", entry)
    event_type = entry.get("eventType", "unknown")
    print(f"[EVENT TYPE] {event_type}")
    print(f"[PARSED TICKET] {json.dumps(ticket, default=str)[:2000]}")

    return ticket


def _build_ticket_data(ticket: dict) -> dict:
    """Build normalized ticket_data from Zoho's nested payload structure."""
    contact = ticket.get("contact") or {}
    first = contact.get("firstName") or ""
    last = contact.get("lastName") or ""
    contact_name = f"{first} {last}".strip() or ""

    raw_desc = ticket.get("description") or ""
    clean_body = re.sub(r"<[^>]+>", "", raw_desc).strip()

    return {
        "ticket_id": str(ticket.get("id", "unknown")),
        "ticket_number": str(ticket.get("ticketNumber", "")),
        "subject": ticket.get("subject", ""),
        "body": clean_body,
        "contact_email": contact.get("email", ""),
        "contact_name": contact_name,
    }

_check_env()
init_db()

app = FastAPI(title="Vome Support Agent")

# ---------------------------------------------------------------------------
# APScheduler — daily digest at 18:00 America/Montreal
# ---------------------------------------------------------------------------

_scheduler = BackgroundScheduler(timezone="America/Montreal")
_scheduler.add_job(send_daily_digest, CronTrigger(hour=18, minute=0))
_scheduler.start()


# ---------------------------------------------------------------------------
# Slack signature verification
# ---------------------------------------------------------------------------

def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret:
        return True  # Skip verification if not configured (dev only)
    base = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
    expected = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Slack events webhook
# ---------------------------------------------------------------------------

SLACK_TICKETS_CHANNEL = os.environ.get("SLACK_TICKETS_CHANNEL", "")
SLACK_FIELD_FEEDBACK_CHANNEL = os.environ.get("SLACK_CHANNEL_FIELD_FEEDBACK", "")


@app.post("/webhook/slack-events")
async def slack_events_webhook(request: Request):
    raw_body = await request.body()

    # Verify Slack request signature
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not _verify_slack_signature(raw_body, timestamp, signature):
        return Response(content="Invalid signature", status_code=403)

    payload = json.loads(raw_body)

    # Slack URL verification challenge (sent once when configuring Event Subscriptions)
    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}

    event = payload.get("event", {})
    event_type = event.get("type", "")
    channel = event.get("channel", "")

    # Ignore bot messages and non-message events we don't handle
    if event.get("bot_id") or event.get("subtype"):
        return {"status": "ok"}

    # file_shared — attach file data to event and route as a reply
    if event_type == "file_shared":
        file_id = event.get("file_id")
        if file_id and channel == SLACK_TICKETS_CHANNEL:
            try:
                file_info = __import__("slack_sdk", fromlist=["WebClient"])
                # Fetch file metadata from Slack
                from slack_sdk import WebClient as _WC
                _wc = _WC(token=os.environ.get("SLACK_BOT_TOKEN", ""))
                fdata = _wc.files_info(file=file_id).get("file", {})
                handle_reply({
                    "user": event.get("user_id"),
                    "text": "",
                    "thread_ts": fdata.get("shares", {}).get("public", {}).get(channel, [{}])[0].get("ts"),
                    "channel": channel,
                    "files": [fdata],
                })
            except Exception as e:
                print(f"file_shared handling failed: {e}")
        return {"status": "ok"}

    # message event — route by channel
    if event_type == "message":
        thread_ts = event.get("thread_ts")
        user = event.get("user")
        text = event.get("text", "")
        files = event.get("files", [])

        if channel == SLACK_TICKETS_CHANNEL and thread_ts:
            # Only process replies in threads (not new top-level messages)
            if thread_ts != event.get("ts"):
                handle_reply({
                    "user": user,
                    "text": text,
                    "thread_ts": thread_ts,
                    "channel": channel,
                    "files": files,
                })

        elif channel == SLACK_FIELD_FEEDBACK_CHANNEL:
            handle_field_feedback({
                "user": user,
                "text": text,
                "ts": event.get("ts", ""),
                "thread_ts": thread_ts,
                "files": files,
            })

    return {"status": "ok"}


@app.post("/webhook/zoho-ticket")
async def zoho_ticket_webhook(request: Request):
    raw_body = await request.body()
    ticket = _extract_zoho_payload(raw_body)
    ticket_data = _build_ticket_data(ticket)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(
        f"[{timestamp}] Ticket #{ticket_data['ticket_number']} "
        f"(ID: {ticket_data['ticket_id']}) "
        f"from {ticket_data['contact_email'] or 'no email'} "
        f"-- {ticket_data['subject'] or 'no subject'}"
    )

    process_ticket(ticket_data)

    return {"status": "ok"}


@app.post("/webhook/zoho-update")
async def zoho_update_webhook(request: Request):
    raw_body = await request.body()
    ticket = _extract_zoho_payload(raw_body)
    ticket_id = str(ticket.get("id", "unknown"))
    ticket_number = str(ticket.get("ticketNumber", ""))

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{timestamp}] Ticket update #{ticket_number} (ID: {ticket_id})")

    process_ticket_update(ticket_id)

    return {"status": "ok"}


@app.post("/webhook/clickup-status")
async def clickup_status_webhook(request: Request):
    raw_body = await request.body()
    payload = json.loads(raw_body)

    event = payload.get("event", "")
    task_id = payload.get("task_id", "")

    if event != "taskStatusUpdated" or not task_id:
        return {"status": "ok"}

    for item in payload.get("history_items", []):
        if item.get("field") != "status":
            continue
        new_status = (
            (item.get("after") or {}).get("status") or ""
        ).lower().strip()
        if new_status not in ("on prod", "on_prod", "on prod ✅"):
            continue
        user = item.get("user") or {}
        engineer_name = (
            user.get("username")
            or user.get("email")
            or "Engineer"
        )
        timestamp = datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        print(
            f"[{timestamp}] ON PROD detected — "
            f"task {task_id} by {engineer_name}"
        )
        handle_on_prod(task_id, engineer_name)
        break

    return {"status": "ok"}


@app.get("/health")
async def health():
    env_status = {v: bool(os.environ.get(v)) for v in REQUIRED_ENV}
    return {"status": "ok", "env": env_status}
