"""
ops/assign.py

POST /ops/ticket/{zoho_ticket_id}/assign — assigns a ticket to an
engineer without necessarily sending a reply.
"""

from datetime import datetime, timezone

from database import get_thread_by_ticket_id, update_thread
from ops.zoho_sync import (
    TEAM,
    set_zoho_status,
    set_zoho_owner,
    set_clickup_status,
    set_clickup_assignee,
    post_internal_note,
)
from ops.draft import generate_draft
from ops.send import send_reply


def assign_ticket(
    zoho_ticket_id: str,
    engineer: str,
    send_ack: bool = True,
) -> dict:
    """
    Assign a ticket to an engineer.

    1. Resolve engineer to Zoho + ClickUp IDs
    2. Update Zoho ticket owner
    3. Update Zoho status to Processing
    4. Update ClickUp assignee
    5. Update ClickUp status to In Progress
    6. If send_ack: generate + send an acknowledgment reply
    7. Update PostgreSQL
    """
    engineer_key = engineer.strip().lower()
    if engineer_key not in TEAM:
        return {
            "success": False,
            "message": f"Unknown engineer: {engineer}. Valid: {', '.join(TEAM.keys())}",
        }

    eng = TEAM[engineer_key]
    eng_name = eng["name"]
    eng_zoho_id = eng["zoho_id"]
    eng_clickup_id = eng["clickup_id"]

    errors = []

    # Zoho: set owner + status
    if not set_zoho_owner(zoho_ticket_id, eng_zoho_id):
        errors.append("Failed to set Zoho owner")
    if not set_zoho_status(zoho_ticket_id, "Processing"):
        errors.append("Failed to set Zoho status to Processing")

    # ClickUp
    db_row = get_thread_by_ticket_id(zoho_ticket_id)
    clickup_task_id = ""
    if db_row:
        _, row_data = db_row
        clickup_task_id = row_data.get("clickup_task_id", "")

    if clickup_task_id:
        if not set_clickup_assignee(clickup_task_id, eng_clickup_id):
            errors.append("Failed to set ClickUp assignee")
        if not set_clickup_status(clickup_task_id, "in progress"):
            errors.append("Failed to set ClickUp status")

    # Internal note
    post_internal_note(
        zoho_ticket_id,
        f"Assigned to {eng_name} via Command Center.",
    )

    # Update DB
    if db_row:
        thread_ts = db_row[0]
        try:
            update_thread(
                thread_ts,
                zoho_assignee_id=eng_zoho_id,
                clickup_assignee_id=eng_clickup_id,
                status="processing",
                last_action=f"assigned_{engineer_key}",
                last_action_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            errors.append(f"DB update failed: {e}")

    # Send acknowledgment reply if requested
    ack_result = None
    if send_ack:
        draft = generate_draft(zoho_ticket_id, draft_type="acknowledge")
        if draft.get("draft") and not draft["draft"].startswith("(Draft generation failed"):
            ack_result = send_reply(
                zoho_ticket_id=zoho_ticket_id,
                content=draft["draft"],
                zoho_status_after="Processing",
                clickup_action="in_progress",
                assignee_clickup_id=eng_clickup_id,
                assignee_zoho_id=eng_zoho_id,
            )
            if ack_result and not ack_result.get("success"):
                errors.extend(ack_result.get("errors", []))

    return {
        "success": len(errors) == 0,
        "engineer": eng_name,
        "zoho_owner": eng_zoho_id,
        "clickup_assignee": eng_clickup_id,
        "ack_sent": bool(ack_result and ack_result.get("success")),
        "errors": errors,
        "message": (
            f"Assigned to {eng_name}."
            + (" Acknowledgment sent." if ack_result and ack_result.get("success") else "")
        ),
    }
