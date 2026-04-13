"""
ops/router.py

FastAPI router mounting all /ops/ endpoints for the Ticket Command Center.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ops.auth import verify_ops_token
from ops.tickets import fetch_active_tickets, get_dashboard_stats
from ops.thread import fetch_thread
from ops.draft import generate_draft
from ops.send import send_reply
from ops.assign import assign_ticket
from ops.close import close_ticket
from ops.park import park_ticket

ops_router = APIRouter(dependencies=[Depends(verify_ops_token)])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class DraftRequest(BaseModel):
    draft_type: str = "request_info"
    redraft_instruction: str = ""
    engineer_note: str = ""


class SendRequest(BaseModel):
    content: str
    zoho_status_after: str = "On Hold"
    clickup_action: str = "leave"
    assignee_clickup_id: int | None = None
    assignee_zoho_id: str | None = None


class AssignRequest(BaseModel):
    engineer: str
    send_ack: bool = True


class CloseRequest(BaseModel):
    send_closure_note: bool = True
    resolution: str = "completed"
    closure_message: str | None = None


class ParkRequest(BaseModel):
    note: str = ""
    wake_date: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@ops_router.get("/debug-zoho")
def debug_zoho():
    """Debug: test Zoho call from ops module."""
    from agent import _zoho_desk_call, _unwrap_mcp_result, ZOHO_ORG_ID
    raw = _zoho_desk_call("ZohoDesk_getTickets", {
        "query_params": {
            "orgId": str(ZOHO_ORG_ID),
            "from": "0",
            "limit": "5",
        },
    })
    info = {
        "raw_type": str(type(raw)),
        "raw_truthy": bool(raw),
    }
    if isinstance(raw, dict):
        info["raw_keys"] = list(raw.keys())
        content = raw.get("content", [])
        info["content_len"] = len(content)
        if content:
            first = content[0]
            info["first_type"] = first.get("type", "?")
            text_val = first.get("text", "")
            info["text_preview"] = text_val[:200]
    return info


@ops_router.get("/tickets")
def get_tickets(
    filter: str = Query("all", alias="filter"),
    limit: int = Query(50, le=200),
):
    """Return the prioritized ticket queue for the dashboard."""
    tickets = fetch_active_tickets(filter_type=filter, limit=limit)
    stats = get_dashboard_stats(tickets)
    return {
        "tickets": tickets,
        "stats": stats,
        "total": len(tickets),
    }


@ops_router.get("/ticket/{zoho_ticket_id}/thread")
def get_thread(zoho_ticket_id: str):
    """Return the full conversation thread for a ticket."""
    return fetch_thread(zoho_ticket_id)


@ops_router.post("/ticket/{zoho_ticket_id}/draft")
def post_draft(zoho_ticket_id: str, body: DraftRequest):
    """Generate a Claude draft reply for a ticket."""
    return generate_draft(
        zoho_ticket_id=zoho_ticket_id,
        draft_type=body.draft_type,
        redraft_instruction=body.redraft_instruction,
        engineer_note_override=body.engineer_note,
    )


@ops_router.post("/ticket/{zoho_ticket_id}/send")
def post_send(zoho_ticket_id: str, body: SendRequest):
    """Send a reply and sync status across Zoho + ClickUp."""
    return send_reply(
        zoho_ticket_id=zoho_ticket_id,
        content=body.content,
        zoho_status_after=body.zoho_status_after,
        clickup_action=body.clickup_action,
        assignee_clickup_id=body.assignee_clickup_id,
        assignee_zoho_id=body.assignee_zoho_id,
    )


@ops_router.post("/ticket/{zoho_ticket_id}/assign")
def post_assign(zoho_ticket_id: str, body: AssignRequest):
    """Assign a ticket to an engineer."""
    return assign_ticket(
        zoho_ticket_id=zoho_ticket_id,
        engineer=body.engineer,
        send_ack=body.send_ack,
    )


@ops_router.post("/ticket/{zoho_ticket_id}/close")
def post_close(zoho_ticket_id: str, body: CloseRequest):
    """Close a ticket completely."""
    return close_ticket(
        zoho_ticket_id=zoho_ticket_id,
        send_closure_note=body.send_closure_note,
        resolution=body.resolution,
        closure_message=body.closure_message,
    )


@ops_router.post("/ticket/{zoho_ticket_id}/park")
def post_park(zoho_ticket_id: str, body: ParkRequest):
    """Park a ticket."""
    return park_ticket(
        zoho_ticket_id=zoho_ticket_id,
        note=body.note,
        wake_date=body.wake_date,
    )
