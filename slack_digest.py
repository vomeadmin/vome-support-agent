"""
slack_digest.py

Sends the end-of-day digest to #vome-tickets at 18:00 America/Montreal.
Scheduled via APScheduler in main.py.
"""

import os
from datetime import datetime, timezone

import httpx
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from slack_ticket_brief import _load_thread_map, CHANNEL_TICKETS

_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"
CLICKUP_TEAM_ID = os.environ.get("CLICKUP_TEAM_ID", "")

CLICKUP_USER_ONLYG = os.environ.get("CLICKUP_USER_ONLYG", "")
CLICKUP_USER_SANJAY = os.environ.get("CLICKUP_USER_SANJAY", "")


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _get_engineer_task_count(user_id: str) -> int:
    """Query ClickUp for in-progress tasks assigned to this user."""
    if not CLICKUP_TEAM_ID or not CLICKUP_API_TOKEN or not user_id:
        return 0
    try:
        r = httpx.get(
            f"{CLICKUP_BASE}/team/{CLICKUP_TEAM_ID}/task",
            params={
                "assignees[]": user_id,
                "statuses[]": "in progress",
                "include_closed": False,
            },
            headers={"Authorization": CLICKUP_API_TOKEN},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        return len(data.get("tasks", []))
    except Exception as e:
        print(f"ClickUp engineer task count failed (user {user_id}): {e}")
        return 0


def _format_ticket_list(entries: list[dict]) -> str:
    if not entries:
        return "  (none)"
    lines = []
    for e in entries:
        num = e.get("ticket_number", e.get("ticket_id", "?"))
        subj = e.get("subject", "(no subject)")
        lines.append(f"  • #{num} — {subj}")
    return "\n".join(lines)


def send_daily_digest():
    """
    Build and post the end-of-day digest to #vome-tickets.
    Called by APScheduler at 18:00 America/Montreal.
    """
    if not CHANNEL_TICKETS:
        print("send_daily_digest: SLACK_TICKETS_CHANNEL not set")
        return

    today = _today_str()
    thread_map = _load_thread_map()

    # Partition today's tickets by status
    handled: list[dict] = []
    parked: list[dict] = []
    open_tickets: list[dict] = []

    for ts, entry in thread_map.items():
        if entry.get("date") != today:
            continue
        status = entry.get("status", "open")
        if status == "handled":
            handled.append(entry)
        elif status == "parked":
            parked.append(entry)
        else:
            open_tickets.append(entry)

    # Engineer task counts
    onlyg_count = _get_engineer_task_count(CLICKUP_USER_ONLYG)
    sanjay_count = _get_engineer_task_count(CLICKUP_USER_SANJAY)

    # Format date for display
    display_date = datetime.now(timezone.utc).strftime("%B %-d, %Y")

    # Build digest
    lines = [f"📋 *End of Day — {display_date}*\n"]

    lines.append(f"✅ *Handled today:* {len(handled)}")
    lines.append(_format_ticket_list(handled))
    lines.append("")

    lines.append(f"⏸ *Parked — needs follow-up:* {len(parked)}")
    lines.append(_format_ticket_list(parked))
    lines.append("")

    lines.append(f"🔴 *Open with no response:* {len(open_tickets)}")
    lines.append(_format_ticket_list(open_tickets))
    lines.append("")

    lines.append("*Engineers:*")
    lines.append(f"OnlyG — {onlyg_count} task{'s' if onlyg_count != 1 else ''} in progress")
    lines.append(f"Sanjay — {sanjay_count} task{'s' if sanjay_count != 1 else ''} in progress")

    if not parked and not open_tickets:
        lines.append("\nAll clear — nothing pending 🎉")

    message = "\n".join(lines)

    try:
        _slack.chat_postMessage(channel=CHANNEL_TICKETS, text=message)
        print(f"Daily digest posted to #vome-tickets ({today})")
    except SlackApiError as e:
        print(f"send_daily_digest: Slack error: {e.response['error']}")
    except Exception as e:
        print(f"send_daily_digest: error: {e}")
