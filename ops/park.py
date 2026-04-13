"""
ops/park.py

POST /ops/ticket/{zoho_ticket_id}/park — parks a ticket (On Hold + Sleeping).
"""

from datetime import datetime, timezone

from database import get_thread_by_ticket_id, update_thread
from ops.zoho_sync import (
    FIELD_WAKE_DATE,
    set_zoho_status,
    set_clickup_status,
    set_clickup_custom_field,
    post_internal_note,
)


def park_ticket(
    zoho_ticket_id: str,
    note: str = "",
    wake_date: str | None = None,
) -> dict:
    """
    Park a ticket.

    1. Zoho -> On Hold
    2. ClickUp -> Sleeping + Wake Date field
    3. Internal note
    4. Update PostgreSQL
    """
    errors = []

    # Zoho -> On Hold
    if not set_zoho_status(zoho_ticket_id, "On Hold"):
        errors.append("Failed to set Zoho status to On Hold")

    # ClickUp -> Sleeping
    db_row = get_thread_by_ticket_id(zoho_ticket_id)
    clickup_task_id = ""
    if db_row:
        _, row_data = db_row
        clickup_task_id = row_data.get("clickup_task_id", "")

    if clickup_task_id:
        if not set_clickup_status(clickup_task_id, "sleeping"):
            errors.append("Failed to set ClickUp status to Sleeping")

        # Set wake date if provided
        if wake_date:
            # ClickUp date fields expect Unix timestamp in ms
            try:
                dt = datetime.strptime(wake_date, "%Y-%m-%d")
                ts_ms = int(dt.timestamp() * 1000)
                set_clickup_custom_field(
                    clickup_task_id, FIELD_WAKE_DATE, ts_ms
                )
            except ValueError:
                errors.append(f"Invalid wake_date format: {wake_date}")

    # Internal note
    note_text = "Parked by Sam via Command Center."
    if note:
        note_text += f" Note: {note}"
    if wake_date:
        note_text += f" Wake: {wake_date}"
    post_internal_note(zoho_ticket_id, note_text)

    # Update DB
    if db_row:
        thread_ts = db_row[0]
        update_fields = {
            "status": "on_hold",
            "parked": True,
            "last_action": "parked",
            "last_action_at": datetime.now(timezone.utc),
        }
        if wake_date:
            update_fields["wake_date"] = wake_date
        try:
            update_thread(thread_ts, **update_fields)
        except Exception as e:
            errors.append(f"DB update failed: {e}")

    return {
        "success": len(errors) == 0,
        "zoho_new_status": "On Hold",
        "clickup_new_status": "sleeping",
        "wake_date": wake_date,
        "errors": errors,
        "message": (
            "Ticket parked."
            + (f" Wake date: {wake_date}" if wake_date else "")
        ),
    }
