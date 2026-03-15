import os

from dotenv import load_dotenv

load_dotenv()

from datetime import datetime, timezone

from fastapi import FastAPI, Request

from agent import process_ticket

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
    payload = await request.json()

    ticket_data = {
        "ticket_id": payload.get("ticket_id", "unknown"),
        "subject": payload.get("subject", ""),
        "body": payload.get("body", ""),
        "contact_email": payload.get("contact_email", ""),
        "contact_name": payload.get("contact_name", ""),
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(
        f"[{timestamp}] Ticket #{ticket_data['ticket_id']} "
        f"from {ticket_data['contact_email'] or 'no email'} "
        f"— {ticket_data['subject'] or 'no subject'}"
    )

    process_ticket(ticket_data)

    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
