"""
error_reports.py

One place that decides whether a ticket is an automated "Vome Error Report".

WHAT THESE ARE
--------------
The web app's ErrorBoundary offers the user a mailto link when the UI crashes.
It sends to support with the subject "Vome Error Report" and a body that opens
with "=== Vome Error Report ===" followed by a stack trace, the build hash, the
route, and browser details.

WHY THEY ARE TREATED DIFFERENTLY
--------------------------------
Nobody wrote these by hand. The person who triggered one rarely remembers the
crash, and almost never recognizes a later "the issue you reported is fixed"
email as relating to anything they did. So:

  * intake does not auto-acknowledge them (they are left as New for Sam)
  * the ON PROD flow does not email a resolution; the Zoho ticket is just
    closed silently

Both call sites share this detector so the two behaviors cannot drift apart.
"""

# Markers as the web app writes them (vome-react src/components/ErrorBoundary.js:
# the mailto subject and the first line of the generated body).
_MARKERS = (
    "vome error report",
    "error report ===",
)


def is_error_report(*text_parts: str | None) -> bool:
    """True when any of *text_parts* carries an error-report marker.

    Pass whatever is available: subject, description, ClickUp task title, etc.
    Matching is case insensitive.

    >>> is_error_report("Vome Error Report", "")
    True
    >>> is_error_report("", "=== Vome Error Report === TypeError: x is null")
    True
    >>> is_error_report("Shifts not saving", "The save button does nothing")
    False
    """
    combined = " ".join(p for p in text_parts if p).lower()
    return any(marker in combined for marker in _MARKERS)
