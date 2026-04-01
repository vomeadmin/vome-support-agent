"""
on_prod_handler.py

Handles the ON PROD -> Slack notification flow.

When a ClickUp task status changes to ON PROD:
  1. Read the Zoho Ticket Link custom field from the task
  2. Fetch full ticket details + conversations from Zoho
  3. Generate a resolution draft with Claude
  4. Find the existing Slack thread (or create a new one)
  5. Post the ON PROD notification with a pending draft
"""

import os
import re

import anthropic
import httpx
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from agent import (
    SYSTEM_PROMPT,
    ZOHO_ORG_ID,
    _extract_ticket_fields,
    _format_conversations,
    _zoho_mcp_call,
    _unwrap_mcp_result,
    fetch_ticket_conversations,
    fetch_ticket_from_zoho,
)
from slack_ticket_brief import CHANNEL_TICKETS
from database import (
    get_thread,
    get_thread_by_ticket_id,
    save_thread,
    update_thread,
)

_anthropic = anthropic.Anthropic()
_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

# Custom field ID for Zoho Ticket Link (from context.md)
FIELD_ZOHO_TICKET_LINK = "4776215b-c725-4d79-8f20-c16f0f0145ac"

_SEP = "─────────────────────────────────────"


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
        print(f"[ON PROD] ClickUp get task failed ({task_id}): {e}")
        return None


def _extract_zoho_ticket_id(task: dict) -> str | None:
    """Extract Zoho ticket ID from the Zoho Ticket Link custom field."""
    for field in task.get("custom_fields") or []:
        if field.get("id") != FIELD_ZOHO_TICKET_LINK:
            continue
        value = field.get("value") or ""
        # URL format: .../ShowHomePage.do#Cases/dv/{ticket_id}
        m = re.search(r"/dv/(\d+)", str(value))
        if m:
            return m.group(1)
        # Plain numeric ID stored directly
        stripped = str(value).strip()
        if stripped.isdigit():
            return stripped
    return None


def update_clickup_status_finished(task_id: str) -> bool:
    """Set ClickUp task status to FINISHED."""
    try:
        r = httpx.put(
            f"{CLICKUP_BASE}/task/{task_id}",
            json={"status": "FINISHED"},
            headers={
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[ON PROD] ClickUp status update failed ({task_id}): {e}")
        return False


# ---------------------------------------------------------------------------
# Zoho status update
# ---------------------------------------------------------------------------

def _set_zoho_status_final_review(zoho_ticket_id: str) -> bool:
    """Update the Zoho ticket status to 'Final Review'."""
    result = _zoho_mcp_call("ZohoDesk_updateTicket", {
        "body": {"status": "Final Review"},
        "path_variables": {"ticketId": str(zoho_ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(f"[ON PROD] Failed to set Zoho ticket {zoho_ticket_id} to Final Review")
        return False
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"[ON PROD] Zoho status update failed for ticket {zoho_ticket_id}: {data}")
        return False
    print(f"[ON PROD] Zoho ticket {zoho_ticket_id} status set to Final Review")
    return True


# ---------------------------------------------------------------------------
# Thread map helpers
# ---------------------------------------------------------------------------

def _find_thread_ts(zoho_ticket_id: str) -> str | None:
    """Return the Slack thread_ts for this Zoho ticket, or None."""
    result = get_thread_by_ticket_id(zoho_ticket_id)
    if result:
        return result[0]
    return None


def set_thread_on_prod_pending(thread_ts: str):
    """Mark a thread as awaiting client notification after ON PROD."""
    update_thread(thread_ts, status="on_prod_pending")


def _store_pending_send(thread_ts: str, message: str):
    """Persist the resolution draft as pending_send in the database."""
    update_thread(thread_ts, pending_send=message)


# ---------------------------------------------------------------------------
# Draft generation
# ---------------------------------------------------------------------------

def _generate_resolution_draft(
    ticket_fields: dict,
    conversations_text: str,
    classification: str,
    module: str,
    language: str | None = None,
) -> str:
    """Call Claude to generate a resolution confirmation response.

    If ``language`` is provided (e.g. "French"), the draft will be
    written in that language.  Otherwise defaults to English.
    """
    contact_name = ticket_fields.get("contact_name", "")
    subject = ticket_fields.get("subject", "")
    body = ticket_fields.get("description", "")

    lang_instruction = ""
    if language and language.lower() != "english":
        lang_instruction = (
            f"\nIMPORTANT: The client communicates in {language}. "
            f"Write the entire draft response in {language}. "
            f"Do not mix languages.\n"
        )

    user_message = (
        "Generate a resolution confirmation response for this support ticket.\n"
        "The issue has been fixed and deployed to production.\n"
        "Follow all voice guidelines from the system prompt strictly.\n"
        "Confirm what was resolved -- be specific to the issue, not generic.\n"
        "Do not use technical jargon or mention internal tools or processes.\n"
        "Do not use an em-dash anywhere in the response.\n"
        "Output the draft response only -- no labels, no analysis, no preamble.\n"
        f"{lang_instruction}\n"
        f"Classification: {classification}\n"
        f"Module: {module}\n"
        f"Client: {contact_name}\n"
        f"Subject: {subject}\n"
        f"Original ticket:\n{body}\n\n"
        f"Conversation thread:\n{conversations_text}"
    )

    response = _anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Slack posting
# ---------------------------------------------------------------------------

def _on_prod_message(
    ticket_number: str, engineer_name: str, draft: str, include_header: bool = False,
    ticket_fields: dict | None = None, zoho_ticket_id: str = "",
) -> str:
    """Build the ON PROD Slack message block."""
    lines = [
        f"🚀 *On Prod — #{ticket_number}*",
        f"*{engineer_name} marked this as fixed.*",
    ]
    if include_header and ticket_fields:
        contact_name = ticket_fields.get("contact_name", "")
        contact_email = ticket_fields.get("contact_email", "")
        subject = ticket_fields.get("subject", "")
        zoho_url = (
            f"https://desk.zoho.com/support/vomevolunteer"
            f"/ShowHomePage.do#Cases/dv/{zoho_ticket_id}"
        )
        contact_line = (
            f"{contact_name} ({contact_email})"
            if contact_email
            else contact_name or "Unknown"
        )
        lines += [
            f"*Contact:* {contact_line}",
            f"*Subject:* {subject}",
            f"*Zoho:* {zoho_url}",
        ]
    lines += [
        "",
        _SEP,
        "💬 *SUGGESTED RESOLUTION — not sent yet*",
        "",
        draft,
        _SEP,
        "Reply `confirm` to send to client",
        "Reply `send: [your version]` to customise",
        "Reply `cancel` to hold — I'll remind you in tonight's digest",
    ]
    return "\n".join(lines)


def _post_to_existing_thread(
    thread_ts: str,
    ticket_number: str,
    engineer_name: str,
    draft: str,
) -> bool:
    """Post ON PROD notification as a reply in an existing Slack thread."""
    text = _on_prod_message(ticket_number, engineer_name, draft)
    try:
        _slack.chat_postMessage(
            channel=CHANNEL_TICKETS,
            thread_ts=thread_ts,
            text=text,
        )
        return True
    except SlackApiError as e:
        print(f"[ON PROD] Slack post failed: {e.response['error']}")
        return False


def _create_new_thread(
    zoho_ticket_id: str,
    ticket_fields: dict,
    engineer_name: str,
    draft: str,
    clickup_task_id: str,
) -> str | None:
    """
    Create a new #vome-tickets message for pre-Slack tickets.
    Returns thread_ts of the new message, or None on failure.
    """
    subject = ticket_fields.get("subject", "(unknown subject)")
    text = _on_prod_message(
        ticket_number=zoho_ticket_id,
        engineer_name=engineer_name,
        draft=draft,
        include_header=True,
        ticket_fields=ticket_fields,
        zoho_ticket_id=zoho_ticket_id,
    )
    try:
        resp = _slack.chat_postMessage(channel=CHANNEL_TICKETS, text=text)
        thread_ts = resp["ts"]
        save_thread(
            thread_ts=thread_ts,
            ticket_id=zoho_ticket_id,
            ticket_number=zoho_ticket_id,
            subject=subject,
            channel=CHANNEL_TICKETS,
            clickup_task_id=clickup_task_id,
        )
        return thread_ts
    except SlackApiError as e:
        print(f"[ON PROD] New thread post failed: {e.response['error']}")
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def handle_on_prod(task_id: str, engineer_name: str) -> bool:
    """
    Main entry point for the ON PROD flow.

    Called when a ClickUp task status changes to ON PROD.
    Returns True if the Slack notification was posted successfully.
    """
    print(f"[ON PROD] Task {task_id} marked on prod by {engineer_name}")

    # Step 1 — fetch ClickUp task
    task = _get_clickup_task(task_id)
    if not task:
        print(f"[ON PROD] Could not fetch ClickUp task {task_id}")
        return False

    task_title = task.get("name", task_id)
    description = task.get("description") or ""

    # Pull classification + module from task description for draft context
    cl_match = re.search(
        r"Classification:\s*(.+)", description, re.IGNORECASE
    )
    mod_match = re.search(
        r"Module:\s*(.+)", description, re.IGNORECASE
    )
    classification = cl_match.group(1).strip() if cl_match else "Bug"
    module = mod_match.group(1).strip() if mod_match else "Other"

    # Step 2 — extract Zoho ticket ID
    zoho_ticket_id = _extract_zoho_ticket_id(task)
    if not zoho_ticket_id:
        print(
            f"[ON PROD] No Zoho ticket ID in task {task_id} ({task_title}) "
            "— Zoho Ticket Link field may be empty"
        )
        return False

    print(f"[ON PROD] Zoho ticket ID: {zoho_ticket_id}")

    # Step 3 — set Zoho ticket status to Final Review
    _set_zoho_status_final_review(zoho_ticket_id)

    # Step 4 — fetch Zoho ticket + conversations
    zoho_ticket = fetch_ticket_from_zoho(zoho_ticket_id)
    conversations_result = fetch_ticket_conversations(zoho_ticket_id)

    fields = _extract_ticket_fields(zoho_ticket) if zoho_ticket else {}
    conversations_text = _format_conversations(conversations_result)

    # Step 5 — determine client language from thread_map
    language = None
    thread_ts = _find_thread_ts(zoho_ticket_id)
    if thread_ts:
        thread_entry = get_thread(thread_ts) or {}
        classification_data = thread_entry.get("classification") or {}
        # Language may be stored directly or inferred from tags
        language = classification_data.get("language")

    # Step 6 — generate resolution draft (in client's language)
    try:
        draft = _generate_resolution_draft(
            ticket_fields=fields,
            conversations_text=conversations_text,
            classification=classification,
            module=module,
            language=language,
        )
    except Exception as e:
        print(f"[ON PROD] Draft generation failed: {e}")
        first_name = fields.get("contact_name", "there").split()[0]
        draft = (
            f"Hi {first_name}, the issue you reported has been resolved "
            "and the fix is now live. Let us know if anything else comes up.\n"
            "Best,\n\nSam | Vome support\nsupport.vomevolunteer.com"
        )

    # Step 7 — find existing Slack thread or create new one
    # (thread_ts may already be set from the language lookup above)
    if not thread_ts:
        thread_ts = _find_thread_ts(zoho_ticket_id)

    if thread_ts:
        thread_entry = get_thread(thread_ts) or {}
        ticket_number = thread_entry.get("ticket_number") or zoho_ticket_id
        posted = _post_to_existing_thread(
            thread_ts=thread_ts,
            ticket_number=ticket_number,
            engineer_name=engineer_name,
            draft=draft,
        )
        if not posted:
            return False
    else:
        print(
            f"[ON PROD] No existing Slack thread for ticket {zoho_ticket_id} "
            "— creating new message"
        )
        thread_ts = _create_new_thread(
            zoho_ticket_id=zoho_ticket_id,
            ticket_fields=fields,
            engineer_name=engineer_name,
            draft=draft,
            clickup_task_id=task_id,
        )
        if not thread_ts:
            return False

    # Store pending draft + mark status
    _store_pending_send(thread_ts, draft)
    set_thread_on_prod_pending(thread_ts)
    print(f"[ON PROD] Notification posted, thread_ts={thread_ts}")
    return True
