"""
kb_search.py

Zoho Desk KB article search with freshness scoring.
Used by the intake widget to deflect self-service questions
and flag stale or missing KB articles.
"""

import os
from datetime import datetime, timezone

from agent import _zoho_desk_call, _unwrap_mcp_result

ZOHO_ORG_ID = os.environ.get("ZOHO_ORG_ID", "")


def search_kb(query: str, limit: int = 5) -> list[dict]:
    """Search Zoho Desk KB articles by keyword query.

    Returns a list of scored article dicts, sorted by relevance.
    Each dict contains: title, url, days_stale, action, article_id, permalink.
    """
    if not query or not query.strip():
        return []

    result = _zoho_desk_call("ZohoDesk_searchArticleTranslations", {
        "query_params": {
            "orgId": str(ZOHO_ORG_ID),
            "searchStr": query.strip(),
            "limit": limit,
            "sortBy": "relevance",
        },
    })

    raw = _unwrap_mcp_result(result)
    if not raw:
        print(f"[KB] No results for query: {query}")
        return []

    # Zoho returns articles in a "data" list or directly as a list
    articles = []
    if isinstance(raw, dict):
        articles = raw.get("data", raw.get("articles", []))
    elif isinstance(raw, list):
        articles = raw

    scored = []
    for article in articles:
        scored_article = score_article(article)
        if scored_article:
            scored.append(scored_article)

    return scored


def score_article(article: dict) -> dict | None:
    """Score a KB article by freshness and return a structured result.

    Returns None if article data is invalid.
    """
    if not isinstance(article, dict):
        return None

    title = article.get("title", "")
    article_id = str(article.get("id", ""))
    permalink = article.get("permalink", "")
    modified_time_str = article.get("modifiedTime", "")

    if not title or not article_id:
        return None

    # Parse modified time
    days_stale = _compute_days_stale(modified_time_str)

    # Build the help center URL
    url = ""
    if permalink:
        url = f"https://support.vomevolunteer.com/portal/en/kb/articles/{permalink}"
    elif article_id:
        url = f"https://support.vomevolunteer.com/portal/en/kb/articles/{article_id}"

    # Freshness scoring
    if days_stale is None:
        action = "suggest_with_caveat"
    elif days_stale > 365:
        action = "flag_stale"
    elif days_stale > 90:
        action = "suggest_with_caveat"
    else:
        action = "suggest"

    return {
        "title": title,
        "url": url,
        "days_stale": days_stale,
        "action": action,
        "article_id": article_id,
        "permalink": permalink,
    }


def _compute_days_stale(modified_time_str: str) -> int | None:
    """Parse Zoho datetime string and compute days since last modification."""
    if not modified_time_str:
        return None
    try:
        # Zoho uses ISO 8601 format: 2025-01-15T10:30:00.000Z
        modified = datetime.fromisoformat(
            modified_time_str.replace("Z", "+00:00")
        )
        now = datetime.now(timezone.utc)
        return (now - modified).days
    except (ValueError, TypeError):
        return None


def get_best_kb_match(query: str) -> dict | None:
    """Search KB and return the best article suitable for deflection.

    Returns the top article with action "suggest" or "suggest_with_caveat",
    or None if no suitable article is found.
    """
    results = search_kb(query, limit=3)
    for article in results:
        if article["action"] in ("suggest", "suggest_with_caveat"):
            return article
    return None


# ---------------------------------------------------------------------------
# Repeat issue tracking (uses database.py)
# ---------------------------------------------------------------------------

def log_unmatched_issue(
    fingerprint: str,
    org_id: str | None = None,
    user_email: str | None = None,
) -> int:
    """Log an issue that had no KB match. Returns the count of occurrences
    in the last 30 days for this fingerprint.
    """
    from database import _get_engine, DATABASE_URL
    from sqlalchemy import text

    if not DATABASE_URL or not fingerprint:
        return 0

    fingerprint = fingerprint.strip().lower()
    engine = _get_engine()
    now = datetime.now(timezone.utc)

    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO kb_deflection_log "
                    "(issue_fingerprint, org_id, user_email, created_at) "
                    "VALUES (:fp, :org, :email, :ts)"
                ),
                {
                    "fp": fingerprint,
                    "org": org_id,
                    "email": user_email,
                    "ts": now,
                },
            )

            result = conn.execute(
                text(
                    "SELECT COUNT(*) FROM kb_deflection_log "
                    "WHERE issue_fingerprint = :fp "
                    "AND created_at > NOW() - INTERVAL '30 days'"
                ),
                {"fp": fingerprint},
            )
            count = result.scalar() or 0

        return count
    except Exception as e:
        print(f"[KB] log_unmatched_issue failed: {e}")
        return 0


def check_and_create_kb_task(
    fingerprint: str,
    org_id: str | None = None,
    user_email: str | None = None,
) -> bool:
    """Log an unmatched issue and create a ClickUp task if seen 3+ times.

    Returns True if a ClickUp task was created.
    """
    count = log_unmatched_issue(fingerprint, org_id, user_email)
    if count < 3:
        return False

    # Check if we already have a task for this fingerprint (avoid duplicates)
    # Simple approach: only create on exactly the 3rd occurrence
    if count != 3:
        return False

    try:
        from clickup_tasks import (
            CLICKUP_API_TOKEN,
            CLICKUP_BASE,
            LIST_RAW_INTAKE,
            FIELD_SOURCE,
            SOURCE_ZOHO_TICKET,
        )
        import httpx

        if not CLICKUP_API_TOKEN:
            print("[KB] CLICKUP_API_TOKEN not set -- skipping KB task creation")
            return False

        headers = {
            "Authorization": CLICKUP_API_TOKEN,
            "Content-Type": "application/json",
        }

        task_data = {
            "name": f"KB article needed -- {fingerprint}",
            "description": (
                f"This issue has been reported {count} times in the last 30 days "
                f"with no matching KB article.\n\n"
                f"**Issue:** {fingerprint}\n"
                f"**Last reported by:** {user_email or 'unknown'}\n"
                f"**Org:** {org_id or 'unknown'}\n\n"
                f"Action: Create a KB article covering this topic."
            ),
            "status": "to do",
            "custom_fields": [
                {"id": FIELD_SOURCE, "value": SOURCE_ZOHO_TICKET},
            ],
        }

        resp = httpx.post(
            f"{CLICKUP_BASE}/list/{LIST_RAW_INTAKE}/task",
            json=task_data,
            headers=headers,
            timeout=15,
        )

        if resp.status_code == 200:
            task_id = resp.json().get("id", "")
            print(f"[KB] Created ClickUp task {task_id} for KB gap: {fingerprint}")
            return True
        else:
            print(f"[KB] ClickUp task creation failed: {resp.status_code} {resp.text[:200]}")
            return False

    except Exception as e:
        print(f"[KB] KB task creation error: {e}")
        return False


def flag_stale_article(article: dict) -> bool:
    """Create a ClickUp task to refresh a stale KB article (> 365 days old).

    Returns True if a ClickUp task was created.
    """
    title = article.get("title", "Unknown article")
    days = article.get("days_stale", 0)
    url = article.get("url", "")

    try:
        from clickup_tasks import (
            CLICKUP_API_TOKEN,
            CLICKUP_BASE,
            LIST_RAW_INTAKE,
        )
        import httpx

        if not CLICKUP_API_TOKEN:
            return False

        headers = {
            "Authorization": CLICKUP_API_TOKEN,
            "Content-Type": "application/json",
        }

        task_data = {
            "name": f"KB refresh needed -- {title}",
            "description": (
                f"This KB article is {days} days old and was surfaced during "
                f"a support intake but not used for deflection due to staleness.\n\n"
                f"**Article:** {title}\n"
                f"**URL:** {url}\n"
                f"**Days since update:** {days}\n\n"
                f"Action: Review and update this article or archive if no longer relevant."
            ),
            "status": "to do",
        }

        resp = httpx.post(
            f"{CLICKUP_BASE}/list/{LIST_RAW_INTAKE}/task",
            json=task_data,
            headers=headers,
            timeout=15,
        )

        if resp.status_code == 200:
            task_id = resp.json().get("id", "")
            print(f"[KB] Created stale article task {task_id}: {title}")
            return True
        else:
            print(f"[KB] Stale article task failed: {resp.status_code}")
            return False

    except Exception as e:
        print(f"[KB] flag_stale_article error: {e}")
        return False


# ---------------------------------------------------------------------------
# Scheduled KB health scan
# ---------------------------------------------------------------------------

def run_kb_health_scan():
    """Scan all KB articles for staleness. Creates ClickUp tasks for
    articles over 365 days old.

    Intended to run weekly via APScheduler.
    """
    print("[KB SCAN] Starting weekly KB health scan...")

    # Fetch all KB categories first
    result = _zoho_desk_call("ZohoDesk_getAllKBRootCategories", {
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })

    raw = _unwrap_mcp_result(result)
    if not raw:
        print("[KB SCAN] Failed to fetch KB categories")
        return

    categories = []
    if isinstance(raw, dict):
        categories = raw.get("data", [])
    elif isinstance(raw, list):
        categories = raw

    stale_count = 0
    scanned_count = 0

    for category in categories:
        cat_id = category.get("id")
        if not cat_id:
            continue

        # Fetch articles in this category
        articles_result = _zoho_desk_call(
            "ZohoDesk_getArticles",
            {
                "path_variables": {"categoryId": str(cat_id)},
                "query_params": {
                    "orgId": str(ZOHO_ORG_ID),
                    "limit": 100,
                },
            },
        )

        articles_raw = _unwrap_mcp_result(articles_result)
        if not articles_raw:
            continue

        articles = []
        if isinstance(articles_raw, dict):
            articles = articles_raw.get("data", [])
        elif isinstance(articles_raw, list):
            articles = articles_raw

        for article in articles:
            scanned_count += 1
            scored = score_article(article)
            if scored and scored["action"] == "flag_stale":
                stale_count += 1
                flag_stale_article(scored)

    print(
        f"[KB SCAN] Complete. Scanned {scanned_count} articles, "
        f"flagged {stale_count} as stale."
    )
