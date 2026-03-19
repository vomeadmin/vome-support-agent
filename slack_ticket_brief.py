import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
CHANNEL_TICKETS = os.environ.get("SLACK_TICKETS_CHANNEL", "")

THREAD_MAP_PATH = Path(__file__).parent / "thread_map.json"


def _load_thread_map() -> dict:
    if THREAD_MAP_PATH.exists():
        try:
            return json.loads(THREAD_MAP_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_thread_map(data: dict):
    THREAD_MAP_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def save_thread_mapping(
    thread_ts: str,
    ticket_id: str,
    ticket_number: str,
    subject: str,
    clickup_task_id: str | None = None,
):
    """Persist thread_ts → ticket metadata in thread_map.json."""
    data = _load_thread_map()
    data[thread_ts] = {
        "ticket_id": ticket_id,
        "ticket_number": ticket_number,
        "clickup_task_id": clickup_task_id,
        "subject": subject,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "channel": CHANNEL_TICKETS,
        "status": "open",
    }
    _save_thread_map(data)


def _extract_from_response(agent_response: str, field: str) -> str:
    """Extract a labelled field from the agent's structured analysis text."""
    match = re.search(
        rf"^{field}:\s*(.+)",
        agent_response,
        re.IGNORECASE | re.MULTILINE,
    )
    return match.group(1).strip() if match else ""


def _extract_clickup_url(agent_response: str) -> str | None:
    """Try to find a ClickUp task URL embedded in the agent response."""
    match = re.search(r"https://app\.clickup\.com/t/\S+", agent_response)
    return match.group(0).rstrip(".,)") if match else None


def send_ticket_brief(
    ticket_id: str,
    ticket_number: str,
    subject: str,
    crm: dict,
    agent_response: str,
    clickup_task_url: str | None,
    zoho_ticket_url: str,
    clickup_task_id: str | None = None,
    attachment_count: int = 0,
    open_ticket_count: int | None = None,
    contact_name: str = "",
    contact_email: str = "",
    issue_summary: str = "",
    latest_reply: str = "",
    timing: str = "",
    priority: str = "",
    suggested_owner: str = "",
) -> str | None:
    """
    Post a structured ticket brief to #vome-tickets.

    Returns thread_ts of the posted message (used as the thread root for all
    subsequent replies), or None if the post failed.

    Also persists the thread_ts → ticket mapping to thread_map.json.
    """
    if not CHANNEL_TICKETS:
        print(
            "send_ticket_brief: SLACK_TICKETS_CHANNEL not configured"
            " — skipping"
        )
        return None

    # --- Extract classification data from agent response ---
    classification = (
        _extract_from_response(agent_response, "CLASSIFICATION") or "Unknown"
    )
    module = (
        _extract_from_response(agent_response, "MODULE") or "Unknown"
    )

    if not priority:
        priority = (
            _extract_from_response(agent_response, "PRIORITY") or ""
        )
    if not timing:
        timing = (
            _extract_from_response(agent_response, "TIMING") or ""
        )
    if not suggested_owner:
        suggested_owner = (
            _extract_from_response(agent_response, "SUGGESTED OWNER") or ""
        )

    # --- WHO block ---
    account = crm.get("account_name") or "Unknown account"
    tier = crm.get("tier") or "Unknown"
    arr_raw = crm.get("arr")
    if arr_raw:
        try:
            arr_display = f"${int(float(arr_raw)):,}"
        except (ValueError, TypeError):
            arr_display = f"${arr_raw}"
    else:
        arr_display = "ARR unknown"

    who_line2_parts = []
    if contact_name or contact_email:
        if contact_name and contact_email:
            name_email = f"{contact_name} ({contact_email})"
        else:
            name_email = contact_name or contact_email
        who_line2_parts.append(name_email)
    else:
        contact_type = "Admin" if crm.get("found") else "Volunteer"
        who_line2_parts.append(contact_type)
    if open_ticket_count is not None:
        n = open_ticket_count
        who_line2_parts.append(f"{n} prior ticket{'s' if n != 1 else ''}")
    who_line2 = " | ".join(who_line2_parts)

    # --- Links ---
    cu_link = (
        clickup_task_url
        or _extract_clickup_url(agent_response)
        or "(pending)"
    )

    # --- Assemble brief ---
    divider = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    brief = (
        f"{divider}\n"
        f"🎫 *#{ticket_number} — {subject}*\n\n"
        f"*WHO:* {account} | {tier} | {arr_display}\n"
        f"{who_line2}\n"
    )

    if issue_summary:
        brief += f"\n*ISSUE:*\n{issue_summary}\n"

    if latest_reply:
        brief += f"\n*LATEST:* \"{latest_reply}\"\n"

    if attachment_count > 0:
        n = attachment_count
        brief += f"\n📎 {n} attachment{'s' if n != 1 else ''} — view in Zoho\n"

    # Priority display: "P2 | Same day" or "P2 | Not same day"
    timing_clean = timing.strip().capitalize() if timing else ""
    priority_line = priority if priority else ""
    if timing_clean:
        if priority_line:
            priority_line = f"{priority_line} | {timing_clean}"
        else:
            priority_line = timing_clean

    brief += f"\n*TYPE:* {classification} | {module}\n"
    if priority_line:
        brief += f"*PRIORITY:* {priority_line}\n"
    if suggested_owner:
        brief += f"*SUGGESTED:* {suggested_owner}\n"

    brief += (
        f"\n*ClickUp:* {cu_link}  |  *Zoho:* {zoho_ticket_url}\n"
        f"{divider}\n"
        "Reply with your response and I'll send it to the client. Or use:\n"
        "`draft` `assign [name]` `p1/p2/p3`\n"
        "`skip` `note [text]` `tier [X]` `arr [X]`\n"
        f"{divider}"
    )

    try:
        resp = _slack.chat_postMessage(
            channel=CHANNEL_TICKETS, text=brief
        )
        thread_ts = resp["ts"]
        print(
            f"Ticket brief posted — ticket {ticket_id},"
            f" thread_ts={thread_ts}"
        )

        save_thread_mapping(
            thread_ts=thread_ts,
            ticket_id=ticket_id,
            ticket_number=ticket_number,
            subject=subject,
            clickup_task_id=clickup_task_id,
        )
        return thread_ts

    except SlackApiError as e:
        err = e.response["error"]
        print(
            f"send_ticket_brief: Slack error for ticket"
            f" {ticket_id}: {err}"
        )
        return None
    except Exception as e:
        print(f"send_ticket_brief: error for ticket {ticket_id}: {e}")
        return None
