"""
stale_waiting_client_sweeper.py

Daily sweep that closes tickets parked in Zoho "Awaiting Client Response"
when the client has not replied for N days (default 30).

Scheduled from main.py at 07:30 America/Montreal. Also triggerable by hand:
    POST /ops/sweeps/stale-waiting-client   {"dry_run": true}

WHY THIS EXISTS
    clickup_waiting_client_handler parks tickets in Zoho
    "Awaiting Client Response" + ClickUp "awaiting client response". Nothing
    ever un-parks them if the client simply never answers, and
    ops.tickets._fetch_zoho_active_tickets deliberately excludes that status
    from the Command Center queue, so the pile is invisible. This closes it.

THE TWO RULES THAT KEEP THIS SAFE
 1. Staleness is measured from the LAST OUTBOUND EMAIL, never modifiedTime.
    modifiedTime moves on every tag write, status sync and sync_zoho_to_clickup
    pass, so a ticket would keep resetting its own clock and never age out. If
    no outbound timestamp can be resolved (Zoho or the DB), the ticket is
    SKIPPED, never closed on a guessed date.
 2. Both systems must agree the ticket is parked. If Zoho says awaiting-client
    but the ClickUp task sits in "in progress" / "on dev", that is live dev
    work and closing Zoho would kill the visible half of it. Those are
    reported to Slack as drift instead of being closed.

Closing is reversible by design: a client reply on a closed ticket flips it
back to Processing and resurfaces it in Slack (see is_zoho_reply_event ->
process_ticket_update in main.py, and test_closed_reply_guard.py).
"""

import os
import time
from datetime import datetime, timezone

import httpx

from agent import (
    ZOHO_ORG_ID,
    NOTE_HEADER,
    UPDATE_HEADER,
    TEAM_EMAILS,
    _zoho_desk_call,
    _unwrap_mcp_result,
)
from database import (
    get_thread_by_ticket_id,
    update_thread,
    claim_sweeper_run,
    finish_sweeper_run,
)
from ops.zoho_sync import (
    FIELD_RESOLUTION,
    RESOLUTION_MAP,
    set_zoho_status,
    set_clickup_status,
    set_clickup_custom_field,
    send_zoho_reply,
    post_internal_note,
)
from signatures import sign_message
from slack import post_to_log
from status_constants import (
    normalize_status,
    CU_AWAITING_CLIENT,
    CU_WAITING_ON_CLIENT,
    CU_DONE,
    CU_WRITE_CLOSED_UPPER,
    ZOHO_AWAITING_CLIENT_RESPONSE,
    ZOHO_CLOSED,
    ZOHO_TAG_WAITING_CLIENT,
    THREAD_CLOSED,
)

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

# ---------------------------------------------------------------------------
# Configuration (all env-overridable so the rollout can be tuned without a
# deploy). DRY RUN DEFAULTS TO ON: the first live run would otherwise hit the
# entire historical backlog in one pass.
# ---------------------------------------------------------------------------

STALE_DAYS = int(os.environ.get("STALE_WAITING_DAYS", "30"))
DRY_RUN_DEFAULT = os.environ.get(
    "STALE_SWEEP_DRY_RUN", "true"
).strip().lower() in ("1", "true", "yes")
MAX_CLOSES = int(os.environ.get("STALE_SWEEP_MAX_CLOSES", "25"))
SEND_CLOSING_EMAIL = os.environ.get(
    "STALE_SWEEP_SEND_CLOSING_EMAIL", "true"
).strip().lower() in ("1", "true", "yes")
ZOHO_DEPARTMENT_ID = os.environ.get(
    "STALE_SWEEP_DEPARTMENT_ID", "569440000000006907"
)
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL_STALE_SWEEP", "")

# Pause between tickets so a large sweep does not trip Zoho rate limits.
# Each ticket costs ~1 read call, each close ~4 write calls.
THROTTLE_SECONDS = float(os.environ.get("STALE_SWEEP_THROTTLE", "0.4"))

# Resolution key written to the ClickUp resolution custom field. "no_response"
# only resolves if CLICKUP_RESOLUTION_NO_RESPONSE is set (see
# ops/zoho_sync.py); when it is not, the field write is skipped rather than
# the ticket being mislabeled "completed".
RESOLUTION_KEY = os.environ.get("STALE_SWEEP_RESOLUTION", "no_response")

# Marker on the internal note so an auto-close is always identifiable.
AUTO_CLOSE_MARKER = "[vic-auto-close-stale]"

# ClickUp statuses that agree the ticket is parked on the client.
_CU_PARKED = {CU_AWAITING_CLIENT, CU_WAITING_ON_CLIENT}
# ClickUp statuses that are already finished: no ClickUp write needed, and no
# conflict with closing Zoho.
_CU_FINISHED = {CU_DONE, "closed", "complete", "completed"}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_zoho_dt(value: str | None) -> datetime | None:
    """Parse a Zoho ISO timestamp ("2026-04-10T09:44:43.000Z") to aware UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Treat a naive DB timestamp as UTC (that is how they are written)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _days_between(then: datetime, now: datetime) -> int:
    return max(0, (now - then).days)


# ---------------------------------------------------------------------------
# Zoho: find the parked tickets
# ---------------------------------------------------------------------------

def _fetch_awaiting_client_tickets() -> tuple[list[dict], bool]:
    """Every Zoho ticket currently in "Awaiting Client Response".

    Returns (tickets, paging_cap_hit). Pages the list endpoint. The status
    filter is passed to Zoho AND applied client-side, so an unsupported or
    ignored query param cannot widen the sweep to tickets we must not touch.

    paging_cap_hit is True when all 5 pages came back full, i.e. the parked
    pile may be larger than we looked at. Surfaced in the Slack report so a
    truncated scan never reads as "we covered everything".
    """
    target = normalize_status(ZOHO_AWAITING_CLIENT_RESPONSE)
    seen: set[str] = set()
    found: list[dict] = []
    exhausted = False

    for offset in range(0, 500, 100):
        result = _zoho_desk_call("ZohoDesk_getTickets", {
            "query_params": {
                "orgId": str(ZOHO_ORG_ID),
                "departmentId": ZOHO_DEPARTMENT_ID,
                "status": ZOHO_AWAITING_CLIENT_RESPONSE,
                "include": "contacts",
                "from": str(offset),
                "limit": "100",
            },
        })
        data = _unwrap_mcp_result(result)
        batch = data.get("data") if isinstance(data, dict) else data
        if not isinstance(batch, list) or not batch:
            exhausted = True
            break

        for t in batch:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or "")
            if not tid or tid in seen:
                continue
            if normalize_status(t.get("status")) != target:
                continue
            seen.add(tid)
            found.append(t)

        if len(batch) < 100:
            exhausted = True
            break

    if not exhausted:
        print(
            "[STALE SWEEP] WARNING: hit the 500-ticket paging cap; more "
            "parked tickets may exist and will be picked up next run"
        )

    print(
        f"[STALE SWEEP] {len(found)} tickets in "
        f"{ZOHO_AWAITING_CLIENT_RESPONSE}"
    )
    return found, not exhausted


def _fetch_conversation_entries(ticket_id: str) -> list[dict]:
    """Raw conversation entries for a ticket, as Zoho returns them.

    Deliberately NOT agent.fetch_ticket_conversations: that hydrates every
    thread body with an extra getThread call per message. The sweep only needs
    timestamps and author types, so hydration would multiply the API cost of a
    50-ticket run for nothing.
    """
    result = _zoho_desk_call("ZohoDesk_getTicketConversations", {
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {
            "orgId": str(ZOHO_ORG_ID),
            "from": 0,
            "limit": 100,
        },
    })
    data = _unwrap_mcp_result(result)
    entries = data.get("data") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _entry_direction(entry: dict, contact_email: str) -> str:
    """Classify one conversation entry as "out" (us), "in" (client) or "".

    Mirrors the author checks in agent._get_latest_client_reply and
    agent._has_agent_reply so this agrees with the rest of the pipeline.
    """
    # Internal comments are not email traffic in either direction.
    if not entry.get("isPublic", True):
        return ""
    if entry.get("type") == "comment":
        return ""
    content = entry.get("content", "") or ""
    if NOTE_HEADER in content or UPDATE_HEADER in content:
        return ""

    author = entry.get("author", {}) or {}
    author_type = (author.get("type") or "").upper()
    author_email = (author.get("email") or "").lower()

    if author_type == "AGENT" or author_email in TEAM_EMAILS:
        return "out"
    if entry.get("direction") == "out":
        return "out"

    # The original ticket body is inbound client traffic, but it is always
    # older than our reply so it can never look like "they answered us".
    if entry.get("isDescriptionThread"):
        return "in"

    is_client = (
        author_type == "END_USER"
        or (author_email and author_email == contact_email.lower())
        or (author_email and author_email not in TEAM_EMAILS
            and author_type != "AGENT")
    )
    return "in" if is_client else ""


def _last_email_times(
    ticket_id: str, contact_email: str
) -> tuple[datetime | None, datetime | None]:
    """(last_outbound_at, last_inbound_at) from the ticket conversation.

    Sorted explicitly rather than trusting Zoho's ordering.
    """
    outbound: list[datetime] = []
    inbound: list[datetime] = []

    for entry in _fetch_conversation_entries(ticket_id):
        when = _parse_zoho_dt(
            entry.get("createdTime") or entry.get("modifiedTime")
        )
        if not when:
            continue
        direction = _entry_direction(entry, contact_email)
        if direction == "out":
            outbound.append(when)
        elif direction == "in":
            inbound.append(when)

    return (
        max(outbound) if outbound else None,
        max(inbound) if inbound else None,
    )


# ---------------------------------------------------------------------------
# ClickUp
# ---------------------------------------------------------------------------

def _get_clickup_status(task_id: str) -> str | None:
    """Normalized ClickUp status name, or None if the task is unreadable."""
    if not CLICKUP_API_TOKEN or not task_id:
        return None
    try:
        r = httpx.get(
            f"{CLICKUP_BASE}/task/{task_id}",
            headers={"Authorization": CLICKUP_API_TOKEN},
            timeout=15,
        )
        r.raise_for_status()
        status = (r.json().get("status") or {}).get("status")
        return normalize_status(status)
    except Exception as e:
        print(f"[STALE SWEEP] ClickUp get task failed ({task_id}): {e}")
        return None


def _add_clickup_comment(task_id: str, text: str) -> bool:
    """Post a comment on a ClickUp task (handlers keep their own copy)."""
    if not CLICKUP_API_TOKEN or not task_id:
        return False
    try:
        r = httpx.post(
            f"{CLICKUP_BASE}/task/{task_id}/comment",
            json={"comment_text": text, "notify_all": False},
            headers={
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[STALE SWEEP] ClickUp comment failed ({task_id}): {e}")
        return False


# ---------------------------------------------------------------------------
# Client-facing closing note
# ---------------------------------------------------------------------------

_CLOSING_BODY_EN = (
    "Hi {name},\n\n"
    "We have not heard back on this one, so we are closing the ticket for "
    "now.\n\n"
    "Nothing is lost. If you reply to this email the ticket reopens and comes "
    "straight back to our team, with the full history attached."
)

_CLOSING_BODY_FR = (
    "Bonjour {name},\n\n"
    "Nous n'avons pas eu de retour de votre part, alors nous fermons ce "
    "ticket pour le moment.\n\n"
    "Rien n'est perdu. Si vous repondez a ce courriel, le ticket est rouvert "
    "et revient directement a notre equipe, avec tout l'historique."
)


def _closing_message(contact_first_name: str, language: str | None) -> str:
    """Fixed-template closing note, signed as Vic.

    Deliberately NOT an LLM draft: a sweep runs unattended over many tickets,
    so the copy must be deterministic, reviewable and free.
    """
    is_fr = str(language or "").lower().startswith("fr")
    template = _CLOSING_BODY_FR if is_fr else _CLOSING_BODY_EN
    name = (contact_first_name or "").strip()
    if not name:
        name = "la" if is_fr else "there"
    body = template.format(name=name)
    return sign_message(body, "vic", language or "en")


def _internal_note(days_idle: int, last_out: datetime, source: str) -> str:
    return (
        f"Vic closed this ticket automatically after {days_idle} days with no "
        f"client response.\n"
        f"Last outbound email: {last_out.strftime('%Y-%m-%d %H:%M UTC')} "
        f"(source: {source}).\n"
        f"The ticket reopens on any client reply.\n"
        f"{AUTO_CLOSE_MARKER}"
    )


# ---------------------------------------------------------------------------
# Per-ticket assessment
# ---------------------------------------------------------------------------

def _assess(ticket: dict, now: datetime, days: int) -> dict:
    """Decide what to do with one parked ticket.

    Returns a verdict dict whose "action" is one of:
        close                 stale, both systems agree, safe to close
        skip_recent           not idle long enough yet
        skip_client_replied   client answered after our last email
        skip_no_timestamp     could not resolve our last outbound email
        skip_clickup_busy     ClickUp task is in active dev work (drift)
    """
    ticket_id = str(ticket.get("id") or "")
    contact = ticket.get("contact") or {}
    contact_email = contact.get("email") or ticket.get("email") or ""

    verdict = {
        "ticket_id": ticket_id,
        "ticket_number": str(ticket.get("ticketNumber") or ""),
        "subject": (ticket.get("subject") or "")[:80],
        "contact_email": contact_email,
        "contact_first_name": contact.get("firstName") or "",
        "clickup_task_id": "",
        "clickup_status": "",
        "language": None,
        "days_idle": 0,
        "last_outbound": None,
        "timestamp_source": "",
        "action": "",
        "reason": "",
        "has_waiting_tag": (
            ZOHO_TAG_WAITING_CLIENT in (ticket.get("tags") or [])
        ),
    }

    # --- our last email out, and anything the client sent after it ----------
    last_out, last_in = _last_email_times(ticket_id, contact_email)
    source = "zoho_outbound_thread"

    db_row = get_thread_by_ticket_id(ticket_id)
    if db_row:
        _, row = db_row
        verdict["clickup_task_id"] = row.get("clickup_task_id") or ""
        classification = row.get("classification") or {}
        if isinstance(classification, dict):
            verdict["language"] = classification.get("language")
        if last_out is None:
            # Fallback: when the waiting-client handler parked the ticket.
            fallback = (
                _as_utc(row.get("last_action_at"))
                or _as_utc(row.get("updated_at"))
            )
            if fallback:
                last_out = fallback
                source = "db_last_action_at"

    if last_out is None:
        verdict["action"] = "skip_no_timestamp"
        verdict["reason"] = "no outbound email timestamp in Zoho or the DB"
        return verdict

    verdict["last_outbound"] = last_out
    verdict["timestamp_source"] = source
    verdict["days_idle"] = _days_between(last_out, now)

    if last_in and last_in > last_out:
        verdict["action"] = "skip_client_replied"
        verdict["reason"] = (
            f"client replied {last_in.strftime('%Y-%m-%d')}, after our "
            f"{last_out.strftime('%Y-%m-%d')} email; the parked status is "
            f"stale"
        )
        return verdict

    if verdict["days_idle"] < days:
        verdict["action"] = "skip_recent"
        verdict["reason"] = f"only {verdict['days_idle']}d idle"
        return verdict

    # --- rule 2: both systems must agree the ticket is parked ---------------
    task_id = verdict["clickup_task_id"]
    if task_id:
        cu_status = _get_clickup_status(task_id)
        verdict["clickup_status"] = cu_status or "unreadable"
        if cu_status and cu_status not in _CU_PARKED | _CU_FINISHED:
            verdict["action"] = "skip_clickup_busy"
            verdict["reason"] = (
                f"ClickUp task is '{cu_status}', not parked on the client"
            )
            return verdict

    verdict["action"] = "close"
    verdict["reason"] = f"{verdict['days_idle']}d with no client response"
    return verdict


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _close_one(verdict: dict, send_email: bool) -> dict:
    """Do the actual closing. Returns {"errors": [...]}."""
    ticket_id = verdict["ticket_id"]
    task_id = verdict["clickup_task_id"]
    errors: list[str] = []

    # 1. Courtesy note to the client, BEFORE the status flips, so a reply
    #    lands on a ticket whose history is still intact.
    if send_email:
        if verdict["contact_email"]:
            message = _closing_message(
                verdict["contact_first_name"], verdict["language"]
            )
            if not send_zoho_reply(
                ticket_id, message, verdict["contact_email"]
            ):
                errors.append("closing email failed")
        else:
            errors.append("no contact email, closing note skipped")

    # 2. Zoho -> Closed. This is the gate for the ClickUp write below: if Zoho
    #    will not close, closing ClickUp would manufacture exactly the drift
    #    (one side closed, the other parked) that rule 2 exists to catch.
    zoho_closed = set_zoho_status(ticket_id, ZOHO_CLOSED)
    if not zoho_closed:
        errors.append("Zoho status -> Closed failed, ClickUp left untouched")

    # 3. Internal note (isPublic=false, never emails the client)
    note = _internal_note(
        verdict["days_idle"],
        verdict["last_outbound"],
        verdict["timestamp_source"],
    )
    if not post_internal_note(ticket_id, note):
        errors.append("Zoho internal note failed")

    # 4. ClickUp: same note (whoever reopens this may land in ClickUp, not
    #    Zoho), then close, then label the resolution.
    if task_id and zoho_closed:
        _add_clickup_comment(task_id, note)
        if verdict["clickup_status"] not in _CU_FINISHED:
            if not set_clickup_status(task_id, CU_WRITE_CLOSED_UPPER):
                errors.append("ClickUp status -> CLOSED failed")
        resolution_id = RESOLUTION_MAP.get(RESOLUTION_KEY)
        if resolution_id:
            set_clickup_custom_field(task_id, FIELD_RESOLUTION, resolution_id)
        else:
            print(
                f"[STALE SWEEP] resolution '{RESOLUTION_KEY}' has no ClickUp "
                f"option id, field left unset on task {task_id}"
            )

    # 5. DB. Also gated on the Zoho write: our thread status must never claim
    #    closed while Zoho still shows the ticket parked, or the next run would
    #    not even see it as needing a retry.
    if zoho_closed:
        db_row = get_thread_by_ticket_id(ticket_id)
        if db_row:
            try:
                update_thread(
                    db_row[0],
                    status=THREAD_CLOSED,
                    last_action="auto_closed_no_client_response",
                    last_action_at=datetime.now(timezone.utc),
                )
            except Exception as e:
                errors.append(f"DB update failed: {e}")

    return {"errors": errors, "closed": zoho_closed}


# ---------------------------------------------------------------------------
# Slack report
# ---------------------------------------------------------------------------

def _ticket_line(v: dict) -> str:
    num = f"#{v['ticket_number']}" if v["ticket_number"] else v["ticket_id"]
    return f"* {num} ({v['days_idle']}d) {v['subject']}"


def _build_report(summary: dict) -> str:
    mode = (
        "DRY RUN, nothing was changed" if summary["dry_run"] else "live"
    )
    lines = [
        f"*Stale awaiting-client sweep* ({mode})",
        f"Threshold: {summary['days']} days with no client response.",
        f"Parked tickets scanned: {summary['scanned']}",
    ]

    if summary.get("paging_cap_hit"):
        lines.append(
            ":warning: Paging cap reached, so this scan may be incomplete. "
            "The rest get picked up on the next run."
        )

    closed = summary["closed"]
    lines.append(
        f"{'Would close' if summary['dry_run'] else 'Closed'}: {len(closed)}"
    )
    for v in closed[:20]:
        lines.append(_ticket_line(v))
    if len(closed) > 20:
        lines.append(f"  ...and {len(closed) - 20} more")

    if summary["capped"]:
        left = summary["eligible_total"] - summary["max_closes"]
        lines.append(
            f":warning: Capped at {summary['max_closes']} per run. {left} "
            f"eligible tickets were left for the next run."
        )

    if summary.get("failed"):
        lines.append(
            f":x: *Close failed* ({len(summary['failed'])}): still parked, "
            f"the next run retries them."
        )
        for v in summary["failed"][:10]:
            lines.append(_ticket_line(v))

    drift = summary["skipped"].get("skip_clickup_busy", [])
    if drift:
        lines.append(
            f"\n:warning: *Status drift* ({len(drift)}): Zoho says "
            f"awaiting-client, ClickUp says active work. Not closed."
        )
        for v in drift[:10]:
            lines.append(
                f"{_ticket_line(v)}, ClickUp: {v['clickup_status']}"
            )

    replied = summary["skipped"].get("skip_client_replied", [])
    if replied:
        lines.append(
            f"\n:warning: *Client already replied* ({len(replied)}): still "
            f"parked in Zoho, so the reply webhook likely missed them. Not "
            f"closed."
        )
        for v in replied[:10]:
            lines.append(f"{_ticket_line(v)}, {v['reason']}")

    no_ts = summary["skipped"].get("skip_no_timestamp", [])
    if no_ts:
        lines.append(
            f"\n:grey_question: No resolvable outbound date ({len(no_ts)}): "
            f"skipped rather than closed on a guess."
        )
        for v in no_ts[:10]:
            lines.append(_ticket_line(v))

    recent = summary["skipped"].get("skip_recent", [])
    if recent:
        lines.append(f"\nStill inside the window: {len(recent)}")

    if summary["errors"]:
        lines.append(f"\n:x: Errors ({len(summary['errors'])}):")
        for e in summary["errors"][:10]:
            lines.append(f"* {e}")

    return "\n".join(lines)


def _post_report(text: str):
    try:
        if SLACK_CHANNEL:
            from slack import client as _slack_client
            _slack_client.chat_postMessage(channel=SLACK_CHANNEL, text=text)
        else:
            post_to_log(text)
    except Exception as e:
        print(f"[STALE SWEEP] Slack report failed: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_stale_waiting_client_sweep(
    dry_run: bool | None = None,
    days: int | None = None,
    limit: int | None = None,
    force: bool = False,
) -> dict:
    """Close tickets parked on the client for `days` with no reply.

    dry_run  assess and report, change nothing. Defaults to
             STALE_SWEEP_DRY_RUN (which itself defaults to true).
    days     staleness threshold, defaults to STALE_WAITING_DAYS.
    limit    max closes this run, defaults to STALE_SWEEP_MAX_CLOSES.
    force    bypass the once-per-day claim, for manual re-runs.
    """
    dry_run = DRY_RUN_DEFAULT if dry_run is None else dry_run
    days = STALE_DAYS if days is None else days
    max_closes = MAX_CLOSES if limit is None else limit
    now = datetime.now(timezone.utc)

    # APScheduler runs on the single web process. A restart near 07:30 can
    # skip the run and a second process would double it, so the live path
    # claims the day in Postgres first. Dry runs never claim.
    run_key = f"stale_waiting_client:{now.strftime('%Y-%m-%d')}"
    if not dry_run and not force:
        if not claim_sweeper_run(run_key):
            print(f"[STALE SWEEP] {run_key} already claimed, skipping")
            return {"status": "already_ran", "run_key": run_key}

    tickets, paging_cap_hit = _fetch_awaiting_client_tickets()

    verdicts: list[dict] = []
    for t in tickets:
        try:
            verdicts.append(_assess(t, now, days))
        except Exception as e:
            print(f"[STALE SWEEP] assess failed for {t.get('id')}: {e}")
        time.sleep(THROTTLE_SECONDS)

    eligible = [v for v in verdicts if v["action"] == "close"]
    # Oldest first: if the cap bites, the most stale tickets go first.
    eligible.sort(key=lambda v: v["days_idle"], reverse=True)
    to_close = eligible[:max_closes]

    skipped: dict[str, list[dict]] = {}
    for v in verdicts:
        if v["action"] != "close":
            skipped.setdefault(v["action"], []).append(v)

    errors: list[str] = []
    # In a dry run every eligible ticket is reported as "would close". In a
    # live run only the ones whose Zoho write actually landed count as closed;
    # the rest stay parked and the next run retries them.
    closed_ok: list[dict] = list(to_close)
    failed: list[dict] = []

    if not dry_run:
        closed_ok = []
        for v in to_close:
            label = v["ticket_number"] or v["ticket_id"]
            try:
                result = _close_one(v, SEND_CLOSING_EMAIL)
                for e in result["errors"]:
                    errors.append(f"#{label}: {e}")
                if result["closed"]:
                    closed_ok.append(v)
                    print(
                        f"[STALE SWEEP] closed #{label} "
                        f"({v['days_idle']}d idle)"
                    )
                else:
                    failed.append(v)
            except Exception as e:
                errors.append(f"#{label}: {e}")
                failed.append(v)
            time.sleep(THROTTLE_SECONDS)

    summary = {
        "status": "ok",
        "dry_run": dry_run,
        "days": days,
        "max_closes": max_closes,
        "scanned": len(verdicts),
        "eligible_total": len(eligible),
        "capped": len(eligible) > max_closes,
        "paging_cap_hit": paging_cap_hit,
        "closed": closed_ok,
        "failed": failed,
        "skipped": skipped,
        "errors": errors,
        "run_key": run_key,
    }

    report = _build_report(summary)
    print(report)
    _post_report(report)

    if not dry_run and not force:
        finish_sweeper_run(run_key, {
            "scanned": summary["scanned"],
            "closed": len(closed_ok),
            "failed": len(failed),
            "eligible_total": summary["eligible_total"],
            "errors": len(errors),
        })

    return _serializable(summary)


def _serializable(summary: dict) -> dict:
    """Verdict dicts carry datetimes; make the HTTP response JSON-safe."""
    def clean(v: dict) -> dict:
        out = {k: val for k, val in v.items() if k != "last_outbound"}
        last = v.get("last_outbound")
        out["last_outbound"] = last.isoformat() if last else None
        return out

    return {
        **summary,
        "closed": [clean(v) for v in summary["closed"]],
        "failed": [clean(v) for v in summary.get("failed", [])],
        "skipped": {
            k: [clean(v) for v in vs] for k, vs in summary["skipped"].items()
        },
    }
