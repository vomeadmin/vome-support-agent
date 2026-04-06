"""
clickup_waiting_client_handler.py

Handles ClickUp taskStatusUpdated -> "Waiting on Client".

When an engineer sets a task to "Waiting on Client":
  1. Fetch the linked Zoho ticket via Zoho Ticket Link custom field
  2. Fetch ClickUp task comments for engineer context
  3. Look up the client's language from thread_map
  4. Have Claude draft a contextual message using the engineer's notes
  5. Route draft to Slack (#admin-tickets) for Sam to review
  6. Tag the Zoho ticket with "waiting-client"
  7. Update thread_map status to "waiting-client"
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
    _unwrap_mcp_result,
    _zoho_desk_call,
    fetch_ticket_conversations,
    fetch_ticket_from_zoho,
)
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

CHANNEL_FINAL_REVIEW = os.environ.get(
    "SLACK_CHANNEL_SUPPORT_FINAL_REVIEW", ""
)

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
        print(
            f"[WAITING] ClickUp get task failed ({task_id}): {e}"
        )
        return None


def _get_clickup_task_comments(task_id: str) -> list[dict]:
    """Fetch comments on a ClickUp task (engineer notes)."""
    try:
        r = httpx.get(
            f"{CLICKUP_BASE}/task/{task_id}/comment",
            headers={"Authorization": CLICKUP_API_TOKEN},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("comments", [])
    except Exception as e:
        print(
            f"[WAITING] ClickUp comments fetch failed"
            f" ({task_id}): {e}"
        )
        return []


def _format_engineer_context(
    task: dict, comments: list[dict]
) -> str:
    """Build a text block of all engineer context from ClickUp.

    Includes the task description and all comments.
    """
    parts: list[str] = []
    desc = (task.get("description") or "").strip()
    if desc:
        parts.append(f"Task description:\n{desc}")

    for c in comments:
        # Each comment has comment_text or comment list
        text_parts = []
        for item in c.get("comment", []):
            if item.get("text"):
                text_parts.append(item["text"])
        comment_text = " ".join(text_parts).strip()
        if not comment_text:
            comment_text = c.get("comment_text", "").strip()
        if comment_text:
            user = c.get("user", {}).get("username", "engineer")
            parts.append(f"{user}: {comment_text}")

    return "\n\n".join(parts) if parts else ""


def _extract_zoho_ticket_id(task: dict) -> str | None:
    """Extract Zoho ticket ID from the Zoho Ticket Link field."""
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

def _find_thread_and_data(
    zoho_ticket_id: str,
) -> tuple[str | None, dict]:
    """Return (thread_ts, thread_data) for a Zoho ticket."""
    result = get_thread_by_ticket_id(zoho_ticket_id)
    if not result:
        return (None, {})
    thread_ts = result[0]
    data = get_thread(thread_ts) or {}
    return (thread_ts, data)


def _store_pending_send(thread_ts: str, message: str):
    """Persist the draft as pending_send in the database."""
    update_thread(thread_ts, pending_send=message)


# ---------------------------------------------------------------------------
# Claude draft: contextual "need more info"
# ---------------------------------------------------------------------------

def _generate_need_info_message(
    ticket_fields: dict,
    conversations_text: str,
    engineer_context: str,
    language: str | None = None,
) -> str:
    """Use Claude to draft a 'need more info' message.

    Uses the engineer's ClickUp comments/notes as primary context
    to determine what information to request from the client.
    """
    contact_name = ticket_fields.get("contact_name", "")
    subject = ticket_fields.get("subject", "")
    body = ticket_fields.get("description", "")

    lang_instruction = ""
    if language and language.lower() != "english":
        lang_instruction = (
            f"\nIMPORTANT: The client communicates in"
            f" {language}. Write the entire message in"
            f" {language}. Do not mix languages.\n"
        )

    engineer_block = ""
    if engineer_context:
        engineer_block = (
            "\n\nENGINEER'S NOTES (from ClickUp task):\n"
            "These are the engineer's internal notes about"
            " what they found and what they need. Use this"
            " as the PRIMARY basis for your message. Translate"
            " the engineer's technical findings into a clear,"
            " client-friendly request.\n"
            f"{engineer_context}\n"
        )

    prompt = (
        "An engineer is working on this support ticket and"
        " needs more information from the client.\n"
        "Generate a message asking the client for the specific"
        " information needed.\n\n"
        "CRITICAL: Read the engineer's notes carefully. They"
        " explain what was investigated and what info is"
        " missing. Your message must reflect their specific"
        " findings and requests, NOT generic questions.\n\n"
        "Rules:\n"
        "- Be specific about what you need based on the"
        " engineer's notes\n"
        "- If the engineer found something specific, mention"
        " it (e.g. 'We looked into X and found Y, but need"
        " Z to continue')\n"
        "- Follow all voice guidelines from system prompt\n"
        "- Do not use an em-dash anywhere in the response\n"
        "- Do not mention engineering, internal tools, or"
        " ClickUp\n"
        "- Say 'we' not 'I' when referring to the team's"
        " investigation\n"
        "- Sign off as: Sam | Vome support /"
        " support.vomevolunteer.com\n"
        "- Output the message only, no labels or preamble\n"
        f"{lang_instruction}"
        f"{engineer_block}\n"
        f"Client: {contact_name}\n"
        f"Subject: {subject}\n"
        f"Original ticket:\n{body}\n\n"
        f"Conversation thread:\n{conversations_text}"
    )

    response = _anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Zoho helpers
# ---------------------------------------------------------------------------

def _tag_zoho_ticket(ticket_id: str, tag: str) -> bool:
    """Add a tag to a Zoho ticket."""
    result = _zoho_desk_call("ZohoDesk_updateTicket", {
        "body": {"tag": tag},
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(
            f"[WAITING] Failed to tag ticket"
            f" {ticket_id} with '{tag}'"
        )
        return False
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(
            f"[WAITING] Tag failed on ticket"
            f" {ticket_id}: {data}"
        )
        return False
    print(f"[WAITING] Ticket {ticket_id} tagged '{tag}'")
    return True


# ---------------------------------------------------------------------------
# Slack posting
# ---------------------------------------------------------------------------

def _waiting_client_message(
    ticket_number: str,
    engineer_name: str,
    draft: str,
    ticket_fields: dict | None = None,
    zoho_ticket_id: str = "",
    clickup_task_id: str = "",
) -> str:
    """Build the Waiting on Client Slack message."""
    zoho_url = (
        "https://desk.zoho.com/support/vomevolunteer"
        f"/ShowHomePage.do#Cases/dv/{zoho_ticket_id}"
    ) if zoho_ticket_id else ""
    clickup_url = (
        f"https://app.clickup.com/t/{clickup_task_id}"
    ) if clickup_task_id else ""

    lines = [
        f":hourglass: *Waiting on Client"
        f" — #{ticket_number}*",
        f"*{engineer_name} needs more info from client.*",
    ]
    if ticket_fields:
        contact_name = ticket_fields.get("contact_name", "")
        contact_email = ticket_fields.get(
            "contact_email", ""
        )
        subject = ticket_fields.get("subject", "")
        contact_line = (
            f"{contact_name} ({contact_email})"
            if contact_email
            else contact_name or "Unknown"
        )
        lines.append(f"*Contact:* {contact_line}")
        lines.append(f"*Subject:* {subject}")

    link_parts = []
    if zoho_url:
        link_parts.append(f"<{zoho_url}|Zoho>")
    if clickup_url:
        link_parts.append(f"<{clickup_url}|ClickUp>")
    if link_parts:
        lines.append(" | ".join(link_parts))

    lines += [
        "",
        _SEP,
        ":speech_balloon: *DRAFT — not sent yet*",
        "",
        draft,
        _SEP,
        "`confirm` — send as-is",
        "`send: [your version]` — send your custom reply",
        "`redraft: [your notes]` — redraft with pointers",
        "`cancel` — hold, don't send",
    ]
    return "\n".join(lines)


def _post_to_existing_thread(
    thread_ts: str,
    ticket_number: str,
    engineer_name: str,
    draft: str,
    zoho_ticket_id: str = "",
    clickup_task_id: str = "",
    ticket_fields: dict | None = None,
) -> bool:
    """Post notification as a reply in an existing thread."""
    text = _waiting_client_message(
        ticket_number, engineer_name, draft,
        ticket_fields=ticket_fields,
        zoho_ticket_id=zoho_ticket_id,
        clickup_task_id=clickup_task_id,
    )
    try:
        _slack.chat_postMessage(
            channel=CHANNEL_FINAL_REVIEW,
            thread_ts=thread_ts,
            text=text,
        )
        return True
    except SlackApiError as e:
        print(
            "[WAITING] Slack post failed:"
            f" {e.response['error']}"
        )
        return False


def _create_new_thread(
    zoho_ticket_id: str,
    ticket_fields: dict,
    engineer_name: str,
    draft: str,
    clickup_task_id: str,
) -> str | None:
    """Create a new Slack thread for this ticket.

    Returns thread_ts of the new message, or None.
    """
    subject = ticket_fields.get(
        "subject", "(unknown subject)"
    )
    text = _waiting_client_message(
        ticket_number=zoho_ticket_id,
        engineer_name=engineer_name,
        draft=draft,
        ticket_fields=ticket_fields,
        zoho_ticket_id=zoho_ticket_id,
        clickup_task_id=clickup_task_id,
    )
    try:
        resp = _slack.chat_postMessage(
            channel=CHANNEL_FINAL_REVIEW, text=text,
        )
        thread_ts = resp["ts"]
        save_thread(
            thread_ts=thread_ts,
            ticket_id=zoho_ticket_id,
            ticket_number=zoho_ticket_id,
            subject=subject,
            channel=CHANNEL_FINAL_REVIEW,
            clickup_task_id=clickup_task_id,
        )
        return thread_ts
    except SlackApiError as e:
        print(
            "[WAITING] New thread failed:"
            f" {e.response['error']}"
        )
        return None


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def handle_waiting_on_client(
    task_id: str, engineer_name: str
) -> bool:
    """Process a ClickUp task moving to 'Waiting on Client'.

    Drafts a contextual message using the engineer's notes
    and posts it to Slack for Sam's review before sending.
    Returns True on success.
    """
    print(
        f"[WAITING] Task {task_id} set to Waiting on Client"
        f" by {engineer_name}"
    )

    # 1. Fetch ClickUp task
    task = _get_clickup_task(task_id)
    if not task:
        print(f"[WAITING] Could not fetch task {task_id}")
        return False

    task_title = task.get("name", task_id)

    # 2. Fetch ClickUp comments (engineer's notes)
    comments = _get_clickup_task_comments(task_id)
    engineer_context = _format_engineer_context(
        task, comments
    )
    if engineer_context:
        print(
            f"[WAITING] Got engineer context"
            f" ({len(comments)} comments)"
        )
    else:
        print("[WAITING] No engineer context found")

    # 3. Extract Zoho ticket ID
    zoho_ticket_id = _extract_zoho_ticket_id(task)
    if not zoho_ticket_id:
        print(
            f"[WAITING] No Zoho Ticket Link on task"
            f" {task_id} ({task_title})"
        )
        return False

    print(f"[WAITING] Zoho ticket ID: {zoho_ticket_id}")

    # 4. Fetch Zoho ticket + conversations
    zoho_ticket = fetch_ticket_from_zoho(zoho_ticket_id)
    conversations_result = fetch_ticket_conversations(
        zoho_ticket_id
    )

    fields = (
        _extract_ticket_fields(zoho_ticket)
        if zoho_ticket else {}
    )
    conversations_text = _format_conversations(
        conversations_result
    )

    contact_email = fields.get("contact_email", "")
    if not contact_email:
        print(
            f"[WAITING] No contact email on ticket"
            f" {zoho_ticket_id}"
        )
        return False

    # 5. Get language from thread_map
    language = None
    thread_ts, thread_data = _find_thread_and_data(
        zoho_ticket_id
    )
    if thread_data:
        cls = thread_data.get("classification") or {}
        language = cls.get("language")

    # 6. Generate draft with engineer context
    try:
        draft = _generate_need_info_message(
            ticket_fields=fields,
            conversations_text=conversations_text,
            engineer_context=engineer_context,
            language=language,
        )
    except Exception as e:
        print(f"[WAITING] Claude draft failed: {e}")
        first_name = (
            fields.get("contact_name") or "there"
        ).split()[0]
        draft = (
            f"Hi {first_name}, we've been looking into"
            " this and need a bit more information to"
            " continue. Could you provide any additional"
            " details, such as screenshots, steps to"
            " reproduce, or the affected user's email?\n"
            "Best,\n\nSam | Vome support\n"
            "support.vomevolunteer.com"
        )

    # 7. Post to Slack for review (NOT auto-send)
    if not thread_ts:
        thread_ts, thread_data = _find_thread_and_data(
            zoho_ticket_id
        )

    if thread_ts:
        ticket_number = (
            thread_data.get("ticket_number")
            or zoho_ticket_id
        )
        posted = _post_to_existing_thread(
            thread_ts=thread_ts,
            ticket_number=ticket_number,
            engineer_name=engineer_name,
            draft=draft,
            zoho_ticket_id=zoho_ticket_id,
            clickup_task_id=task_id,
            ticket_fields=fields,
        )
        if not posted:
            return False
    else:
        print(
            f"[WAITING] No Slack thread for ticket"
            f" {zoho_ticket_id} — creating new one"
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

    # 8. Store draft as pending + update status
    _store_pending_send(thread_ts, draft)
    update_thread(thread_ts, status="waiting-client")
    print(
        f"[WAITING] Draft posted to Slack for review,"
        f" thread_ts={thread_ts}"
    )

    # 9. Tag the Zoho ticket
    _tag_zoho_ticket(zoho_ticket_id, "waiting-client")

    print(
        f"[WAITING] Done for task {task_id}"
        f" / ticket {zoho_ticket_id}"
    )
    return True
