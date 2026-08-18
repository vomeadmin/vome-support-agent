"""
weekly_engineering_report.py

Two Slack reports on the engineering ClickUp board, both READ ONLY.

    Friday  (close the week)  -- what went out versus what came in, plus the
                                 week-over-week completed trend.
    Monday  (open the week)   -- what is on the plate right now, and what moved
                                 over the weekend.

Scheduled from main.py. Both are also triggerable by hand:
    POST /ops/reports/engineering   {"kind": "friday", "dry_run": true}

WHY TWO REPORTS AND NOT ONE
    They are different documents. Friday is retrospective and carries no asks,
    because nobody can action anything at 5pm on a Friday, and a "your
    pile grew" message heading into a weekend has no outlet. Monday is the
    actionable one.

THE FOUR THINGS THAT KEEP THE NUMBERS HONEST

 1. ClickUp's own open/closed flag is useless here. "awaiting client response",
    "sleeping", "declined" and "on prod" are all type "done" on this board, so
    include_closed=false STILL returns them. Buckets come from
    status_constants.report_bucket(), never from ClickUp's flag.

 2. "on prod" and "Closed" are ONE outcome. A task set to on prod auto-advances
    to Closed once the client is emailed, so on prod is a staging state. Both
    map to BUCKET_SHIPPED and a task that makes the whole trip inside one
    window is counted once.

 3. Flow comes from diffing snapshots, not from the API. ClickUp cannot answer
    "which tasks moved out of awaiting-client this week": time_in_status gives
    durations, not transition dates, and date_done is overwritten as a task
    advances. So every run snapshots the board, and flow is snapshot(N-1)
    versus snapshot(N). Week one therefore has standing figures only, which is
    reported honestly rather than filled in with a guess.

 4. Per-engineer columns do not sum to Total. Most tasks carry both engineers,
    so a per-person figure means "involved in", not "owns". Every table prints
    a shared count so the gap is explainable instead of looking like a bug.

DELIBERATELY NOT IN THE REPORT
    "escalated" tasks are Sam's to action, not the engineers', so they are
    bucketed as other and never counted in the plate total.
"""

import os
from datetime import datetime, timedelta, timezone

import httpx

from database import (
    claim_sweeper_run,
    finish_sweeper_run,
    get_eng_report_history,
    get_eng_snapshot,
    get_previous_eng_run_key,
    save_eng_report_figures,
    save_eng_snapshot,
)
from slack import post_to_log
from status_constants import (
    CU_AWAITING_CLIENT,
    CU_IN_PROGRESS,
    CU_ON_DEV,
    CU_QUEUED,
    CU_USER_EDUCATION,
    normalize_status,
    report_bucket,
)

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"
CLICKUP_TEAM_ID = os.environ.get("CLICKUP_TEAM_ID", "")

# Space 90114113004 "Vome Product" holds Priority Queue, Raw Intake and
# Accepted Backlog. Env-overridable so a board reorganisation does not need a
# code change.
CLICKUP_SPACE_ID = os.environ.get("CLICKUP_SPACE_PRODUCT", "90114113004")

# ---------------------------------------------------------------------------
# Engineers. Sanjay is FRONTEND, OnlyG is BACKEND -- nothing in the ClickUp API
# or the rest of this codebase records that mapping, and it is easy to get
# backwards, so it is stated once here and read from env ids.
# ---------------------------------------------------------------------------

ENGINEERS = [
    {
        "key": "sanjay",
        "label": "Sanjay",
        "discipline": "FE",
        "clickup_id": str(os.environ.get("CLICKUP_USER_SANJAY", "") or ""),
    },
    {
        "key": "onlyg",
        "label": "OnlyG",
        "discipline": "BE",
        "clickup_id": str(os.environ.get("CLICKUP_USER_ONLYG", "") or ""),
    },
]

REPORT_FRIDAY = "friday"
REPORT_MONDAY = "monday"

# Statuses shown as rows in the standing table, in board order.
_PLATE_ROWS = [CU_QUEUED, CU_IN_PROGRESS, CU_ON_DEV]
_PARKED_ROWS = [CU_AWAITING_CLIENT, CU_USER_EDUCATION]

# Friendly row labels. Board names are kept verbatim so any row can be matched
# to a ClickUp column without translation.
_ROW_LABEL = {
    CU_QUEUED: "queued",
    CU_IN_PROGRESS: "in progress",
    CU_ON_DEV: "on dev",
    CU_AWAITING_CLIENT: "awaiting client response",
    CU_USER_EDUCATION: "user education",
}

# A task created inside this window is excluded from the aging callout: it is
# not reasonable to expect movement on something that arrived hours ago.
GRACE_HOURS = int(os.environ.get("ENG_REPORT_GRACE_HOURS", "48"))

# Where the report lands. Defaults to the agent log so the first live weeks can
# be reconciled against the board before the team ever sees it.
REPORT_CHANNEL = os.environ.get("SLACK_CHANNEL_ENG_REPORT", "")

# Weeks of history shown in the completed trend.
TREND_WEEKS = int(os.environ.get("ENG_REPORT_TREND_WEEKS", "6"))


# ---------------------------------------------------------------------------
# ClickUp fetch
# ---------------------------------------------------------------------------

def _fetch_one_assignee(clickup_id: str) -> list[dict] | None:
    """Every not-yet-Closed task in the product space assigned to one person.

    Returns None on any HTTP failure so the caller can abort the whole run:
    partial data would silently understate every figure and still land in Slack
    looking authoritative.
    """
    tasks: list[dict] = []
    page = 0
    while True:
        params: list[tuple[str, str]] = [
            ("space_ids[]", CLICKUP_SPACE_ID),
            ("include_closed", "false"),
            ("subtasks", "true"),
            ("assignees[]", clickup_id),
            ("page", str(page)),
        ]
        try:
            r = httpx.get(
                f"{CLICKUP_BASE}/team/{CLICKUP_TEAM_ID}/task",
                params=params,
                headers={"Authorization": CLICKUP_API_TOKEN},
                timeout=30,
            )
            r.raise_for_status()
            batch = r.json().get("tasks", [])
        except Exception as e:
            print(
                f"[eng-report] ClickUp fetch failed "
                f"(assignee {clickup_id}, page {page}): {e}"
            )
            return None
        tasks.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        if page > 50:   # 5000 tasks for one person; runaway backstop
            print(f"[eng-report] pagination cap hit for assignee {clickup_id}")
            break
    return tasks


def _fetch_assigned_tasks() -> list[dict]:
    """Union of per-engineer queries, deduplicated by task id.

    ONE QUERY PER ENGINEER, NEVER ONE QUERY WITH TWO assignees[] VALUES.
    ClickUp's /team/{id}/task does NOT OR multiple assignees[] params despite
    what the filter semantics imply. Measured against the live board on
    2026-08-18:

        assignees[]=sanjay                 -> 109 tasks
        assignees[]=onlyg                  ->  82 tasks
        union of the two                   -> 173 tasks
        assignees[]=sanjay&assignees[]=onlyg ->  85 tasks   <-- drops 88

    It is not an AND either: tasks assigned to BOTH engineers were among the
    ones dropped. So the multi-value form is simply unreliable and the only
    trustworthy shape is one request per person, unioned here.

    include_closed stays false: "Closed" accumulates forever and would drag
    years of history through pagination. Shipped work is detected by a task
    LEAVING this result set, which the snapshot diff sees for free.
    """
    if not CLICKUP_TEAM_ID or not CLICKUP_API_TOKEN:
        print("[eng-report] CLICKUP_TEAM_ID or CLICKUP_API_TOKEN not set")
        return []

    assignee_ids = [e["clickup_id"] for e in ENGINEERS if e["clickup_id"]]
    if not assignee_ids:
        print("[eng-report] no engineer ClickUp ids configured")
        return []

    by_id: dict[str, dict] = {}
    for uid in assignee_ids:
        batch = _fetch_one_assignee(uid)
        if batch is None:
            return []
        for t in batch:
            tid = t.get("id")
            if tid:
                by_id[tid] = t   # dedupes tasks carrying both engineers
    return list(by_id.values())


def _to_snapshot_rows(tasks: list[dict]) -> list[dict]:
    """Flatten ClickUp task payloads into snapshot rows."""
    rows = []
    for t in tasks:
        status = (t.get("status") or {}).get("status", "") or ""
        rows.append({
            "task_id": t.get("id", ""),
            "status": normalize_status(status),
            "bucket": report_bucket(status),
            "assignees": [
                int(a["id"]) for a in (t.get("assignees") or [])
                if a.get("id") is not None
            ],
            "list_id": str((t.get("list") or {}).get("id", "") or ""),
            "task_name": t.get("name", "") or "",
            "date_created": str(t.get("date_created", "") or ""),
        })
    return [r for r in rows if r["task_id"]]


# ---------------------------------------------------------------------------
# Counting helpers
# ---------------------------------------------------------------------------

def _fmt_date(dt: datetime, with_weekday: bool = False) -> str:
    """"Aug 21" / "Friday Aug 21", without the POSIX-only %-d.

    slack_digest.py uses %-d, which is glibc-specific. It works on the Heroku
    dyno and raises ValueError on Windows, so local runs of this module would
    crash on formatting alone. Built by hand instead.
    """
    base = f"{dt.strftime('%b')} {dt.day}"
    return f"{dt.strftime('%A')} {base}" if with_weekday else base


def _prev_report_label(prev_run_key: str | None) -> str:
    """"the Friday report (Aug 21)" from a run_key, for the Monday header.

    The Monday window is "everything since the last snapshot", so the header
    must name that snapshot. Deriving it from a now-minus-3-days offset would
    print a plausible but wrong timestamp whenever a run was missed or
    re-triggered, which is worse than being vague.
    """
    if not prev_run_key:
        return "the last report"
    parts = prev_run_key.split("-")
    kind = parts[2] if len(parts) > 2 else ""
    try:
        when = datetime.strptime("-".join(parts[-3:]), "%Y-%m-%d")
        return f"the {kind.capitalize()} report ({_fmt_date(when)})"
    except (ValueError, IndexError):
        return "the last report"


def _is_assigned(row: dict, clickup_id: str) -> bool:
    if not clickup_id:
        return False
    try:
        return int(clickup_id) in (row.get("assignees") or [])
    except (TypeError, ValueError):
        return False


def _split(rows: list[dict]) -> dict:
    """Total plus a per-engineer count, and how many carry both engineers.

    total is DEDUPLICATED (one task, one count). The per-engineer numbers are
    membership counts and will overlap, which is what shared_both quantifies.
    """
    out = {"total": len(rows)}
    for e in ENGINEERS:
        out[e["key"]] = sum(
            1 for r in rows if _is_assigned(r, e["clickup_id"])
        )
    ids = [e["clickup_id"] for e in ENGINEERS if e["clickup_id"]]
    out["shared_both"] = sum(
        1 for r in rows if all(_is_assigned(r, i) for i in ids)
    ) if len(ids) > 1 else 0
    return out


def _ms_to_dt(ms: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Standing and flow
# ---------------------------------------------------------------------------

def _compute_standing(curr: dict[str, dict]) -> dict:
    """Where the pile sits right now, by status."""
    rows = list(curr.values())
    plate = [r for r in rows if r["bucket"] == "active"]
    parked = [r for r in rows if r["bucket"] == "parked"]
    other = [r for r in rows if r["bucket"] == "other"]

    by_status = {}
    for status in _PLATE_ROWS + _PARKED_ROWS:
        by_status[status] = _split(
            [r for r in rows if r["status"] == status]
        )

    cutoff = datetime.now(timezone.utc) - timedelta(hours=GRACE_HOURS)
    fresh = [
        r for r in plate
        if (_ms_to_dt(r["date_created"]) or datetime.min.replace(
            tzinfo=timezone.utc)) >= cutoff
    ]

    return {
        "by_status": by_status,
        "plate": _split(plate),
        "parked": _split(parked),
        "other_total": len(other),
        "fresh_48h": len(fresh),
        "active_total": len(plate),
    }


def _compute_flow(prev: dict[str, dict], curr: dict[str, dict]) -> dict:
    """Movement between two snapshots.

    IN and OUT are both defined relative to THE PLATE, which is the only thing
    that represents claimable engineer workload:

      IN   new         -- task absent from the previous snapshot
           back_from_client -- was parked, is now active again
      OUT  shipped     -- was active or parked, now on prod / Closed
           to_client   -- was active, now awaiting client response
           to_education -- was active, now user education

    A task that vanished from the snapshot entirely counts as shipped: the
    fetch excludes only the Closed status, so vanishing means it got Closed.
    """
    new_rows, back_rows = [], []
    shipped_rows, to_client_rows, to_education_rows = [], [], []

    for tid, row in curr.items():
        before = prev.get(tid)
        if before is None:
            new_rows.append(row)
            continue
        was, now = before["bucket"], row["bucket"]
        if was == "parked" and now == "active":
            back_rows.append(row)
        elif was in ("active", "parked") and now == "shipped":
            shipped_rows.append(row)
        elif was == "active" and row["status"] == CU_AWAITING_CLIENT:
            to_client_rows.append(row)
        elif was == "active" and row["status"] == CU_USER_EDUCATION:
            to_education_rows.append(row)

    # Gone from the snapshot => moved to Closed, which the fetch excludes.
    for tid, before in prev.items():
        if tid not in curr and before["bucket"] in ("active", "parked"):
            shipped_rows.append(before)

    total_in = len(new_rows) + len(back_rows)
    total_out = (
        len(shipped_rows) + len(to_client_rows) + len(to_education_rows)
    )

    prev_active = sum(1 for r in prev.values() if r["bucket"] == "active")
    curr_active = sum(1 for r in curr.values() if r["bucket"] == "active")

    return {
        "new": _split(new_rows),
        "back_from_client": _split(back_rows),
        "shipped": _split(shipped_rows),
        "to_client": _split(to_client_rows),
        "to_education": _split(to_education_rows),
        "total_in": total_in,
        "total_out": total_out,
        # Authoritative plate change: measured from the standing counts, not
        # from total_in - total_out, so it cannot drift from what the board
        # actually shows.
        "net_change": curr_active - prev_active,
        "prev_active": prev_active,
        "curr_active": curr_active,
    }


def _weekend_movers(prev: dict[str, dict], curr: dict[str, dict]) -> dict:
    """Tasks whose status changed between the two snapshots, per engineer.

    Only used by the Monday report, where the gap between snapshots IS the
    weekend, so any change means someone worked through it.
    """
    moved = [
        row for tid, row in curr.items()
        if tid in prev and prev[tid]["status"] != row["status"]
    ]
    per = {}
    for e in ENGINEERS:
        n = sum(1 for r in moved if _is_assigned(r, e["clickup_id"]))
        if n:
            per[e["label"]] = n
    return {"total": len(moved), "per_engineer": per}


# ---------------------------------------------------------------------------
# Formatting
#
# Tables go inside a code fence: Slack renders those monospace, which is the
# only way fixed-width columns actually line up.
# ---------------------------------------------------------------------------

_W_LABEL = 30
_W_COL = 8


def _header_row() -> str:
    cells = "".join(
        f"{e['label'] + ' (' + e['discipline'] + ')':>{_W_COL + 4}}"
        for e in ENGINEERS
    )
    return f"{'':<{_W_LABEL}}{'Total':>{_W_COL}}{cells}"


def _row(label: str, split: dict) -> str:
    cells = "".join(
        f"{split.get(e['key'], 0):>{_W_COL + 4}}" for e in ENGINEERS
    )
    return f"{label:<{_W_LABEL}}{split.get('total', 0):>{_W_COL}}{cells}"


def _rule() -> str:
    return f"{'':<{_W_LABEL}}{'-' * _W_COL:>{_W_COL}}" + "".join(
        f"{'-' * _W_COL:>{_W_COL + 4}}" for _ in ENGINEERS
    )


def _trend_line(history: list[dict]) -> list[str]:
    """Week-over-week completed counts, oldest first."""
    past = [h for h in history if h["report_kind"] == REPORT_FRIDAY]
    if len(past) < 2:
        return []
    lines = ["*Completed, week by week*", "```"]
    for h in reversed(past[:TREND_WEEKS]):
        when = (h["window_end"] or "")[:10] or h["run_key"]
        bar = "#" * min(h["shipped"], 40)
        lines.append(f"{when}  {h['shipped']:>3}  {bar}")
    lines.append("```")
    return lines


def _format_friday(flow: dict | None, standing: dict, history: list[dict],
                   window_start: datetime, window_end: datetime) -> str:
    lines = [
        f"*Week closed: {_fmt_date(window_start)} to "
        f"{_fmt_date(window_end)}*",
        "",
    ]

    if flow is None:
        lines += [
            "_First run: no previous snapshot to compare against, so there is "
            "no in-versus-out yet. Standing figures below are live. Flow "
            "starts with the next report._",
            "",
        ]
    else:
        net = flow["net_change"]
        direction = (
            "plate shrank" if net < 0
            else "plate grew" if net > 0
            else "plate unchanged"
        )
        lines += [
            "*Volume in versus out*",
            "```",
            _header_row(),
            _row("IN   new tasks", flow["new"]),
            _row("     back from client", flow["back_from_client"]),
            _rule(),
            _row("OUT  shipped", flow["shipped"]),
            _row("     pushed to client", flow["to_client"]),
            _row("     user education", flow["to_education"]),
            _rule(),
            f"{'     total in':<{_W_LABEL}}{flow['total_in']:>{_W_COL}}",
            f"{'     total out':<{_W_LABEL}}{flow['total_out']:>{_W_COL}}",
            f"{'NET  plate change':<{_W_LABEL}}{net:>+{_W_COL}}"
            f"    {direction}",
            "```",
            f"Shared by both: {flow['shipped'].get('shared_both', 0)} of "
            f"{flow['shipped'].get('total', 0)} shipped. Per-engineer numbers "
            "count involvement, so they overlap and will not sum to Total.",
            "",
        ]

    lines += _plate_block(standing)

    trend = _trend_line(history)
    if trend:
        lines += [""] + trend

    return "\n".join(lines)


def _format_monday(flow: dict | None, standing: dict, weekend: dict,
                   window_end: datetime,
                   prev_run_key: str | None = None) -> str:
    lines = [
        f"*Week open: {_fmt_date(window_end, with_weekday=True)}*",
        "",
    ]
    lines += _plate_block(standing)

    if flow is not None:
        lines += [
            "",
            f"*Since {_prev_report_label(prev_run_key)}*",
            "```",
            f"{'new in':<{_W_LABEL}}{flow['new'].get('total', 0):>{_W_COL}}",
            f"{'clients came back on':<{_W_LABEL}}"
            f"{flow['back_from_client'].get('total', 0):>{_W_COL}}"
            "    live again",
            f"{'shipped':<{_W_LABEL}}"
            f"{flow['shipped'].get('total', 0):>{_W_COL}}",
            "```",
        ]

    # Only printed when someone actually worked. A "0" printed next to a name
    # reads as a callout; an absent line is neutral.
    if weekend.get("per_engineer"):
        credit = ", ".join(
            f"{name} moved {n}" for name, n in weekend["per_engineer"].items()
        )
        lines += ["", f"*Worked over the weekend:* {credit}. Thank you."]

    return "\n".join(lines)


def _plate_block(standing: dict) -> list[str]:
    lines = [
        "*What is on the plate right now*",
        "```",
        _header_row(),
    ]
    for status in _PLATE_ROWS:
        lines.append(_row(_ROW_LABEL[status], standing["by_status"][status]))
    lines.append(_rule())
    lines.append(_row("total on plate", standing["plate"]))
    lines.append("")
    lines.append("Waiting on the client (not yours)")
    for status in _PARKED_ROWS:
        lines.append(_row(
            "  " + _ROW_LABEL[status], standing["by_status"][status]
        ))
    lines.append("```")
    lines.append(
        f"Shared by both: {standing['plate'].get('shared_both', 0)} of "
        f"{standing['plate'].get('total', 0)} on the plate."
    )
    lines.append(
        f"Arrived in the last {GRACE_HOURS}h: {standing['fresh_48h']}, "
        "no movement expected yet."
    )
    if standing["other_total"]:
        lines.append(
            f"Other statuses (escalated, sleeping, declined, needs client "
            f"info): {standing['other_total']}. Not engineering load."
        )
    return lines


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_engineering_report(
    kind: str = REPORT_FRIDAY,
    dry_run: bool = True,
    force: bool = False,
) -> dict:
    """Build, persist and post one engineering report.

    dry_run posts to #vome-agent-log instead of the team channel, and defaults
    to ON so a hand trigger cannot surprise the team. The snapshot is saved
    either way: the figures are only useful if collection is unbroken, and a
    dry run still represents a real board state.

    force skips the once-per-day claim, for re-running after a formatting fix.
    """
    if kind not in (REPORT_FRIDAY, REPORT_MONDAY):
        return {"ok": False, "error": f"unknown report kind: {kind}"}

    now = datetime.now(timezone.utc)
    run_key = f"eng-report-{kind}-{now.strftime('%Y-%m-%d')}"

    if not force and not claim_sweeper_run(run_key):
        print(f"[eng-report] {run_key} already claimed, skipping")
        return {"ok": True, "skipped": "already ran today", "run_key": run_key}

    tasks = _fetch_assigned_tasks()
    if not tasks:
        finish_sweeper_run(run_key, {"error": "no tasks fetched"})
        return {"ok": False, "error": "ClickUp fetch returned nothing"}

    rows = _to_snapshot_rows(tasks)
    save_eng_snapshot(run_key, rows)
    curr = {r["task_id"]: r for r in rows}

    prev_key = get_previous_eng_run_key(run_key)
    prev = get_eng_snapshot(prev_key) if prev_key else {}

    standing = _compute_standing(curr)
    flow = _compute_flow(prev, curr) if prev else None
    weekend = _weekend_movers(prev, curr) if prev else {"per_engineer": {}}

    window_end = now
    window_start = now - timedelta(days=7 if kind == REPORT_FRIDAY else 3)

    history = get_eng_report_history(limit=TREND_WEEKS + 2)

    if kind == REPORT_FRIDAY:
        message = _format_friday(
            flow, standing, history, window_start, window_end
        )
    else:
        message = _format_monday(
            flow, standing, weekend, window_end, prev_key
        )

    figures = {
        "kind": kind,
        "standing": standing,
        "flow": flow or {},
        "weekend": weekend,
        "prev_run_key": prev_key,
        "task_count": len(rows),
        "total_in": (flow or {}).get("total_in", 0),
        "total_out": (flow or {}).get("total_out", 0),
        "shipped": (flow or {}).get("shipped", {}).get("total", 0),
        "net_change": (flow or {}).get("net_change", 0),
        "active_total": standing["active_total"],
    }
    save_eng_report_figures(run_key, kind, window_start, window_end, figures)

    posted = _post(message, dry_run=dry_run)
    finish_sweeper_run(run_key, {
        "kind": kind,
        "dry_run": dry_run,
        "posted": posted,
        "tasks": len(rows),
        "active": standing["active_total"],
        "net_change": figures["net_change"],
    })

    return {
        "ok": True,
        "run_key": run_key,
        "kind": kind,
        "dry_run": dry_run,
        "posted": posted,
        "prev_run_key": prev_key,
        "figures": figures,
        "message": message,
    }


def _post(message: str, dry_run: bool) -> bool:
    """Post the report. Dry runs go to the agent log, never the team."""
    if dry_run or not REPORT_CHANNEL:
        try:
            post_to_log(f"[eng-report DRY RUN]\n{message}")
            return True
        except Exception as e:
            print(f"[eng-report] agent-log post failed: {e}")
            return False
    try:
        from slack import client
        client.chat_postMessage(channel=REPORT_CHANNEL, text=message)
        return True
    except Exception as e:
        print(f"[eng-report] Slack post failed: {e}")
        return False


def run_friday_report() -> dict:
    """APScheduler entry point for the Friday close-the-week report."""
    return run_engineering_report(
        kind=REPORT_FRIDAY,
        dry_run=os.environ.get(
            "ENG_REPORT_DRY_RUN", "true"
        ).strip().lower() in ("1", "true", "yes"),
    )


def run_monday_report() -> dict:
    """APScheduler entry point for the Monday open-the-week report."""
    return run_engineering_report(
        kind=REPORT_MONDAY,
        dry_run=os.environ.get(
            "ENG_REPORT_DRY_RUN", "true"
        ).strip().lower() in ("1", "true", "yes"),
    )
