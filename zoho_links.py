"""
zoho_links.py

Single source of truth for turning a stored "Zoho Ticket Link" into a Zoho
ticket ID, plus a shared alert for when that extraction fails.

Background: every ClickUp trigger handler (on prod, needs client info, user
education, escalated, assignee sync) has to recover the Zoho ticket ID from
the task's "Zoho Ticket Link" custom field before it can act. Each handler
used to carry its own copy of the parser, and all of them only understood the
legacy Zoho Desk URL (.../ShowHomePage.do#Cases/dv/{id}). When tasks began
carrying the current Zoho Desk URL (.../agent/.../tickets/details/{id}), or a
blank link, the parsers silently returned None and the handlers bailed without
sending or closing anything. Centralizing the logic here means every handler
learns new URL shapes at once, and a failed extraction becomes visible instead
of dying in the logs.
"""

import os
import re

# ClickUp custom field ID for "Zoho Ticket Link" (same across every list).
FIELD_ZOHO_TICKET_LINK = "4776215b-c725-4d79-8f20-c16f0f0145ac"

# URL shapes we can pull a ticket ID out of, tried in order.
_ID_PATTERNS = (
    r"/dv/(\d+)",               # legacy: .../ShowHomePage.do#Cases/dv/{id}
    r"/tickets/details/(\d+)",  # current: .../agent/.../tickets/details/{id}
)


def parse_zoho_ticket_id(value) -> str | None:
    """Return the Zoho ticket ID contained in a stored link value, or None.

    Accepts either Zoho Desk URL format, a bare numeric ID, or (as a last
    resort) any value ending in a long run of digits. Returns None only when
    there is genuinely no ID-looking token, so callers can treat None as
    "no usable link".
    """
    value = str(value or "")
    for pat in _ID_PATTERNS:
        m = re.search(pat, value)
        if m:
            return m.group(1)
    stripped = value.strip()
    if stripped.isdigit():
        return stripped
    # Safety net: a trailing run of 6+ digits (Zoho ticket IDs are long).
    # Guards against a future URL shape without silently breaking again.
    m = re.search(r"(\d{6,})\D*$", value)
    return m.group(1) if m else None


def extract_zoho_ticket_id(task: dict) -> str | None:
    """Pull the Zoho ticket ID from a ClickUp task's Zoho Ticket Link field."""
    for field in task.get("custom_fields") or []:
        if field.get("id") != FIELD_ZOHO_TICKET_LINK:
            continue
        return parse_zoho_ticket_id(field.get("value"))
    return None


def alert_missing_ticket_link(
    handler_label: str, task_id: str, task_title: str = ""
) -> None:
    """Post a Slack heads-up when a trigger handler can't find a ticket ID.

    Extraction failures used to die in the logs, so a task would sit in a
    trigger column looking processed while nothing happened. This surfaces it
    to the support channel so a human can fix the Zoho link and re-trigger.
    Best-effort: never raises.
    """
    channel = os.environ.get("SLACK_CHANNEL_SUPPORT_FINAL_REVIEW", "")
    if not channel:
        return
    task_url = f"https://app.clickup.com/t/{task_id}"
    text = (
        f":warning: *{handler_label} could not trigger, no usable Zoho link*\n"
        f"*{task_title or task_id}*\n{task_url}\n"
        "This task is in a trigger status but its *Zoho Ticket Link* field is "
        "blank or in a format Vic can't read, so no email was sent and nothing "
        "was closed. Paste the ticket's Zoho link, then re-set the status to "
        "re-trigger."
    )
    try:
        from slack_sdk import WebClient

        WebClient(
            token=os.environ.get("SLACK_BOT_TOKEN", "")
        ).chat_postMessage(channel=channel, text=text)
    except Exception as e:
        print(
            f"[ALERT] Slack extraction-failure alert failed"
            f" for {task_id}: {e}"
        )
