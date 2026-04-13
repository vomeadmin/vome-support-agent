"""
ops/close.py

POST /ops/ticket/{zoho_ticket_id}/close — closes a ticket completely.
"""

from datetime import datetime, timezone

from database import get_thread_by_ticket_id, update_thread
from ops.zoho_sync import (
    RESOLUTION_MAP,
    FIELD_RESOLUTION,
    set_zoho_status,
    set_clickup_status,
    set_clickup_custom_field,
    send_zoho_reply,
    post_internal_note,
    get_zoho_ticket_contact_email,
)
from ops.draft import generate_draft


def close_ticket(
    zoho_ticket_id: str,
    send_closure_note: bool = True,
    resolution: str = "completed",
    closure_message: str | None = None,
) -> dict:
    """
    Close a ticket.

    1. Optionally generate + send a closure note
    2. Set Zoho status to Closed
    3. Set ClickUp status to Done + resolution field
    4. Update PostgreSQL
    """
    errors = []

    # -----------------------------------------------------------------------
    # 1. Send closure note if requested
    # -----------------------------------------------------------------------
    if send_closure_note:
        message = closure_message
        if not message:
            # Auto-generate a closure note
            draft = generate_draft(zoho_ticket_id, draft_type="close")
            message = draft.get("draft", "")

        if message and not message.startswith("(Draft generation failed"):
            contact_email = get_zoho_ticket_contact_email(zoho_ticket_id)
            result = send_zoho_reply(zoho_ticket_id, message, contact_email)
            if not result:
                errors.append("Failed to send closure note")

    # -----------------------------------------------------------------------
    # 2. Zoho -> Closed
    # -----------------------------------------------------------------------
    if not set_zoho_status(zoho_ticket_id, "Closed"):
        errors.append("Failed to set Zoho status to Closed")

    # -----------------------------------------------------------------------
    # 3. ClickUp -> Done + resolution
    # -----------------------------------------------------------------------
    db_row = get_thread_by_ticket_id(zoho_ticket_id)
    clickup_task_id = ""
    if db_row:
        _, row_data = db_row
        clickup_task_id = row_data.get("clickup_task_id", "")

    if clickup_task_id:
        if not set_clickup_status(clickup_task_id, "done"):
            errors.append("Failed to set ClickUp status to Done")

        # Set resolution custom field
        resolution_option_id = RESOLUTION_MAP.get(resolution)
        if resolution_option_id:
            set_clickup_custom_field(
                clickup_task_id, FIELD_RESOLUTION, resolution_option_id
            )

    # -----------------------------------------------------------------------
    # 4. Internal note
    # -----------------------------------------------------------------------
    post_internal_note(
        zoho_ticket_id,
        f"Ticket closed via Command Center. Resolution: {resolution}.",
    )

    # -----------------------------------------------------------------------
    # 5. Update DB
    # -----------------------------------------------------------------------
    if db_row:
        thread_ts = db_row[0]
        try:
            update_thread(
                thread_ts,
                status="closed",
                last_action="closed",
                last_action_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            errors.append(f"DB update failed: {e}")

    return {
        "success": len(errors) == 0,
        "zoho_new_status": "Closed",
        "clickup_new_status": "done",
        "resolution": resolution,
        "errors": errors,
        "message": f"Ticket closed. Resolution: {resolution}.",
    }
