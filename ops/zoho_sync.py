"""
ops/zoho_sync.py

Shared helpers for updating Zoho Desk and ClickUp from Command Center actions.
Re-uses existing MCP/REST patterns from agent.py and clickup_tasks.py.
"""

import os
import re

import httpx

from agent import (
    ZOHO_ORG_ID,
    _zoho_desk_call,
    _unwrap_mcp_result,
    fetch_ticket_from_zoho,
    fetch_ticket_conversations,
)

ZOHO_FROM_ADDRESS = os.environ.get(
    "ZOHO_FROM_ADDRESS", "support@vomevolunteer.zohodesk.com"
)

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

# Field IDs for ClickUp custom fields we need to write
FIELD_RESOLUTION = os.environ.get(
    "CLICKUP_FIELD_RESOLUTION",
    "63ef3458-cfa6-4a0b-ae44-18858cd555f0",
)
FIELD_WAKE_DATE = os.environ.get(
    "CLICKUP_FIELD_WAKE_DATE",
    "701fdb31-1341-426a-be88-23d2e10edfec",
)

# Resolution option IDs
RESOLUTION_COMPLETED = "c51c9782-0bca-4c40-a4fe-a272427dc347"
RESOLUTION_DECLINED = "8600a963-ab55-430f-a86f-2b1d0f911156"
RESOLUTION_SLEEPING = "560c14b1-70bb-4387-8d21-941d0543873c"
RESOLUTION_DUPLICATE = "4ad0a7eb-5fb6-4ef1-af0c-0188c7d24a3e"

RESOLUTION_MAP = {
    "completed": RESOLUTION_COMPLETED,
    "declined": RESOLUTION_DECLINED,
    "sleeping": RESOLUTION_SLEEPING,
    "duplicate": RESOLUTION_DUPLICATE,
}

# Team ID lookup
TEAM = {
    "sanjay": {
        "name": "Sanjay Jangid",
        "zoho_id": os.environ.get("SANJAY_ZOHO_AGENT_ID", "569440000023159001"),
        "clickup_id": int(os.environ.get("SANJAY_CLICKUP_ID", "4434086")),
    },
    "onlyg": {
        "name": "OnlyG",
        "zoho_id": os.environ.get("ONLYG_ZOHO_AGENT_ID", "569440000023160001"),
        "clickup_id": int(os.environ.get("ONLYG_CLICKUP_ID", "49257687")),
    },
    "sam": {
        "name": "Sam Fagen",
        "zoho_id": os.environ.get("SAM_ZOHO_AGENT_ID", "569440000000139001"),
        "clickup_id": int(os.environ.get("SAM_CLICKUP_ID", "3691763")),
    },
}


# ---------------------------------------------------------------------------
# Zoho helpers
# ---------------------------------------------------------------------------

def set_zoho_status(ticket_id: str, status: str) -> bool:
    """Update the Zoho ticket status (e.g. 'On Hold', 'Closed')."""
    result = _zoho_desk_call("ZohoDesk_updateTicket", {
        "body": {"status": status},
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(f"[OPS] Zoho status update failed for {ticket_id}")
        return False
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"[OPS] Zoho status update error for {ticket_id}: {data}")
        return False
    print(f"[OPS] Zoho ticket {ticket_id} -> {status}")
    return True


def set_zoho_owner(ticket_id: str, assignee_id: str) -> bool:
    """Update the Zoho ticket owner/assignee."""
    result = _zoho_desk_call("ZohoDesk_updateTicket", {
        "body": {"assigneeId": assignee_id},
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(f"[OPS] Zoho owner update failed for {ticket_id}")
        return False
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"[OPS] Zoho owner update error for {ticket_id}: {data}")
        return False
    print(f"[OPS] Zoho ticket {ticket_id} owner -> {assignee_id}")
    return True


def send_zoho_reply(ticket_id: str, content: str, to_email: str = "") -> dict | None:
    """Send an email reply via Zoho Desk (actually emails the client)."""
    body = {
        "channel": "EMAIL",
        "fromEmailAddress": ZOHO_FROM_ADDRESS,
        "content": content,
        "contentType": "plainText",
    }
    if to_email:
        body["to"] = to_email

    result = _zoho_desk_call("ZohoDesk_sendReply", {
        "body": body,
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })

    if not result:
        print(f"[OPS] sendReply failed for ticket {ticket_id}")
        return None
    if isinstance(result, dict) and result.get("isError"):
        print(f"[OPS] sendReply error for ticket {ticket_id}: {result}")
        return None
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"[OPS] sendReply Zoho error for ticket {ticket_id}: {data}")
        return None
    print(f"[OPS] Reply sent on ticket {ticket_id}")
    return data


def post_internal_note(ticket_id: str, note: str) -> bool:
    """Post an internal (non-public) note on a Zoho Desk ticket."""
    result = _zoho_desk_call("ZohoDesk_createTicketComment", {
        "body": {
            "content": note,
            "isPublic": "false",
            "contentType": "plainText",
        },
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(f"[OPS] Internal note failed for ticket {ticket_id}")
        return False
    print(f"[OPS] Internal note posted on ticket {ticket_id}")
    return True


def get_zoho_ticket_contact_email(ticket_id: str) -> str:
    """Fetch the contact email for a Zoho ticket."""
    result = fetch_ticket_from_zoho(ticket_id)
    if not result:
        return ""
    data = _unwrap_mcp_result(result)
    if not isinstance(data, dict):
        return ""
    contact = data.get("contact") or {}
    return contact.get("email", "")


# ---------------------------------------------------------------------------
# ClickUp helpers
# ---------------------------------------------------------------------------

def _clickup_headers() -> dict:
    return {
        "Authorization": CLICKUP_API_TOKEN,
        "Content-Type": "application/json",
    }


def get_clickup_task(task_id: str) -> dict | None:
    """Fetch a ClickUp task by ID."""
    if not CLICKUP_API_TOKEN or not task_id:
        return None
    try:
        r = httpx.get(
            f"{CLICKUP_BASE}/task/{task_id}",
            headers={"Authorization": CLICKUP_API_TOKEN},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[OPS] ClickUp get task failed ({task_id}): {e}")
        return None


def set_clickup_status(task_id: str, status: str) -> bool:
    """Update a ClickUp task's status."""
    if not CLICKUP_API_TOKEN or not task_id:
        return False
    try:
        r = httpx.put(
            f"{CLICKUP_BASE}/task/{task_id}",
            json={"status": status},
            headers=_clickup_headers(),
            timeout=15,
        )
        r.raise_for_status()
        print(f"[OPS] ClickUp task {task_id} -> {status}")
        return True
    except Exception as e:
        print(f"[OPS] ClickUp status update failed ({task_id}): {e}")
        return False


def set_clickup_assignee(task_id: str, assignee_id: int) -> bool:
    """Set assignee on a ClickUp task (replaces existing)."""
    if not CLICKUP_API_TOKEN or not task_id:
        return False
    try:
        r = httpx.put(
            f"{CLICKUP_BASE}/task/{task_id}",
            json={"assignees": {"add": [assignee_id], "rem": []}},
            headers=_clickup_headers(),
            timeout=15,
        )
        r.raise_for_status()
        print(f"[OPS] ClickUp task {task_id} assignee -> {assignee_id}")
        return True
    except Exception as e:
        print(f"[OPS] ClickUp assignee update failed ({task_id}): {e}")
        return False


def set_clickup_custom_field(task_id: str, field_id: str, value) -> bool:
    """Set a custom field on a ClickUp task."""
    if not CLICKUP_API_TOKEN or not task_id:
        return False
    try:
        r = httpx.post(
            f"{CLICKUP_BASE}/task/{task_id}/field/{field_id}",
            json={"value": value},
            headers=_clickup_headers(),
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[OPS] ClickUp custom field update failed ({task_id}): {e}")
        return False


def get_clickup_comments(task_id: str) -> list:
    """Fetch all comments on a ClickUp task."""
    if not CLICKUP_API_TOKEN or not task_id:
        return []
    try:
        r = httpx.get(
            f"{CLICKUP_BASE}/task/{task_id}/comment",
            headers={"Authorization": CLICKUP_API_TOKEN},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("comments", [])
    except Exception as e:
        print(f"[OPS] ClickUp get comments failed ({task_id}): {e}")
        return []


def extract_zoho_ticket_id_from_task(task: dict) -> str | None:
    """Extract Zoho ticket ID from ClickUp task's custom field."""
    field_id = "4776215b-c725-4d79-8f20-c16f0f0145ac"
    for field in task.get("custom_fields") or []:
        if field.get("id") != field_id:
            continue
        value = field.get("value") or ""
        m = re.search(r"/dv/(\d+)", str(value))
        if m:
            return m.group(1)
        stripped = str(value).strip()
        if stripped.isdigit():
            return stripped
    return None


def extract_custom_field_value(task: dict, field_id: str):
    """Extract a custom field value from a ClickUp task."""
    for field in task.get("custom_fields") or []:
        if field.get("id") == field_id:
            return field.get("value")
    return None
