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
    _zoho_desk_call,
    _unwrap_mcp_result,
    fetch_ticket_conversations,
    fetch_ticket_from_zoho,
)
CHANNEL_FINAL_REVIEW = os.environ.get("SLACK_CHANNEL_SUPPORT_FINAL_REVIEW", "")
CHANNEL_FINISHED_TASKS = os.environ.get("SLACK_CHANNEL_FINISHED_TASKS", "")
from database import (
    get_thread,
    get_thread_by_ticket_id,
    save_thread,
    update_thread,
)
from status_constants import (
    CU_WRITE_CLOSED_TITLE,
    ZOHO_FINAL_REVIEW,
    ZOHO_CLOSED,
    THREAD_CLOSED,
    THREAD_ON_PROD_PENDING,
    THREAD_ON_PROD_SENT,
)
from signatures import signature, sign_message

ZOHO_FROM_ADDRESS = os.environ.get(
    "ZOHO_FROM_ADDRESS", "support@vomevolunteer.zohodesk.com"
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
    """Set ClickUp task status to Closed."""
    try:
        r = httpx.put(
            f"{CLICKUP_BASE}/task/{task_id}",
            json={"status": CU_WRITE_CLOSED_TITLE},
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
    result = _zoho_desk_call("ZohoDesk_updateTicket", {
        "body": {"status": ZOHO_FINAL_REVIEW},
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
    update_thread(thread_ts, status=THREAD_ON_PROD_PENDING)


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
        "Keep it brief and surface level. Let the client know an update has "
        "been made and the issue they reported should now be resolved. Invite "
        "them to check on their end and reply if they still run into any "
        "problems. Reference their issue only at a high level.\n"
        "Do not be technical. Do not explain the fix, the root cause, the "
        "steps taken, or mention internal tools or processes.\n"
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
    # Auto-send category: resolution replies are signed "Vic".
    return sign_message(response.content[0].text.strip(), "vic", language)


# ---------------------------------------------------------------------------
# Pre-send thread review — has the client already been told it's fixed?
# ---------------------------------------------------------------------------

def _assess_resolution_state(
    ticket_fields: dict, conversations_text: str
) -> dict:
    """Decide whether the client has ALREADY been told this issue is fixed.

    Returns a dict:
        {
          "already_confirmed_fixed": bool,
          "recommendation": "skip" | "send",
          "reason": str,
          "last_team_reply": str,
        }

    Conservative by design: only "skip" when a prior TEAM reply clearly
    confirms the fix/resolution for THIS issue. A generic acknowledgment
    ("we received this", "we'll get back to you") is NOT a resolution and
    means "send". On any failure, defaults to "send" (never silently skip).
    """
    contact_name = ticket_fields.get("contact_name", "")
    subject = ticket_fields.get("subject", "")

    prompt = (
        "An engineer just marked this ticket's fix as live (ON PROD).\n"
        "Before we email the client a resolution confirmation, decide "
        "whether the client has ALREADY been told, in a previous reply "
        "from our team, that THIS specific issue is fixed / resolved / "
        "deployed / working now.\n\n"
        "Rules:\n"
        "- A generic acknowledgment ('thanks for reporting this', 'we'll "
        "get back to you', 'we're looking into it') is NOT a resolution "
        "confirmation. If that is the most recent team reply, we still "
        "need to send the fixed email.\n"
        "- Only treat it as already resolved if a team reply clearly tells "
        "the client this issue has been fixed/resolved/deployed/works now.\n"
        "- Weigh timing and order: who sent the most recent message, what "
        "it said, and the dates. If the client replied AFTER a fix "
        "confirmation saying it still is not working, it is NOT resolved.\n"
        "- When unsure, choose 'send'. A missed update is worse than a "
        "slightly redundant note; only choose 'skip' when you are "
        "confident a fix confirmation already went out.\n\n"
        f"Subject: {subject}\n"
        f"Client: {contact_name}\n\n"
        "Full conversation thread (oldest to newest, with timestamps):\n"
        f"{conversations_text}\n\n"
        "Return valid JSON only, no prose, no code fences:\n"
        "{\n"
        '  "already_confirmed_fixed": true or false,\n'
        '  "recommendation": "skip" or "send",\n'
        '  "reason": "one sentence explaining the decision",\n'
        '  "last_team_reply": "one-line summary of the most recent team '
        'reply to the client, or empty if none"\n'
        "}"
    )

    try:
        resp = _anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        data = json.loads(raw)
    except Exception as e:
        print(f"[ON PROD] Resolution-state review failed: {e}")
        return {
            "already_confirmed_fixed": False,
            "recommendation": "send",
            "reason": f"review failed ({e}); defaulting to send",
            "last_team_reply": "",
        }

    already = bool(data.get("already_confirmed_fixed"))
    rec = str(data.get("recommendation") or "").strip().lower()
    if rec not in ("skip", "send"):
        rec = "skip" if already else "send"
    return {
        "already_confirmed_fixed": already,
        "recommendation": rec,
        "reason": str(data.get("reason", "")),
        "last_team_reply": str(data.get("last_team_reply", "")),
    }


# ---------------------------------------------------------------------------
# Slack posting
# ---------------------------------------------------------------------------

def _on_prod_message(
    ticket_number: str,
    engineer_name: str,
    draft: str,
    ticket_fields: dict | None = None,
    zoho_ticket_id: str = "",
    clickup_task_id: str = "",
) -> str:
    """Build the ON PROD Slack message block."""
    zoho_url = (
        f"https://desk.zoho.com/support/vomevolunteer"
        f"/ShowHomePage.do#Cases/dv/{zoho_ticket_id}"
    ) if zoho_ticket_id else ""
    clickup_url = (
        f"https://app.clickup.com/t/{clickup_task_id}"
    ) if clickup_task_id else ""

    lines = [
        f":rocket: *On Prod — #{ticket_number}*",
        f"*{engineer_name} marked this as fixed.*",
    ]
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

    # Always show links
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
        ":speech_balloon: *SUGGESTED RESOLUTION — not sent yet*",
        "",
        draft,
        _SEP,
        "`confirm` — send as-is",
        "`send: [your version]` — send your custom reply",
        "`redraft: [your notes]` — redraft with your pointers",
        "`cancel` — hold for tonight's digest",
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
    """Post ON PROD notification as a reply in an existing Slack thread."""
    text = _on_prod_message(
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
        ticket_fields=ticket_fields,
        zoho_ticket_id=zoho_ticket_id,
        clickup_task_id=clickup_task_id,
    )
    try:
        resp = _slack.chat_postMessage(channel=CHANNEL_FINAL_REVIEW, text=text)
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
        print(f"[ON PROD] New thread post failed: {e.response['error']}")
        return None


# ---------------------------------------------------------------------------
# Auto-send (resolution email -> close Zoho + ClickUp)
# ---------------------------------------------------------------------------

def _set_zoho_status_closed(zoho_ticket_id: str) -> bool:
    """Set the Zoho ticket status to Closed."""
    result = _zoho_desk_call("ZohoDesk_updateTicket", {
        "body": {"status": ZOHO_CLOSED},
        "path_variables": {"ticketId": str(zoho_ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(f"[ON PROD] Failed to close Zoho ticket {zoho_ticket_id}")
        return False
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(
            f"[ON PROD] Zoho close failed for ticket"
            f" {zoho_ticket_id}: {data}"
        )
        return False
    print(f"[ON PROD] Zoho ticket {zoho_ticket_id} closed")
    return True


def _send_resolution_email(
    zoho_ticket_id: str, content: str, to_email: str, cc_email: str = ""
) -> bool:
    """Email the resolution reply to the client via ZohoDesk_sendReply."""
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
        "path_variables": {"ticketId": str(zoho_ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(
            f"[ON PROD] sendReply failed (no result) — ticket {zoho_ticket_id}"
        )
        return False
    if isinstance(result, dict) and result.get("isError"):
        print(f"[ON PROD] sendReply error — ticket {zoho_ticket_id}: {result}")
        return False
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"[ON PROD] sendReply Zoho error — ticket {zoho_ticket_id}: {data}")
        return False
    print(f"[ON PROD] Resolution emailed to client — ticket {zoho_ticket_id}")
    return True


def _on_prod_sent_message(
    ticket_number: str,
    engineer_name: str,
    draft: str,
    ticket_fields: dict | None = None,
    zoho_ticket_id: str = "",
    clickup_task_id: str = "",
) -> str:
    """Build the informational 'auto-sent + closed' Slack record (no buttons)."""
    zoho_url = (
        f"https://desk.zoho.com/support/vomevolunteer"
        f"/ShowHomePage.do#Cases/dv/{zoho_ticket_id}"
    ) if zoho_ticket_id else ""
    clickup_url = (
        f"https://app.clickup.com/t/{clickup_task_id}"
    ) if clickup_task_id else ""

    lines = [
        f":white_check_mark: *On Prod — #{ticket_number} — auto-sent to client*",
        f"*{engineer_name} marked this fixed. Vic emailed the client and"
        " the ticket is closed.*",
    ]
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

    lines += [
        "",
        _SEP,
        ":outbox_tray: *SENT TO CLIENT (signed Vic)*",
        "",
        draft,
        _SEP,
        "Zoho ticket closed | ClickUp task closed.",
    ]
    return "\n".join(lines)


def _on_prod_skipped_message(
    ticket_number: str,
    engineer_name: str,
    assessment: dict,
    ticket_fields: dict | None = None,
    zoho_ticket_id: str = "",
    clickup_task_id: str = "",
) -> str:
    """Build the 'closed without emailing — already resolved' Slack record."""
    zoho_url = (
        f"https://desk.zoho.com/support/vomevolunteer"
        f"/ShowHomePage.do#Cases/dv/{zoho_ticket_id}"
    ) if zoho_ticket_id else ""
    clickup_url = (
        f"https://app.clickup.com/t/{clickup_task_id}"
    ) if clickup_task_id else ""

    lines = [
        f":white_check_mark: *On Prod — #{ticket_number} — closed,"
        " no email sent*",
        f"*{engineer_name} marked this fixed. Vic reviewed the thread and"
        " the client was already told it's resolved, so no email was"
        " sent.*",
    ]
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

    reason = assessment.get("reason", "")
    last_reply = assessment.get("last_team_reply", "")
    lines += [
        "",
        _SEP,
        ":no_bell: *No email sent (duplicate avoided)*",
        f"Why: {reason}" if reason else "Why: already confirmed resolved",
    ]
    if last_reply:
        lines.append(f"Last team reply: {last_reply}")
    lines += [
        _SEP,
        "Zoho ticket closed | ClickUp task closed.",
    ]
    return "\n".join(lines)


def _post_on_prod_record(
    zoho_ticket_id: str,
    clickup_task_id: str,
    text: str,
    status: str,
    ticket_fields: dict,
    thread_ts: str | None,
) -> None:
    """Post an on-prod outcome record to Slack and set the thread status.

    Replies into the existing thread when there is one, otherwise posts a
    standalone message and maps it so a later client reply on the now-closed
    ticket is still handled.
    """
    if thread_ts:
        thread_entry = get_thread(thread_ts) or {}
        channel = thread_entry.get("channel") or CHANNEL_FINAL_REVIEW
        try:
            _slack.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text
            )
        except SlackApiError as e:
            print(f"[ON PROD] Record post failed: {e.response['error']}")
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
        print(f"[ON PROD] Record post failed: {e.response['error']}")


def _notify_auto_sent(
    zoho_ticket_id: str,
    clickup_task_id: str,
    engineer_name: str,
    draft: str,
    ticket_fields: dict,
    thread_ts: str | None,
) -> None:
    """Post the auto-sent record to Slack and mark the thread as sent."""
    ticket_number = zoho_ticket_id
    if thread_ts:
        ticket_number = (
            (get_thread(thread_ts) or {}).get("ticket_number")
            or zoho_ticket_id
        )
    text = _on_prod_sent_message(
        ticket_number, engineer_name, draft,
        ticket_fields=ticket_fields,
        zoho_ticket_id=zoho_ticket_id,
        clickup_task_id=clickup_task_id,
    )
    _post_on_prod_record(
        zoho_ticket_id, clickup_task_id, text,
        THREAD_ON_PROD_SENT, ticket_fields, thread_ts,
    )


def _notify_closed_no_send(
    zoho_ticket_id: str,
    clickup_task_id: str,
    engineer_name: str,
    assessment: dict,
    ticket_fields: dict,
    thread_ts: str | None,
) -> None:
    """Post the 'closed, no email (already resolved)' record to Slack."""
    ticket_number = zoho_ticket_id
    if thread_ts:
        ticket_number = (
            (get_thread(thread_ts) or {}).get("ticket_number")
            or zoho_ticket_id
        )
    text = _on_prod_skipped_message(
        ticket_number, engineer_name, assessment,
        ticket_fields=ticket_fields,
        zoho_ticket_id=zoho_ticket_id,
        clickup_task_id=clickup_task_id,
    )
    _post_on_prod_record(
        zoho_ticket_id, clickup_task_id, text,
        THREAD_CLOSED, ticket_fields, thread_ts,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def handle_on_prod(task_id: str, engineer_name: str) -> bool:
    """
    Main entry point for the ON PROD flow.

    Called when a ClickUp task status changes to ON PROD. Generates the
    resolution reply (signed Vic), emails it to the client automatically,
    then closes the Zoho ticket and the ClickUp task and posts a record to
    Slack. If the reply cannot be auto-sent (no contact email, empty draft,
    or a send error), it falls back to posting a review draft to Slack with
    confirm/send/cancel and leaves the ticket in Final Review.
    Returns True on success (auto-sent or review draft posted).
    """
    print(f"[ON PROD] Task {task_id} marked on prod by {engineer_name}")

    # Step 1 — fetch ClickUp task
    task = _get_clickup_task(task_id)
    if not task:
        print(f"[ON PROD] Could not fetch ClickUp task {task_id}")
        return False

    task_title = task.get("name", task_id)
    task_url = task.get("url") or f"https://app.clickup.com/t/{task_id}"
    description = task.get("description") or ""

    # Always post a brief notification to #eng-alerts so Ron + team
    # can see every on-prod update in real time
    if CHANNEL_FINISHED_TASKS:
        try:
            _slack.chat_postMessage(
                channel=CHANNEL_FINISHED_TASKS,
                text=(
                    f":rocket: *On Prod* — {engineer_name} shipped: "
                    f"*{task_title}*\n{task_url}"
                ),
            )
            print(f"[ON PROD] Alert posted to #eng-alerts for {task_id}")
        except SlackApiError as e:
            print(f"[ON PROD] eng-alerts post failed: {e.response['error']}")

    # Pull classification + module from task description for draft context
    cl_match = re.search(
        r"Classification:\s*(.+)", description, re.IGNORECASE
    )
    mod_match = re.search(
        r"Module:\s*(.+)", description, re.IGNORECASE
    )
    classification = cl_match.group(1).strip() if cl_match else "Bug"
    module = mod_match.group(1).strip() if mod_match else "Other"

    # Step 2 — extract Zoho ticket ID (only support tickets get the
    # full final-review flow with draft generation)
    zoho_ticket_id = _extract_zoho_ticket_id(task)
    if not zoho_ticket_id:
        print(
            f"[ON PROD] No Zoho ticket ID in task {task_id} ({task_title}) "
            "— no support ticket to review, eng-alert posted"
        )
        return True  # eng-alert was posted, that's enough

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

    # Step 5b — review the thread before sending. If the client has already
    # been told this specific issue is fixed, do NOT send a duplicate: just
    # close the ticket on Zoho and the task on ClickUp.
    assessment = _assess_resolution_state(fields, conversations_text)
    print(
        f"[ON PROD] Resolution review — ticket {zoho_ticket_id}:"
        f" recommendation={assessment['recommendation']}"
        f" already_fixed={assessment['already_confirmed_fixed']}"
        f" reason={assessment['reason']}"
    )

    if assessment["recommendation"] == "skip":
        _set_zoho_status_closed(zoho_ticket_id)
        update_clickup_status_finished(task_id)
        _notify_closed_no_send(
            zoho_ticket_id=zoho_ticket_id,
            clickup_task_id=task_id,
            engineer_name=engineer_name,
            assessment=assessment,
            ticket_fields=fields,
            thread_ts=thread_ts,
        )
        print(
            f"[ON PROD] Already resolved in thread — closed without"
            f" emailing — ticket {zoho_ticket_id}"
        )
        return True

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
            + signature("vic")
        )

    # Step 7 — AUTO-SEND the resolution to the client (signed Vic), then
    # close the ticket on Zoho and the task on ClickUp.
    contact_email = fields.get("contact_email", "")
    cc_email = fields.get("cc_email", "")

    can_send = (
        bool(contact_email) and bool(draft) and len(draft.strip()) >= 20
    )
    sent = (
        _send_resolution_email(
            zoho_ticket_id, draft,
            to_email=contact_email, cc_email=cc_email,
        )
        if can_send
        else False
    )

    if sent:
        # Close everywhere
        _set_zoho_status_closed(zoho_ticket_id)
        update_clickup_status_finished(task_id)
        # Post a record to Slack for visibility / Vic-output validation
        _notify_auto_sent(
            zoho_ticket_id=zoho_ticket_id,
            clickup_task_id=task_id,
            engineer_name=engineer_name,
            draft=draft,
            ticket_fields=fields,
            thread_ts=thread_ts,
        )
        print(
            f"[ON PROD] Auto-sent + closed — ticket {zoho_ticket_id},"
            f" task {task_id}"
        )
        return True

    # --- Fallback: cannot auto-send (no contact email, empty draft, or the
    # send failed). Degrade to the manual-review flow so nothing is lost: post
    # the draft to Slack with confirm/send/cancel; Zoho stays in Final Review.
    print(
        f"[ON PROD] Auto-send unavailable for ticket {zoho_ticket_id} "
        "— falling back to Slack review"
    )

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
            zoho_ticket_id=zoho_ticket_id,
            clickup_task_id=task_id,
            ticket_fields=fields,
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

    # Store pending draft + mark status for the manual confirm flow
    _store_pending_send(thread_ts, draft)
    set_thread_on_prod_pending(thread_ts)
    print(f"[ON PROD] Review draft posted, thread_ts={thread_ts}")
    return True
