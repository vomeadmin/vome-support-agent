"""
kb_context.py

Shared help-center retrieval for surfaces that draft a reply.

intake.py has had KB grounding since the widget shipped, but agent.py,
which writes the ticket reply drafts, had none: its only grounding was
system_prompt.md, response_templates.md and the landing-page feature
catalog. So a newly published article improved widget deflection and
never improved a drafted reply. This module closes that gap.

The retrieval path is the same Postgres FTS index the widget uses
(kb_articles, populated nightly by kb_sync.py). The framing is not: a
widget answer is a live conversation with the user, a drafted reply is
something Sam reviews and sends, so the block below tells Claude to
ground the draft and cite the article rather than to walk someone
through it in chat.

Import this lazily from agent.py. kb_sync imports agent for the Zoho
MCP helpers, so a module-level import here would close the cycle.
"""

# Characters of article body handed to the model per article. Three
# articles at this size is roughly 2k tokens, which is affordable next to
# a full ticket thread and enough for Claude to quote real steps.
BODY_CHARS = 2500

# Articles per draft. Two is usually the real answer plus a near miss;
# three catches the case where the answer is split across articles.
MAX_ARTICLES = 3

# Above this, an article gets an explicit "verify before promising"
# caveat. Most Vome articles stay accurate for years, so this is
# deliberately generous.
STRONG_CAVEAT_DAYS = 730
SOFT_CAVEAT_DAYS = 365

# Query length fed to Postgres FTS. Long queries dilute ts_rank_cd
# without adding recall, since the match-count floor is only 2 lexemes.
MAX_QUERY_CHARS = 400


def _to_locale(detected_lang: str | None) -> str | None:
    """Map agent.py's `_detect_language` output onto a kb_articles locale.

    `_detect_language` returns "French" or None. None means "not detected
    as French", which is not the same as "definitely English", so it maps
    to None (search both languages) rather than to "en".
    """
    if not detected_lang:
        return None
    return "fr" if detected_lang.strip().lower().startswith("fr") else None


def build_search_query(
    subject: str = "",
    latest_message: str = "",
    body: str = "",
) -> str:
    """Build the FTS query for a ticket.

    The latest client message is weighted first because a thread evolves:
    the subject is often the original complaint and the live question is
    whatever they last asked. The original body is only a fallback for a
    ticket with no reply yet.
    """
    parts = [(subject or "").strip()]
    tail = (latest_message or "").strip() or (body or "").strip()
    if tail:
        parts.append(tail)
    query = " ".join(p for p in parts if p)
    return query[:MAX_QUERY_CHARS].strip()


def search_articles(query: str, locale: str | None = None) -> list[dict]:
    """Return ranked published articles for `query`, or [] on any failure.

    Never raises. A KB outage must degrade the draft, not block it.
    """
    if not query:
        return []
    try:
        from kb_sync import search_kb_articles

        return search_kb_articles(
            query,
            n_results=MAX_ARTICLES,
            language=locale,
            body_chars=BODY_CHARS,
        )
    except Exception as e:
        print(f"[KB CONTEXT] article search failed: {e}")
        return []


def _freshness_note(days: int | None) -> str:
    if days is None:
        return ""
    if days > STRONG_CAVEAT_DAYS:
        return (
            "  (published over two years ago: check the steps still match "
            "the product before promising them, and say so if unsure)"
        )
    if days > SOFT_CAVEAT_DAYS:
        return "  (over a year old: worth a sanity check)"
    return ""


def format_articles_block(articles: list[dict]) -> str:
    """Render articles as a prompt block. Empty string if there are none."""
    if not articles:
        return ""

    lines = [
        "",
        "HELP CENTER ARTICLES (retrieved from the live Vome knowledge "
        "base, ranked by relevance to this ticket):",
        "These are real published articles. Use them like this:",
        "  - If one actually answers the ticket, base the draft on it and "
        "include its URL so the client can follow along.",
        "  - Paraphrase in Sam's voice. Do not paste the article verbatim "
        "and do not invent steps the article does not contain.",
        "  - If none of them address what the client is asking, ignore "
        "them completely. Do not cite an article that is only loosely "
        "related, and never link one just to have a link in the reply.",
        "  - Retrieval is keyword based, so a listed article is a "
        "candidate, not a verified answer. Judge it on its content.",
    ]

    for i, art in enumerate(articles, start=1):
        days = art.get("days_stale")
        days_str = "unknown" if days is None else f"{days} days ago"
        lines.append("")
        lines.append(f"--- Article {i}: {art.get('title', '')}")
        lines.append(f"URL: {art.get('url', '')}")
        lines.append(f"Last updated: {days_str}{_freshness_note(days)}")
        body = (art.get("body") or "").strip()
        if body:
            lines.append("Content:")
            lines.append(body)
        else:
            lines.append("Content: (unavailable, title-only match)")

    lines.append("")
    return "\n".join(lines)


def build_ticket_kb_block(
    subject: str = "",
    latest_message: str = "",
    body: str = "",
    detected_lang: str | None = None,
) -> str:
    """One call for agent.py: search the KB for a ticket and render it.

    Returns "" when there is no query or no match, so the caller can
    concatenate it unconditionally.
    """
    query = build_search_query(
        subject=subject, latest_message=latest_message, body=body
    )
    if not query:
        return ""

    locale = _to_locale(detected_lang)
    articles = search_articles(query, locale=locale)

    # A French ticket with no French article still deserves an answer:
    # most of the knowledge base is English, and Claude drafts in French
    # from English source material perfectly well.
    if not articles and locale == "fr":
        articles = search_articles(query, locale=None)

    if not articles:
        print(f"[KB CONTEXT] no article match for: {query[:80]}")
        return ""

    titles = ", ".join(a.get("title", "") for a in articles)
    print(f"[KB CONTEXT] {len(articles)} article(s) matched: {titles[:160]}")
    return format_articles_block(articles)
