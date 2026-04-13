"""
ops/thread.py

GET /ops/ticket/{zoho_ticket_id}/thread — returns the full conversation
thread for a ticket (for the slide-in panel).
"""

import re
from datetime import datetime

from agent import (
    ZOHO_ORG_ID,
    _zoho_desk_call,
    _unwrap_mcp_result,
    fetch_ticket_conversations,
    fetch_ticket_from_zoho,
)
from database import get_thread_by_ticket_id
from ops.zoho_sync import get_clickup_comments


def fetch_thread(zoho_ticket_id: str) -> dict:
    """
    Fetch the full thread for a ticket:
    1. Zoho Desk conversations (client messages, outbound replies, internal notes)
    2. ClickUp task comments (engineer notes)
    """
    # -----------------------------------------------------------------------
    # Zoho ticket metadata
    # -----------------------------------------------------------------------
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

    # Ticket metadata for the panel header
    subject = ticket_data.get("subject", "")
    ticket_number = str(ticket_data.get("ticketNumber", ""))
    status = ticket_data.get("status", "")

    # CRM data from DB
    db_row = get_thread_by_ticket_id(zoho_ticket_id)
    crm = {}
    clickup_task_id = ""
    if db_row:
        _, row_data = db_row
        crm = row_data.get("crm", {})
        clickup_task_id = row_data.get("clickup_task_id", "")

    org_name = crm.get("org_name", "") or crm.get("account_name", "")
    tier = crm.get("offering", "")
    arr = crm.get("arr", 0)

    # Assignee
    assignee = ticket_data.get("assignee") or {}
    assignee_name = ""
    if assignee:
        a_first = assignee.get("firstName") or ""
        a_last = assignee.get("lastName") or ""
        assignee_name = f"{a_first} {a_last}".strip()

    # -----------------------------------------------------------------------
    # Zoho conversations
    # -----------------------------------------------------------------------
    conv_result = fetch_ticket_conversations(zoho_ticket_id)
    conversations = _unwrap_mcp_result(conv_result) if conv_result else []

    if isinstance(conversations, dict):
        conversations = conversations.get("data", [])
    if not isinstance(conversations, list):
        conversations = []

    threads = []
    for entry in conversations:
        author_info = entry.get("author") or {}
        author_name = author_info.get("name", "Unknown")
        author_email = author_info.get("email", "")

        timestamp = entry.get("createdTime") or entry.get("sendDateTime", "")
        content_raw = entry.get("content") or entry.get("summary") or ""
        # Strip HTML
        content = re.sub(r"<[^>]+>", "", content_raw).strip()

        is_public = entry.get("isPublic", True)
        direction = entry.get("direction", "")

        # Determine direction label
        if direction == "in":
            direction_label = "inbound"
        elif direction == "out":
            direction_label = "outbound"
        else:
            direction_label = "internal" if not is_public else "outbound"

        # Attachments
        attachments = []
        for att in entry.get("attachments") or []:
            attachments.append({
                "name": att.get("name", "attachment"),
                "url": att.get("href", ""),
                "size": att.get("size", 0),
            })

        threads.append({
            "id": str(entry.get("id", "")),
            "direction": direction_label,
            "author": author_name,
            "author_email": author_email,
            "timestamp": timestamp,
            "content": content,
            "is_internal": not is_public,
            "attachments": attachments,
        })

    # -----------------------------------------------------------------------
    # ClickUp comments
    # -----------------------------------------------------------------------
    clickup_comments_out = []
    if clickup_task_id:
        raw_comments = get_clickup_comments(clickup_task_id)
        for c in raw_comments:
            user = c.get("user", {})
            comment_text_parts = []
            for ct in c.get("comment", []):
                if ct.get("type") == "text":
                    comment_text_parts.append(ct.get("text", ""))
            text_combined = "".join(comment_text_parts).strip()
            if not text_combined:
                continue

            # Convert ClickUp timestamp (ms) to ISO
            ts_ms = c.get("date", 0)
            try:
                ts_str = datetime.fromtimestamp(
                    int(ts_ms) / 1000
                ).isoformat() + "Z"
            except Exception:
                ts_str = ""

            clickup_comments_out.append({
                "author": user.get("username", "Unknown"),
                "timestamp": ts_str,
                "text": text_combined,
            })

    return {
        "zoho_ticket_id": zoho_ticket_id,
        "ticket_number": ticket_number,
        "subject": subject,
        "status": status,
        "contact_name": contact_name,
        "contact_email": contact_email,
        "org_name": org_name,
        "tier": tier,
        "arr": arr,
        "assignee_name": assignee_name,
        "clickup_task_id": clickup_task_id,
        "threads": threads,
        "clickup_comments": clickup_comments_out,
    }
