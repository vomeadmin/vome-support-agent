"""
kb_gap.py

Closes the support feedback loop: a question a client asks that our help
center does not answer should end up as a new article, and a question it
answers badly should end up as a better one.

Everything here feeds or reads ONE table, kb_deflection_log. Two sources
write to it:

  1. The Vic widget. Already wired before this module existed, via
     intake.py -> kb_search.check_and_create_kb_task. Fires when a user
     asks Vic something and no article matched.

  2. Email tickets, through the "user education" status. When an engineer
     triages a ClickUp task to user education they have concluded the
     client misunderstood how something works. That is the strongest
     help-center-hole signal we produce, and until this module it never
     reached the counter: check_and_create_kb_task had exactly two call
     sites, both in intake.py. So the richest source of "our FAQ has a
     hole here" was invisible to the thing that detects holes.

And one scheduled pass reads it:

  run_kb_gap_clustering() -- monthly. Individual fingerprints are too
  granular to act on. "cant-find-shift-button" and "where-do-i-add-a-shift"
  are one article, and the 3-strikes counter in kb_search never sees them
  as the same thing. This clusters a quarter of signals into topics,
  checks each topic against the live KB index, and files the uncovered
  ones as ClickUp tasks with a drafted article already in the body.

Why the draft rides along on the ClickUp task instead of going straight
to Zoho: an article is public the moment it is published, and nothing
here is good enough to publish unread. The team already works out of
ClickUp, so the draft lands where the work happens and a human decides
whether it goes live. Nothing in this module writes to Zoho Desk.
"""

import json
import os
import re
from datetime import datetime, timezone

import anthropic

from model_config import SUPPORT_MODEL, SUPPORT_MODEL_FAST

_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

# A topic needs this many signals in the window before it is worth anyone's
# time. Below it we are looking at one client's confusion, not a pattern.
MIN_SIGNALS_FOR_TOPIC = 3

# How far back the monthly clustering pass looks. A quarter is long enough
# that a slow-burn topic (three tickets over ten weeks) still clusters, and
# short enough that an article we wrote in the meantime stops the topic from
# being re-filed forever.
CLUSTER_WINDOW_DAYS = 90

# An article older than this is treated as not really covering the topic.
# Matches the "suggest_with_caveat" boundary in kb_search.score_article.
COVERAGE_STALE_DAYS = 365

SOURCE_VIC = "vic_widget"
SOURCE_USER_EDUCATION = "user_education"


# ---------------------------------------------------------------------------
# 1. Recording a user-education signal
# ---------------------------------------------------------------------------

_FINGERPRINT_PROMPT = """A client asked Vome support a question. An engineer \
reviewed it and concluded the client misunderstood how the product works, \
rather than hitting a bug.

Reduce this to a short topic slug so we can count how often clients ask the \
same thing.

SUBJECT: {subject}

WHAT THE CLIENT ASKED:
{description}

WHAT THE ENGINEER SAID THE ANSWER IS:
{explanation}

Return a JSON object:

{{
  "fingerprint": "lowercase-kebab-slug, 3 to 6 words, naming the THING the client misunderstood, not this one client's phrasing",
  "question": "the underlying question in one plain sentence, as a client would ask it",
  "module": "the Vome module this sits in (scheduling, opportunities, forms, sequences, chat, database, reporting, billing, account) or 'general'"
}}

The slug must generalise. "shift-times-show-wrong-timezone" is good because \
another client hitting the same confusion produces the same slug. \
"acme-health-shifts-are-an-hour-off" is bad because it names one client. \
Do not include client names, dates, or ticket numbers.

Return ONLY the JSON object."""


def fingerprint_user_education(
    subject: str,
    description: str,
    explanation: str,
) -> dict | None:
    """Reduce one user-education ticket to a countable topic slug.

    Uses the fast model: this is a labelling call on a handful of tickets a
    day, and the slug only has to be stable, not clever. Returns None on any
    failure so the caller can carry on without a gap signal.
    """
    subject = (subject or "").strip()
    description = (description or "").strip()
    explanation = (explanation or "").strip()

    if not (subject or description) or not explanation:
        return None

    prompt = _FINGERPRINT_PROMPT.format(
        subject=subject or "(no subject)",
        description=description[:3000] or "(no description)",
        explanation=explanation[:4000],
    )

    try:
        response = _client.messages.create(
            model=SUPPORT_MODEL_FAST,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)
        parsed = json.loads(text)
    except Exception as e:
        print(f"[KB GAP] fingerprint failed: {e}")
        return None

    fingerprint = (parsed.get("fingerprint") or "").strip().lower()
    # Normalise whatever the model returned into a real slug. It usually
    # obeys the kebab instruction, but a stray space or capital would split
    # one topic into two rows that never reach the 3-signal threshold.
    fingerprint = re.sub(r"[^a-z0-9]+", "-", fingerprint).strip("-")
    if not fingerprint or len(fingerprint) < 3:
        return None

    return {
        "fingerprint": fingerprint[:120],
        "question": (parsed.get("question") or "").strip(),
        "module": (parsed.get("module") or "general").strip().lower(),
    }


def _kb_coverage(query: str) -> dict:
    """Ask the live KB index whether we already answer this.

    Returns {covered, stale, title, url, days_stale}. "covered" means an
    article exists AND is fresh enough to count. A stale hit is reported
    separately because it is a different job: refresh an article rather
    than write one.
    """
    blank = {
        "covered": False,
        "stale": False,
        "title": "",
        "url": "",
        "days_stale": None,
    }
    if not query:
        return blank

    try:
        from kb_sync import search_kb_articles
        hits = search_kb_articles(query, n_results=1, body_chars=200)
    except Exception as e:
        # A KB lookup failure must not stop us recording the signal. Treat
        # it as "unknown coverage" and let the monthly pass sort it out.
        print(f"[KB GAP] coverage lookup failed: {e}")
        return blank

    if not hits:
        return blank

    top = hits[0]
    days_stale = top.get("days_stale")
    stale = days_stale is not None and days_stale > COVERAGE_STALE_DAYS

    return {
        "covered": not stale,
        "stale": stale,
        "title": top.get("title", ""),
        "url": top.get("url", ""),
        "days_stale": days_stale,
    }


def record_user_education_gap(
    subject: str,
    description: str,
    explanation: str,
    org_id: str | None = None,
    user_email: str | None = None,
    zoho_ticket_id: str | None = None,
    clickup_task_id: str | None = None,
) -> dict | None:
    """Log one user-education ticket as a help-center signal.

    Called from clickup_user_education_handler after the explanation has
    gone out. Best effort throughout: this is instrumentation hanging off a
    handler that emails clients, so every failure path returns quietly
    rather than raising into the send flow.

    Returns the recorded signal, or None if nothing was recorded.
    """
    labelled = fingerprint_user_education(subject, description, explanation)
    if not labelled:
        return None

    fingerprint = labelled["fingerprint"]
    coverage = _kb_coverage(labelled["question"] or subject)

    ok = _log_signal(
        fingerprint=fingerprint,
        source=SOURCE_USER_EDUCATION,
        org_id=org_id,
        user_email=user_email,
        zoho_ticket_id=zoho_ticket_id,
        clickup_task_id=clickup_task_id,
        question=labelled["question"],
        module=labelled["module"],
        kb_covered=coverage["covered"],
    )
    if not ok:
        return None

    print(
        f"[KB GAP] user education signal '{fingerprint}'"
        f" (covered={coverage['covered']}, stale={coverage['stale']})"
        f" from ticket {zoho_ticket_id or '?'}"
    )

    return {
        "fingerprint": fingerprint,
        "question": labelled["question"],
        "module": labelled["module"],
        "coverage": coverage,
    }


def _log_signal(
    fingerprint: str,
    source: str,
    org_id: str | None = None,
    user_email: str | None = None,
    zoho_ticket_id: str | None = None,
    clickup_task_id: str | None = None,
    question: str = "",
    module: str = "",
    kb_covered: bool = False,
) -> bool:
    """Insert one row into kb_deflection_log. True if it landed."""
    from database import _get_engine, DATABASE_URL
    from sqlalchemy import text

    if not DATABASE_URL or not fingerprint:
        return False

    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO kb_deflection_log "
                    "(issue_fingerprint, org_id, user_email, created_at, "
                    " source, zoho_ticket_id, clickup_task_id, "
                    " question, module, kb_covered) "
                    "VALUES (:fp, :org, :email, :ts, "
                    " :source, :zoho, :cu, :q, :mod, :covered)"
                ),
                {
                    "fp": fingerprint.strip().lower(),
                    "org": org_id,
                    "email": user_email,
                    "ts": datetime.now(timezone.utc),
                    "source": source,
                    "zoho": zoho_ticket_id,
                    "cu": clickup_task_id,
                    "q": question[:500],
                    "mod": module[:60],
                    "covered": bool(kb_covered),
                },
            )
        return True
    except Exception as e:
        print(f"[KB GAP] _log_signal failed: {e}")
        return False


# ---------------------------------------------------------------------------
# 2. Monthly clustering
# ---------------------------------------------------------------------------

def _load_signals(window_days: int = CLUSTER_WINDOW_DAYS) -> list[dict]:
    """Every gap signal in the window, grouped by fingerprint.

    Both sources land in one table, so a topic that arrives through Vic and
    through email tickets counts once with both sources attached. That is
    the whole point of routing user education here rather than into its own
    counter.
    """
    from database import _get_engine, DATABASE_URL
    from sqlalchemy import text

    if not DATABASE_URL:
        return []

    try:
        engine = _get_engine()
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT issue_fingerprint, COUNT(*) AS signal_count, "
                    "       MAX(created_at) AS last_seen, "
                    "       MIN(created_at) AS first_seen, "
                    "       COUNT(DISTINCT org_id) AS org_count, "
                    "       BOOL_OR(COALESCE(kb_covered, FALSE)) AS any_covered, "
                    "       STRING_AGG(DISTINCT COALESCE(source, 'vic_widget'), ',') AS sources, "
                    "       STRING_AGG(DISTINCT NULLIF(module, ''), ',') AS modules, "
                    "       STRING_AGG(DISTINCT NULLIF(question, ''), ' | ') AS questions "
                    "FROM kb_deflection_log "
                    f"WHERE created_at > NOW() - INTERVAL '{int(window_days)} days' "
                    "GROUP BY issue_fingerprint "
                    "ORDER BY signal_count DESC"
                )
            ).mappings().all()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[KB GAP] _load_signals failed: {e}")
        return []


def _load_analysed_education(window_days: int = CLUSTER_WINDOW_DAYS) -> list[dict]:
    """Closed ClickUp tasks the weekly scan already flagged as article-worthy.

    clickup_knowledge.analyze_task writes kb_article_candidate and
    kb_article_topic on every task it mines, and nothing has ever read them.
    They are the richest input we have for actually WRITING the article:
    problem_statement is the client's words, client_facing_explanation is
    the answer in Sam's voice, already stripped of internal detail.
    """
    from database import _get_engine, DATABASE_URL
    from sqlalchemy import text

    if not DATABASE_URL:
        return []

    try:
        engine = _get_engine()
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT task_id, name, module, zoho_ticket_id, "
                    "       closed_at, analysis "
                    "FROM analyzed_clickup_tasks "
                    f"WHERE analyzed_at > NOW() - INTERVAL '{int(window_days)} days' "
                    "  AND (analysis->>'kb_article_candidate' = 'true' "
                    "       OR analysis->>'resolution_type' = 'user_education') "
                    "ORDER BY analyzed_at DESC "
                    "LIMIT 300"
                )
            ).mappings().all()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[KB GAP] _load_analysed_education failed: {e}")
        return []


_CLUSTER_PROMPT = """You are grouping Vome support signals into help centre \
topics. Each signal is a question a client asked that our help centre did not \
answer well.

Many signals are the same underlying topic worded differently. Group them.

SIGNALS (fingerprint, how many times, what was asked):
{signals}

RESOLVED TICKETS the engineering team flagged as article-worthy in the same \
period (these carry the actual answer):
{analysed}

Return a JSON object:

{{
  "topics": [
    {{
      "topic_key": "lowercase-kebab-slug naming the article that should exist",
      "title": "the help centre article title a client would search for",
      "question": "the question this article answers, in one plain sentence",
      "module": "scheduling|opportunities|forms|sequences|chat|database|reporting|billing|account|general",
      "fingerprints": ["every signal fingerprint that belongs to this topic"],
      "task_ids": ["every resolved task id whose answer feeds this topic"],
      "signal_count": 0,
      "why_it_matters": "one sentence on what clients keep getting stuck on"
    }}
  ]
}}

Rules:
- signal_count is the SUM of the counts of the fingerprints you grouped.
- Only group signals that one article would genuinely serve. Two adjacent \
questions that need two different answers are two topics.
- A topic with one signal and no resolved ticket is noise. Leave it out.
- Order topics by signal_count, highest first.
- Use only fingerprints and task ids that appear above. Do not invent any.

Return ONLY the JSON object."""


def _cluster_signals(signals: list[dict], analysed: list[dict]) -> list[dict]:
    """One Claude pass grouping raw fingerprints into article topics."""
    if not signals and not analysed:
        return []

    signal_lines = []
    for s in signals[:200]:
        questions = (s.get("questions") or "").strip()
        if len(questions) > 300:
            questions = questions[:300] + "..."
        signal_lines.append(
            f"- {s['issue_fingerprint']} (x{s['signal_count']},"
            f" sources: {s.get('sources') or 'unknown'})"
            + (f": {questions}" if questions else "")
        )

    analysed_lines = []
    for t in analysed[:120]:
        a = t.get("analysis") or {}
        topic = a.get("kb_article_topic") or a.get("problem_statement") or ""
        if not topic:
            continue
        analysed_lines.append(
            f"- [{t['task_id']}] module={t.get('module') or a.get('module') or 'general'}"
            f" topic={topic}"
        )

    prompt = _CLUSTER_PROMPT.format(
        signals="\n".join(signal_lines) or "(none)",
        analysed="\n".join(analysed_lines) or "(none)",
    )

    try:
        response = _client.messages.create(
            model=SUPPORT_MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)
        parsed = json.loads(text)
    except Exception as e:
        print(f"[KB GAP] clustering failed: {e}")
        return []

    topics = parsed.get("topics") or []

    # Recompute signal_count from our own numbers rather than trusting the
    # model's arithmetic. The count decides what gets filed, so it has to
    # come from the table, not from a language model adding things up.
    by_fingerprint = {s["issue_fingerprint"]: s for s in signals}
    cleaned = []
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        key = re.sub(
            r"[^a-z0-9]+", "-", (topic.get("topic_key") or "").lower()
        ).strip("-")
        if not key:
            continue

        fingerprints = [
            f for f in (topic.get("fingerprints") or [])
            if f in by_fingerprint
        ]
        count = sum(
            int(by_fingerprint[f]["signal_count"]) for f in fingerprints
        )
        task_ids = [
            t for t in (topic.get("task_ids") or [])
            if any(a["task_id"] == t for a in analysed)
        ]

        cleaned.append({
            "topic_key": key[:120],
            "title": (topic.get("title") or key).strip()[:200],
            "question": (topic.get("question") or "").strip(),
            "module": (topic.get("module") or "general").strip().lower(),
            "fingerprints": fingerprints,
            "task_ids": task_ids,
            "signal_count": count,
            "why_it_matters": (topic.get("why_it_matters") or "").strip(),
        })

    cleaned.sort(key=lambda t: t["signal_count"], reverse=True)
    return cleaned


_ARTICLE_PROMPT = """Write a Vome help centre article.

Vome is volunteer management software. The reader is a volunteer coordinator \
or administrator using Vome, not an engineer.

ARTICLE TITLE: {title}
THE QUESTION IT ANSWERS: {question}
MODULE: {module}

WHAT CLIENTS ACTUALLY ASKED ({signal_count} times in the last 90 days):
{questions}

HOW THE TEAM ANSWERED IT ON REAL TICKETS:
{answers}

{existing}

Write the article in Markdown. Structure:

- One or two sentences saying what this article covers and who it is for.
- A "## Steps" section with numbered steps, if there is a procedure.
- A "## Notes" section for the things clients get wrong, drawn from the \
tickets above.

Rules:
- Ground every instruction in the ticket answers above. If they do not say \
where a button lives, describe the outcome rather than inventing a menu path.
- Mark anything you could not confirm with "TODO: confirm". A reviewer will \
fill those in. Guessing is worse than an obvious gap.
- Plain language, second person, present tense. No internal detail, no \
engineer names, no ticket numbers.
- No em dashes anywhere. Use periods, commas, parentheses or "and".
- 400 words maximum.

Return ONLY the article Markdown, starting with the title as an H1."""


def _draft_article(topic: dict, analysed: list[dict], signals: list[dict]) -> str:
    """Draft the article this topic is asking for.

    Deliberately conservative: the prompt tells it to leave "TODO: confirm"
    rather than invent a menu path, because a confident wrong instruction in
    a help centre article is worse than a visible hole. A human publishes
    this, so an obvious gap gets filled and a plausible lie does not.
    """
    by_fingerprint = {s["issue_fingerprint"]: s for s in signals}
    questions = []
    for f in topic["fingerprints"]:
        row = by_fingerprint.get(f) or {}
        q = (row.get("questions") or "").strip()
        questions.append(f"- {f}" + (f": {q}" if q else ""))

    answers = []
    for t in analysed:
        if t["task_id"] not in topic["task_ids"]:
            continue
        a = t.get("analysis") or {}
        parts = []
        if a.get("problem_statement"):
            parts.append(f"  Problem: {a['problem_statement']}")
        if a.get("root_cause") and a["root_cause"] != "not stated":
            parts.append(f"  Cause: {a['root_cause']}")
        if a.get("client_facing_explanation"):
            parts.append(f"  Answer given: {a['client_facing_explanation']}")
        if a.get("diagnostic_questions"):
            qs = "; ".join(a["diagnostic_questions"][:3])
            parts.append(f"  Questions that narrowed it down: {qs}")
        if parts:
            answers.append(f"- [{t['task_id']}]\n" + "\n".join(parts))

    coverage = _kb_coverage(topic["question"] or topic["title"])
    if coverage["stale"]:
        existing = (
            "AN ARTICLE ALREADY EXISTS BUT IS STALE"
            f" ({coverage['days_stale']} days old):"
            f" \"{coverage['title']}\" {coverage['url']}\n"
            "Clients are still asking, so treat this as a rewrite."
        )
    elif coverage["covered"]:
        existing = (
            "AN ARTICLE ALREADY EXISTS AND IS RECENT:"
            f" \"{coverage['title']}\" {coverage['url']}\n"
            "Clients are still asking anyway, so the problem is findability"
            " or clarity, not absence. Write the version that would have"
            " stopped these tickets."
        )
    else:
        existing = "NO EXISTING ARTICLE COVERS THIS."

    prompt = _ARTICLE_PROMPT.format(
        title=topic["title"],
        question=topic["question"] or topic["title"],
        module=topic["module"],
        signal_count=topic["signal_count"],
        questions="\n".join(questions) or "(no detail captured)",
        answers="\n".join(answers) or "(no resolved tickets matched)",
        existing=existing,
    )

    try:
        response = _client.messages.create(
            model=SUPPORT_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"[KB GAP] article draft failed for {topic['topic_key']}: {e}")
        return ""


# ---------------------------------------------------------------------------
# 3. Filing the result
# ---------------------------------------------------------------------------

def _seen_topics() -> dict:
    """Topics already filed, so a monthly pass does not re-file them."""
    from database import _get_engine, DATABASE_URL
    from sqlalchemy import text

    if not DATABASE_URL:
        return {}
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT topic_key, clickup_task_id, status, signal_count "
                    "FROM kb_gap_topics"
                )
            ).mappings().all()
        return {r["topic_key"]: dict(r) for r in rows}
    except Exception as e:
        print(f"[KB GAP] _seen_topics failed: {e}")
        return {}


def _save_topic(topic: dict, clickup_task_id: str, status: str) -> None:
    from database import _get_engine, DATABASE_URL
    from sqlalchemy import text

    if not DATABASE_URL:
        return
    now = datetime.now(timezone.utc)
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO kb_gap_topics "
                    "(topic_key, title, module, signal_count, "
                    " clickup_task_id, status, first_filed_at, last_seen_at) "
                    "VALUES (:key, :title, :mod, :count, :cu, :status, "
                    "        :now, :now) "
                    "ON CONFLICT (topic_key) DO UPDATE SET "
                    "  signal_count = EXCLUDED.signal_count, "
                    "  last_seen_at = EXCLUDED.last_seen_at, "
                    "  title = EXCLUDED.title"
                ),
                {
                    "key": topic["topic_key"],
                    "title": topic["title"],
                    "mod": topic["module"],
                    "count": topic["signal_count"],
                    "cu": clickup_task_id,
                    "status": status,
                    "now": now,
                },
            )
    except Exception as e:
        print(f"[KB GAP] _save_topic failed: {e}")


def _create_topic_task(topic: dict, draft: str, coverage: dict) -> str:
    """File one ClickUp task carrying the drafted article.

    Returns the task id, or "" on failure.
    """
    try:
        from clickup_tasks import (
            CLICKUP_API_TOKEN,
            CLICKUP_BASE,
            LIST_RAW_INTAKE,
        )
        import httpx
    except Exception as e:
        print(f"[KB GAP] clickup import failed: {e}")
        return ""

    if not CLICKUP_API_TOKEN:
        print("[KB GAP] CLICKUP_API_TOKEN not set, skipping task creation")
        return ""

    if coverage["stale"]:
        headline = (
            f"KB refresh -- {topic['title']}"
        )
        situation = (
            f"An article exists but is {coverage['days_stale']} days old and "
            f"clients are still asking.\nExisting: {coverage['title']} "
            f"{coverage['url']}"
        )
    elif coverage["covered"]:
        headline = f"KB rewrite -- {topic['title']}"
        situation = (
            "An article already covers this and clients are asking anyway, "
            "so this is a findability or clarity problem rather than a "
            f"missing article.\nExisting: {coverage['title']} {coverage['url']}"
        )
    else:
        headline = f"KB article needed -- {topic['title']}"
        situation = "No help centre article covers this today."

    description = (
        f"**{topic['signal_count']} client signals in the last "
        f"{CLUSTER_WINDOW_DAYS} days.**\n\n"
        f"{situation}\n\n"
        f"**Question clients are asking:** {topic['question']}\n"
        f"**Module:** {topic['module']}\n"
        f"**Why it matters:** {topic['why_it_matters']}\n\n"
        f"**Signals grouped into this topic:** "
        f"{', '.join(topic['fingerprints']) or '(none)'}\n"
        f"**Source tickets:** "
        f"{', '.join(topic['task_ids']) or '(none)'}\n\n"
        "---\n\n"
        "## Drafted article (review before publishing)\n\n"
        "This draft was written from the answers the team gave on the "
        "tickets above. Anything it could not confirm is marked "
        "`TODO: confirm`. Nothing has been sent to Zoho Desk: publishing "
        "is a human step.\n\n"
        f"{draft or '_(draft generation failed, write from the signals above)_'}"
    )

    try:
        resp = httpx.post(
            f"{CLICKUP_BASE}/list/{LIST_RAW_INTAKE}/task",
            json={
                "name": headline,
                "description": description,
                "status": "to do",
            },
            headers={
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        if resp.status_code == 200:
            task_id = resp.json().get("id", "")
            print(f"[KB GAP] filed {topic['topic_key']} as task {task_id}")
            return task_id
        print(
            f"[KB GAP] task creation failed: {resp.status_code}"
            f" {resp.text[:200]}"
        )
        return ""
    except Exception as e:
        print(f"[KB GAP] task creation error: {e}")
        return ""


def _post_report(filed: list[dict], skipped: list[dict], window_days: int) -> None:
    """One Slack summary of the pass, to the agent log channel."""
    channel = os.environ.get("SLACK_CHANNEL_AGENT_LOG", "")
    if not channel:
        return
    try:
        from slack_sdk import WebClient
        wc = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))

        lines = [
            ":books: *Help centre gap report*",
            f"Window: last {window_days} days",
            "",
        ]
        if filed:
            lines.append(f"*Filed {len(filed)} topic(s):*")
            for t in filed:
                url = (
                    f"https://app.clickup.com/t/{t['clickup_task_id']}"
                    if t.get("clickup_task_id") else ""
                )
                label = t["title"]
                lines.append(
                    f"• {t['signal_count']} signals: "
                    + (f"<{url}|{label}>" if url else label)
                    + f"  _{t['kind']}_"
                )
        else:
            lines.append("No new topics crossed the threshold.")

        if skipped:
            lines.append("")
            lines.append(
                f"_Already filed, still active: "
                f"{', '.join(s['title'] for s in skipped[:8])}_"
            )

        wc.chat_postMessage(channel=channel, text="\n".join(lines))
    except Exception as e:
        print(f"[KB GAP] Slack report failed: {e}")


# ---------------------------------------------------------------------------
# 4. Entry point
# ---------------------------------------------------------------------------

def run_kb_gap_clustering(
    window_days: int = CLUSTER_WINDOW_DAYS,
    max_topics: int = 5,
    dry_run: bool = False,
) -> dict:
    """Monthly pass: cluster gap signals into topics and file the top ones.

    max_topics caps how many ClickUp tasks one pass creates. Five is a
    month's worth of article writing. Filing twenty would mean nobody
    writes any of them.
    """
    print(
        f"[KB GAP] clustering pass starting"
        f" (window={window_days}d, max_topics={max_topics},"
        f" dry_run={dry_run})"
    )

    signals = _load_signals(window_days)
    analysed = _load_analysed_education(window_days)
    if not signals and not analysed:
        print("[KB GAP] no signals in window, nothing to do")
        return {"topics": 0, "filed": 0, "skipped": 0}

    topics = _cluster_signals(signals, analysed)
    print(f"[KB GAP] {len(signals)} fingerprints clustered into {len(topics)} topics")

    already = _seen_topics()
    filed, skipped = [], []

    for topic in topics:
        if len(filed) >= max_topics:
            break
        if topic["signal_count"] < MIN_SIGNALS_FOR_TOPIC:
            continue

        prior = already.get(topic["topic_key"])
        if prior and prior.get("status") != "published":
            # Already on someone's plate. Bump the count so the task
            # reflects reality, but do not file a duplicate.
            skipped.append(topic)
            if not dry_run:
                _save_topic(topic, prior.get("clickup_task_id") or "", "open")
            continue

        coverage = _kb_coverage(topic["question"] or topic["title"])
        kind = (
            "refresh" if coverage["stale"]
            else "rewrite" if coverage["covered"]
            else "new article"
        )

        if dry_run:
            filed.append({**topic, "clickup_task_id": "", "kind": kind})
            continue

        draft = _draft_article(topic, analysed, signals)
        task_id = _create_topic_task(topic, draft, coverage)
        _save_topic(topic, task_id, "open")
        filed.append({**topic, "clickup_task_id": task_id, "kind": kind})

    _post_report(filed, skipped, window_days)

    summary = {
        "topics": len(topics),
        "filed": len(filed),
        "skipped": len(skipped),
        "signals": len(signals),
        "analysed_tasks": len(analysed),
        "filed_topics": [
            {
                "topic_key": t["topic_key"],
                "title": t["title"],
                "signal_count": t["signal_count"],
                "clickup_task_id": t.get("clickup_task_id", ""),
                "kind": t["kind"],
            }
            for t in filed
        ],
    }
    print(f"[KB GAP] pass complete: {summary['filed']} filed, {summary['skipped']} skipped")
    return summary


def run_monthly_kb_gap_pass() -> dict:
    """Scheduler entry point. Claims the run so a restart cannot double-file."""
    from database import claim_sweeper_run, finish_sweeper_run

    run_key = f"kb_gap_{datetime.now(timezone.utc):%Y_%m}"
    if not claim_sweeper_run(run_key):
        print(f"[KB GAP] {run_key} already claimed, skipping")
        return {"skipped": True, "reason": "already_claimed"}

    dry_run = os.environ.get("KB_GAP_DRY_RUN", "").lower() == "true"
    try:
        summary = run_kb_gap_clustering(dry_run=dry_run)
    except Exception as e:
        print(f"[KB GAP] pass failed: {e}")
        summary = {"error": str(e)}

    finish_sweeper_run(run_key, summary)
    return summary


if __name__ == "__main__":
    import sys
    print(json.dumps(
        run_kb_gap_clustering(dry_run="--dry-run" in sys.argv),
        indent=2,
        default=str,
    ))
