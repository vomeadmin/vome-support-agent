import os
import re

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from database import save_thread, get_thread

_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
CHANNEL_TICKETS = os.environ.get("SLACK_TICKETS_CHANNEL", "")


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


def _extract_draft_response(agent_response: str) -> str:
    """Extract the DRAFT RESPONSE section from Claude's agent output."""
    m = re.search(
        r"DRAFT RESPONSE[^\n]*\n+(.+?)\n+(?:────|AGENT ANALYSIS)",
        agent_response,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    return ""


def send_ticket_brief(
    ticket_id: str,
    ticket_number: str,
    subject: str,
    crm: dict,
    agent_response: str,
    clickup_task_url: str | None,
    zoho_ticket_url: str,
    clickup_task_id: str | None = None,
    has_attachments: bool = False,
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

    # --- Assemble brief (mobile-first, no separators) ---

    # Contact display name for first line
    display_name = contact_name or (
        "Admin" if crm.get("found") else "Volunteer"
    )

    brief = f"🎫 *#{ticket_number} — {subject}*\n"
    brief += f"{display_name} | {account} | {tier}\n"

    if issue_summary:
        brief += f"\n{issue_summary}\n"

    if latest_reply:
        brief += f"\n_\"{latest_reply}\"_\n"

    # Attachment block
    if has_attachments:
        n = attachment_count
        noun = "attachment" if n == 1 else "attachments"
        brief += f"\n📎 {n} {noun} — check Zoho first\n"

    # Priority display: "P2 | Same day" or "P2 | Not same day"
    timing_clean = timing.strip().capitalize() if timing else ""
    priority_line = priority if priority else ""
    if timing_clean:
        if priority_line:
            priority_line = f"{priority_line} | {timing_clean}"
        else:
            priority_line = timing_clean

    type_parts = [classification, module]
    if priority_line:
        type_parts.append(priority_line)
    brief += f"\n{' | '.join(type_parts)}\n"

    if suggested_owner:
        brief += f"Suggested: {suggested_owner}\n"

    brief += f"{cu_link} | {zoho_ticket_url}\n"

    # Unclear classification guidance
    is_unclear = "unclear" in classification.lower()
    if is_unclear and has_attachments:
        pass  # Attachment note above is sufficient
    elif is_unclear and not has_attachments:
        brief += "\n💬 Suggested: ask for more details\n"

    brief += "\nReply in plain English to take action."

    try:
        resp = _slack.chat_postMessage(
            channel=CHANNEL_TICKETS, text=brief
        )
        thread_ts = resp["ts"]
        print(
            f"Ticket brief posted — ticket {ticket_id},"
            f" thread_ts={thread_ts}"
        )

        save_thread(
            thread_ts=thread_ts,
            ticket_id=ticket_id,
            ticket_number=ticket_number,
            subject=subject,
            channel=CHANNEL_TICKETS,
            clickup_task_id=clickup_task_id,
            classification={
                "type": classification,
                "module": module,
                "platform": (
                    _extract_from_response(agent_response, "PLATFORM")
                ),
                "priority": priority,
                "auto_score": (
                    _extract_from_response(agent_response, "AUTO SCORE")
                ),
                "suggested_owner": suggested_owner,
                "issue_summary": issue_summary,
            },
            crm=crm,
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
