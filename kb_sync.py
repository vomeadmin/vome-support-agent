"""
kb_sync.py

Nightly sync of Zoho Desk KB articles into Postgres (kb_articles table).

This script:
1. Fetches all KB categories from Zoho Desk
2. Fetches all articles in each category
3. UPSERTs article content into Postgres -- only changed rows are
   touched (compared by Zoho's modifiedTime).
4. Deletes any rows whose article was removed from Zoho.

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


# =====================================================================
# Fetch articles from Zoho
# =====================================================================

def fetch_all_kb_articles() -> list[dict]:
    """Fetch all KB articles from all categories in Zoho Desk.

    Returns list of {id, title, content, permalink, category,
    modifiedTime, createdTime, language, status, url}.
    """
    articles = []

    print("[KB SYNC] Fetching KB categories...")
    cat_result = _zoho_desk_call(
        "ZohoDesk_getAllKBRootCategories",
        {"query_params": {"orgId": str(ZOHO_ORG_ID)}},
    )
    raw_cats = _unwrap_mcp_result(cat_result)
    if not raw_cats:
        print("[KB SYNC] Failed to fetch categories")
        return []

    categories = []
    if isinstance(raw_cats, dict):
        categories = raw_cats.get("data", [])
    elif isinstance(raw_cats, list):
        categories = raw_cats

    print(f"[KB SYNC] Found {len(categories)} categories")

    for cat in categories:
        cat_id = cat.get("id")
        cat_name = cat.get("name", "Unknown")
        if not cat_id:
            continue

        print(f"[KB SYNC] Category: {cat_name} (ID: {cat_id})")
        time.sleep(ZOHO_DELAY)

        art_result = _zoho_desk_call(
            "ZohoDesk_getArticles",
            {
                # categoryId belongs in query_params for this MCP server.
                # Passing it as a path variable returns "Invalid keys
                # found in path variable" and silently yields 0 articles.
                "query_params": {
                    "orgId": str(ZOHO_ORG_ID),
                    "categoryId": str(cat_id),
                    "limit": 100,
                },
            },
        )

        raw_articles = _unwrap_mcp_result(art_result)
        if not raw_articles:
            continue

        art_list = []
        if isinstance(raw_articles, dict):
            art_list = raw_articles.get("data", [])
        elif isinstance(raw_articles, list):
            art_list = raw_articles

        for art in art_list:
            article_id = str(art.get("id", ""))
            if not article_id:
                continue

            time.sleep(ZOHO_DELAY)
            detail = _fetch_article_detail(article_id)
            if not detail:
                continue

            raw_content = detail.get("answer", "") or ""
            clean_content = re.sub(r"<[^>]+>", " ", raw_content)
            clean_content = re.sub(r"\s+", " ", clean_content).strip()

            title = detail.get("title", art.get("title", ""))
            permalink = detail.get("permalink", "")

            language = "en"
            if detail.get("locale"):
                language = (
                    "fr" if "fr" in detail["locale"].lower()
                    else "en"
                )

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

        print(f"[KB SYNC]   -> {len(art_list)} articles in {cat_name}")

    print(f"[KB SYNC] Total articles fetched: {len(articles)}")
    return articles


def _fetch_article_detail(article_id: str) -> dict | None:
    """Fetch full article content from Zoho Desk."""
    result = _zoho_desk_call(
        "ZohoDesk_getArticle",
        {
            "path_variables": {"articleId": str(article_id)},
            "query_params": {"orgId": str(ZOHO_ORG_ID)},
        },
    )
    raw = _unwrap_mcp_result(result)
    if isinstance(raw, dict):
        return raw
    return None


def _build_article_url(permalink: str, article_id: str) -> str:
    base = "https://support.vomevolunteer.com/portal/en/kb/articles"
    if permalink:
        return f"{base}/{permalink}"
    return f"{base}/{article_id}"


# =====================================================================
# Upsert into Postgres
# =====================================================================

def sync_articles_to_db(articles: list[dict]) -> dict:
    """UPSERT each article into kb_articles, then delete missing rows.

    Returns {added, updated, unchanged, removed, skipped}.
    """
    stats = {
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "removed": 0,
        "skipped": 0,
    }

    seen_ids = []
    for article in articles:
        article_id = str(article.get("id") or "")
        if not article_id:
            continue
        # Skip articles with effectively no body
        body = (article.get("content") or "").strip()
        if len(body) < 20:
            stats["skipped"] += 1
            continue

        result = upsert_kb_article(article)
        if result in stats:
            stats[result] += 1
        seen_ids.append(article_id)

    # Drop rows for articles that no longer exist in Zoho
    if seen_ids:
        stats["removed"] = delete_missing_kb_articles(seen_ids)

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
        return

    stats = sync_articles_to_db(articles)

    print(f"\n{'=' * 60}")
    print("KB SYNC COMPLETE")
    print(f"  Added:     {stats['added']}")
    print(f"  Updated:   {stats['updated']}")
    print(f"  Unchanged: {stats['unchanged']}")
    print(f"  Removed:   {stats['removed']}")
    print(f"  Skipped:   {stats['skipped']}  (empty body)")
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)


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
