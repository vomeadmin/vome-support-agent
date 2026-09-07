"""
knowledge.py

Read side of the self-learning pipeline.

ticket_analyzer.py has always mined closed tickets into
`knowledge_sections`, and clickup_knowledge.py now does the same for
closed ClickUp tasks. Nothing ever read either one. The only consumer was
intake.py, which loaded knowledge_book/sams_voice.md from local disk at
import time, and that path never worked in production for two reasons:

  * the dyno filesystem is ephemeral, so a generated file is wiped on the
    next restart or deploy, and
  * a prompt assembled at import time cannot pick up a regeneration
    without a restart anyway.

So this module reads from Postgres, at request time, behind a short TTL
cache. Regenerate the knowledge book and every surface picks it up within
the cache window, no deploy and no restart.

Usage from any drafting surface:

    system = knowledge.augment_system_prompt(SYSTEM_PROMPT)

Everything degrades to the unmodified prompt: no database, no sections,
or a query failure all return the base prompt unchanged.
"""

import threading
import time

# The cross-cutting style guide. Synthesised from every analysed ticket,
# so it is the one section worth carrying on every single draft.
VOICE_SECTION_KEY = "sams_voice"

# What the resolution patterns from closed ClickUp tasks are filed under.
ENGINEERING_SECTION_KEY = "engineering_resolutions"

# Character budgets. These are prompt real estate on every draft, so they
# are deliberately tight. The voice guide is the priority; a category
# section is a bonus when the caller knows the category.
VOICE_MAX_CHARS = 9000
CATEGORY_MAX_CHARS = 7000

# How long a loaded section stays cached in-process. Long enough that a
# busy hour is a handful of queries, short enough that a regeneration
# goes live the same day without a restart.
CACHE_TTL_SECONDS = 900

_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


def _load_sections(section_keys: list[str]) -> list[dict]:
    """Fetch sections from Postgres, cached for CACHE_TTL_SECONDS."""
    cache_key = "|".join(sorted(section_keys))
    now = time.time()

    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]

    try:
        from database import get_current_knowledge_sections

        sections = get_current_knowledge_sections(section_keys)
    except Exception as e:
        print(f"[KNOWLEDGE] section load failed: {e}")
        sections = []

    with _cache_lock:
        _cache[cache_key] = (now, sections)
    return sections


def clear_cache() -> None:
    """Drop the cache. Called right after a regeneration."""
    with _cache_lock:
        _cache.clear()


def _truncate(content: str, limit: int) -> str:
    content = (content or "").strip()
    if len(content) <= limit:
        return content
    # Cut on a paragraph break so a section never ends mid-sentence.
    cut = content[:limit]
    boundary = cut.rfind("\n\n")
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip() + "\n\n[section truncated]"


def build_knowledge_block(category: str | None = None) -> str:
    """Render the learned-knowledge prompt block. "" when nothing exists.

    Always includes the voice guide. Adds the section for `category` when
    the caller already knows it, which the Command Center does (the
    classification is on the ticket row) and the webhook path does not
    (classification is an output of the very call being built).
    """
    wanted = [VOICE_SECTION_KEY, ENGINEERING_SECTION_KEY]
    if category:
        wanted.append(category.strip().lower())

    sections = _load_sections(wanted)
    if not sections:
        return ""

    by_key = {s["section_key"]: s for s in sections}
    parts: list[str] = []

    voice = by_key.get(VOICE_SECTION_KEY)
    if voice:
        parts.append(
            "## SAM'S VOICE (learned from "
            f"{voice['ticket_count']} real closed tickets)\n\n"
            "This is how Sam actually writes to clients. Mirror the tone, "
            "the sentence length and the specific phrasing. It is a style "
            "guide, not a script: never paste a phrase that does not fit "
            "the situation in front of you.\n\n"
            + _truncate(voice["content"], VOICE_MAX_CHARS)
        )

    eng = by_key.get(ENGINEERING_SECTION_KEY)
    if eng:
        parts.append(
            "## HOW THESE ISSUES GET RESOLVED (learned from "
            f"{eng['ticket_count']} closed engineering tasks)\n\n"
            "Patterns from work the team has already shipped: what the "
            "underlying cause usually turns out to be, and what the client "
            "was told. Use it to recognise a familiar issue and to set "
            "accurate expectations. Never quote internal engineering "
            "detail to a client, and never promise a fix or a date from "
            "this alone.\n\n"
            + _truncate(eng["content"], CATEGORY_MAX_CHARS)
        )

    if category:
        cat = by_key.get(category.strip().lower())
        if cat and cat["section_key"] not in (
            VOICE_SECTION_KEY, ENGINEERING_SECTION_KEY
        ):
            parts.append(
                f"## {cat['title'].upper()} (learned from "
                f"{cat['ticket_count']} closed tickets in this category)\n\n"
                + _truncate(cat["content"], CATEGORY_MAX_CHARS)
            )

    if not parts:
        return ""

    header = (
        "\n\n---\n\n"
        "# LEARNED FROM CLOSED WORK\n\n"
        "The sections below were generated from real closed Vome tickets "
        "and engineering tasks. They describe what has actually worked. "
        "They do not override the rules above, and they are not a source "
        "of product facts: for how a feature works, use the help center "
        "articles and the feature catalog.\n\n"
    )
    return header + "\n\n---\n\n".join(parts)


def augment_system_prompt(
    base_prompt: str, category: str | None = None
) -> str:
    """Append the learned-knowledge block to a system prompt.

    Call this at request time, never at import time. Returns `base_prompt`
    unchanged when there is nothing learned yet.
    """
    try:
        block = build_knowledge_block(category)
    except Exception as e:
        print(f"[KNOWLEDGE] augment_system_prompt failed: {e}")
        return base_prompt
    return base_prompt + block if block else base_prompt
