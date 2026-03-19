"""
field_feedback.py

Handles Ron's messages in #vome-field-feedback.

Flow for a new top-level message:
  1. Acknowledge immediately in thread: "Got it — logging this now ✓"
  2. Run Claude to classify and extract structure
  3. Create ClickUp task immediately (don't wait for confirmation)
  4. If org name is missing, ask Ron one question in thread
  5. Store state in feedback_map.json

Flow for a thread reply:
  - If awaiting org name: update ClickUp task and close the loop
  - Otherwise: append Ron's context to task description
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import httpx

from agent import SYSTEM_PROMPT
from clickup_tasks import (
    SOURCE_FIELD_FEEDBACK,
    _build_title,
    _determine_assignee,
    _determine_list,
    _map_module_option,
    _map_platform_option,
    _map_priority,
    _map_type_option,
    _parse_agent_response,
    create_clickup_task,
    CLICKUP_BASE,
    CLICKUP_API_TOKEN,
    FIELD_TYPE,
    FIELD_PLATFORM,
    FIELD_MODULE,
    FIELD_SOURCE,
    FIELD_HIGHEST_TIER,
    FIELD_AUTO_SCORE,
)

_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
_anthropic = anthropic.Anthropic()

CHANNEL_FIELD_FEEDBACK = os.environ.get("SLACK_CHANNEL_FIELD_FEEDBACK", "")

FEEDBACK_MAP_PATH = Path(__file__).parent / "feedback_map.json"


# ---------------------------------------------------------------------------
# feedback_map.json helpers
# ---------------------------------------------------------------------------

def _load_feedback_map() -> dict:
    if FEEDBACK_MAP_PATH.exists():
        try:
            return json.loads(
                FEEDBACK_MAP_PATH.read_text(encoding="utf-8")
            )
        except Exception:
            pass
    return {}


def _save_feedback_map(data: dict):
    FEEDBACK_MAP_PATH.write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def _store_feedback(
    thread_ts: str,
    task_id: str,
    task_url: str,
    classification: str,
    priority: str,
    awaiting: str | None,
):
    data = _load_feedback_map()
    data[thread_ts] = {
        "task_id": task_id,
        "task_url": task_url,
        "classification": classification,
        "priority": priority,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "awaiting": awaiting,
    }
    _save_feedback_map(data)


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def _reply(thread_ts: str, text: str):
    try:
        _slack.chat_postMessage(
            channel=CHANNEL_FIELD_FEEDBACK,
            thread_ts=thread_ts,
            text=text,
        )
    except SlackApiError as e:
        print(f"field_feedback reply failed: {e.response['error']}")


# ---------------------------------------------------------------------------
# ClickUp update helpers
# ---------------------------------------------------------------------------

def _cu_update_task(task_id: str, payload: dict) -> bool:
    try:
        r = httpx.put(
            f"{CLICKUP_BASE}/task/{task_id}",
            json=payload,
            headers={
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"ClickUp update failed ({task_id}): {e}")
        return False


def _append_description(task_id: str, note: str):
    """Append a note to the task description by fetching then updating."""
    try:
        r = httpx.get(
            f"{CLICKUP_BASE}/task/{task_id}",
            headers={"Authorization": CLICKUP_API_TOKEN},
            timeout=15,
        )
        r.raise_for_status()
        existing = r.json().get("description", "") or ""
        updated = f"{existing}\n\n{note}".strip()
        _cu_update_task(task_id, {"description": updated})
    except Exception as e:
        print(f"ClickUp description append failed ({task_id}): {e}")


# ---------------------------------------------------------------------------
# Claude classification
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = """\
Classify this field feedback from Ron and return ONLY the structured block \
below. No prose, no explanation.

CLASSIFICATION: [Bug — Frontend (web) / Bug — Frontend (mobile) / \
Bug — Backend / Data / Feature request / UX / Improvement / \
Access / Visibility issue / General question]
MODULE: [module name from the module list]
PLATFORM: [Web / Mobile / Both]
PRIORITY: [P1 / P2 / P3]
AUTO SCORE: [0-100]
ORG NAME: [organisation name if mentioned, else MISSING]
EXISTING CLIENT: [yes / no / unknown]

Use the priority and classification rules from your system prompt.
Field feedback from an existing confirmed client is P2 minimum.
Unconfirmed prospect feedback is P3.
"""


def _classify_feedback(text: str) -> dict:
    """
    Call Claude to classify a field feedback message.
    Returns the parsed dict from _parse_agent_response plus ORG NAME
    and EXISTING CLIENT fields.
    """
    user_message = (
        "⚠️ SOURCE: Slack #vome-field-feedback — Field Feedback from Ron\n\n"
        f"Ron's message:\n{text}"
    )
    try:
        response = _anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": _CLASSIFY_PROMPT},
            ],
        )
        raw = _CLASSIFY_PROMPT + response.content[0].text
    except Exception as e:
        print(f"field_feedback classification failed: {e}")
        raw = ""

    parsed = _parse_agent_response(raw)

    # Extract ORG NAME and EXISTING CLIENT from the same raw block
    import re
    org_match = re.search(
        r"^ORG NAME:\s*(.+)", raw, re.IGNORECASE | re.MULTILINE
    )
    existing_match = re.search(
        r"^EXISTING CLIENT:\s*(.+)", raw, re.IGNORECASE | re.MULTILINE
    )

    parsed["org_name"] = (
        org_match.group(1).strip()
        if org_match and "missing" not in org_match.group(1).lower()
        else None
    )
    parsed["existing_client"] = (
        existing_match.group(1).strip().lower()
        if existing_match
        else "unknown"
    )
    return parsed


# ---------------------------------------------------------------------------
# Main handlers
# ---------------------------------------------------------------------------

def handle_field_feedback(event: dict):
    """
    Route a #vome-field-feedback Slack event.

    - Top-level message (no thread_ts, or thread_ts == ts): new feedback
    - Thread reply: Ron confirming info or Sam adding context
    """
    text = (event.get("text") or "").strip()
    ts = event.get("ts", "")
    thread_ts = event.get("thread_ts")
    user = event.get("user", "")

    if not text:
        return

    # Determine if this is a new post or a thread reply
    is_reply = thread_ts and thread_ts != ts

    if is_reply:
        _handle_feedback_reply(thread_ts, text, user)
    else:
        _handle_new_feedback(ts, text)


def _handle_new_feedback(thread_ts: str, text: str):
    """Process a new top-level field feedback message from Ron."""

    # Step 1 — Acknowledge immediately
    _reply(thread_ts, "Got it — logging this now ✓")

    # Step 2 — Classify with Claude
    parsed = _classify_feedback(text)
    classification = parsed.get("classification") or "Investigation"
    priority = parsed.get("priority") or "P3"
    org_name = parsed.get("org_name")
    existing_client = parsed.get("existing_client", "unknown")

    # Step 3 — Build ticket_data and crm for create_clickup_task
    # Use org name if known; otherwise mark as pending
    account_label = org_name or "Field Feedback Ron"

    ticket_data = {
        "ticket_id": f"ff-{thread_ts}",
        "ticket_number": "",
        "subject": _summarise_subject(text),
        "body": text,
        "contact_email": "",
        "contact_name": "Ron Segev",
    }

    crm: dict = {"found": False}
    if org_name:
        # Mark as existing client if confirmed, else prospect
        is_existing = existing_client == "yes"
        crm = {
            "found": True,
            "account_name": org_name,
            "tier": "Unknown",
            "arr": None,
            "contact_type": "admin" if is_existing else "prospect",
        }

    # Step 4 — Create ClickUp task immediately
    result = create_clickup_task(
        ticket_data=ticket_data,
        agent_response=_build_agent_response_block(parsed),
        crm=crm,
        zoho_url="",
        source_option_id=SOURCE_FIELD_FEEDBACK,
    )

    task_id = result["task_id"] if result else None
    task_url = result["task_url"] if result else None

    # Step 5 — Ask one follow-up question if org name is missing
    awaiting = None
    if not org_name:
        _reply(thread_ts, "Which org is this for?")
        awaiting = "org_name"

        # Mark task as awaiting confirmation
        if task_id:
            _cu_update_task(
                task_id,
                {"description": (
                    f"{text}\n\nStatus: Awaiting Ron confirmation — org name missing"
                )},
            )
    else:
        # All info present — close the loop
        engineer = _engineer_label(classification)
        _reply(
            thread_ts,
            f"Logged as {classification}, {priority}. "
            f"{engineer}",
        )

    # Step 6 — Persist state
    if task_id:
        _store_feedback(
            thread_ts=thread_ts,
            task_id=task_id,
            task_url=task_url or "",
            classification=classification,
            priority=priority,
            awaiting=awaiting,
        )
    else:
        print(f"field_feedback: ClickUp task creation failed for {thread_ts}")


def _handle_feedback_reply(thread_ts: str, text: str, user: str):
    """Handle a reply in an existing field feedback thread."""
    feedback_map = _load_feedback_map()
    entry = feedback_map.get(thread_ts)

    if not entry:
        # Not a tracked feedback thread — ignore
        return

    task_id = entry.get("task_id")
    classification = entry.get("classification", "")
    priority = entry.get("priority", "P3")
    awaiting = entry.get("awaiting")

    if awaiting == "org_name":
        # Ron's reply is the org name
        org_name = text.strip()
        if task_id:
            # Update task name and description with confirmed org
            subject = _summarise_subject(
                feedback_map.get(thread_ts, {}).get("classification", "")
            )
            new_title = (
                f"{org_name} — {subject} — {priority}"
                if subject
                else f"{org_name} — Field feedback — {priority}"
            )
            _cu_update_task(task_id, {"name": new_title})
            _append_description(
                task_id,
                f"Org confirmed by Ron: {org_name}",
            )

        # Clear the awaiting flag
        entry["awaiting"] = None
        feedback_map[thread_ts] = entry
        _save_feedback_map(feedback_map)

        engineer = _engineer_label(classification)
        _reply(
            thread_ts,
            f"Updated — logged as {classification}, {priority}. "
            f"{engineer}",
        )

    else:
        # Sam or Ron adding context voluntarily — append to task
        if task_id:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            _append_description(
                task_id,
                f"[Follow-up note — {date_str}]: {text}",
            )
            _reply(thread_ts, "✓ Note added to ClickUp task")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _summarise_subject(text: str) -> str:
    """Return a short subject line from feedback text (first 60 chars)."""
    first_line = text.split("\n")[0].strip()
    if len(first_line) > 60:
        return first_line[:57] + "..."
    return first_line


def _engineer_label(classification: str) -> str:
    """Return a human-readable engineer attribution string."""
    cl = classification.lower()
    if any(x in cl for x in ("frontend", "ux", "ui")):
        return "Sanjay will pick this up."
    if any(x in cl for x in ("backend", "data", "access", "api")):
        return "OnlyG will pick this up."
    return "Task is in the queue."


def _build_agent_response_block(parsed: dict) -> str:
    """
    Build a fake agent-response string in the format that
    _parse_agent_response() expects, so create_clickup_task() can
    parse it without any changes to its internals.
    """
    lines = [
        f"CLASSIFICATION: {parsed.get('classification', 'Investigation')}",
        f"MODULE: {parsed.get('module', 'Other')}",
        f"PLATFORM: {parsed.get('platform', 'Web')}",
        f"PRIORITY: {parsed.get('priority', 'P3')}",
        f"AUTO SCORE: {parsed.get('auto_score', '0')}",
        f"TIMING: Unknown",
    ]
    return "\n".join(lines)
