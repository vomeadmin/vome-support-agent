"""Tests for the help-center grounding shared by the drafting surfaces.

agent.py, slack_reply_handler.py and ops/draft.py all call
kb_context.build_ticket_kb_block. The contract that matters to them is:
it never raises, it returns "" when there is nothing useful, and when it
does return a block the article URL is in it so the draft can cite it.
"""
import os

for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import kb_context  # noqa: E402


def _hit(title="Reserving a shift", days=10):
    return {
        "title": title,
        "url": "https://support.vomevolunteer.com/portal/en/kb/articles/x",
        "body": "Open the schedule, pick a shift, then press Reserve.",
        "days_stale": days,
        "score": 0.4,
    }


def _stub_search(monkeypatch, result, seen=None):
    def _search(query, locale=None):
        if seen is not None:
            seen.append((query, locale))
        return result(query, locale) if callable(result) else result
    monkeypatch.setattr(kb_context, "search_articles", _search)


# ---------------------------------------------------------- query building

def test_query_leads_with_the_latest_message_not_the_subject():
    """A thread evolves. What they asked last is what needs answering."""
    q = kb_context.build_search_query(
        subject="Cannot log in",
        latest_message="actually now the shift reserve button is greyed out",
    )
    assert "Cannot log in" in q
    assert "reserve button is greyed out" in q


def test_query_falls_back_to_the_body_when_there_is_no_reply_yet():
    q = kb_context.build_search_query(
        subject="Shifts", latest_message="", body="how do I publish a shift"
    )
    assert "how do I publish a shift" in q


def test_query_is_capped():
    q = kb_context.build_search_query(subject="s", latest_message="x" * 5000)
    assert len(q) <= kb_context.MAX_QUERY_CHARS


def test_empty_inputs_produce_no_block(monkeypatch):
    _stub_search(monkeypatch, [_hit()])
    assert kb_context.build_ticket_kb_block() == ""


# ------------------------------------------------------------- the block

def test_block_carries_url_and_body_so_the_draft_can_cite_it(monkeypatch):
    _stub_search(monkeypatch, [_hit()])
    block = kb_context.build_ticket_kb_block(subject="reserve a shift")

    assert "Reserving a shift" in block
    assert "kb/articles/x" in block
    assert "press Reserve" in block


def test_no_match_returns_empty_string(monkeypatch):
    _stub_search(monkeypatch, [])
    assert kb_context.build_ticket_kb_block(subject="reserve a shift") == ""


def test_block_tells_the_model_to_ignore_irrelevant_articles(monkeypatch):
    """Retrieval is keyword based, so the prompt has to allow a miss."""
    _stub_search(monkeypatch, [_hit()])
    block = kb_context.build_ticket_kb_block(subject="reserve a shift")
    assert "ignore" in block.lower()
    assert "candidate, not a verified answer" in block


def test_old_article_carries_a_verify_caveat(monkeypatch):
    _stub_search(monkeypatch, [_hit(days=900)])
    block = kb_context.build_ticket_kb_block(subject="reserve a shift")
    assert "over two years ago" in block


def test_recent_article_carries_no_caveat(monkeypatch):
    _stub_search(monkeypatch, [_hit(days=5)])
    block = kb_context.build_ticket_kb_block(subject="reserve a shift")
    assert "over two years ago" not in block
    assert "over a year old" not in block


# ------------------------------------------------------------- language

def test_french_ticket_searches_french_first():
    assert kb_context._to_locale("French") == "fr"


def test_undetected_language_searches_both():
    """None means 'not detected as French', not 'definitely English'."""
    assert kb_context._to_locale(None) is None


def test_french_ticket_falls_back_to_english_articles(monkeypatch):
    """Most of the KB is English. A French ticket still deserves grounding."""
    seen = []
    _stub_search(
        monkeypatch,
        lambda q, locale: [] if locale == "fr" else [_hit()],
        seen=seen,
    )

    block = kb_context.build_ticket_kb_block(
        subject="reserver un quart", detected_lang="French"
    )

    assert [locale for _, locale in seen] == ["fr", None]
    assert "Reserving a shift" in block


# ------------------------------------------------------------- resilience

def test_a_broken_index_degrades_the_draft_instead_of_blocking_it(monkeypatch):
    """A Postgres outage must cost the draft its grounding, not block it."""
    import sys
    import types

    def _boom(*_a, **_k):
        raise RuntimeError("connection refused")

    fake_kb_sync = types.ModuleType("kb_sync")
    fake_kb_sync.search_kb_articles = _boom
    monkeypatch.setitem(sys.modules, "kb_sync", fake_kb_sync)

    assert kb_context.search_articles("anything") == []
    assert kb_context.build_ticket_kb_block(subject="anything") == ""
