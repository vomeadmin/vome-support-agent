"""Tests for the self-learning pipeline.

The pipeline existed before this: ticket_analyzer mined closed tickets
into `knowledge_sections` and nothing ever read the table, while the one
consumer (intake.py) read a markdown file from an ephemeral dyno disk at
import time. These tests pin the two halves of the fix:

  * knowledge.py reads sections from Postgres at request time, caches
    them briefly, and degrades to the untouched prompt on any failure.
  * clickup_knowledge.py mines only finished tasks, and only ones with
    enough substance to be worth a Claude call.

No network, no database.
"""
import os

for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import clickup_knowledge  # noqa: E402
import knowledge  # noqa: E402


def _section(key, title, content, count=100):
    return {
        "section_key": key,
        "title": title,
        "content": content,
        "version": 1,
        "ticket_count": count,
        "updated_at": "2026-09-01T00:00:00",
    }


def _stub_sections(monkeypatch, sections, calls=None):
    import database

    def _get(section_keys=None):
        if calls is not None:
            calls.append(section_keys)
        if section_keys is None:
            return sections
        return [s for s in sections if s["section_key"] in section_keys]

    monkeypatch.setattr(database, "get_current_knowledge_sections", _get)
    knowledge.clear_cache()


# =====================================================================
# knowledge.py: the read side
# =====================================================================

def test_no_learned_sections_leaves_the_prompt_untouched(monkeypatch):
    _stub_sections(monkeypatch, [])
    base = "BASE PROMPT"
    assert knowledge.augment_system_prompt(base) == base


def test_voice_guide_is_appended_when_it_exists(monkeypatch):
    _stub_sections(monkeypatch, [
        _section("sams_voice", "Sam's Voice", "Sam opens with 'Hey there'."),
    ])

    out = knowledge.augment_system_prompt("BASE PROMPT")

    assert out.startswith("BASE PROMPT")
    assert "WHAT THE TEAM KNOWS" in out
    assert "*(learned)*" in out
    assert "Hey there" in out


def test_engineering_section_is_appended_and_marked_internal(monkeypatch):
    _stub_sections(monkeypatch, [
        _section(
            "engineering_resolutions",
            "How These Issues Get Resolved",
            "Shifts show an hour early when timezones disagree.",
        ),
    ])

    out = knowledge.build_knowledge_block()

    assert "timezones disagree" in out
    assert "Never quote internal engineering" in out
    assert "never promise a fix or a date" in out.lower()


def test_category_section_only_loads_when_the_caller_knows_it(monkeypatch):
    calls = []
    _stub_sections(monkeypatch, [
        _section("sams_voice", "Sam's Voice", "voice content"),
        _section("bug", "Bug Reports", "bug content"),
    ], calls=calls)

    without = knowledge.build_knowledge_block()
    assert "bug content" not in without
    assert "bug" not in calls[0]

    knowledge.clear_cache()
    with_cat = knowledge.build_knowledge_block(category="bug")
    assert "bug content" in with_cat
    assert "BUG REPORTS" in with_cat


def test_sections_are_truncated_to_their_budget(monkeypatch):
    huge = "para\n\n" * 5000
    _stub_sections(monkeypatch, [_section("sams_voice", "Sam's Voice", huge)])

    out = knowledge.build_knowledge_block()

    assert "[section truncated]" in out
    assert len(out) < knowledge.VOICE_MAX_CHARS + 2000


def test_sections_are_cached_between_calls(monkeypatch):
    calls = []
    _stub_sections(monkeypatch, [
        _section("sams_voice", "Sam's Voice", "voice"),
    ], calls=calls)

    knowledge.build_knowledge_block()
    knowledge.build_knowledge_block()
    knowledge.build_knowledge_block()

    assert len(calls) == 1, "every draft must not re-query Postgres"


def test_clear_cache_forces_a_reload(monkeypatch):
    """The weekly refresh calls this so a new book goes live at once."""
    calls = []
    _stub_sections(monkeypatch, [
        _section("sams_voice", "Sam's Voice", "voice"),
    ], calls=calls)

    knowledge.build_knowledge_block()
    knowledge.clear_cache()
    knowledge.build_knowledge_block()

    assert len(calls) == 2


def test_a_database_failure_degrades_to_the_base_prompt(monkeypatch):
    import database

    def _boom(section_keys=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(database, "get_current_knowledge_sections", _boom)
    knowledge.clear_cache()

    assert knowledge.augment_system_prompt("BASE") == "BASE"


# =====================================================================
# clickup_knowledge.py: the mining side
# =====================================================================

def test_only_finished_tasks_are_mined(monkeypatch):
    """Mining an in-progress task would learn a resolution that has not
    happened yet."""
    tasks = [
        {"id": "1", "name": "Closed one", "description": "d",
         "status": {"status": "Closed"}, "custom_fields": []},
        {"id": "2", "name": "Still queued", "description": "d",
         "status": {"status": "queued"}, "custom_fields": []},
        {"id": "3", "name": "In progress", "description": "d",
         "status": {"status": "in progress"}, "custom_fields": []},
        {"id": "4", "name": "Taught the client", "description": "d",
         "status": {"status": "user education"}, "custom_fields": []},
        {"id": "5", "name": "Turned down", "description": "d",
         "status": {"status": "declined"}, "custom_fields": []},
    ]

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"tasks": tasks, "last_page": True}

    monkeypatch.setattr(
        clickup_knowledge.httpx, "get", lambda *a, **k: _Resp()
    )
    monkeypatch.setattr(clickup_knowledge.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        clickup_knowledge, "SOURCE_LISTS", {"list1": "Priority Queue"}
    )

    got = clickup_knowledge.fetch_closed_tasks()

    assert sorted(t["task_id"] for t in got) == ["1", "4"]


_HEADER = (
    "**Account:** YMCA Canada | **Tier:** Pro | **ARR:** $1,260\n"
    "**Zoho ticket:** #7728\n"
    "**Zoho link:** https://desk.zoho.com/support/vomevolunteer/"
    "ShowHomePage.do#Cases/dv/569440000038630472\n"
    "\n---\n\n"
)


def test_generated_header_is_stripped_from_the_report():
    got = clickup_knowledge.strip_task_boilerplate(
        _HEADER + "Volunteers cannot register for the conference."
    )
    assert got == "Volunteers cannot register for the conference."


def test_a_description_with_no_header_is_left_alone():
    text = "Just a plain description with no generated header at all."
    assert clickup_knowledge.strip_task_boilerplate(text) == text


def test_thin_tasks_are_skipped_before_paying_for_a_claude_call():
    thin = {"description": "broken"}
    assert not clickup_knowledge.has_enough_substance(thin, [])
    assert not clickup_knowledge.has_enough_substance(thin, ["Sam: fixed"])


def test_a_header_only_task_is_not_mistaken_for_substance():
    """Every task carries ~250 characters of generated header. Measuring
    the raw description would call an empty task substantial."""
    assert not clickup_knowledge.has_enough_substance(
        {"description": _HEADER}, []
    )


def test_zoho_ticket_id_falls_back_to_the_description_link():
    task = {"custom_fields": [], "description": _HEADER + "report text"}
    assert (
        clickup_knowledge._extract_zoho_ticket_id(task)
        == "569440000038630472"
    )


def test_a_task_with_a_real_description_is_worth_mining():
    task = {"description": "x" * 120}
    assert clickup_knowledge.has_enough_substance(task, [])


def test_a_thin_task_with_a_real_discussion_is_worth_mining():
    task = {"description": "broken"}
    comments = ["Sanjay: " + "y" * 200]
    assert clickup_knowledge.has_enough_substance(task, comments)


def test_dropdown_custom_fields_resolve_to_their_label():
    task = {
        "custom_fields": [
            {
                "id": "field-1",
                "value": 1,
                "type_config": {
                    "options": [{"name": "Bug"}, {"name": "Scheduling"}]
                },
            }
        ]
    }
    assert clickup_knowledge._custom_field(task, "field-1") == "Scheduling"


def test_linked_zoho_ticket_id_is_extracted_from_a_url():
    task = {
        "custom_fields": [
            {
                "id": clickup_knowledge.FIELD_ZOHO_TICKET_LINK,
                "value": (
                    "https://desk.zoho.com/support/vomevolunteer/"
                    "ShowHomePage.do#Cases/dv/569440000012345678"
                ),
            }
        ]
    }
    assert (
        clickup_knowledge._extract_zoho_ticket_id(task)
        == "569440000012345678"
    )


def test_no_linked_ticket_returns_empty_not_none():
    bare = {"custom_fields": [], "description": "no link here"}
    assert clickup_knowledge._extract_zoho_ticket_id(bare) == ""


# =====================================================================
# ClickUp comment parsing
#
# Every consumer filtered comment blocks on `type == "text"`. ClickUp's
# blocks carry no `type` key, so the filter matched nothing and the
# Command Center's engineer notes, the thread view and the ticket list's
# engineer comment all silently saw zero comments.
# =====================================================================

def test_comment_text_is_read_from_the_plain_field():
    from clickup_tasks import extract_comment_text

    comment = {
        "comment_text": "Account is active, send them to Forgot Password.",
        "comment": [
            {"text": "Account is active, send them to Forgot Password.",
             "attributes": {}},
        ],
    }
    assert extract_comment_text(comment).startswith("Account is active")


def test_blocks_without_a_type_key_are_still_read():
    """The regression: this is the real shape ClickUp returns."""
    from clickup_tasks import extract_comment_text

    comment = {
        "comment": [
            {"text": "Fixed in ", "attributes": {}},
            {"text": "the scheduler.", "attributes": {}},
        ],
    }
    assert extract_comment_text(comment) == "Fixed in the scheduler."


def test_an_empty_comment_returns_an_empty_string():
    from clickup_tasks import extract_comment_text

    assert extract_comment_text({}) == ""
    assert extract_comment_text({"comment": []}) == ""
    assert extract_comment_text(None) == ""


def test_analysis_json_survives_markdown_fences(monkeypatch):
    class _Block:
        text = '```json\n{"category": "bug", "module": "scheduling"}\n```'

    class _Resp:
        content = [_Block()]

    monkeypatch.setattr(
        clickup_knowledge._client.messages, "create", lambda **k: _Resp()
    )

    got = clickup_knowledge.analyze_task(
        {"name": "n", "list_name": "l", "status": "closed",
         "description": "d"},
        ["Sam: notes"],
    )

    assert got == {"category": "bug", "module": "scheduling"}


def test_unparseable_analysis_returns_none_instead_of_raising(monkeypatch):
    class _Block:
        text = "I could not analyse this task."

    class _Resp:
        content = [_Block()]

    monkeypatch.setattr(
        clickup_knowledge._client.messages, "create", lambda **k: _Resp()
    )

    assert clickup_knowledge.analyze_task({"name": "n"}, []) is None
