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
    match = re.search(rf"^{field}:\s*(.+)", agent_response, re.IGNORECASE | re.MULTILINE)
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
    attachment_count: int = 0,
    open_ticket_count: int | None = None,
) -> str | None:
    """
    Post a structured ticket brief to #vome-tickets.

    Returns thread_ts of the posted message (used as the thread root for all
    subsequent replies), or None if the post failed.

    Also persists the thread_ts → ticket mapping to thread_map.json.
    """
    if not CHANNEL_TICKETS:
        print("send_ticket_brief: SLACK_TICKETS_CHANNEL not configured — skipping")
        return None

    # --- Extract classification data from agent response ---
    classification = _extract_from_response(agent_response, "CLASSIFICATION") or "Unknown"
    module = _extract_from_response(agent_response, "MODULE") or "Unknown"
    pattern_note = _extract_from_response(agent_response, "PATTERN")

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

    contact_type = "Admin" if crm.get("found") else "Volunteer"
    prior_str = ""
    if open_ticket_count is not None:
        prior_str = f" | {open_ticket_count} prior ticket{'s' if open_ticket_count != 1 else ''}"

    # --- WHAT block ---
    attach_line = ""
    if attachment_count > 0:
        attach_line = f"\n📎 {attachment_count} attachment{'s' if attachment_count != 1 else ''} — view in Zoho"

    # --- Links ---
    cu_link = clickup_task_url or _extract_clickup_url(agent_response) or "(pending)"

    # --- Assemble brief ---
    brief = (
        f"🎫 *#{ticket_number} — {subject}*\n\n"
        f"*WHO:* {account} | {tier} | {arr_display}\n"
        f"{contact_type}{prior_str}\n\n"
        f"*WHAT:* {classification} | {module}{attach_line}\n"
    )

    if pattern_note:
        brief += f"\n*SIGNAL:* {pattern_note}\n"

    brief += (
        f"\n*ClickUp:* {cu_link}  |  *Zoho:* {zoho_ticket_url}\n"
        "───────────────────────────────────────\n"
        "Reply with your response and I'll send it to the client. Or use:\n"
        "`draft` `assign [name]` `p1/p2/p3`\n"
        "`skip` `note [text]` `tier [X]` `arr [X]`"
    )

    try:
        resp = _slack.chat_postMessage(channel=CHANNEL_TICKETS, text=brief)
        thread_ts = resp["ts"]
        print(f"Ticket brief posted — ticket {ticket_id}, thread_ts={thread_ts}")

        save_thread_mapping(
            thread_ts=thread_ts,
            ticket_id=ticket_id,
            ticket_number=ticket_number,
            subject=subject,
        )
        return thread_ts

    except SlackApiError as e:
        print(f"send_ticket_brief: Slack error for ticket {ticket_id}: {e.response['error']}")
        return None
    except Exception as e:
        print(f"send_ticket_brief: error for ticket {ticket_id}: {e}")
        return None
