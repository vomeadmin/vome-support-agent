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

Run this script once to process all historical tickets:
    python ticket_analyzer.py

It tracks progress in the database, so it's safe to restart --
it will pick up where it left off.

The Knowledge Book output lives in:
    knowledge_book/  (markdown files, one per section)
    knowledge_book/_summary.md  (overview + stats)
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

        result = _zoho_desk_call("ZohoDesk_getTickets", {
            "query_params": {
                "orgId": str(ZOHO_ORG_ID),
                "from": offset,
                "limit": 100,
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
    raw_desc = ticket.get("description", "")
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

        content = entry.get("content", "")
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
            model="claude-sonnet-4-20250514",
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


def generate_knowledge_book():
    """Generate the full Knowledge Book from analyzed tickets."""
    analyses = get_all_analyses()
    if not analyses:
        print("[BOOK] No analyzed tickets found")
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

        # Build summaries for Claude
        summaries = []
        for t in tickets:
            a = t["analysis"]
            summary = (
                f"Ticket #{t['ticket_number']}: {t['subject']}\n"
                f"  Category: {a.get('category', '?')}\n"
                f"  Module: {a.get('module', '?')}\n"
                f"  Resolution: {a.get('resolution_type', '?')}\n"
                f"  Sam's approach: {a.get('sam_response_pattern', 'N/A')}\n"
                f"  Key phrases: {', '.join(a.get('key_phrases', []))}\n"
                f"  Follow-ups asked: {', '.join(a.get('follow_up_questions_asked', []))}\n"
                f"  Resolution: {a.get('resolution_summary', 'N/A')}\n"
                f"  Training value: {a.get('training_value', '?')}\n"
                f"  Training notes: {a.get('training_notes', '')}\n"
                f"  Tone notes: {a.get('sam_tone_notes', '')}\n"
                f"  FAQ topic: {a.get('suggested_faq_topic', 'none')}\n"
                f"  Was deflectable: {a.get('was_deflectable', False)}\n"
                f"  Language: {t['language']}"
            )
            summaries.append(summary)

        # Truncate if too many tickets
        summaries_text = "\n\n---\n\n".join(summaries)
        if len(summaries_text) > 15000:
            summaries_text = summaries_text[:15000] + (
                "\n\n[... additional tickets truncated]"
            )

        prompt = BOOK_SYNTHESIS_PROMPT.format(
            ticket_count=len(tickets),
            section_title=title,
            category=category,
            ticket_summaries=summaries_text,
        )

        try:
            response = _client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4000,
                messages=[
                    {"role": "user", "content": prompt}
                ],
            )
            section_content = response.content[0].text
            sections[category] = {
                "title": title,
                "content": section_content,
                "ticket_count": len(tickets),
            }

            # Write to file
            filename = f"{category}.md"
            filepath = KNOWLEDGE_BOOK_DIR / filename
            filepath.write_text(
                f"# {title}\n\n"
                f"*Generated from {len(tickets)} tickets "
                f"| Last updated: "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
                f"{section_content}",
                encoding="utf-8",
            )
            print(f"[BOOK] Written: {filename}")

            # Save to database
            _save_knowledge_section(
                category, title, section_content, len(tickets)
            )

            time.sleep(CLAUDE_DELAY)

        except Exception as e:
            print(f"[BOOK] Failed to generate {title}: {e}")

    # Generate Sam's Voice guide (cross-cutting)
    _generate_voice_guide(analyses)

    # Generate summary
    _generate_summary(sections, analyses)

    print("[BOOK] Knowledge Book generation complete!")


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
            model="claude-sonnet-4-20250514",
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
        "at runtime. When a new support conversation starts, "
        "the system retrieves the most similar past tickets "
        "from ChromaDB and includes Sam's actual responses "
        "as examples.",
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

def run_full_analysis():
    """Run the complete analysis pipeline.

    1. Fetch all ticket IDs from Zoho
    2. For each unanalyzed ticket: fetch detail, analyze, store
    3. Generate the Knowledge Book
    """
    print("=" * 60)
    print("VOME KNOWLEDGE BOOK BUILDER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Initialize DB tables
    init_db()

    # Step 1: Fetch all ticket IDs
    all_tickets = fetch_all_ticket_ids()
    if not all_tickets:
        print("[ERROR] No tickets found in Zoho Desk")
        return

    # Step 2: Filter to unanalyzed tickets
    unanalyzed = [
        t for t in all_tickets
        if not is_ticket_analyzed(t["id"])
    ]
    print(
        f"\n[PROGRESS] {len(all_tickets)} total tickets, "
        f"{len(all_tickets) - len(unanalyzed)} already analyzed, "
        f"{len(unanalyzed)} remaining\n"
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

    print(f"\n{'=' * 60}")
    print(
        f"Analysis complete: {processed} processed, "
        f"{failed} failed"
    )
    print(f"{'=' * 60}\n")

    # Step 4: Generate Knowledge Book
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


if __name__ == "__main__":
    run_full_analysis()
