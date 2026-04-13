"""
ops/scoring.py

Composite priority scoring for the ticket dashboard.
Cards are sorted by score descending — higher = needs attention sooner.
"""


def compute_priority_score(ticket: dict) -> int:
    score = 0

    # Base score from ClickUp auto_score field (0-100)
    score += ticket.get("auto_score", 0) or 0

    # Tier weight
    tier_weights = {
        "Ultimate": 50,
        "Enterprise": 40,
        "Pro": 25,
        "Recruit": 10,
        "Prospect": 5,
        "Volunteer": 0,
        "Unknown": 5,
    }
    score += tier_weights.get(ticket.get("tier", "Unknown"), 5)

    # ARR weight (every $1,000 ARR = 5 points, max 50)
    arr = ticket.get("arr_dollars", 0) or 0
    score += min(50, int(arr / 1000) * 5)

    # Priority level from ClickUp
    priority_weights = {"urgent": 40, "high": 25, "normal": 10, "low": 0}
    score += priority_weights.get(ticket.get("priority"), 0)

    # Status urgency
    status_weights = {
        "new": 20,
        "processing": 5,
        "needs_review": 15,
        "final_review": 10,
        "waiting": 0,
    }
    score += status_weights.get(
        ticket.get("zoho_status_normalized"), 0
    )

    # Age penalty
    days_since_update = ticket.get("days_since_update", 0) or 0
    if days_since_update > 7:
        score += 20
    elif days_since_update > 3:
        score += 10

    # P1 override — always at the top
    if ticket.get("priority") == "urgent":
        score += 200

    return score
