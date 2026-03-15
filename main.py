import os

from dotenv import load_dotenv

load_dotenv()

import json
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

_check_env()

app = FastAPI(title="Vome Support Agent")


@app.post("/webhook/zoho-ticket")
async def zoho_ticket_webhook(request: Request):
    raw_body = await request.body()
    print(f"[RAW PAYLOAD] {raw_body.decode('utf-8', errors='replace')}")

    payload = json.loads(raw_body)

    # Zoho may send a list or a dict
    if isinstance(payload, list):
        print(f"Payload is a list with {len(payload)} item(s) -- using first")
        entry = payload[0] if payload else {}
    else:
        entry = payload

    print(f"[PARSED ENTRY] {json.dumps(entry, default=str)[:2000]}")

    ticket_data = {
        "ticket_id": entry.get("ticket_id", entry.get("ticketId", entry.get("id", "unknown"))),
        "subject": entry.get("subject", ""),
        "body": entry.get("body", entry.get("description", "")),
        "contact_email": entry.get("contact_email", entry.get("contactEmail", entry.get("email", ""))),
        "contact_name": entry.get("contact_name", entry.get("contactName", "")),
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(
        f"[{timestamp}] Ticket #{ticket_data['ticket_id']} "
        f"from {ticket_data['contact_email'] or 'no email'} "
        f"— {ticket_data['subject'] or 'no subject'}"
    )

    process_ticket(ticket_data)

    return {"status": "ok"}


@app.post("/webhook/zoho-update")
async def zoho_update_webhook(request: Request):
    raw_body = await request.body()
    print(f"[RAW UPDATE PAYLOAD] {raw_body.decode('utf-8', errors='replace')}")

    payload = json.loads(raw_body)

    if isinstance(payload, list):
        print(f"Update payload is a list with {len(payload)} item(s) -- using first")
        entry = payload[0] if payload else {}
    else:
        entry = payload

    print(f"[PARSED UPDATE ENTRY] {json.dumps(entry, default=str)[:2000]}")

    ticket_id = entry.get("ticket_id", entry.get("ticketId", entry.get("id", "unknown")))

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{timestamp}] Ticket update #{ticket_id}")

    process_ticket_update(ticket_id)

    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
