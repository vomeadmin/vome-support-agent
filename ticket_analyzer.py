"""
ticket_analyzer.py

Batch analysis of Zoho Desk tickets to build the Vome Knowledge Book.

This script:
1. Fetches all tickets from Zoho Desk in batches (respecting rate limits)
2. Extracts full conversation threads (client messages + Sam's responses)
3. Runs Claude analysis on each ticket to extract patterns
4. Stores results in PostgreSQL (analyzed_tickets table)
5. After all tickets are analyzed, generates the Knowledge Book sections

The Knowledge Book is a living training document organized by category:
- Sam's voice and tone patterns
- Category-specific response patterns (bugs, billing, auth, etc.)
- Common scenarios with example responses
- FAQ entries (what to say for common questions)
- Decision patterns (when to escalate, when to answer directly)

Run this script to process historical tickets:
    python ticket_analyzer.py              # bounded run
    python ticket_analyzer.py --all        # no ceiling
    python ticket_analyzer.py --limit 300

It tracks progress in the database, so it's safe to restart --
it will pick up where it left off. run_weekly_knowledge_refresh() is the
scheduled entry point: it analyses newly closed tickets, mines newly
closed ClickUp tasks (clickup_knowledge.py), regenerates the book and
drops the read-side cache.

Postgres is the authoritative store. `knowledge_sections` is what
knowledge.py reads into live prompts; the markdown under knowledge_book/
is a local debugging convenience only, because the app runs on an
ephemeral filesystem where generated files do not survive a restart.
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

import anthropic

from agent import (
    _zoho_desk_call,
    _unwrap_mcp_result,
    ZOHO_ORG_ID,
    TEAM_EMAILS,
)
from database import _get_engine, DATABASE_URL, init_db
from model_config import SUPPORT_MODEL
import knowledge_synthesis as ks

# Fix Windows encoding
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

_client = anthropic.Anthropic()

KNOWLEDGE_BOOK_DIR = Path(__file__).parent / "knowledge_book"
KNOWLEDGE_BOOK_DIR.mkdir(exist_ok=True)

# Rate limiting: pause between Zoho API calls
ZOHO_BATCH_SIZE = 50
ZOHO_DELAY_BETWEEN_CALLS = 1.5  # seconds
ZOHO_DELAY_BETWEEN_BATCHES = 10  # seconds

# Claude analysis: pause between calls to avoid rate limits
CLAUDE_DELAY = 2  # seconds


# =====================================================================
# Phase 1: Fetch tickets from Zoho
# =====================================================================

def fetch_all_ticket_ids() -> list[dict]:
    """Fetch all ticket IDs and basic metadata from Zoho Desk.

    Returns list of {id, ticketNumber, subject, status, createdTime}.
    """
    all_tickets = []
    offset = 0

    while True:
        print(f"[FETCH] Fetching tickets offset={offset}...")

        # Zoho MCP requires departmentId to list tickets
        # Explicitly list all statuses to get closed/resolved tickets
        result = _zoho_desk_call("ZohoDesk_getTickets", {
            "query_params": {
                "orgId": str(ZOHO_ORG_ID),
                "departmentId": "569440000000006907",
                "status": "Open,Closed,On Hold,Escalated",
                "from": str(offset),
                "limit": "100",
                "sortBy": "createdTime",
            },
        })

        raw = _unwrap_mcp_result(result)
        if not raw:
            print("[FETCH] No more results or API error")
            break

        tickets = []
        if isinstance(raw, dict):
            tickets = raw.get("data", [])
        elif isinstance(raw, list):
            tickets = raw

        if not tickets:
            print("[FETCH] Empty batch -- done")
            break

        for t in tickets:
            all_tickets.append({
                "id": str(t.get("id", "")),
                "ticketNumber": str(t.get("ticketNumber", "")),
                "subject": t.get("subject", ""),
                "status": t.get("status", ""),
                "createdTime": t.get("createdTime", ""),
            })

        print(f"[FETCH] Got {len(tickets)} tickets (total: {len(all_tickets)})")

        if len(tickets) < 100:
            break

        offset += 100
        time.sleep(ZOHO_DELAY_BETWEEN_CALLS)

    print(f"[FETCH] Total tickets found: {len(all_tickets)}")
    return all_tickets


def fetch_ticket_detail(ticket_id: str) -> dict | None:
    """Fetch full ticket details + conversation thread."""
    # Get ticket details
    ticket_result = _zoho_desk_call("ZohoDesk_getTicket", {
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {
            "orgId": str(ZOHO_ORG_ID),
            "include": "contacts,assignee",
        },
    })
    time.sleep(ZOHO_DELAY_BETWEEN_CALLS)

    ticket = _unwrap_mcp_result(ticket_result)
    if not ticket or not isinstance(ticket, dict):
        return None

    # Get conversations
    conv_result = _zoho_desk_call("ZohoDesk_getTicketConversations", {
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {
            "orgId": str(ZOHO_ORG_ID),
            "from": 0,
            "limit": 100,
        },
    })
    time.sleep(ZOHO_DELAY_BETWEEN_CALLS)

    conversations = _unwrap_mcp_result(conv_result)

    return {
        "ticket": ticket,
        "conversations": conversations,
    }


def extract_conversation_thread(detail: dict) -> dict:
    """Extract a clean conversation thread from ticket detail.

    Returns {
        subject, client_name, client_email, status,
        messages: [{role: "client"|"sam"|"agent", content, timestamp}],
        has_sam_response, turn_count, language
    }
    """
    ticket = detail.get("ticket", {})
    conversations = detail.get("conversations", {})

    subject = ticket.get("subject", "")
    contact = ticket.get("contact", {}) or {}
    client_name = (
        f"{contact.get('firstName', '')} {contact.get('lastName', '')}"
    ).strip() or "Unknown"
    client_email = contact.get("email", "")
    status = ticket.get("status", "")

    # Parse conversation entries
    messages = []
    entries = []
    if isinstance(conversations, dict):
        entries = conversations.get("data", [])
    elif isinstance(conversations, list):
        entries = conversations

    # Add original ticket body as first message
    raw_desc = ticket.get("description") or ""
    clean_desc = re.sub(r"<[^>]+>", "", raw_desc).strip()
    if clean_desc:
        messages.append({
            "role": "client",
            "content": clean_desc,
            "timestamp": ticket.get("createdTime", ""),
        })

    has_sam_response = False

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("isDescriptionThread"):
            continue

        content = entry.get("content") or ""
        clean_content = re.sub(r"<[^>]+>", "", content).strip()
        if not clean_content:
            continue

        author = entry.get("author", {}) or {}
        author_email = (author.get("email") or "").lower()
        author_type = (author.get("type") or "").upper()
        direction = entry.get("direction", "")

        if author_type == "AGENT" or direction == "out":
            if author_email in {
                e.lower() for e in TEAM_EMAILS
            }:
                role = "sam"
                has_sam_response = True
            else:
                role = "agent"
        else:
            role = "client"

        messages.append({
            "role": role,
            "content": clean_content,
            "timestamp": entry.get("createdTime", ""),
        })

    # Detect language
    all_text = " ".join(m["content"] for m in messages[:3])
    language = "fr" if _is_french(all_text) else "en"

    return {
        "subject": subject,
        "client_name": client_name,
        "client_email": client_email,
        "status": status,
        "messages": messages,
        "has_sam_response": has_sam_response,
        "turn_count": len(messages),
        "language": language,
    }


def _is_french(text: str) -> bool:
    """Basic French detection."""
    fr_words = {
        "bonjour", "merci", "je", "nous", "vous",
        "est", "sont", "pour", "avec", "dans",
        "qui", "que", "les", "des", "une",
        "sur", "pas", "mon", "notre", "votre",
    }
    words = text.lower().split()
    if len(words) < 5:
        return False
    fr_count = sum(1 for w in words if w in fr_words)
    return fr_count / len(words) > 0.15


# =====================================================================
# Phase 2: Analyze tickets with Claude
# =====================================================================

ANALYSIS_PROMPT = """You are analyzing a Zoho Desk support ticket from Vome, a volunteer management CRM platform. Your goal is to extract patterns from how Sam (the CEO) handles support.

TICKET SUBJECT: {subject}
CLIENT: {client_name} ({client_email})
STATUS: {status}
LANGUAGE: {language}

CONVERSATION:
{conversation}

Analyze this ticket and return a JSON object with these fields:

{{
  "category": "bug|feature_request|billing|auth|how_to|data_issue|account_management|unclear",
  "module": "the Vome module this relates to (scheduling, opportunities, forms, etc.) or 'general'",
  "complexity": "simple|moderate|complex",
  "resolution_type": "answered_directly|created_ticket|escalated|redirected_to_org|template_used|account_action",
  "was_deflectable": true/false (could a KB article have answered this?),
  "suggested_faq_topic": "short topic string if this should be an FAQ, or null",
  "sam_tone_notes": "specific observations about Sam's tone, phrasing, or approach in THIS ticket",
  "sam_response_pattern": "what Sam did step by step (e.g. 'confirmed account exists, suggested password reset, offered to bypass auth')",
  "key_phrases": ["list of notable phrases Sam used that feel personal/human"],
  "follow_up_questions_asked": ["list of follow-up questions Sam asked to gather more info"],
  "resolution_summary": "one sentence describing how this was resolved",
  "training_value": "high|medium|low (how useful is this as a training example?)",
  "training_notes": "why this ticket is valuable for training, or what pattern it demonstrates"
}}

Be specific about Sam's actual language and approach. Don't generalize -- quote his words when notable. If Sam didn't respond, analyze the ticket content and suggest how it should have been handled.

Return ONLY the JSON object, no other text."""


def analyze_ticket(thread: dict) -> dict | None:
    """Run Claude analysis on a single ticket thread."""
    # Build conversation text
    conv_lines = []
    for msg in thread["messages"]:
        label = {
            "client": f"CLIENT ({thread['client_name']})",
            "sam": "SAM",
            "agent": "AGENT",
        }.get(msg["role"], msg["role"].upper())
        conv_lines.append(f"[{label}]: {msg['content']}")

    conversation_text = "\n\n".join(conv_lines)

    # Truncate very long conversations
    if len(conversation_text) > 8000:
        conversation_text = conversation_text[:8000] + "\n\n[... truncated]"

    prompt = ANALYSIS_PROMPT.format(
        subject=thread["subject"],
        client_name=thread["client_name"],
        client_email=thread["client_email"],
        status=thread["status"],
        language=thread["language"],
        conversation=conversation_text,
    )

    try:
        response = _client.messages.create(
            model=SUPPORT_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        # Parse JSON from response
        # Handle possible markdown wrapping
        if text.startswith("```"):
            text = re.sub(
                r"^```(?:json)?\s*\n?", "", text
            )
            text = re.sub(r"\n?```\s*$", "", text)

        return json.loads(text)
    except Exception as e:
        print(f"[ANALYZE] Claude analysis failed: {e}")
        return None


# =====================================================================
# Phase 3: Store results
# =====================================================================

def is_ticket_analyzed(ticket_id: str) -> bool:
    """Check if a ticket has already been analyzed."""
    if not DATABASE_URL:
        return False
    try:
        engine = _get_engine()
        from sqlalchemy import text as sql_text
        with engine.connect() as conn:
            result = conn.execute(
                sql_text(
                    "SELECT 1 FROM analyzed_tickets "
                    "WHERE ticket_id = :tid"
                ),
                {"tid": ticket_id},
            )
            return result.first() is not None
    except Exception:
        return False


def save_analysis(
    ticket_id: str,
    ticket_number: str,
    subject: str,
    thread: dict,
    analysis: dict,
):
    """Save ticket analysis to the database."""
    if not DATABASE_URL:
        print("[DB] DATABASE_URL not set -- skipping save")
        return
    try:
        engine = _get_engine()
        from sqlalchemy import text as sql_text
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(
                sql_text("""
                    INSERT INTO analyzed_tickets
                        (ticket_id, ticket_number, subject,
                         category, module, language, turn_count,
                         has_sam_response, analysis, analyzed_at)
                    VALUES
                        (:tid, :tnum, :subj, :cat, :mod, :lang,
                         :turns, :has_sam, CAST(:analysis AS jsonb),
                         :now)
                    ON CONFLICT (ticket_id) DO UPDATE SET
                        analysis = CAST(EXCLUDED.analysis AS jsonb),
                        analyzed_at = EXCLUDED.analyzed_at
                """),
                {
                    "tid": ticket_id,
                    "tnum": ticket_number,
                    "subj": subject,
                    "cat": analysis.get("category", ""),
                    "mod": analysis.get("module", ""),
                    "lang": thread.get("language", "en"),
                    "turns": thread.get("turn_count", 0),
                    "has_sam": (
                        "true" if thread.get("has_sam_response")
                        else "false"
                    ),
                    "analysis": json.dumps(analysis),
                    "now": now,
                },
            )
    except Exception as e:
        print(f"[DB] Failed to save analysis: {e}")


def get_analysis_stats() -> dict:
    """Get counts of analyzed tickets by category."""
    if not DATABASE_URL:
        return {}
    try:
        engine = _get_engine()
        from sqlalchemy import text as sql_text
        with engine.connect() as conn:
            result = conn.execute(
                sql_text(
                    "SELECT category, COUNT(*) as cnt "
                    "FROM analyzed_tickets "
                    "GROUP BY category ORDER BY cnt DESC"
                )
            )
            return {
                row["category"]: row["cnt"]
                for row in result.mappings()
            }
    except Exception:
        return {}


def get_all_analyses() -> list[dict]:
    """Fetch all analyzed tickets from the database."""
    if not DATABASE_URL:
        return []
    try:
        engine = _get_engine()
        from sqlalchemy import text as sql_text
        with engine.connect() as conn:
            result = conn.execute(
                sql_text(
                    "SELECT * FROM analyzed_tickets "
                    "ORDER BY analyzed_at"
                )
            )
            rows = []
            for row in result.mappings():
                analysis = row["analysis"]
                if isinstance(analysis, str):
                    analysis = json.loads(analysis)
                rows.append({
                    "ticket_id": row["ticket_id"],
                    "ticket_number": row["ticket_number"],
                    "subject": row["subject"],
                    "category": row["category"],
                    "module": row["module"],
                    "language": row["language"],
                    "turn_count": row["turn_count"],
                    "has_sam_response": (
                        row["has_sam_response"] == "true"
                    ),
                    "analysis": analysis,
                })
            return rows
    except Exception as e:
        print(f"[DB] Failed to fetch analyses: {e}")
        return []


# =====================================================================
# Phase 4: Generate Knowledge Book
# =====================================================================

BOOK_SYNTHESIS_PROMPT = """You are creating a section of the Vome Support Knowledge Book -- a living training guide for support agents (both human and AI) based on {ticket_count} real support tickets handled by Sam, the CEO.

SECTION: {section_title}
CATEGORY: {category}

Here are the analyzed tickets for this section:

{ticket_summaries}

Based on these real tickets, write a comprehensive training section that includes:

1. **Overview** -- What this category covers, how common it is
2. **Sam's Approach** -- How Sam handles these tickets (tone, style, specific phrases he uses)
3. **Common Scenarios** -- The specific situations that come up, with Sam's actual response patterns
4. **Example Responses** -- 3-5 templated responses based on Sam's real language (not generic -- use his actual phrases and style)
5. **Key Decision Points** -- When to answer directly vs create a ticket vs escalate
6. **Red Flags** -- Things to watch for that need special handling
7. **FAQ Entries** -- Common questions with answers based on how Sam handles them

IMPORTANT:
- Use Sam's actual language and phrases where possible
- Be specific, not generic. "Sam says 'Let me take a look'" is better than "Respond warmly"
- Include both English and French response patterns if French tickets exist
- Note any patterns that have changed over time (features that were added, issues that were fixed)
- Flag any responses that reference features or behaviors that may have changed

Write in markdown format. This will be read by both humans and AI agents."""


ENGINEERING_SYNTHESIS_PROMPT = """You are writing the "How these issues get resolved" section of the Vome Support Knowledge Book, from {task_count} finished engineering tasks.

Each entry below is a real closed task: what a client reported, what it turned out to be, and how it was resolved.

{task_summaries}

Write a training section that a support agent (human or AI) reads before drafting a reply. Include:

1. **Recurring issues** -- the problems that come up again and again, what they look like from the client's side, and what they usually turn out to be
2. **Diagnostic questions that work** -- the questions that actually narrow these down, grouped by symptom
3. **Safe client-facing explanations** -- how to describe each recurring cause to a client, in plain warm language with zero internal detail
4. **User education patterns** -- the cases that were not bugs at all, and how the client was walked through the right way to do it
5. **Where to be careful** -- issues where the obvious answer is wrong, or where support should confirm before promising anything
6. **Article gaps** -- topics that keep generating tickets and have no help center article

Rules:
- Ground every claim in the entries above. If something appears once, say so rather than presenting it as a pattern.
- Never include internal engineering detail that would be unsafe to repeat to a client: no code, no file names, no engineer names.
- Do not promise fixes or dates. A past fix does not mean a current one is coming.
- Be specific. "Shifts appear an hour off when the org timezone and the shift timezone disagree" beats "timezone issues occur".

Write in markdown. Keep it under 2500 words."""


ENGINEERING_MAP_PROMPT = """You are extracting raw observations from one batch of finished Vome engineering tasks, for the "How these issues get resolved" section of the support knowledge book. This is batch {index} of {total}; a later pass merges every batch.

TASKS IN THIS BATCH:

{task_summaries}

Extract, in compact markdown, only what these tasks actually show:

- **Issues**: what the client experienced, one line each, grouped when two tasks are the same underlying problem
- **Causes**: what each turned out to be. Write "not stated" where the task never says.
- **Diagnostic questions**: the questions that narrowed it down, phrased so support could ask them again
- **Safe explanations**: how the cause was described to the client, with no internal detail
- **User education**: the cases that were not bugs, and what the client was actually shown
- **Careful**: where the obvious answer was wrong
- **Article gaps**: topics flagged as needing a help center article

Rules:
- Only what is in these tasks. Where a cause is not stated, say so rather than inferring one.
- Mark repetition, for example "(4 tasks)". The merge pass uses that to tell a pattern from a one-off.
- No code, no file names, no engineer names.
- Be terse. Notes, not prose.

Return only the notes."""


ENGINEERING_REDUCE_PROMPT = """You are writing the "How these issues get resolved" section of the Vome Support Knowledge Book.

Below are observations extracted from {batch_count} batches spanning {task_count} finished engineering tasks.

{notes}

Merge them into one training section that a support agent reads before drafting a reply:

1. **Recurring issues** -- the problems that come up again and again, what they look like from the client's side, and what they usually turn out to be
2. **Diagnostic questions that work** -- grouped by symptom
3. **Safe client-facing explanations** -- each recurring cause in plain warm language, zero internal detail
4. **User education patterns** -- the cases that were not bugs, and how the client was walked through it
5. **Where to be careful** -- where the obvious answer is wrong, or support should confirm before promising
6. **Article gaps** -- topics that keep generating tickets with no help center article

Rules:
- Merge duplicates and lead with what appears most often. Where the notes carry counts, weight by them.
- Keep a one-off only if it is genuinely instructive, and mark it as rare.
- Never include detail unsafe to repeat to a client: no code, no file names, no engineer names.
- Do not promise fixes or dates. A past fix does not mean a current one is coming.
- Be specific. "Shifts appear an hour off when the org timezone and the shift timezone disagree" beats "timezone issues occur".

Write in markdown. Keep it under 2500 words."""


def _task_summary(t: dict) -> str:
    """One closed task rendered for synthesis."""
    a = t["analysis"]
    questions = a.get("diagnostic_questions") or []
    return (
        f"Task: {t['name']}\n"
        f"  Category: {a.get('category', '?')} / "
        f"module: {a.get('module', '?')}\n"
        f"  Client saw: {a.get('problem_statement', 'N/A')}\n"
        f"  Root cause: {a.get('root_cause', 'not stated')}\n"
        f"  Resolution: {a.get('resolution_type', '?')} -- "
        f"{a.get('resolution_summary', 'N/A')}\n"
        f"  Told the client: {a.get('client_facing_explanation', '')}\n"
        f"  Diagnostic questions: {', '.join(questions)}\n"
        f"  Recurrence risk: {a.get('recurrence_risk', '?')}\n"
        f"  Article candidate: {a.get('kb_article_candidate', False)} "
        f"({a.get('kb_article_topic') or 'none'})\n"
        f"  Training notes: {a.get('training_notes', '')}"
    )


def _generate_engineering_section() -> int:
    """Synthesise closed ClickUp tasks into the engineering_resolutions
    section. Returns the number of tasks it was built from.

    This is the half of the knowledge book that Zoho cannot provide: the
    diagnosis, and what the issue actually turned out to be.
    """
    try:
        from clickup_knowledge import get_all_task_analyses
    except Exception as e:
        print(f"[BOOK] clickup_knowledge unavailable: {e}")
        return 0

    task_analyses = get_all_task_analyses()
    if not task_analyses:
        print("[BOOK] No mined ClickUp tasks yet -- skipping engineering section")
        return 0

    print(
        f"[BOOK] Generating engineering section from "
        f"{len(task_analyses)} closed tasks..."
    )

    selected = ks.rank_and_select(
        task_analyses,
        value_of=lambda t: t["analysis"].get("training_value"),
        recency_of=lambda t: t.get("closed_at"),
        module_of=lambda t: t["analysis"].get("module"),
    )
    summaries = [_task_summary(t) for t in selected]

    content = ks.map_reduce_section(
        summaries,
        section_title="How These Issues Get Resolved",
        direct_prompt=lambda joined: ENGINEERING_SYNTHESIS_PROMPT.format(
            task_count=len(selected),
            task_summaries=joined,
        ),
        map_prompt=lambda joined, i, n: ENGINEERING_MAP_PROMPT.format(
            index=i, total=n, task_summaries=joined,
        ),
        reduce_prompt=lambda notes, batches: ENGINEERING_REDUCE_PROMPT.format(
            batch_count=batches,
            task_count=len(selected),
            notes=notes,
        ),
        client=_client,
        model=SUPPORT_MODEL,
        delay=CLAUDE_DELAY,
    )

    if not content:
        print("[BOOK] Failed to generate engineering section")
        return 0

    header = ks.coverage_note(len(selected), len(task_analyses))
    try:
        (KNOWLEDGE_BOOK_DIR / "engineering_resolutions.md").write_text(
            "# How These Issues Get Resolved\n\n"
            f"{header}\n"
            f"*Last updated: "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
            f"{content}",
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[BOOK] Could not write engineering_resolutions.md: {e}")

    _save_knowledge_section(
        "engineering_resolutions",
        "How These Issues Get Resolved",
        f"{header}\n\n{content}",
        len(task_analyses),
    )
    print(
        f"[BOOK] Written: engineering_resolutions "
        f"({len(selected)}/{len(task_analyses)})"
    )
    return len(task_analyses)


BOOK_MAP_PROMPT = """You are extracting raw observations from one batch of real Vome support tickets, for the "{section_title}" section of the support knowledge book. This is batch {index} of {total}; a later pass merges every batch into the final section.

TICKETS IN THIS BATCH:

{ticket_summaries}

Extract, in compact markdown, only what these tickets actually show:

- **Scenarios**: the distinct situations that appear, one line each
- **Sam's moves**: what he does step by step in each, quoting his wording where it is distinctive
- **Phrases**: exact phrases worth reusing
- **Follow-up questions**: what he asks to narrow things down
- **Decision points**: what made him answer directly vs create a ticket vs escalate
- **Red flags**: anything that needed special handling
- **FAQ candidates**: questions that recur

Rules:
- Only what is in these tickets. Do not generalise past them and do not invent examples.
- Mark repetition, for example "(3 tickets)". The merge pass uses that to tell a pattern from a one-off.
- Be terse. Notes, not prose.

Return only the notes."""


BOOK_REDUCE_PROMPT = """You are writing the "{section_title}" section of the Vome Support Knowledge Book, a living training guide for support agents (human and AI), covering the category "{category}".

Below are observations extracted from {batch_count} batches spanning {ticket_count} real tickets handled by Sam, the CEO.

{notes}

Merge them into one training section:

1. **Overview** -- what this category covers, how common it is
2. **Sam's Approach** -- tone, style, the specific phrases he uses
3. **Common Scenarios** -- the situations that come up, with his response patterns
4. **Example Responses** -- 3-5 templates in his real language, not generic
5. **Key Decision Points** -- when to answer directly vs create a ticket vs escalate
6. **Red Flags** -- what needs special handling
7. **FAQ Entries** -- common questions answered in his style

Rules:
- Merge duplicates across batches and lead with what appears most often. Where the notes carry counts, weight by them.
- Keep a one-off only if it is genuinely instructive, and mark it as rare.
- Use Sam's actual language. "Sam says 'Let me take a look'" beats "respond warmly".
- Include French patterns if the notes show them.
- Flag anything referencing features or behaviour that may since have changed.

Write in markdown, for both humans and AI agents."""


def _ticket_summary(t: dict) -> str:
    """One ticket rendered for synthesis."""
    a = t["analysis"]
    return (
        f"Ticket #{t['ticket_number']}: {t['subject']}\n"
        f"  Category: {a.get('category', '?')}\n"
        f"  Module: {a.get('module', '?')}\n"
        f"  Resolution: {a.get('resolution_type', '?')}\n"
        f"  Sam's approach: {a.get('sam_response_pattern', 'N/A')}\n"
        f"  Key phrases: {', '.join(a.get('key_phrases', []))}\n"
        f"  Follow-ups asked: "
        f"{', '.join(a.get('follow_up_questions_asked', []))}\n"
        f"  Resolution: {a.get('resolution_summary', 'N/A')}\n"
        f"  Training value: {a.get('training_value', '?')}\n"
        f"  Training notes: {a.get('training_notes', '')}\n"
        f"  Tone notes: {a.get('sam_tone_notes', '')}\n"
        f"  FAQ topic: {a.get('suggested_faq_topic', 'none')}\n"
        f"  Was deflectable: {a.get('was_deflectable', False)}\n"
        f"  Language: {t['language']}"
    )


def generate_knowledge_book():
    """Generate the full Knowledge Book from analyzed tickets and tasks."""
    analyses = get_all_analyses()
    if not analyses:
        # Closed ClickUp tasks can still carry the whole book on their
        # own, so an empty ticket table is not a reason to bail out.
        print("[BOOK] No analyzed tickets found")
        _generate_engineering_section()
        _refresh_read_cache()
        return

    print(f"[BOOK] Generating Knowledge Book from {len(analyses)} tickets...")

    # Group by category
    by_category = {}
    for a in analyses:
        cat = a["category"] or "uncategorized"
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(a)

    # Category display names
    category_titles = {
        "bug": "Bug Reports",
        "feature_request": "Feature Requests",
        "billing": "Billing & Account Questions",
        "auth": "Authentication & Access Issues",
        "how_to": "How-To Questions",
        "data_issue": "Data Issues",
        "account_management": "Account Management",
        "unclear": "Unclear or Complex Tickets",
        "uncategorized": "Uncategorized",
    }

    sections = {}

    for category, tickets in by_category.items():
        title = category_titles.get(category, category.title())
        print(
            f"[BOOK] Generating section: {title} "
            f"({len(tickets)} tickets)..."
        )

        # Rank by training value then recency, and round-robin across
        # modules, so a section reflects the spread of the category
        # rather than whichever entries happened to be analysed first.
        selected = ks.rank_and_select(
            tickets,
            value_of=lambda t: t["analysis"].get("training_value"),
            # Zoho ticket numbers increase over time, so they double as
            # the recency key. analyzed_tickets carries no ticket date.
            recency_of=lambda t: t.get("ticket_number"),
            module_of=lambda t: t["analysis"].get("module"),
        )
        summaries = [_ticket_summary(t) for t in selected]

        section_content = ks.map_reduce_section(
            summaries,
            section_title=title,
            direct_prompt=lambda joined: BOOK_SYNTHESIS_PROMPT.format(
                ticket_count=len(selected),
                section_title=title,
                category=category,
                ticket_summaries=joined,
            ),
            map_prompt=lambda joined, i, n: BOOK_MAP_PROMPT.format(
                section_title=title,
                index=i,
                total=n,
                ticket_summaries=joined,
            ),
            reduce_prompt=lambda notes, batches: BOOK_REDUCE_PROMPT.format(
                section_title=title,
                category=category,
                batch_count=batches,
                ticket_count=len(selected),
                notes=notes,
            ),
            client=_client,
            model=SUPPORT_MODEL,
            delay=CLAUDE_DELAY,
        )

        if not section_content:
            print(f"[BOOK] Failed to generate {title}")
            continue

        header = ks.coverage_note(len(selected), len(tickets))
        sections[category] = {
            "title": title,
            "content": section_content,
            "ticket_count": len(tickets),
        }

        try:
            (KNOWLEDGE_BOOK_DIR / f"{category}.md").write_text(
                f"# {title}\n\n"
                f"{header}\n"
                f"*Last updated: "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
                f"{section_content}",
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[BOOK] Could not write {category}.md: {e}")

        # ticket_count stays the full category size so the status
        # endpoint reports the real corpus, not the sampled slice.
        _save_knowledge_section(
            category, title, f"{header}\n\n{section_content}", len(tickets)
        )
        print(f"[BOOK] Written: {title} ({len(selected)}/{len(tickets)})")
        time.sleep(CLAUDE_DELAY)

    # Generate Sam's Voice guide (cross-cutting)
    _generate_voice_guide(analyses)

    # Generate the engineering resolutions section from closed ClickUp
    # tasks. This is the source that carries the actual diagnosis.
    _generate_engineering_section()

    # Generate summary
    _generate_summary(sections, analyses)

    _refresh_read_cache()

    print("[BOOK] Knowledge Book generation complete!")


def _refresh_read_cache():
    """Drop knowledge.py's TTL cache so the new book goes live at once."""
    try:
        import knowledge
        knowledge.clear_cache()
        print("[BOOK] Read-side cache cleared")
    except Exception as e:
        print(f"[BOOK] Could not clear read cache: {e}")


def _generate_voice_guide(analyses: list[dict]):
    """Generate the cross-cutting 'Sam's Voice' style guide."""
    # Collect all tone notes and key phrases
    tone_notes = []
    key_phrases = []
    follow_ups = []

    for a in analyses:
        analysis = a["analysis"]
        if analysis.get("sam_tone_notes"):
            tone_notes.append(analysis["sam_tone_notes"])
        key_phrases.extend(analysis.get("key_phrases", []))
        follow_ups.extend(
            analysis.get("follow_up_questions_asked", [])
        )

    # Deduplicate
    key_phrases = list(set(key_phrases))[:100]
    follow_ups = list(set(follow_ups))[:50]

    prompt = f"""Based on {len(analyses)} analyzed support tickets, create "Sam's Voice" -- a comprehensive style guide that captures how Sam (CEO of Vome) communicates with support customers.

TONE OBSERVATIONS FROM TICKETS:
{chr(10).join(f"- {n}" for n in tone_notes[:80])}

KEY PHRASES SAM USES:
{chr(10).join(f"- {p}" for p in key_phrases)}

FOLLOW-UP QUESTIONS SAM ASKS:
{chr(10).join(f"- {q}" for q in follow_ups)}

Write a style guide covering:

1. **Overall Tone** -- How Sam sounds (warm? direct? casual? formal?)
2. **Greeting Patterns** -- How Sam opens responses
3. **Closing Patterns** -- How Sam signs off
4. **Empathy Patterns** -- How Sam acknowledges frustration or problems
5. **Question Style** -- How Sam asks for more info (direct? soft? embedded?)
6. **Action Language** -- How Sam describes what he's doing ("I just went ahead and...", "Let me take a look...")
7. **Signature Phrases** -- The phrases that are distinctly Sam's
8. **Things Sam Never Says** -- Patterns to avoid
9. **French Communication** -- How Sam handles French-language tickets
10. **Escalation Language** -- How Sam transitions to "I need to look into this further"

Be extremely specific. Quote actual phrases. This guide should allow someone (or an AI) to write responses that are indistinguishable from Sam's."""

    try:
        response = _client.messages.create(
            model=SUPPORT_MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.content[0].text

        filepath = KNOWLEDGE_BOOK_DIR / "sams_voice.md"
        filepath.write_text(
            "# Sam's Voice -- Style Guide\n\n"
            f"*Generated from {len(analyses)} tickets "
            f"| Last updated: "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
            f"{content}",
            encoding="utf-8",
        )
        print("[BOOK] Written: sams_voice.md")

        _save_knowledge_section(
            "sams_voice",
            "Sam's Voice -- Style Guide",
            content,
            len(analyses),
        )

    except Exception as e:
        print(f"[BOOK] Failed to generate voice guide: {e}")


def _generate_summary(
    sections: dict,
    analyses: list[dict],
):
    """Generate the Knowledge Book summary/index."""
    stats = get_analysis_stats()

    lines = [
        "# Vome Support Knowledge Book",
        "",
        f"*Auto-generated from {len(analyses)} Zoho Desk tickets*",
        f"*Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
        "",
        "## Overview",
        "",
        f"Total tickets analyzed: **{len(analyses)}**",
        f"Tickets with Sam's response: "
        f"**{sum(1 for a in analyses if a['has_sam_response'])}**",
        "",
        "## Tickets by Category",
        "",
    ]

    for cat, count in sorted(
        stats.items(), key=lambda x: -x[1]
    ):
        lines.append(f"- **{cat}**: {count} tickets")

    lines.extend([
        "",
        "## Sections",
        "",
    ])

    for cat, info in sections.items():
        lines.append(
            f"- [{info['title']}]({cat}.md) "
            f"({info['ticket_count']} tickets)"
        )

    lines.extend([
        "",
        "- [Sam's Voice -- Style Guide](sams_voice.md)",
        "",
        "## How to Use This Book",
        "",
        "### For AI Agents",
        "The intake prompt (`intake_prompt.md`) loads the "
        "Sam's Voice guide and relevant category sections "
        "at runtime so the agent can mirror Sam's tone "
        "and approach when answering new support conversations.",
        "",
        "### For Human Agents",
        "Read the Sam's Voice guide first to understand the "
        "tone and style. Then reference the category-specific "
        "sections for common scenarios and response templates.",
        "",
        "### Keeping It Current",
        "This book is regenerated periodically as new tickets "
        "are processed. Sections are versioned -- when a "
        "feature changes (e.g., 'we don't have this feature' "
        "becomes 'we now have this feature'), the next "
        "regeneration will reflect the updated responses.",
        "",
        "To flag outdated content, update the relevant FAQ "
        "entries or response templates, and the next analysis "
        "run will incorporate the changes.",
    ])

    filepath = KNOWLEDGE_BOOK_DIR / "_summary.md"
    filepath.write_text("\n".join(lines), encoding="utf-8")
    print("[BOOK] Written: _summary.md")


def _save_knowledge_section(
    section_key: str,
    title: str,
    content: str,
    ticket_count: int,
):
    """Save a knowledge section to the database."""
    if not DATABASE_URL:
        return
    try:
        engine = _get_engine()
        from sqlalchemy import text as sql_text
        now = datetime.now(timezone.utc)

        with engine.begin() as conn:
            # Mark old versions as not current
            conn.execute(
                sql_text(
                    "UPDATE knowledge_sections "
                    "SET is_current = 'false' "
                    "WHERE section_key = :key"
                ),
                {"key": section_key},
            )

            # Get next version number
            result = conn.execute(
                sql_text(
                    "SELECT COALESCE(MAX(version), 0) + 1 "
                    "FROM knowledge_sections "
                    "WHERE section_key = :key"
                ),
                {"key": section_key},
            )
            next_version = result.scalar()

            # Insert new version
            conn.execute(
                sql_text("""
                    INSERT INTO knowledge_sections
                        (section_key, title, content, version,
                         ticket_count, is_current,
                         created_at, updated_at)
                    VALUES
                        (:key, :title, :content, :version,
                         :count, 'true', :now, :now)
                """),
                {
                    "key": section_key,
                    "title": title,
                    "content": content,
                    "version": next_version,
                    "count": ticket_count,
                    "now": now,
                },
            )
    except Exception as e:
        print(f"[DB] Failed to save section: {e}")


# =====================================================================
# Main runner
# =====================================================================

def run_full_analysis(
    limit: int | None = None,
    generate_book: bool = True,
) -> dict:
    """Run the analysis pipeline.

    1. Fetch all ticket IDs from Zoho
    2. For each unanalyzed ticket: fetch detail, analyze, store
    3. Generate the Knowledge Book

    `limit` caps how many tickets one run will analyse. The backlog is
    ~2,300 closed tickets at roughly five seconds each, which is hours of
    wall clock, so a scheduled run takes a bounded bite and the rest
    carries to the next one. None means no ceiling.

    Set `generate_book=False` to analyse without re-synthesising, which
    the weekly refresh uses so the book is generated once at the end
    rather than once per source.

    Returns {total, already_analyzed, processed, failed, remaining}.
    """
    print("=" * 60)
    print("VOME KNOWLEDGE BOOK BUILDER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Initialize DB tables
    init_db()

    outcome = {
        "total": 0,
        "already_analyzed": 0,
        "processed": 0,
        "failed": 0,
        "remaining": 0,
    }

    # Step 1: Fetch all ticket IDs
    all_tickets = fetch_all_ticket_ids()
    if not all_tickets:
        print("[ERROR] No tickets found in Zoho Desk")
        return outcome

    # Step 2: Filter to unanalyzed tickets
    unanalyzed = [
        t for t in all_tickets
        if not is_ticket_analyzed(t["id"])
    ]
    outcome["total"] = len(all_tickets)
    outcome["already_analyzed"] = len(all_tickets) - len(unanalyzed)

    if limit is not None and len(unanalyzed) > limit:
        outcome["remaining"] = len(unanalyzed) - limit
        unanalyzed = unanalyzed[:limit]

    print(
        f"\n[PROGRESS] {len(all_tickets)} total tickets, "
        f"{outcome['already_analyzed']} already analyzed, "
        f"{len(unanalyzed)} this run, "
        f"{outcome['remaining']} left for the next run\n"
    )

    # Step 3: Process each ticket
    processed = 0
    failed = 0

    for i, ticket_meta in enumerate(unanalyzed):
        ticket_id = ticket_meta["id"]
        ticket_num = ticket_meta["ticketNumber"]
        subject = ticket_meta["subject"]

        print(
            f"[{i+1}/{len(unanalyzed)}] "
            f"Ticket #{ticket_num}: {subject[:60]}"
        )

        try:
            # Fetch full detail
            detail = fetch_ticket_detail(ticket_id)
            if not detail:
                print(f"  -> SKIP: Failed to fetch detail")
                failed += 1
                continue

            # Extract thread
            thread = extract_conversation_thread(detail)
            if thread["turn_count"] == 0:
                print(f"  -> SKIP: Empty conversation")
                failed += 1
                continue

            # Analyze with Claude
            analysis = analyze_ticket(thread)
            if not analysis:
                print(f"  -> SKIP: Analysis failed")
                failed += 1
                continue

            # Save
            save_analysis(
                ticket_id=ticket_id,
                ticket_number=ticket_num,
                subject=subject,
                thread=thread,
                analysis=analysis,
            )

            processed += 1
            training_val = analysis.get("training_value", "?")
            category = analysis.get("category", "?")
            print(
                f"  -> OK: {category} "
                f"(training: {training_val}, "
                f"turns: {thread['turn_count']}, "
                f"sam: {'yes' if thread['has_sam_response'] else 'no'})"
            )

            # Rate limiting
            time.sleep(CLAUDE_DELAY)
        except Exception as e:
            print(f"  -> ERROR: {e}")
            failed += 1
            continue

        # Progress update every 25 tickets
        if (i + 1) % 25 == 0:
            stats = get_analysis_stats()
            print(
                f"\n--- Progress: {processed} analyzed, "
                f"{failed} failed, "
                f"{len(unanalyzed) - i - 1} remaining ---"
            )
            print(
                f"--- Categories so far: {stats} ---\n"
            )

    outcome["processed"] = processed
    outcome["failed"] = failed

    print(f"\n{'=' * 60}")
    print(
        f"Analysis complete: {processed} processed, "
        f"{failed} failed"
    )
    print(f"{'=' * 60}\n")

    # Step 4: Generate Knowledge Book
    if generate_book:
        print("[BOOK] Generating Knowledge Book...")
        generate_knowledge_book()

    # Final stats
    stats = get_analysis_stats()
    print(f"\n{'=' * 60}")
    print("FINAL STATS")
    print(f"{'=' * 60}")
    for cat, count in sorted(
        stats.items(), key=lambda x: -x[1]
    ):
        print(f"  {cat}: {count}")
    print(f"  TOTAL: {sum(stats.values())}")
    print(
        f"\nKnowledge Book files: "
        f"{KNOWLEDGE_BOOK_DIR.absolute()}"
    )
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return outcome


# =====================================================================
# Weekly refresh (scheduled entry point)
# =====================================================================

# Per-run ceilings. Env-overridable because the right number depends on
# how big the backlog still is: generous while backfilling, small once
# the pipeline is only keeping up with the week's closures.
WEEKLY_TICKET_LIMIT = int(os.environ.get("KNOWLEDGE_TICKET_LIMIT", "200"))
WEEKLY_TASK_LIMIT = int(os.environ.get("KNOWLEDGE_TASK_LIMIT", "150"))


def run_weekly_knowledge_refresh() -> dict:
    """Learn from the week's closed work, then rebuild the book.

    This is what makes the agent self-learning rather than a one-off
    batch that somebody remembers to trigger. Order matters: both
    sources are mined first, then the book is synthesised once, then the
    read cache is dropped so live prompts pick it up.

    Claims the run in Postgres so a restart near the trigger time, or a
    second dyno, cannot double-run it.
    """
    from database import claim_sweeper_run, finish_sweeper_run

    run_key = f"knowledge-refresh-{datetime.now().strftime('%Y-%m-%d')}"
    if not claim_sweeper_run(run_key):
        print(f"[KNOWLEDGE] {run_key} already claimed -- skipping")
        return {"status": "already_ran"}

    print("=" * 60)
    print("WEEKLY KNOWLEDGE REFRESH")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    summary: dict = {"tickets": {}, "clickup": {}, "book": "skipped"}

    try:
        summary["tickets"] = run_full_analysis(
            limit=WEEKLY_TICKET_LIMIT, generate_book=False
        )
    except Exception as e:
        print(f"[KNOWLEDGE] ticket analysis failed: {e}")
        summary["tickets"] = {"error": str(e)}

    try:
        from clickup_knowledge import run_clickup_knowledge_scan
        summary["clickup"] = run_clickup_knowledge_scan(
            limit=WEEKLY_TASK_LIMIT
        )
    except Exception as e:
        print(f"[KNOWLEDGE] ClickUp scan failed: {e}")
        summary["clickup"] = {"error": str(e)}

    # Rebuild once, from whatever both passes produced. Worth doing even
    # if a pass errored: the other source may still have new rows.
    try:
        generate_knowledge_book()
        summary["book"] = "regenerated"
    except Exception as e:
        print(f"[KNOWLEDGE] book generation failed: {e}")
        summary["book"] = f"failed: {e}"

    finish_sweeper_run(run_key, summary)
    _post_refresh_summary(summary)

    print("=" * 60)
    print(f"WEEKLY KNOWLEDGE REFRESH COMPLETE: {summary}")
    print("=" * 60)
    return summary


def _post_refresh_summary(summary: dict) -> None:
    """Post a short Slack note so the pipeline is visibly alive."""
    tickets = summary.get("tickets") or {}
    tasks = summary.get("clickup") or {}
    remaining = (
        int(tickets.get("remaining", 0) or 0)
        + int(tasks.get("remaining", 0) or 0)
    )
    backlog = (
        f"\n{remaining} item(s) left in the backlog for next week."
        if remaining else ""
    )
    message = (
        f":books: *Weekly knowledge refresh*\n"
        f"Tickets: {tickets.get('processed', 0)} newly analysed "
        f"({tickets.get('already_analyzed', 0)} already known, "
        f"{tickets.get('failed', 0)} failed)\n"
        f"ClickUp tasks: {tasks.get('processed', 0)} newly mined "
        f"({tasks.get('skipped', 0)} too thin to learn from, "
        f"{tasks.get('failed', 0)} failed)\n"
        f"Knowledge book: {summary.get('book')}"
        f"{backlog}"
    )
    try:
        from slack import post_to_log
        post_to_log(message)
    except Exception as e:
        print(f"[KNOWLEDGE] Slack summary failed: {e}")


if __name__ == "__main__":
    if "--weekly" in sys.argv:
        run_weekly_knowledge_refresh()
    elif "--book-only" in sys.argv:
        generate_knowledge_book()
    else:
        run_limit = None if "--all" in sys.argv else 200
        if "--limit" in sys.argv:
            run_limit = int(sys.argv[sys.argv.index("--limit") + 1])
        run_full_analysis(limit=run_limit)
