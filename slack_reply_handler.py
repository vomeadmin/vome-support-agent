"""
slack_reply_handler.py

Handles all of Sam's replies in #vome-tickets threads.
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import anthropic
import httpx
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from agent import (
    NOTE_HEADER,
    SYSTEM_PROMPT,
    UPDATE_HEADER,
    ZOHO_ORG_ID,
    _detect_language,
    _extract_ticket_fields,
    _format_conversations,
    _unwrap_mcp_result,
    _zoho_mcp_call,
    fetch_crm_account,
    fetch_ticket_conversations,
    fetch_ticket_from_zoho,
)
from slack_ticket_brief import _load_thread_map, _save_thread_map

# ---------------------------------------------------------------------------
# Clients & config
# ---------------------------------------------------------------------------

_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
_anthropic = anthropic.Anthropic()

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"
CLICKUP_ACCEPTED_BACKLOG_LIST = "901113389889"

PRIORITY_MAP = {"p1": 1, "p2": 2, "p3": 3}

# ---------------------------------------------------------------------------
# Assignee config — ClickUp IDs, Zoho agent IDs, Zoho statuses
# ---------------------------------------------------------------------------

# Canonical key → ClickUp user ID
_ASSIGNEE_IDS = {
    "sam":    os.environ.get("CLICKUP_USER_SAM", ""),
    "onlyg":  os.environ.get("CLICKUP_USER_ONLYG", ""),
    "sanjay": os.environ.get("CLICKUP_USER_SANJAY", ""),
    "ron":    os.environ.get("CLICKUP_USER_RON", ""),
}

# Canonical key → Zoho Desk agent ID
_ZOHO_AGENT_IDS = {
    "sam":    os.environ.get("ZOHO_AGENT_SAM", ""),
    "onlyg":  os.environ.get("ZOHO_AGENT_BACKEND", ""),
    "sanjay": os.environ.get("ZOHO_AGENT_FRONTEND", ""),
    "ron":    os.environ.get("ZOHO_AGENT_RON", ""),
}

# Canonical key → Zoho ticket status after assignment
_ZOHO_STATUS = {
    "sam":    "Processing",
    "onlyg":  "Pending Dev Fix",
    "sanjay": "Pending Dev Fix",
    "ron":    "Processing",
}

# Canonical key → human-readable display label
_ASSIGNEE_DISPLAY = {
    "sam":    "Sam",
    "onlyg":  "OnlyG",
    "sanjay": "Sanjay",
    "ron":    "Ron",
}

# Canonical key → Zoho team/agent display label (shown in confirmation)
_ZOHO_AGENT_LABEL = {
    "sam":    "Support Agent (Sam)",
    "onlyg":  "Backend Team (OnlyG)",
    "sanjay": "Frontend Team (Sanjay)",
    "ron":    "Vome Support Team (Ron)",
}

# All accepted aliases → canonical key (case-insensitive after .lower())
_ASSIGNEE_ALIASES: dict[str, str] = {
    # Sam
    "sam":      "sam",
    "saul":     "sam",
    "me":       "sam",
    "myself":   "sam",
    # OnlyG
    "onlyg":    "onlyg",
    "only g":   "onlyg",
    "backend":  "onlyg",
    # Sanjay
    "sanjay":   "sanjay",
    "frontend": "sanjay",
    # Ron
    "ron":      "ron",
}

# Keep for any legacy references
ASSIGNEE_MAP = _ASSIGNEE_IDS

# Custom field IDs (from context.md)
FIELD_HIGHEST_TIER = "be348a1d-6a63-4da8-83bb-9038b24264ff"
FIELD_COMBINED_ARR = "29c41859-f24b-4143-9af4-a34202205641"
FIELD_AUTO_SCORE = "fd77f978-eca8-499e-bc3c-dc1bf4b8181e"

# Words that signal an internal instruction — never send to client
_INTERNAL_KEYWORDS = {
    "assign", "clickup", "priority", "score",
    "p1", "p2", "p3", "tier", "arr", "onlyg", "sanjay",
    "sam", "note", "skip", "move", "backlog", "draft",
    "route", "fix",
}

# Patterns that signal Sam is proposing a re-draft inline.
# Captures everything after the colon as the client-facing text.
_REPHRASE_RE = re.compile(
    r"\b(?:saying something like|something like|"
    r"try this|try something like|how about|use this)\s*:\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)

# Patterns that mean Sam is describing what to tell the client.
# These trigger immediate AI-generated draft generation.
_DRAFT_TRIGGERS = re.compile(
    r"\b(?:"
    r"draft\s+(?:a\s+)?(?:reply|response|something|an?\s+\w+)"
    r"|write\s+(?:a\s+)?(?:reply|response|message)"
    r"|tell\s+them\b"
    r"|let\s+them\s+know\b"
    r"|respond\s+saying\b"
    r"|say\s+something\s+like\b"
    r")",
    re.IGNORECASE,
)

# Patterns that signal Sam wants the ticket closed after sending.
_CLOSE_TICKET_RE = re.compile(
    r"\b(?:close\s+(?:the\s+)?ticket|close\s+it)\b",
    re.IGNORECASE,
)

# Explicit note syntax — ONLY these trigger ClickUp note creation.
_EXPLICIT_NOTE_RE = re.compile(
    r"^(?:add\s+)?note\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)

# Patterns that mean Sam wants to see the ticket content.
_SHOW_TICKET_RE = re.compile(
    r"\b(?:"
    r"what\s+did\s+(?:she|he|they)\s+say"
    r"|what\s+(?:was|is)\s+the\s+(?:message|issue|ticket|problem|request)"
    r"|what\s+exactly\s+did\s+they\s+(?:write|say|send)"
    r"|show\s+me\s+the\s+ticket"
    r"|can\s+you\s+(?:show|provide)"
    r"|(?:she|he|they|the\s+client)\s+said"
    r"|more\s+details"
    r"|tell\s+me\s+more"
    r")",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# ClickUp REST helpers
# ---------------------------------------------------------------------------

def _cu_headers() -> dict:
    return {
        "Authorization": CLICKUP_API_TOKEN,
        "Content-Type": "application/json",
    }


def _cu_update_task(task_id: str, payload: dict) -> bool:
    try:
        r = httpx.put(
            f"{CLICKUP_BASE}/task/{task_id}",
            json=payload,
            headers=_cu_headers(),
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"ClickUp update task failed ({task_id}): {e}")
        return False


def _cu_set_field(task_id: str, field_id: str, value) -> bool:
    try:
        r = httpx.post(
            f"{CLICKUP_BASE}/task/{task_id}/field/{field_id}",
            json={"value": value},
            headers=_cu_headers(),
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"ClickUp set field failed ({task_id}/{field_id}): {e}")
        return False


def _cu_move_to_list(task_id: str, list_id: str) -> bool:
    try:
        r = httpx.post(
            f"{CLICKUP_BASE}/list/{list_id}/task/{task_id}",
            headers=_cu_headers(),
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"ClickUp move task failed ({task_id} -> {list_id}): {e}")
        return False


def _cu_upload_attachment(
    task_id: str, filename: str, file_content: bytes
) -> bool:
    try:
        r = httpx.post(
            f"{CLICKUP_BASE}/task/{task_id}/attachment",
            headers={"Authorization": CLICKUP_API_TOKEN},
            files={"attachment": (filename, file_content)},
            timeout=30,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"ClickUp attachment upload failed ({task_id}): {e}")
        return False


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def _add_reaction(channel: str, ts: str, emoji: str):
    try:
        _slack.reactions_add(channel=channel, timestamp=ts, name=emoji)
    except SlackApiError as e:
        if e.response["error"] != "already_reacted":
            print(f"reactions_add failed ({emoji}): {e.response['error']}")


def _reply(channel: str, thread_ts: str, text: str):
    try:
        _slack.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=text
        )
    except SlackApiError as e:
        print(f"chat_postMessage failed: {e.response['error']}")


# ---------------------------------------------------------------------------
# Thread map status
# ---------------------------------------------------------------------------

def _set_thread_status(thread_ts: str, status: str):
    data = _load_thread_map()
    if thread_ts in data:
        data[thread_ts]["status"] = status
        _save_thread_map(data)


def _store_pending_send(
    thread_ts: str, message: str, close_after: bool = False
):
    """Save a client message that needs Sam's `confirm` before sending."""
    data = _load_thread_map()
    if thread_ts in data:
        data[thread_ts]["pending_send"] = message
        data[thread_ts]["close_after_send"] = close_after
        _save_thread_map(data)


def _pop_pending_send(thread_ts: str) -> str | None:
    """Return and clear the pending client message, or None if absent."""
    data = _load_thread_map()
    entry = data.get(thread_ts)
    if not entry:
        return None
    msg = entry.pop("pending_send", None)
    if msg is not None:
        _save_thread_map(data)
    return msg


def _has_internal_keyword(text: str) -> bool:
    """Return True if text contains any internal-only keyword."""
    words = set(re.findall(r"\b\w+\b", text.lower()))
    if words & _INTERNAL_KEYWORDS:
        return True
    # Phrase checks
    low = text.lower()
    return any(
        phrase in low
        for phrase in ("on clickup", "in clickup", "to clickup")
    )


# ---------------------------------------------------------------------------
# Zoho client reply (emails the client via ZohoDesk_sendReply)
# ---------------------------------------------------------------------------

ZOHO_FROM_ADDRESS = os.environ.get(
    "ZOHO_FROM_ADDRESS", "admin@vomevolunteer.com"
)


def _send_client_reply(
    ticket_id: str, content: str, to_email: str = ""
) -> bool:
    """Send an email reply to the client via ZohoDesk_sendReply.

    This actually emails the client — do NOT use for internal notes.
    Internal notes use ZohoDesk_createTicketComment with isPublic=False.
    """
    body: dict = {
        "channel": "EMAIL",
        "fromEmailAddress": ZOHO_FROM_ADDRESS,
        "content": content,
        "contentType": "plainText",
    }
    if to_email:
        body["to"] = to_email

    result = _zoho_mcp_call("ZohoDesk_sendReply", {
        "body": body,
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if result:
        print(f"Email reply sent to client — Zoho ticket {ticket_id}")
        return True
    print(
        f"Failed to send email reply — Zoho ticket {ticket_id}"
    )
    return False


# ---------------------------------------------------------------------------
# Zoho ticket assignment helpers
# ---------------------------------------------------------------------------

def _zoho_assign_ticket(ticket_id: str, agent_id: str) -> bool:
    """Reassign a Zoho Desk ticket to the given agent ID."""
    result = _zoho_mcp_call("ZohoDesk_updateTicket", {
        "body": {"assigneeId": str(agent_id)},
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if result:
        print(
            f"Zoho ticket {ticket_id} assigned to agent {agent_id}"
        )
        return True
    print(
        f"Zoho assign failed — ticket {ticket_id}, agent {agent_id}"
    )
    return False


def _zoho_set_status(ticket_id: str, status: str) -> bool:
    """Update the status of a Zoho Desk ticket."""
    result = _zoho_mcp_call("ZohoDesk_updateTicket", {
        "body": {"status": status},
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if result:
        print(f"Zoho ticket {ticket_id} status → {status}")
        return True
    print(f"Zoho status update failed — ticket {ticket_id}")
    return False


# ---------------------------------------------------------------------------
# Draft instruction detection helpers
# ---------------------------------------------------------------------------

def _has_close_instruction(text: str) -> bool:
    """Return True if Sam's message asks to close the ticket."""
    return bool(_CLOSE_TICKET_RE.search(text))


def _is_client_response_instruction(text: str) -> bool:
    """Return True if Sam's message describes what to tell the client."""
    return bool(_DRAFT_TRIGGERS.search(text)) or _has_close_instruction(text)


# ---------------------------------------------------------------------------
# Draft generation
# ---------------------------------------------------------------------------

def _generate_draft(ticket_id: str) -> str:
    """Call Claude to generate a fresh draft response for this ticket."""
    zoho_ticket = fetch_ticket_from_zoho(ticket_id)
    conversations_result = fetch_ticket_conversations(ticket_id)

    if not zoho_ticket:
        return "(Could not fetch ticket data to generate draft)"

    fields = _extract_ticket_fields(zoho_ticket)
    contact_email = fields["contact_email"]
    body = fields["description"]
    subject = fields["subject"]
    contact_name = fields["contact_name"]
    thread_text = _format_conversations(conversations_result)

    crm = (
        fetch_crm_account(contact_email)
        if contact_email
        else {"found": False}
    )
    if crm["found"]:
        enrichment_block = (
            f"Account: {crm['account_name']}\n"
            f"Tier: {crm['tier']}\n"
            f"ARR: {crm['arr']}"
        )
    else:
        enrichment_block = "Contact type: Volunteer"

    lang_note = ""
    detected = _detect_language(body) or _detect_language(thread_text)
    if detected:
        lang_note = f"\nRespond in {detected}.\n"

    user_message = (
        "Generate a client-facing draft response for this ticket.\n"
        "Follow all voice guidelines from the system prompt.\n"
        "Output the draft response only — no analysis, no labels.\n\n"
        f"{enrichment_block}\n{lang_note}"
        f"Subject: {subject}\n"
        f"Client: {contact_name}\n"
        f"Ticket body:\n{body}\n\n"
        f"Full thread:\n{thread_text}"
    )

    response = _anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def _generate_draft_from_instruction(
    ticket_id: str, sam_instruction: str
) -> str:
    """Generate a client-facing draft from Sam's plain-language instruction."""
    zoho_ticket = fetch_ticket_from_zoho(ticket_id)
    conversations_result = fetch_ticket_conversations(ticket_id)

    if not zoho_ticket:
        return "(Could not fetch ticket data to generate draft)"

    fields = _extract_ticket_fields(zoho_ticket)
    contact_name = fields["contact_name"]
    body = fields["description"]
    subject = fields["subject"]
    thread_text = _format_conversations(conversations_result)

    user_message = (
        f"Sam wants to send this message to the client: "
        f"'{sam_instruction}'\n\n"
        f"The ticket context is:\n"
        f"Subject: {subject}\n"
        f"Client: {contact_name}\n"
        f"Ticket body:\n{body}\n\n"
        f"Full thread:\n{thread_text}\n\n"
        "Write a clean client-facing response following the voice guidelines "
        "in the system prompt. Keep it short and warm. "
        "Do not use em-dash. "
        "Sign off: Best, Sam | Vome team"
    )

    response = _anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Conversation formatter (for thread/history and draft commands)
# ---------------------------------------------------------------------------

_CONV_SEP = "─────────────────────────────────────"


def _format_conversation_for_slack(
    ticket_id: str, thread_data: dict
) -> str:
    """Fetch fresh ticket + conversations from Zoho and format for Slack.

    Always fetches live data — never uses cached values.
    Returns a formatted multi-line string ready to post.
    """
    zoho_ticket = fetch_ticket_from_zoho(ticket_id)
    conversations_result = fetch_ticket_conversations(ticket_id)

    fields = _extract_ticket_fields(zoho_ticket) if zoho_ticket else {}
    subject = fields.get("subject") or thread_data.get("subject", "")
    contact_name = fields.get("contact_name", "")
    contact_email = fields.get("contact_email", "")
    created_time = fields.get("created_time", "")

    ticket_number = thread_data.get("ticket_number", ticket_id)
    zoho_url = (
        f"https://desk.zoho.com/support/vomevolunteer"
        f"/ShowHomePage.do#Cases/dv/{ticket_id}"
    )

    if contact_name and contact_email:
        contact_line = f"{contact_name} ({contact_email})"
    else:
        contact_line = contact_name or contact_email or "Unknown"

    lines = [
        f"📋 *Full Conversation — #{ticket_number}*",
        f"*Subject:* {subject}",
        f"*Contact:* {contact_line}",
    ]
    if created_time:
        lines.append(f"*Opened:* {created_time}")
    lines.append(_CONV_SEP)

    # Unwrap and reverse to chronological order (Zoho returns newest first)
    data = (
        _unwrap_mcp_result(conversations_result)
        if conversations_result else None
    )
    if isinstance(data, dict):
        data = data.get("data", [])
    if not isinstance(data, list):
        data = []

    msg_count = 0
    last_timestamp = ""

    for entry in reversed(data):
        content = entry.get("content") or entry.get("summary", "")
        # Skip agent's own analysis notes
        if content and (NOTE_HEADER in content or UPDATE_HEADER in content):
            continue

        author = entry.get("author", {}) or {}
        author_name = author.get("name") or "Unknown"
        timestamp = entry.get("createdTime") or entry.get("sendDateTime", "")
        is_public = entry.get("isPublic", True)

        msg_count += 1
        if timestamp:
            last_timestamp = timestamp

        clean = re.sub(r"<[^>]+>", "", content or "").strip()
        visibility = "" if is_public else " [internal]"

        lines.append(f"*{author_name}*{visibility} — {timestamp}")
        lines.append(clean if clean else "(no content)")

        # Attachment count per message
        attachments = entry.get("attachments")
        if isinstance(attachments, list):
            attach_count = len(attachments)
        else:
            try:
                attach_count = int(entry.get("attachmentCount") or 0)
            except (ValueError, TypeError):
                attach_count = 0
        if attach_count > 0:
            noun = "attachment" if attach_count == 1 else "attachments"
            lines.append(f"📎 {attach_count} {noun}")

        lines.append(_CONV_SEP)

    if msg_count == 0:
        lines.append("(No messages found)")
        lines.append(_CONV_SEP)

    noun = "message" if msg_count == 1 else "messages"
    lines.append(f"{msg_count} {noun} total")
    if last_timestamp:
        lines.append(f"Last activity: {last_timestamp}")
    lines.append(f"View in Zoho: {zoho_url}")
    lines.append(_CONV_SEP)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File attachment handling
# ---------------------------------------------------------------------------

def _download_slack_file(url: str) -> bytes | None:
    try:
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        r = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            follow_redirects=True,
        )
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"Slack file download failed: {e}")
        return None


def _handle_attachments(
    files: list,
    clickup_task_id: str | None,
    channel: str,
    thread_ts: str,
):
    if not files or not clickup_task_id:
        return
    uploaded = 0
    for f in files:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        filename = f.get("name") or f"attachment_{int(time.time())}"
        content = _download_slack_file(url)
        if content and _cu_upload_attachment(
            clickup_task_id, filename, content
        ):
            uploaded += 1
    if uploaded:
        noun = "screenshot" if uploaded == 1 else "screenshots"
        _reply(channel, thread_ts, f"📎 {uploaded} {noun} added to ClickUp")


# ---------------------------------------------------------------------------
# Command parser
# ---------------------------------------------------------------------------

def _parse_commands(text: str) -> tuple[dict, str]:
    """
    Extract recognized commands from text.
    Returns (commands_dict, remaining_text).
    remaining_text is everything left after stripping command tokens.
    Command order matters: note is parsed last and consumes trailing text.
    """
    remaining = text
    commands: dict[str, str] = {}

    # p1 / p2 / p3
    m = re.search(r"\b(p[123])\b", remaining, re.IGNORECASE)
    if m:
        commands["priority"] = m.group(1).lower()
        remaining = remaining[:m.start()] + remaining[m.end():]

    # assign [name]
    m = re.search(r"\bassign\s+(\w+)", remaining, re.IGNORECASE)
    if m:
        commands["assign"] = m.group(1)
        remaining = remaining[:m.start()] + remaining[m.end():]

    # tier [value]
    m = re.search(r"\btier\s+(\S+)", remaining, re.IGNORECASE)
    if m:
        commands["tier"] = m.group(1)
        remaining = remaining[:m.start()] + remaining[m.end():]

    # arr [number]
    m = re.search(r"\barr\s+([\d,]+)", remaining, re.IGNORECASE)
    if m:
        commands["arr"] = m.group(1).replace(",", "")
        remaining = remaining[:m.start()] + remaining[m.end():]

    # score [number]
    m = re.search(r"\bscore\s+(\d+)", remaining, re.IGNORECASE)
    if m:
        commands["score"] = m.group(1)
        remaining = remaining[:m.start()] + remaining[m.end():]

    # Clean up separators left behind after command extraction
    response = re.sub(r"^[\s\-\u2014|]+", "", remaining).strip()
    response = re.sub(r"[\s\-\u2014|]+$", "", response).strip()

    return commands, response


# ---------------------------------------------------------------------------
# Natural language parsing (Claude fallback)
# ---------------------------------------------------------------------------

_NL_HELP = (
    "Got it — no actions taken.\n\n"
    "Nothing sent to client.\n"
    "To send a response use:\n"
    "`send: [your message]`\n"
    "Or reply `draft` for a suggested response.\n\n"
    "Other commands: `assign Sam` `p1` `p2` `p3` "
    "`score [n]` `skip` `move backlog`"
)


def _parse_with_claude(text: str) -> dict:
    """
    Call Claude to parse natural language intent from Sam's message.
    Returns a dict with the same keys as the JSON schema, or {} on failure.
    """
    prompt = (
        f"Sam sent this reply about a support ticket:\n'{text}'\n\n"
        "Extract any instructions intended for the system"
        " (not for the client).\n"
        "Return valid JSON only — no prose, no code block markers.\n"
        "{\n"
        '  "assign_to": null or "Sam/OnlyG/Sanjay/Ron",\n'
        '  "priority": null or "p1/p2/p3",\n'
        '  "score": null or number,\n'
        '  "tier": null or string,\n'
        '  "arr": null or number,\n'
        '  "note": null or string,\n'
        '  "client_response": null or string,\n'
        '  "skip": false or true\n'
        "}\n\n"
        "Rules:\n"
        "- client_response: only text clearly written TO the client "
        "(e.g. starts with Hi/Hello/Thanks, or is a direct answer to "
        "their question). If the message contains internal instructions "
        "(assign, clickup, priority, score, tier, arr, sam, onlyg, "
        "sanjay, skip, move, backlog, draft) set client_response to "
        "null — never mix client text with internal instructions.\n"
        "- assign_to: look for names Sam, Saul (=Sam), OnlyG, "
        "Sanjay, Ron\n"
        "- score: any number mentioned in context of "
        "priority/importance"
    )
    try:
        response = _anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        parsed = json.loads(raw)
        print(f"Claude NLP parse result: {parsed}")
        return parsed
    except Exception as e:
        print(f"Claude NLP parse failed: {e}")
        return {}


def _build_confirmation(action_lines: list[str]) -> str:
    """Build the structured confirmation message after commands run."""
    lines = ["Got it — actions taken:"]
    for al in action_lines:
        lines.append(f"→ {al}")
    lines.append("")
    lines.append(
        "Nothing sent to client.\n"
        "To send a response use:\n"
        "`send: [your message]`\n"
        "Or reply `draft` for a suggested response."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def handle_reply(event: dict):
    """
    Process a Slack message event from a #vome-tickets thread.

    event keys: user, text, thread_ts, channel, files (optional list)
    """
    thread_ts = event.get("thread_ts")
    channel = event.get("channel")
    text = (event.get("text") or "").strip()
    files = event.get("files", [])

    if not thread_ts or not channel:
        return

    thread_data = _load_thread_map().get(thread_ts)
    if not thread_data:
        return  # Not a known ticket brief thread — ignore

    ticket_id = thread_data["ticket_id"]
    clickup_task_id = thread_data.get("clickup_task_id")

    # Handle file attachments (independent of text commands)
    if files:
        _handle_attachments(files, clickup_task_id, channel, thread_ts)

    if not text:
        return

    text_lower = text.lower().strip()

    # -----------------------------------------------------------------------
    # Single-word commands that short-circuit all other parsing
    # -----------------------------------------------------------------------

    if text_lower == "skip":
        _add_reaction(channel, thread_ts, "double_vertical_bar")
        _reply(
            channel, thread_ts,
            "Parked — will appear in tonight's digest",
        )
        if clickup_task_id:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            _cu_update_task(
                clickup_task_id,
                {"description": (
                    f"Sam parked this on {date_str} — pending follow-up"
                )},
            )
        _set_thread_status(thread_ts, "parked")
        return

    if text_lower in ("thread", "history"):
        try:
            convo = _format_conversation_for_slack(ticket_id, thread_data)
            footer = (
                "Reply `draft` for a suggested response\n"
                "Reply `send: [message]` to respond directly"
            )
            _reply(channel, thread_ts, f"{convo}\n{footer}")
        except Exception as e:
            _reply(
                channel, thread_ts,
                f"Could not fetch conversation: {e}",
            )
        return

    if text_lower == "draft":
        try:
            convo = _format_conversation_for_slack(ticket_id, thread_data)
            draft = _generate_draft(ticket_id)
            _store_pending_send(thread_ts, draft)
            draft_block = (
                f"{_CONV_SEP}\n"
                f"💬 *SUGGESTED RESPONSE — not sent yet*\n"
                f"{draft}\n"
                f"{_CONV_SEP}\n"
                f"Reply `confirm` to send this\n"
                f"Reply `send: [your version]` to send your own\n"
                f"Reply `cancel` to do nothing"
            )
            _reply(channel, thread_ts, f"{convo}\n{draft_block}")
        except Exception as e:
            _reply(channel, thread_ts, f"Draft generation failed: {e}")
        return

    if text_lower == "cancel":
        _pop_pending_send(thread_ts)
        if thread_data.get("status") == "on_prod_pending":
            # Mark as on_prod_cancelled so digest flags it
            data = _load_thread_map()
            if thread_ts in data:
                data[thread_ts]["status"] = "on_prod_cancelled"
                _save_thread_map(data)
            _reply(
                channel, thread_ts,
                "Held — will appear in tonight's digest"
                " as pending client notification.",
            )
        else:
            _reply(channel, thread_ts, "Cancelled — nothing sent to client.")
        return

    if text_lower == "move backlog":
        if clickup_task_id:
            if _cu_move_to_list(
                clickup_task_id, CLICKUP_ACCEPTED_BACKLOG_LIST
            ):
                _reply(channel, thread_ts, "✓ Moved to Accepted Backlog")
            else:
                _reply(
                    channel, thread_ts,
                    "Could not move task — check ClickUp manually",
                )
        else:
            _reply(
                channel, thread_ts,
                "No ClickUp task ID on record for this ticket",
            )
        return

    # -----------------------------------------------------------------------
    # SAFETY: confirm — fire the pending client message
    # -----------------------------------------------------------------------

    if text_lower == "confirm":
        pending = _pop_pending_send(thread_ts)
        if not pending:
            _reply(
                channel, thread_ts,
                "Nothing pending to confirm.\n"
                "To compose a client response: "
                "`reply: [your message]`",
            )
            return

        # Check close_after_send flag before clearing thread state
        close_after = thread_data.get("close_after_send", False)

        _send_client_reply(ticket_id, pending)
        _add_reaction(channel, thread_ts, "white_check_mark")
        _set_thread_status(thread_ts, "handled")

        # Clear close_after_send flag
        if close_after:
            data = _load_thread_map()
            if thread_ts in data:
                data[thread_ts]["close_after_send"] = False
                _save_thread_map(data)

        zoho_base = "https://desk.zoho.com/support/vomevolunteer"
        zoho_url = (
            f"{zoho_base}/ShowHomePage.do#Cases/dv/{ticket_id}"
        )
        is_on_prod = (
            thread_data.get("status") == "on_prod_pending"
        )

        if close_after:
            # Close Zoho ticket and finish ClickUp task
            _zoho_set_status(ticket_id, "Closed")
            if clickup_task_id:
                _cu_update_task(clickup_task_id, {"status": "FINISHED"})
            _reply(
                channel, thread_ts,
                "✓ Sent to client\n"
                "✓ Zoho ticket closed\n"
                "✓ ClickUp marked FINISHED",
            )
        elif clickup_task_id:
            if is_on_prod:
                from on_prod_handler import (
                    update_clickup_status_finished,
                )
                update_clickup_status_finished(clickup_task_id)
            else:
                _cu_update_task(
                    clickup_task_id, {"status": "acknowledged"}
                )
            if is_on_prod:
                _reply(
                    channel, thread_ts,
                    "✓ Sent to client\n"
                    "✓ ClickUp marked FINISHED",
                )
            else:
                _reply(
                    channel, thread_ts,
                    f"✓ Sent to client\nView in Zoho: {zoho_url}",
                )
        else:
            _reply(
                channel, thread_ts,
                f"✓ Sent to client\nView in Zoho: {zoho_url}",
            )
        return

    # -----------------------------------------------------------------------
    # SAFETY: explicit send prefix — reply: / send:
    # -----------------------------------------------------------------------

    explicit_send_match = re.match(
        r"^(?:reply|send)\s*:\s*(.+)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if explicit_send_match:
        client_msg = explicit_send_match.group(1).strip()
        # Still block if internal keywords slipped in
        if _has_internal_keyword(client_msg):
            _reply(
                channel, thread_ts,
                "That message contains internal keywords and cannot "
                "be sent to the client.\n"
                "Please remove references to team members, ClickUp, "
                "or internal commands.",
            )
            return
        _store_pending_send(thread_ts, client_msg)
        preview = (
            client_msg[:300] + "..."
            if len(client_msg) > 300
            else client_msg
        )
        _reply(
            channel, thread_ts,
            f"Ready to send to client:\n\"{preview}\"\n\n"
            "Reply `confirm` to send or `cancel` to discard.",
        )
        return

    # -----------------------------------------------------------------------
    # Inline re-draft: "saying something like: [client text]"
    # -----------------------------------------------------------------------

    rephrase_m = _REPHRASE_RE.search(text)
    if rephrase_m:
        client_msg = rephrase_m.group(1).strip()
        if _has_internal_keyword(client_msg):
            _reply(
                channel, thread_ts,
                "That message contains internal keywords and cannot "
                "be sent to the client.\n"
                "Please remove references to team members, ClickUp, "
                "or internal commands.",
            )
            return
        _store_pending_send(thread_ts, client_msg)
        preview = (
            client_msg[:300] + "..."
            if len(client_msg) > 300
            else client_msg
        )
        _reply(
            channel, thread_ts,
            f"Ready to send to client:\n\"{preview}\"\n\n"
            "Reply `confirm` to send or `cancel` to discard.",
        )
        return

    # -----------------------------------------------------------------------
    # Show ticket — Sam asking to see ticket content.
    # -----------------------------------------------------------------------

    if _SHOW_TICKET_RE.search(text):
        try:
            convo = _format_conversation_for_slack(ticket_id, thread_data)
            footer = (
                "Reply `draft` for a suggested response\n"
                "or `send: [your message]` to respond directly"
            )
            _reply(channel, thread_ts, f"{convo}\n{footer}")
        except Exception as e:
            _reply(
                channel, thread_ts,
                f"Could not fetch conversation: {e}",
            )
        return

    # -----------------------------------------------------------------------
    # Draft instruction — Sam describing what to tell the client.
    # This MUST come before command parsing so it never falls through
    # to "note added" behaviour.
    # -----------------------------------------------------------------------

    if _is_client_response_instruction(text):
        close_after = _has_close_instruction(text)
        try:
            draft = _generate_draft_from_instruction(ticket_id, text)
            _store_pending_send(thread_ts, draft, close_after=close_after)
            close_note = (
                "\nTicket will be closed after sending."
                if close_after else ""
            )
            _reply(
                channel, thread_ts,
                f"Here's a draft — not sent yet:{close_note}\n"
                f"{_CONV_SEP}\n"
                f"{draft}\n"
                f"{_CONV_SEP}\n"
                "Reply `confirm` to send\n"
                "Reply `send: [your version]` to edit\n"
                "Reply `cancel` to do nothing",
            )
        except Exception as e:
            _reply(channel, thread_ts, f"Draft generation failed: {e}")
        return

    # -----------------------------------------------------------------------
    # Explicit note: / add note: — ONLY these add to ClickUp notes.
    # -----------------------------------------------------------------------

    explicit_note_m = _EXPLICIT_NOTE_RE.match(text)
    if explicit_note_m and clickup_task_id:
        note_text = explicit_note_m.group(1).strip()
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _cu_update_task(
            clickup_task_id,
            {"description": (
                f"[Note from Sam — {date_str}]: {note_text}"
            )},
        )
        _reply(channel, thread_ts, "✓ Note added to ClickUp")
        return

    # -----------------------------------------------------------------------
    # Command parsing (exact keywords first, Claude NLP fallback)
    # -----------------------------------------------------------------------

    commands, remaining = _parse_commands(text)

    # NLP fallback when no exact commands detected
    if not commands:
        nl = _parse_with_claude(text)

        if nl.get("skip"):
            _add_reaction(channel, thread_ts, "double_vertical_bar")
            _set_thread_status(thread_ts, "parked")
            if clickup_task_id:
                date_str = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d"
                )
                _cu_update_task(
                    clickup_task_id,
                    {"description": (
                        f"Sam parked this on {date_str} — "
                        "pending follow-up"
                    )},
                )
            _reply(
                channel, thread_ts,
                "✓ Actions taken:\n"
                "→ Ticket parked — will appear in tonight's digest",
            )
            return

        if nl.get("assign_to"):
            commands["assign"] = nl["assign_to"]
        if nl.get("priority"):
            commands["priority"] = nl["priority"].lower()
        if nl.get("score") is not None:
            try:
                commands["score"] = str(int(nl["score"]))
            except (ValueError, TypeError):
                pass
        if nl.get("tier"):
            commands["tier"] = nl["tier"]
        if nl.get("arr") is not None:
            try:
                commands["arr"] = str(int(nl["arr"]))
            except (ValueError, TypeError):
                pass
        # NLP note and client_response are ignored — Sam must use
        # explicit "note [text]" syntax or the draft instruction flow.

    # Nothing actionable at all
    if not commands:
        _reply(channel, thread_ts, _NL_HELP)
        return

    # -----------------------------------------------------------------------
    # Execute commands, collecting action lines
    # -----------------------------------------------------------------------

    action_lines: list[str] = []

    if "priority" in commands and clickup_task_id:
        p = commands["priority"]
        cu_p = PRIORITY_MAP.get(p)
        if cu_p:
            _cu_update_task(clickup_task_id, {"priority": cu_p})
            action_lines.append(f"Priority set: {p.upper()}")

    if "assign" in commands:
        raw_name = commands["assign"].lower().strip()
        canonical = _ASSIGNEE_ALIASES.get(raw_name)
        if canonical:
            cu_user_id = _ASSIGNEE_IDS.get(canonical, "")
            zoho_agent_id = _ZOHO_AGENT_IDS.get(canonical, "")
            zoho_status = _ZOHO_STATUS[canonical]
            zoho_label = _ZOHO_AGENT_LABEL[canonical]
            cu_display = _ASSIGNEE_DISPLAY[canonical]

            if clickup_task_id and cu_user_id:
                _cu_update_task(
                    clickup_task_id,
                    {"assignees": {"add": [int(cu_user_id)]}},
                )

            if zoho_agent_id:
                _zoho_assign_ticket(ticket_id, zoho_agent_id)
                _zoho_set_status(ticket_id, zoho_status)

            if cu_user_id or zoho_agent_id:
                action_lines.append(
                    f"Assigned to: {zoho_label}\n"
                    f"  Zoho status: {zoho_status}\n"
                    f"  ClickUp assignee: {cu_display}"
                )
            else:
                action_lines.append(
                    f"Assign failed: {cu_display} IDs not configured"
                )
        else:
            action_lines.append(
                "Name not recognised — who did you mean?"
                " Options: Sam, OnlyG, Sanjay, Ron"
            )

    if "tier" in commands and clickup_task_id:
        _cu_set_field(
            clickup_task_id, FIELD_HIGHEST_TIER, commands["tier"]
        )
        action_lines.append(f"Tier updated: {commands['tier']}")

    if "arr" in commands and clickup_task_id:
        try:
            arr_val = int(commands["arr"])
            _cu_set_field(
                clickup_task_id, FIELD_COMBINED_ARR, arr_val
            )
            action_lines.append(f"ARR set: ${arr_val:,}")
        except ValueError:
            action_lines.append("ARR — invalid number")

    if "score" in commands and clickup_task_id:
        try:
            score_val = int(commands["score"])
            _cu_set_field(
                clickup_task_id, FIELD_AUTO_SCORE, score_val
            )
            action_lines.append(f"Auto Score updated: {score_val}")
        except ValueError:
            pass

    # -----------------------------------------------------------------------
    # Confirmation — always post, never auto-send to client
    # -----------------------------------------------------------------------

    if action_lines:
        _reply(channel, thread_ts, _build_confirmation(action_lines))
    else:
        _reply(channel, thread_ts, _NL_HELP)
