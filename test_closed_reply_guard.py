"""Regression test for the auto-close bug on ticket 7905 (#...38755009).

A client replied "still unable to see any shift times ... screenshots attached"
to a resolved ticket and it was silently re-closed, because:
  1. the conversations list endpoint returns `summary` (not `content`) on reply
     threads, so the classifiers judged empty text, and
  2. a closed ticket re-closed on a bare "ack" classification.

These tests pin the fix without hitting the network.
"""
import os
# Dummy creds so module-level client constructors don't raise on import.
# None of these make network calls at construction time.
for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import agent  # noqa: E402

# The real reply thread object as Zoho's getTicketConversations returned it:
# `summary` present, NO `content` key, 3 attachments.
REAL_REPLY = {
    "summary": (
        "Hi Sam, I just took a look and am still unable to see any shift "
        "times from the opportunity dashboard. I'll attach a few screenshots "
        "of my view of one deactivated opportunity- perha..."
    ),
    "hasAttach": True,
    "attachmentCount": "3",
    "author": {"type": "END_USER", "email": "volunteers@sasklegion.ca"},
}


def test_reply_text_is_read_from_summary_when_content_missing():
    text = agent._extract_reply_text(REAL_REPLY)
    assert text, "reply text must not be empty (regression: was '' -> '(no text content)')"
    assert "still unable" in text.lower()


def test_action_signal_fires_on_real_reply():
    text = agent._extract_reply_text(REAL_REPLY)
    assert agent._ACTION_SIGNAL_RE.search(text), "'still'/'unable' must register as action-needed"


def test_real_reply_is_not_a_confident_ack():
    # Attachments alone disqualify it; the model is never consulted here.
    text = agent._extract_reply_text(REAL_REPLY)
    assert agent._is_confident_ack(text, has_attachment=True) is False
    # And even without attachments, the action signal disqualifies it.
    assert agent._is_confident_ack(text, has_attachment=False) is False


def test_empty_or_unreadable_reply_is_never_a_confident_ack():
    assert agent._is_confident_ack("", has_attachment=False) is False
    assert agent._is_confident_ack("   ", has_attachment=False) is False


def test_no_action_guard_also_rejects_the_real_reply():
    text = agent._extract_reply_text(REAL_REPLY)
    assert agent._is_no_action_reply(text, has_attachment=True) is False


def test_status_change_is_not_treated_as_a_reply():
    # Sam manually closes a ticket: Zoho fires a ticket-field update. The
    # payload carries only `id` (the ticket), no separate reply `ticketId`,
    # and eventType is a ticket update. Must NOT trigger reply-handling, or it
    # would restore the status he just set (the Closed<->Processing loop).
    assert agent.is_zoho_reply_event("Ticket_Update", "569440000038666806", "") is False
    assert agent.is_zoho_reply_event("Ticket_Update", "TID", "TID") is False
    assert agent.is_zoho_reply_event("unknown", "TID", "") is False


def test_client_reply_is_treated_as_a_reply():
    # A thread-add: reply ID in `id`, real ticket ID in `ticketId` (differs).
    assert agent.is_zoho_reply_event("Ticket_Thread_Add", "REPLY_ID", "TICKET_ID") is True
    # eventType signal alone is enough even if the shape is ambiguous.
    assert agent.is_zoho_reply_event("Ticket_Thread_Add", "TID", "") is True
    # Shape signal alone is enough even if eventType is missing/unknown.
    assert agent.is_zoho_reply_event("unknown", "REPLY_ID", "TICKET_ID") is True


def test_confident_ack_still_allows_a_genuine_thanks(monkeypatch):
    # A real "thank you!" with no attachments/signals: the model is consulted
    # and, if it agrees, this remains a confident ack (so genuine acks still
    # auto-close and don't clutter the queue).
    monkeypatch.setattr(agent, "_classify_no_action_reply", lambda t: True)
    assert agent._is_confident_ack("Thank you so much!", has_attachment=False) is True
    # ...but attachments override even a model "ack".
    assert agent._is_confident_ack("Thank you so much!", has_attachment=True) is False


if __name__ == "__main__":
    # Minimal runner so this works without pytest installed.
    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            import inspect
            kwargs = {"monkeypatch": _MP()} if "monkeypatch" in inspect.signature(fn).parameters else {}
            fn(**kwargs)
            print(f"PASS {name}")
            passed += 1
    print(f"\n{passed} passed")
