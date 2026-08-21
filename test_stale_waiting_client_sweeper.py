"""Tests for the stale awaiting-client sweep.

Pins the two rules that keep the sweep safe:
  1. staleness comes from the last OUTBOUND email, never modifiedTime
  2. a ticket is only closed when Zoho and ClickUp agree it is parked

No network: every Zoho/ClickUp/DB reader is monkeypatched.

Run with pytest, or directly:  py test_stale_waiting_client_sweeper.py
"""
import os
from datetime import datetime, timedelta, timezone

# Dummy creds so module-level client constructors don't raise on import.
# None of these make network calls at construction time.
for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import stale_waiting_client_sweeper as sweeper  # noqa: E402

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
CONTACT = "admin@bigcharity.org"


def _iso(days_ago: int) -> str:
    dt = NOW - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _out(days_ago: int) -> dict:
    """An outbound email from our team."""
    return {
        "id": f"out-{days_ago}",
        "createdTime": _iso(days_ago),
        "summary": "Could you confirm which site the shift is on?",
        "author": {"type": "AGENT", "email": "s.fagen@vomevolunteer.com"},
    }


def _in(days_ago: int) -> dict:
    """An inbound email from the client."""
    return {
        "id": f"in-{days_ago}",
        "createdTime": _iso(days_ago),
        "summary": "Sorry for the delay, it is the Halifax site.",
        "author": {"type": "END_USER", "email": CONTACT},
    }


def _ticket(**over) -> dict:
    base = {
        "id": "5001",
        "ticketNumber": "7905",
        "subject": "Shift times missing on dashboard",
        "status": "Awaiting Client Response",
        # The trap: modifiedTime moves on any edit (tag/status/sync writes),
        # so it must never be read as "when we last emailed the client".
        "modifiedTime": _iso(0),
        "contact": {"email": CONTACT, "firstName": "Dana"},
        "tags": ["waiting-client"],
    }
    base.update(over)
    return base


def _patch(monkeypatch, entries, clickup_status="awaiting client response",
           db_row=("ts-1", {"clickup_task_id": "cu-1", "classification": {}})):
    monkeypatch.setattr(
        sweeper, "_fetch_conversation_entries", lambda _tid: entries
    )
    monkeypatch.setattr(
        sweeper, "_get_clickup_status",
        lambda _task: sweeper.normalize_status(clickup_status)
        if clickup_status else None,
    )
    monkeypatch.setattr(
        sweeper, "get_thread_by_ticket_id", lambda _tid: db_row
    )


# ---------------------------------------------------------------------------
# Rule 1: staleness is measured from our last outbound email
# ---------------------------------------------------------------------------

def test_stale_ticket_closes(monkeypatch):
    _patch(monkeypatch, [_out(41), _in(45)])
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "close", v["reason"]
    assert v["days_idle"] == 41
    assert v["timestamp_source"] == "zoho_outbound_thread"


def test_modified_time_today_does_not_reset_the_clock(monkeypatch):
    """REGRESSION: using modifiedTime would make this ticket 0 days idle.

    modifiedTime answers "when was this record last edited", not "when did we
    last write to the client", and tag/status/sync writes all move it. The idle
    clock must come from the outbound thread alone.
    """
    _patch(monkeypatch, [_out(60)])
    v = sweeper._assess(_ticket(modifiedTime=_iso(0)), NOW, days=30)
    assert v["action"] == "close"
    assert v["days_idle"] == 60


def test_conversation_order_does_not_matter(monkeypatch):
    """Zoho returns newest-first today; the sweep must not depend on that."""
    oldest_first = [_in(70), _out(60), _out(41)]
    newest_first = list(reversed(oldest_first))
    _patch(monkeypatch, oldest_first)
    a = sweeper._assess(_ticket(), NOW, days=30)
    _patch(monkeypatch, newest_first)
    b = sweeper._assess(_ticket(), NOW, days=30)
    assert a["days_idle"] == b["days_idle"] == 41


def test_recent_ticket_is_left_alone(monkeypatch):
    _patch(monkeypatch, [_out(9)])
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "skip_recent"


def test_exactly_at_threshold_closes(monkeypatch):
    _patch(monkeypatch, [_out(30)])
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "close"


def test_no_resolvable_timestamp_is_skipped_not_closed(monkeypatch):
    """Never close on a guessed date."""
    _patch(monkeypatch, [], db_row=None)
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "skip_no_timestamp"


def test_db_last_action_at_is_the_fallback(monkeypatch):
    """When Zoho has no readable outbound thread, fall back to the DB stamp."""
    _patch(monkeypatch, [], db_row=("ts-1", {
        "clickup_task_id": "cu-1",
        "classification": {},
        # Naive, as written by update_thread; must be read as UTC.
        "last_action_at": (NOW - timedelta(days=44)).replace(tzinfo=None),
    }))
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "close"
    assert v["days_idle"] == 44
    assert v["timestamp_source"] == "db_last_action_at"


# ---------------------------------------------------------------------------
# Rule 1b: a client who answered is never auto-closed
# ---------------------------------------------------------------------------

def test_client_reply_after_our_email_blocks_the_close(monkeypatch):
    _patch(monkeypatch, [_out(40), _in(38)])
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "skip_client_replied"
    assert "2026-07-11" in v["reason"]  # the reply date, 38 days back


def test_description_thread_alone_never_looks_like_a_reply(monkeypatch):
    """The original ticket body is inbound but always predates our reply."""
    body = _in(50)
    body["isDescriptionThread"] = True
    _patch(monkeypatch, [body, _out(40)])
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "close"


def test_internal_note_is_not_mistaken_for_a_client_reply(monkeypatch):
    note = {
        "id": "note-1",
        "createdTime": _iso(1),
        "isPublic": False,
        "type": "comment",
        "content": "engineer poked at this yesterday",
        "author": {"type": "AGENT", "email": "s.fagen@vomevolunteer.com"},
    }
    _patch(monkeypatch, [note, _out(40)])
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "close"
    assert v["days_idle"] == 40


def test_agent_analysis_note_is_ignored(monkeypatch):
    """AGENT ANALYSIS / AGENT UPDATE posts are ours, not client traffic."""
    analysis = {
        "id": "an-1",
        "createdTime": _iso(2),
        "content": sweeper.NOTE_HEADER + "\nsome analysis",
        "author": {"type": "END_USER", "email": CONTACT},
    }
    _patch(monkeypatch, [analysis, _out(40)])
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "close"


# ---------------------------------------------------------------------------
# Rule 2: both systems must agree the ticket is parked
# ---------------------------------------------------------------------------

def test_active_clickup_task_blocks_the_close(monkeypatch):
    _patch(monkeypatch, [_out(45)], clickup_status="in progress")
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "skip_clickup_busy"
    assert v["clickup_status"] == "in progress"


def test_on_dev_clickup_task_blocks_the_close(monkeypatch):
    _patch(monkeypatch, [_out(45)], clickup_status="on dev")
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "skip_clickup_busy"


def test_already_done_clickup_task_still_closes_zoho(monkeypatch):
    _patch(monkeypatch, [_out(45)], clickup_status="done")
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "close"


def test_unreadable_clickup_task_does_not_block(monkeypatch):
    """A ClickUp outage must not silently stop the sweep."""
    _patch(monkeypatch, [_out(45)], clickup_status=None)
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "close"
    assert v["clickup_status"] == "unreadable"


def test_ticket_with_no_clickup_task_closes(monkeypatch):
    _patch(monkeypatch, [_out(45)], db_row=("ts-1", {
        "clickup_task_id": "", "classification": {},
    }))
    v = sweeper._assess(_ticket(), NOW, days=30)
    assert v["action"] == "close"


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

def test_closing_message_promises_the_reopen_and_signs_as_vic():
    msg = sweeper._closing_message("Dana", None)
    assert "Hi Dana," in msg
    assert "reopens" in msg
    assert "Vic" in msg
    # One signature only.
    assert msg.count("support.vomevolunteer.com") == 1


def test_closing_message_falls_back_when_no_first_name():
    assert "Hi there," in sweeper._closing_message("", None)


def test_closing_message_uses_french_when_the_ticket_is_french():
    msg = sweeper._closing_message("Dana", "French")
    assert "Bonjour Dana," in msg
    assert "Cordialement," in msg


def test_no_em_dashes_in_client_facing_copy():
    """House rule: no em dashes in copy."""
    for lang in (None, "French"):
        assert "—" not in sweeper._closing_message("Dana", lang)


def test_internal_note_says_what_vic_did_and_carries_the_marker():
    note = sweeper._internal_note(
        41, NOW - timedelta(days=41), "zoho_outbound_thread"
    )
    assert "Vic closed this ticket automatically after 41 days" in note
    assert "reopens on any client reply" in note
    assert sweeper.AUTO_CLOSE_MARKER in note


# ---------------------------------------------------------------------------
# Entry-point behavior
# ---------------------------------------------------------------------------

def test_dry_run_changes_nothing(monkeypatch):
    monkeypatch.setattr(
        sweeper, "_fetch_awaiting_client_tickets", lambda: ([_ticket()], False)
    )
    _patch(monkeypatch, [_out(45)])
    monkeypatch.setattr(sweeper, "_post_report", lambda _t: None)
    monkeypatch.setattr(sweeper, "time", _NoSleep)

    def _boom(*a, **k):
        raise AssertionError("dry run must not write anything")

    monkeypatch.setattr(sweeper, "_close_one", _boom)
    monkeypatch.setattr(sweeper, "claim_sweeper_run", _boom)

    out = sweeper.run_stale_waiting_client_sweep(dry_run=True, days=30)
    assert out["dry_run"] is True
    assert out["scanned"] == 1
    assert len(out["closed"]) == 1          # reported as "would close"
    assert out["closed"][0]["last_outbound"]  # serialized to a string


def test_cap_takes_the_most_stale_first(monkeypatch):
    tickets = [
        _ticket(id="1", ticketNumber="101"),
        _ticket(id="2", ticketNumber="102"),
        _ticket(id="3", ticketNumber="103"),
    ]
    idle_by_id = {"1": 35, "2": 90, "3": 60}
    monkeypatch.setattr(
        sweeper, "_fetch_awaiting_client_tickets", lambda: (tickets, False)
    )
    monkeypatch.setattr(
        sweeper, "_fetch_conversation_entries",
        lambda tid: [_out(idle_by_id[str(tid)])],
    )
    monkeypatch.setattr(
        sweeper, "_get_clickup_status", lambda _t: "awaiting client response"
    )
    monkeypatch.setattr(sweeper, "get_thread_by_ticket_id", lambda _t: None)
    monkeypatch.setattr(sweeper, "_post_report", lambda _t: None)
    monkeypatch.setattr(sweeper, "time", _NoSleep)

    out = sweeper.run_stale_waiting_client_sweep(
        dry_run=True, days=30, limit=2
    )
    assert out["capped"] is True
    assert out["eligible_total"] == 3
    assert [v["ticket_number"] for v in out["closed"]] == ["102", "103"]


def test_send_email_override_reaches_close_and_report(monkeypatch):
    """Backfill batches must be able to close without emailing clients."""
    monkeypatch.setattr(
        sweeper, "_fetch_awaiting_client_tickets", lambda: ([_ticket()], False)
    )
    _patch(monkeypatch, [_out(300)])
    captured = []
    monkeypatch.setattr(sweeper, "_post_report", captured.append)
    monkeypatch.setattr(sweeper, "time", _NoSleep)
    monkeypatch.setattr(sweeper, "claim_sweeper_run", lambda _k: True)
    monkeypatch.setattr(sweeper, "finish_sweeper_run", lambda _k, _s: None)
    seen = []
    monkeypatch.setattr(
        sweeper, "_close_one",
        lambda v, send: seen.append(send) or {"errors": [], "closed": True},
    )

    out = sweeper.run_stale_waiting_client_sweep(
        dry_run=False, days=30, send_email=False
    )
    assert seen == [False]
    assert out["send_email"] is False
    assert "no client email" in captured[0]


def test_client_email_is_off_by_default():
    """POLICY: the sweep never writes to the client unless told to.

    Pins the env default, so flipping it back on has to be a deliberate,
    reviewed change rather than a passing edit.
    """
    assert sweeper.SEND_CLOSING_EMAIL is False


def test_default_run_closes_without_emailing(monkeypatch):
    """With send_email unset, _close_one must receive False."""
    monkeypatch.setattr(
        sweeper, "_fetch_awaiting_client_tickets", lambda: ([_ticket()], False)
    )
    _patch(monkeypatch, [_out(200)])
    monkeypatch.setattr(sweeper, "_post_report", lambda _t: None)
    monkeypatch.setattr(sweeper, "time", _NoSleep)
    monkeypatch.setattr(sweeper, "claim_sweeper_run", lambda _k: True)
    monkeypatch.setattr(sweeper, "finish_sweeper_run", lambda _k, _s: None)
    seen = []
    monkeypatch.setattr(
        sweeper, "_close_one",
        lambda v, send: seen.append(send) or {"errors": [], "closed": True},
    )

    out = sweeper.run_stale_waiting_client_sweep(dry_run=False, days=30)
    assert seen == [False]
    assert out["send_email"] is False


def test_report_states_when_clients_are_emailed(monkeypatch):
    monkeypatch.setattr(
        sweeper, "_fetch_awaiting_client_tickets", lambda: ([_ticket()], False)
    )
    _patch(monkeypatch, [_out(45)])
    captured = []
    monkeypatch.setattr(sweeper, "_post_report", captured.append)
    monkeypatch.setattr(sweeper, "time", _NoSleep)

    sweeper.run_stale_waiting_client_sweep(
        dry_run=True, days=30, send_email=True
    )
    assert "Clients ARE emailed a closing note." in captured[0]


def test_paging_cap_is_reported_not_hidden(monkeypatch):
    """A truncated scan must never read as 'we covered everything'."""
    monkeypatch.setattr(
        sweeper, "_fetch_awaiting_client_tickets", lambda: ([_ticket()], True)
    )
    _patch(monkeypatch, [_out(45)])
    captured = []
    monkeypatch.setattr(sweeper, "_post_report", captured.append)
    monkeypatch.setattr(sweeper, "time", _NoSleep)

    out = sweeper.run_stale_waiting_client_sweep(dry_run=True, days=30)
    assert out["paging_cap_hit"] is True
    assert "Paging cap reached" in captured[0]


# ---------------------------------------------------------------------------
# Live-run write ordering
# ---------------------------------------------------------------------------

def _live_patch(monkeypatch, zoho_ok=True):
    """Stub every write path; record what got called."""
    calls = {"email": [], "zoho_status": [], "note": [], "cu_status": [],
             "cu_comment": [], "cu_field": [], "db": []}
    monkeypatch.setattr(
        sweeper, "send_zoho_reply",
        lambda t, m, e: calls["email"].append(t) or {"ok": True},
    )
    monkeypatch.setattr(
        sweeper, "set_zoho_status",
        lambda t, s: (calls["zoho_status"].append((t, s)), zoho_ok)[1],
    )
    monkeypatch.setattr(
        sweeper, "post_internal_note",
        lambda t, n: calls["note"].append(n) or True,
    )
    monkeypatch.setattr(
        sweeper, "set_clickup_status",
        lambda t, s: calls["cu_status"].append((t, s)) or True,
    )
    monkeypatch.setattr(
        sweeper, "_add_clickup_comment",
        lambda t, n: calls["cu_comment"].append(t) or True,
    )
    monkeypatch.setattr(
        sweeper, "set_clickup_custom_field",
        lambda t, f, v: calls["cu_field"].append((f, v)) or True,
    )
    monkeypatch.setattr(
        sweeper, "update_thread",
        lambda ts, **kw: calls["db"].append(kw),
    )
    monkeypatch.setattr(
        sweeper, "get_thread_by_ticket_id",
        lambda _t: ("ts-1", {"clickup_task_id": "cu-1", "classification": {}}),
    )
    return calls


def _verdict(**over) -> dict:
    v = {
        "ticket_id": "5001",
        "ticket_number": "7905",
        "subject": "Shift times missing",
        "contact_email": CONTACT,
        "contact_first_name": "Dana",
        "clickup_task_id": "cu-1",
        "clickup_status": "awaiting client response",
        "language": None,
        "days_idle": 41,
        "last_outbound": NOW - timedelta(days=41),
        "timestamp_source": "zoho_outbound_thread",
        "action": "close",
        "reason": "41d",
        "has_waiting_tag": True,
    }
    v.update(over)
    return v


def test_live_close_writes_both_systems(monkeypatch):
    calls = _live_patch(monkeypatch)
    result = sweeper._close_one(_verdict(), send_email=True)
    assert result["closed"] is True
    assert result["errors"] == []
    assert calls["zoho_status"] == [("5001", "Closed")]
    assert calls["cu_status"] == [("cu-1", "CLOSED")]
    assert len(calls["email"]) == 1
    # The internal note lands on BOTH systems.
    assert len(calls["note"]) == 1 and len(calls["cu_comment"]) == 1
    assert calls["db"][0]["last_action"] == "auto_closed_no_client_response"


def test_failed_zoho_close_leaves_clickup_and_db_untouched(monkeypatch):
    """Half-closing a ticket manufactures the exact drift rule 2 detects."""
    calls = _live_patch(monkeypatch, zoho_ok=False)
    result = sweeper._close_one(_verdict(), send_email=False)
    assert result["closed"] is False
    assert calls["cu_status"] == []
    assert calls["cu_comment"] == []
    assert calls["db"] == []


def test_already_done_clickup_task_is_not_rewritten(monkeypatch):
    calls = _live_patch(monkeypatch)
    sweeper._close_one(_verdict(clickup_status="done"), send_email=False)
    assert calls["cu_status"] == []      # already finished
    assert calls["cu_comment"] == ["cu-1"]  # note still posted


def test_resolution_field_is_skipped_when_no_option_id(monkeypatch):
    """Better unset than mislabeled 'completed'."""
    calls = _live_patch(monkeypatch)
    monkeypatch.setattr(sweeper, "RESOLUTION_MAP", {"no_response": ""})
    sweeper._close_one(_verdict(), send_email=False)
    assert calls["cu_field"] == []


def test_resolution_field_is_written_when_configured(monkeypatch):
    calls = _live_patch(monkeypatch)
    monkeypatch.setattr(sweeper, "RESOLUTION_MAP", {"no_response": "opt-9"})
    sweeper._close_one(_verdict(), send_email=False)
    assert calls["cu_field"] == [(sweeper.FIELD_RESOLUTION, "opt-9")]


def test_missing_contact_email_is_reported_but_still_closes(monkeypatch):
    calls = _live_patch(monkeypatch)
    result = sweeper._close_one(
        _verdict(contact_email=""), send_email=True
    )
    assert calls["email"] == []
    assert any("no contact email" in e for e in result["errors"])
    assert result["closed"] is True


# ---------------------------------------------------------------------------
# Drain mode
# ---------------------------------------------------------------------------

def _fake_sweeps(monkeypatch, script):
    """Feed run_stale_waiting_client_sweep a canned sequence of results.

    Each script entry is (closed, failed, eligible_total).
    """
    calls = []

    def fake(**kw):
        calls.append(kw)
        c, f, e = script[min(len(calls) - 1, len(script) - 1)]
        # Honour the limit the drain passed, as the real sweep does. Without
        # this the stub can "close" more than it was asked to and the
        # DRAIN_MAX_TOTAL assertion tests nothing.
        c = min(c, kw.get("limit") or c)
        return {
            "closed": [{"n": i} for i in range(c)],
            "failed": [{"n": i} for i in range(f)],
            "eligible_total": e,
            "errors": [],
        }

    monkeypatch.setattr(sweeper, "run_stale_waiting_client_sweep", fake)
    monkeypatch.setattr(sweeper, "_post_report", lambda _t: None)
    monkeypatch.setattr(sweeper, "time", _NoSleep)
    return calls


def test_drain_loops_until_the_pool_is_empty(monkeypatch):
    calls = _fake_sweeps(monkeypatch, [
        (50, 0, 120), (50, 0, 70), (20, 0, 20), (0, 0, 0),
    ])
    out = sweeper.run_stale_waiting_client_drain(days=60, limit=50)
    assert out["stopped_because"] == "pool_empty"
    assert out["total_closed"] == 120
    assert out["batches_run"] == 4
    assert len(calls) == 4


def test_drain_always_runs_live_and_forces(monkeypatch):
    """A drain is explicit, so it bypasses the once-per-day claim."""
    calls = _fake_sweeps(monkeypatch, [(10, 0, 10), (0, 0, 0)])
    sweeper.run_stale_waiting_client_drain(days=60, limit=50)
    assert all(c["dry_run"] is False for c in calls)
    assert all(c["force"] is True for c in calls)


def test_drain_stops_when_a_batch_makes_no_progress(monkeypatch):
    """Eligible tickets but zero closed means something is broken.

    Retrying cannot help and would spin forever, so it must stop and say so.
    """
    calls = _fake_sweeps(monkeypatch, [(0, 50, 200)])
    out = sweeper.run_stale_waiting_client_drain(days=60, limit=50)
    assert out["stopped_because"] == "no_progress"
    assert len(calls) == 1


def test_drain_respects_the_batch_ceiling(monkeypatch):
    calls = _fake_sweeps(monkeypatch, [(10, 0, 500)])
    out = sweeper.run_stale_waiting_client_drain(
        days=60, limit=10, max_batches=3
    )
    assert out["stopped_because"] == "max_batches"
    assert len(calls) == 3


def test_drain_respects_the_total_close_ceiling(monkeypatch):
    calls = _fake_sweeps(monkeypatch, [(50, 0, 999)])
    monkeypatch.setattr(sweeper, "DRAIN_MAX_TOTAL", 120)
    out = sweeper.run_stale_waiting_client_drain(
        days=60, limit=50, max_batches=99
    )
    assert out["stopped_because"] == "drain_max_total"
    assert out["total_closed"] == 120
    # Final batch is trimmed so the ceiling is never overshot.
    assert calls[-1]["limit"] == 20


def test_drain_passes_send_email_through(monkeypatch):
    calls = _fake_sweeps(monkeypatch, [(5, 0, 5), (0, 0, 0)])
    sweeper.run_stale_waiting_client_drain(days=60, limit=50, send_email=False)
    assert all(c["send_email"] is False for c in calls)


def test_drain_reports_every_stop_reason(monkeypatch):
    """The final Slack line must exist for each stop reason, not KeyError."""
    for script, expect in (
        ([(5, 0, 5), (0, 0, 0)], "pool_empty"),
        ([(0, 1, 9)], "no_progress"),
        ([(1, 0, 99)], "max_batches"),
    ):
        _fake_sweeps(monkeypatch, script)
        posted = []
        monkeypatch.setattr(sweeper, "_post_report", posted.append)
        out = sweeper.run_stale_waiting_client_drain(
            days=60, limit=1, max_batches=1 if expect == "max_batches" else 9
        )
        assert out["stopped_because"] == expect
        assert posted and "drain finished" in posted[0]


# ---------------------------------------------------------------------------
# Minimal runner so this file works without pytest installed
# ---------------------------------------------------------------------------

class _NoSleep:
    """Stands in for the `time` module so tests do not actually sleep."""

    @staticmethod
    def sleep(_seconds):
        return None

class _MonkeyPatch:
    """Tiny stand-in for pytest's monkeypatch fixture."""

    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._undo):
            setattr(target, name, old)
        self._undo = []


def _main() -> int:
    import inspect

    tests = [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failures = []
    for name, fn in tests:
        mp = _MonkeyPatch()
        try:
            if "monkeypatch" in inspect.signature(fn).parameters:
                fn(mp)
            else:
                fn()
            print(f"  ok    {name}")
        except Exception as e:
            failures.append((name, e))
            print(f"  FAIL  {name}: {e}")
        finally:
            mp.undo()

    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
