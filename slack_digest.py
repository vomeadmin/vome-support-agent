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

from slack_ticket_brief import CHANNEL_TICKETS
from database import get_all_threads

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
        print("send_daily_digest: SLACK_CHANNEL_VOME_TICKETS not set")
        return

    today = _today_str()
    thread_map = get_all_threads()

    # Partition today's tickets by status
    handled: list[dict] = []
    parked: list[dict] = []
    open_tickets: list[dict] = []
    on_prod_pending: list[dict] = []

    for ts, entry in thread_map.items():
        status = entry.get("status", "open")
        # on_prod_cancelled can be from any date — always surface
        if status == "on_prod_cancelled":
            on_prod_pending.append(entry)
            continue
        if entry.get("date") != today:
            continue
        if status == "handled":
            handled.append(entry)
        elif status == "parked":
            parked.append(entry)
        elif status == "on_prod_pending":
            on_prod_pending.append(entry)
        else:
            open_tickets.append(entry)

    # Engineer task counts
    onlyg_count = _get_engineer_task_count(CLICKUP_USER_ONLYG)
    sanjay_count = _get_engineer_task_count(CLICKUP_USER_SANJAY)

    # Format date for display
    display_date = datetime.now(timezone.utc).strftime("%B %-d, %Y")

    # Separate urgent/high-value tickets that need Sam's attention
    needs_attention: list[dict] = []
    routine: list[dict] = []

    for e in open_tickets:
        crm = e.get("crm") or {}
        classification = e.get("classification") or {}
        arr = crm.get("arr")
        tier = crm.get("tier", "").lower()
        category = classification.get("category", "").lower()
        complexity = classification.get("complexity", "").lower()

        is_high_value = tier in (
            "enterprise", "ultimate"
        ) or (arr and float(arr) >= 1500)
        is_urgent_bug = category in ("bug", "investigation") and (
            complexity in ("high", "very-high")
        )

        if is_high_value or is_urgent_bug:
            needs_attention.append(e)
        else:
            routine.append(e)

    # Build digest
    lines = [f"*EOD Summary — {display_date}*\n"]

    if needs_attention:
        lines.append(
            f"*Needs your attention ({len(needs_attention)}):*"
        )
        for e in needs_attention:
            num = e.get("ticket_number", "?")
            subj = e.get("subject", "(no subject)")
            crm = e.get("crm") or {}
            account = crm.get("account_name", "")
            tier = crm.get("tier", "")
            tag = f" | {account} ({tier})" if account else ""
            lines.append(f"  *#{num}*{tag} — {subj}")
        lines.append("")

    if on_prod_pending:
        lines.append(
            f"*On prod, client not yet notified"
            f" ({len(on_prod_pending)}):*"
        )
        for e in on_prod_pending:
            num = e.get("ticket_number", "?")
            subj = e.get("subject", "(no subject)")
            lines.append(f"  #{num} — {subj}")
        lines.append("")

    lines.append(
        f"*Open:* {len(open_tickets)} total"
        f" | *Handled today:* {len(handled)}"
        f" | *Parked:* {len(parked)}"
    )

    lines.append("")
    lines.append("*Engineers:*")
    lines.append(
        f"OnlyG — {onlyg_count} in progress"
        f" | Sanjay — {sanjay_count} in progress"
    )

    if not needs_attention and not on_prod_pending:
        lines.append("\nNothing urgent — all clear")

    message = "\n".join(lines)

    try:
        _slack.chat_postMessage(channel=CHANNEL_TICKETS, text=message)
        print(f"Daily digest posted to #vome-tickets ({today})")
    except SlackApiError as e:
        print(f"send_daily_digest: Slack error: {e.response['error']}")
    except Exception as e:
        print(f"send_daily_digest: error: {e}")
