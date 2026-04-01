"""
clickup_needs_review_handler.py

Handles ClickUp taskStatusUpdated -> "Needs Review".

When an engineer sets a task to "Needs Review":
  1. Fetch the ClickUp task to get current assignee + Zoho ticket link
  2. Look up tier + complexity from thread_map
  3. Ping Sam in #vome-support-engineering with context
  4. Update thread_map status to "needs-review"
"""

import os
import re

import httpx

from database import (
    get_thread,
    get_thread_by_ticket_id,
    update_thread,
)
from slack import post_to_engineering

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

FIELD_ZOHO_TICKET_LINK = "4776215b-c725-4d79-8f20-c16f0f0145ac"

# ClickUp user ID -> display name
CLICKUP_USER_NAMES = {
    4434086: "Sanjay",
    49257687: "OnlyG",
    3691763: "Sam",
    4434980: "Ron",
}


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
        print(f"[NEEDS REVIEW] ClickUp get task failed ({task_id}): {e}")
        return None


def _extract_zoho_ticket_id(task: dict) -> str | None:
    """Extract Zoho ticket ID from the Zoho Ticket Link custom field."""
    for field in task.get("custom_fields") or []:
        if field.get("id") != FIELD_ZOHO_TICKET_LINK:
            continue
        value = field.get("value") or ""
        m = re.search(r"/dv/(\d+)", str(value))
        if m:
            return m.group(1)
        stripped = str(value).strip()
        if stripped.isdigit():
            return stripped
    return None


def _get_current_assignee(task: dict) -> tuple[int | None, str]:
    """Return (clickup_user_id, display_name) of the current assignee."""
    assignees = task.get("assignees") or []
    if not assignees:
        return (None, "Unassigned")
    first = assignees[0]
    uid = first.get("id")
    if uid:
        try:
            uid = int(uid)
        except (ValueError, TypeError):
            uid = None
    name = CLICKUP_USER_NAMES.get(uid, first.get("username", "Unknown"))
    return (uid, name)


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def handle_needs_review(task_id: str, engineer_name: str) -> bool:
    """Process a ClickUp task moving to 'Needs Review'.

    Pings Sam in #vome-support-engineering with full context.
    Returns True on success.
    """
    print(f"[NEEDS REVIEW] Task {task_id} set to Needs Review by {engineer_name}")

    # 1. Fetch ClickUp task
    task = _get_clickup_task(task_id)
    if not task:
        print(f"[NEEDS REVIEW] Could not fetch ClickUp task {task_id}")
        return False

    task_title = task.get("name", task_id)
    task_url = task.get("url") or f"https://app.clickup.com/t/{task_id}"

    # 2. Get current assignee
    assignee_id, assignee_name = _get_current_assignee(task)

    # 3. Extract Zoho ticket ID
    zoho_ticket_id = _extract_zoho_ticket_id(task)
    zoho_url = ""
    if zoho_ticket_id:
        zoho_url = (
            f"https://desk.zoho.com/support/vomevolunteer"
            f"/ShowHomePage.do#Cases/dv/{zoho_ticket_id}"
        )

    # 4. Look up tier + complexity from thread_map
    tier = "unknown"
    complexity = "unknown"
    ticket_number = zoho_ticket_id or task_id
    thread_ts = None

    if zoho_ticket_id:
        result = get_thread_by_ticket_id(zoho_ticket_id)
        if result:
            thread_ts = result[0]
            thread_data = get_thread(thread_ts) or {}
            classification = thread_data.get("classification") or {}
            tier = classification.get("client_tier") or classification.get("tier") or "unknown"
            complexity = classification.get("complexity") or "unknown"
            ticket_number = thread_data.get("ticket_number") or ticket_number

    # 5. Ping #vome-support-engineering
    lines = [
        f"Needs Review -- #{ticket_number}",
        f"*{task_title}*",
        f"Current assignee: {assignee_name} | tier: {tier} | complexity: {complexity}",
        f"{engineer_name} hit a wall and needs input.",
    ]
    if zoho_url:
        lines.append(f"Zoho: {zoho_url}")
    lines.append(f"ClickUp: {task_url}")

    message = "\n".join(lines)

    try:
        post_to_engineering(message)
        print(f"[NEEDS REVIEW] Slack ping sent for task {task_id}")
    except Exception as e:
        print(f"[NEEDS REVIEW] Slack post failed: {e}")
        return False

    # 6. Update thread_map status
    if thread_ts:
        update_thread(thread_ts, status="needs-review")
        print(f"[NEEDS REVIEW] Thread {thread_ts} status set to needs-review")

    print(f"[NEEDS REVIEW] Done for task {task_id}")
    return True
