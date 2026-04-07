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
    _zoho_desk_call,
    fetch_crm_account,
    fetch_ticket_conversations,
    fetch_ticket_from_zoho,
)
from clickup_tasks import (
    _map_type_option,
    _map_platform_option,
    _map_module_option,
    FIELD_TYPE,
    FIELD_PLATFORM,
    FIELD_MODULE,
    FIELD_SOURCE,
    FIELD_HIGHEST_TIER,
    FIELD_REQUESTING_CLIENTS,
    FIELD_COMBINED_ARR,
    FIELD_AUTO_SCORE,
    FIELD_ZOHO_TICKET_LINK,
    SOURCE_ZOHO_TICKET,
)
from database import (
    get_thread,
    is_event_processed,
    mark_event_processed,
    update_thread,
)

# ---------------------------------------------------------------------------
# Clients & config
# ---------------------------------------------------------------------------

_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
_anthropic = anthropic.Anthropic()

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"
CLICKUP_LIST_PRIORITY_QUEUE = "901113386257"
CLICKUP_ACCEPTED_BACKLOG_LIST = "901113389889"
CLICKUP_LIST_RAW_INTAKE = "901113386484"
CLICKUP_LIST_SLEEPING = "901113389897"

SLACK_CHANNEL_ENGINEERING = os.environ.get(
    "SLACK_CHANNEL_VOME_SUPPORT_ENGINEERING", ""
)

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
    "onlyg":  "Pending Developer Fix",
    "sanjay": "Pending Developer Fix",
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
# Used for exact dict lookup first, then substring matching as fallback.
_ASSIGNEE_ALIASES: dict[str, str] = {
    # Sam
    "sam":      "sam",
    "saul":     "sam",
    "me":       "sam",
    "myself":   "sam",
    # OnlyG
    "onlyg":    "onlyg",
    "only g":   "onlyg",
    "only-g":   "onlyg",
    "backend":  "onlyg",
    "back end": "onlyg",
    "back-end": "onlyg",
    "g":        "onlyg",
    # Sanjay
    "sanjay":   "sanjay",
    "san":      "sanjay",
    "frontend": "sanjay",
    "front end": "sanjay",
    "front-end": "sanjay",
    # Ron
    "ron":      "ron",
    "sales":    "ron",
}

# Phrase patterns that map to Sam (checked via substring)
_SAM_PHRASES = [
    "i'll take it", "i will take it",
    "i'll handle it", "i will handle it",
    "assign to me", "take it myself",
]


def _resolve_assignee(raw: str) -> str | None:
    """Resolve a raw name/phrase to a canonical assignee key.

    Tries exact match first, then substring matching against aliases
    and Sam phrases.
    """
    low = raw.lower().strip()

    # 1. Exact match
    if low in _ASSIGNEE_ALIASES:
        return _ASSIGNEE_ALIASES[low]

    # 2. Sam phrase match
    for phrase in _SAM_PHRASES:
        if phrase in low:
            return "sam"

    # 3. Substring match — check if any alias appears in the input
    # Sort by length descending so longer matches win (e.g. "only g" before "g")
    for alias in sorted(_ASSIGNEE_ALIASES, key=len, reverse=True):
        # For single-char aliases like "g", require word boundary
        if len(alias) <= 2:
            if re.search(rf"\b{re.escape(alias)}\b", low):
                return _ASSIGNEE_ALIASES[alias]
        elif alias in low:
            return _ASSIGNEE_ALIASES[alias]

    return None

# Keep for any legacy references
ASSIGNEE_MAP = _ASSIGNEE_IDS

# Custom field IDs imported from clickup_tasks:
# FIELD_HIGHEST_TIER, FIELD_COMBINED_ARR, FIELD_AUTO_SCORE

# Standard signature block
_SIGNATURE = "Best,\n\nSam | Vome support\nsupport.vomevolunteer.com"

# Patterns that mean Sam wants the previous draft back
_RESTORE_DRAFT_RE = re.compile(
    r"\b(?:"
    r"you had it before|go back to that|use that one"
    r"|the previous version|previous draft|restore"
    r"|that was better|go back|the one before"
    r"|last draft|original draft|earlier draft"
    r"|what did you have|what was the draft"
    r")\b",
    re.IGNORECASE,
)

# Patterns that mean Sam wants greeting + signature wrapped around text
_WRAP_RE = re.compile(
    r"\b(?:"
    r"keep hi|add hi|add greeting|add signature|add sig"
    r"|wrap it|add the greeting|keep the greeting"
    r"|just add hi|add hi \w+ \+ sig"
    r"|keep .+ \+ signature|add .+ \+ sig"
    r")\b",
    re.IGNORECASE,
)

# Words that signal an internal instruction — never send to client
_INTERNAL_KEYWORDS = {
    "assign", "clickup", "priority", "score",
    "p1", "p2", "p3", "tier", "arr", "onlyg", "sanjay",
    "sam", "note", "skip", "move", "backlog", "draft",
    "route", "fix",
}

# Phrases that indicate a test / placeholder / garbage message that should
# never be emailed to a real client.
_JUNK_SEND_PATTERNS = [
    r"\btest\s*(reply|email|message|response)\b",
    r"\bplease\s+ignore\b",
    r"\bignore\s+this\b",
    r"\btest\s*$",                    # message that is just "test"
    r"^\s*test\s*$",                  # standalone "test"
    r"\bdo\s+not\s+send\b",
    r"\bdon'?t\s+send\b",
    r"\blorem\s+ipsum\b",
    r"\basdf\b",
    r"\bplaceholder\b",
    r"\btesting\s+1\s*2\s*3\b",
    r"\bfoo\s*bar\b",
    r"\bhello\s+world\b",
]
_JUNK_SEND_RE = re.compile(
    "|".join(_JUNK_SEND_PATTERNS), re.IGNORECASE
)


def _is_junk_content(text: str) -> str | None:
    """Return a reason string if *text* looks like test/junk, else None."""
    stripped = text.strip()
    # Very short messages are suspicious
    if len(stripped) < 10:
        return "Message is too short to be a real client reply"
    m = _JUNK_SEND_RE.search(stripped)
    if m:
        return f"Message matches blocked pattern: '{m.group()}'"
    return None

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
# Orphaned thread recovery
# ---------------------------------------------------------------------------

def _recover_orphaned_thread(
    channel: str, thread_ts: str
) -> dict | None:
    """Try to recover a thread that isn't in the DB.

    Reads the parent message, extracts a Zoho ticket ID from
    the text, finds the original thread data by ticket ID, and
    re-saves the thread mapping with the new thread_ts so future
    commands work.
    """
    try:
        resp = _slack.conversations_replies(
            channel=channel, ts=thread_ts, limit=1,
        )
        parent_msgs = resp.get("messages", [])
        if not parent_msgs:
            return None
        parent_text = parent_msgs[0].get("text", "")

        # Extract ticket ID from Zoho URL in parent message
        m = re.search(r"/dv/(\d+)", parent_text)
        if not m:
            return None
        ticket_id = m.group(1)

        # Find the original thread data by ticket ID
        from database import (
            get_thread_by_ticket_id, save_thread,
        )
        info = get_thread_by_ticket_id(ticket_id)
        if not info:
            return None
        _, original_data = info

        # Re-save under the new thread_ts so it works going
        # forward (preserves pending_send, classification, etc.)
        save_thread(
            thread_ts=thread_ts,
            ticket_id=ticket_id,
            ticket_number=original_data.get(
                "ticket_number", ""
            ),
            subject=original_data.get("subject", ""),
            channel=channel,
            clickup_task_id=original_data.get(
                "clickup_task_id"
            ),
            classification=original_data.get(
                "classification"
            ),
            crm=original_data.get("crm"),
        )
        # Copy over pending_send if it exists on original
        pending = original_data.get("pending_send")
        if pending:
            update_thread(thread_ts, pending_send=pending)

        print(
            f"[RECOVER] Orphaned thread {thread_ts}"
            f" linked to ticket {ticket_id}"
        )
        return get_thread(thread_ts)
    except Exception as e:
        print(f"[RECOVER] Failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Thread map status
# ---------------------------------------------------------------------------

def _set_thread_status(thread_ts: str, status: str):
    update_thread(thread_ts, status=status)


def _store_pending_send(
    thread_ts: str, message: str, close_after: bool = False
):
    """Save a client message that needs Sam's `confirm` before sending."""
    update_thread(
        thread_ts,
        pending_send=message,
        pending_draft=message,  # Always keep a copy for recall
        close_after_send="true" if close_after else "false",
    )


def _pop_pending_send(thread_ts: str) -> str | None:
    """Return and clear the pending client message, or None if absent."""
    entry = get_thread(thread_ts)
    if not entry:
        return None
    msg = entry.get("pending_send")
    if msg is not None:
        update_thread(thread_ts, pending_send=None)
    return msg


def _get_pending_draft(thread_ts: str) -> str | None:
    """Retrieve the last draft shown to Sam (never cleared)."""
    entry = get_thread(thread_ts)
    if not entry:
        return None
    return entry.get("pending_draft")


def _wrap_with_greeting_sig(
    body: str, contact_name: str = ""
) -> str:
    """Wrap Sam's exact text with Hi [name] greeting and signature.

    Does NOT rewrite or change any of Sam's words.
    """
    first_name = contact_name.split()[0] if contact_name else "there"
    return f"Hi {first_name},\n\n{body}\n\n{_SIGNATURE}"


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
    "ZOHO_FROM_ADDRESS", "support@vomevolunteer.zohodesk.com"
)


def _send_client_reply(
    ticket_id: str, content: str, to_email: str = "",
    cc_email: str = "",
) -> bool:
    """Send an email reply to the client via ZohoDesk_sendReply.

    This actually emails the client — do NOT use for internal notes.
    Internal notes use ZohoDesk_createTicketComment with isPublic=False.
    """
    # Last-resort guard: never send junk/test content to a client
    junk_reason = _is_junk_content(content)
    if junk_reason:
        print(
            f"[SEND] BLOCKED — Zoho ticket {ticket_id}: {junk_reason}"
        )
        return False

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

    print(
        f"[SEND] Calling ZohoDesk_sendReply on ticket {ticket_id} "
        f"from={ZOHO_FROM_ADDRESS} to={to_email or '(ticket default)'}"
        f"{f' cc={cc_email}' if cc_email else ''}"
    )

    result = _zoho_desk_call("ZohoDesk_sendReply", {
        "body": body,
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })

    if not result:
        print(f"[SEND] FAILED — Zoho ticket {ticket_id}, no result")
        return False

    # Check for MCP-level isError flag (Zoho returns errors as content)
    if isinstance(result, dict) and result.get("isError"):
        print(f"[SEND] FAILED — Zoho ticket {ticket_id}: {result}")
        return False

    # Also check unwrapped content for Zoho error codes
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"[SEND] FAILED — Zoho ticket {ticket_id}: {data}")
        return False

    print(f"[SEND] Success — Zoho ticket {ticket_id}")
    return True


# ---------------------------------------------------------------------------
# Zoho ticket assignment helpers
# ---------------------------------------------------------------------------

def _zoho_assign_ticket(ticket_id: str, agent_id: str) -> bool:
    """Reassign a Zoho Desk ticket to the given agent ID."""
    result = _zoho_desk_call("ZohoDesk_updateTicket", {
        "body": {"assigneeId": str(agent_id)},
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(f"Zoho assign failed — ticket {ticket_id}, no result")
        return False

    if isinstance(result, dict) and result.get("isError"):
        print(f"Zoho assign failed — ticket {ticket_id}: {result}")
        return False

    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"Zoho assign failed — ticket {ticket_id}: {data}")
        return False

    print(f"Zoho ticket {ticket_id} assigned to agent {agent_id}")
    return True


def _zoho_set_status(ticket_id: str, status: str) -> bool:
    """Update the status of a Zoho Desk ticket."""
    result = _zoho_desk_call("ZohoDesk_updateTicket", {
        "body": {"status": status},
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(f"Zoho status update failed — ticket {ticket_id}, no result")
        return False

    if isinstance(result, dict) and result.get("isError"):
        print(f"Zoho status update failed — ticket {ticket_id}: {result}")
        return False

    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"Zoho status update failed — ticket {ticket_id}: {data}")
        return False

    print(f"Zoho ticket {ticket_id} status → {status}")
    return True


# ---------------------------------------------------------------------------
# Thread map helpers
# ---------------------------------------------------------------------------

def _update_thread_clickup_id(thread_ts: str, task_id: str):
    """Persist a newly created ClickUp task ID into the database."""
    update_thread(thread_ts, clickup_task_id=task_id)


# ---------------------------------------------------------------------------
# ClickUp task creation from stored thread classification
# ---------------------------------------------------------------------------

def _create_task_from_thread(
    thread_data: dict,
    list_id: str,
    assignee_cu_id: str | None = None,
    priority_override: str | None = None,
    thread_analysis: dict | None = None,
) -> dict | None:
    """Create a ClickUp task using thread analysis + stored classification.

    When thread_analysis is provided (from _analyze_thread_for_task), its
    richer fields override the sparse stored classification.

    Returns {"task_id": str, "task_url": str, "analysis": dict} or None.
    """
    if not CLICKUP_API_TOKEN:
        print("_create_task_from_thread: CLICKUP_API_TOKEN not set")
        return None

    ta = thread_analysis or {}
    classification = thread_data.get("classification", {})
    crm = thread_data.get("crm", {})
    ticket_id = thread_data.get("ticket_id", "")
    ticket_number = thread_data.get("ticket_number", "")
    subject = thread_data.get("subject", "")

    zoho_url = (
        "https://desk.zoho.com/support/vomevolunteer"
        f"/ShowHomePage.do#Cases/dv/{ticket_id}"
    )

    # Priority: explicit override > thread analysis > stored classification
    priority_str = (
        priority_override
        or (ta.get("priority") or "").upper()
        or classification.get("priority", "P3")
    ).upper().strip()
    if not re.match(r"^P[123]$", priority_str):
        priority_str = "P3"
    cu_priority = PRIORITY_MAP.get(priority_str.lower(), 3)

    # Title: use thread analysis title if available, else fallback
    account = (
        crm.get("account_name")
        or ("Volunteer" if not crm.get("found") else "Unknown")
    )
    if ta.get("title"):
        # Thread-derived title: prepend account, append priority
        ta_title = ta["title"]
        if len(ta_title) > 65:
            ta_title = ta_title[:62] + "..."
        title = f"{account} — {ta_title} — {priority_str}"
    else:
        subj = subject.strip()
        if len(subj) > 65:
            subj = subj[:62] + "..."
        title = f"{account} — {subj} — {priority_str}"

    # Description: thread analysis provides richer context
    tier = crm.get("tier") or "Unknown"
    arr_raw = crm.get("arr")
    if arr_raw:
        try:
            arr_str = f"${int(float(arr_raw)):,}"
        except (ValueError, TypeError):
            arr_str = f"${arr_raw}"
    else:
        arr_str = "Unknown"

    desc_lines = [
        f"Account: {account} | Tier: {tier} | ARR: {arr_str}",
        f"Zoho ticket: #{ticket_number}",
        f"Zoho link: {zoho_url}",
        "",
    ]
    # Thread analysis description is the richest source
    if ta.get("description"):
        desc_lines.append(ta["description"])
        desc_lines.append("")
    else:
        issue_summary = classification.get("issue_summary", "")
        if issue_summary:
            desc_lines.append(issue_summary)
            desc_lines.append("")

    if ta.get("affected_clients"):
        desc_lines.append(f"Affected clients: {ta['affected_clients']}")

    # Use thread analysis fields, falling back to stored classification
    cl_type = ta.get("type") or classification.get("type", "")
    cl_module = ta.get("module") or classification.get("module", "")
    cl_platform = ta.get("platform") or classification.get("platform", "")
    if cl_type:
        desc_lines.append(f"Classification: {cl_type}")
    if cl_module:
        desc_lines.append(f"Module: {cl_module}")
    if cl_platform:
        desc_lines.append(f"Platform: {cl_platform}")
    description = "\n".join(desc_lines)

    # Custom fields — use thread-derived fields with fallbacks
    custom_fields = []
    if cl_type:
        custom_fields.append(
            {"id": FIELD_TYPE, "value": _map_type_option(cl_type)}
        )
    platform_opt = _map_platform_option(cl_platform)
    if platform_opt:
        custom_fields.append(
            {"id": FIELD_PLATFORM, "value": platform_opt}
        )
    if cl_module:
        mod_opt = _map_module_option(cl_module)
        if mod_opt:
            custom_fields.append(
                {"id": FIELD_MODULE, "value": mod_opt}
            )
    custom_fields.append(
        {"id": FIELD_SOURCE, "value": SOURCE_ZOHO_TICKET}
    )
    custom_fields.append(
        {"id": FIELD_ZOHO_TICKET_LINK, "value": zoho_url}
    )
    if crm.get("tier") and crm["tier"] != "Unknown":
        custom_fields.append(
            {"id": FIELD_HIGHEST_TIER, "value": crm["tier"]}
        )
    arr_value = None
    if arr_raw:
        try:
            arr_value = int(float(arr_raw))
        except (ValueError, TypeError):
            pass
    if crm.get("found") and crm.get("account_name"):
        clients_str = (
            f"{crm['account_name']} ({tier},"
            f" ${arr_value:,})" if arr_value
            else f"{crm['account_name']} ({tier}, unknown ARR)"
        )
        custom_fields.append(
            {"id": FIELD_REQUESTING_CLIENTS, "value": clients_str}
        )
    if arr_value is not None:
        custom_fields.append(
            {"id": FIELD_COMBINED_ARR, "value": arr_value}
        )
    auto_score = classification.get("auto_score")
    if auto_score:
        try:
            custom_fields.append(
                {"id": FIELD_AUTO_SCORE, "value": int(auto_score)}
            )
        except (ValueError, TypeError):
            pass

    payload: dict = {
        "name": title,
        "description": description,
        "priority": cu_priority,
        "status": "QUEUED",
        "custom_fields": custom_fields,
    }
    if assignee_cu_id:
        payload["assignees"] = [int(assignee_cu_id)]

    try:
        r = httpx.post(
            f"{CLICKUP_BASE}/list/{list_id}/task",
            json=payload,
            headers=_cu_headers(),
            timeout=20,
        )
        r.raise_for_status()
        task = r.json()
        task_id = task.get("id", "")
        task_url = (
            task.get("url")
            or f"https://app.clickup.com/t/{task_id}"
        )
        print(f"ClickUp task created: {title} (ID: {task_id})")
        return {"task_id": task_id, "task_url": task_url, "analysis": ta}
    except Exception as e:
        print(f"ClickUp task creation failed: {e}")
        return None


def _smart_create_task(
    thread_data: dict,
    list_id: str,
    channel: str,
    thread_ts: str,
    assignee_cu_id: str | None = None,
    priority_override: str | None = None,
) -> dict | None:
    """Create a ClickUp task with full thread context analysis.

    Fetches all thread messages, runs Claude analysis for smart labeling,
    creates the task, uploads any thread attachments, and prompts for
    missing fields.

    Returns {"task_id": str, "task_url": str, "analysis": dict} or None.
    """
    # 1. Fetch full thread context
    thread_context = _fetch_thread_context(channel, thread_ts)

    # 2. Analyze with Claude for smart labeling
    analysis = _analyze_thread_for_task(thread_context, thread_data)

    # 3. Create the task with enriched data
    result = _create_task_from_thread(
        thread_data,
        list_id,
        assignee_cu_id=assignee_cu_id,
        priority_override=priority_override,
        thread_analysis=analysis,
    )
    if not result:
        return None

    task_id = result["task_id"]
    task_url = result["task_url"]

    # 4. Upload all thread attachments to the new task
    all_files = thread_context.get("all_files", [])
    uploaded = 0
    for f in all_files:
        content = _download_slack_file(f["url"])
        if content and _cu_upload_attachment(task_id, f["name"], content):
            uploaded += 1
    if uploaded:
        noun = "attachment" if uploaded == 1 else "attachments"
        print(f"[TASK] {uploaded} {noun} uploaded to ClickUp task {task_id}")

    # 5. Prompt for missing fields if any
    _prompt_missing_fields(channel, thread_ts, analysis, task_url)

    return result


# ---------------------------------------------------------------------------
# Engineering channel notification
# ---------------------------------------------------------------------------

def _notify_engineering(
    engineer_name: str,
    ticket_number: str,
    subject: str,
    account_name: str,
    priority: str,
    module: str,
    issue_summary: str,
    task_url: str,
    zoho_url: str,
):
    """Post assignment notification to #vome-support-engineering."""
    if not SLACK_CHANNEL_ENGINEERING:
        print(
            "_notify_engineering: SLACK_CHANNEL_VOME_SUPPORT_ENGINEERING not set"
        )
        return
    msg = (
        f"\U0001f527 *New task assigned — {engineer_name}*\n"
        f"#{ticket_number} — {subject}\n"
        f"Client: {account_name}\n"
        f"Priority: {priority} | Module: {module}\n"
        f"Issue: {issue_summary}\n"
        f"ClickUp: {task_url}\n"
        f"Zoho: {zoho_url}"
    )
    try:
        _slack.chat_postMessage(
            channel=SLACK_CHANNEL_ENGINEERING, text=msg
        )
    except SlackApiError as e:
        print(
            "Engineering notification failed:"
            f" {e.response['error']}"
        )


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
        "Sign off: Best,\n\nSam | Vome support\nsupport.vomevolunteer.com"
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
# Thread context fetching (for smart ClickUp task creation/updates)
# ---------------------------------------------------------------------------

def _fetch_thread_context(
    channel: str, thread_ts: str
) -> dict:
    """Fetch all messages and files from a Slack thread.

    Returns {
        "messages": [{"user": str, "text": str, "ts": str, "files": [...]}],
        "all_files": [{"url": str, "name": str, "mimetype": str}],
        "full_text": str,  # concatenated thread text for Claude
    }
    """
    messages: list[dict] = []
    all_files: list[dict] = []
    text_parts: list[str] = []
    try:
        resp = _slack.conversations_replies(
            channel=channel, ts=thread_ts, limit=50,
        )
        for m in resp.get("messages", []):
            msg = {
                "user": m.get("user", "unknown"),
                "text": m.get("text", ""),
                "ts": m.get("ts", ""),
                "files": m.get("files", []),
            }
            messages.append(msg)
            if msg["text"]:
                text_parts.append(msg["text"])
            for f in msg["files"]:
                url = f.get("url_private_download") or f.get("url_private")
                if url:
                    all_files.append({
                        "url": url,
                        "name": f.get("name", "attachment"),
                        "mimetype": f.get("mimetype", ""),
                    })
    except SlackApiError as e:
        print(f"[THREAD] Fetch failed: {e.response['error']}")
    return {
        "messages": messages,
        "all_files": all_files,
        "full_text": "\n---\n".join(text_parts),
    }


def _analyze_thread_for_task(
    thread_context: dict, thread_data: dict
) -> dict:
    """Use Claude to analyze full thread context and derive ClickUp task fields.

    Returns a dict with: title, description, type, platform, module,
    priority, affected_clients, missing_fields (list of questions to ask).
    """
    full_text = thread_context.get("full_text", "")
    n_files = len(thread_context.get("all_files", []))

    # Include any existing classification/CRM as hints
    classification = thread_data.get("classification", {})
    crm = thread_data.get("crm", {})
    subject = thread_data.get("subject", "")

    hint_block = ""
    if classification or crm:
        hint_parts = []
        if subject:
            hint_parts.append(f"Ticket subject: {subject}")
        if classification.get("type"):
            cl_type = classification["type"]
            hint_parts.append(
                f"Existing classification: {cl_type}"
            )
        if classification.get("module"):
            hint_parts.append(
                f"Module: {classification['module']}"
            )
        if classification.get("platform"):
            hint_parts.append(
                f"Platform: {classification['platform']}"
            )
        if classification.get("issue_summary"):
            hint_parts.append(
                f"Issue summary: "
                f"{classification['issue_summary']}"
            )
        if crm.get("account_name"):
            hint_parts.append(f"Account: {crm['account_name']}")
        if crm.get("tier"):
            hint_parts.append(f"Tier: {crm['tier']}")
        if hint_parts:
            hint_block = (
                "\nExisting ticket data (use as context, but the thread "
                "may have evolved):\n" + "\n".join(hint_parts) + "\n"
            )

    file_note = ""
    if n_files:
        file_note = (
            f"\n{n_files} screenshot(s)/attachment(s)"
            " were shared in the thread."
        )

    prompt = (
        "Analyze this Slack thread and extract structured"
        " data for a ClickUp task.\n\n"
        f"Thread messages:\n{full_text}\n"
        f"{file_note}"
        f"{hint_block}\n"
        "Return valid JSON only — no prose, no code block"
        " markers.\n"
        "{\n"
        '  "title": "concise task title (under 80 chars,'
        ' describe the issue/request)",\n'
        '  "description": "detailed description including'
        ' steps, context, affected areas",\n'
        '  "type": "bug" or "feature" or "improvement"'
        ' or "ux" or "investigation" or null,\n'
        '  "platform": "web" or "mobile" or "both"'
        ' or null,\n'
        '  "module": one of ["volunteer homepage",'
        ' "reserve schedule", "opportunities",'
        ' "sequences", "forms", "admin dashboard",'
        ' "admin scheduling", "admin settings",'
        ' "admin permissions", "sites", "groups",'
        ' "categories", "hour tracking", "kiosk",'
        ' "email communications", "chat", "reports",'
        ' "kpi dashboards", "integrations",'
        ' "access / authentication", "other"]'
        ' or null,\n'
        '  "priority": "p1" or "p2" or "p3" or null,\n'
        '  "affected_clients": "specific client names'
        ' mentioned" or null,\n'
        '  "missing_fields": ["list of questions to ask'
        " for fields that could not be determined from"
        " context. Only genuinely unknown fields that"
        ' would help document the task."]\n'
        "}\n\n"
        "Rules:\n"
        "- Infer type from context: crashes/errors = bug,"
        " new capability = feature, existing flow"
        " improvement = improvement, confusing UX = ux,"
        " unclear root cause = investigation\n"
        "- Infer platform from screenshots or keywords"
        " (app/mobile/iOS/Android = mobile,"
        " browser/dashboard/admin = web)\n"
        "- Infer module from the feature area discussed\n"
        "- title: action-oriented, e.g."
        " 'Fix X when Y', 'Add X to Y'\n"
        "- description: thorough, all relevant details\n"
        "- missing_fields: only genuinely missing info."
        " If screenshots shared, don't ask for them."
        " If client named, don't ask which client."
        " Max 2-3 questions for truly unknown info."
    )
    try:
        response = _anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        parsed = json.loads(raw)
        print(f"[THREAD] Task analysis: {parsed}")
        return parsed
    except Exception as e:
        print(f"[THREAD] Task analysis failed: {e}")
        return {}


def _prompt_missing_fields(
    channel: str, thread_ts: str, analysis: dict, task_url: str
) -> None:
    """Post follow-up questions for fields that couldn't be inferred."""
    missing = analysis.get("missing_fields", [])
    if not missing:
        return
    # Limit to top 3 most useful questions
    questions = missing[:3]
    lines = [f"Task created: {task_url}", ""]
    lines.append("A few details that would help document this properly:")
    for i, q in enumerate(questions, 1):
        lines.append(f"{i}. {q}")
    lines.append("")
    lines.append("Reply here and I'll update the ClickUp task.")
    _reply(channel, thread_ts, "\n".join(lines))


def _auto_update_clickup_from_thread(
    clickup_task_id: str,
    text: str,
    files: list,
    channel: str,
    thread_ts: str,
    thread_data: dict,
) -> bool:
    """Append new thread context to an existing ClickUp task.

    Called when a non-command message arrives in a thread that already
    has a linked ClickUp task. Uses Claude to determine if the message
    contains substantive new context worth appending.

    Returns True if the task was updated.
    """
    if not clickup_task_id or (not text and not files):
        return False

    # Use Claude to decide if this message has substantive context
    prompt = (
        "A user sent a new message in a support thread that has a linked "
        "ClickUp task. Decide if this message contains new information "
        "that should be appended to the task.\n\n"
        f"Message: \"{text}\"\n"
        f"Attachments: {len(files)} file(s)\n\n"
        "Return valid JSON only:\n"
        "{\n"
        '  "has_context": true/false,\n'
        '  "summary": "one-line summary of the new info to append" or null,\n'
        '  "update_fields": {\n'
        '    "type": "bug/feature/improvement/ux/investigation" or null,\n'
        '    "platform": "web/mobile/both" or null,\n'
        '    "module": "module name" or null\n'
        '  }\n'
        "}\n\n"
        "Rules:\n"
        "- has_context=true if the message provides: steps to reproduce, "
        "affected clients, screenshots context, platform info, clarification, "
        "error details, or answers to earlier questions\n"
        "- has_context=false if: it's just a command (assign, move, draft, "
        "confirm, etc.), a thank you, or a short acknowledgment\n"
        "- summary: brief but specific, e.g. 'Confirmed this affects "
        "iOS and Android, client Habitat for Humanity'\n"
        "- update_fields: only fill in fields the message clarifies"
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
    except Exception as e:
        print(f"[UPDATE] Context check failed: {e}")
        return False

    if not parsed.get("has_context"):
        return False

    updated = False
    summary = parsed.get("summary", "")

    # Append new context to task description
    if summary:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        note = f"\n\n[Update — {date_str}]: {summary}"
        # Fetch current description and append
        try:
            r = httpx.get(
                f"{CLICKUP_BASE}/task/{clickup_task_id}",
                headers=_cu_headers(),
                timeout=15,
            )
            r.raise_for_status()
            current_desc = r.json().get("description", "")
            _cu_update_task(
                clickup_task_id,
                {"description": current_desc + note},
            )
            updated = True
            print(
                "[UPDATE] Appended context to"
                f" ClickUp task {clickup_task_id}"
            )
        except Exception as e:
            print(f"[UPDATE] Failed to append context: {e}")

    # Update custom fields if the message clarified them
    update_fields = parsed.get("update_fields", {})
    if update_fields.get("type"):
        opt = _map_type_option(update_fields["type"])
        if opt:
            _cu_set_field(clickup_task_id, FIELD_TYPE, opt)
            updated = True
    if update_fields.get("platform"):
        opt = _map_platform_option(update_fields["platform"])
        if opt:
            _cu_set_field(clickup_task_id, FIELD_PLATFORM, opt)
            updated = True
    if update_fields.get("module"):
        opt = _map_module_option(update_fields["module"])
        if opt:
            _cu_set_field(clickup_task_id, FIELD_MODULE, opt)
            updated = True

    # Upload any new attachments
    for f in files:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        filename = f.get("name") or "attachment"
        content = _download_slack_file(url)
        if content and _cu_upload_attachment(clickup_task_id, filename, content):
            updated = True

    if updated:
        _add_reaction(channel, thread_ts, "memo")

    return updated


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

    # assign [name] — capture one or two words to handle "Only G"
    m = re.search(
        r"\bassign\s+(?:to\s+)?(\w+(?:\s+\w)?)", remaining, re.IGNORECASE
    )
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
    "Reply in plain English to take action."
)


# Map task_list values to ClickUp list IDs
_TASK_LIST_MAP = {
    "priority_queue": CLICKUP_LIST_PRIORITY_QUEUE,
    "raw_intake": CLICKUP_LIST_RAW_INTAKE,
    "accepted_backlog": CLICKUP_ACCEPTED_BACKLOG_LIST,
    "sleeping": CLICKUP_LIST_SLEEPING,
}


def _parse_with_claude(text: str, thread_data: dict) -> dict:
    """
    Call Claude to interpret Sam's full natural-language reply.
    Returns a rich dict of ALL intended actions, or {} on failure.
    """
    ticket_number = thread_data.get("ticket_number", "?")
    classification = thread_data.get("classification", {})
    crm = thread_data.get("crm", {})

    context_block = (
        f"Classification: {classification.get('type', 'Unknown')}\n"
        f"Module: {classification.get('module', 'Unknown')}\n"
        f"Contact: {crm.get('account_name', 'Unknown')}\n"
        f"Issue: {classification.get('issue_summary', 'Unknown')}"
    )

    prompt = (
        f"Sam sent this reply about ticket #{ticket_number}:\n"
        f"'{text}'\n\n"
        f"The ticket context is:\n{context_block}\n\n"
        "Extract ALL intended actions. "
        "Return valid JSON only — no prose, no code block markers.\n"
        "{\n"
        '  "generate_draft": true/false,\n'
        '  "draft_instruction": "what to say to client '
        'if generate_draft is true",\n'
        '  "assign_to": null or "Sam/OnlyG/Sanjay/Ron",\n'
        '  "priority": null or "p1/p2/p3",\n'
        '  "auto_score": null or number,\n'
        '  "tier": null or string,\n'
        '  "arr": null or number,\n'
        '  "create_task": true/false,\n'
        '  "task_list": "priority_queue" or "raw_intake" '
        'or "accepted_backlog" or "sleeping",\n'
        '  "close_ticket": false or true,\n'
        '  "skip": false or true,\n'
        '  "client_response": null or string,\n'
        '  "verbatim_text": null or string,\n'
        '  "wrap_with_greeting": false or true,\n'
        '  "restore_draft": false or true,\n'
        '  "needs_clarification": false or true,\n'
        '  "clarification_question": null or string\n'
        "}\n\n"
        "Rules for extraction:\n"
        "- If message mentions drafting, writing, sending, "
        "responding to client -> generate_draft: true, and put "
        "the gist of what Sam wants to say in draft_instruction\n"
        "- If Sam provides EXACT text he wants sent to the client "
        "(e.g. 'for the email say...' or 'tell them:' followed by "
        "the response body), put that exact text in verbatim_text. "
        "Do NOT put it in client_response or draft_instruction.\n"
        "- If Sam says to add greeting, add Hi, add signature, "
        "wrap it, keep Hi + sig -> wrap_with_greeting: true\n"
        "- If Sam says go back, previous draft, restore, "
        "use that one, you had it before -> restore_draft: true\n"
        "- If message mentions assigning, routing, OnlyG, Sanjay, "
        "backend, frontend -> extract assign_to\n"
        "- If message mentions a number in context of score/"
        "priority -> extract auto_score\n"
        "- If message says p1/p2/p3 or high/medium/low priority "
        "-> extract priority (map high=p1, medium=p2, low=p3)\n"
        "- If message is ambiguous and you cannot determine intent "
        "-> needs_clarification: true, ask ONE specific question\n"
        "- create_task: true ONLY when assign_to is set OR task_list "
        "is specified OR priority is set standalone. "
        "Do NOT set create_task to true for draft-only or close-only requests.\n"
        "- client_response: only if Sam provides exact text to send "
        "verbatim (e.g. after 'send:' or 'reply:'). Otherwise null.\n"
        "- close_ticket: true only if Sam explicitly says to close "
        "the ticket\n"
        "- skip: true only if Sam explicitly says to skip/park this"
    )
    try:
        response = _anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
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


def _build_confirmation(
    action_lines: list[str],
    draft: str | None = None,
    close_after: bool = False,
) -> str:
    """Build the combined confirmation message after commands run."""
    lines = ["✓ Actions taken:"]
    for al in action_lines:
        lines.append(f"→ {al}")

    if draft:
        lines.append("")
        lines.append("Draft ready — not sent yet:")
        lines.append("─────────────────────────────────────")
        lines.append(draft)
        lines.append("─────────────────────────────────────")
        if close_after:
            lines.append("Ticket will be closed after sending.")
        lines.append("Reply `confirm` to send")
        lines.append("Reply `cancel` to hold")
    else:
        lines.append("")
        lines.append("Reply in plain English to take action.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def handle_reply(event: dict):
    """
    Process a Slack message event from a #vome-tickets thread.

    event keys: user, text, thread_ts, channel, files (optional list),
                client_msg_id (optional — used for dedup)
    """
    # Deduplicate — Slack can deliver the same event multiple times
    event_id = (
        event.get("client_msg_id")
        or event.get("event_ts")
        or ""
    )
    if event_id and is_event_processed(event_id):
        print(f"[DEDUP] Skipping duplicate event: {event_id}")
        return
    if event_id:
        mark_event_processed(event_id)

    thread_ts = event.get("thread_ts")
    channel = event.get("channel")
    text = (event.get("text") or "").strip()
    files = event.get("files", [])

    if not thread_ts or not channel:
        return

    thread_data = get_thread(thread_ts)
    if not thread_data:
        # Fallback: try to recover by reading the parent message
        # and extracting a ticket ID (handles pre-fix orphaned threads)
        thread_data = _recover_orphaned_thread(
            channel, thread_ts
        )
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
            footer = "Reply in plain English to take action."
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
            update_thread(thread_ts, status="on_prod_cancelled")
            _reply(
                channel, thread_ts,
                "Held — will appear in tonight's digest"
                " as pending client notification.",
            )
        else:
            _reply(channel, thread_ts, "Cancelled — nothing sent to client.")
        return

    if text_lower.startswith("redraft:") or text_lower.startswith("redraft "):
        notes = text[text.index(":") + 1:].strip() if ":" in text else text[8:].strip()
        _fresh = get_thread(thread_ts) or {}
        pending = _fresh.get("pending_send", "")
        if not pending:
            _reply(channel, thread_ts, "Nothing pending to redraft.")
            return

        # Fetch ticket context for Claude
        _ticket_raw = fetch_ticket_from_zoho(ticket_id)
        _fields = _extract_ticket_fields(_ticket_raw) if _ticket_raw else {}
        conversations_result = fetch_ticket_conversations(ticket_id)
        conversations_text = _format_conversations(conversations_result)

        from agent import SYSTEM_PROMPT
        import anthropic
        _claude = anthropic.Anthropic()

        redraft_prompt = (
            "You previously drafted this reply to a client:\n\n"
            f"{pending}\n\n"
            "The reviewer wants you to redraft it with these notes:\n\n"
            f"{notes}\n\n"
            "Keep the same general intent but incorporate the feedback. "
            "Match the client's language. Output only the new draft, "
            "no labels or preamble.\n\n"
            f"Client: {_fields.get('contact_name', '')}\n"
            f"Subject: {_fields.get('subject', '')}\n"
            f"Conversation:\n{conversations_text}"
        )

        try:
            resp = _claude.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=600,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": redraft_prompt}],
            )
            new_draft = resp.content[0].text.strip()
        except Exception as e:
            print(f"Redraft failed: {e}")
            _reply(channel, thread_ts, f"Redraft failed: {e}")
            return

        # Store new draft as pending
        update_thread(thread_ts, pending_send=new_draft)

        _reply(
            channel, thread_ts,
            f"Redrafted:\n\n{new_draft}\n\n"
            "`confirm` — send this version\n"
            "`redraft: [more notes]` — adjust again\n"
            "`cancel` — discard",
        )
        return

    if text_lower == "move backlog":
        if clickup_task_id:
            if _cu_move_to_list(
                clickup_task_id, CLICKUP_ACCEPTED_BACKLOG_LIST
            ):
                _reply(
                    channel, thread_ts, "✓ Moved to Accepted Backlog"
                )
            else:
                _reply(
                    channel, thread_ts,
                    "Could not move task — check ClickUp manually",
                )
        else:
            result = _smart_create_task(
                thread_data, CLICKUP_ACCEPTED_BACKLOG_LIST,
                channel, thread_ts,
            )
            if result:
                _update_thread_clickup_id(thread_ts, result["task_id"])
                _reply(
                    channel, thread_ts,
                    f"✓ ClickUp task created in Accepted Backlog\n"
                    f"{result['task_url']}",
                )
            else:
                _reply(
                    channel, thread_ts,
                    "Could not create ClickUp task — check logs",
                )
        return

    if text_lower in ("move feature", "move raw"):
        if clickup_task_id:
            if _cu_move_to_list(
                clickup_task_id, CLICKUP_LIST_RAW_INTAKE
            ):
                _reply(channel, thread_ts, "✓ Moved to Raw Intake")
            else:
                _reply(
                    channel, thread_ts,
                    "Could not move task — check ClickUp manually",
                )
        else:
            result = _smart_create_task(
                thread_data, CLICKUP_LIST_RAW_INTAKE,
                channel, thread_ts,
            )
            if result:
                _update_thread_clickup_id(thread_ts, result["task_id"])
                _reply(
                    channel, thread_ts,
                    f"✓ ClickUp task created in Raw Intake\n"
                    f"{result['task_url']}",
                )
            else:
                _reply(
                    channel, thread_ts,
                    "Could not create ClickUp task — check logs",
                )
        return

    if text_lower == "move sleeping":
        if clickup_task_id:
            if _cu_move_to_list(
                clickup_task_id, CLICKUP_LIST_SLEEPING
            ):
                _reply(channel, thread_ts, "✓ Moved to Sleeping")
            else:
                _reply(
                    channel, thread_ts,
                    "Could not move task — check ClickUp manually",
                )
        else:
            result = _smart_create_task(
                thread_data, CLICKUP_LIST_SLEEPING,
                channel, thread_ts,
            )
            if result:
                _update_thread_clickup_id(thread_ts, result["task_id"])
                _reply(
                    channel, thread_ts,
                    f"✓ ClickUp task created in Sleeping\n"
                    f"{result['task_url']}",
                )
            else:
                _reply(
                    channel, thread_ts,
                    "Could not create ClickUp task — check logs",
                )
        return

    sleep_m = re.match(r"^sleep\s+(.+)$", text.strip(), re.IGNORECASE)
    if sleep_m:
        wake_date_text = sleep_m.group(1).strip()
        if clickup_task_id:
            if _cu_move_to_list(
                clickup_task_id, CLICKUP_LIST_SLEEPING
            ):
                _reply(
                    channel, thread_ts,
                    f"✓ Moved to Sleeping — wake date: {wake_date_text}\n"
                    "Set Wake Date in ClickUp to complete.",
                )
            else:
                _reply(
                    channel, thread_ts,
                    "Could not move task — check ClickUp manually",
                )
        else:
            result = _smart_create_task(
                thread_data, CLICKUP_LIST_SLEEPING,
                channel, thread_ts,
            )
            if result:
                _update_thread_clickup_id(thread_ts, result["task_id"])
                _reply(
                    channel, thread_ts,
                    f"✓ ClickUp task created in Sleeping\n"
                    f"Wake date: {wake_date_text}\n"
                    f"Set Wake Date in ClickUp: {result['task_url']}",
                )
            else:
                _reply(
                    channel, thread_ts,
                    "Could not create ClickUp task — check logs",
                )
        return

    # -----------------------------------------------------------------------
    # SAFETY: confirm — fire the pending client message
    # -----------------------------------------------------------------------

    if text_lower == "confirm":
        # Always read the latest pending_send from the DB — not a stale
        # local copy — so we always send the most recent draft.
        _fresh = get_thread(thread_ts) or {}
        pending = _fresh.get("pending_send")
        if not pending:
            _reply(
                channel, thread_ts,
                "Nothing pending to confirm.\n"
                "Reply in plain English to take action.",
            )
            return
        # Clear it now (same as _pop_pending_send, but we already fetched)
        update_thread(thread_ts, pending_send=None)

        # --- Content validation: block junk / test messages ---
        junk_reason = _is_junk_content(pending)
        if junk_reason:
            # Re-store so Sam can fix and retry
            update_thread(thread_ts, pending_send=pending)
            _reply(
                channel, thread_ts,
                f"Blocked — this doesn't look like a real client reply.\n"
                f"Reason: {junk_reason}\n\n"
                "Please write a proper response and try again.",
            )
            return

        # Check close_after_send flag before clearing thread state
        close_after = thread_data.get("close_after_send", False)

        zoho_base = "https://desk.zoho.com/support/vomevolunteer"
        zoho_url = (
            f"{zoho_base}/ShowHomePage.do#Cases/dv/{ticket_id}"
        )

        # Fetch contact email + CC so Zoho sendReply has recipients
        _ticket_raw = fetch_ticket_from_zoho(ticket_id)
        _fields = _extract_ticket_fields(_ticket_raw) if _ticket_raw else {}
        _to = _fields.get("contact_email", "")
        _cc = _fields.get("cc_email", "")

        sent_ok = _send_client_reply(
            ticket_id, pending, to_email=_to, cc_email=_cc
        )
        if not sent_ok:
            # Re-store the pending message so Sam can retry
            _store_pending_send(thread_ts, pending, close_after=close_after)
            _reply(
                channel, thread_ts,
                "Failed to send — Zoho API error.\n"
                f"Please send manually in Zoho: {zoho_url}\n\n"
                "The draft is still pending. "
                "Reply `confirm` to retry.",
            )
            return

        _add_reaction(channel, thread_ts, "white_check_mark")
        _set_thread_status(thread_ts, "handled")

        # Clear close_after_send flag
        if close_after:
            update_thread(thread_ts, close_after_send="false")
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
                _zoho_set_status(ticket_id, "Closed")
            else:
                _cu_update_task(
                    clickup_task_id,
                    {"status": "WAITING ON CLIENT"},
                )
                _zoho_set_status(
                    ticket_id, "Awaiting Client Response"
                )
            if is_on_prod:
                _reply(
                    channel, thread_ts,
                    "✓ Sent to client\n"
                    "✓ Zoho ticket closed\n"
                    "✓ ClickUp marked FINISHED",
                )
            else:
                _reply(
                    channel, thread_ts,
                    "✓ Sent to client\n"
                    "✓ Zoho → Awaiting Client Response\n"
                    "✓ ClickUp → Waiting on Client\n"
                    f"View in Zoho: {zoho_url}",
                )
        else:
            # No ClickUp task — just send and update Zoho status
            status_ok = _zoho_set_status(ticket_id, "Awaiting Client Response")
            status_line = (
                "✓ Zoho status → Awaiting Client Response"
                if status_ok
                else "⚠ Zoho status update failed — update manually"
            )
            _reply(
                channel, thread_ts,
                f"✓ Sent to client\n"
                f"{status_line}\n"
                f"View in Zoho: {zoho_url}",
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
            footer = "Reply in plain English to take action."
            _reply(channel, thread_ts, f"{convo}\n{footer}")
        except Exception as e:
            _reply(
                channel, thread_ts,
                f"Could not fetch conversation: {e}",
            )
        return

    # -----------------------------------------------------------------------
    # Restore previous draft — Sam wants the last draft back
    # -----------------------------------------------------------------------

    if _RESTORE_DRAFT_RE.search(text):
        prev = _get_pending_draft(thread_ts)
        if prev:
            _store_pending_send(thread_ts, prev)
            _reply(
                channel, thread_ts,
                "Here's the previous draft:\n"
                "─────────────────────────────────────\n"
                f"{prev}\n"
                "─────────────────────────────────────\n"
                "Reply `confirm` to send\n"
                "Reply `cancel` to hold",
            )
        else:
            _reply(
                channel, thread_ts,
                "No previous draft found for this ticket.",
            )
        return

    # -----------------------------------------------------------------------
    # Wrap — Sam wants greeting + signature added to previous text
    # -----------------------------------------------------------------------

    if _WRAP_RE.search(text):
        prev = _get_pending_draft(thread_ts)
        if prev:
            # Strip existing greeting/signature if present
            body = prev
            # Remove leading "Hi [name]," if present
            body = re.sub(
                r"^Hi\s+\w+,?\s*\n*", "", body, flags=re.IGNORECASE
            ).strip()
            # Remove trailing signature if present
            body = re.sub(
                r"\n*Best,?\s*\n.*$", "", body,
                flags=re.IGNORECASE | re.DOTALL,
            ).strip()
            # Get contact name from thread data
            contact_name = ""
            crm_data = thread_data.get("crm", {})
            if crm_data.get("account_name"):
                contact_name = crm_data["account_name"]
            # Also try classification for contact info
            cls = thread_data.get("classification", {})
            summary = cls.get("issue_summary", "")
            # Try to extract first name from summary
            name_m = re.match(r"(\w+)\s+from\s+", summary)
            if name_m:
                contact_name = name_m.group(1)

            wrapped = _wrap_with_greeting_sig(body, contact_name)
            _store_pending_send(thread_ts, wrapped)
            _reply(
                channel, thread_ts,
                "Wrapped with greeting + signature:\n"
                "─────────────────────────────────────\n"
                f"{wrapped}\n"
                "─────────────────────────────────────\n"
                "Reply `confirm` to send\n"
                "Reply `cancel` to hold",
            )
        else:
            _reply(
                channel, thread_ts,
                "No previous text to wrap. "
                "Send the response text first, then ask me to wrap it.",
            )
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
    # Unified natural language parsing — pass everything through Claude
    # -----------------------------------------------------------------------

    # Try exact command parsing first for speed
    commands, remaining = _parse_commands(text)

    # Always run Claude NLP to catch natural language intent
    nl = _parse_with_claude(text, thread_data)

    # Handle NLP-detected restore/wrap before other processing
    if nl.get("restore_draft") and not commands:
        prev = _get_pending_draft(thread_ts)
        if prev:
            _store_pending_send(thread_ts, prev)
            _reply(
                channel, thread_ts,
                "Here's the previous draft:\n"
                "─────────────────────────────────────\n"
                f"{prev}\n"
                "─────────────────────────────────────\n"
                "Reply `confirm` to send\n"
                "Reply `cancel` to hold",
            )
        else:
            _reply(
                channel, thread_ts,
                "No previous draft found for this ticket.",
            )
        return

    if nl.get("wrap_with_greeting") and not commands:
        prev = _get_pending_draft(thread_ts)
        if prev:
            body = prev
            body = re.sub(
                r"^Hi\s+\w+,?\s*\n*", "", body, flags=re.IGNORECASE
            ).strip()
            body = re.sub(
                r"\n*Best,?\s*\n.*$", "", body,
                flags=re.IGNORECASE | re.DOTALL,
            ).strip()
            # Extract contact first name from thread data
            contact_name = ""
            cls = thread_data.get("classification", {})
            summary = cls.get("issue_summary", "")
            name_m = re.match(r"(\w+)\s+from\s+", summary)
            if name_m:
                contact_name = name_m.group(1)
            wrapped = _wrap_with_greeting_sig(body, contact_name)
            _store_pending_send(thread_ts, wrapped)
            _reply(
                channel, thread_ts,
                "Wrapped with greeting + signature:\n"
                "─────────────────────────────────────\n"
                f"{wrapped}\n"
                "─────────────────────────────────────\n"
                "Reply `confirm` to send\n"
                "Reply `cancel` to hold",
            )
        else:
            _reply(
                channel, thread_ts,
                "No previous text to wrap. "
                "Send the response text first.",
            )
        return

    # Handle verbatim text — Sam provided exact response body
    if nl.get("verbatim_text") and not commands:
        verbatim = nl["verbatim_text"]
        # Extract contact first name
        contact_name = ""
        cls = thread_data.get("classification", {})
        summary = cls.get("issue_summary", "")
        name_m = re.match(r"(\w+)\s+from\s+", summary)
        if name_m:
            contact_name = name_m.group(1)
        wrapped = _wrap_with_greeting_sig(verbatim, contact_name)
        _store_pending_send(thread_ts, wrapped)
        _reply(
            channel, thread_ts,
            "Draft ready — not sent yet:\n"
            "─────────────────────────────────────\n"
            f"{wrapped}\n"
            "─────────────────────────────────────\n"
            "Reply `confirm` to send\n"
            "Reply `cancel` to hold",
        )
        return

    # Merge: Claude results fill in anything exact parsing missed
    if nl.get("needs_clarification") and not commands and not nl.get("skip"):
        question = (
            nl.get("clarification_question")
            or "Could you clarify what you'd like me to do?"
        )
        _reply(
            channel, thread_ts,
            f"I want to make sure I do the right thing here — {question}",
        )
        return

    if nl.get("skip"):
        _add_reaction(channel, thread_ts, "double_vertical_bar")
        _set_thread_status(thread_ts, "parked")
        if clickup_task_id:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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

    # Merge NLP results into commands dict
    if nl.get("assign_to") and "assign" not in commands:
        commands["assign"] = nl["assign_to"]
    if nl.get("priority") and "priority" not in commands:
        commands["priority"] = nl["priority"].lower()
    if nl.get("auto_score") is not None and "score" not in commands:
        try:
            commands["score"] = str(int(nl["auto_score"]))
        except (ValueError, TypeError):
            pass
    if nl.get("tier") and "tier" not in commands:
        commands["tier"] = nl["tier"]
    if nl.get("arr") is not None and "arr" not in commands:
        try:
            commands["arr"] = str(int(nl["arr"]))
        except (ValueError, TypeError):
            pass

    # Determine if we need a draft
    wants_draft = nl.get("generate_draft", False)
    draft_instruction = nl.get("draft_instruction") or ""
    close_after = nl.get("close_ticket", False)

    # Also detect draft intent via regex for speed/reliability
    if _is_client_response_instruction(text) and not wants_draft:
        wants_draft = True
        draft_instruction = text
    if _has_close_instruction(text):
        close_after = True

    # Determine task list
    task_list_key = nl.get("task_list")
    wants_task = nl.get("create_task", False)

    # create_task is true whenever assign_to or task_list or priority
    if commands.get("assign") or task_list_key or commands.get("priority"):
        wants_task = True

    # Do NOT create a task when the only intent is drafting or closing
    if wants_task and not (
        commands.get("assign") or task_list_key or commands.get("priority")
    ):
        wants_task = False

    # Nothing actionable at all — but if a ClickUp task exists,
    # try auto-updating it with any new context from this message
    if (
        not commands
        and not wants_draft
        and not wants_task
        and not close_after
        and not nl.get("client_response")
    ):
        if clickup_task_id:
            _auto_update_clickup_from_thread(
                clickup_task_id, text, files,
                channel, thread_ts, thread_data,
            )
        else:
            _reply(channel, thread_ts, _NL_HELP)
        return

    # -----------------------------------------------------------------------
    # Execute ALL extracted actions in sequence
    # -----------------------------------------------------------------------

    action_lines: list[str] = []
    task_just_created = False

    # 1. Create ClickUp task if needed
    target_list = (
        _TASK_LIST_MAP.get(task_list_key, CLICKUP_LIST_PRIORITY_QUEUE)
        if task_list_key
        else CLICKUP_LIST_PRIORITY_QUEUE
    )

    if wants_task and not clickup_task_id:
        assignee_cu_id = None
        if "assign" in commands:
            raw_name = commands["assign"].lower().strip()
            canonical = _resolve_assignee(raw_name)
            if canonical:
                assignee_cu_id = _ASSIGNEE_IDS.get(canonical) or None

        result = _smart_create_task(
            thread_data,
            target_list,
            channel,
            thread_ts,
            assignee_cu_id=assignee_cu_id,
            priority_override=(
                commands.get("priority", "").upper() or None
            ),
        )
        if result:
            clickup_task_id = result["task_id"]
            task_just_created = True
            _update_thread_clickup_id(thread_ts, clickup_task_id)
            list_label = (task_list_key or "priority_queue").replace(
                "_", " "
            ).title()
            action_lines.append(
                f"ClickUp task created in {list_label}:\n"
                f"  {result['task_url']}"
            )

    # 2. Assign engineer if needed
    if "assign" in commands:
        raw_name = commands["assign"].lower().strip()
        canonical = _resolve_assignee(raw_name)
        print(
            f"[ASSIGN] raw='{commands['assign']}' "
            f"lower='{raw_name}' canonical={canonical}"
        )
        if canonical:
            cu_user_id = _ASSIGNEE_IDS.get(canonical, "")
            zoho_agent_id = _ZOHO_AGENT_IDS.get(canonical, "")
            zoho_status = _ZOHO_STATUS[canonical]
            cu_display = _ASSIGNEE_DISPLAY[canonical]

            task_url = None

            if clickup_task_id and cu_user_id:
                # Only skip update if task was just created with
                # this assignee already set during creation
                if not task_just_created:
                    _cu_update_task(
                        clickup_task_id,
                        {"assignees": {"add": [int(cu_user_id)]}},
                    )
                task_url = (
                    f"https://app.clickup.com/t/{clickup_task_id}"
                )

            if zoho_agent_id:
                _zoho_assign_ticket(ticket_id, zoho_agent_id)
                _zoho_set_status(ticket_id, zoho_status)

            # Notify engineering channel for OnlyG or Sanjay
            if canonical in ("onlyg", "sanjay"):
                if not task_url and clickup_task_id:
                    task_url = (
                        f"https://app.clickup.com/t/{clickup_task_id}"
                    )
                if task_url:
                    classification = thread_data.get(
                        "classification", {}
                    )
                    crm = thread_data.get("crm", {})
                    zoho_url = (
                        "https://desk.zoho.com/support/vomevolunteer"
                        f"/ShowHomePage.do#Cases/dv/{ticket_id}"
                    )
                    _notify_engineering(
                        engineer_name=cu_display,
                        ticket_number=thread_data.get(
                            "ticket_number", ""
                        ),
                        subject=thread_data.get("subject", ""),
                        account_name=(
                            crm.get("account_name") or "Unknown"
                        ),
                        priority=classification.get(
                            "priority", "P3"
                        ),
                        module=classification.get(
                            "module", "Unknown"
                        ),
                        issue_summary=classification.get(
                            "issue_summary", ""
                        ),
                        task_url=task_url,
                        zoho_url=zoho_url,
                    )

            if cu_user_id or zoho_agent_id:
                action_lines.append(f"Assigned to: {cu_display}")
                if canonical in ("onlyg", "sanjay") and task_url:
                    action_lines.append("Engineering channel notified")
            else:
                action_lines.append(
                    f"Assign failed: {cu_display} IDs not configured"
                )
        else:
            action_lines.append(
                "Name not recognised — who did you mean?"
                " Options: Sam, OnlyG, Sanjay, Ron"
            )

    # 3. Set fields if needed
    if "priority" in commands:
        p = commands["priority"]
        cu_p = PRIORITY_MAP.get(p)
        if cu_p and clickup_task_id and not wants_task:
            # Only update priority if we didn't just create with it
            _cu_update_task(clickup_task_id, {"priority": cu_p})
            action_lines.append(f"Priority: {p.upper()}")

    if "tier" in commands and clickup_task_id:
        _cu_set_field(
            clickup_task_id, FIELD_HIGHEST_TIER, commands["tier"]
        )
        action_lines.append(f"Tier: {commands['tier']}")

    if "arr" in commands and clickup_task_id:
        try:
            arr_val = int(commands["arr"])
            _cu_set_field(
                clickup_task_id, FIELD_COMBINED_ARR, arr_val
            )
            action_lines.append(f"ARR: ${arr_val:,}")
        except ValueError:
            action_lines.append("ARR — invalid number")

    if "score" in commands and clickup_task_id:
        try:
            score_val = int(commands["score"])
            _cu_set_field(
                clickup_task_id, FIELD_AUTO_SCORE, score_val
            )
            action_lines.append(f"Auto Score: {score_val}")
        except ValueError:
            pass

    # 4. Generate draft if needed
    draft_text = None
    if wants_draft:
        try:
            draft_text = _generate_draft_from_instruction(
                ticket_id, draft_instruction or text
            )
            _store_pending_send(
                thread_ts, draft_text, close_after=close_after
            )
        except Exception as e:
            action_lines.append(f"Draft generation failed: {e}")

    # Handle explicit client_response from NLP (verbatim send)
    if not wants_draft and nl.get("client_response"):
        client_msg = nl["client_response"]
        if not _has_internal_keyword(client_msg):
            _store_pending_send(
                thread_ts, client_msg, close_after=close_after
            )
            draft_text = client_msg

    # -----------------------------------------------------------------------
    # Post one combined confirmation
    # -----------------------------------------------------------------------

    if action_lines or draft_text:
        _reply(
            channel, thread_ts,
            _build_confirmation(
                action_lines, draft=draft_text, close_after=close_after
            ),
        )
    elif close_after and not wants_draft:
        # Close-only with no draft requested — close ticket directly
        zoho_base = "https://desk.zoho.com/support/vomevolunteer"
        zoho_url = f"{zoho_base}/ShowHomePage.do#Cases/dv/{ticket_id}"
        status_ok = _zoho_set_status(ticket_id, "Closed")
        if clickup_task_id:
            _cu_update_task(clickup_task_id, {"status": "FINISHED"})
        _set_thread_status(thread_ts, "handled")
        if status_ok:
            cu_line = (
                "\n✓ ClickUp marked FINISHED"
                if clickup_task_id else ""
            )
            _reply(
                channel, thread_ts,
                f"✓ Zoho ticket closed{cu_line}\n"
                f"View in Zoho: {zoho_url}",
            )
        else:
            _reply(
                channel, thread_ts,
                f"⚠ Zoho status update failed — close manually:\n{zoho_url}",
            )
    elif close_after:
        # Close with draft — generate a closing message first
        try:
            draft_text = _generate_draft_from_instruction(
                ticket_id, "close the ticket with a brief confirmation"
            )
            _store_pending_send(
                thread_ts, draft_text, close_after=True
            )
            _reply(
                channel, thread_ts,
                _build_confirmation(
                    [], draft=draft_text, close_after=True
                ),
            )
        except Exception as e:
            _reply(
                channel, thread_ts,
                f"Draft generation failed: {e}",
            )
    else:
        # No actions taken — try auto-updating ClickUp if task exists
        if clickup_task_id:
            _auto_update_clickup_from_thread(
                clickup_task_id, text, files,
                channel, thread_ts, thread_data,
            )
        else:
            _reply(channel, thread_ts, _NL_HELP)
