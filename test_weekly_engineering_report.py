"""
test_weekly_engineering_report.py

Covers the parts of the engineering report that can be wrong silently: bucket
mapping, the shared-assignee split, and flow direction across a snapshot diff.

Run with:  py -m pytest test_weekly_engineering_report.py -q
Or plain:  py test_weekly_engineering_report.py     (prints both reports)
"""

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("CLICKUP_USER_SANJAY", "4434086")
os.environ.setdefault("CLICKUP_USER_ONLYG", "49257687")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")

import weekly_engineering_report as rpt  # noqa: E402
from status_constants import report_bucket  # noqa: E402

SANJAY = 4434086
ONLYG = 49257687
SAM = 3691763

NOW_MS = int(datetime.now(timezone.utc).timestamp() * 1000)
OLD_MS = int(
    (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
)


def _task(tid, status, assignees, name="t", created=OLD_MS):
    """A ClickUp task payload trimmed to the fields the report reads."""
    return {
        "id": tid,
        "status": {"status": status},
        "assignees": [{"id": a} for a in assignees],
        "list": {"id": "901113386257"},
        "name": name,
        "date_created": str(created),
    }


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

def test_on_prod_and_closed_are_one_bucket():
    """The whole point of merging them: on prod auto-advances to Closed."""
    assert report_bucket("on prod") == "shipped"
    assert report_bucket("Closed") == "shipped"
    assert report_bucket("CLOSED") == "shipped"


def test_done_type_statuses_are_not_active():
    """These return from include_closed=false and must never count as load."""
    for s in ("awaiting client response", "sleeping", "declined"):
        assert report_bucket(s) != "active", s


def test_escalated_is_not_on_the_plate():
    """Escalated tasks are Sam's, so they must stay out of the plate total."""
    assert report_bucket("escalated") == "other"


def test_casing_and_separator_variants():
    assert report_bucket("In_Progress") == "active"
    assert report_bucket("On Prod ✅") == "shipped"


# ---------------------------------------------------------------------------
# The shared-assignee split
# ---------------------------------------------------------------------------

def test_split_does_not_double_count_total():
    rows = rpt._to_snapshot_rows([
        _task("a", "queued", [SANJAY]),
        _task("b", "queued", [ONLYG]),
        _task("c", "queued", [SANJAY, ONLYG]),
    ])
    s = rpt._split(rows)
    assert s["total"] == 3           # deduplicated
    assert s["sanjay"] == 2          # membership, overlaps
    assert s["onlyg"] == 2
    assert s["shared_both"] == 1     # explains why 2 + 2 != 3


def test_third_assignee_does_not_break_shared():
    """Sam is on many tasks; that must not count as 'shared by both devs'."""
    rows = rpt._to_snapshot_rows([_task("a", "queued", [SANJAY, SAM])])
    s = rpt._split(rows)
    assert s["shared_both"] == 0
    assert s["sanjay"] == 1


# ---------------------------------------------------------------------------
# Flow across a snapshot diff
# ---------------------------------------------------------------------------

def _snap(tasks):
    return {r["task_id"]: r for r in rpt._to_snapshot_rows(tasks)}


def test_flow_directions():
    prev = _snap([
        _task("keep", "queued", [SANJAY]),
        _task("ship", "in progress", [ONLYG]),
        _task("push", "in progress", [SANJAY]),
        _task("educate", "in progress", [ONLYG]),
        _task("returning", "awaiting client response", [SANJAY]),
        _task("closing", "on dev", [ONLYG]),
    ])
    curr = _snap([
        _task("keep", "queued", [SANJAY]),
        _task("ship", "on prod", [ONLYG]),
        _task("push", "awaiting client response", [SANJAY]),
        _task("educate", "user education", [ONLYG]),
        _task("returning", "in progress", [SANJAY]),
        # "closing" is absent: it went to Closed, which the fetch excludes.
        _task("brandnew", "queued", [ONLYG], created=NOW_MS),
    ])
    f = rpt._compute_flow(prev, curr)

    assert f["new"]["total"] == 1
    assert f["back_from_client"]["total"] == 1
    assert f["to_client"]["total"] == 1
    assert f["to_education"]["total"] == 1
    # "ship" moved to on prod, "closing" vanished into Closed.
    assert f["shipped"]["total"] == 2

    assert f["total_in"] == 2                   # new + back from client
    assert f["total_out"] == 4                  # shipped 2 + client 1 + edu 1


def test_net_change_comes_from_standing_not_arithmetic():
    """net_change must track the board, not total_in - total_out.

    A task moving queued -> in progress is neither in nor out, so the two
    numbers legitimately disagree and the standing delta is the honest one.
    """
    prev = _snap([_task("a", "queued", [SANJAY])])
    curr = _snap([_task("a", "in progress", [SANJAY])])
    f = rpt._compute_flow(prev, curr)
    assert f["total_in"] == 0
    assert f["total_out"] == 0
    assert f["net_change"] == 0
    assert f["prev_active"] == 1 and f["curr_active"] == 1


def test_vanished_parked_task_is_not_counted_twice():
    prev = _snap([_task("a", "awaiting client response", [SANJAY])])
    curr = {}
    f = rpt._compute_flow(prev, curr)
    assert f["shipped"]["total"] == 1
    assert f["total_out"] == 1


# ---------------------------------------------------------------------------
# Standing
# ---------------------------------------------------------------------------

def test_standing_excludes_parked_and_other_from_plate():
    curr = _snap([
        _task("a", "queued", [SANJAY]),
        _task("b", "in progress", [ONLYG]),
        _task("c", "awaiting client response", [SANJAY]),
        _task("d", "escalated", [SANJAY]),
        _task("e", "sleeping", [ONLYG]),
    ])
    st = rpt._compute_standing(curr)
    assert st["active_total"] == 2
    assert st["parked"]["total"] == 1
    assert st["other_total"] == 2


def test_fresh_grace_window():
    curr = _snap([
        _task("old", "queued", [SANJAY], created=OLD_MS),
        _task("new", "queued", [ONLYG], created=NOW_MS),
    ])
    st = rpt._compute_standing(curr)
    assert st["fresh_48h"] == 1


def test_weekend_movers_omits_idle_engineer():
    """An absent name is neutral; a printed 0 next to a name is a callout."""
    prev = _snap([
        _task("a", "queued", [SANJAY]),
        _task("b", "queued", [ONLYG]),
    ])
    curr = _snap([
        _task("a", "on dev", [SANJAY]),
        _task("b", "queued", [ONLYG]),
    ])
    w = rpt._weekend_movers(prev, curr)
    assert w["per_engineer"] == {"Sanjay": 1}
    assert "OnlyG" not in w["per_engineer"]


# ---------------------------------------------------------------------------
# The fetch must issue ONE REQUEST PER ENGINEER
#
# ClickUp's /team/{id}/task does not OR multiple assignees[] values. Measured
# on the live board 2026-08-18: sanjay alone 109, onlyg alone 82, union 173,
# but both passed together returned only 85. Regression guard for that.
# ---------------------------------------------------------------------------

def test_fetch_queries_each_engineer_separately(monkeypatch=None):
    calls = []

    class _Resp:
        def __init__(self, tasks):
            self._tasks = tasks

        def raise_for_status(self):
            pass

        def json(self):
            return {"tasks": self._tasks}

    def fake_get(url, params=None, headers=None, timeout=None):
        assignees = [v for k, v in (params or []) if k == "assignees[]"]
        calls.append(assignees)
        # Exactly one assignee per request, never two.
        assert len(assignees) == 1, f"batched assignees: {assignees}"
        if assignees[0] == str(SANJAY):
            return _Resp([
                _task("shared", "queued", [SANJAY, ONLYG]),
                _task("s-only", "queued", [SANJAY]),
            ])
        return _Resp([
            _task("shared", "queued", [SANJAY, ONLYG]),
            _task("o-only", "queued", [ONLYG]),
        ])

    orig = rpt.httpx.get
    rpt.httpx.get = fake_get
    rpt.CLICKUP_TEAM_ID = rpt.CLICKUP_TEAM_ID or "team"
    rpt.CLICKUP_API_TOKEN = rpt.CLICKUP_API_TOKEN or "tok"
    try:
        tasks = rpt._fetch_assigned_tasks()
    finally:
        rpt.httpx.get = orig

    assert len(calls) == 2, f"expected one call per engineer, got {calls}"
    # The task on both engineers must appear exactly once.
    ids = [t["id"] for t in tasks]
    assert sorted(ids) == ["o-only", "s-only", "shared"], ids
    assert ids.count("shared") == 1


def test_fetch_aborts_on_partial_failure():
    """A failed page must abort, not return half a board as if complete."""
    class _Boom:
        def raise_for_status(self):
            raise RuntimeError("500")

        def json(self):
            return {}

    orig = rpt.httpx.get
    rpt.httpx.get = lambda *a, **k: _Boom()
    rpt.CLICKUP_TEAM_ID = rpt.CLICKUP_TEAM_ID or "team"
    rpt.CLICKUP_API_TOKEN = rpt.CLICKUP_API_TOKEN or "tok"
    try:
        assert rpt._fetch_assigned_tasks() == []
    finally:
        rpt.httpx.get = orig


# ---------------------------------------------------------------------------
# Rendering — no assertions on exact text, just that it builds and aligns
# ---------------------------------------------------------------------------

def _demo_snapshots():
    prev_tasks = (
        [_task(f"q{i}", "queued", [SANJAY, ONLYG]) for i in range(11)]
        + [_task(f"q2{i}", "queued", [ONLYG]) for i in range(3)]
        + [_task(f"p{i}", "in progress", [SANJAY]) for i in range(5)]
        + [_task(f"d{i}", "on dev", [ONLYG]) for i in range(4)]
        + [_task(f"ac{i}", "awaiting client response", [SANJAY])
           for i in range(66)]
        + [_task(f"ue{i}", "user education", [ONLYG]) for i in range(2)]
        + [_task(f"esc{i}", "escalated", [SANJAY]) for i in range(2)]
    )
    curr_tasks = list(prev_tasks)
    # 6 shipped, 4 pushed to client, 3 came back, 5 brand new
    for i in range(6):
        curr_tasks = [t for t in curr_tasks if t["id"] != f"q{i}"]
    for i in range(4):
        curr_tasks = [
            _task(t["id"], "awaiting client response", [SANJAY])
            if t["id"] == f"p{i}" else t
            for t in curr_tasks
        ]
    for i in range(3):
        curr_tasks = [
            _task(t["id"], "in progress", [SANJAY])
            if t["id"] == f"ac{i}" else t
            for t in curr_tasks
        ]
    curr_tasks += [
        _task(f"n{i}", "queued", [ONLYG], created=NOW_MS) for i in range(5)
    ]
    return _snap(prev_tasks), _snap(curr_tasks)


def test_reports_render():
    prev, curr = _demo_snapshots()
    st = rpt._compute_standing(curr)
    fl = rpt._compute_flow(prev, curr)
    wk = rpt._weekend_movers(prev, curr)
    now = datetime.now(timezone.utc)

    friday = rpt._format_friday(fl, st, [], now - timedelta(days=7), now)
    monday = rpt._format_monday(
        fl, st, wk, now, "eng-report-friday-2026-08-21"
    )

    assert "Week closed" in friday
    assert "Week open" in monday
    assert "on the plate" in friday
    # Backticks must be balanced or Slack renders the table as prose.
    assert friday.count("```") % 2 == 0
    assert monday.count("```") % 2 == 0


def test_first_run_says_so_instead_of_faking_flow():
    _, curr = _demo_snapshots()
    st = rpt._compute_standing(curr)
    now = datetime.now(timezone.utc)
    out = rpt._format_friday(None, st, [], now - timedelta(days=7), now)
    assert "First run" in out
    assert "in-versus-out" in out


if __name__ == "__main__":
    prev, curr = _demo_snapshots()
    st = rpt._compute_standing(curr)
    fl = rpt._compute_flow(prev, curr)
    wk = rpt._weekend_movers(prev, curr)
    now = datetime.now(timezone.utc)
    history = [
        {"report_kind": "friday", "shipped": n, "window_end": d, "run_key": ""}
        for n, d in [
            (6, "2026-08-21"), (11, "2026-08-14"), (9, "2026-08-07"),
            (14, "2026-07-31"), (8, "2026-07-24"),
        ]
    ]
    print("=" * 78)
    print(rpt._format_friday(fl, st, history, now - timedelta(days=7), now))
    print("=" * 78)
    print(rpt._format_monday(fl, st, wk, now, "eng-report-friday-2026-08-21"))
    print("=" * 78)
