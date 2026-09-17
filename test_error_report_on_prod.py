"""Automated Vome Error Reports must never get a client email.

The web app's ErrorBoundary files these on the user's behalf when the UI
crashes. The person who triggered one rarely remembers it and almost never
recognizes a later "the issue you reported is fixed" note as related to
anything they did, so on ON PROD we close the ticket silently instead.

These tests pin that behavior without hitting the network.
"""
import os

# Dummy creds so module-level client constructors don't raise on import.
for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import error_reports  # noqa: E402
from signatures import signature  # noqa: E402
import on_prod_handler  # noqa: E402

# The subject and first body line the web app generates (vome-react
# src/components/ErrorBoundary.js).
ERROR_SUBJECT = "Vome Error Report"
ERROR_BODY = (
    "=== Vome Error Report ===\n"
    "Message: TypeError: Cannot read properties of null\n"
    "Route: /opportunity/dashboard\n"
    "Build: 2edc4af\n"
)


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

def test_detector_matches_subject_and_body():
    assert error_reports.is_error_report(ERROR_SUBJECT, "") is True
    assert error_reports.is_error_report("", ERROR_BODY) is True
    assert error_reports.is_error_report("vome ERROR report") is True


def test_detector_ignores_normal_tickets():
    assert error_reports.is_error_report(
        "Shifts not saving",
        "When I click save on a shift nothing happens.",
    ) is False
    assert error_reports.is_error_report("", "") is False
    assert error_reports.is_error_report(None, None) is False


# ---------------------------------------------------------------------------
# ON PROD flow
# ---------------------------------------------------------------------------

def _install_spies(monkeypatch, ticket_fields):
    """Stub every outbound call in handle_on_prod; record what was hit."""
    calls = {"sent": [], "closed": [], "final_review": [], "clickup": [],
             "slack": [], "drafted": []}

    monkeypatch.setattr(on_prod_handler, "_get_clickup_task", lambda t: {
        "name": ticket_fields.get("subject", ""),
        "url": f"https://app.clickup.com/t/{t}",
        "description": "Classification: Bug\nModule: Schedule",
    })
    monkeypatch.setattr(
        on_prod_handler, "_extract_zoho_ticket_id", lambda task: "99001"
    )
    monkeypatch.setattr(
        on_prod_handler, "fetch_ticket_from_zoho", lambda tid: {"id": tid}
    )
    monkeypatch.setattr(
        on_prod_handler, "fetch_ticket_conversations", lambda tid: {}
    )
    monkeypatch.setattr(
        on_prod_handler, "_extract_ticket_fields", lambda t: ticket_fields
    )
    monkeypatch.setattr(
        on_prod_handler, "_format_conversations", lambda c: ""
    )
    monkeypatch.setattr(on_prod_handler, "_find_thread_ts", lambda tid: None)
    monkeypatch.setattr(on_prod_handler, "CHANNEL_FINISHED_TASKS", "")

    monkeypatch.setattr(
        on_prod_handler, "_send_resolution_email",
        lambda tid, content, to_email, cc_email="": (
            calls["sent"].append((tid, to_email)) or True
        ),
    )
    monkeypatch.setattr(
        on_prod_handler, "_set_zoho_status_closed",
        lambda tid: calls["closed"].append(tid) or True,
    )
    monkeypatch.setattr(
        on_prod_handler, "_set_zoho_status_final_review",
        lambda tid: calls["final_review"].append(tid) or True,
    )
    monkeypatch.setattr(
        on_prod_handler, "update_clickup_status_finished",
        lambda tid: calls["clickup"].append(tid) or True,
    )
    monkeypatch.setattr(
        on_prod_handler, "_post_on_prod_record",
        lambda zid, cid, text, status, fields, ts: calls["slack"].append(
            (status, text)
        ),
    )
    monkeypatch.setattr(
        on_prod_handler, "_generate_resolution_draft",
        lambda **kw: calls["drafted"].append(kw) or (
            "Hi Dana, the issue you reported is now resolved and the update "
            "is live. Take a look when you get a chance and let us know if "
            "anything still looks off.\n\n" + signature("vic")
        ),
    )
    monkeypatch.setattr(
        on_prod_handler, "_assess_resolution_state",
        lambda fields, text: {
            "already_confirmed_fixed": False,
            "recommendation": "send",
            "reason": "",
            "last_team_reply": "",
        },
    )
    return calls


def test_error_report_is_closed_without_emailing(monkeypatch):
    calls = _install_spies(monkeypatch, {
        "subject": ERROR_SUBJECT,
        "description": ERROR_BODY,
        "contact_name": "Dana Ruiz",
        "contact_email": "dana@example.org",
        "cc_email": "",
    })

    assert on_prod_handler.handle_on_prod("cu123", "Sanjay") is True

    assert calls["sent"] == [], "no client email may go out on an error report"
    assert calls["drafted"] == [], "no draft should even be generated"
    assert calls["closed"] == ["99001"], "Zoho ticket must be closed"
    assert calls["clickup"] == ["cu123"], "ClickUp task must be closed"
    assert calls["final_review"] == [], (
        "error reports must not pass through Final Review"
    )

    # A Slack record still goes out so the close is visible internally.
    assert len(calls["slack"]) == 1
    status, text = calls["slack"][0]
    assert status == on_prod_handler.THREAD_CLOSED
    assert "no email sent" in text.lower()


def test_normal_ticket_still_gets_the_resolution_email(monkeypatch):
    calls = _install_spies(monkeypatch, {
        "subject": "Shifts not saving",
        "description": "When I click save on a shift nothing happens.",
        "contact_name": "Dana Ruiz",
        "contact_email": "dana@example.org",
        "cc_email": "",
    })

    assert on_prod_handler.handle_on_prod("cu456", "Sanjay") is True

    assert calls["sent"] == [("99001", "dana@example.org")]
    assert calls["final_review"] == ["99001"]
    assert calls["closed"] == ["99001"]
    assert calls["clickup"] == ["cu456"]
