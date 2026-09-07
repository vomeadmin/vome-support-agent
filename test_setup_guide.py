"""Tests for bringing the in-app Setup Guide into Vic's knowledge.

The guide's substance (7 stages, 45 sections, the decisions each settles
and 112 tips) is hardcoded in vome-react, not in Zoho, so none of it
reached the agent. scripts/sync_setup_guide.py extracts it and
setup_guide.py condenses it into a knowledge section.

These cover the extraction rules that were wrong on the first pass and
the fallbacks that keep the section building when the index is behind.
"""
import os
import sys
from pathlib import Path

for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

sys.path.insert(0, str(Path(__file__).parent / "scripts"))

import setup_guide  # noqa: E402
import sync_setup_guide as sync  # noqa: E402


# =====================================================================
# Extraction (scripts/sync_setup_guide.py)
# =====================================================================

SAMPLE_JS = """
export const STAGE = { ORIENTATION: 'orientation', RECRUIT: 'recruit' };

const orientationSections = [
  {
    key: 'fundamentals',
    titleKey: 'sg_fundamentals_title',
    introKey: 'sg_fundamentals_intro',
    articles: [
      { slug: 'how-vome-is-structured', labelKey: 'sg_article_structure' },
      { slug: 'how-does-vome-support-work', labelKey: 'sg_article_support' },
    ],
    blocks: [
      {
        type: 'tip',
        titleKey: 'sg_support_account_title',
        textKey: 'sg_support_account_text',
      },
      { type: 'hierarchy' },
      {
        type: 'decision',
        textKey: 'sg_categories_text',
      },
    ],
  },
  {
    key: 'plan',
    titleKey: 'sg_plan_title',
    articles: [],
    blocks: [],
  },
];

const recruitSections = [
  {
    key: 'forms',
    titleKey: 'sg_forms_title',
    articles: [{ slug: 'setting-up-forms', labelKey: 'sg_article_forms' }],
    blocks: [],
  },
];

export const SETUP_GUIDE_STAGES = [
  {
    key: STAGE.ORIENTATION,
    labelKey: 'sg_stage_orientation',
    sections: orientationSections,
  },
  {
    key: STAGE.RECRUIT,
    labelKey: 'sg_stage_recruit',
    sections: recruitSections,
  },
];
"""

SAMPLE_STRINGS = {
    "sg_stage_orientation": "Orientation",
    "sg_stage_recruit": "Recruit",
    "sg_fundamentals_title": "How Vome is structured",
    "sg_fundamentals_intro": "Five words carry the whole platform here.",
    "sg_article_structure": "How Vome is structured",
    "sg_article_support": "Mastering the Vome fundamentals",
    "sg_support_account_title": "Create your support account first",
    "sg_support_account_text": (
        "Your Vome support account is separate from the credentials you "
        "use to sign in here, so make it before you are stuck."
    ),
    "sg_categories_text": (
        "Categories are folders. They give your account its shape and "
        "nobody is ever assigned to one."
    ),
    "sg_plan_title": "What your plan covers",
    "sg_forms_title": "Setting up forms",
}


def test_stages_are_parsed_in_order():
    stages = sync.parse_stages(SAMPLE_JS)
    assert [s["key"] for s in stages] == ["orientation", "recruit"]
    assert stages[0]["sections_var"] == "orientationSections"


def test_sections_and_article_slugs_are_extracted():
    block = sync._sections_block(SAMPLE_JS, "orientationSections")
    sections = sync.parse_sections(block)

    assert [s["key"] for s in sections] == ["fundamentals", "plan"]
    assert sections[0]["articles"] == [
        "how-vome-is-structured", "how-does-vome-support-work",
    ]


def test_article_link_labels_are_not_treated_as_guidance():
    """First pass swept labelKey into the prose bucket, so link text like
    'Mastering the Vome fundamentals' read as setup advice."""
    block = sync._sections_block(SAMPLE_JS, "orientationSections")
    section = sync.parse_sections(block)[0]

    assert "sg_article_structure" not in section["question_keys"]
    assert "sg_article_support" not in section["question_keys"]


def test_tip_text_is_not_duplicated_into_the_prose_bucket():
    block = sync._sections_block(SAMPLE_JS, "orientationSections")
    section = sync.parse_sections(block)[0]

    assert "sg_support_account_text" in section["block_keys"]
    assert "sg_support_account_text" not in section["question_keys"]


def test_untyped_structural_blocks_contribute_nothing():
    block = sync._sections_block(SAMPLE_JS, "orientationSections")
    section = sync.parse_sections(block)[0]
    # `{ type: 'hierarchy' }` is a diagram, not advice.
    assert all("hierarchy" not in k for k in section["block_keys"])


def test_english_wins_when_a_key_appears_twice(tmp_path, monkeypatch):
    """postlogin.js holds English first and French later in one file."""
    f = tmp_path / "postlogin.js"
    f.write_text(
        '        sg_stage_orientation: "Orientation",\n'
        '        sg_stage_orientation: "Orientation FR",\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sync, "TRANSLATIONS", f)

    assert sync.load_english_strings()["sg_stage_orientation"] == "Orientation"


def test_a_missing_stage_array_raises_rather_than_shipping_empty(monkeypatch):
    assert sync.parse_stages("const nothing = 1;") == []


def test_short_strings_are_dropped_as_ui_chrome(tmp_path, monkeypatch):
    """Button labels are not guidance."""
    content = tmp_path / "setupGuideContent.js"
    content.write_text(SAMPLE_JS, encoding="utf-8")
    strings = dict(SAMPLE_STRINGS)
    strings["sg_categories_text"] = "Next"

    monkeypatch.setattr(sync, "CONTENT_JS", content)
    monkeypatch.setattr(sync, "load_english_strings", lambda: strings)
    monkeypatch.setattr(
        sync, "REACT_ROOT", content.parent.parent.parent.parent
    )

    data = sync.build()
    points = data["stages"][0]["sections"][0]["prompts"]
    assert "Next" not in points


def test_build_produces_the_expected_shape(tmp_path, monkeypatch):
    content = tmp_path / "setupGuideContent.js"
    content.write_text(SAMPLE_JS, encoding="utf-8")
    monkeypatch.setattr(sync, "CONTENT_JS", content)
    monkeypatch.setattr(sync, "load_english_strings", lambda: SAMPLE_STRINGS)
    monkeypatch.setattr(
        sync, "REACT_ROOT", content.parent.parent.parent.parent
    )

    data = sync.build()

    assert [s["label"] for s in data["stages"]] == ["Orientation", "Recruit"]
    assert data["all_article_slugs"] == [
        "how-does-vome-support-work",
        "how-vome-is-structured",
        "setting-up-forms",
    ]
    first = data["stages"][0]["sections"][0]
    assert first["title"] == "How Vome is structured"
    assert "Five words" in first["intro"]
    assert any("Categories are folders" in p for p in first["prompts"])
    assert any("separate from the credentials" in a for a in first["advice"])


# =====================================================================
# Section building (setup_guide.py)
# =====================================================================

SOURCE = {
    "generated": "2026-09-07",
    "stages": [
        {
            "key": "orientation",
            "label": "Orientation",
            "sections": [
                {
                    "key": "fundamentals",
                    "title": "How Vome is structured",
                    "intro": "Five words carry the platform.",
                    "articles": ["how-vome-is-structured"],
                    "advice": ["Create your support account first."],
                    "prompts": ["Categories are folders."],
                }
            ],
        }
    ],
    "all_article_slugs": ["how-vome-is-structured"],
}


def test_a_missing_source_digest_is_reported_not_crashed(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_guide, "SOURCE_JSON", tmp_path / "nope.json")
    assert setup_guide.load_source() is None
    assert setup_guide.generate_setup_guide_section() == 0


def test_urls_fall_back_to_the_canonical_form(monkeypatch):
    """A freshly published article the index has not caught up with must
    still get a working link rather than being dropped."""
    monkeypatch.setattr(
        setup_guide, "get_kb_articles_by_permalink_prefix", lambda *a, **k: []
    )

    urls = setup_guide.resolve_article_urls(["brand-new-article"])

    assert urls["brand-new-article"] == (
        "https://support.vomevolunteer.com/portal/en/kb/articles/"
        "brand-new-article"
    )


def test_indexed_urls_are_preferred_over_the_canonical_form(monkeypatch):
    monkeypatch.setattr(
        setup_guide, "get_kb_articles_by_permalink_prefix",
        lambda *a, **k: [
            {"permalink": "how-vome-is-structured",
             "url": "https://support.vomevolunteer.com/real/url"},
        ],
    )

    urls = setup_guide.resolve_article_urls(["how-vome-is-structured"])

    assert urls["how-vome-is-structured"].endswith("/real/url")


def test_a_db_failure_still_yields_links(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(
        setup_guide, "get_kb_articles_by_permalink_prefix", _boom
    )

    urls = setup_guide.resolve_article_urls(["some-article"])

    assert urls["some-article"].endswith("/some-article")


def test_stage_blocks_carry_the_ordering_and_the_urls():
    urls = {"how-vome-is-structured": "https://example.test/a"}
    blocks = setup_guide.build_stage_blocks(SOURCE, urls)

    assert len(blocks) == 1
    assert "STAGE 1 of 7: Orientation" in blocks[0]
    assert "Categories are folders." in blocks[0]
    assert "TIP: Create your support account first." in blocks[0]
    assert "https://example.test/a" in blocks[0]


def test_the_section_is_saved_with_a_coverage_header(monkeypatch):
    saved = {}

    def _save(key, title, content, count):
        saved.update(
            key=key, title=title, content=content, count=count
        )

    monkeypatch.setattr(setup_guide, "load_source", lambda: SOURCE)
    monkeypatch.setattr(
        setup_guide, "resolve_article_urls", lambda slugs: {}
    )
    monkeypatch.setattr(
        setup_guide, "build_section_content", lambda blocks: "THE SECTION"
    )
    import ticket_analyzer
    monkeypatch.setattr(ticket_analyzer, "_save_knowledge_section", _save)

    count = setup_guide.generate_setup_guide_section()

    assert count == 1
    assert saved["key"] == "setup_guide"
    assert "THE SECTION" in saved["content"]
    assert "1 stages" in saved["content"] or "1 sections" in saved["content"]


def test_a_failed_synthesis_does_not_overwrite_the_live_section(monkeypatch):
    saved = []
    monkeypatch.setattr(setup_guide, "load_source", lambda: SOURCE)
    monkeypatch.setattr(setup_guide, "resolve_article_urls", lambda s: {})
    monkeypatch.setattr(setup_guide, "build_section_content", lambda b: None)
    import ticket_analyzer
    monkeypatch.setattr(
        ticket_analyzer, "_save_knowledge_section",
        lambda *a: saved.append(a),
    )

    assert setup_guide.generate_setup_guide_section() == 0
    assert saved == [], "a failed run must leave the previous section alone"


# =====================================================================
# The reader treats it as authoritative, not as learned experience
# =====================================================================

def test_setup_guide_is_labelled_published_not_learned(monkeypatch):
    import database
    import knowledge

    monkeypatch.setattr(
        database, "get_current_knowledge_sections",
        lambda section_keys=None: [{
            "section_key": "setup_guide",
            "title": "Setting up Vome",
            "content": "THE PATH",
            "version": 1,
            "ticket_count": 45,
            "updated_at": "2026-09-07T00:00:00",
        }],
    )
    knowledge.clear_cache()

    block = knowledge.build_knowledge_block()

    assert "*(published)*" in block
    assert "authoritative product guidance" in block
    # The header must scope its "not a source of product facts" caveat to
    # the *learned* sections. An earlier version applied it to everything,
    # which told the model to discount the one section it should trust.
    assert "Sections marked *published*" in block
    assert "They are authoritative." in block
    learned_caveat = "never as a source of product facts"
    assert learned_caveat in block
    assert block.index("Sections marked *learned*") < block.index(
        learned_caveat
    ) < block.index("Sections marked *published*")


def test_setup_guide_is_requested_on_every_prompt():
    import knowledge
    assert knowledge.SETUP_SECTION_KEY == "setup_guide"
    assert setup_guide.SECTION_KEY == knowledge.SETUP_SECTION_KEY


# =====================================================================
# The endpoint must not block the web process
#
# It first shipped running the synthesis inline. In production that held
# the only uvicorn worker for 297 seconds, and a status request from
# another client timed out during the window. On a support agent that is
# queued Zoho ticket webhooks and Slack events.
# =====================================================================

def test_every_long_endpoint_runs_in_a_thread():
    import inspect
    import os as _os

    _os.environ.setdefault("DATABASE_URL", "")
    import main

    long_endpoints = [
        "kb_sync_run",
        "knowledge_book_refresh",
        "knowledge_book_clickup_scan",
        "knowledge_book_setup_guide",
        "run_knowledge_book",
    ]
    blocking = [
        name for name in long_endpoints
        if "threading.Thread" not in inspect.getsource(getattr(main, name))
    ]
    assert not blocking, f"these block the worker: {blocking}"


def test_regenerate_is_available_and_threads():
    """The book-only path. Three deploys in a row killed a refresh mid
    mining, and each time the regeneration was lost because it only runs
    as the last step of a ninety minute job."""
    import inspect
    import os as _os

    _os.environ.setdefault("DATABASE_URL", "")
    import main

    src = inspect.getsource(main.knowledge_book_regenerate)
    assert "threading.Thread" in src
    assert "generate_knowledge_book" in src
    # It must not mine. That is the whole point of the split.
    assert "run_full_analysis" not in src
    assert "run_clickup_knowledge_scan" not in src


def test_regenerate_refuses_to_race_a_running_refresh():
    import inspect
    import main

    src = inspect.getsource(main.knowledge_book_regenerate)
    assert "_analysis_running" in src
    assert "refresh_in_progress" in src
