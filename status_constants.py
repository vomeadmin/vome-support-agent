"""
status_constants.py

Single source of truth for every status / state string used across the
support agent. Centralized here so the upcoming renames and new statuses
cannot silently miss a casing variant scattered across the codebase.

IMPORTANT (behavior-preserving step):
    Every constant below holds the EXACT string value currently in use.
    Nothing here renames a value or wires a new status into live matching.
    Several ClickUp/Zoho statuses are written to their APIs with
    inconsistent casing today (e.g. "Closed" vs "CLOSED", "queued" vs
    "QUEUED"). That inconsistency is faithfully preserved with distinct
    constants and flagged with comments, so a later step can unify the
    casing deliberately rather than by accident.

Four vocabularies live here, and they must not be conflated:
  1. CU_*      — ClickUp status NAMES, canonical lowercase form, used for
                 INBOUND matching (compare normalize_status(x) == CU_...).
  2. CU_WRITE_* — exact strings we SEND to the ClickUp API (casing varies).
  3. THREAD_*  — our own ticket_threads.status values (PostgreSQL).
  4. ZOHO_*    — Zoho Desk status NAMES we write, plus ZNORM_* for the
                 normalized lowercase keys used by the dashboard scorer.
  + ACTION_*   — Command Center "clickup_action" keys (ops/draft, ops/send).
"""

import re

# ---------------------------------------------------------------------------
# Normalizer — used at INBOUND ClickUp status match sites so that casing and
# separator variants ("On Prod", "on_prod", "on prod ✅") all resolve to the
# same canonical CU_* constant. Lowercases, unifies separators to a single
# space, and drops trailing emoji/punctuation.
#
# This preserves every literal the inbound sites matched before (those forms
# are a subset of what normalize_status() maps to the canonical value); it
# only widens matching for inputs that were never sent in practice.
# ---------------------------------------------------------------------------

def normalize_status(value: str | None) -> str:
    """Canonicalize a status string for inbound matching.

    >>> normalize_status("On Prod")
    'on prod'
    >>> normalize_status("on_prod")
    'on prod'
    >>> normalize_status("on prod ✅")
    'on prod'
    >>> normalize_status("Needs_Review")
    'needs review'
    """
    if not value:
        return ""
    s = value.strip().lower()
    s = re.sub(r"[\s_\-]+", " ", s)      # unify separators -> single space
    s = re.sub(r"[^a-z0-9 ]+", "", s)    # drop emoji / punctuation
    return s.strip()


# ---------------------------------------------------------------------------
# 1. ClickUp status NAMES — canonical lowercase form (for inbound matching).
#    Compare with: normalize_status(incoming) == CU_xxx
# ---------------------------------------------------------------------------

CU_QUEUED = "queued"
CU_IN_PROGRESS = "in progress"
CU_ON_DEV = "on dev"
CU_NEEDS_REVIEW = "needs review"
CU_WAITING_ON_CLIENT = "waiting on client"
CU_ON_PROD = "on prod"
CU_SLEEPING = "sleeping"
CU_DONE = "done"


# ---------------------------------------------------------------------------
# 2. ClickUp status strings we WRITE to the API.
#    Casing is intentionally inconsistent across the codebase TODAY — these
#    constants preserve the exact bytes currently sent at each call site.
#    A later step can collapse these to a single canonical write value.
# ---------------------------------------------------------------------------

CU_WRITE_QUEUED_LOWER = "queued"          # clickup_tasks.create_clickup_task
CU_WRITE_QUEUED_UPPER = "QUEUED"          # agent._update_clickup_task_status re-queue
CU_WRITE_CLOSED_TITLE = "Closed"          # on_prod_handler + slack confirm close
CU_WRITE_CLOSED_UPPER = "CLOSED"          # clickup_tasks.close_clickup_task
CU_WRITE_WAITING_ON_CLIENT_UPPER = "WAITING ON CLIENT"  # slack confirm flow


# ---------------------------------------------------------------------------
# 3. Internal thread_map (PostgreSQL ticket_threads.status) values.
# ---------------------------------------------------------------------------

THREAD_OPEN = "open"
THREAD_HANDLED = "handled"
THREAD_CLOSED = "closed"
THREAD_PARKED = "parked"
THREAD_ON_PROD_PENDING = "on_prod_pending"
THREAD_ON_PROD_CANCELLED = "on_prod_cancelled"
THREAD_ON_PROD_SENT = "on_prod_sent"
THREAD_WAITING_CLIENT = "waiting-client"
THREAD_NEEDS_REVIEW = "needs-review"
THREAD_ESCALATED = "escalated"


# ---------------------------------------------------------------------------
# 4. Zoho Desk status NAMES we write (exact display casing).
# ---------------------------------------------------------------------------

ZOHO_NEW = "New"
# DEPRECATED -- the team does not use the bare "Open" status. A customer
# reply (incl. on a closed ticket) goes to ZOHO_PROCESSING instead. Kept
# only so the string resolves if any historical reference remains; no code
# should write this.
ZOHO_OPEN = "Open"
ZOHO_PROCESSING = "Processing"
ZOHO_IN_PROGRESS = "In Progress"
ZOHO_ON_HOLD = "On Hold"
# DEPRECATED -- no longer written by the agent. This Zoho status has a
# statusType of "On Hold", which parked tickets and buried inbound client
# replies. Customer replies and engineer assignment now use ZOHO_PROCESSING.
# Kept only so the string resolves if any historical reference remains.
ZOHO_PENDING_DEVELOPER_FIX = "Pending Developer Fix"
ZOHO_FINAL_REVIEW = "Final Review"
ZOHO_AWAITING_CLIENT_RESPONSE = "Awaiting Client Response"
ZOHO_CLOSED = "Closed"
ZOHO_RESOLVED = "Resolved"

# Zoho normalized keys — the lowercase/underscore vocabulary produced by
# ops.tickets._normalize_zoho_status and consumed by ops.scoring. These are
# NOT the same as the display-case ZOHO_* names above.
ZNORM_NEW = "new"
ZNORM_PROCESSING = "processing"
ZNORM_WAITING = "waiting"
ZNORM_FINAL_REVIEW = "final_review"
ZNORM_CLOSED = "closed"
ZNORM_NEEDS_REVIEW = "needs_review"   # set only as an effective_status override


# ---------------------------------------------------------------------------
# Zoho ticket tags (private/internal tags written to Zoho).
# ---------------------------------------------------------------------------

ZOHO_TAG_WAITING_CLIENT = "waiting-client"


# ---------------------------------------------------------------------------
# Command Center "clickup_action" keys (ops/draft DRAFT_DEFAULTS ->
# ops/send CLICKUP_ACTION_MAP). These are action identifiers, not statuses.
# ---------------------------------------------------------------------------

ACTION_LEAVE = "leave"
ACTION_CLOSE_TEMPORARILY = "close_temporarily"
ACTION_IN_PROGRESS = "in_progress"
ACTION_WAITING_ON_CLIENT = "waiting_on_client"
ACTION_DONE = "done"
ACTION_SLEEPING = "sleeping"


# ---------------------------------------------------------------------------
# New SOP vocabulary — LIVE on the ClickUp board (verified column names).
# CU_NEEDS_CLIENT_INFO is the engineer-set trigger (inbound match).
# CU_AWAITING_CLIENT is the parked column we WRITE after Vic sends the request.
# CU_ESCALATED is the renamed "needs review" (main.py trigger wiring pending).
# ---------------------------------------------------------------------------

CU_NEEDS_CLIENT_INFO = "needs client info"
CU_AWAITING_CLIENT = "awaiting client response"
CU_ESCALATED = "escalated"

# CU_USER_EDUCATION is the engineer-set trigger for the user-education
# auto-send: the dev explains why the user is misunderstanding the
# feature/steps and what they can do, Vic turns that into a client email
# and closes the ticket (handled in clickup_user_education_handler).
CU_USER_EDUCATION = "user education"

# CU_DECLINED completes the live board vocabulary. Verified on both Priority
# Queue and Accepted Backlog (space 90114113004), whose 11 columns are:
# queued, in progress, needs client info, user education, escalated, on dev,
# awaiting client response, sleeping, declined, on prod, Closed.
CU_DECLINED = "declined"


# ---------------------------------------------------------------------------
# Reporting buckets — used by weekly_engineering_report.py.
#
# WHY THESE ARE NOT ClickUp's OWN open/closed FLAG:
#   "awaiting client response", "sleeping", "declined" and "on prod" all carry
#   type "done" in ClickUp, so a query with include_closed=false STILL returns
#   them. Only "Closed" is type "closed". Any code that treats ClickUp's flag
#   as "is this active dev work" gets a wildly inflated active count.
#
# CU_SHIPPED deliberately merges "on prod" and "Closed": a task set to on prod
# auto-advances to Closed once the client is emailed about it, so on prod is a
# short-lived staging state, not a separate outcome. Counting both would double
# count anything that made the whole trip inside one reporting window.
# ---------------------------------------------------------------------------

# On the devs' plate — the only bucket that represents claimable workload.
BUCKET_ACTIVE = frozenset({CU_QUEUED, CU_IN_PROGRESS, CU_ON_DEV})

# Left the plate as delivered work.
BUCKET_SHIPPED = frozenset({CU_ON_PROD, "closed"})

# Left the plate but is not delivered: the client owes us something. Pushing a
# task here IS output from the devs' side, and a client reply pushes it back.
BUCKET_PARKED = frozenset({CU_AWAITING_CLIENT, CU_USER_EDUCATION})

# Everything else. Tracked only so report totals reconcile against the board.
# CU_ESCALATED lives here on purpose: escalated tasks are Sam's to action, not
# the devs', so they must not land in the plate total.
BUCKET_OTHER = frozenset({
    CU_NEEDS_CLIENT_INFO,
    CU_ESCALATED,
    CU_SLEEPING,
    CU_DECLINED,
    CU_WAITING_ON_CLIENT,   # legacy spelling, still on old tasks
    CU_NEEDS_REVIEW,        # legacy name for escalated
    CU_DONE,                # legacy, predates the on prod -> Closed automation
})


def report_bucket(status: str | None) -> str:
    """Map a ClickUp status to a reporting bucket name.

    Accepts any casing/separator variant; normalize_status() handles that.
    Returns one of "active", "shipped", "parked", "other".

    >>> report_bucket("In Progress")
    'active'
    >>> report_bucket("on prod")
    'shipped'
    >>> report_bucket("Awaiting Client Response")
    'parked'
    >>> report_bucket("escalated")
    'other'
    >>> report_bucket(None)
    'other'
    """
    s = normalize_status(status)
    if s in BUCKET_ACTIVE:
        return "active"
    if s in BUCKET_SHIPPED:
        return "shipped"
    if s in BUCKET_PARKED:
        return "parked"
    return "other"
