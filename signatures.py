"""
signatures.py

Single sender-keyed source for the client-facing email signature.

Today the signature is set in ~6 places with two different values:
  - "Vome team"          — system prompt + auto-acknowledgment templates
  - "Sam | Vome support" — slack_reply_handler, ops/draft, waiting-client

This module centralizes them so the upcoming auto-send work can flip the
signature per sender in ONE place.

BEHAVIOR-PRESERVING (this step):
    Nothing is wired to the new "vic"/"sam" senders yet. Every call site
    currently passes a "legacy_*" sender whose output reproduces that site's
    existing string byte-for-byte. The "vic"/"sam" entries are defined for
    the later flip and are intentionally unused for now.

Two accessors:
    signature(sender, lang="en") -> the full closing block a site emits
        (e.g. "Best,\\n\\nSam | Vome support\\nsupport.vomevolunteer.com").
        Used by sites that drop the whole block into the message/prompt.
    signature_name(sender) -> just the signer NAME line
        (e.g. "Sam | Vome support"). Used by prompt-instruction sites that
        phrase the sign-off their own way and only need the name, so their
        prompt text stays byte-identical.
"""

import re

SIGNATURE_DOMAIN = "support.vomevolunteer.com"

# Per-sender NAME line — the part that actually varies between senders.
_SENDER_NAMES = {
    # Target model — NOT WIRED YET (flip happens in the behavior steps).
    "vic": "Vic",
    "sam": "Sam | Vome team",
    # Legacy values — used now to reproduce current output exactly.
    "legacy_sam_support": "Sam | Vome support",
    "legacy_vome_team": "Vome team",
}

# Full closing blocks, keyed [sender][lang]. The legacy entries are
# byte-exact reproductions of the strings embedded at each call site today
# (note: the French auto-ack templates close with "Cordialement,").
_SIGNATURE_BLOCKS = {
    "legacy_sam_support": {
        "en": "Best,\n\nSam | Vome support\nsupport.vomevolunteer.com",
    },
    "legacy_vome_team": {
        "en": "Best,\n\nVome team\nsupport.vomevolunteer.com",
        "fr": "Cordialement,\n\nVome team\nsupport.vomevolunteer.com",
    },
    # Target senders — now LIVE (wired in the behavior step). Full closing
    # blocks; French variants use "Cordialement,".
    "vic": {
        "en": "Best,\n\nVic\nSupport Team\nVome Volunteer\nsupport.vomevolunteer.com",
        "fr": "Cordialement,\n\nVic\nSupport Team\nVome Volunteer\nsupport.vomevolunteer.com",
    },
    "sam": {
        "en": "Best,\n\nSam | Vome team\nsupport.vomevolunteer.com",
        "fr": "Cordialement,\n\nSam | Vome team\nsupport.vomevolunteer.com",
    },
}


def signature_name(sender: str) -> str:
    """Return just the signer NAME line for *sender*."""
    return _SENDER_NAMES[sender]


def signature(sender: str, lang: str = "en") -> str:
    """Return the full client-facing closing block for *sender*.

    Falls back to the English block when *lang* has no dedicated form.
    """
    blocks = _SIGNATURE_BLOCKS[sender]
    return blocks.get(lang, blocks["en"])


# ---------------------------------------------------------------------------
# Programmatic signing for model-generated drafts.
#
# The model is now instructed to write the body only (no closing/signature),
# so we append the correct signature in code. sign_message() also defends
# against double-signing: if the model still emits a trailing sign-off, it is
# stripped before the real signature is appended.
# ---------------------------------------------------------------------------

# A line that is purely separators / decoration (e.g. the "----" rule the
# STEP 8 note format puts between the draft and the analysis).
_SEP_LINE_RE = re.compile(r"^[\s─\-=_*~]+$")

# Any line announcing the internal AGENT ANALYSIS section. Used to keep the
# signature at the END OF THE DRAFT BODY in compound (draft + analysis)
# outputs, rather than after the analysis.
_ANALYSIS_RE = re.compile(r"(?im)^.*AGENT ANALYSIS.*$")

# Closing words the model might emit despite the no-signature instruction.
_CLOSING_WORDS = {
    "best", "cordialement", "regards", "best regards", "kind regards",
    "sincerely", "cheers", "warm regards", "warmly", "best wishes",
    "sincerement", "sincèrement", "bien a vous", "bien à vous",
}

# Trailing standalone name / domain lines that belong to a sign-off.
_SIGNOFF_NAME_LINES = {
    "vic", "sam", "vome team", "sam | vome team", "sam | vome support",
    "vome support", "equipe vome", "équipe vome",
    "support team", "vome volunteer",
    "support.vomevolunteer.com",
}


def _norm_lang(lang: str | None) -> str:
    """Map a language name/code to the signature lang key ("en"/"fr")."""
    return "fr" if str(lang or "").lower().startswith("fr") else "en"


def _strip_trailing_signoff(text: str) -> str:
    """Remove a trailing sign-off block (closing word, name/domain lines,
    separators, blank lines) from the end of *text*. Leaves the body intact
    when it does not end in a recognizable sign-off."""
    lines = text.rstrip("\n").split("\n")
    while lines:
        last = lines[-1].strip()
        low = last.rstrip(",").lower()
        if (
            not last
            or _SEP_LINE_RE.match(last)
            or low in _CLOSING_WORDS
            or low in _SIGNOFF_NAME_LINES
        ):
            lines.pop()
            continue
        break
    return "\n".join(lines).rstrip()


def sign_message(text: str, sender: str, lang: str = "en") -> str:
    """Append the *sender* signature to a model-generated draft.

    - Guarantees exactly one signature (strips any sign-off the model emitted).
    - For compound output (a DRAFT RESPONSE followed by an AGENT ANALYSIS
      section), the signature is placed at the end of the draft body, before
      the analysis, so the client-facing portion is signed correctly.
    """
    sig = signature(sender, _norm_lang(lang))
    body = text or ""
    m = _ANALYSIS_RE.search(body)
    if not m:
        return f"{_strip_trailing_signoff(body)}\n\n{sig}"
    head = _strip_trailing_signoff(body[:m.start()])
    tail = body[m.start():].rstrip()
    return f"{head}\n\n{sig}\n\n{tail}"
