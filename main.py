import os

from dotenv import load_dotenv

load_dotenv()

import json
import re
from datetime import datetime, timezone

from fastapi import FastAPI, Request

from agent import process_ticket, process_ticket_update

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

app = FastAPI(title="Vome Support Agent")


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


@app.get("/health")
async def health():
    return {"status": "ok"}
