"""
test_outbound_guard.py

Regression tests for the outbound guard.

The anchor test is INCIDENT_8945: the exact text that was auto-emailed to
Summit Metro Parks on 2026-09-07 on ticket #8945. It must never validate.

The must-pass fixtures are real client emails from the same thread, so a
guard that blocks ordinary support copy fails these.

Run:  py -m pytest test_outbound_guard.py -q
"""

from outbound_guard import validate_client_message


# The message that actually went out. Model commentary addressed to its
# operator, signed "Vic" by sign_message(), sent, ticket closed.
INCIDENT_8945 = """I need to pause before drafting anything here.

The prompt asks me to write a "feature works as intended / how-to explanation" message. But that is not what this ticket is. Reading the dev notes carefully:

**This ticket contains multiple active bugs**, several of which are unresolved or unverified:

- Item 1 (volunteer schedules not loading from profiles): still occurring, possibly not fixed despite a prior ticket being closed
- Item 5 (4 hours logging as 400 hours): flagged as a data integrity issue, not just a display glitch, still awaiting a screenshot to confirm scope
- Item 4 (reports slow or requiring refresh): likely related to the original error report
- Item 6 (shift notifications not linking to specific shifts): flagged for investigation
- The original error report (reservations-hours-log, build 2edc4af): not verified as fixed before a fix claim was sent to the client
- Sanjay's "update pushed" message was sent without confirming it addressed this specific error

Lisa's latest message (Sep 3) is simply: "Will do. Thanks for this reply and outlining next steps."

**There is nothing to respond to right now.** The team owes Lisa updates on the seven issues, not the reverse. She has acknowledged the previous message and is waiting.

**Why I cannot write the requested draft:**

1. This is not a how-to or feature explanation situation. These are real bugs under active investigation.
2. Writing a message explaining "how the feature works" would be factually wrong and would mislead Lisa about the nature of her reported issues.
3. Several items have no confirmed resolution. Drafting a response that implies things are handled would repeat the error already made on Sep 2 (fix claim sent before verification).
4. The no-confirmation rule in the system prompt applies directly: I must never confirm, diagnose, or describe a bug status unless verified.

**What should actually happen:**

The team needs to resolve or confirm status on each of the seven items before any outbound message goes to Lisa. When there is something confirmed to report, I can draft a structured update covering each item by status. That draft should be reviewed by Sam before sending, per routing rules for this account tier.

If you would like, I can draft that structured update template now, with placeholders for each item's status to be filled in once the engineering team confirms them. That would be the appropriate next output for this ticket.

Best,

Vic
Support Team
Vome Volunteer
support.vomevolunteer.com"""


# A real, correct client email from the same ticket (Sep 2, signed Sam).
GOOD_LONG = """Hi Lisa,

Thank you so much for sharing all of this insight. That is a genuinely useful list, and it tells us more than seven separate reports would have.

We will review each of these workflows on our end and come back to you.

One thing that would help us: we would appreciate it if Jessica could submit individual tickets for any of these that are still occurring. Grouping them like this is great for spotting patterns, but a ticket per live issue lets us track and resolve each one properly rather than losing them inside one thread.

On the individual volunteer schedules not loading from their profiles, could you try clearing the cookies and cache on your browser, or opening it in a private browsing window, then loading the page again? Let us know either way whether that changes anything, it will help us narrow down where the problem sits.

Sam | Vome team
support.vomevolunteer.com"""


# A real, correct short resolution email from the same ticket.
GOOD_SHORT = """Hi Lisa, we've pushed an update that should take care of the error you reported in the reservations and hours log. Could you give it another try and let us know if everything looks right on your end?

Best,

Vic
Support Team
Vome Volunteer
support.vomevolunteer.com"""


def test_incident_8945_is_blocked():
    result = validate_client_message(
        INCIDENT_8945, category="user_education", contact_name="Lisa Meranti"
    )
    assert result["ok"] is False
    # Not just blocked, blocked for the right set of reasons.
    joined = " | ".join(result["reasons"]).lower()
    assert "prompt" in joined
    assert "markdown" in joined
    assert "internal team member" in joined
    assert any("draft" in r.lower() for r in result["reasons"])


def test_good_long_reply_passes():
    result = validate_client_message(
        GOOD_LONG, category="needs_info", contact_name="Lisa Meranti"
    )
    assert result["ok"] is True, result["reasons"]


def test_good_short_resolution_passes():
    result = validate_client_message(
        GOOD_SHORT, category="on_prod", contact_name="Lisa Meranti"
    )
    assert result["ok"] is True, result["reasons"]


def test_empty_draft_is_blocked():
    result = validate_client_message("")
    assert result["ok"] is False
    assert result["reasons"] == ["draft is empty"]


def test_short_draft_is_blocked():
    result = validate_client_message("Fixed.", require_signature=False)
    assert result["ok"] is False


def test_missing_signature_is_blocked():
    result = validate_client_message(
        "Hi Lisa, the issue you reported is resolved. Please give it "
        "another try and let us know how it goes."
    )
    assert result["ok"] is False
    assert any("signature" in r for r in result["reasons"])


def test_refusal_phrasing_is_blocked():
    draft = (
        "I cannot write this message because the ticket is still open.\n\n"
        "Best,\n\nVic\nSupport Team\nVome Volunteer\n"
        "support.vomevolunteer.com"
    )
    result = validate_client_message(draft, category="on_prod")
    assert result["ok"] is False


def test_internal_tooling_mention_is_blocked():
    draft = (
        "Hi Lisa, we have logged this on our ClickUp board and the dev will "
        "take a look shortly. Thanks for your patience.\n\n"
        "Best,\n\nVic\nSupport Team\nVome Volunteer\n"
        "support.vomevolunteer.com"
    )
    result = validate_client_message(draft, category="needs_info")
    assert result["ok"] is False


def test_teammate_name_in_body_is_blocked():
    draft = (
        "Hi Lisa, Sanjay pushed a change for this yesterday so it should be "
        "working now. Let us know if anything is still off.\n\n"
        "Best,\n\nVic\nSupport Team\nVome Volunteer\n"
        "support.vomevolunteer.com"
    )
    result = validate_client_message(draft, category="on_prod")
    assert result["ok"] is False


def test_client_named_sam_is_not_flagged_as_internal():
    draft = (
        "Hi Sam, thanks for flagging this. The issue you reported has been "
        "resolved, so please give it another try when you get a chance.\n\n"
        "Best,\n\nVic\nSupport Team\nVome Volunteer\n"
        "support.vomevolunteer.com"
    )
    result = validate_client_message(
        draft, category="on_prod", contact_name="Sam Whitfield"
    )
    assert result["ok"] is True, result["reasons"]


def test_runaway_length_is_blocked():
    filler = (
        "Thanks for your patience while we looked into this for you. "
    ) * 80
    draft = f"Hi Lisa,\n\n{filler}\n\nBest,\n\nVic\nsupport.vomevolunteer.com"
    result = validate_client_message(draft, category="on_prod")
    assert result["ok"] is False
    assert any("characters" in r for r in result["reasons"])


def test_em_dash_warns_but_does_not_block():
    draft = (
        "Hi Lisa, the issue you reported is resolved — please give it "
        "another try and let us know how it goes.\n\n"
        "Best,\n\nVic\nSupport Team\nVome Volunteer\n"
        "support.vomevolunteer.com"
    )
    result = validate_client_message(draft, category="on_prod")
    assert result["ok"] is True, result["reasons"]
    assert result["warnings"]
