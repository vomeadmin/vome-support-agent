"""
clickup_waiting_client_handler.py

Handles ClickUp taskStatusUpdated -> "Waiting on Client".

When an engineer sets a task to "Waiting on Client":
  1. Fetch the linked Zoho ticket via Zoho Ticket Link custom field
  2. Look up the client's language from thread_map
  3. Have Claude draft a "need more info" message in the client's language
  4. Auto-send via ZohoDesk_sendReply (immediate, not draft)
  5. Tag the Zoho ticket with "waiting-client"
  6. Update thread_map status to "waiting-client"
"""

import os
import re

import anthropic
import httpx

from agent import (
    SYSTEM_PROMPT,
    ZOHO_FROM_ADDRESS,
    ZOHO_ORG_ID,
    _extract_ticket_fields,
    _format_conversations,
    _unwrap_mcp_result,
    _zoho_desk_call,
    fetch_ticket_conversations,
    fetch_ticket_from_zoho,
)
from database import (
    get_thread,
    get_thread_by_ticket_id,
    update_thread,
)

_anthropic = anthropic.Anthropic()

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

FIELD_ZOHO_TICKET_LINK = "4776215b-c725-4d79-8f20-c16f0f0145ac"


# ---------------------------------------------------------------------------
# ClickUp helpers
# ---------------------------------------------------------------------------

def _get_clickup_task(task_id: str) -> dict | None:
    """Fetch a ClickUp task by ID."""
    try:
        r = httpx.get(
            f"{CLICKUP_BASE}/task/{task_id}",
            headers={"Authorization": CLICKUP_API_TOKEN},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[WAITING] ClickUp get task failed ({task_id}): {e}")
        return None


def _extract_zoho_ticket_id(task: dict) -> str | None:
    """Extract Zoho ticket ID from the Zoho Ticket Link custom field."""
    for field in task.get("custom_fields") or []:
        if field.get("id") != FIELD_ZOHO_TICKET_LINK:
            continue
        value = field.get("value") or ""
        m = re.search(r"/dv/(\d+)", str(value))
        if m:
            return m.group(1)
        stripped = str(value).strip()
        if stripped.isdigit():
            return stripped
    return None


# ---------------------------------------------------------------------------
# Thread map helpers
# ---------------------------------------------------------------------------

def _find_thread_and_data(zoho_ticket_id: str) -> tuple[str | None, dict]:
    """Return (thread_ts, thread_data) for a Zoho ticket, or (None, {})."""
    result = get_thread_by_ticket_id(zoho_ticket_id)
    if not result:
        return (None, {})
    thread_ts = result[0]
    data = get_thread(thread_ts) or {}
    return (thread_ts, data)


# ---------------------------------------------------------------------------
# Claude draft: "need more info"
# ---------------------------------------------------------------------------

def _generate_need_info_message(
    ticket_fields: dict,
    conversations_text: str,
    language: str | None = None,
) -> str:
    """Use Claude to draft a 'need more info' message for the client.

    The message should be specific to the ticket -- not generic. Claude reads
    the ticket and conversation to determine what information is missing.
    """
    contact_name = ticket_fields.get("contact_name", "")
    subject = ticket_fields.get("subject", "")
    body = ticket_fields.get("description", "")

    lang_instruction = ""
    if language and language.lower() != "english":
        lang_instruction = (
            f"\nIMPORTANT: The client communicates in {language}. "
            f"Write the entire message in {language}. "
            f"Do not mix languages.\n"
        )

    prompt = (
        "An engineer is working on this support ticket but needs more "
        "information from the client before they can continue.\n"
        "Generate a short, warm message asking the client for the specific "
        "information that seems to be missing. Read the ticket and "
        "conversation carefully to determine what is needed.\n"
        "Follow all voice guidelines from the system prompt strictly.\n"
        "Do not use an em-dash anywhere in the response.\n"
        "Do not mention engineering, internal tools, or ClickUp.\n"
        "Sign off as: Sam | Vome support / support.vomevolunteer.com\n"
        "Output the message only -- no labels, no preamble.\n"
        f"{lang_instruction}\n"
        f"Client: {contact_name}\n"
        f"Subject: {subject}\n"
        f"Original ticket:\n{body}\n\n"
        f"Conversation thread:\n{conversations_text}"
    )

    response = _anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Zoho helpers
# ---------------------------------------------------------------------------

def _send_reply(ticket_id: str, content: str, to_email: str) -> bool:
    """Send a reply to the client via ZohoDesk (immediate send, not draft)."""
    result = _zoho_desk_call("ZohoDesk_sendReply", {
        "body": {
            "channel": "EMAIL",
            "fromEmailAddress": ZOHO_FROM_ADDRESS,
            "to": to_email,
            "content": content,
            "contentType": "plainText",
        },
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(f"[WAITING] sendReply failed on ticket {ticket_id}")
        return False
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"[WAITING] sendReply failed on ticket {ticket_id}: {data}")
        return False
    print(f"[WAITING] Need-info message sent on ticket {ticket_id}")
    return True


def _tag_zoho_ticket(ticket_id: str, tag: str) -> bool:
    """Add a tag to a Zoho ticket."""
    result = _zoho_desk_call("ZohoDesk_updateTicket", {
        "body": {"tag": tag},
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(f"[WAITING] Failed to tag ticket {ticket_id} with '{tag}'")
        return False
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"[WAITING] Tag failed on ticket {ticket_id}: {data}")
        return False
    print(f"[WAITING] Ticket {ticket_id} tagged '{tag}'")
    return True


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def handle_waiting_on_client(task_id: str, engineer_name: str) -> bool:
    """Process a ClickUp task moving to 'Waiting on Client'.

    Sends a 'need more info' message to the client and tags the Zoho ticket.
    Returns True on success.
    """
    print(f"[WAITING] Task {task_id} set to Waiting on Client by {engineer_name}")

    # 1. Fetch ClickUp task
    task = _get_clickup_task(task_id)
    if not task:
        print(f"[WAITING] Could not fetch ClickUp task {task_id}")
        return False

    task_title = task.get("name", task_id)

    # 2. Extract Zoho ticket ID
    zoho_ticket_id = _extract_zoho_ticket_id(task)
    if not zoho_ticket_id:
        print(
            f"[WAITING] No Zoho Ticket Link on task {task_id} ({task_title}) "
            "-- cannot send need-info message"
        )
        return False

    print(f"[WAITING] Zoho ticket ID: {zoho_ticket_id}")

    # 3. Fetch Zoho ticket + conversations
    zoho_ticket = fetch_ticket_from_zoho(zoho_ticket_id)
    conversations_result = fetch_ticket_conversations(zoho_ticket_id)

    fields = _extract_ticket_fields(zoho_ticket) if zoho_ticket else {}
    conversations_text = _format_conversations(conversations_result)

    contact_email = fields.get("contact_email", "")
    if not contact_email:
        print(f"[WAITING] No contact email on ticket {zoho_ticket_id} -- cannot send reply")
        return False

    # 4. Get language from thread_map
    language = None
    thread_ts, thread_data = _find_thread_and_data(zoho_ticket_id)
    if thread_data:
        classification_data = thread_data.get("classification") or {}
        language = classification_data.get("language")

    # 5. Generate need-info message with Claude
    try:
        message = _generate_need_info_message(
            ticket_fields=fields,
            conversations_text=conversations_text,
            language=language,
        )
    except Exception as e:
        print(f"[WAITING] Claude draft generation failed: {e}")
        first_name = (fields.get("contact_name") or "there").split()[0]
        message = (
            f"Hi {first_name}, we're looking into this and need a bit more "
            "information to continue. Could you provide any additional details, "
            "such as screenshots, steps to reproduce, or the affected user's email?\n"
            "Best,\n\nSam | Vome support\nsupport.vomevolunteer.com"
        )

    # 6. Auto-send via Zoho
    _send_reply(zoho_ticket_id, message, contact_email)

    # 7. Tag the Zoho ticket
    _tag_zoho_ticket(zoho_ticket_id, "waiting-client")

    # 8. Update thread_map status
    if thread_ts:
        update_thread(thread_ts, status="waiting-client")
        print(f"[WAITING] Thread {thread_ts} status set to waiting-client")

    print(f"[WAITING] Done for task {task_id} / ticket {zoho_ticket_id}")
    return True
