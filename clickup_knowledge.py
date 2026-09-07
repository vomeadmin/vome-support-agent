"""
clickup_knowledge.py

Mines closed ClickUp tasks for support knowledge.

ClickUp was previously used for workflow only: statuses, assignees,
sweeps and reports. Nothing ever read a finished task back. That left the
single richest source in the stack untouched, because a closed task
carries what a Zoho thread does not: the engineering diagnosis, what the
cause actually turned out to be, and what the client was told once the
team understood it.

This module reads Closed (and user education) tasks from the three
support lists, runs one Claude pass per task to extract the pattern, and
stores it in `analyzed_clickup_tasks`. ticket_analyzer.generate_knowledge_book
then synthesises those rows into the `engineering_resolutions` section,
which knowledge.py injects into every drafted reply.

Resumable and incremental: a task already in the table is skipped, so a
weekly run only pays for what closed that week.

    python clickup_knowledge.py              # bounded run
    python clickup_knowledge.py --limit 50   # smaller bite
    python clickup_knowledge.py --status     # what has been mined
"""

import json
import re
import sys
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

import anthropic

from clickup_tasks import (
    CLICKUP_API_TOKEN,
    CLICKUP_BASE,
    extract_comment_text,
    LIST_PRIORITY_QUEUE,
    LIST_RAW_INTAKE,
    LIST_ACCEPTED_BACKLOG,
    FIELD_MODULE,
    FIELD_TYPE,
    FIELD_ZOHO_TICKET_LINK,
)
from database import _get_engine, DATABASE_URL
from model_config import SUPPORT_MODEL

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

_client = anthropic.Anthropic()

# The three support lists, by their ClickUp list id.
SOURCE_LISTS = {
    LIST_PRIORITY_QUEUE: "Priority Queue",
    LIST_RAW_INTAKE: "Raw Intake",
    LIST_ACCEPTED_BACKLOG: "Accepted Backlog",
}

# Statuses that mean the work is finished and the outcome is known.
# "Closed" is the real terminal status on these lists (not "done", which
# is the normalised value used elsewhere in the codebase). "user
# education" is included because those are precisely the tickets where
# the answer was an explanation, which is the most reusable kind.
# "declined" is excluded: a turned-down feature request teaches nothing
# about resolving an issue.
TERMINAL_STATUSES = {"closed", "done", "user education"}

# Claude pacing, matching ticket_analyzer.
CLAUDE_DELAY = 2
CLICKUP_DELAY = 0.4

# Default ceiling for one run, so a scheduled job has a bounded cost and
# chips away at a backlog across weeks instead of running for hours.
DEFAULT_RUN_LIMIT = 150


# =====================================================================
# Fetch
# =====================================================================

def _headers() -> dict:
    return {"Authorization": CLICKUP_API_TOKEN}


def _custom_field(task: dict, field_id: str):
    for field in task.get("custom_fields") or []:
        if field.get("id") == field_id:
            value = field.get("value")
            # Dropdown fields return an index into type_config.options.
            if isinstance(value, int):
                options = (
                    field.get("type_config", {}).get("options", []) or []
                )
                if 0 <= value < len(options):
                    return options[value].get("name", "")
            return value
    return None


def fetch_closed_tasks() -> list[dict]:
    """List every finished task across the three support lists.

    Returns light task dicts. The description arrives with the list
    payload, so this needs no per-task call; only comments do.
    """
    if not CLICKUP_API_TOKEN:
        print("[CU KNOWLEDGE] CLICKUP_API_TOKEN not set")
        return []

    out = []
    for list_id, list_name in SOURCE_LISTS.items():
        page = 0
        found = 0
        while page < 50:  # safety cap, 100 tasks per page
            try:
                resp = httpx.get(
                    f"{CLICKUP_BASE}/list/{list_id}/task",
                    headers=_headers(),
                    params={
                        "include_closed": "true",
                        "archived": "false",
                        "subtasks": "true",
                        "page": page,
                    },
                    timeout=30,
                )
                resp.raise_for_status()
            except Exception as e:
                print(f"[CU KNOWLEDGE] {list_name} page {page} failed: {e}")
                break

            payload = resp.json()
            tasks = payload.get("tasks", []) or []
            for task in tasks:
                status = (
                    (task.get("status") or {}).get("status") or ""
                ).strip().lower()
                if status not in TERMINAL_STATUSES:
                    continue
                out.append({
                    "task_id": str(task.get("id", "")),
                    "name": task.get("name", ""),
                    "description": (task.get("description") or "").strip(),
                    "status": status,
                    "list_name": list_name,
                    "date_done": task.get("date_done") or task.get("date_closed"),
                    "module": _custom_field(task, FIELD_MODULE) or "",
                    "type": _custom_field(task, FIELD_TYPE) or "",
                    "zoho_ticket_id": _extract_zoho_ticket_id(task),
                })
                found += 1

            if payload.get("last_page") or not tasks:
                break
            page += 1
            time.sleep(CLICKUP_DELAY)

        print(f"[CU KNOWLEDGE] {list_name}: {found} finished tasks")

    return out


_ZOHO_ID_RE = re.compile(r"(\d{10,})")


def _extract_zoho_ticket_id(task: dict) -> str:
    """Pull the Zoho ticket id off the task.

    Prefers the linked-ticket custom field. Roughly one task in seven has
    no value there, so it falls back to the Zoho link that
    create_clickup_task writes into the description header.
    """
    raw = _custom_field(task, FIELD_ZOHO_TICKET_LINK)
    if raw:
        match = _ZOHO_ID_RE.search(str(raw))
        if match:
            return match.group(1)

    description = task.get("description") or ""
    if "desk.zoho.com" in description:
        for line in description.splitlines():
            if "desk.zoho.com" in line:
                match = _ZOHO_ID_RE.search(line)
                if match:
                    return match.group(1)
    return ""


# create_clickup_task prefixes every description with an account/tier/link
# header, then a horizontal rule, then the actual report. The header is
# real context for the model but it is not substance, so it is stripped
# before deciding whether a task is worth a Claude call. Otherwise a task
# whose only body is the boilerplate reads as 250 characters of content.
_BOILERPLATE_MARKERS = ("**Account:**", "**Zoho ticket:**", "**Zoho link:**")


def strip_task_boilerplate(description: str) -> str:
    """Return the human-written part of a task description."""
    text = (description or "").strip()
    if not text:
        return ""
    head = text[:400]
    if not any(marker in head for marker in _BOILERPLATE_MARKERS):
        return text
    # The generated header ends at the first horizontal rule.
    for separator in ("\n---\n", "\n---", "---"):
        idx = text.find(separator)
        if idx != -1:
            return text[idx + len(separator):].strip()
    # No rule found: drop the header lines themselves.
    kept = [
        line for line in text.splitlines()
        if not any(marker in line for marker in _BOILERPLATE_MARKERS)
    ]
    return "\n".join(kept).strip()


def fetch_task_comments(task_id: str) -> list[str]:
    """Return comment lines as 'author: text', oldest first."""
    if not CLICKUP_API_TOKEN or not task_id:
        return []
    try:
        resp = httpx.get(
            f"{CLICKUP_BASE}/task/{task_id}/comment",
            headers=_headers(),
            timeout=20,
        )
        resp.raise_for_status()
        comments = resp.json().get("comments", []) or []
    except Exception as e:
        print(f"[CU KNOWLEDGE] comments failed for {task_id}: {e}")
        return []

    lines = []
    for c in reversed(comments):  # ClickUp returns newest first
        author = (c.get("user") or {}).get("username", "Unknown")
        body = extract_comment_text(c)
        if body:
            lines.append(f"{author}: {body}")
    return lines


# =====================================================================
# Analyse
# =====================================================================

ANALYSIS_PROMPT = """You are mining a finished engineering task from Vome, a volunteer management platform, for support knowledge. The goal is to capture what this issue actually turned out to be and what a client should be told when it comes up again.

TASK: {name}
LIST: {list_name}
FINAL STATUS: {status}
MODULE: {module}
TYPE: {type}

DESCRIPTION (the original report):
{description}

INTERNAL DISCUSSION (engineer and support comments, oldest first):
{comments}

Return a JSON object with these fields:

{{
  "category": "bug|feature_request|billing|auth|how_to|data_issue|account_management|unclear",
  "module": "the Vome module this relates to (scheduling, opportunities, forms, sequences, chat, etc.) or 'general'",
  "problem_statement": "one sentence, in plain client-facing language, describing what the client experienced",
  "root_cause": "what it actually turned out to be, in plain language, or 'not stated' if the thread never says",
  "resolution_type": "code_fix|config_change|user_education|data_correction|no_action|unresolved",
  "resolution_summary": "one or two sentences on how it was resolved",
  "client_facing_explanation": "how you would explain this to a client in Sam's voice, with no internal engineering detail. Empty string if there is nothing safe to say.",
  "diagnostic_questions": ["the questions that actually narrowed this down, phrased so support could ask them again"],
  "recurrence_risk": "high|medium|low (how likely is another client to hit this)",
  "kb_article_candidate": true/false (would a help center article prevent this ticket?),
  "kb_article_topic": "suggested article topic if kb_article_candidate is true, else null",
  "training_value": "high|medium|low",
  "training_notes": "what a support agent should take away from this task"
}}

Rules:
- Ground everything in the text above. If the thread does not say what the cause was, say "not stated" rather than inventing one.
- client_facing_explanation must never contain internal detail: no code, no file names, no engineer names, no ticket mechanics.
- Be specific. "A timezone mismatch made shifts show an hour early" beats "a display issue".

Return ONLY the JSON object, no other text."""


def analyze_task(task: dict, comments: list[str]) -> dict | None:
    """Run one Claude pass over a finished task. None on failure."""
    comment_text = "\n\n".join(comments) if comments else "(no comments)"
    if len(comment_text) > 8000:
        comment_text = comment_text[:8000] + "\n\n[... truncated]"

    description = task.get("description") or "(no description)"
    if len(description) > 4000:
        description = description[:4000] + "\n\n[... truncated]"

    prompt = ANALYSIS_PROMPT.format(
        name=task.get("name", ""),
        list_name=task.get("list_name", ""),
        status=task.get("status", ""),
        module=task.get("module") or "unspecified",
        type=task.get("type") or "unspecified",
        description=description,
        comments=comment_text,
    )

    try:
        response = _client.messages.create(
            model=SUPPORT_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)
        return json.loads(text)
    except Exception as e:
        print(f"[CU KNOWLEDGE] analysis failed: {e}")
        return None


def has_enough_substance(task: dict, comments: list[str]) -> bool:
    """Skip tasks with nothing to learn from.

    A one-line task closed with no discussion produces a confident,
    useless analysis, which is worse than no row at all.

    Measured on the human-written part only. Every task carries a ~250
    character generated header, so an unstripped length check would call
    an empty task substantial.
    """
    description = strip_task_boilerplate(task.get("description") or "")
    if len(description) >= 80:
        return True
    joined = " ".join(comments)
    return len(joined.strip()) >= 120


# =====================================================================
# Store
# =====================================================================

def is_task_analyzed(task_id: str) -> bool:
    if not DATABASE_URL:
        return False
    try:
        from sqlalchemy import text as sql_text
        with _get_engine().connect() as conn:
            row = conn.execute(
                sql_text(
                    "SELECT 1 FROM analyzed_clickup_tasks "
                    "WHERE task_id = :tid"
                ),
                {"tid": task_id},
            ).first()
        return row is not None
    except Exception:
        return False


def save_task_analysis(task: dict, analysis: dict, comment_count: int):
    if not DATABASE_URL:
        print("[CU KNOWLEDGE] DATABASE_URL not set -- skipping save")
        return
    try:
        from sqlalchemy import text as sql_text
        closed_at = None
        raw_done = task.get("date_done")
        if raw_done:
            try:
                closed_at = datetime.fromtimestamp(
                    int(raw_done) / 1000, tz=timezone.utc
                )
            except (ValueError, TypeError, OSError):
                closed_at = None

        with _get_engine().begin() as conn:
            conn.execute(
                sql_text("""
                    INSERT INTO analyzed_clickup_tasks
                        (task_id, name, list_name, category, module,
                         zoho_ticket_id, comment_count, closed_at,
                         analysis, analyzed_at)
                    VALUES
                        (:tid, :name, :list, :cat, :mod, :zoho,
                         :ccount, :closed, CAST(:analysis AS jsonb), :now)
                    ON CONFLICT (task_id) DO UPDATE SET
                        analysis = CAST(EXCLUDED.analysis AS jsonb),
                        analyzed_at = EXCLUDED.analyzed_at
                """),
                {
                    "tid": task["task_id"],
                    "name": task.get("name", "")[:500],
                    "list": task.get("list_name", ""),
                    "cat": analysis.get("category", ""),
                    "mod": analysis.get("module", ""),
                    "zoho": task.get("zoho_ticket_id", ""),
                    "ccount": comment_count,
                    "closed": closed_at,
                    "analysis": json.dumps(analysis),
                    "now": datetime.now(timezone.utc),
                },
            )
    except Exception as e:
        print(f"[CU KNOWLEDGE] save failed for {task.get('task_id')}: {e}")


def get_all_task_analyses() -> list[dict]:
    """Every mined task, for the knowledge book synthesiser."""
    if not DATABASE_URL:
        return []
    try:
        from sqlalchemy import text as sql_text
        with _get_engine().connect() as conn:
            rows = conn.execute(
                sql_text(
                    "SELECT task_id, name, list_name, category, module, "
                    "       zoho_ticket_id, closed_at, analysis "
                    "FROM analyzed_clickup_tasks ORDER BY analyzed_at"
                )
            ).mappings().all()

        out = []
        for r in rows:
            analysis = r["analysis"]
            if isinstance(analysis, str):
                analysis = json.loads(analysis)
            out.append({
                "task_id": r["task_id"],
                "name": r["name"],
                "list_name": r["list_name"],
                "category": r["category"],
                "module": r["module"],
                "zoho_ticket_id": r["zoho_ticket_id"],
                # Recency key for knowledge_synthesis.rank_and_select.
                "closed_at": r["closed_at"],
                "analysis": analysis or {},
            })
        return out
    except Exception as e:
        print(f"[CU KNOWLEDGE] fetch analyses failed: {e}")
        return []


def get_task_analysis_stats() -> dict:
    if not DATABASE_URL:
        return {}
    try:
        from sqlalchemy import text as sql_text
        with _get_engine().connect() as conn:
            rows = conn.execute(
                sql_text(
                    "SELECT category, COUNT(*) AS cnt "
                    "FROM analyzed_clickup_tasks "
                    "GROUP BY category ORDER BY cnt DESC"
                )
            ).mappings().all()
        return {r["category"]: r["cnt"] for r in rows}
    except Exception:
        return {}


# =====================================================================
# Runner
# =====================================================================

def run_clickup_knowledge_scan(limit: int | None = DEFAULT_RUN_LIMIT) -> dict:
    """Mine finished ClickUp tasks that have not been mined yet.

    Returns {found, already_analyzed, processed, skipped, failed,
    remaining}.
    """
    print("=" * 60)
    print("CLICKUP KNOWLEDGE SCAN")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    tasks = fetch_closed_tasks()
    stats = {
        "found": len(tasks),
        "already_analyzed": 0,
        "processed": 0,
        "skipped": 0,
        "failed": 0,
        "remaining": 0,
    }
    if not tasks:
        return stats

    pending = []
    for task in tasks:
        if not task["task_id"]:
            continue
        if is_task_analyzed(task["task_id"]):
            stats["already_analyzed"] += 1
        else:
            pending.append(task)

    if limit is not None and len(pending) > limit:
        stats["remaining"] = len(pending) - limit
        pending = pending[:limit]

    print(
        f"[CU KNOWLEDGE] {stats['found']} finished tasks, "
        f"{stats['already_analyzed']} already mined, "
        f"{len(pending)} to process this run, "
        f"{stats['remaining']} left for the next one"
    )

    for i, task in enumerate(pending, start=1):
        name = (task.get("name") or "")[:60]
        print(f"[{i}/{len(pending)}] {task['task_id']}: {name}")

        comments = fetch_task_comments(task["task_id"])
        time.sleep(CLICKUP_DELAY)

        if not has_enough_substance(task, comments):
            print("  -> SKIP: nothing substantial to learn from")
            stats["skipped"] += 1
            continue

        analysis = analyze_task(task, comments)
        if not analysis:
            stats["failed"] += 1
            continue

        save_task_analysis(task, analysis, len(comments))
        stats["processed"] += 1
        print(
            f"  -> OK: {analysis.get('category', '?')} / "
            f"{analysis.get('resolution_type', '?')} "
            f"(training: {analysis.get('training_value', '?')})"
        )
        time.sleep(CLAUDE_DELAY)

    print("=" * 60)
    print(
        f"CLICKUP KNOWLEDGE SCAN COMPLETE: "
        f"{stats['processed']} mined, {stats['skipped']} skipped, "
        f"{stats['failed']} failed, {stats['remaining']} remaining"
    )
    print("=" * 60)
    return stats


if __name__ == "__main__":
    if "--status" in sys.argv:
        print("Mined ClickUp tasks by category:")
        for cat, n in get_task_analysis_stats().items():
            print(f"  {n:>5}  {cat}")
    else:
        run_limit = DEFAULT_RUN_LIMIT
        if "--limit" in sys.argv:
            run_limit = int(sys.argv[sys.argv.index("--limit") + 1])
        if "--all" in sys.argv:
            run_limit = None
        run_clickup_knowledge_scan(limit=run_limit)
