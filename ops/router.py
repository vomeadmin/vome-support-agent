"""
ops/router.py

FastAPI router mounting all /ops/ endpoints for the Ticket Command Center.
"""

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
)
from pydantic import BaseModel

from ops.auth import verify_ops_token
from ops.tickets import fetch_active_tickets, get_dashboard_stats
from database import get_vic_metrics, get_recent_sweeper_runs
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


class StaleSweepRequest(BaseModel):
    """Manual trigger for the stale awaiting-client sweep.

    dry_run defaults to True here regardless of STALE_SWEEP_DRY_RUN: a hand
    trigger should never close tickets unless the caller says so explicitly.
    """
    dry_run: bool = True
    days: int | None = None
    limit: int | None = None
    force: bool = False
    # None = use STALE_SWEEP_SEND_CLOSING_EMAIL. Pass False to close a
    # historical backlog silently.
    send_email: bool | None = None


class StaleDrainRequest(BaseModel):
    """Clear the whole eligible pool in batches, in the background.

    `confirm` is required and there is no dry_run: a drain only makes sense
    live, since a dry run never shrinks the pool. Preview with the normal
    endpoint first.
    """
    confirm: bool = False
    days: int | None = None
    limit: int | None = None          # closes per batch
    send_email: bool | None = None
    max_batches: int | None = None
    pause_seconds: float | None = None


class EngReportRequest(BaseModel):
    """Manual trigger for an engineering report.

    dry_run defaults to True: a hand trigger posts to #vome-agent-log, never to
    the team channel, so testing a formatting change cannot spam the engineers.
    force skips the once-per-day claim so a fixed report can be re-run.
    """

    kind: str = "friday"
    dry_run: bool = True
    force: bool = False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

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


@ops_router.get("/metrics")
def get_metrics(days: int = Query(30, ge=1, le=365)):
    """Vic performance overview: how many widget chats Vic resolved on
    its own vs. escalated to the team as a ticket, over the last `days`
    days. Includes the deflection rate, today's split, a breakdown by
    resolution type, and the top topics Vic resolved without a ticket.
    """
    return get_vic_metrics(days=days)


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


# ---------------------------------------------------------------------------
# Scheduled sweeps (manual trigger + run history)
# ---------------------------------------------------------------------------

@ops_router.post("/sweeps/stale-waiting-client")
def post_stale_waiting_client_sweep(body: StaleSweepRequest):
    """Run the stale awaiting-client sweep now.

    Imported lazily: the sweeper pulls in agent + ops.zoho_sync, and importing
    it at module load would add that cost to every /ops request.
    """
    from stale_waiting_client_sweeper import run_stale_waiting_client_sweep

    return run_stale_waiting_client_sweep(
        dry_run=body.dry_run,
        days=body.days,
        limit=body.limit,
        force=body.force,
        send_email=body.send_email,
    )


@ops_router.post("/sweeps/stale-waiting-client/drain")
def post_stale_waiting_client_drain(
    body: StaleDrainRequest, background: BackgroundTasks
):
    """Clear the whole eligible pool in self-throttling batches.

    Returns immediately and runs in the background: a few hundred closes takes
    ~15 minutes of API calls, which no gateway will hold a request open for.
    Progress arrives as one Slack report per batch, plus a final summary.

    Live only, and `confirm` must be true. There is no dry-run drain, because
    a dry run does not shrink the pool so the loop would never converge. Use
    POST /sweeps/stale-waiting-client for previews.
    """
    from stale_waiting_client_sweeper import (
        run_stale_waiting_client_drain,
    )

    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail=(
                "Drain closes tickets for real and cannot be previewed. "
                "Pass confirm=true to proceed, or use "
                "POST /sweeps/stale-waiting-client with dry_run=true first."
            ),
        )

    background.add_task(
        run_stale_waiting_client_drain,
        days=body.days,
        limit=body.limit,
        send_email=body.send_email,
        max_batches=body.max_batches,
        pause_seconds=body.pause_seconds,
    )
    return {
        "status": "started",
        "message": (
            "Drain running in the background. Watch the sweep Slack channel: "
            "one report per batch, then a final summary."
        ),
        "days": body.days,
        "batch_size": body.limit,
    }


@ops_router.get("/sweeps/runs")
def get_sweep_runs(limit: int = Query(20, ge=1, le=100)):
    """Recent scheduled sweep runs, newest first."""
    return {"runs": get_recent_sweeper_runs(limit=limit)}


@ops_router.post("/reports/engineering")
def post_engineering_report(body: EngReportRequest):
    """Build and post an engineering report now (read only, writes nothing).

    Imported lazily to match the sweep endpoint: the module pulls in httpx and
    the database helpers, and there is no reason for every /ops request to pay
    that import cost.
    """
    from weekly_engineering_report import run_engineering_report

    return run_engineering_report(
        kind=body.kind,
        dry_run=body.dry_run,
        force=body.force,
    )


@ops_router.get("/reports/engineering/history")
def get_engineering_report_history(
    kind: str = Query("", regex="^(friday|monday|)$"),
    limit: int = Query(12, ge=1, le=52),
):
    """Saved report figures, newest first. Powers week-over-week reporting."""
    from database import get_eng_report_history

    return {"history": get_eng_report_history(report_kind=kind, limit=limit)}
