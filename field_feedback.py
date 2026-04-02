"""
field_feedback.py

Conversational agent for #vome-field-feedback.

Every message (from Ron, Sam, or anyone on the team) is sent to Claude
with the full thread history and CONTEXT.md.  Claude decides what to do
— create a ClickUp task, update one, delete one, ask a follow-up, or
just acknowledge — via tool_use.  The agent always replies in-thread
telling the user what it understood, what it did, and linking to any
ClickUp tasks involved.
"""

import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ---------------------------------------------------------------------------
# Clients & config
# ---------------------------------------------------------------------------

_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
_anthropic = anthropic.Anthropic()

CHANNEL_FIELD_FEEDBACK = os.environ.get("SLACK_CHANNEL_VOME_FIELD_FEEDBACK", "")

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

SAM_SLACK_ID = os.environ.get("SAM_SLACK_ID", "U01B9FQNSBU")

# Feedback map — persists thread_ts → task metadata across restarts
FEEDBACK_MAP_PATH = Path(__file__).parent / "feedback_map.json"

# Load CONTEXT.md once at import time — this is the agent's brain
_CONTEXT_PATH = Path(__file__).parent / "context.md"
_CONTEXT_MD = _CONTEXT_PATH.read_text(encoding="utf-8") if _CONTEXT_PATH.exists() else ""


# ---------------------------------------------------------------------------
# ClickUp field IDs (imported from context, kept here for tool use)
# ---------------------------------------------------------------------------

# List IDs
LIST_PRIORITY_QUEUE = "901113386257"
LIST_RAW_INTAKE = "901113386484"
LIST_ACCEPTED_BACKLOG = "901113389889"
LIST_SLEEPING = "901113389897"
LIST_DECLINED = "901113389900"

# Custom field IDs
FIELD_TYPE = "e0e439f5-397d-432d-addd-e90fbf50cd30"
FIELD_PLATFORM = "5f1ff65b-18fc-49db-89aa-2c1f355ec1e7"
FIELD_MODULE = "3f111d48-e92a-4d5e-92d9-e193c80b20cc"
FIELD_SOURCE = "857e1262-cb5c-4c22-b8da-770a8fcfa82e"
FIELD_HIGHEST_TIER = "be348a1d-6a63-4da8-83bb-9038b24264ff"
FIELD_REQUESTING_CLIENTS = "e2de3bd0-6ad9-4b31-bb09-104f6bef383d"
FIELD_COMBINED_ARR = "29c41859-f24b-4143-9af4-a34202205641"
FIELD_AUTO_SCORE = "fd77f978-eca8-499e-bc3c-dc1bf4b8181e"
FIELD_ZOHO_TICKET_LINK = "4776215b-c725-4d79-8f20-c16f0f0145ac"

# Source option IDs
SOURCE_FIELD_FEEDBACK = "ef5fcb3c-c27d-443c-bef2-32be1521baf1"
SOURCE_INTERNAL = "ea82838e-f5ee-4cc6-b5d3-da4ef9052343"

# Type option IDs
TYPE_OPTIONS = {
    "bug": "d9c82e67-c46b-48d1-95f7-9c1f5b2fc2df",
    "feature": "41a1ea4e-eec9-418d-a684-3c17cdd8dd67",
    "ux": "da749879-7b3a-4fd5-a1cb-c85fcb719569",
    "improvement": "9864f852-39cc-481c-aafc-c2f2ebdba30b",
    "investigation": "f9bd67bb-5b85-49fb-bd4a-21295f01cf5a",
}

# Platform option IDs
PLATFORM_OPTIONS = {
    "web": "946c8214-6a65-4e63-a437-d98415dc1439",
    "mobile": "070470c3-c248-4d64-8ceb-8c95df82506b",
    "both": "2d69c526-e58f-4486-bc6a-168cd812f0bf",
}

# Module option IDs
MODULE_OPTIONS = {
    "volunteer homepage": "1fd64528-970a-48da-8881-9a0fb4ac96f4",
    "reserve schedule": "197109a7-2974-4210-94f1-a1e97990830f",
    "opportunities": "af0b8949-5281-4655-8326-c77dcfb2ecf7",
    "sequences": "04d7e808-d94f-48bb-b5eb-f567e6cf41ca",
    "forms": "aa6f6c17-7260-44fd-a862-99daaf7d77c0",
    "admin dashboard": "b13da71b-46ab-45c6-82ab-fa37a18fc0b3",
    "admin scheduling": "f36d0e31-2f2b-4044-bb5c-fb4fef06cb74",
    "admin settings": "d9d9051c-d733-4ac0-8607-1a75153b021b",
    "admin permissions": "bf68973f-f858-4083-9a60-7dc014b6e1f3",
    "sites": "5f5cc57b-6259-4bab-88dd-64b3a34036f1",
    "groups": "36178405-828f-4471-80a4-cedb2eb0be59",
    "categories": "f4f5021b-d528-41c1-b832-87a67e5ba0ae",
    "hour tracking": "53f02923-e8a5-4c30-8a42-c2bddaa75778",
    "kiosk": "d92abcaa-e2a3-46f6-bb99-2025aa3984d3",
    "email communications": "c2c3a5ae-e8db-4da5-9ab3-736c3a76b66f",
    "chat": "a4d5a4bd-049b-4dfa-b6c2-2843661faad4",
    "reports": "95a6e4bf-eaa0-4cd4-87ed-9298a37da0c6",
    "kpi dashboards": "ef0db184-315f-4420-bccb-bb7dee73e7a1",
    "integrations": "938a5549-70dd-40f2-9db2-4176d10b4221",
    "access / authentication": "fd457e45-e25c-4910-871f-fe67bf5391d3",
    "other": "cbe38d18-9d9f-4e49-abf5-5101f09349ff",
}

# Assignee ClickUp IDs
ASSIGNEE_IDS = {
    "sam": os.environ.get("CLICKUP_USER_SAM", "3691763"),
    "onlyg": os.environ.get("CLICKUP_USER_ONLYG", "49257687"),
    "sanjay": os.environ.get("CLICKUP_USER_SANJAY", "4434086"),
}

# Priority map
PRIORITY_MAP = {"p1": 1, "p2": 2, "p3": 3}


# ---------------------------------------------------------------------------
# Tool definitions for Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "create_clickup_task",
        "description": (
            "Create a new task in ClickUp under the VOME Operations space. "
            "Use this when someone reports a bug, feature request, feedback, "
            "or any actionable item that should be tracked. The task will be "
            "routed to the correct list based on type and priority."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "Task title in format: [Org/Source] — [Issue summary] — [P1/P2/P3]. "
                        "Example: 'UMMS — Volunteer visibility bug — P1'"
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Full task description with all relevant context from the conversation.",
                },
                "type": {
                    "type": "string",
                    "enum": ["bug", "feature", "ux", "improvement", "investigation"],
                    "description": "Task type classification.",
                },
                "platform": {
                    "type": "string",
                    "enum": ["web", "mobile", "both"],
                    "description": "Which platform is affected.",
                },
                "module": {
                    "type": "string",
                    "description": (
                        "Module name from the module list (e.g. 'Volunteer Homepage', "
                        "'Reserve Schedule', 'Forms', 'Admin Dashboard', etc.)"
                    ),
                },
                "priority": {
                    "type": "string",
                    "enum": ["P1", "P2", "P3"],
                    "description": "Priority level based on classification rules.",
                },
                "auto_score": {
                    "type": "integer",
                    "description": "Auto score 0-100 based on urgency, client value, breadth, recency.",
                },
                "assignee": {
                    "type": "string",
                    "enum": ["sam", "onlyg", "sanjay"],
                    "description": "Engineer to assign. Frontend/UX → sanjay, Backend/data → onlyg.",
                },
                "org_name": {
                    "type": "string",
                    "description": "Client organization name if known.",
                },
                "tier": {
                    "type": "string",
                    "description": "Client tier if known (Ultimate, Enterprise, Pro, Recruit, Prospect).",
                },
                "list": {
                    "type": "string",
                    "enum": ["priority_queue", "raw_intake", "accepted_backlog"],
                    "description": (
                        "Which list to put the task in. Bugs/access/urgent → priority_queue, "
                        "features → raw_intake, non-urgent UX → accepted_backlog."
                    ),
                },
            },
            "required": ["title", "description", "type", "priority", "list"],
        },
    },
    {
        "name": "update_clickup_task",
        "description": (
            "Update an existing ClickUp task. Use this when someone provides "
            "additional context, corrections, or wants to change task properties "
            "like priority, assignee, title, description, or status. "
            "You can find the task_id from the thread's previous conversation context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The ClickUp task ID to update.",
                },
                "title": {"type": "string", "description": "New task title."},
                "description": {"type": "string", "description": "New full description (replaces existing)."},
                "append_note": {
                    "type": "string",
                    "description": "Text to append to existing description (does not replace).",
                },
                "priority": {
                    "type": "string",
                    "enum": ["P1", "P2", "P3"],
                    "description": "New priority level.",
                },
                "assignee": {
                    "type": "string",
                    "enum": ["sam", "onlyg", "sanjay"],
                    "description": "New assignee.",
                },
                "status": {
                    "type": "string",
                    "description": "New status (e.g. QUEUED, IN PROGRESS, ON PROD, DONE).",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "delete_clickup_task",
        "description": (
            "Delete a ClickUp task. Use this when someone explicitly says to "
            "remove, cancel, or delete a task — for example if the feedback "
            "turned out to be invalid or a duplicate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The ClickUp task ID to delete.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why the task is being deleted.",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_clickup_task",
        "description": (
            "Fetch details of an existing ClickUp task. Use this when you "
            "need to check current state before updating, or when someone "
            "asks about the status of a task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The ClickUp task ID to fetch.",
                },
            },
            "required": ["task_id"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _resolve_type_option(type_str: str) -> str:
    t = type_str.lower()
    for key, val in TYPE_OPTIONS.items():
        if key in t:
            return val
    return TYPE_OPTIONS["investigation"]


def _resolve_platform_option(platform_str: str) -> str | None:
    p = platform_str.lower()
    return PLATFORM_OPTIONS.get(p)


def _resolve_module_option(module_str: str) -> str | None:
    m = module_str.lower().strip()
    if m in MODULE_OPTIONS:
        return MODULE_OPTIONS[m]
    for key, val in MODULE_OPTIONS.items():
        if key in m or m in key:
            return val
    return MODULE_OPTIONS.get("other")


def _resolve_list_id(list_str: str) -> str:
    return {
        "priority_queue": LIST_PRIORITY_QUEUE,
        "raw_intake": LIST_RAW_INTAKE,
        "accepted_backlog": LIST_ACCEPTED_BACKLOG,
    }.get(list_str, LIST_PRIORITY_QUEUE)


def _exec_create_task(params: dict) -> dict:
    """Execute create_clickup_task tool call."""
    list_id = _resolve_list_id(params.get("list", "priority_queue"))
    priority_int = PRIORITY_MAP.get(params["priority"].lower(), 3)

    custom_fields = []
    custom_fields.append({"id": FIELD_TYPE, "value": _resolve_type_option(params.get("type", "investigation"))})
    custom_fields.append({"id": FIELD_SOURCE, "value": SOURCE_FIELD_FEEDBACK})

    platform_opt = _resolve_platform_option(params.get("platform", ""))
    if platform_opt:
        custom_fields.append({"id": FIELD_PLATFORM, "value": platform_opt})

    module_opt = _resolve_module_option(params.get("module", "other"))
    if module_opt:
        custom_fields.append({"id": FIELD_MODULE, "value": module_opt})

    if params.get("auto_score") is not None:
        custom_fields.append({"id": FIELD_AUTO_SCORE, "value": int(params["auto_score"])})

    if params.get("tier"):
        custom_fields.append({"id": FIELD_HIGHEST_TIER, "value": params["tier"]})

    if params.get("org_name"):
        custom_fields.append({"id": FIELD_REQUESTING_CLIENTS, "value": params["org_name"]})

    payload = {
        "name": params["title"],
        "description": params["description"],
        "priority": priority_int,
        "status": "QUEUED",
        "custom_fields": custom_fields,
    }

    assignee = params.get("assignee", "")
    if assignee and assignee in ASSIGNEE_IDS:
        uid = ASSIGNEE_IDS[assignee]
        if uid:
            payload["assignees"] = [int(uid)]

    r = httpx.post(
        f"{CLICKUP_BASE}/list/{list_id}/task",
        json=payload,
        headers={"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    task = r.json()
    task_id = task.get("id", "")
    task_url = task.get("url") or f"https://app.clickup.com/t/{task_id}"
    print(f"[field_feedback] ClickUp task created: {params['title']} (ID: {task_id})")
    return {"task_id": task_id, "task_url": task_url, "title": params["title"]}


def _exec_update_task(params: dict) -> dict:
    """Execute update_clickup_task tool call."""
    task_id = params["task_id"]
    payload = {}

    if params.get("title"):
        payload["name"] = params["title"]
    if params.get("status"):
        payload["status"] = params["status"]
    if params.get("priority"):
        payload["priority"] = PRIORITY_MAP.get(params["priority"].lower(), 3)

    if params.get("assignee") and params["assignee"] in ASSIGNEE_IDS:
        uid = ASSIGNEE_IDS[params["assignee"]]
        if uid:
            payload["assignees"] = {"add": [int(uid)]}

    if params.get("description"):
        payload["description"] = params["description"]
    elif params.get("append_note"):
        # Fetch existing description first, then append
        try:
            r = httpx.get(
                f"{CLICKUP_BASE}/task/{task_id}",
                headers={"Authorization": CLICKUP_API_TOKEN},
                timeout=15,
            )
            r.raise_for_status()
            existing = r.json().get("description", "") or ""
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            payload["description"] = f"{existing}\n\n[{date_str}] {params['append_note']}".strip()
        except Exception as e:
            print(f"[field_feedback] Failed to fetch task for append: {e}")
            payload["description"] = params["append_note"]

    if payload:
        r = httpx.put(
            f"{CLICKUP_BASE}/task/{task_id}",
            json=payload,
            headers={"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()

    task_url = f"https://app.clickup.com/t/{task_id}"
    print(f"[field_feedback] ClickUp task updated: {task_id}")
    return {"task_id": task_id, "task_url": task_url, "updated_fields": list(payload.keys())}


def _exec_delete_task(params: dict) -> dict:
    """Execute delete_clickup_task tool call."""
    task_id = params["task_id"]
    r = httpx.delete(
        f"{CLICKUP_BASE}/task/{task_id}",
        headers={"Authorization": CLICKUP_API_TOKEN},
        timeout=15,
    )
    r.raise_for_status()
    print(f"[field_feedback] ClickUp task deleted: {task_id} — reason: {params.get('reason', 'n/a')}")
    return {"task_id": task_id, "deleted": True}


def _exec_get_task(params: dict) -> dict:
    """Execute get_clickup_task tool call."""
    task_id = params["task_id"]
    r = httpx.get(
        f"{CLICKUP_BASE}/task/{task_id}",
        headers={"Authorization": CLICKUP_API_TOKEN},
        timeout=15,
    )
    r.raise_for_status()
    task = r.json()
    return {
        "task_id": task_id,
        "title": task.get("name", ""),
        "status": (task.get("status") or {}).get("status", ""),
        "priority": (task.get("priority") or {}).get("priority", ""),
        "assignees": [a.get("username", "") for a in task.get("assignees", [])],
        "description": (task.get("description") or "")[:500],
        "url": task.get("url") or f"https://app.clickup.com/t/{task_id}",
    }


TOOL_EXECUTORS = {
    "create_clickup_task": _exec_create_task,
    "update_clickup_task": _exec_update_task,
    "delete_clickup_task": _exec_delete_task,
    "get_clickup_task": _exec_get_task,
}


# ---------------------------------------------------------------------------
# Feedback map persistence
# ---------------------------------------------------------------------------

def _load_feedback_map() -> dict:
    if FEEDBACK_MAP_PATH.exists():
        try:
            return json.loads(FEEDBACK_MAP_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_feedback_map(data: dict):
    FEEDBACK_MAP_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
        print(f"[field_feedback] Slack reply failed: {e.response['error']}")


def _get_thread_history(thread_ts: str) -> list[dict]:
    """Fetch all messages in a Slack thread for conversation context."""
    try:
        result = _slack.conversations_replies(
            channel=CHANNEL_FIELD_FEEDBACK,
            ts=thread_ts,
            limit=50,
        )
        messages = result.get("messages", [])
        return messages
    except SlackApiError as e:
        print(f"[field_feedback] Failed to fetch thread history: {e.response['error']}")
        return []


def _get_user_name(user_id: str) -> str:
    """Resolve a Slack user ID to a display name."""
    if user_id == SAM_SLACK_ID:
        return "Sam"
    try:
        result = _slack.users_info(user=user_id)
        profile = result.get("user", {}).get("profile", {})
        return profile.get("display_name") or profile.get("real_name") or "Team member"
    except Exception:
        return "Team member"


def _format_thread_for_claude(messages: list[dict], bot_user_id: str | None = None) -> str:
    """Format Slack thread messages into a readable conversation for Claude."""
    lines = []
    for msg in messages:
        user_id = msg.get("user", "")
        text = msg.get("text", "").strip()
        if not text:
            continue

        # Identify the speaker
        if msg.get("bot_id") or user_id == bot_user_id:
            speaker = "Agent (you, in a previous response)"
        elif user_id == SAM_SLACK_ID:
            speaker = "Sam"
        else:
            speaker = _get_user_name(user_id)

        lines.append(f"[{speaker}]: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System prompt for the field feedback agent
# ---------------------------------------------------------------------------

def _build_system_prompt(thread_context: dict) -> str:
    """Build the system prompt with full CONTEXT.md and thread-specific state."""
    task_context = ""
    if thread_context.get("task_id"):
        task_context = (
            f"\n\nTHREAD STATE:\n"
            f"This thread already has a ClickUp task: {thread_context['task_id']}\n"
            f"Task URL: {thread_context.get('task_url', 'unknown')}\n"
            f"Use this task_id for any updates or deletions in this thread."
        )

    return f"""{_CONTEXT_MD}

---

## YOUR ROLE IN #vome-field-feedback

You are the Vome support agent operating inside the #vome-field-feedback Slack channel.
You receive messages from team members — primarily Ron (Sales) and Sam (CEO/Engineer).

Your job is to be a conversational agent. When someone posts feedback, a bug report,
a feature request, a task, or any actionable item:

1. UNDERSTAND what they're telling you. If it's unclear, ask specific follow-up questions.
2. TAKE ACTION using your tools — create ClickUp tasks, update existing ones, or delete them.
3. ALWAYS RESPOND telling the person:
   - What you understood from their message
   - What action you took (with a link to the ClickUp task)
   - Any questions you have if information is missing

Key behaviors:
- Ron often sends fragmented, incomplete info from calls. That's fine. Create the task
  with what you have and ask targeted follow-ups for anything critical that's missing
  (like org name, which platform, etc.). Never ask more than one question at a time.
- Sam may provide more structured input or corrections. Always act on Sam's instructions.
- If someone says to delete, cancel, or remove a task — do it and confirm.
- If someone provides additional context in a thread — update the existing ClickUp task.
- If someone wants to change priority, assignee, or any detail — update the task.
- Route tasks to the correct list: bugs/access → priority_queue, features → raw_intake,
  non-urgent UX → accepted_backlog.
- Assign engineers based on type: frontend/UX → sanjay, backend/data/API → onlyg.
- Be concise. No walls of text. Brief acknowledgment + what you did + link.
- Use the priority and classification rules from the context document above.
{task_context}"""


# ---------------------------------------------------------------------------
# Main conversation loop with Claude
# ---------------------------------------------------------------------------

def _run_agent(thread_ts: str, new_message: str, user_name: str, thread_context: dict) -> tuple[str, dict]:
    """
    Send the conversation to Claude with tools, execute any tool calls,
    and return the final text response + updated context.

    Returns (response_text, updated_thread_context).
    """
    system_prompt = _build_system_prompt(thread_context)

    # Build messages — include thread history if this is a reply
    messages = []
    thread_history = thread_context.get("thread_history", "")
    if thread_history:
        messages.append({
            "role": "user",
            "content": (
                f"Here is the conversation so far in this Slack thread:\n\n"
                f"{thread_history}\n\n"
                f"---\n\n"
                f"New message from {user_name}:\n{new_message}"
            ),
        })
    else:
        messages.append({
            "role": "user",
            "content": f"New message from {user_name} in #vome-field-feedback:\n\n{new_message}",
        })

    # Run Claude with tool use — loop until we get a final text response
    max_iterations = 5
    for _ in range(max_iterations):
        try:
            response = _anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )
        except Exception as e:
            print(f"[field_feedback] Claude API call failed: {e}")
            return "Something went wrong on my end — I'll retry shortly.", thread_context

        # Process the response
        text_parts = []
        tool_results = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_name = block.name
                tool_input = block.input
                tool_id = block.id

                print(f"[field_feedback] Tool call: {tool_name}({json.dumps(tool_input, default=str)[:300]})")

                try:
                    executor = TOOL_EXECUTORS.get(tool_name)
                    if executor:
                        result = executor(tool_input)

                        # Track task ID in thread context
                        if tool_name == "create_clickup_task" and result.get("task_id"):
                            thread_context["task_id"] = result["task_id"]
                            thread_context["task_url"] = result.get("task_url", "")
                        elif tool_name == "delete_clickup_task" and result.get("deleted"):
                            thread_context.pop("task_id", None)
                            thread_context.pop("task_url", None)

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": json.dumps(result, default=str),
                        })
                    else:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": json.dumps({"error": f"Unknown tool: {tool_name}"}),
                            "is_error": True,
                        })
                except Exception as e:
                    print(f"[field_feedback] Tool execution failed: {tool_name} — {e}")
                    traceback.print_exc()
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": json.dumps({"error": str(e)}),
                        "is_error": True,
                    })

        # If there were tool calls, send results back to Claude for the final response
        if tool_results:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue  # Loop for Claude to produce final text after seeing tool results

        # No tool calls — we have the final response
        final_text = "\n".join(text_parts).strip()
        if not final_text:
            final_text = "Got it."

        # Deduplicate: if Claude repeated the same block, keep only the first
        lines = final_text.split("\n")
        if len(lines) > 4:
            half = len(lines) // 2
            first_half = "\n".join(lines[:half]).strip()
            second_half = "\n".join(lines[half:]).strip()
            if first_half and second_half and (
                second_half in first_half or first_half in second_half
            ):
                final_text = first_half if len(first_half) >= len(second_half) else second_half

        return final_text, thread_context

    # Exhausted iterations
    final_text = "\n".join(text_parts).strip() if text_parts else "Got it — task processed."
    return final_text, thread_context


# ---------------------------------------------------------------------------
# Entry point — called from main.py
# ---------------------------------------------------------------------------

def handle_field_feedback(event: dict):
    """
    Route a #vome-field-feedback Slack event to the conversational agent.
    Works for any team member — Ron, Sam, or anyone else.
    """
    text = (event.get("text") or "").strip()
    ts = event.get("ts", "")
    thread_ts = event.get("thread_ts")
    user_id = event.get("user", "")

    if not text:
        return

    # Determine the thread root — new post or reply
    is_reply = thread_ts and thread_ts != ts
    root_ts = thread_ts if is_reply else ts

    # Identify the user
    user_name = _get_user_name(user_id)

    # Load existing thread context from feedback map
    feedback_map = _load_feedback_map()
    thread_context = feedback_map.get(root_ts, {})

    # Fetch thread history for replies
    if is_reply:
        thread_messages = _get_thread_history(root_ts)
        # Get our bot user ID to identify our own messages
        bot_user_id = None
        try:
            auth = _slack.auth_test()
            bot_user_id = auth.get("user_id")
        except Exception:
            pass
        thread_context["thread_history"] = _format_thread_for_claude(thread_messages, bot_user_id)

    # Run the conversational agent
    response_text, updated_context = _run_agent(root_ts, text, user_name, thread_context)

    # Save updated thread context
    updated_context["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    feedback_map[root_ts] = updated_context
    _save_feedback_map(feedback_map)

    # Reply in thread
    _reply(root_ts, response_text)
