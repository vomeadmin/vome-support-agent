"""
kb_sync.py

Nightly sync of Zoho Desk KB articles into Postgres (kb_articles table).

This script:
1. Fetches all KB categories from Zoho Desk
2. Fetches all articles in each category
3. UPSERTs article content into Postgres -- only changed rows are
   touched (compared by Zoho's modifiedTime).
4. Deletes any rows whose article was removed from Zoho, but ONLY when
   the fetch completed cleanly (see LAST_FETCH_COMPLETE below).

Only Published articles are indexed. Drafts and unpublished articles
live at portal URLs that 404 for clients, so quoting them would send a
customer to a dead link.

Deletion safety: the delete step removes every row whose id was not seen
during the fetch. If a category errors mid-run, or an article's detail
call fails, those ids are missing from the seen set and a blind delete
would silently wipe them from the agent's knowledge. So the fetch tracks
its own completeness and the delete is skipped whenever anything failed.

At runtime, intake.py queries kb_articles via Postgres FTS to find
relevant articles for the current conversation -- the full article
body is fed into the prompt so Claude can paraphrase real instructions
instead of guessing from titles.

Schedule: nightly via APScheduler (see main.py).
Manual: `python kb_sync.py` to sync, `python kb_sync.py --status` to
inspect index health.
"""

import re
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from agent import (
    _zoho_desk_call,
    _unwrap_mcp_result,
    ZOHO_ORG_ID,
)
from database import (
    upsert_kb_article,
    delete_missing_kb_articles,
    search_kb_articles_db,
    kb_index_status,
)

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# Rate limiting
ZOHO_DELAY = 1.5  # seconds between API calls

# Only these Zoho article statuses are indexed. Zoho also returns
# "Draft" and "Unpublished" from getArticles; both are invisible on the
# public help center, so an answer grounded in one would cite a URL the
# client cannot open.
PUBLISHED_STATUSES = {"published"}

# Attempts per getArticle detail call. The Zoho MCP proxy drops roughly
# one call in 500, and the same id succeeds immediately on retry. Without
# this a single blip marks the whole fetch incomplete, which blocks the
# delete step for that run and, if it happened most nights, would mean
# articles deleted in Zoho were never pruned from the index.
DETAIL_ATTEMPTS = 3


# =====================================================================
# Fetch articles from Zoho
# =====================================================================

LAST_FETCH_DEBUG: list[dict] = []

# True only when the last fetch reached every category and every article
# detail without a single failure. sync_articles_to_db refuses to run the
# delete step unless this is True, so a partial fetch can never be
# mistaken for "these articles no longer exist in Zoho".
# None means no fetch has run in this process yet, which is the normal
# state of a freshly restarted dyno between nightly syncs. False means a
# fetch ran and something failed. Keeping those distinct matters: a bare
# False on /kb-sync/status reads as "the sync is broken" when it usually
# just means "nothing has run since the last deploy".
LAST_FETCH_COMPLETE: bool | None = None

# Failures from the last fetch, surfaced in Slack and in
# /kb-sync/status. Each is {"kind": ..., "message": ...}. The kind is
# what lets the alert state the real consequence: losing a whole
# category is a different problem from one article's detail call
# timing out, and the old alert described neither correctly.
LAST_FETCH_FAILURES: list[dict] = []

# Could not enumerate categories at all, so the run indexed nothing.
FAILURE_CATEGORY_LIST = "category_list"
# One category's article listing failed, so every article in it is
# absent from this run.
FAILURE_CATEGORY = "category"
# One article's detail call failed. Its existing row is untouched, so it
# is stale rather than missing.
FAILURE_ARTICLE = "article"

# Tokens that signal a category is French. Detected on lowercased
# category name. `detail.locale` from Zoho always returns "en" (it
# reflects the fetched translation, not the article's primary
# language), so we fall back to the category name.
_FRENCH_NAME_TOKENS = (
    "connaissances",
    "français",
    " et ",
    " de la ",
    " des ",
)


def _detect_language_from_category(category_name: str) -> str:
    name = (category_name or "").lower()
    for tok in _FRENCH_NAME_TOKENS:
        if tok in name:
            return "fr"
    return "en"


def _record_failure(kind: str, message: str) -> None:
    """Record one fetch failure, tagged with what it costs."""
    LAST_FETCH_FAILURES.append({"kind": kind, "message": message})
    print(f"[KB SYNC]   failure ({kind}): {message}")


def failure_messages() -> list[str]:
    """Just the human-readable text, for logs and older callers."""
    return [f.get("message", "") for f in LAST_FETCH_FAILURES]


def _extract_mcp_error(raw) -> str | None:
    """If the MCP response is an error wrapper, return its message text."""
    if not isinstance(raw, dict):
        return None
    if not raw.get("isError"):
        return None
    for block in raw.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            return (block.get("text", "") or "")[:500]
    return "isError=true with no text block"


def fetch_all_kb_articles() -> list[dict]:
    """Fetch all KB articles from all categories in Zoho Desk.

    Returns list of {id, title, content, permalink, category,
    modifiedTime, createdTime, language, status, url}.

    Only Published articles are returned (see PUBLISHED_STATUSES).

    Side effect: writes a per-category trace into the module-level
    LAST_FETCH_DEBUG so /kb-sync/status can surface what happened
    when a sync mysteriously returns 0 articles, and sets
    LAST_FETCH_COMPLETE / LAST_FETCH_FAILURES so the caller knows
    whether the delete step is safe to run.
    """
    global LAST_FETCH_DEBUG, LAST_FETCH_COMPLETE, LAST_FETCH_FAILURES
    LAST_FETCH_DEBUG = []
    LAST_FETCH_FAILURES = []
    LAST_FETCH_COMPLETE = False
    articles = []

    print("[KB SYNC] Fetching KB categories...")
    cat_result = _zoho_desk_call(
        "ZohoDesk_getAllKBRootCategories",
        {"query_params": {"orgId": str(ZOHO_ORG_ID)}},
    )
    cat_err = _extract_mcp_error(cat_result)
    if cat_err:
        print(f"[KB SYNC] Category fetch errored: {cat_err}")
        LAST_FETCH_DEBUG.append({"stage": "categories", "error": cat_err})
        _record_failure(
            FAILURE_CATEGORY_LIST, f"category list failed: {cat_err}"
        )
        return []

    raw_cats = _unwrap_mcp_result(cat_result)
    if not raw_cats:
        print("[KB SYNC] Failed to fetch categories")
        LAST_FETCH_DEBUG.append({
            "stage": "categories", "error": "empty response",
        })
        _record_failure(
            FAILURE_CATEGORY_LIST,
            "category list returned an empty response",
        )
        return []

    categories = []
    if isinstance(raw_cats, dict):
        categories = raw_cats.get("data", [])
    elif isinstance(raw_cats, list):
        categories = raw_cats

    print(f"[KB SYNC] Found {len(categories)} categories")
    LAST_FETCH_DEBUG.append({
        "stage": "categories", "count": len(categories),
    })

    for cat in categories:
        cat_id = cat.get("id")
        cat_name = cat.get("name", "Unknown")
        if not cat_id:
            continue

        print(f"[KB SYNC] Category: {cat_name} (ID: {cat_id})")

        # Zoho's getArticles caps limit at 50, so paginate via `from`.
        # `from` is a 1-indexed record number (must be > 0), not a
        # 0-based offset. categoryId must go in query_params (not
        # path_variables) for this MCP server.
        art_list: list[dict] = []
        page_from = 1
        page_size = 50
        max_pages = 20  # safety cap (1000 articles per category)
        errored = False
        for _ in range(max_pages):
            time.sleep(ZOHO_DELAY)
            page_result = _zoho_desk_call(
                "ZohoDesk_getArticles",
                {
                    "query_params": {
                        "orgId": str(ZOHO_ORG_ID),
                        "categoryId": str(cat_id),
                        "from": page_from,
                        "limit": page_size,
                    },
                },
            )
            page_err = _extract_mcp_error(page_result)
            if page_err:
                print(
                    f"[KB SYNC]   getArticles errored for "
                    f"{cat_name} @ from={page_from}: {page_err}"
                )
                LAST_FETCH_DEBUG.append({
                    "stage": "getArticles",
                    "category": cat_name,
                    "category_id": str(cat_id),
                    "from": page_from,
                    "error": page_err,
                })
                _record_failure(
                    FAILURE_CATEGORY,
                    f"{cat_name}: getArticles failed at from={page_from}: "
                    f"{page_err}",
                )
                errored = True
                break

            unwrapped = _unwrap_mcp_result(page_result)
            page_items: list = []
            if isinstance(unwrapped, dict):
                page_items = unwrapped.get("data", []) or []
            elif isinstance(unwrapped, list):
                page_items = unwrapped

            art_list.extend(page_items)
            if len(page_items) < page_size:
                break
            page_from += page_size

        if errored:
            # Do NOT fall through to the article loop. Leaving this
            # category out of the seen-ids set is what makes the delete
            # step dangerous, which is why LAST_FETCH_COMPLETE stays
            # False and the delete is skipped for the whole run.
            continue

        LAST_FETCH_DEBUG.append({
            "stage": "getArticles",
            "category": cat_name,
            "category_id": str(cat_id),
            "article_count": len(art_list),
        })

        skipped_unpublished = 0
        for art in art_list:
            article_id = str(art.get("id", ""))
            if not article_id:
                continue

            # Cheap pre-filter: the list payload already carries status,
            # so an unpublished article costs no detail call.
            if not _is_published(art.get("status")):
                skipped_unpublished += 1
                continue

            time.sleep(ZOHO_DELAY)
            detail = _fetch_article_detail(article_id)
            if not detail:
                _record_failure(
                    FAILURE_ARTICLE,
                    f"{cat_name}: getArticle detail failed for "
                    f"{article_id}",
                )
                continue

            # The detail payload is authoritative. An article unpublished
            # between the list call and this one still gets dropped.
            if not _is_published(detail.get("status")):
                skipped_unpublished += 1
                continue

            raw_content = detail.get("answer", "") or ""
            clean_content = re.sub(r"<[^>]+>", " ", raw_content)
            clean_content = re.sub(r"\s+", " ", clean_content).strip()

            title = detail.get("title", art.get("title", ""))
            permalink = detail.get("permalink", "")

            # detail.locale always returns "en" from Zoho (it's the
            # fetched translation's locale, not the article's primary
            # language). Infer from the category name instead.
            language = _detect_language_from_category(cat_name)

            articles.append({
                "id": article_id,
                "title": title,
                "content": clean_content,
                "permalink": permalink,
                "category": cat_name,
                "modifiedTime": (
                    detail.get("modifiedTime")
                    or art.get("modifiedTime", "")
                ),
                "createdTime": (
                    detail.get("createdTime")
                    or art.get("createdTime", "")
                ),
                "language": language,
                "status": detail.get("status", ""),
                "url": _build_article_url(permalink, article_id),
            })

        if skipped_unpublished:
            LAST_FETCH_DEBUG.append({
                "stage": "status_filter",
                "category": cat_name,
                "skipped_unpublished": skipped_unpublished,
            })
        print(
            f"[KB SYNC]   -> {len(art_list)} articles in {cat_name} "
            f"({skipped_unpublished} skipped as unpublished)"
        )

    LAST_FETCH_COMPLETE = not LAST_FETCH_FAILURES
    print(f"[KB SYNC] Total articles fetched: {len(articles)}")
    if not LAST_FETCH_COMPLETE:
        print(
            f"[KB SYNC] Fetch INCOMPLETE, "
            f"{len(LAST_FETCH_FAILURES)} failure(s). "
            f"Delete step will be skipped."
        )
    return articles


def _is_published(status: str | None) -> bool:
    """True if a Zoho article status means the article is publicly live."""
    return (status or "").strip().lower() in PUBLISHED_STATUSES


def _fetch_article_detail(
    article_id: str, attempts: int = DETAIL_ATTEMPTS
) -> dict | None:
    """Fetch full article content from Zoho Desk, retrying transient errors.

    The MCP server expects the path variable key to be `id`, NOT
    `articleId`. Using `articleId` returns the error "Mandatory path
    variable 'id' is not present in tool body" and the article body
    silently comes back empty.

    Retries matter more here than they look. A single failed detail call
    marks the whole fetch incomplete, which correctly blocks the delete
    step, but a run that never completes also never prunes an article
    deleted in Zoho. Observed failure rate is well under one percent and
    the same ids succeed immediately on retry, so a couple of attempts
    turns "clean run" back into the normal outcome.
    """
    for attempt in range(1, max(1, attempts) + 1):
        result = _zoho_desk_call(
            "ZohoDesk_getArticle",
            {
                "path_variables": {"id": str(article_id)},
                "query_params": {"orgId": str(ZOHO_ORG_ID)},
            },
        )
        if not (isinstance(result, dict) and result.get("isError")):
            raw = _unwrap_mcp_result(result)
            if isinstance(raw, dict) and "isError" not in raw:
                return raw

        if attempt < attempts:
            # Linear backoff. These failures look like proxy rate
            # limiting, so backing off beats hammering the same id.
            time.sleep(ZOHO_DELAY * attempt)
            print(
                f"[KB SYNC]   retrying getArticle {article_id} "
                f"(attempt {attempt + 1}/{attempts})"
            )
    return None


def _build_article_url(permalink: str, article_id: str) -> str:
    base = "https://support.vomevolunteer.com/portal/en/kb/articles"
    if permalink:
        return f"{base}/{permalink}"
    return f"{base}/{article_id}"


# =====================================================================
# Upsert into Postgres
# =====================================================================

def sync_articles_to_db(
    articles: list[dict],
    allow_delete: bool | None = None,
) -> dict:
    """UPSERT each article into kb_articles, then delete missing rows.

    `allow_delete` gates the delete step. Leave it None to inherit
    LAST_FETCH_COMPLETE from the fetch that produced `articles`, which is
    what every caller wants: the delete removes every row not seen during
    the fetch, so running it after a partial fetch wipes whole categories
    out of the agent's knowledge with no warning. Pass True only when you
    are certain `articles` is the complete Zoho set.

    Returns {added, updated, unchanged, removed, skipped,
    skipped_unpublished, delete_skipped}.
    """
    if allow_delete is None:
        # LAST_FETCH_COMPLETE is None when no fetch has run in this
        # process. Treat that like a failed fetch: never delete on the
        # word of a set of articles this process did not fetch itself.
        allow_delete = LAST_FETCH_COMPLETE is True

    stats = {
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "removed": 0,
        "skipped": 0,
        "skipped_unpublished": 0,
        "delete_skipped": False,
        # Set when the delete was refused for exceeding the blast-radius
        # cap, holding the number of rows it declined to remove.
        "delete_refused": 0,
    }

    seen_ids = []
    for article in articles:
        article_id = str(article.get("id") or "")
        if not article_id:
            continue

        # Belt and braces. fetch_all_kb_articles already filters these
        # out, but sync_articles_to_db is also called directly.
        if not _is_published(article.get("status")):
            stats["skipped_unpublished"] += 1
            continue

        # Skip articles with effectively no body, but still count them as
        # seen. A sub-20-character body is far more often a stripped-HTML
        # anomaly than a genuinely empty article, and keeping the last
        # good row beats deleting a real article over a parse glitch.
        body = (article.get("content") or "").strip()
        if len(body) < 20:
            stats["skipped"] += 1
            seen_ids.append(article_id)
            continue

        result = upsert_kb_article(article)
        if result in stats:
            stats[result] += 1
        seen_ids.append(article_id)

    # Drop rows for articles that no longer exist in Zoho, or that were
    # unpublished since the last run. Only ever on a complete fetch.
    if seen_ids and allow_delete:
        removed = delete_missing_kb_articles(seen_ids)
        if removed < 0:
            # Refused for blast radius. Nothing was deleted.
            stats["delete_refused"] = -removed
            stats["removed"] = 0
        else:
            stats["removed"] = removed
    elif seen_ids:
        stats["delete_skipped"] = True

    return stats


# =====================================================================
# Runtime query (used by intake.py)
# =====================================================================

def search_kb_articles(
    query: str,
    n_results: int = 2,
    language: str | None = None,
    body_chars: int = 3000,
) -> list[dict]:
    """Search KB articles via Postgres FTS.

    Returns list of {title, url, body, content_preview, category,
    modifiedTime, days_stale, score}.
    """
    rows = search_kb_articles_db(
        query=query,
        language=language,
        limit=n_results,
        body_chars=body_chars,
    )

    out = []
    for row in rows:
        body = row.get("body", "")
        preview = body[:200] + "..." if len(body) > 200 else body
        out.append({
            "title": row["title"],
            "url": row["url"],
            "body": body,
            "content_preview": preview,
            "category": row.get("category", ""),
            "modifiedTime": row.get("modified_time", ""),
            "days_stale": row.get("days_stale"),
            "score": row.get("score"),
        })
    return out


# =====================================================================
# Main sync runner
# =====================================================================

def run_kb_sync():
    """Run the full KB article sync.

    Fetches all articles from Zoho Desk and UPSERTs them into Postgres.
    Safe to run repeatedly -- only changed articles are written.
    """
    print("=" * 60)
    print("VOME KB ARTICLE SYNC")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    articles = fetch_all_kb_articles()
    if not articles:
        print("[KB SYNC] No articles to sync")
        _alert_fetch_failures(articles_indexed=0)
        return

    stats = sync_articles_to_db(articles)

    print(f"\n{'=' * 60}")
    print("KB SYNC COMPLETE")
    print(f"  Added:       {stats['added']}")
    print(f"  Updated:     {stats['updated']}")
    print(f"  Unchanged:   {stats['unchanged']}")
    print(f"  Removed:     {stats['removed']}")
    print(f"  Skipped:     {stats['skipped']}  (empty body)")
    print(
        f"  Unpublished: {stats['skipped_unpublished']}  "
        f"(draft/unpublished, not indexed)"
    )
    if stats["delete_skipped"]:
        print("  Delete step SKIPPED (fetch was incomplete)")
    if stats["delete_refused"]:
        print(
            f"  Delete step REFUSED: would have removed "
            f"{stats['delete_refused']} rows, over the blast-radius cap"
        )
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    _alert_fetch_failures(articles_indexed=len(articles), stats=stats)
    _alert_mass_delete(stats)


def _alert_mass_delete(stats: dict) -> None:
    """Shout when the delete step refused a mass removal.

    This is the case the completeness guard cannot see: a category that
    returns an empty list successfully looks exactly like every article
    in it being deleted. Somebody has to look at it.
    """
    refused = stats.get("delete_refused") or 0
    if not refused:
        return
    message = (
        f":rotating_light: *KB sync refused a mass delete* "
        f"({refused} rows).\n"
        f"The fetch completed without errors, but pruning that many rows "
        f"means a whole category reported empty. Nothing was removed and "
        f"the index is unchanged.\n"
        f"If a category really was emptied in Zoho this is correct and "
        f"the rows can be cleared by re-running the sync with the "
        f"override. If not, check the category in Zoho before anything "
        f"else."
    )
    try:
        from slack import post_to_log
        post_to_log(message)
    except Exception as e:
        print(f"[KB SYNC] mass-delete alert failed: {e}")


def build_failure_alert(
    articles_indexed: int, stats: dict | None = None
) -> str:
    """Compose the Slack alert for a fetch that hit failures.

    Split out from the posting so the wording is testable. Every claim
    here has to follow from what actually failed: the first real alert
    told us new and edited articles were missing on a run that added 47
    and updated 44, and said nothing about the consequence that mattered.
    """
    stats = stats or {}
    by_kind: dict[str, list[str]] = {}
    for failure in LAST_FETCH_FAILURES:
        by_kind.setdefault(
            failure.get("kind", FAILURE_ARTICLE), []
        ).append(failure.get("message", ""))

    category_list = by_kind.get(FAILURE_CATEGORY_LIST, [])
    categories = by_kind.get(FAILURE_CATEGORY, [])
    articles = by_kind.get(FAILURE_ARTICLE, [])

    headline = (
        f":warning: *KB sync ran incomplete* "
        f"({len(LAST_FETCH_FAILURES)} failure(s), "
        f"{articles_indexed} articles fetched)"
    )

    if stats:
        written = (
            f"Indexed this run: {stats.get('added', 0)} added, "
            f"{stats.get('updated', 0)} updated, "
            f"{stats.get('unchanged', 0)} unchanged."
        )
    else:
        written = ""

    consequences = []
    if category_list:
        consequences.append(
            "Categories could not be listed at all, so this run indexed "
            "nothing. The index still holds whatever the last good run "
            "wrote."
        )
    if categories:
        consequences.append(
            f"{len(categories)} categor"
            f"{'y' if len(categories) == 1 else 'ies'} could not be "
            f"listed, so every article in "
            f"{'it' if len(categories) == 1 else 'them'} is absent from "
            f"this run. New and edited articles there are missing until "
            f"the next clean run."
        )
    if articles:
        consequences.append(
            f"{len(articles)} article(s) could not be fetched. Their "
            f"existing rows were left untouched, so they are stale "
            f"rather than missing, and everything else indexed normally."
        )

    # The one that is true on every incomplete run, and the one the
    # original alert left out.
    consequences.append(
        "The delete step was skipped. Rows for articles deleted or "
        "unpublished in Zoho were not pruned, so the agent can still "
        "cite them until a clean run removes them."
    )

    lines = [f.get("message", "") for f in LAST_FETCH_FAILURES][:15]
    more = len(LAST_FETCH_FAILURES) - len(lines)
    body = "\n".join(f"  - {line}" for line in lines)
    if more > 0:
        body += f"\n  - ...and {more} more"

    parts = [headline]
    if written:
        parts.append(written)
    parts.extend(consequences)
    parts.append(body)
    return "\n".join(parts)


def _alert_fetch_failures(
    articles_indexed: int, stats: dict | None = None
) -> None:
    """Post to Slack if the last fetch hit failures.

    Without this the nightly sync fails into a log line nobody reads,
    and the first symptom is the agent quietly citing articles that no
    longer exist.
    """
    if not LAST_FETCH_FAILURES:
        return
    try:
        from slack import post_to_log
        post_to_log(build_failure_alert(articles_indexed, stats))
    except Exception as e:
        print(f"[KB SYNC] Slack alert failed: {e}")


def print_kb_status():
    """Print the current state of the kb_articles index."""
    print("=" * 60)
    print("VOME KB INDEX STATUS")
    print("=" * 60)

    status = kb_index_status()
    if status.get("error"):
        print(f"Failed to read kb_articles: {status['error']}")
        return

    total = status.get("total", 0)
    if total == 0:
        print("No articles indexed. Run `python kb_sync.py` to populate.")
        return

    print(f"Articles indexed: {total}")

    by_lang = status.get("by_language", {}) or {}
    if by_lang:
        print(f"\nBy language: {by_lang}")

    by_status = status.get("by_status", {}) or {}
    if by_status:
        print(f"By status:   {by_status}")
        bad = {
            k: v for k, v in by_status.items()
            if k.strip().lower() in ("draft", "unpublished", "review")
        }
        if bad:
            print(
                f"  WARNING: {sum(bad.values())} non-published rows are "
                f"indexed. They are filtered out at query time, and the "
                f"next clean sync will remove them."
            )

    top_cats = status.get("top_categories", []) or []
    if top_cats:
        print("\nTop categories:")
        for cat, n in top_cats:
            print(f"  {n:>4}  {cat}")

    if status.get("synced_oldest"):
        print(f"\nLast synced (oldest entry): {status['synced_oldest']}")
    if status.get("synced_newest"):
        print(f"Last synced (newest entry): {status['synced_newest']}")
    if status.get("modified_oldest"):
        print(f"\nOldest article modifiedTime: {status['modified_oldest']}")
    if status.get("modified_newest"):
        print(f"Newest article modifiedTime: {status['modified_newest']}")

    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        print_kb_status()
    else:
        run_kb_sync()
