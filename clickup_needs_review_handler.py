"""
clickup_needs_review_handler.py

Handles ClickUp taskStatusUpdated -> "escalated".

When an engineer sets a task to "escalated":
  1. Fetch the ClickUp task to get current assignee + Zoho ticket link
  2. Look up account / tier / ARR / complexity from thread_map
  3. Post a structured escalation card to the #escalated-tickets channel
     in real time so Sam and the engineers can discuss it in-thread
  4. Update thread_map status to "escalated"
"""

import os
import re

import httpx
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from database import (
    get_thread,
    get_thread_by_ticket_id,
    update_thread,
)
from status_constants import THREAD_ESCALATED

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))

# Dedicated "escalated tickets" channel. Defaults to the channel Sam created;
# override with SLACK_CHANNEL_ESCALATIONS if it ever moves.
CHANNEL_ESCALATIONS = os.environ.get(
    "SLACK_CHANNEL_ESCALATIONS", "C0BB3JCT51A"
)

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

def handle_escalated(task_id: str, engineer_name: str) -> bool:
    """Process a ClickUp task moving to 'escalated'.

    Posts a structured escalation card to the dedicated #escalated-tickets
    channel in real time so Sam and the engineers can discuss it in-thread.
    Returns True on success.
    """
    print(f"[ESCALATED] Task {task_id} escalated by {engineer_name}")

    if not CHANNEL_ESCALATIONS:
        print("[ESCALATED] SLACK_CHANNEL_ESCALATIONS not set -- skipping")
        return False

    # 1. Fetch ClickUp task
    task = _get_clickup_task(task_id)
    if not task:
        print(f"[ESCALATED] Could not fetch ClickUp task {task_id}")
        return False

    task_title = task.get("name", task_id)
    task_url = task.get("url") or f"https://app.clickup.com/t/{task_id}"

    # 2. Current assignee
    assignee_id, assignee_name = _get_current_assignee(task)

    # 3. Zoho ticket link
    zoho_ticket_id = _extract_zoho_ticket_id(task)
    zoho_url = ""
    if zoho_ticket_id:
        zoho_url = (
            f"https://desk.zoho.com/support/vomevolunteer"
            f"/ShowHomePage.do#Cases/dv/{zoho_ticket_id}"
        )

    # 4. Enrich from thread_map (account / tier / ARR / complexity)
    account = "Unknown"
    tier = "unknown"
    arr_display = "unknown"
    complexity = "unknown"
    ticket_number = zoho_ticket_id or task_id
    thread_ts = None

    if zoho_ticket_id:
        result = get_thread_by_ticket_id(zoho_ticket_id)
        if result:
            thread_ts = result[0]
            thread_data = get_thread(thread_ts) or {}
            ticket_number = thread_data.get("ticket_number") or ticket_number
            classification = thread_data.get("classification") or {}
            complexity = classification.get("complexity") or "unknown"
            crm = thread_data.get("crm") or {}
            account = crm.get("account_name") or account
            tier = (
                crm.get("tier")
                or classification.get("client_tier")
                or classification.get("tier")
                or "unknown"
            )
            arr_raw = crm.get("arr")
            if arr_raw:
                try:
                    arr_display = f"${int(float(arr_raw)):,}"
                except (ValueError, TypeError):
                    arr_display = str(arr_raw)

    # 5. Build the escalation card
    link_parts = []
    if zoho_url:
        link_parts.append(f"<{zoho_url}|Zoho>")
    link_parts.append(f"<{task_url}|ClickUp>")

    lines = [
        f":rotating_light: *Escalated — #{ticket_number}*",
        f"*{task_title}*",
        f"*{engineer_name}* escalated this and needs input.",
        f"*Account:* {account} | *Tier:* {tier} | *ARR:* {arr_display}",
        f"*Assignee:* {assignee_name} | *Complexity:* {complexity}",
        " | ".join(link_parts),
        "",
        "Reply in this thread to discuss.",
    ]
    message = "\n".join(lines)

    # 6. Post to the dedicated escalations channel
    try:
        _slack.chat_postMessage(channel=CHANNEL_ESCALATIONS, text=message)
        print(f"[ESCALATED] Card posted to escalations channel for {task_id}")
    except SlackApiError as e:
        print(f"[ESCALATED] Slack post failed: {e.response['error']}")
        return False

    # 7. Update thread_map status
    if thread_ts:
        update_thread(thread_ts, status=THREAD_ESCALATED)
        print(f"[ESCALATED] Thread {thread_ts} status set to escalated")

    print(f"[ESCALATED] Done for task {task_id}")
    return True
