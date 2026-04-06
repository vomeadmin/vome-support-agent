"""
slack_agent_mention_handler.py

Handles @Agent mentions in any Slack channel.

When someone @mentions the bot, it reads thread context, uses Claude to
extract a task, and creates it in ClickUp.  Supports explicit commands:
  @Agent task [description]
  @Agent task for [name]
  @Agent note
  @Agent help
"""

import json
import os
import re
from datetime import datetime, timezone

import anthropic
import httpx
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ---------------------------------------------------------------------------
# Clients & config
# ---------------------------------------------------------------------------

_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
_anthropic = anthropic.Anthropic()

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

SLACK_BOT_USER_ID = os.environ.get("SLACK_BOT_USER_ID", "")

# ---------------------------------------------------------------------------
# ClickUp user IDs
# ---------------------------------------------------------------------------

ASSIGNEE_MAP = {
    "sam": 3691763,
    "saul": 3691763,
    "sanjay": 4434086,
    "onlyg": 49257687,
    "only g": 49257687,
    "ron": 4434980,
}

# ---------------------------------------------------------------------------
# ClickUp space / folder / list map (complete)
# ---------------------------------------------------------------------------

# Flat lookup: list_id -> (space_name, list_name)
LIST_NAMES = {
    # Operations
    "901106558605": ("Operations", "Admin Stuff"),
    # Vome Product
    "901113386257": ("Vome Product", "Priority Queue"),
    "901113386484": ("Vome Product", "Raw Intake"),
    "901113389889": ("Vome Product", "Accepted Backlog"),
    "901113389897": ("Vome Product", "Sleeping"),
    "901113389900": ("Vome Product", "Declined"),
    "901113386518": ("Vome Product", "Done"),
    # Customer Success + Support
    "901103827042": ("Customer Success + Support", "CRM"),
    "901103182101": ("Customer Success + Support", "Customer Success"),
    "901103182102": ("Customer Success + Support", "Support"),
    # Marketing
    "900302214243": ("Marketing", "Blog posts"),
    "900302214245": ("Marketing", "Landing page"),
    "901100291772": ("Marketing", "Growth Hacking"),
    "901102939374": ("Marketing", "Materials"),
    "901103182110": ("Marketing", "Sales / Partnerships"),
    "901103185658": ("Marketing", "Prospect / Clients New Feature Updates"),
    "901103784739": ("Marketing", "General"),
    # Vome Dev
    "42278953": ("Vome Dev", "Team Assigned FE"),
    "900301523409": ("Vome Dev", "Pipeline"),
    "901102882382": ("Vome Dev", "Team Assigned BE"),
    "901109857522": ("Vome Dev", "Important Tasks"),
    "901100253592": ("Vome Dev", "Needs Urgent Testing"),
    # Sales-Driven Product Notes
    "900301146956": ("Sales-Driven Product Notes", "Bugs & Urgent Updates"),
    "901105157737": ("Sales-Driven Product Notes", "UX Improvements"),
    "900301962965": ("Sales-Driven Product Notes", "New features (BE)"),
    "900301962946": ("Sales-Driven Product Notes", "New features (FE)"),
    "900301146964": ("Sales-Driven Product Notes", "UI Updates"),
    "900301146997": ("Sales-Driven Product Notes", "Bugs (Mobile)"),
    "900301147000": ("Sales-Driven Product Notes", "New features (Mobile)"),
    "901111176491": ("Sales-Driven Product Notes", "UX Improvements (Mobile)"),
    # Feature Triage
    "901113019498": ("Feature Triage", "Current Sprint"),
    "901111077276": ("Feature Triage", "Needs Triage"),
    "901113168391": ("Feature Triage", "Q&A Testing"),
    "901113228452": ("Feature Triage", "Needle-Moving Features"),
}

# Full map text sent to Claude for destination resolution
_SPACE_LIST_MAP = """
Operations (space: 90110779340):
  - Admin Stuff -> 901106558605

Vome Product (space: 90114113004):
  Master Queue folder:
    - Priority Queue -> 901113386257
  Feature Requests folder:
    - Raw Intake -> 901113386484
    - Accepted Backlog -> 901113389889
    - Sleeping -> 901113389897
    - Declined -> 901113389900
  Completed folder:
    - Done -> 901113386518

Customer Success + Support (space: 90110703798):
  - CRM -> 901103827042
  - Customer Success -> 901103182101
  - Support -> 901103182102

Marketing (space: 90030392048):
  - Blog posts -> 900302214243
  - Landing page -> 900302214245
  - Growth Hacking -> 901100291772
  - Materials -> 901102939374
  - Sales / Partnerships -> 901103182110
  - Prospect / Clients New Feature Updates -> 901103185658
  - General -> 901103784739

Vome Dev (space: 10692349):
  Frontend folder:
    - Team Assigned FE -> 42278953
    - Pipeline -> 900301523409
  Backend folder:
    - Team Assigned BE -> 901102882382
    - Important Tasks -> 901109857522
  Q&A folder:
    - Needs Urgent Testing -> 901100253592

Sales-Driven Product Notes (space: 90030196343):
  Web folder:
    - Bugs & Urgent Updates -> 900301146956
    - UX Improvements -> 901105157737
    - New features (BE) -> 900301962965
    - New features (FE) -> 900301962946
    - UI Updates -> 900301146964
  Mobile folder:
    - Bugs -> 900301146997
    - New features -> 900301147000
    - UX Improvements (Mobile) -> 901111176491

Feature Triage (space: 90113824636):
  - Current Sprint -> 901113019498
  - Needs Triage -> 901111077276
  - Q&A Testing -> 901113168391
  - Needle-Moving Features -> 901113228452
"""

# Channel ID -> default list ID
CHANNEL_DEFAULTS = {}

# Populated at first use from env vars (channel IDs not available at import)
_channel_defaults_loaded = False


def _load_channel_defaults():
    global _channel_defaults_loaded, CHANNEL_DEFAULTS
    if _channel_defaults_loaded:
        return
    _channel_defaults_loaded = True

    # Product/engineering channels -> Priority Queue
    # Only Operations channel -> Admin Stuff
    CHANNEL_DEFAULTS.update({
        os.environ.get("SLACK_CHANNEL_OPS", ""): "901106558605",
        os.environ.get("SLACK_CHANNEL_ENG_FRONTEND", ""): "901113386257",
        os.environ.get("SLACK_CHANNEL_ENG_BACKEND", ""): "901113386257",
        os.environ.get("SLACK_CHANNEL_VOME_FIELD_FEEDBACK", ""): "901113386484",
        os.environ.get("SLACK_CHANNEL_VOME_FEATURE_REQUESTS", ""): "901113386484",
        os.environ.get("SLACK_CHANNEL_SUPPORT_QUEUE_SANJAY", ""): "901113386257",
        os.environ.get("SLACK_CHANNEL_SUPPORT_QUEUE_ONLYG", ""): "901113386257",
        os.environ.get("SLACK_CHANNEL_ENG_INFRA", ""): "901113386257",
        os.environ.get("SLACK_CHANNEL_VOME_SUPPORT_ENGINEERING", ""): "901113386257",
    })
    # Remove empty-string keys (unset env vars)
    CHANNEL_DEFAULTS.pop("", None)


# Default for any channel not mapped above -> Priority Queue
# (product work is the most common use case)
DEFAULT_LIST_ID = "901113386257"  # Vome Product / Priority Queue

PRIORITY_MAP = {"urgent": 1, "high": 2, "normal": 3, "low": 4}

# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

HELP_TEXT = """\
Here's what I can do when you @mention me:

*`@Agent task [description]`* -- Create a ClickUp task. I'll use the thread context to fill in details.
*`@Agent task for [name]`* -- Same, but assigned to a specific person (Sam, Sanjay, OnlyG, Ron).
*`@Agent note`* -- Capture this conversation as a note in ClickUp (no assignee).
*`@Agent help`* -- Show this message.

I'll pick the best ClickUp list based on the channel, or you can say things like "add to Frontend" or "put in Marketing / Blog posts" to override.\
"""


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def _strip_mention(text: str) -> str:
    """Remove the @Agent mention tag from message text."""
    # Slack formats mentions as <@U12345>
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


def _fetch_thread_messages(channel: str, thread_ts: str) -> list[dict]:
    """Fetch all messages in a Slack thread."""
    try:
        resp = _slack.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=50,
        )
        messages = resp.get("messages", [])
        # Format for Claude
        return [
            {
                "user": m.get("user", "unknown"),
                "text": m.get("text", ""),
                "ts": m.get("ts", ""),
            }
            for m in messages
        ]
    except SlackApiError as e:
        print(f"[MENTION] Thread fetch failed: {e.response['error']}")
        return []


def _fetch_recent_channel_messages(channel: str) -> list[dict]:
    """Fetch the last 8 messages from the channel, stopping at a 2-hour gap."""
    try:
        resp = _slack.conversations_history(
            channel=channel,
            limit=8,
        )
        raw = resp.get("messages", [])
        if not raw:
            return []

        # Messages come newest-first; reverse for chronological order
        raw = list(reversed(raw))

        # Filter out messages with >2 hour gaps from the latest
        latest_ts = float(raw[-1].get("ts", "0"))
        two_hours = 2 * 60 * 60
        filtered = []
        for m in raw:
            msg_ts = float(m.get("ts", "0"))
            if latest_ts - msg_ts > two_hours:
                continue
            filtered.append({
                "user": m.get("user", "unknown"),
                "text": m.get("text", ""),
                "ts": m.get("ts", ""),
            })

        return filtered
    except SlackApiError as e:
        print(f"[MENTION] Channel history fetch failed: {e.response['error']}")
        return []


def _reply_in_thread(channel: str, thread_ts: str, text: str):
    """Post a reply in the thread (not broadcast to channel)."""
    try:
        _slack.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=text,
            reply_broadcast=False,
        )
    except SlackApiError as e:
        print(f"[MENTION] Slack reply failed: {e.response['error']}")


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------

def _parse_command(text: str) -> dict:
    """Parse the @Agent command from stripped message text.

    Returns dict with:
      command: "task" | "note" | "help" | "unknown"
      explicit_title: str or None (user-provided title after "task")
      assignee_name: str or None (name after "for")
    """
    lower = text.lower().strip()

    if lower == "help" or lower.startswith("help"):
        return {"command": "help", "explicit_title": None, "assignee_name": None}

    if lower == "note" or lower.startswith("note"):
        return {"command": "note", "explicit_title": None, "assignee_name": None}

    if lower.startswith("task"):
        remainder = text[4:].strip()

        # Check for "task for [name] [optional description]"
        for_match = re.match(
            r"for\s+(sam|saul|sanjay|onlyg|only\s*g|ron)\b\s*(.*)",
            remainder,
            re.IGNORECASE,
        )
        if for_match:
            assignee_name = for_match.group(1).strip().lower()
            desc = for_match.group(2).strip() or None
            return {
                "command": "task",
                "explicit_title": desc,
                "assignee_name": assignee_name,
            }

        # Plain "task [description]"
        return {
            "command": "task",
            "explicit_title": remainder if remainder else None,
            "assignee_name": None,
        }

    # No recognized command -- treat as implicit task
    return {"command": "task", "explicit_title": text if text else None, "assignee_name": None}


# ---------------------------------------------------------------------------
# Destination resolution
# ---------------------------------------------------------------------------

_DEST_PATTERNS = [
    r"(?:add|put|goes?|move)\s+(?:to|in|into)\s+(.+?)(?:\s+list)?$",
    r"(?:in|into)\s+(.+?)(?:\s+list)?$",
    r"(.+?)\s+list$",
]


def _extract_explicit_destination(text: str) -> str | None:
    """Check if the user specified an explicit destination list."""
    lower = text.lower()
    for pattern in _DEST_PATTERNS:
        m = re.search(pattern, lower)
        if m:
            return m.group(1).strip()
    return None


def _resolve_destination_with_claude(
    user_text: str,
    explicit_dest: str | None,
    channel: str,
) -> tuple[str, bool]:
    """Use Claude to resolve a natural-language destination to a list ID.

    Returns (list_id, was_defaulted).
    """
    _load_channel_defaults()

    # If no explicit destination and channel has a default, use it
    if not explicit_dest and channel in CHANNEL_DEFAULTS:
        return (CHANNEL_DEFAULTS[channel], False)

    # If no explicit destination and no channel default, use global default
    if not explicit_dest:
        return (DEFAULT_LIST_ID, True)

    # Ask Claude to resolve the explicit destination
    prompt = (
        "Given this ClickUp space and list map, identify the list ID that "
        "best matches the user's requested destination. Return ONLY the "
        "numeric list ID, nothing else. If no match is clear, return "
        f'"{DEFAULT_LIST_ID}".\n\n'
        f"User's destination request: \"{explicit_dest}\"\n"
        f"Full message context: \"{user_text}\"\n\n"
        f"Space and list map:\n{_SPACE_LIST_MAP}"
    )

    try:
        resp = _anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=50,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # Extract numeric ID from response
        m = re.search(r"\d{6,}", raw)
        if m:
            list_id = m.group(0)
            if list_id in LIST_NAMES:
                return (list_id, False)
    except Exception as e:
        print(f"[MENTION] Claude destination resolution failed: {e}")

    # Fallback: try simple substring matching against list names
    dest_lower = explicit_dest.lower()
    for lid, (space, name) in LIST_NAMES.items():
        if dest_lower in name.lower() or dest_lower in space.lower():
            return (lid, False)

    return (DEFAULT_LIST_ID, True)


# ---------------------------------------------------------------------------
# Task extraction with Claude
# ---------------------------------------------------------------------------

def _extract_task_with_claude(
    messages: list[dict],
    explicit_title: str | None,
    assignee_name: str | None,
    is_note: bool,
) -> dict | None:
    """Use Claude to extract task details from conversation context.

    Returns dict with: title, description, priority, assignee_id
    or None on failure.
    """
    # Build conversation text
    msg_lines = []
    for m in messages:
        user = m.get("user", "unknown")
        text = m.get("text", "")
        # Strip bot mentions from context messages
        text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
        if text:
            msg_lines.append(f"<@{user}>: {text}")

    conversation = "\n".join(msg_lines) if msg_lines else "(no context available)"

    # Resolve assignee ID upfront if name given
    assignee_id = None
    if assignee_name:
        assignee_id = ASSIGNEE_MAP.get(assignee_name.lower())

    title_instruction = ""
    if explicit_title:
        title_instruction = (
            f'\nThe user explicitly requested this title: "{explicit_title}". '
            "Use it as the title verbatim. Use the conversation only for the description."
        )

    type_label = "note" if is_note else "task"

    prompt = (
        f"You are extracting a ClickUp {type_label} from a Slack conversation. "
        "Given the messages below, produce a JSON object with:\n"
        "- title: short, action-oriented task title (max 80 chars)\n"
        "- description: fuller context from the conversation (2-4 sentences, "
        "what needs to be done and why)\n"
        "- priority: 'urgent', 'high', 'normal', or 'low' (infer from words "
        "like 'urgent', 'blocking', 'asap', 'when you get a chance')\n"
        f"- assignee_id: {assignee_id if assignee_id else 'null'}\n"
        "- auto_score: integer 0-100 if the user specified one, otherwise null\n"
        f"{title_instruction}\n\n"
        f"Conversation:\n{conversation}\n\n"
        "Return only valid JSON, no other text."
    )

    try:
        resp = _anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()

        # Extract JSON from response (Claude sometimes wraps in ```json blocks)
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            print(f"[MENTION] Claude returned non-JSON: {raw[:200]}")
            return None

        data = json.loads(json_match.group(0))

        # Ensure assignee_id is set if we resolved it above
        if assignee_id and not data.get("assignee_id"):
            data["assignee_id"] = assignee_id

        return data

    except (json.JSONDecodeError, Exception) as e:
        print(f"[MENTION] Claude task extraction failed: {e}")
        return None


# ---------------------------------------------------------------------------
# ClickUp task creation
# ---------------------------------------------------------------------------

def _create_clickup_task(
    list_id: str,
    title: str,
    description: str,
    priority: str,
    assignee_id: int | None,
    is_note: bool = False,
    auto_score: int | None = None,
) -> dict | None:
    """Create a ClickUp task via REST API.

    Returns {"task_id": str, "task_url": str} or None.
    """
    if not CLICKUP_API_TOKEN:
        print("[MENTION] CLICKUP_API_TOKEN not set -- cannot create task")
        return None

    cu_priority = PRIORITY_MAP.get(priority, 3)

    # Use "queued" for Priority Queue, omit status for other lists
    payload: dict = {
        "name": title,
        "description": description,
        "priority": cu_priority,
    }
    if list_id == "901113386257":  # Priority Queue
        payload["status"] = "queued"
    if assignee_id:
        payload["assignees"] = [assignee_id]

    # Custom fields
    if auto_score is not None:
        FIELD_AUTO_SCORE = "fd77f978-eca8-499e-bc3c-dc1bf4b8181e"
        payload["custom_fields"] = [
            {"id": FIELD_AUTO_SCORE, "value": auto_score}
        ]

    try:
        r = httpx.post(
            f"{CLICKUP_BASE}/list/{list_id}/task",
            json=payload,
            headers={
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        r.raise_for_status()
        task = r.json()
        task_id = task.get("id", "")
        task_url = task.get("url") or f"https://app.clickup.com/t/{task_id}"
        print(f"[MENTION] ClickUp task created: {title} (ID: {task_id}, list: {list_id})")
        return {"task_id": task_id, "task_url": task_url}
    except httpx.HTTPStatusError as e:
        print(f"[MENTION] ClickUp create failed (HTTP {e.response.status_code}): {e.response.text[:300]}")
        return None
    except Exception as e:
        print(f"[MENTION] ClickUp create failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def handle_agent_mention(event: dict) -> None:
    """Process an app_mention event from Slack.

    Parses the command, gathers thread context, extracts a task via Claude,
    creates it in ClickUp, and replies in-thread.
    """
    channel = event.get("channel", "")
    user = event.get("user", "")
    raw_text = event.get("text", "")
    ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or ts  # Use message ts if not in a thread

    print(f"[MENTION] @Agent mention from {user} in {channel}: {raw_text[:100]}")

    # Strip the @mention to get the command
    stripped = _strip_mention(raw_text)

    # Parse command
    cmd = _parse_command(stripped)

    # --- HELP ---
    if cmd["command"] == "help":
        _reply_in_thread(channel, thread_ts, HELP_TEXT)
        return

    # --- TASK or NOTE ---
    is_note = cmd["command"] == "note"

    # Gather context
    if thread_ts != ts:
        # We're inside a thread -- fetch full thread
        messages = _fetch_thread_messages(channel, thread_ts)
    else:
        # Top-level message -- fetch recent channel messages
        messages = _fetch_recent_channel_messages(channel)
        if not messages:
            messages = [{"user": user, "text": stripped, "ts": ts}]

    # Resolve destination
    explicit_dest = _extract_explicit_destination(stripped)
    list_id, was_defaulted = _resolve_destination_with_claude(
        user_text=stripped,
        explicit_dest=explicit_dest,
        channel=channel,
    )

    # Extract task details with Claude
    task_data = _extract_task_with_claude(
        messages=messages,
        explicit_title=cmd["explicit_title"],
        assignee_name=cmd["assignee_name"],
        is_note=is_note,
    )

    if not task_data:
        _reply_in_thread(
            channel, thread_ts,
            "Couldn't create the task -- I wasn't able to extract task details "
            "from the conversation. Try again with more detail.",
        )
        return

    title = task_data.get("title", "Untitled task")
    description = task_data.get("description", "")
    priority = task_data.get("priority", "normal")
    assignee_id = task_data.get("assignee_id")
    auto_score = task_data.get("auto_score")

    # Ensure assignee_id is an int if present
    if assignee_id:
        try:
            assignee_id = int(assignee_id)
        except (ValueError, TypeError):
            assignee_id = None

    # Ensure auto_score is an int if present
    if auto_score is not None:
        try:
            auto_score = int(auto_score)
        except (ValueError, TypeError):
            auto_score = None

    # Notes have no assignee
    if is_note:
        assignee_id = None

    # Create the ClickUp task
    result = _create_clickup_task(
        list_id=list_id,
        title=title,
        description=description,
        priority=priority,
        assignee_id=assignee_id,
        is_note=is_note,
        auto_score=auto_score,
    )

    if not result:
        _reply_in_thread(
            channel, thread_ts,
            "Couldn't create the task -- ClickUp API returned an error. "
            "Try again or create it manually.",
        )
        return

    # Build reply
    space_name, list_name = LIST_NAMES.get(list_id, ("Unknown", "Unknown"))
    label = "Note" if is_note else "Task"

    reply = (
        f"{label} created in {space_name} / {list_name}:\n"
        f"*{title}*\n"
        f"{result['task_url']}"
    )

    if was_defaulted:
        reply += (
            "\n\n_(defaulted to Operations / Admin Stuff -- "
            "reply with a different destination to move it)_"
        )

    _reply_in_thread(channel, thread_ts, reply)
    print(f"[MENTION] Done -- task {result['task_id']} in {space_name} / {list_name}")
