"""
clickup_tasks.py

Creates ClickUp tasks from processed Zoho tickets.
Called from agent.py after Claude analysis is complete.
"""

import os
import re

import httpx

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

# ---------------------------------------------------------------------------
# List IDs
# ---------------------------------------------------------------------------

LIST_PRIORITY_QUEUE = "901113386257"
LIST_RAW_INTAKE = "901113386484"
LIST_ACCEPTED_BACKLOG = "901113389889"

# ---------------------------------------------------------------------------
# Custom field IDs
# ---------------------------------------------------------------------------

FIELD_TYPE = "e0e439f5-397d-432d-addd-e90fbf50cd30"
FIELD_PLATFORM = "5f1ff65b-18fc-49db-89aa-2c1f355ec1e7"
FIELD_MODULE = "3f111d48-e92a-4d5e-92d9-e193c80b20cc"
FIELD_SOURCE = "857e1262-cb5c-4c22-b8da-770a8fcfa82e"
FIELD_HIGHEST_TIER = "be348a1d-6a63-4da8-83bb-9038b24264ff"
FIELD_REQUESTING_CLIENTS = "e2de3bd0-6ad9-4b31-bb09-104f6bef383d"
FIELD_COMBINED_ARR = "29c41859-f24b-4143-9af4-a34202205641"
FIELD_AUTO_SCORE = "fd77f978-eca8-499e-bc3c-dc1bf4b8181e"
FIELD_ZOHO_TICKET_LINK = "4776215b-c725-4d79-8f20-c16f0f0145ac"

# Type option IDs
TYPE_BUG = "d9c82e67-c46b-48d1-95f7-9c1f5b2fc2df"
TYPE_FEATURE = "41a1ea4e-eec9-418d-a684-3c17cdd8dd67"
TYPE_UX = "da749879-7b3a-4fd5-a1cb-c85fcb719569"
TYPE_IMPROVEMENT = "9864f852-39cc-481c-aafc-c2f2ebdba30b"
TYPE_INVESTIGATION = "f9bd67bb-5b85-49fb-bd4a-21295f01cf5a"

# Platform option IDs
PLATFORM_WEB = "946c8214-6a65-4e63-a437-d98415dc1439"
PLATFORM_MOBILE = "070470c3-c248-4d64-8ceb-8c95df82506b"
PLATFORM_BOTH = "2d69c526-e58f-4486-bc6a-168cd812f0bf"

# Module option IDs
MODULE_IDS = {
    "volunteer homepage": "1fd64528-970a-48da-8881-9a0fb4ac96f4",
    "reserve schedule": "197109a7-2974-4210-94f1-a1e97990830f",
    "opportunities": "af0b8949-5281-4655-8326-c77dcfb2ecf7",
    "sequences": "04d7e808-d94f-48bb-b5eb-f567e6cf41ca",
    "forms": "aa6f6c17-7260-44fd-a862-99daaf7d77c0",
    "admin dashboard": "b13da71b-46ab-45c6-82ab-fa37a18fc0b3",
    "admin scheduling": "f36d0e31-2f2b-4044-bb5c-fb4fef06cb74",
    "admin settings": "d9d9051c-d733-4ac0-8607-1a75153b021b",
    "admin permissions": "bf68973f-f858-4083-9a60-7dc014b6e1f3",
    "sites": "5f5cc57b-6259-4bab-88dd-64b3a34036f1",
    "groups": "36178405-828f-4471-80a4-cedb2eb0be59",
    "categories": "f4f5021b-d528-41c1-b832-87a67e5ba0ae",
    "hour tracking": "53f02923-e8a5-4c30-8a42-c2bddaa75778",
    "kiosk": "d92abcaa-e2a3-46f6-bb99-2025aa3984d3",
    "email communications": "c2c3a5ae-e8db-4da5-9ab3-736c3a76b66f",
    "chat": "a4d5a4bd-049b-4dfa-b6c2-2843661faad4",
    "reports": "95a6e4bf-eaa0-4cd4-87ed-9298a37da0c6",
    "kpi dashboards": "ef0db184-315f-4420-bccb-bb7dee73e7a1",
    "integrations": "938a5549-70dd-40f2-9db2-4176d10b4221",
    "access / authentication": "fd457e45-e25c-4910-871f-fe67bf5391d3",
    "other": "cbe38d18-9d9f-4e49-abf5-5101f09349ff",
}

# Source option IDs
SOURCE_ZOHO_TICKET = "9b678f29-3b49-4842-9305-ada436cfc0b3"
SOURCE_FIELD_FEEDBACK = "ef5fcb3c-c27d-443c-bef2-32be1521baf1"

# ClickUp user IDs
CLICKUP_USER_SANJAY = 4434086
CLICKUP_USER_ONLYG = 49257687
CLICKUP_USER_SAM = 3691763

# ClickUp priority: 1=urgent(P1), 2=high(P2), 3=normal(P3)
PRIORITY_MAP = {"p1": 1, "p2": 2, "p3": 3}

# Auto Score (0-100) matrix: category urgency + complexity + client tier
# Higher = more urgent, engineers sort by this to prioritize work

_CATEGORY_BASE = {
    "bug": 40,
    "investigation": 35,
    "auth": 45,
    "feature": 15,
    "how-to": 5,
    "billing": 5,
}

_COMPLEXITY_BUMP = {
    "low": 0,
    "medium": 10,
    "high": 20,
    "very-high": 30,
}

_TIER_BUMP = {
    "very-high": 25,   # Ultimate / $4k+ ARR
    "high": 15,         # Enterprise / $1.5k-4k ARR
    "medium": 5,        # Pro / $1k-1.5k ARR
    "low": 0,
}


def _compute_auto_score(analysis: dict) -> int:
    """Compute Auto Score (0-100) from category, complexity, and client tier."""
    cat = analysis.get("category", "")
    cx = analysis.get("complexity", "low")
    tier = analysis.get("client_tier", "low")

    base = _CATEGORY_BASE.get(cat, 20)
    cx_bump = _COMPLEXITY_BUMP.get(cx, 0)
    tier_bump = _TIER_BUMP.get(tier, 0)

    return min(base + cx_bump + tier_bump, 100)


# Legacy map kept for backward compat (field feedback / reply handler)
COMPLEXITY_SCORE_MAP = {
    "low": 20,
    "medium": 40,
    "high": 60,
    "very-high": 80,
}

# Engineer type -> ClickUp assignee ID
ENGINEER_ASSIGNEE_MAP = {
    "frontend": CLICKUP_USER_SANJAY,
    "mobile": CLICKUP_USER_SANJAY,
    "backend": CLICKUP_USER_ONLYG,
}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _extract(field: str, text: str) -> str:
    """Extract a labelled field, handling optional markdown bold."""
    m = re.search(
        rf"^\*?\*?{re.escape(field)}\*?\*?:\s*\*?\*?(.+)",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if not m:
        return ""
    return m.group(1).strip().rstrip("*").strip()


def _parse_agent_response(agent_response: str) -> dict:
    """Pull the structured fields out of Claude's AGENT ANALYSIS block."""
    return {
        "classification": _extract("CLASSIFICATION", agent_response),
        "module": _extract("MODULE", agent_response),
        "platform": _extract("PLATFORM", agent_response),
        "priority": _extract("PRIORITY", agent_response),
        "auto_score": _extract("AUTO SCORE", agent_response),
        "timing": _extract("TIMING", agent_response),
    }


def _derive_priority_label(analysis: dict) -> str:
    """Derive P1/P2/P3 from tier and complexity.

    tier:very-high OR complexity:very-high -> P1
    complexity:high OR tier:high           -> P2
    else                                   -> P3
    """
    tier = analysis.get("client_tier", "low")
    cx = analysis.get("complexity", "low")
    if tier == "very-high" or cx == "very-high":
        return "P1"
    if cx == "high" or tier == "high":
        return "P2"
    return "P3"


def _determine_list_from_category(category: str) -> str | None:
    """Route to the correct ClickUp list based on category.

    Returns list ID or None (no task should be created).
    """
    if category in ("bug", "investigation", "auth"):
        return LIST_PRIORITY_QUEUE
    # feature, how-to, billing -> no ClickUp task (Sam curates manually)
    return None


def _determine_assignee_from_analysis(analysis: dict) -> int | None:
    """Map engineer_type to ClickUp user ID.

    auth category always routes to OnlyG regardless of engineer_type.
    """
    cat = analysis.get("category", "")
    if cat == "auth":
        return CLICKUP_USER_ONLYG
    eng = analysis.get("engineer_type", "")
    if eng == "unclear":
        return CLICKUP_USER_SANJAY
    return ENGINEER_ASSIGNEE_MAP.get(eng)


def _map_type_option(classification: str) -> str:
    """Map classification string to the Type custom field option ID."""
    cl = classification.lower()
    if "bug" in cl:
        return TYPE_BUG
    if "feature" in cl:
        return TYPE_FEATURE
    if "ux" in cl:
        return TYPE_UX
    if "improvement" in cl:
        return TYPE_IMPROVEMENT
    # Access issue, Direct action, General question, Unclear → Investigation
    return TYPE_INVESTIGATION


def _map_platform_option(platform: str) -> str | None:
    """Map platform string to the Platform custom field option ID."""
    pl = platform.lower()
    if "both" in pl:
        return PLATFORM_BOTH
    if "mobile" in pl:
        return PLATFORM_MOBILE
    if "web" in pl:
        return PLATFORM_WEB
    return None


def _map_module_option(module: str) -> str | None:
    """Map module name to the Module custom field option ID."""
    ml = module.lower().strip()
    # Exact match first
    if ml in MODULE_IDS:
        return MODULE_IDS[ml]
    # Partial match fallback
    for key, option_id in MODULE_IDS.items():
        if key in ml or ml in key:
            return option_id
    return MODULE_IDS["other"]


def _map_priority(priority: str) -> int:
    """Map P1/P2/P3 string to ClickUp priority integer."""
    p = priority.lower().strip()
    # Accept "p1", "p2", "p3" or "1", "2", "3"
    for label, val in PRIORITY_MAP.items():
        if label in p:
            return val
    return 3  # default normal


def _determine_list(classification: str, priority: str) -> str:
    """Route to the correct ClickUp list based on classification + priority.

    Legacy helper kept for backward compatibility (field_feedback, reply handler).
    New ticket flow uses _determine_list_from_category instead.
    """
    cl = classification.lower()
    if "feature" in cl:
        return LIST_RAW_INTAKE
    if "ux" in cl:
        if "p3" in priority.lower():
            return LIST_ACCEPTED_BACKLOG
        return LIST_PRIORITY_QUEUE
    return LIST_PRIORITY_QUEUE


def _determine_assignee(classification: str) -> int | None:
    """Infer suggested assignee from classification type.

    Legacy helper kept for backward compatibility (field_feedback, reply handler).
    New ticket flow uses _determine_assignee_from_analysis instead.
    """
    cl = classification.lower()
    if any(x in cl for x in ("frontend", "ux", "ui")):
        return CLICKUP_USER_SANJAY
    if any(x in cl for x in ("backend", "data", "access", "api")):
        return CLICKUP_USER_ONLYG
    return None


def _build_title(
    ticket_data: dict, crm: dict, subject: str, priority: str,
    issue_summary: str = "",
) -> str:
    """Build task title: [Account] -- [concise issue] -- [P1/P2/P3]

    Prefers the first sentence of issue_summary (always English) over
    the raw subject which may be in another language or outdated.
    """
    if crm.get("found"):
        account = crm.get("account_name") or "Unknown"
    else:
        account = "Volunteer"

    # Use first sentence of issue summary if available
    if issue_summary:
        first_sentence = issue_summary.split(".")[0].strip()
        subj = first_sentence[:65]
    else:
        subj = subject.strip()
        if len(subj) > 65:
            subj = subj[:62] + "..."

    p = priority.upper().strip()
    if not re.match(r"^P[123]$", p):
        p = "P3"

    return f"{account} -- {subj} -- {p}"


def _build_description(
    ticket_data: dict,
    crm: dict,
    parsed: dict,
    zoho_url: str,
    issue_summary: str = "",
) -> str:
    """Build a clean ClickUp task description (always English)."""
    account = crm.get("account_name") or "Unknown"
    tier = crm.get("tier") or "Unknown"
    arr_raw = crm.get("arr")
    if arr_raw:
        try:
            arr_str = f"${int(float(arr_raw)):,}"
        except (ValueError, TypeError):
            arr_str = f"${arr_raw}"
    else:
        arr_str = "Unknown"

    ticket_num = ticket_data.get("ticket_number") or ticket_data.get(
        "ticket_id", ""
    )

    lines = [
        f"**Account:** {account} | **Tier:** {tier} | **ARR:** {arr_str}",
        f"**Zoho ticket:** #{ticket_num}",
        f"**Zoho link:** {zoho_url}",
        "",
        "---",
        "",
    ]

    if issue_summary:
        lines.append(issue_summary)
        lines.append("")

    classification = parsed.get("classification", "")
    module = parsed.get("module", "")
    timing = parsed.get("timing", "")

    meta_parts = []
    if classification:
        meta_parts.append(f"Classification: {classification}")
    if module:
        meta_parts.append(f"Module: {module}")
    if parsed.get("platform"):
        meta_parts.append(f"Platform: {parsed['platform']}")
    if timing:
        meta_parts.append(f"Timing: {timing}")
    if meta_parts:
        lines.append(" | ".join(meta_parts))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def create_clickup_task(
    ticket_data: dict,
    agent_response: str,
    crm: dict,
    zoho_url: str,
    source_option_id: str | None = None,
    analysis: dict | None = None,
) -> dict | None:
    """
    Parse agent response and create a ClickUp task via REST API.

    When ``analysis`` is provided (dict with category, complexity,
    client_tier, engineer_type, flags), routing and scoring use the
    new classification system.  Otherwise falls back to legacy parsing
    for backward compatibility (field feedback, reply handler).

    Returns {"task_id": str, "task_url": str} or None on any failure
    (including categories that should not produce a task).
    Never raises -- errors are logged and None is returned so the main
    pipeline continues without crashing.
    """
    if not CLICKUP_API_TOKEN:
        print("create_clickup_task: CLICKUP_API_TOKEN not set -- skipping")
        return None

    try:
        parsed = _parse_agent_response(agent_response)
        subject = ticket_data.get("subject", "Unknown issue")

        # -----------------------------------------------------------------
        # Routing, priority, assignee, score -- new vs legacy path
        # -----------------------------------------------------------------
        if analysis:
            category = analysis.get("category", "")

            # Determine list -- may return None (no task for how-to/billing)
            list_id = _determine_list_from_category(category)
            if list_id is None:
                print(
                    f"create_clickup_task: category '{category}' "
                    f"does not get a ClickUp task -- skipping"
                )
                return None

            # Priority label from tier + complexity
            priority_label = _derive_priority_label(analysis)
            cu_priority = PRIORITY_MAP.get(priority_label.lower(), 3)

            # Assignee from engineer_type
            assignee = _determine_assignee_from_analysis(analysis)

            # Auto score from category + complexity + tier
            auto_score = _compute_auto_score(analysis)

            # Type mapping: category -> ClickUp Type option
            cat_type_map = {
                "bug": TYPE_BUG,
                "investigation": TYPE_INVESTIGATION,
                "feature": TYPE_FEATURE,
                "auth": TYPE_INVESTIGATION,
            }
            type_option = cat_type_map.get(category, TYPE_INVESTIGATION)
        else:
            # Legacy path (field feedback / reply handler)
            classification = parsed["classification"]
            priority_label = parsed["priority"] or "P3"
            list_id = _determine_list(classification, priority_label)
            cu_priority = _map_priority(priority_label)
            assignee = _determine_assignee(classification)
            type_option = _map_type_option(classification)

            auto_score = None
            if parsed.get("auto_score"):
                try:
                    auto_score = int(parsed["auto_score"])
                except ValueError:
                    pass

        # -----------------------------------------------------------------
        # Build task title and description (always English)
        # -----------------------------------------------------------------
        issue_summary = _extract("ISSUE SUMMARY", agent_response)
        title = _build_title(
            ticket_data, crm, subject, priority_label,
            issue_summary=issue_summary,
        )
        description = _build_description(
            ticket_data, crm, parsed, zoho_url,
            issue_summary=issue_summary,
        )

        # ARR
        arr_value = None
        if crm.get("arr"):
            try:
                arr_value = int(float(crm["arr"]))
            except (ValueError, TypeError):
                pass

        # -----------------------------------------------------------------
        # Custom fields
        # -----------------------------------------------------------------
        custom_fields = []

        # Type
        custom_fields.append({"id": FIELD_TYPE, "value": type_option})

        # Platform
        platform_option = _map_platform_option(parsed.get("platform", ""))
        if platform_option:
            custom_fields.append(
                {"id": FIELD_PLATFORM, "value": platform_option}
            )

        # Module
        module_option = _map_module_option(parsed.get("module", ""))
        if module_option:
            custom_fields.append(
                {"id": FIELD_MODULE, "value": module_option}
            )

        # Source -- caller can override (e.g. Field Feedback)
        source = source_option_id or SOURCE_ZOHO_TICKET
        custom_fields.append({"id": FIELD_SOURCE, "value": source})

        # Zoho Ticket Link (always included)
        custom_fields.append(
            {"id": FIELD_ZOHO_TICKET_LINK, "value": zoho_url}
        )

        # Highest Tier -- map CRM plan tier to ClickUp dropdown option UUID
        tier_option_map = {
            "ultimate": "a4857fdc-2212-4e94-bc4b-a9dc3cbe2156",
            "enterprise": "8ee06497-a017-4863-8e2e-1baee9f5d5fe",
            "pro": "5dcd5624-ab1e-4ce6-a191-4a44b5862cd9",
            "recruit": "ff26f6e4-2936-43f6-ae92-7ea65522983b",
            "prospect": "f34e3c4f-7518-4f2f-83bd-628a304ab328",
            "volunteer": "bffde2a3-0a61-409b-9021-f4bcb1b46e11",
            "internal": "54849b41-9293-4c57-be3e-fd6e6da51fa8",
        }
        crm_tier = (crm.get("tier") or "").strip().lower()
        tier_option = tier_option_map.get(crm_tier)
        if not tier_option and not crm.get("found"):
            tier_option = tier_option_map["volunteer"]
        if tier_option:
            custom_fields.append(
                {"id": FIELD_HIGHEST_TIER, "value": tier_option}
            )

        # Requesting Clients
        if crm.get("found") and crm.get("account_name"):
            tier = crm.get("tier", "")
            arr_display = f"${arr_value:,}" if arr_value else "unknown ARR"
            clients_str = f"{crm['account_name']} ({tier}, {arr_display})"
            custom_fields.append(
                {"id": FIELD_REQUESTING_CLIENTS, "value": clients_str}
            )

        # Combined ARR
        if arr_value is not None:
            custom_fields.append(
                {"id": FIELD_COMBINED_ARR, "value": arr_value}
            )

        # Auto Score
        if auto_score is not None:
            custom_fields.append(
                {"id": FIELD_AUTO_SCORE, "value": auto_score}
            )

        # -----------------------------------------------------------------
        # Assemble and POST
        # -----------------------------------------------------------------
        payload: dict = {
            "name": title,
            "description": description,
            "priority": cu_priority,
            "status": "QUEUED",
            "custom_fields": custom_fields,
        }
        if assignee:
            payload["assignees"] = [assignee]

        url = f"{CLICKUP_BASE}/list/{list_id}/task"
        r = httpx.post(
            url,
            json=payload,
            headers={
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        r.raise_for_status()
        task = r.json()

        task_id = task.get("id", "")
        task_url = task.get("url") or f"https://app.clickup.com/t/{task_id}"

        print(
            f"ClickUp task created: {title} "
            f"(ID: {task_id}, list: {list_id})"
        )
        return {"task_id": task_id, "task_url": task_url}

    except httpx.HTTPStatusError as e:
        print(
            f"ClickUp task creation failed (HTTP {e.response.status_code}): "
            f"{e.response.text[:300]}"
        )
        return None
    except Exception as e:
        print(f"ClickUp task creation failed: {e}")
        return None
