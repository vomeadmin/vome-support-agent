"""
outbound_guard.py

Last line of defense before a model-generated draft is emailed to a client.

WHY THIS EXISTS
---------------
The unattended auto-send paths (ON PROD resolution, user education, needs
client info) generate a draft with the model and email it to the client with
no human in the loop. Until this module existed, the only gate was:

    can_send = bool(contact_email) and bool(draft) and len(draft.strip()) >= 20

That is "has an address and is at least 20 characters". It does not check that
the text is actually a client-facing message.

On 2026-09-07 that gap sent ticket #8945 (Summit Metro Parks) an email in
which the model, instead of writing a client reply, explained to its operator
why it could not write one. It quoted internal dev notes, named an engineer,
listed unverified bugs, referenced the system prompt and internal routing
rules, and offered to "draft that structured update template now". It was
signed "Vic, Support Team" and the ticket was closed immediately after.

The model behaved reasonably: it was told to explain that a feature works as
intended, on a ticket that was actually seven open bugs, and it refused. The
bug is that we treat any string of 20+ characters as a sendable email.

WHAT THIS DOES
--------------
validate_client_message() is a deterministic (no model call, no network)
check that the text reads as a message to a client rather than as model
commentary, internal notes, or a refusal. It fails CLOSED: callers must route
a rejected draft to human review instead of sending it.

Deliberately no model call. This runs on the output of a model that has
already misbehaved, so asking another model to grade it adds a second thing
that can fail open. Every check here is a regex or a length test.
"""

import os
import re

from signatures import SIGNATURE_DOMAIN, _strip_trailing_signoff

# ---------------------------------------------------------------------------
# Blocking patterns
#
# Each entry is (reason, compiled pattern). Kept narrow on purpose: a false
# positive costs one draft going to Slack for a human, a false negative costs
# a client seeing our internals. But a guard that fires on ordinary replies
# gets switched off, so patterns must not match normal support copy such as
# "our team is looking into it" or "we've pushed an update".
# ---------------------------------------------------------------------------

_META_PATTERNS = [
    # The model addressing its operator or talking about its own task.
    ("refers to its prompt or instructions", re.compile(
        r"\b(?:the|my|this)\s+(?:system\s+)?prompt\b"
        r"|\bsystem\s+prompt\b"
        r"|\bmy\s+instructions\b"
        r"|\bas\s+an\s+AI\b"
        r"|\bas\s+a\s+language\s+model\b",
        re.IGNORECASE)),
    ("declines or defers the task", re.compile(
        r"\bI\s+(?:cannot|can't|can\s+not|am\s+unable\s+to|won't|will\s+not)"
        r"\s+(?:write|draft|produce|generate|complete|comply|confirm)\b"
        r"|\bwhy\s+I\s+(?:cannot|can't|can\s+not)\b"
        r"|\bI\s+need\s+to\s+pause\b"
        r"|\bI\s+must\s+(?:never|not)\b"
        r"|\bthere\s+is\s+nothing\s+to\s+respond\s+to\b"
        r"|\bwhat\s+should\s+actually\s+happen\b",
        re.IGNORECASE)),
    ("talks about drafting instead of being the message", re.compile(
        r"\b(?:the|this|that|a|requested)\s+draft\b"
        r"|\bdraft\s+(?:response|reply|message|update|template)\b"
        r"|\bI\s+(?:can|could|will|'ll)\s+draft\b"
        r"|\bbefore\s+drafting\b"
        r"|\bplaceholders?\b",
        re.IGNORECASE)),
    ("asks the reader to authorize more work", re.compile(
        r"\bwould\s+you\s+like\s+me\s+to\b"
        r"|\bif\s+you\s+would\s+like,?\s+I\s+can\b"
        r"|\blet\s+me\s+know\s+if\s+you\s+want\s+me\s+to\b"
        r"|\bshall\s+I\b",
        re.IGNORECASE)),
]

_INTERNAL_PATTERNS = [
    ("exposes internal notes or tooling", re.compile(
        r"\bdev(?:'s|s')?\s+notes?\b"
        r"|\bengineer(?:'s|s')?\s+notes?\b"
        r"|\binternal\s+note\b"
        r"|\bClickUp\b"
        r"|\bZoho\b"
        r"|\bSlack\b"
        r"|\bthe\s+dev(?:s)?\b"
        r"|\bthe\s+engineer(?:s)?\b"
        r"|\bengineering\s+team\b",
        re.IGNORECASE)),
    ("exposes internal process or routing", re.compile(
        r"\brouting\s+rules?\b"
        r"|\baccount\s+tier\b"
        r"|\breviewed\s+by\s+\w+\s+before\s+sending\b"
        r"|\bper\s+(?:our\s+)?(?:internal\s+)?(?:routing|escalation)\b",
        re.IGNORECASE)),
    ("discusses unverified or unconfirmed fix status", re.compile(
        r"\bnot\s+verified\b"
        r"|\bunverified\b"
        r"|\bwithout\s+confirming\b"
        r"|\bfix\s+claim\b"
        r"|\bbefore\s+verification\b",
        re.IGNORECASE)),
]

_FORMAT_PATTERNS = [
    # Outbound mail is sent as contentType "plainText", so markdown markup
    # never renders. Its presence means the text was not written as an email.
    ("contains markdown markup", re.compile(
        r"\*\*|^#{1,6}\s|```", re.MULTILINE)),
]

# Teammate names that must never appear in the body of a client email. The
# signature block is stripped before this runs, so a message legitimately
# signed by Sam does not trip it.
_DEFAULT_INTERNAL_NAMES = (
    "sam", "ron", "sanjay", "onlyg", "vic",
)

# A draft longer than this is not the brief note these categories are meant
# to produce, and is almost always the model writing an essay to itself.
_MAX_CHARS = 3500


def _internal_names() -> tuple[str, ...]:
    """Roster of internal names, overridable with OUTBOUND_INTERNAL_NAMES."""
    raw = os.environ.get("OUTBOUND_INTERNAL_NAMES", "")
    if raw.strip():
        return tuple(
            n.strip().lower() for n in raw.split(",") if n.strip()
        )
    return _DEFAULT_INTERNAL_NAMES


def _body_without_signature(draft: str) -> str:
    """The draft with its trailing signature block removed.

    sign_message() appends the signature, so the signer's own name and the
    support domain live there legitimately. Name and internal-reference
    checks run on the body only.
    """
    body = draft or ""
    # Drop the appended signature block, then any sign-off the model wrote.
    idx = body.rfind(SIGNATURE_DOMAIN)
    if idx != -1:
        body = body[:idx]
    return _strip_trailing_signoff(body)


def validate_client_message(
    draft: str,
    *,
    category: str = "",
    contact_name: str = "",
    require_signature: bool = True,
) -> dict:
    """Decide whether *draft* is safe to email to a client unattended.

    Returns::

        {
          "ok": bool,          # False means DO NOT SEND, route to a human
          "reasons": [str],    # blocking reasons, safe to show in Slack
          "warnings": [str],   # non-blocking notes (e.g. em dash present)
        }

    Fails closed: an empty or unparseable draft is not ok.
    """
    reasons: list[str] = []
    warnings: list[str] = []

    text = (draft or "").strip()
    if not text:
        return {"ok": False, "reasons": ["draft is empty"], "warnings": []}

    if len(text) < 20:
        reasons.append("draft is too short to be a real reply")

    if len(text) > _MAX_CHARS:
        reasons.append(
            f"draft is {len(text)} characters, over the "
            f"{_MAX_CHARS} limit for an auto-sent reply"
        )

    body = _body_without_signature(text)

    for group in (_META_PATTERNS, _INTERNAL_PATTERNS, _FORMAT_PATTERNS):
        for reason, pattern in group:
            m = pattern.search(body)
            if m:
                reasons.append(f"{reason} (matched \"{m.group(0).strip()}\")")

    # Internal teammate names in the body (signature already stripped).
    contact_tokens = {
        t.lower() for t in re.findall(r"[A-Za-z]+", contact_name or "")
    }
    for name in _internal_names():
        if name in contact_tokens:
            continue  # the client is called this; not an internal leak
        if re.search(rf"\b{re.escape(name)}\b", body, re.IGNORECASE):
            reasons.append(f"names an internal team member (\"{name}\")")

    if require_signature and SIGNATURE_DOMAIN not in text:
        reasons.append("signature block is missing")

    # Non-blocking: every drafting prompt forbids em dashes, so one getting
    # through means the prompt was not followed, which is worth seeing.
    if "—" in text or "–" in text:
        warnings.append("contains an em dash or en dash")

    ok = not reasons
    if not ok:
        label = f"[GUARD] {category or 'outbound'} draft BLOCKED: "
        print(label + "; ".join(reasons))
    return {"ok": ok, "reasons": reasons, "warnings": warnings}


def guard_failure_notice(category: str, result: dict) -> str:
    """Slack-ready explanation of why a draft was held back."""
    lines = [
        ":no_entry: *Outbound guard blocked this reply. Nothing was sent "
        "to the client and the ticket was left open.*",
        f"*Category:* {category or 'unknown'}",
        "*Why:*",
    ]
    lines += [f"> {r}" for r in result.get("reasons", [])]
    if result.get("warnings"):
        lines.append("*Also noted:* " + "; ".join(result["warnings"]))
    lines.append(
        "Rewrite it in the thread and send, or cancel. Do not click send "
        "on the draft below without reading it."
    )
    return "\n".join(lines)
