"""
clickup_user_education_handler.py

Handles ClickUp taskStatusUpdated -> "user education".

When an engineer sets a task to "user education", the dev has concluded
the user is misunderstanding how a feature works (or the steps), and the
ticket just needs a clear explanation of how it actually works and what
they can do -- not a code fix and not a request for more info.

  1. Fetch the linked Zoho ticket + the dev's ClickUp notes (the explanation)
  2. Review the thread: has this same explanation already been sent?
  3. If still needed: draft a friendly, plain-language explanation (signed
     Vic) and AUTO-SEND it to the client
  4. Treat the explanation as the resolution: close the ClickUp task and the
     Zoho ticket (a later client reply reopens the Zoho ticket as normal)
  5. If already explained -> skip the email and post a Slack note instead; if
     it can't auto-send -> fall back to a Slack review draft

Mirrors clickup_waiting_client_handler.py and reuses its plumbing; only the
draft tone (educate, don't request) and the post-send outcome (close, don't
park) differ.
"""

import json
import os
import re

import anthropic
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from agent import (
    SYSTEM_PROMPT,
    _extract_ticket_fields,
    _format_conversations,
    fetch_ticket_conversations,
    fetch_ticket_from_zoho,
)
# Reuse the proven ClickUp/Zoho/thread plumbing from the needs-client-info
# handler -- these helpers are generic (fetch task, read comments, map to the
# Zoho ticket, set statuses, send the reply) and behave identically here.
from clickup_waiting_client_handler import (
    CHANNEL_FINAL_REVIEW,
    _SEP,
    _create_new_thread,
    _extract_zoho_ticket_id,
    _find_thread_and_data,
    _format_engineer_context,
    _get_clickup_task,
    _get_clickup_task_comments,
    _post_to_existing_thread,
    _send_info_email,
    _set_clickup_status,
    _set_zoho_status,
    _store_pending_send,
)
from database import get_thread, save_thread, update_thread
from status_constants import (
    THREAD_CLOSED,
    CU_WRITE_CLOSED_TITLE,
    ZOHO_CLOSED,
)
from signatures import signature, sign_message
from model_config import SUPPORT_MODEL

_anthropic = anthropic.Anthropic()
_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))


# ---------------------------------------------------------------------------
# Claude draft: contextual "here's how it works" explanation
# ---------------------------------------------------------------------------

def _generate_education_message(
    ticket_fields: dict,
    conversations_text: str,
    engineer_context: str,
    language: str | None = None,
) -> str:
    """Use Claude to draft a plain-language explanation for the client.

    Uses the dev's ClickUp notes as the primary source of truth for how
    the feature actually works and what the client should do.
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
            "\n\nDEV'S NOTES (from ClickUp task):\n"
            "These are the dev's internal notes explaining how the"
            " feature actually works, where the client's"
            " understanding is off, and what they should do instead."
            " Use this as the PRIMARY basis for your message."
            " Translate the technical explanation into a clear,"
            " friendly, client-facing walkthrough.\n"
            f"{engineer_context}\n"
        )

    prompt = (
        "A client reached out about something on the Vome platform, and"
        " after looking into it our team found the feature is working as"
        " intended -- the client just misunderstands how it works or what"
        " the steps are. Write a warm, helpful message that explains how"
        " the feature actually works and what they can do.\n\n"
        "CRITICAL: Read the dev's notes carefully. They explain the"
        " correct behavior and what the client should do. Your message"
        " must reflect that specific explanation, not generic advice.\n\n"
        "Rules:\n"
        "- Lead with empathy; never make the client feel foolish for"
        " asking\n"
        "- Clearly explain how the feature is meant to work, then give"
        " the concrete steps they should take\n"
        "- Be specific to their situation using the dev's notes (e.g."
        " 'When you do X, the system does Y, so to get Z you'll want"
        " to...')\n"
        "- This is an explanation, NOT a request for more information,"
        " and NOT an apology for a bug -- the feature works correctly\n"
        "- Follow all voice guidelines from the system prompt\n"
        "- Do not use an em-dash anywhere in the response\n"
        "- Do not mention engineering, internal tools, devs, or ClickUp\n"
        "- Say 'we' not 'I' when referring to the team\n"
        "- Invite them to reply if anything is still unclear\n"
        "- Do not write a closing or signature; end at the last"
        " sentence (a signature is appended automatically)\n"
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
    # Auto-send category: user-education explanations are signed "Vic".
    return sign_message(response.content[0].text.strip(), "vic", language)


def _assess_education_state(
    ticket_fields: dict, conversations_text: str, engineer_context: str
) -> dict:
    """Decide whether the explanation should actually be emailed.

    Returns {"recommendation", "reason", "last_relevant"} where
    recommendation is one of:
      - "send": we have not yet explained this to the client.
      - "skip_already_sent": an equivalent explanation was already sent and
        the client has not raised a new question since -> skip the email.
    Defaults to "send" on uncertainty or error.
    """
    contact_name = ticket_fields.get("contact_name", "")
    subject = ticket_fields.get("subject", "")
    prompt = (
        "A dev flagged this ticket as 'user education': the client"
        " misunderstands how a feature works, and we should explain it."
        " Before we email the client, decide whether the explanation"
        " should actually go out.\n\n"
        "Use the dev's notes to understand WHAT needs explaining, then"
        " read the conversation thread and decide:\n"
        "- 'send': we have NOT yet given the client this explanation (or"
        " they replied with a new question that needs a fresh answer).\n"
        "- 'skip_already_sent': we have ALREADY explained this same thing"
        " in the thread and the client has not asked anything new since."
        " Sending again would be a duplicate.\n"
        "Weigh timing and order (who said what, and when). When unsure,"
        " choose 'send'.\n\n"
        f"Subject: {subject}\nClient: {contact_name}\n\n"
        "What the dev wants explained (their notes):\n"
        f"{engineer_context or '(none provided)'}\n\n"
        "Conversation thread (oldest to newest, with timestamps):\n"
        f"{conversations_text}\n\n"
        "Return valid JSON only, no prose, no code fences:\n"
        "{\n"
        '  "recommendation": "send" | "skip_already_sent",\n'
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
        print(f"[USER ED] State review failed: {e}")
        return {
            "recommendation": "send",
            "reason": f"review failed ({e}); defaulting to send",
            "last_relevant": "",
        }
    rec = str(data.get("recommendation") or "").strip().lower()
    if rec not in ("send", "skip_already_sent"):
        rec = "send"
    return {
        "recommendation": rec,
        "reason": str(data.get("reason", "")),
        "last_relevant": str(data.get("last_relevant", "")),
    }


# ---------------------------------------------------------------------------
# Slack records
# ---------------------------------------------------------------------------

def _education_record_message(
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
    """Build the informational Slack record for a user-education outcome."""
    zoho_url = (
        "https://desk.zoho.com/support/vomevolunteer"
        f"/ShowHomePage.do#Cases/dv/{zoho_ticket_id}"
    ) if zoho_ticket_id else ""
    clickup_url = (
        f"https://app.clickup.com/t/{clickup_task_id}"
    ) if clickup_task_id else ""

    if kind == "sent":
        head = (
            f":mortar_board: *User education — #{ticket_number}"
            " — auto-sent (Vic)*"
        )
        sub = (
            f"*{engineer_name} flagged this as a misunderstanding. Vic"
            " emailed the explanation and closed the ticket.*"
        )
    else:  # skip_already_sent
        head = (
            f":no_bell: *User education — #{ticket_number}"
            " — no email sent*"
        )
        sub = (
            f"*{engineer_name} flagged this, but we've already explained"
            " this to the client. Skipped, no duplicate sent.*"
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
        lines.append("Zoho + ClickUp -> Closed.")
    else:
        if reason:
            lines.append(f"Why: {reason}")
        if last_relevant:
            lines.append(f"Last relevant message: {last_relevant}")
        lines.append("Closed -- explanation already sent; no duplicate email.")
        lines.append(_SEP)
    return "\n".join(lines)


def _post_record(
    zoho_ticket_id: str,
    clickup_task_id: str,
    text: str,
    status: str | None,
    ticket_fields: dict,
    thread_ts: str | None,
) -> None:
    """Post a user-education outcome record to Slack.

    When `status` is provided, also set the thread status; pass None to
    leave the thread status untouched (used for the skip outcome).
    """
    if thread_ts:
        thread_entry = get_thread(thread_ts) or {}
        channel = thread_entry.get("channel") or CHANNEL_FINAL_REVIEW
        try:
            _slack.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text
            )
        except SlackApiError as e:
            print(f"[USER ED] Record post failed: {e.response['error']}")
        if status is not None:
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
        if status is not None:
            update_thread(new_ts, status=status)
    except SlackApiError as e:
        print(f"[USER ED] Record post failed: {e.response['error']}")


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def handle_user_education(task_id: str, engineer_name: str) -> bool:
    """Process a ClickUp task moving to 'user education'.

    AUTO-SENDS a plain-language explanation (signed Vic) to the client and
    closes the ticket on both ClickUp and Zoho. Skips the email when the
    same explanation was already sent (Slack note only). Falls back to a
    Slack review draft if it cannot auto-send. Returns True on success.
    """
    print(
        f"[USER ED] Task {task_id} set to user education"
        f" by {engineer_name}"
    )

    # 1. Fetch ClickUp task
    task = _get_clickup_task(task_id)
    if not task:
        print(f"[USER ED] Could not fetch task {task_id}")
        return False
    task_title = task.get("name", task_id)

    # 2. Dev context (task description + comments) — the explanation source
    comments = _get_clickup_task_comments(task_id)
    engineer_context = _format_engineer_context(task, comments)

    # 3. Extract Zoho ticket ID
    zoho_ticket_id = _extract_zoho_ticket_id(task)
    if not zoho_ticket_id:
        print(
            f"[USER ED] No Zoho Ticket Link on task"
            f" {task_id} ({task_title})"
        )
        return False
    print(f"[USER ED] Zoho ticket ID: {zoho_ticket_id}")

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

    # 6. Pre-send review — has this already been explained?
    assessment = _assess_education_state(
        fields, conversations_text, engineer_context
    )
    rec = assessment["recommendation"]
    print(
        f"[USER ED] State review — ticket {zoho_ticket_id}:"
        f" recommendation={rec} reason={assessment['reason']}"
    )

    # 6a. Already explained -> skip the duplicate email, but STILL clear the
    # "user education" trigger column so the task doesn't linger there looking
    # unprocessed. The explanation was already delivered, so close it just
    # like the send path (a later client reply reopens the Zoho ticket).
    if rec == "skip_already_sent":
        _set_clickup_status(task_id, CU_WRITE_CLOSED_TITLE)
        _set_zoho_status(zoho_ticket_id, ZOHO_CLOSED)
        text = _education_record_message(
            "skip_already_sent", ticket_number, engineer_name,
            ticket_fields=fields, zoho_ticket_id=zoho_ticket_id,
            clickup_task_id=task_id, reason=assessment["reason"],
            last_relevant=assessment["last_relevant"],
        )
        _post_record(
            zoho_ticket_id, task_id, text, THREAD_CLOSED, fields, thread_ts
        )
        print(
            f"[USER ED] Already explained — closed without"
            f" duplicate email for {task_id}"
        )
        return True

    # 7. Generate the Vic explanation from the dev's notes
    try:
        draft = _generate_education_message(
            ticket_fields=fields,
            conversations_text=conversations_text,
            engineer_context=engineer_context,
            language=language,
        )
    except Exception as e:
        print(f"[USER ED] Claude draft failed: {e}")
        first_name = (fields.get("contact_name") or "there").split()[0]
        draft = (
            f"Hi {first_name}, thanks for reaching out. We looked into"
            " this and wanted to walk you through how this part of Vome"
            " works so you can get what you need. If anything is still"
            " unclear after this, just reply and we'll be glad to help.\n"
            + signature("vic")
        )

    # 8. Auto-send the explanation to the client (signed Vic)
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
        # The explanation is the resolution -> close both sides. A later
        # client reply reopens the Zoho ticket through the normal pipeline.
        _set_clickup_status(task_id, CU_WRITE_CLOSED_TITLE)
        _set_zoho_status(zoho_ticket_id, ZOHO_CLOSED)
        text = _education_record_message(
            "sent", ticket_number, engineer_name,
            ticket_fields=fields, zoho_ticket_id=zoho_ticket_id,
            clickup_task_id=task_id, draft=draft,
        )
        _post_record(
            zoho_ticket_id, task_id, text,
            THREAD_CLOSED, fields, thread_ts,
        )
        print(
            f"[USER ED] Auto-sent + closed — ticket {zoho_ticket_id},"
            f" task {task_id}"
        )
        return True

    # 9. Fallback — cannot auto-send (no contact email, empty draft, or send
    # error). Post the draft to Slack with confirm/send/cancel for manual send.
    print(
        f"[USER ED] Auto-send unavailable for ticket {zoho_ticket_id}"
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
    update_thread(thread_ts, status=THREAD_CLOSED)
    print(f"[USER ED] Review draft posted, thread_ts={thread_ts}")
    return True
