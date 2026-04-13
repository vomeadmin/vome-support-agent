"""
ops/tickets.py

GET /ops/tickets — returns the prioritized ticket queue for the dashboard.
Merges data from Zoho Desk, PostgreSQL ticket_threads, and ClickUp.
"""

import os
import re
from datetime import datetime, timezone

import httpx

from agent import (
    ZOHO_ORG_ID,
    _zoho_desk_call,
    _unwrap_mcp_result,
    _normalize_tier,
)
from database import _get_engine
from sqlalchemy import text

from ops.scoring import compute_priority_score
from ops.zoho_sync import (
    CLICKUP_API_TOKEN,
    CLICKUP_BASE,
    get_clickup_task,
    get_clickup_comments,
    extract_custom_field_value,
    TEAM,
)


# ClickUp field IDs
FIELD_AUTO_SCORE = "fd77f978-eca8-499e-bc3c-dc1bf4b8181e"
FIELD_HIGHEST_TIER = "be348a1d-6a63-4da8-83bb-9038b24264ff"
FIELD_COMBINED_ARR = "29c41859-f24b-4143-9af4-a34202205641"

# Priority list IDs
LIST_PRIORITY_QUEUE = os.environ.get("CLICKUP_PRIORITY_QUEUE_ID", "901113386257")

# ClickUp user ID -> name
CLICKUP_USER_NAMES = {
    4434086: "Sanjay Jangid",
    49257687: "OnlyG",
    3691763: "Sam Fagen",
    4434980: "Ron Segev",
}


def _normalize_zoho_status(status: str | None) -> str:
    """Normalize Zoho status to lowercase key for scoring."""
    if not status:
        return "new"
    s = status.strip().lower()
    mapping = {
        "new": "new",
        "open": "new",
        "processing": "processing",
        "in progress": "processing",
        "on hold": "waiting",
        "final review": "final_review",
        "closed": "closed",
    }
    return mapping.get(s, s)


def _days_since(dt_str: str | None) -> int:
    """Compute days since a Zoho datetime string."""
    if not dt_str:
        return 0
    try:
        # Zoho uses ISO format: 2026-04-10T09:44:43.000Z
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        return max(0, delta.days)
    except Exception:
        return 0


def _derive_p_level(priority: str | None) -> str:
    """Convert ClickUp priority name to P1/P2/P3."""
    if not priority:
        return "P3"
    p = priority.strip().lower()
    if p == "urgent":
        return "P1"
    if p == "high":
        return "P2"
    return "P3"


def _get_clickup_priority_name(priority_val) -> str:
    """Convert ClickUp priority int/dict to name."""
    if isinstance(priority_val, dict):
        return (priority_val.get("priority") or "normal").lower()
    if isinstance(priority_val, int):
        return {1: "urgent", 2: "high", 3: "normal", 4: "low"}.get(
            priority_val, "normal"
        )
    return "normal"


def fetch_active_tickets(
    filter_type: str = "all",
    limit: int = 50,
) -> list[dict]:
    """
    Build the full prioritized ticket list for the dashboard.

    Steps:
    1. Query PostgreSQL for all open ticket_threads rows
    2. Fetch active Zoho tickets (status: New, Processing, On Hold, Final Review)
    3. Merge: use DB row as base, enrich with live Zoho data
    4. Fetch ClickUp task data for each ticket (status, assignee, comments)
    5. Score and sort
    """
    engine = _get_engine()
    tickets_by_zoho_id: dict[str, dict] = {}

    # -----------------------------------------------------------------------
    # Step 1: Load ticket_threads from PostgreSQL
    # -----------------------------------------------------------------------
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM ticket_threads "
                    "WHERE status NOT IN ('handled', 'closed') "
                    "ORDER BY created_at DESC "
                    "LIMIT 200"
                )
            ).mappings().all()

        for row in rows:
            tid = row["ticket_id"]
            crm = row["crm"]
            if isinstance(crm, str):
                import json
                crm = json.loads(crm)

            classification = row["classification"]
            if isinstance(classification, str):
                import json
                classification = json.loads(classification)

            tickets_by_zoho_id[tid] = {
                "zoho_ticket_id": tid,
                "zoho_ticket_number": row["ticket_number"] or "",
                "subject": row["subject"] or "",
                "clickup_task_id": row["clickup_task_id"] or "",
                "crm_data": crm or {},
                "classification": classification or {},
                "db_status": row["status"] or "open",
                "pending_info": row.get("pending_info", False),
                "missing_info": row.get("missing_info", ""),
                "engineer_note": row.get("engineer_note", ""),
                "parked": row.get("parked", False),
            }
    except Exception as e:
        print(f"[OPS] DB query failed: {e}")

    # -----------------------------------------------------------------------
    # Step 2: Fetch active Zoho tickets
    # -----------------------------------------------------------------------
    zoho_tickets = _fetch_zoho_active_tickets()
    for zt in zoho_tickets:
        tid = str(zt.get("id", ""))
        if not tid:
            continue
        if tid in tickets_by_zoho_id:
            # Merge Zoho live data into DB row
            tickets_by_zoho_id[tid]["_zoho_live"] = zt
        else:
            # Ticket exists in Zoho but not in our DB (rare — created outside agent)
            tickets_by_zoho_id[tid] = {
                "zoho_ticket_id": tid,
                "zoho_ticket_number": str(zt.get("ticketNumber", "")),
                "subject": zt.get("subject", ""),
                "clickup_task_id": "",
                "crm_data": {},
                "classification": {},
                "db_status": "open",
                "pending_info": False,
                "missing_info": "",
                "engineer_note": "",
                "parked": False,
                "_zoho_live": zt,
            }

    # -----------------------------------------------------------------------
    # Step 3: Enrich each ticket with Zoho + ClickUp data, score
    # -----------------------------------------------------------------------
    result_list = []

    for tid, t in tickets_by_zoho_id.items():
        # Skip parked tickets unless specifically filtered
        if t.get("parked") and filter_type != "parked":
            continue

        zoho = t.get("_zoho_live", {})
        crm = t.get("crm_data") or {}
        classification = t.get("classification") or {}

        # Zoho fields
        zoho_status = zoho.get("status") or "New"
        zoho_status_normalized = _normalize_zoho_status(zoho_status)

        # Skip closed tickets unless viewing resolved
        if zoho_status_normalized == "closed" and filter_type != "resolved":
            continue

        contact = zoho.get("contact") or {}
        contact_email = contact.get("email", "")
        org_name = crm.get("org_name", "") or crm.get("account_name", "")
        tier = _normalize_tier(crm.get("offering"))
        arr = crm.get("arr", 0) or 0
        if isinstance(arr, str):
            arr = int(re.sub(r"[^\d]", "", arr) or "0")

        # Timestamps
        last_activity = (
            zoho.get("modifiedTime")
            or zoho.get("createdTime")
            or ""
        )
        days_since_update = _days_since(last_activity)

        # ClickUp data
        cu_task_id = t.get("clickup_task_id", "")
        cu_status = ""
        cu_priority = "normal"
        cu_assignee_name = ""
        cu_assignee_id = None
        cu_link = ""
        auto_score = 0
        engineer_comment = t.get("engineer_note", "")

        if cu_task_id:
            cu_task = get_clickup_task(cu_task_id)
            if cu_task:
                cu_status = (cu_task.get("status", {}).get("status") or "").lower()
                cu_priority = _get_clickup_priority_name(cu_task.get("priority"))
                cu_link = cu_task.get("url", f"https://app.clickup.com/t/{cu_task_id}")

                # Assignee
                assignees = cu_task.get("assignees") or []
                if assignees:
                    cu_assignee_id = assignees[0].get("id")
                    cu_assignee_name = (
                        assignees[0].get("username")
                        or CLICKUP_USER_NAMES.get(cu_assignee_id, "")
                    )

                # Auto score from custom field
                auto_score_val = extract_custom_field_value(cu_task, FIELD_AUTO_SCORE)
                if auto_score_val:
                    try:
                        auto_score = int(float(str(auto_score_val)))
                    except (ValueError, TypeError):
                        pass

                # Fetch latest engineer comment if status needs it
                if cu_status in ("needs review", "waiting on client") and not engineer_comment:
                    comments = get_clickup_comments(cu_task_id)
                    for c in reversed(comments):
                        commenter = c.get("user", {}).get("username", "")
                        if commenter and commenter.lower() not in ("sam", "sam fagen"):
                            comment_text_parts = []
                            for ct in c.get("comment", []):
                                if ct.get("type") == "text":
                                    comment_text_parts.append(ct.get("text", ""))
                            if comment_text_parts:
                                engineer_comment = f"{commenter}: {''.join(comment_text_parts)}"
                                break

        # Also check: ClickUp status may override what we show
        # "needs_review" from ClickUp means Sam needs to act
        effective_status = zoho_status_normalized
        if cu_status in ("needs review",):
            effective_status = "needs_review"
        elif cu_status in ("waiting on client",):
            effective_status = "waiting"

        # Determine Zoho link
        zoho_link = (
            f"https://desk.zoho.com/agent/vomevolunteer/"
            f"vomevolunteer/tickets/details/{tid}"
        )

        # Build scoring dict
        scoring_data = {
            "auto_score": auto_score,
            "tier": tier,
            "arr_dollars": arr,
            "priority": cu_priority,
            "zoho_status_normalized": effective_status,
            "days_since_update": days_since_update,
        }
        priority_score = compute_priority_score(scoring_data)

        module = classification.get("module", "")
        platform = classification.get("platform", "")
        summary = classification.get("summary", "")
        has_attachment = bool(zoho.get("attachmentCount"))

        # Language detection
        language = classification.get("language", "en")

        ticket_out = {
            "zoho_ticket_id": tid,
            "zoho_ticket_number": t.get("zoho_ticket_number", ""),
            "zoho_status": zoho_status,
            "zoho_status_normalized": effective_status,
            "zoho_link": zoho_link,
            "clickup_task_id": cu_task_id,
            "clickup_status": cu_status,
            "clickup_link": cu_link,
            "subject": t.get("subject", ""),
            "summary": summary,
            "org_name": org_name,
            "tier": tier,
            "arr_dollars": arr,
            "priority": cu_priority,
            "p_level": _derive_p_level(cu_priority),
            "module": module,
            "platform": platform,
            "assignee_name": cu_assignee_name,
            "assignee_clickup_id": cu_assignee_id,
            "assignee_zoho_id": "",
            "contact_email": contact_email,
            "missing_info": t.get("missing_info", ""),
            "engineer_comment": engineer_comment,
            "days_since_update": days_since_update,
            "priority_score": priority_score,
            "language": language,
            "has_attachment": has_attachment,
            "resolved": zoho_status_normalized == "closed",
        }

        result_list.append(ticket_out)

    # -----------------------------------------------------------------------
    # Step 4: Filter
    # -----------------------------------------------------------------------
    result_list = _apply_filter(result_list, filter_type)

    # -----------------------------------------------------------------------
    # Step 5: Sort by priority score descending
    # -----------------------------------------------------------------------
    result_list.sort(key=lambda x: x["priority_score"], reverse=True)

    return result_list[:limit]


def _fetch_zoho_active_tickets() -> list[dict]:
    """Fetch tickets from Zoho Desk with active statuses."""
    all_tickets = []

    for status in ("New", "Open", "Processing", "On Hold", "Final Review"):
        result = _zoho_desk_call("ZohoDesk_getTickets", {
            "query_params": {
                "orgId": str(ZOHO_ORG_ID),
                "status": status,
                "from": 0,
                "limit": 100,
                "sortBy": "modifiedTime",
                "include": "contacts",
            },
        })
        if not result:
            continue
        data = _unwrap_mcp_result(result)
        if isinstance(data, dict):
            all_tickets.extend(data.get("data", []))
        elif isinstance(data, list):
            all_tickets.extend(data)

    # Deduplicate by ticket ID
    seen = set()
    deduped = []
    for t in all_tickets:
        tid = str(t.get("id", ""))
        if tid and tid not in seen:
            seen.add(tid)
            deduped.append(t)

    return deduped


def _apply_filter(tickets: list[dict], filter_type: str) -> list[dict]:
    """Apply dashboard filter tabs."""
    if filter_type == "all":
        return [t for t in tickets if not t["resolved"]]
    if filter_type == "p1":
        return [t for t in tickets if t["p_level"] == "P1"]
    if filter_type == "bugs":
        return [
            t for t in tickets
            if "bug" in (t.get("module") or "").lower()
            or t.get("priority") == "urgent"
        ]
    if filter_type == "needs_review":
        return [
            t for t in tickets
            if t["zoho_status_normalized"] == "needs_review"
            or t["clickup_status"] in ("needs review",)
        ]
    if filter_type == "waiting":
        return [
            t for t in tickets
            if t["zoho_status_normalized"] == "waiting"
            or t["clickup_status"] in ("waiting on client",)
        ]
    if filter_type == "final_review":
        return [
            t for t in tickets
            if t["zoho_status_normalized"] == "final_review"
        ]
    if filter_type == "resolved":
        return [t for t in tickets if t["resolved"]]
    if filter_type == "unassigned":
        return [
            t for t in tickets
            if not t["assignee_clickup_id"] and not t["resolved"]
        ]
    return tickets


def get_dashboard_stats(tickets: list[dict]) -> dict:
    """Compute summary stats for the dashboard header."""
    need_response = sum(
        1 for t in tickets
        if t["zoho_status_normalized"] in ("new", "needs_review")
    )
    needs_review = sum(
        1 for t in tickets
        if t["clickup_status"] in ("needs review",)
    )
    waiting_on_client = sum(
        1 for t in tickets
        if t["zoho_status_normalized"] == "waiting"
    )
    resolved_today = 0  # TODO: compute from resolved tickets today

    return {
        "need_response": need_response,
        "needs_review": needs_review,
        "waiting_on_client": waiting_on_client,
        "resolved_today": resolved_today,
    }
