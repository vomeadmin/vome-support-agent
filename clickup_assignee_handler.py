"""
clickup_assignee_handler.py

Handles ClickUp taskAssigneeUpdated webhook events.

When a task's assignee changes in ClickUp:
  1. Map the new ClickUp assignee to a Zoho agent ID
  2. Look up the Zoho ticket via the task's Zoho Ticket Link field
  3. Update the Zoho ticket assignee
  4. Post an internal note documenting the reassignment
  5. If escalated from Sanjay -> OnlyG, ping #vome-support-engineering
"""

import os
import re

import httpx

from agent import (
    ZOHO_ORG_ID,
    _zoho_mcp_call,
    _unwrap_mcp_result,
)
from slack import post_to_engineering

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

# Zoho Ticket Link custom field ID
FIELD_ZOHO_TICKET_LINK = "4776215b-c725-4d79-8f20-c16f0f0145ac"

# ---------------------------------------------------------------------------
# ClickUp user ID <-> Zoho agent ID mapping
# ---------------------------------------------------------------------------

CLICKUP_TO_ZOHO = {
    4434086: "569440000023159001",   # Sanjay
    49257687: "569440000023160001",  # OnlyG
    3691763: "569440000000139001",   # Sam
}

CLICKUP_TO_NAME = {
    4434086: "Sanjay",
    49257687: "OnlyG",
    3691763: "Sam",
}

CLICKUP_SANJAY = 4434086
CLICKUP_ONLYG = 49257687


# ---------------------------------------------------------------------------
# ClickUp helpers
# ---------------------------------------------------------------------------

def _get_clickup_task(task_id: str) -> dict | None:
    """Fetch a ClickUp task by ID."""
    if not CLICKUP_API_TOKEN:
        print("[ASSIGNEE] CLICKUP_API_TOKEN not set -- cannot fetch task")
        return None
    try:
        r = httpx.get(
            f"{CLICKUP_BASE}/task/{task_id}",
            headers={"Authorization": CLICKUP_API_TOKEN},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[ASSIGNEE] ClickUp get task failed ({task_id}): {e}")
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
    return None


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def handle_assignee_updated(payload: dict) -> None:
    """Process a ClickUp taskAssigneeUpdated webhook event.

    Syncs the assignee change back to the linked Zoho Desk ticket,
    posts an internal note, and alerts on Sanjay -> OnlyG escalations.
    """
    event = payload.get("event", "")
    if event != "taskAssigneeUpdated":
        return

    task_id = payload.get("task_id", "")
    if not task_id:
        print("[ASSIGNEE] No task_id in payload -- ignoring")
        return

    # --- Determine new and previous assignee from history_items ---
    new_clickup_id = None
    prev_clickup_id = None

    for item in payload.get("history_items", []):
        if item.get("field") != "assignee":
            continue
        after = item.get("after") or {}
        before = item.get("before") or {}

        # after/before can be a dict with "id" or an int directly
        if isinstance(after, dict):
            try:
                new_clickup_id = int(after.get("id", 0))
            except (ValueError, TypeError):
                pass
        elif isinstance(after, (int, float)):
            new_clickup_id = int(after)

        if isinstance(before, dict):
            try:
                prev_clickup_id = int(before.get("id", 0))
            except (ValueError, TypeError):
                pass
        elif isinstance(before, (int, float)):
            prev_clickup_id = int(before)
        break

    if not new_clickup_id:
        print(f"[ASSIGNEE] Could not extract new assignee from task {task_id} -- ignoring")
        return

    # --- Map to Zoho agent ID ---
    zoho_agent_id = CLICKUP_TO_ZOHO.get(new_clickup_id)
    new_name = CLICKUP_TO_NAME.get(new_clickup_id, f"ClickUp user {new_clickup_id}")

    if not zoho_agent_id:
        print(
            f"[ASSIGNEE] Unknown ClickUp user {new_clickup_id} "
            f"on task {task_id} -- not syncing to Zoho"
        )
        return

    print(f"[ASSIGNEE] Task {task_id} reassigned to {new_name} (ClickUp {new_clickup_id})")

    # --- Fetch ClickUp task to get Zoho Ticket Link ---
    task = _get_clickup_task(task_id)
    if not task:
        return

    zoho_ticket_id = _extract_zoho_ticket_id(task)
    if not zoho_ticket_id:
        print(f"[ASSIGNEE] No Zoho Ticket Link on task {task_id} -- cannot sync")
        return

    task_name = task.get("name", "")
    task_url = task.get("url") or f"https://app.clickup.com/t/{task_id}"
    zoho_url = (
        f"https://desk.zoho.com/support/vomevolunteer"
        f"/ShowHomePage.do#Cases/dv/{zoho_ticket_id}"
    )

    # --- Update Zoho ticket assignee ---
    update_result = _zoho_mcp_call("ZohoDesk_updateTicket", {
        "body": {"assigneeId": zoho_agent_id},
        "path_variables": {"ticketId": str(zoho_ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })

    if not update_result:
        print(f"[ASSIGNEE] Failed to update Zoho ticket {zoho_ticket_id} assignee")
        return

    data = _unwrap_mcp_result(update_result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"[ASSIGNEE] Zoho update failed for ticket {zoho_ticket_id}: {data}")
        return

    print(f"[ASSIGNEE] Zoho ticket {zoho_ticket_id} assignee updated to {new_name}")

    # --- Post internal note ---
    note_content = (
        f"Ticket reassigned to {new_name} following ClickUp task update."
    )
    _zoho_mcp_call("ZohoDesk_createTicketComment", {
        "body": {
            "content": note_content,
            "contentType": "plainText",
            "isPublic": False,
            "attachmentIds": [],
        },
        "path_variables": {"ticketId": str(zoho_ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    print(f"[ASSIGNEE] Internal note posted on Zoho ticket {zoho_ticket_id}")

    # --- Escalation alert: Sanjay -> OnlyG ---
    if new_clickup_id == CLICKUP_ONLYG and prev_clickup_id == CLICKUP_SANJAY:
        # Extract ticket number from task name if available
        ticket_num_match = re.search(r"#(\d+)", task_name)
        ticket_display = f"#{ticket_num_match.group(1)}" if ticket_num_match else f"#{zoho_ticket_id}"

        escalation_msg = (
            f"Ticket {ticket_display} escalated from Sanjay to OnlyG: "
            f"{task_name}\n{zoho_url} | {task_url}"
        )
        try:
            post_to_engineering(escalation_msg)
            print(f"[ASSIGNEE] Escalation alert sent for ticket {zoho_ticket_id}")
        except Exception as e:
            print(f"[ASSIGNEE] Escalation Slack post failed: {e}")
