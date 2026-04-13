"""
ops/draft.py

POST /ops/ticket/{zoho_ticket_id}/draft — generates a Claude draft reply.
"""

import os
from pathlib import Path

import anthropic

from agent import (
    SYSTEM_PROMPT,
    ZOHO_ORG_ID,
    _zoho_desk_call,
    _unwrap_mcp_result,
    _format_conversations,
    _detect_language,
    fetch_ticket_conversations,
    fetch_ticket_from_zoho,
)
from database import get_thread_by_ticket_id
from ops.zoho_sync import get_clickup_task, get_clickup_comments

_anthropic = anthropic.Anthropic()

# Draft type -> suggested Zoho status + ClickUp action
DRAFT_DEFAULTS = {
    "acknowledge": {
        "zoho_status": "Processing",
        "clickup_action": "in_progress",
    },
    "request_info": {
        "zoho_status": "On Hold",
        "clickup_action": "waiting_on_client",
    },
    "resolution": {
        "zoho_status": "Final Review",
        "clickup_action": "done",
    },
    "close": {
        "zoho_status": "Closed",
        "clickup_action": "done",
    },
    "admin_action": {
        "zoho_status": "Processing",
        "clickup_action": "waiting_on_client",
    },
    "escalation": {
        "zoho_status": "Processing",
        "clickup_action": "in_progress",
    },
}


def generate_draft(
    zoho_ticket_id: str,
    draft_type: str = "request_info",
    redraft_instruction: str = "",
    engineer_note_override: str = "",
) -> dict:
    """
    Generate a Claude draft reply for a ticket.

    1. Fetch full Zoho thread
    2. Fetch ClickUp task + comments
    3. Fetch CRM context from DB
    4. Build prompt and call Claude
    5. Return draft + suggested statuses
    """
    # -----------------------------------------------------------------------
    # Gather context
    # -----------------------------------------------------------------------

    # Zoho ticket
    ticket_result = fetch_ticket_from_zoho(zoho_ticket_id)
    ticket_data = _unwrap_mcp_result(ticket_result) if ticket_result else {}
    if not isinstance(ticket_data, dict):
        ticket_data = {}

    contact = ticket_data.get("contact") or {}
    contact_name = ""
    first = contact.get("firstName") or ""
    last = contact.get("lastName") or ""
    if first or last:
        contact_name = f"{first} {last}".strip()
    contact_email = contact.get("email", "")
    subject = ticket_data.get("subject", "")

    # Zoho conversations
    conv_result = fetch_ticket_conversations(zoho_ticket_id)
    formatted_thread = _format_conversations(conv_result)

    # DB context
    db_row = get_thread_by_ticket_id(zoho_ticket_id)
    crm = {}
    clickup_task_id = ""
    classification = {}
    if db_row:
        _, row_data = db_row
        crm = row_data.get("crm", {})
        clickup_task_id = row_data.get("clickup_task_id", "")
        classification = row_data.get("classification", {})

    org_name = crm.get("org_name", "") or crm.get("account_name", "")
    tier = crm.get("offering", "Unknown")
    arr = crm.get("arr", 0) or 0

    # ClickUp context
    cu_status = ""
    engineer_comments = ""
    if clickup_task_id:
        cu_task = get_clickup_task(clickup_task_id)
        if cu_task:
            cu_status = (cu_task.get("status", {}).get("status") or "").lower()

        comments = get_clickup_comments(clickup_task_id)
        comment_lines = []
        for c in comments:
            user = c.get("user", {}).get("username", "Unknown")
            text_parts = []
            for ct in c.get("comment", []):
                if ct.get("type") == "text":
                    text_parts.append(ct.get("text", ""))
            text = "".join(text_parts).strip()
            if text:
                comment_lines.append(f"{user}: {text}")
        engineer_comments = "\n".join(comment_lines)

    if engineer_note_override:
        engineer_comments = engineer_note_override

    # Detect language
    language = _detect_language(formatted_thread) or "en"

    # -----------------------------------------------------------------------
    # Build Claude prompt
    # -----------------------------------------------------------------------
    draft_instructions = _get_draft_instructions(draft_type)

    user_prompt = f"""You are drafting a support reply for ticket #{ticket_data.get('ticketNumber', zoho_ticket_id)}.

## Ticket Details
- Subject: {subject}
- Contact: {contact_name} ({contact_email})
- Organization: {org_name}
- Tier: {tier}
- ARR: ${arr}/yr
- Current Zoho status: {ticket_data.get('status', 'Unknown')}
- Current ClickUp status: {cu_status}

## Full Conversation Thread
{formatted_thread}

## Engineer Notes (ClickUp comments — INTERNAL, do not expose to client)
{engineer_comments or '(No engineer notes)'}

## Classification
{classification}

## Draft Type
{draft_type}: {draft_instructions}

{f'## Redraft Instruction{chr(10)}{redraft_instruction}' if redraft_instruction else ''}

## Rules
- Match the client's language (detected: {language}). If the conversation is in French, reply in French.
- Never use em-dashes (—). Use regular dashes or rewrite.
- Sign as "Sam | Vome support" with "support.vomevolunteer.com" below.
- Be warm, specific, and concise. Reference specific details from the thread.
- If engineer notes mention what they need from the client, translate that into friendly client-facing language. Never expose internal engineering language.
- Do NOT include a subject line — just the reply body.
- Do NOT include "Re:" or ticket numbers in the reply text.

Generate ONLY the reply text. No meta-commentary."""

    # -----------------------------------------------------------------------
    # Call Claude
    # -----------------------------------------------------------------------
    try:
        response = _anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        draft_text = response.content[0].text.strip()
    except Exception as e:
        print(f"[OPS] Claude draft generation failed: {e}")
        draft_text = f"(Draft generation failed: {e})"

    # -----------------------------------------------------------------------
    # Return draft + suggestions
    # -----------------------------------------------------------------------
    defaults = DRAFT_DEFAULTS.get(draft_type, DRAFT_DEFAULTS["request_info"])

    return {
        "draft": draft_text,
        "suggested_zoho_status": defaults["zoho_status"],
        "suggested_clickup_action": defaults["clickup_action"],
        "draft_type": draft_type,
        "language_detected": language,
    }


def _get_draft_instructions(draft_type: str) -> str:
    """Return Claude instructions specific to the draft type."""
    instructions = {
        "acknowledge": (
            "Draft an acknowledgment reply. Let the client know we've received "
            "their issue and our team is looking into it. Set expectations that "
            "we'll follow up soon."
        ),
        "request_info": (
            "Draft a request for more information. Be specific about exactly "
            "what information is missing and why we need it. Reference what "
            "we already know from the thread to show we've read their message."
        ),
        "resolution": (
            "Draft a resolution message. Explain what was fixed or resolved, "
            "in client-friendly terms. Ask the client to confirm the fix works "
            "on their end."
        ),
        "close": (
            "Draft a brief, warm closure note. Thank the client and let them "
            "know they can reach out again if needed."
        ),
        "admin_action": (
            "Draft a message explaining that this requires action from the "
            "organization's admin. Be specific about what the admin needs to "
            "do, with clear steps if possible."
        ),
        "escalation": (
            "Draft an escalation acknowledgment for a high-priority issue. "
            "Convey urgency and commitment. Set a clear expectation for when "
            "the client will hear back."
        ),
    }
    return instructions.get(draft_type, instructions["request_info"])
