"""
ops/send.py

POST /ops/ticket/{zoho_ticket_id}/send — sends a reply and syncs
status across Zoho + ClickUp simultaneously.
"""

import os
from datetime import datetime, timezone

from database import get_thread_by_ticket_id, update_thread
from ops.zoho_sync import (
    TEAM,
    send_zoho_reply,
    set_zoho_status,
    set_zoho_owner,
    post_internal_note,
    set_clickup_status,
    set_clickup_assignee,
    get_zoho_ticket_contact_email,
)

# Slack (optional audit trail)
SLACK_CHANNEL_AGENT_LOG = os.environ.get("SLACK_CHANNEL_AGENT_LOG", "")

# ClickUp action -> status name mapping
CLICKUP_ACTION_MAP = {
    "leave": None,
    "close_temporarily": "waiting on client",
    "in_progress": "in progress",
    "waiting_on_client": "waiting on client",
    "done": "done",
    "sleeping": "sleeping",
}


def send_reply(
    zoho_ticket_id: str,
    content: str,
    zoho_status_after: str = "On Hold",
    clickup_action: str = "leave",
    assignee_clickup_id: int | None = None,
    assignee_zoho_id: str | None = None,
) -> dict:
    """
    Send a reply on a Zoho ticket and sync status to both Zoho + ClickUp.

    Steps:
    1. Send reply via Zoho
    2. Set Zoho ticket status
    3. Set Zoho ticket owner (if assignee provided)
    4. Update ClickUp task status
    5. Update ClickUp assignee (if provided)
    6. Post internal note confirming action
    7. Update PostgreSQL
    """
    results = {
        "success": False,
        "zoho_thread_id": None,
        "zoho_new_status": zoho_status_after,
        "clickup_new_status": None,
        "message": "",
        "errors": [],
    }

    # -----------------------------------------------------------------------
    # 1. Get contact email for the reply
    # -----------------------------------------------------------------------
    contact_email = get_zoho_ticket_contact_email(zoho_ticket_id)

    # -----------------------------------------------------------------------
    # 2. Send the reply via Zoho
    # -----------------------------------------------------------------------
    reply_result = send_zoho_reply(zoho_ticket_id, content, contact_email)
    if not reply_result:
        results["errors"].append("Failed to send reply via Zoho")
        results["message"] = "Reply send failed"
        return results

    results["zoho_thread_id"] = (
        reply_result.get("id") if isinstance(reply_result, dict) else None
    )

    # -----------------------------------------------------------------------
    # 3. Set Zoho status
    # -----------------------------------------------------------------------
    if zoho_status_after:
        ok = set_zoho_status(zoho_ticket_id, zoho_status_after)
        if not ok:
            results["errors"].append(
                f"Failed to set Zoho status to {zoho_status_after}"
            )

    # -----------------------------------------------------------------------
    # 4. Set Zoho owner (if assignee provided)
    # -----------------------------------------------------------------------
    if assignee_zoho_id:
        ok = set_zoho_owner(zoho_ticket_id, assignee_zoho_id)
        if not ok:
            results["errors"].append("Failed to set Zoho ticket owner")

    # -----------------------------------------------------------------------
    # 5. Update ClickUp task status
    # -----------------------------------------------------------------------
    db_row = get_thread_by_ticket_id(zoho_ticket_id)
    clickup_task_id = ""
    if db_row:
        _, row_data = db_row
        clickup_task_id = row_data.get("clickup_task_id", "")

    cu_status_name = CLICKUP_ACTION_MAP.get(clickup_action)
    if cu_status_name and clickup_task_id:
        ok = set_clickup_status(clickup_task_id, cu_status_name)
        if ok:
            results["clickup_new_status"] = cu_status_name
        else:
            results["errors"].append(
                f"Failed to set ClickUp status to {cu_status_name}"
            )

    # -----------------------------------------------------------------------
    # 6. Update ClickUp assignee
    # -----------------------------------------------------------------------
    if assignee_clickup_id and clickup_task_id:
        ok = set_clickup_assignee(clickup_task_id, assignee_clickup_id)
        if not ok:
            results["errors"].append("Failed to set ClickUp assignee")

    # -----------------------------------------------------------------------
    # 7. Post internal note for audit trail
    # -----------------------------------------------------------------------
    note = (
        f"Reply sent via Command Center. "
        f"Zoho -> {zoho_status_after}."
    )
    if cu_status_name:
        note += f" ClickUp -> {cu_status_name}."
    post_internal_note(zoho_ticket_id, note)

    # -----------------------------------------------------------------------
    # 8. Update PostgreSQL
    # -----------------------------------------------------------------------
    if db_row:
        thread_ts = db_row[0]
        update_fields = {
            "status": zoho_status_after.lower().replace(" ", "_"),
            "last_action": "reply_sent",
            "last_action_at": datetime.now(timezone.utc),
        }
        if assignee_zoho_id:
            update_fields["zoho_assignee_id"] = assignee_zoho_id
        if assignee_clickup_id:
            update_fields["clickup_assignee_id"] = assignee_clickup_id
        try:
            update_thread(thread_ts, **update_fields)
        except Exception as e:
            results["errors"].append(f"DB update failed: {e}")

    # -----------------------------------------------------------------------
    # Result
    # -----------------------------------------------------------------------
    results["success"] = len(results["errors"]) == 0
    results["message"] = (
        f"Reply sent. Zoho -> {zoho_status_after}."
        + (f" ClickUp -> {cu_status_name}." if cu_status_name else "")
    )

    return results
