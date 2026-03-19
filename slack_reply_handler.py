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
    SYSTEM_PROMPT,
    ZOHO_ORG_ID,
    _detect_language,
    _extract_ticket_fields,
    _format_conversations,
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

ASSIGNEE_MAP = {
    "onlyg": os.environ.get("CLICKUP_USER_ONLYG", ""),
    "sanjay": os.environ.get("CLICKUP_USER_SANJAY", ""),
    "sam": os.environ.get("CLICKUP_USER_SAM", ""),
    "ron": os.environ.get("CLICKUP_USER_RON", ""),
}

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


def _store_pending_send(thread_ts: str, message: str):
    """Save a client message that needs Sam's `confirm` before sending."""
    data = _load_thread_map()
    if thread_ts in data:
        data[thread_ts]["pending_send"] = message
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

    # note [text] — consumes everything after "note" to end of string
    m = re.search(r"\bnote\s+(.+)", remaining, re.IGNORECASE | re.DOTALL)
    if m:
        commands["note"] = m.group(1).strip()
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

    if text_lower == "draft":
        try:
            draft = _generate_draft(ticket_id)
            _store_pending_send(thread_ts, draft)
            _reply(
                channel,
                thread_ts,
                f"DRAFT — not sent yet:\n\"{draft}\"\n\n"
                "Reply `confirm` to send this, "
                "or `send: [edited version]` to send a different version, "
                "or `cancel` to discard.",
            )
        except Exception as e:
            _reply(channel, thread_ts, f"Draft generation failed: {e}")
        return

    if text_lower == "cancel":
        _pop_pending_send(thread_ts)
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
        _send_client_reply(ticket_id, pending)
        _add_reaction(channel, thread_ts, "white_check_mark")
        _set_thread_status(thread_ts, "handled")
        if clickup_task_id:
            _cu_update_task(
                clickup_task_id, {"status": "acknowledged"}
            )
        zoho_base = "https://desk.zoho.com/support/vomevolunteer"
        zoho_url = (
            f"{zoho_base}/ShowHomePage.do#Cases/dv/{ticket_id}"
        )
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
        if nl.get("note"):
            commands["note"] = nl["note"]
        # NLP client_response is ignored here — Sam must use reply: prefix

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

    if "assign" in commands and clickup_task_id:
        name_key = commands["assign"].lower()
        if name_key == "saul":
            name_key = "sam"
        user_id = ASSIGNEE_MAP.get(name_key, "")
        if user_id:
            _cu_update_task(
                clickup_task_id,
                {"assignees": {"add": [int(user_id)]}},
            )
            action_lines.append(
                f"Assigned to: {name_key.capitalize()}"
            )
        else:
            action_lines.append(
                f"Assign failed: '{commands['assign']}' not recognised"
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

    if "note" in commands and clickup_task_id:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _cu_update_task(
            clickup_task_id,
            {"description": (
                f"[Note from Sam — {date_str}]: {commands['note']}"
            )},
        )
        action_lines.append("Note added to ClickUp")

    # -----------------------------------------------------------------------
    # Confirmation — always post, never auto-send to client
    # -----------------------------------------------------------------------

    if action_lines:
        _reply(channel, thread_ts, _build_confirmation(action_lines))
    else:
        _reply(channel, thread_ts, _NL_HELP)
