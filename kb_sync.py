"""
kb_sync.py

Nightly sync of Zoho Desk KB articles into ChromaDB.

This script:
1. Fetches all KB categories from Zoho Desk
2. Fetches all articles in each category
3. Indexes article content into ChromaDB for semantic search
4. Tracks article modifiedTime so only changed articles are re-indexed

ChromaDB collection: "vome_kb_articles"
Each document = one article with metadata (title, url, category,
modifiedTime, language).

At runtime, intake.py queries this collection to find relevant
articles for the current conversation -- much more accurate than
the keyword-based Zoho search API.

Schedule: Runs nightly via APScheduler (see main.py)
Can also be triggered manually: python kb_sync.py
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import chromadb

from agent import (
    _zoho_desk_call,
    _unwrap_mcp_result,
    ZOHO_ORG_ID,
)

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# ChromaDB persistent storage
CHROMA_DIR = Path(__file__).parent / "chroma_data"
CHROMA_DIR.mkdir(exist_ok=True)

COLLECTION_NAME = "vome_kb_articles"

# Rate limiting
ZOHO_DELAY = 1.5  # seconds between API calls


def get_chroma_client():
    """Get or create the ChromaDB persistent client."""
    return chromadb.PersistentClient(path=str(CHROMA_DIR))


def get_collection():
    """Get or create the KB articles collection."""
    client = get_chroma_client()
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"description": "Vome Zoho Desk KB articles"},
    )


# =====================================================================
# Fetch articles from Zoho
# =====================================================================

def fetch_all_kb_articles() -> list[dict]:
    """Fetch all KB articles from all categories in Zoho Desk.

    Returns list of {id, title, content, permalink, category,
    modifiedTime, createdTime, language, status}.
    """
    articles = []

    # Step 1: Get all root categories
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

    # Step 2: For each category, get sections then articles
    for cat in categories:
        cat_id = cat.get("id")
        cat_name = cat.get("name", "Unknown")
        if not cat_id:
            continue

        print(f"[KB SYNC] Category: {cat_name} (ID: {cat_id})")
        time.sleep(ZOHO_DELAY)

        # Get articles directly from category
        art_result = _zoho_desk_call(
            "ZohoDesk_getArticles",
            {
                "path_variables": {"categoryId": str(cat_id)},
                "query_params": {
                    "orgId": str(ZOHO_ORG_ID),
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

            # Fetch full article content
            time.sleep(ZOHO_DELAY)
            detail = _fetch_article_detail(article_id)
            if not detail:
                continue

            # Clean HTML from content
            raw_content = detail.get("answer", "") or ""
            clean_content = re.sub(r"<[^>]+>", " ", raw_content)
            clean_content = re.sub(r"\s+", " ", clean_content).strip()

            title = detail.get("title", art.get("title", ""))
            permalink = detail.get("permalink", "")

            # Detect language from title/content
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

        print(
            f"[KB SYNC]   -> {len(art_list)} articles in {cat_name}"
        )

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
    """Build the public-facing KB article URL."""
    base = "https://support.vomevolunteer.com/portal/en/kb/articles"
    if permalink:
        return f"{base}/{permalink}"
    return f"{base}/{article_id}"


# =====================================================================
# Index into ChromaDB
# =====================================================================

def sync_articles_to_chromadb(articles: list[dict]) -> dict:
    """Index articles into ChromaDB.

    Returns {added: int, updated: int, unchanged: int, removed: int}.
    """
    collection = get_collection()
    stats = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}

    # Get existing article IDs in ChromaDB
    try:
        existing = collection.get()
        existing_ids = set(existing["ids"]) if existing["ids"] else set()
        existing_meta = {}
        for i, eid in enumerate(existing["ids"]):
            if existing["metadatas"]:
                existing_meta[eid] = existing["metadatas"][i]
    except Exception:
        existing_ids = set()
        existing_meta = {}

    # Track which articles are still in Zoho
    current_ids = set()

    for article in articles:
        art_id = f"kb_{article['id']}"
        current_ids.add(art_id)

        # Skip if content is empty
        if not article["content"] or len(article["content"]) < 20:
            continue

        # Check if article has changed since last sync
        if art_id in existing_ids:
            old_meta = existing_meta.get(art_id, {})
            if (
                old_meta.get("modifiedTime")
                == article["modifiedTime"]
            ):
                stats["unchanged"] += 1
                continue

        # Build document text for embedding
        doc_text = (
            f"{article['title']}\n\n"
            f"{article['content']}"
        )

        # Truncate very long articles
        if len(doc_text) > 8000:
            doc_text = doc_text[:8000]

        metadata = {
            "title": article["title"],
            "url": article["url"],
            "category": article["category"],
            "permalink": article["permalink"],
            "modifiedTime": article["modifiedTime"],
            "createdTime": article["createdTime"],
            "language": article["language"],
            "status": article["status"],
            "zoho_article_id": article["id"],
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }

        if art_id in existing_ids:
            # Update existing
            collection.update(
                ids=[art_id],
                documents=[doc_text],
                metadatas=[metadata],
            )
            stats["updated"] += 1
        else:
            # Add new
            collection.add(
                ids=[art_id],
                documents=[doc_text],
                metadatas=[metadata],
            )
            stats["added"] += 1

    # Remove articles that no longer exist in Zoho
    removed_ids = existing_ids - current_ids
    if removed_ids:
        collection.delete(ids=list(removed_ids))
        stats["removed"] = len(removed_ids)

    return stats


# =====================================================================
# Runtime query (used by intake.py)
# =====================================================================

def search_kb_articles(
    query: str,
    n_results: int = 3,
    language: str | None = None,
) -> list[dict]:
    """Search KB articles by semantic similarity.

    Returns list of {title, url, content_preview, category,
    modifiedTime, days_stale, score}.
    """
    try:
        collection = get_collection()

        where_filter = None
        if language:
            where_filter = {"language": language}

        results = collection.query(
            query_texts=[query],
            n_results=n_results,
            where=where_filter,
        )

        if not results or not results["ids"] or not results["ids"][0]:
            return []

        articles = []
        now = datetime.now(timezone.utc)

        for i, doc_id in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i]
            distance = (
                results["distances"][0][i]
                if results.get("distances")
                else None
            )
            doc = (
                results["documents"][0][i]
                if results.get("documents")
                else ""
            )

            # Compute days stale
            days_stale = None
            mod_time = meta.get("modifiedTime", "")
            if mod_time:
                try:
                    modified = datetime.fromisoformat(
                        mod_time.replace("Z", "+00:00")
                    )
                    days_stale = (now - modified).days
                except (ValueError, TypeError):
                    pass

            # Content preview (first 200 chars)
            content_preview = doc[:200] + "..." if len(doc) > 200 else doc

            articles.append({
                "title": meta.get("title", ""),
                "url": meta.get("url", ""),
                "content_preview": content_preview,
                "category": meta.get("category", ""),
                "modifiedTime": mod_time,
                "days_stale": days_stale,
                "score": 1 - distance if distance is not None else None,
            })

        return articles

    except Exception as e:
        print(f"[KB SEARCH] ChromaDB query failed: {e}")
        return []


# =====================================================================
# Main sync runner
# =====================================================================

def run_kb_sync():
    """Run the full KB article sync.

    Fetches all articles from Zoho Desk and indexes them
    into ChromaDB. Safe to run repeatedly -- only changed
    articles are re-indexed.
    """
    print("=" * 60)
    print("VOME KB ARTICLE SYNC")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Fetch articles
    articles = fetch_all_kb_articles()
    if not articles:
        print("[KB SYNC] No articles to sync")
        return

    # Index into ChromaDB
    stats = sync_articles_to_chromadb(articles)

    print(f"\n{'=' * 60}")
    print("KB SYNC COMPLETE")
    print(f"  Added: {stats['added']}")
    print(f"  Updated: {stats['updated']}")
    print(f"  Unchanged: {stats['unchanged']}")
    print(f"  Removed: {stats['removed']}")
    print(f"  Total in index: {stats['added'] + stats['updated'] + stats['unchanged']}")
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)


if __name__ == "__main__":
    run_kb_sync()
