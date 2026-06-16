"""
clickup_waiting_client_handler.py

Handles ClickUp taskStatusUpdated -> "needs client info".

When an engineer sets a task to "needs client info":
  1. Fetch the linked Zoho ticket + ClickUp engineer notes (what's needed)
  2. Review the thread: is this info already requested, or already provided?
  3. If still needed: draft the request (signed Vic) and AUTO-SEND it
  4. Park the task at "awaiting client response" (Zoho -> Awaiting Client
     Response); it auto-resurfaces to "queued" on the client's reply
  5. If already asked -> park without a duplicate; if already answered ->
     re-queue for the engineer; if it can't auto-send -> Slack review draft
"""

import json
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
from status_constants import (
    THREAD_OPEN,
    THREAD_WAITING_CLIENT,
    CU_AWAITING_CLIENT,
    CU_WRITE_QUEUED_LOWER,
    ZOHO_AWAITING_CLIENT_RESPONSE,
    ZOHO_TAG_WAITING_CLIENT,
)
from signatures import signature, sign_message
from model_config import SUPPORT_MODEL

_anthropic = anthropic.Anthropic()
_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"
ZOHO_FROM_ADDRESS = os.environ.get(
    "ZOHO_FROM_ADDRESS", "support@vomevolunteer.zohodesk.com"
)

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
        "- Do not write a closing or signature; end at the"
        " last sentence (a signature is appended"
        " automatically)\n"
        "- Output the message only, no labels or preamble\n"
        f"{lang_instruction}"
        f"{engineer_block}\n"
        f"Client: {contact_name}\n"
        f"Subject: {subject}\n"
        f"Original ticket:\n{body}\n\n"
        f"Conversation thread:\n{conversations_text}"
    )

    response = _anthropic.messages.create(
        model=SUPPORT_MODEL,
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    # Auto-send category: needs-client-info requests are signed "Vic".
    return sign_message(response.content[0].text.strip(), "vic", language)


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
    original_channel: str,
    ticket_number: str,
    engineer_name: str,
    draft: str,
    zoho_ticket_id: str = "",
    clickup_task_id: str = "",
    ticket_fields: dict | None = None,
) -> str | None:
    """Post notification as a reply in the original thread's channel.

    Returns the message ts on success, None on failure.
    Posts to the original thread's channel (not CHANNEL_FINAL_REVIEW)
    so the thread_ts remains valid.
    """
    target_channel = original_channel or CHANNEL_FINAL_REVIEW
    text = _waiting_client_message(
        ticket_number, engineer_name, draft,
        ticket_fields=ticket_fields,
        zoho_ticket_id=zoho_ticket_id,
        clickup_task_id=clickup_task_id,
    )
    try:
        resp = _slack.chat_postMessage(
            channel=target_channel,
            thread_ts=thread_ts,
            text=text,
        )
        return resp.get("ts")
    except SlackApiError as e:
        print(
            "[WAITING] Slack post failed:"
            f" {e.response['error']}"
        )
        return None


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
# Auto-send (info request -> park at "awaiting client response")
# ---------------------------------------------------------------------------

def _set_clickup_status(task_id: str, status: str) -> bool:
    """Set a ClickUp task's status."""
    if not CLICKUP_API_TOKEN or not task_id:
        return False
    try:
        r = httpx.put(
            f"{CLICKUP_BASE}/task/{task_id}",
            json={"status": status},
            headers={
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        print(f"[NEEDS INFO] ClickUp task {task_id} -> {status}")
        return True
    except Exception as e:
        print(f"[NEEDS INFO] ClickUp status update failed ({task_id}): {e}")
        return False


def _set_zoho_status(ticket_id: str, status: str) -> bool:
    """Set a Zoho ticket's status."""
    result = _zoho_desk_call("ZohoDesk_updateTicket", {
        "body": {"status": status},
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(f"[NEEDS INFO] Zoho status update failed for {ticket_id}")
        return False
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"[NEEDS INFO] Zoho status error for {ticket_id}: {data}")
        return False
    print(f"[NEEDS INFO] Zoho ticket {ticket_id} -> {status}")
    return True


def _send_info_email(
    ticket_id: str, content: str, to_email: str, cc_email: str = ""
) -> bool:
    """Email the info request to the client via ZohoDesk_sendReply."""
    body: dict = {
        "channel": "EMAIL",
        "fromEmailAddress": ZOHO_FROM_ADDRESS,
        "content": content,
        "contentType": "plainText",
    }
    if to_email:
        body["to"] = to_email
    if cc_email:
        body["cc"] = cc_email
    result = _zoho_desk_call("ZohoDesk_sendReply", {
        "body": body,
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(f"[NEEDS INFO] sendReply failed (no result) — ticket {ticket_id}")
        return False
    if isinstance(result, dict) and result.get("isError"):
        print(f"[NEEDS INFO] sendReply error — ticket {ticket_id}: {result}")
        return False
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"[NEEDS INFO] sendReply Zoho error — ticket {ticket_id}: {data}")
        return False
    print(f"[NEEDS INFO] Info request emailed to client — ticket {ticket_id}")
    return True


def _assess_info_request_state(
    ticket_fields: dict, conversations_text: str, engineer_context: str
) -> dict:
    """Decide whether the engineer's info request should be sent.

    Returns {"recommendation", "reason", "last_relevant"} where
    recommendation is one of:
      - "send": the needed info has not been requested yet.
      - "skip_already_asked": an equivalent request is already outstanding
        and we are still waiting -> park, no duplicate.
      - "skip_already_answered": the client already provided it -> re-queue
        for the engineer, no email.
    Defaults to "send" on uncertainty or error (a blocked engineer is worse
    than a rare redundant ask).
    """
    contact_name = ticket_fields.get("contact_name", "")
    subject = ticket_fields.get("subject", "")
    prompt = (
        "An engineer flagged this ticket as needing more information from "
        "the client. Before we email the client, decide whether the request "
        "should actually go out.\n\n"
        "Use the engineer's notes to understand WHAT information is needed, "
        "then read the conversation thread and decide:\n"
        "- 'send': that information has not been asked for yet (or the "
        "engineer needs a new detail not previously requested).\n"
        "- 'skip_already_asked': we have ALREADY asked the client for this "
        "same information and have not received an answer yet. Sending again "
        "would be a duplicate nag.\n"
        "- 'skip_already_answered': the client has ALREADY provided this "
        "information in the thread, so we should not ask again.\n"
        "Weigh timing and order (who said what, and when). When unsure, "
        "choose 'send'.\n\n"
        f"Subject: {subject}\nClient: {contact_name}\n\n"
        "What the engineer needs (their notes):\n"
        f"{engineer_context or '(none provided)'}\n\n"
        "Conversation thread (oldest to newest, with timestamps):\n"
        f"{conversations_text}\n\n"
        "Return valid JSON only, no prose, no code fences:\n"
        "{\n"
        '  "recommendation": "send" | "skip_already_asked" '
        '| "skip_already_answered",\n'
        '  "reason": "one sentence explaining the decision",\n'
        '  "last_relevant": "one-line summary of the most recent relevant '
        'message, or empty"\n'
        "}"
    )
    try:
        resp = _anthropic.messages.create(
            model=SUPPORT_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        data = json.loads(raw)
    except Exception as e:
        print(f"[NEEDS INFO] Request-state review failed: {e}")
        return {
            "recommendation": "send",
            "reason": f"review failed ({e}); defaulting to send",
            "last_relevant": "",
        }
    rec = str(data.get("recommendation") or "").strip().lower()
    if rec not in ("send", "skip_already_asked", "skip_already_answered"):
        rec = "send"
    return {
        "recommendation": rec,
        "reason": str(data.get("reason", "")),
        "last_relevant": str(data.get("last_relevant", "")),
    }


def _needs_info_record_message(
    kind: str,
    ticket_number: str,
    engineer_name: str,
    ticket_fields: dict | None = None,
    zoho_ticket_id: str = "",
    clickup_task_id: str = "",
    draft: str = "",
    reason: str = "",
    last_relevant: str = "",
) -> str:
    """Build the informational Slack record for a needs-client-info outcome."""
    zoho_url = (
        "https://desk.zoho.com/support/vomevolunteer"
        f"/ShowHomePage.do#Cases/dv/{zoho_ticket_id}"
    ) if zoho_ticket_id else ""
    clickup_url = (
        f"https://app.clickup.com/t/{clickup_task_id}"
    ) if clickup_task_id else ""

    if kind == "sent":
        head = (
            f":outbox_tray: *Needs client info — #{ticket_number}"
            " — auto-sent (Vic)*"
        )
        sub = (
            f"*{engineer_name} needs detail from the client. Vic emailed"
            " the request; task parked at 'awaiting client response'.*"
        )
    elif kind == "skip_already_asked":
        head = (
            f":no_bell: *Needs client info — #{ticket_number}"
            " — no email sent*"
        )
        sub = (
            f"*{engineer_name} flagged this, but we've already asked the"
            " client and are still waiting. Parked, no duplicate sent.*"
        )
    else:  # skip_already_answered
        head = (
            f":leftwards_arrow_with_hook: *Needs client info —"
            f" #{ticket_number} — re-queued, no email*"
        )
        sub = (
            f"*{engineer_name} flagged this, but the client already"
            " provided the info. Re-queued for the engineer; no email.*"
        )

    lines = [head, sub]
    if ticket_fields:
        contact_name = ticket_fields.get("contact_name", "")
        contact_email = ticket_fields.get("contact_email", "")
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

    lines.append("")
    lines.append(_SEP)
    if kind == "sent":
        lines.append(":speech_balloon: *SENT TO CLIENT (signed Vic)*")
        lines.append("")
        lines.append(draft)
        lines.append(_SEP)
        lines.append("Zoho -> Awaiting Client Response.")
    else:
        if reason:
            lines.append(f"Why: {reason}")
        if last_relevant:
            lines.append(f"Last relevant message: {last_relevant}")
        lines.append(_SEP)
    return "\n".join(lines)


def _post_record(
    zoho_ticket_id: str,
    clickup_task_id: str,
    text: str,
    status: str,
    ticket_fields: dict,
    thread_ts: str | None,
) -> None:
    """Post a needs-info outcome record to Slack and set the thread status."""
    if thread_ts:
        thread_entry = get_thread(thread_ts) or {}
        channel = thread_entry.get("channel") or CHANNEL_FINAL_REVIEW
        try:
            _slack.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text
            )
        except SlackApiError as e:
            print(f"[NEEDS INFO] Record post failed: {e.response['error']}")
        update_thread(thread_ts, status=status, pending_send=None)
        return
    try:
        resp = _slack.chat_postMessage(channel=CHANNEL_FINAL_REVIEW, text=text)
        new_ts = resp["ts"]
        save_thread(
            thread_ts=new_ts,
            ticket_id=zoho_ticket_id,
            ticket_number=zoho_ticket_id,
            subject=ticket_fields.get("subject", ""),
            channel=CHANNEL_FINAL_REVIEW,
            clickup_task_id=clickup_task_id,
        )
        update_thread(new_ts, status=status)
    except SlackApiError as e:
        print(f"[NEEDS INFO] Record post failed: {e.response['error']}")


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def handle_needs_client_info(
    task_id: str, engineer_name: str
) -> bool:
    """Process a ClickUp task moving to 'needs client info'.

    Reviews the thread, then AUTO-SENDS the engineer-triggered info request
    to the client (signed Vic) and parks the task at 'awaiting client
    response' (Zoho -> Awaiting Client Response). It auto-resurfaces to
    'queued' when the client replies (handled in process_ticket_update).

    Skips the email when the info was already requested (parks, no duplicate)
    or already provided (re-queues for the engineer). Falls back to a Slack
    review draft if it cannot auto-send. Returns True on success.
    """
    print(
        f"[NEEDS INFO] Task {task_id} set to needs client info"
        f" by {engineer_name}"
    )

    # 1. Fetch ClickUp task
    task = _get_clickup_task(task_id)
    if not task:
        print(f"[NEEDS INFO] Could not fetch task {task_id}")
        return False
    task_title = task.get("name", task_id)

    # 2. Engineer context (task description + comments) — drives WHAT to ask
    comments = _get_clickup_task_comments(task_id)
    engineer_context = _format_engineer_context(task, comments)

    # 3. Extract Zoho ticket ID
    zoho_ticket_id = _extract_zoho_ticket_id(task)
    if not zoho_ticket_id:
        print(
            f"[NEEDS INFO] No Zoho Ticket Link on task"
            f" {task_id} ({task_title})"
        )
        return False
    print(f"[NEEDS INFO] Zoho ticket ID: {zoho_ticket_id}")

    # 4. Fetch Zoho ticket + conversations
    zoho_ticket = fetch_ticket_from_zoho(zoho_ticket_id)
    conversations_result = fetch_ticket_conversations(zoho_ticket_id)
    fields = _extract_ticket_fields(zoho_ticket) if zoho_ticket else {}
    conversations_text = _format_conversations(conversations_result)
    contact_email = fields.get("contact_email", "")
    cc_email = fields.get("cc_email", "")

    # 5. Language + thread lookup
    thread_ts, thread_data = _find_thread_and_data(zoho_ticket_id)
    language = None
    if thread_data:
        cls = thread_data.get("classification") or {}
        language = cls.get("language")
    ticket_number = (
        (thread_data or {}).get("ticket_number") or zoho_ticket_id
    )

    # 6. Pre-send review — should this request actually go out?
    assessment = _assess_info_request_state(
        fields, conversations_text, engineer_context
    )
    rec = assessment["recommendation"]
    print(
        f"[NEEDS INFO] Request review — ticket {zoho_ticket_id}:"
        f" recommendation={rec} reason={assessment['reason']}"
    )

    # 6a. Client already provided the info -> re-queue, no email
    if rec == "skip_already_answered":
        _set_clickup_status(task_id, CU_WRITE_QUEUED_LOWER)
        text = _needs_info_record_message(
            "skip_already_answered", ticket_number, engineer_name,
            ticket_fields=fields, zoho_ticket_id=zoho_ticket_id,
            clickup_task_id=task_id, reason=assessment["reason"],
            last_relevant=assessment["last_relevant"],
        )
        _post_record(
            zoho_ticket_id, task_id, text, THREAD_OPEN, fields, thread_ts
        )
        print(f"[NEEDS INFO] Client already answered — re-queued {task_id}")
        return True

    # 6b. Already asked and still waiting -> park, no duplicate
    if rec == "skip_already_asked":
        _set_clickup_status(task_id, CU_AWAITING_CLIENT)
        _set_zoho_status(zoho_ticket_id, ZOHO_AWAITING_CLIENT_RESPONSE)
        _tag_zoho_ticket(zoho_ticket_id, ZOHO_TAG_WAITING_CLIENT)
        text = _needs_info_record_message(
            "skip_already_asked", ticket_number, engineer_name,
            ticket_fields=fields, zoho_ticket_id=zoho_ticket_id,
            clickup_task_id=task_id, reason=assessment["reason"],
            last_relevant=assessment["last_relevant"],
        )
        _post_record(
            zoho_ticket_id, task_id, text,
            THREAD_WAITING_CLIENT, fields, thread_ts,
        )
        print(f"[NEEDS INFO] Already asked, parked {task_id}")
        return True

    # 7. Generate the Vic request from the engineer's notes
    try:
        draft = _generate_need_info_message(
            ticket_fields=fields,
            conversations_text=conversations_text,
            engineer_context=engineer_context,
            language=language,
        )
    except Exception as e:
        print(f"[NEEDS INFO] Claude draft failed: {e}")
        first_name = (fields.get("contact_name") or "there").split()[0]
        draft = (
            f"Hi {first_name}, we've been looking into"
            " this and need a bit more information to"
            " continue. Could you provide any additional"
            " details, such as screenshots, steps to"
            " reproduce, or the affected user's email?\n"
            + signature("vic")
        )

    # 8. Auto-send the request to the client (signed Vic)
    can_send = (
        bool(contact_email) and bool(draft) and len(draft.strip()) >= 20
    )
    sent = (
        _send_info_email(
            zoho_ticket_id, draft,
            to_email=contact_email, cc_email=cc_email,
        )
        if can_send
        else False
    )

    if sent:
        _set_clickup_status(task_id, CU_AWAITING_CLIENT)
        _set_zoho_status(zoho_ticket_id, ZOHO_AWAITING_CLIENT_RESPONSE)
        _tag_zoho_ticket(zoho_ticket_id, ZOHO_TAG_WAITING_CLIENT)
        text = _needs_info_record_message(
            "sent", ticket_number, engineer_name,
            ticket_fields=fields, zoho_ticket_id=zoho_ticket_id,
            clickup_task_id=task_id, draft=draft,
        )
        _post_record(
            zoho_ticket_id, task_id, text,
            THREAD_WAITING_CLIENT, fields, thread_ts,
        )
        print(
            f"[NEEDS INFO] Auto-sent + parked — ticket {zoho_ticket_id},"
            f" task {task_id}"
        )
        return True

    # 9. Fallback — cannot auto-send (no contact email, empty draft, or send
    # error). Post the draft to Slack with confirm/send/cancel for manual send.
    print(
        f"[NEEDS INFO] Auto-send unavailable for ticket {zoho_ticket_id}"
        " — falling back to Slack review"
    )
    if thread_ts:
        original_channel = (
            (thread_data or {}).get("channel") or CHANNEL_FINAL_REVIEW
        )
        msg_ts = _post_to_existing_thread(
            thread_ts=thread_ts,
            original_channel=original_channel,
            ticket_number=ticket_number,
            engineer_name=engineer_name,
            draft=draft,
            zoho_ticket_id=zoho_ticket_id,
            clickup_task_id=task_id,
            ticket_fields=fields,
        )
        if not msg_ts:
            return False
    else:
        thread_ts = _create_new_thread(
            zoho_ticket_id=zoho_ticket_id,
            ticket_fields=fields,
            engineer_name=engineer_name,
            draft=draft,
            clickup_task_id=task_id,
        )
        if not thread_ts:
            return False

    _store_pending_send(thread_ts, draft)
    update_thread(thread_ts, status=THREAD_WAITING_CLIENT)
    _tag_zoho_ticket(zoho_ticket_id, ZOHO_TAG_WAITING_CLIENT)
    print(f"[NEEDS INFO] Review draft posted, thread_ts={thread_ts}")
    return True
