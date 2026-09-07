"""Regression tests for the two KB sync defects.

1. DATA LOSS. delete_missing_kb_articles removes every row whose id was
   not seen during the fetch. fetch_all_kb_articles skips a whole
   category when getArticles errors, and skips an article when its
   detail call fails, so a single Zoho hiccup used to silently delete
   those articles from the agent's knowledge. The delete step now runs
   only after a fetch that reported no failures.

2. UNPUBLISHED ARTICLES. Zoho's getArticles returns Draft and
   Unpublished articles alongside Published ones. Those live at
   help-center URLs that 404 for a client, so they must never reach the
   index or a drafted reply.

No network, no database: the Zoho and Postgres boundaries are stubbed.
"""
import os

for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import kb_sync  # noqa: E402


def _article(aid, status="Published", body="x" * 50):
    return {
        "id": aid,
        "title": f"Article {aid}",
        "content": body,
        "permalink": f"article-{aid}",
        "category": "FAQs & Knowledge Base",
        "modifiedTime": "2026-09-01T10:00:00.000Z",
        "createdTime": "2026-08-01T10:00:00.000Z",
        "language": "en",
        "status": status,
        "url": f"https://support.vomevolunteer.com/portal/en/kb/articles/{aid}",
    }


class _Recorder:
    """Stands in for the Postgres writes."""

    def __init__(self):
        self.upserted = []
        self.delete_calls = []

    def upsert(self, article):
        self.upserted.append(str(article["id"]))
        return "added"

    def delete(self, seen_ids):
        self.delete_calls.append(list(seen_ids))
        return 0


def _patch_db(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(kb_sync, "upsert_kb_article", rec.upsert)
    monkeypatch.setattr(kb_sync, "delete_missing_kb_articles", rec.delete)
    return rec


# ---------------------------------------------------------------------
# 1. The delete guard
# ---------------------------------------------------------------------

def test_delete_runs_after_a_clean_fetch(monkeypatch):
    rec = _patch_db(monkeypatch)
    monkeypatch.setattr(kb_sync, "LAST_FETCH_COMPLETE", True)

    stats = kb_sync.sync_articles_to_db([_article("1"), _article("2")])

    assert rec.delete_calls == [["1", "2"]]
    assert stats["delete_skipped"] is False


def test_delete_is_skipped_after_a_partial_fetch(monkeypatch):
    """The whole point: a partial fetch must not look like a deletion."""
    rec = _patch_db(monkeypatch)
    monkeypatch.setattr(kb_sync, "LAST_FETCH_COMPLETE", False)

    stats = kb_sync.sync_articles_to_db([_article("1")])

    assert rec.delete_calls == [], "delete ran on an incomplete fetch"
    assert stats["delete_skipped"] is True
    # Articles still get indexed. Only the destructive half is held back.
    assert rec.upserted == ["1"]


def test_delete_is_skipped_when_no_fetch_has_run_in_this_process(monkeypatch):
    """A restarted dyno starts at None, not False. Never delete on the
    word of a set of articles this process did not fetch itself."""
    rec = _patch_db(monkeypatch)
    monkeypatch.setattr(kb_sync, "LAST_FETCH_COMPLETE", None)

    stats = kb_sync.sync_articles_to_db([_article("1")])

    assert rec.delete_calls == []
    assert stats["delete_skipped"] is True


def test_explicit_allow_delete_overrides_the_inherited_flag(monkeypatch):
    rec = _patch_db(monkeypatch)
    monkeypatch.setattr(kb_sync, "LAST_FETCH_COMPLETE", False)

    kb_sync.sync_articles_to_db([_article("1")], allow_delete=True)

    assert rec.delete_calls == [["1"]]


def test_short_body_article_is_kept_in_the_seen_set(monkeypatch):
    """A stripped-HTML glitch must not delete a real article."""
    rec = _patch_db(monkeypatch)
    monkeypatch.setattr(kb_sync, "LAST_FETCH_COMPLETE", True)

    stats = kb_sync.sync_articles_to_db(
        [_article("1"), _article("2", body="hi")]
    )

    assert stats["skipped"] == 1
    assert rec.upserted == ["1"]
    assert rec.delete_calls == [["1", "2"]], "id 2 must survive the delete"


# ---------------------------------------------------------------------
# 2. Published-only indexing
# ---------------------------------------------------------------------

def test_draft_and_unpublished_articles_are_not_indexed(monkeypatch):
    rec = _patch_db(monkeypatch)
    monkeypatch.setattr(kb_sync, "LAST_FETCH_COMPLETE", True)

    stats = kb_sync.sync_articles_to_db([
        _article("1", status="Published"),
        _article("2", status="Draft"),
        _article("3", status="Unpublished"),
    ])

    assert rec.upserted == ["1"]
    assert stats["skipped_unpublished"] == 2
    # They are also absent from the seen set, so a clean sync removes any
    # row left over from before this filter existed.
    assert rec.delete_calls == [["1"]]


def test_is_published_accepts_only_published():
    assert kb_sync._is_published("Published")
    assert kb_sync._is_published("published")
    assert kb_sync._is_published("  Published  ")
    assert not kb_sync._is_published("Draft")
    assert not kb_sync._is_published("Unpublished")
    assert not kb_sync._is_published("")
    assert not kb_sync._is_published(None)


# ---------------------------------------------------------------------
# 3. The fetch marks itself incomplete on the real failure modes
# ---------------------------------------------------------------------

def _fake_zoho(monkeypatch, articles_response):
    """Stub _zoho_desk_call with one root category and a scripted
    getArticles response."""
    def _call(tool_name, arguments):
        if tool_name == "ZohoDesk_getAllKBRootCategories":
            return {"data": [{"id": "cat1", "name": "FAQs & Knowledge Base"}]}
        if tool_name == "ZohoDesk_getArticles":
            return articles_response
        if tool_name == "ZohoDesk_getArticle":
            aid = arguments["path_variables"]["id"]
            if aid == "boom":
                return {"isError": True, "content": []}
            return {"id": aid, "title": f"Article {aid}",
                    "answer": "<p>" + "body text " * 10 + "</p>",
                    "permalink": f"article-{aid}", "status": "Published",
                    "modifiedTime": "2026-09-01T10:00:00.000Z",
                    "createdTime": "2026-08-01T10:00:00.000Z"}
        return None

    monkeypatch.setattr(kb_sync, "_zoho_desk_call", _call)
    monkeypatch.setattr(kb_sync, "_unwrap_mcp_result", lambda r: r)
    monkeypatch.setattr(kb_sync.time, "sleep", lambda *_: None)


def test_fetch_is_marked_incomplete_when_a_category_errors(monkeypatch):
    _fake_zoho(monkeypatch, {
        "isError": True,
        "content": [{"type": "text", "text": "rate limit exceeded"}],
    })

    articles = kb_sync.fetch_all_kb_articles()

    assert articles == []
    assert kb_sync.LAST_FETCH_COMPLETE is False
    assert any(
        "getArticles failed" in m for m in kb_sync.failure_messages()
    )
    assert [f["kind"] for f in kb_sync.LAST_FETCH_FAILURES] == [
        kb_sync.FAILURE_CATEGORY
    ]


def test_fetch_is_marked_incomplete_when_an_article_detail_fails(monkeypatch):
    _fake_zoho(monkeypatch, {
        "data": [
            {"id": "ok1", "status": "Published"},
            {"id": "boom", "status": "Published"},
        ],
    })

    articles = kb_sync.fetch_all_kb_articles()

    assert [a["id"] for a in articles] == ["ok1"]
    assert kb_sync.LAST_FETCH_COMPLETE is False, (
        "one missing article must not authorise a delete pass"
    )


# ---------------------------------------------------------------------
# 4. Transient detail failures are retried
#
# The first production run hit two getArticle failures out of ~507. Both
# ids fetched fine on retry, so they were proxy blips. Without a retry a
# single blip marks every run incomplete, which blocks the delete step
# forever and means an article deleted in Zoho is never pruned.
# ---------------------------------------------------------------------

def test_a_transient_detail_failure_is_retried(monkeypatch):
    calls = []

    def _call(tool_name, arguments):
        if tool_name != "ZohoDesk_getArticle":
            return None
        calls.append(arguments["path_variables"]["id"])
        if len(calls) == 1:
            return {"isError": True, "content": []}
        return {"id": "a1", "title": "Recovered", "answer": "body " * 20}

    monkeypatch.setattr(kb_sync, "_zoho_desk_call", _call)
    monkeypatch.setattr(kb_sync, "_unwrap_mcp_result", lambda r: r)
    monkeypatch.setattr(kb_sync.time, "sleep", lambda *_: None)

    got = kb_sync._fetch_article_detail("a1")

    assert got is not None and got["title"] == "Recovered"
    assert len(calls) == 2, "must retry after a transient failure"


def test_retries_are_bounded(monkeypatch):
    calls = []

    def _call(tool_name, arguments):
        calls.append(1)
        return {"isError": True, "content": []}

    monkeypatch.setattr(kb_sync, "_zoho_desk_call", _call)
    monkeypatch.setattr(kb_sync, "_unwrap_mcp_result", lambda r: r)
    monkeypatch.setattr(kb_sync.time, "sleep", lambda *_: None)

    assert kb_sync._fetch_article_detail("a1") is None
    assert len(calls) == kb_sync.DETAIL_ATTEMPTS


def test_a_persistently_failing_article_still_blocks_the_delete(monkeypatch):
    """Retries must not paper over a real failure."""
    _fake_zoho(monkeypatch, {
        "data": [
            {"id": "ok1", "status": "Published"},
            {"id": "boom", "status": "Published"},
        ],
    })

    kb_sync.fetch_all_kb_articles()

    assert kb_sync.LAST_FETCH_COMPLETE is False


def test_clean_fetch_is_marked_complete_and_drops_unpublished(monkeypatch):
    _fake_zoho(monkeypatch, {
        "data": [
            {"id": "ok1", "status": "Published"},
            {"id": "ok2", "status": "Draft"},
        ],
    })

    articles = kb_sync.fetch_all_kb_articles()

    assert [a["id"] for a in articles] == ["ok1"]
    assert kb_sync.LAST_FETCH_COMPLETE is True
    assert kb_sync.LAST_FETCH_FAILURES == []


# ---------------------------------------------------------------------
# 5. The alert says what actually happened
#
# The first real alert read: "anything new or edited in a failed
# category is missing until the next clean run." That run added 47
# articles and updated 44, and no category failed. Two individual
# detail calls did. Meanwhile the consequence that mattered went
# unmentioned: the skipped delete left 17 rows pointing at articles Zoho
# no longer publishes, and those stay citable.
# ---------------------------------------------------------------------

def _set_failures(monkeypatch, failures):
    monkeypatch.setattr(kb_sync, "LAST_FETCH_FAILURES", list(failures))


PROD_STATS = {"added": 47, "updated": 44, "unchanged": 409,
              "removed": 0, "delete_skipped": True}


def test_article_failures_are_not_described_as_missing_content(monkeypatch):
    """Reproduces the real alert and pins the corrected wording."""
    _set_failures(monkeypatch, [
        {"kind": kb_sync.FAILURE_ARTICLE,
         "message": "FAQs & Knowledge Base: getArticle detail failed for 1"},
        {"kind": kb_sync.FAILURE_ARTICLE,
         "message": "FAQs & Knowledge Base: getArticle detail failed for 2"},
    ])

    alert = kb_sync.build_failure_alert(500, PROD_STATS)

    assert "new or edited" not in alert, (
        "no category failed, so nothing new or edited was missing"
    )
    assert "stale rather than missing" in alert
    assert "47 added" in alert and "44 updated" in alert


def test_every_alert_names_the_unpruned_rows(monkeypatch):
    """The consequence that is true on any incomplete run."""
    _set_failures(monkeypatch, [
        {"kind": kb_sync.FAILURE_ARTICLE, "message": "detail failed for 1"},
    ])

    alert = kb_sync.build_failure_alert(500, PROD_STATS)

    assert "delete step was skipped" in alert.lower()
    assert "can still cite them" in alert


def test_a_real_category_failure_does_say_content_is_missing(monkeypatch):
    _set_failures(monkeypatch, [
        {"kind": kb_sync.FAILURE_CATEGORY,
         "message": "FAQ et base de connaissances: getArticles failed"},
    ])

    alert = kb_sync.build_failure_alert(400, PROD_STATS)

    assert "1 category could not be listed" in alert
    assert "New and edited articles there are missing" in alert


def test_losing_the_category_list_says_the_run_indexed_nothing(monkeypatch):
    _set_failures(monkeypatch, [
        {"kind": kb_sync.FAILURE_CATEGORY_LIST,
         "message": "category list failed: 500"},
    ])

    alert = kb_sync.build_failure_alert(0, {})

    assert "indexed nothing" in alert
    assert "last good run" in alert


def test_the_failure_list_is_capped(monkeypatch):
    _set_failures(monkeypatch, [
        {"kind": kb_sync.FAILURE_ARTICLE, "message": f"detail failed for {i}"}
        for i in range(40)
    ])

    alert = kb_sync.build_failure_alert(500, PROD_STATS)

    assert "...and 25 more" in alert


def test_no_failures_posts_nothing(monkeypatch):
    _set_failures(monkeypatch, [])
    posted = []
    monkeypatch.setattr(
        kb_sync, "build_failure_alert",
        lambda *a, **k: posted.append(1) or "x",
    )

    kb_sync._alert_fetch_failures(500, PROD_STATS)

    assert posted == []
