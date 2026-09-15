"""
env_utils.py

Read an environment variable the way a human meant it.

WHY THIS EXISTS: Railway, and every other dashboard where a person pastes a
secret into a web form, stores exactly what was typed. Paste
    ZOHO_CRM_REFRESH_TOKEN="1000.abc.def"
and the quotes become part of the value. The token then fails authentication
with an error that says nothing about quoting, and you lose an afternoon.

The same goes for a trailing space or a newline picked up by a copy.

So: strip whitespace, and strip ONE matching pair of surrounding quotes. A
value that legitimately starts and ends with the same quote character is not a
thing we store, and a lost afternoon is.
"""

import os

_QUOTES = ("'", '"')


def clean(value: str | None) -> str:
    """Strip whitespace and one matching pair of surrounding quotes."""
    if not value:
        return ""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in _QUOTES:
        text = text[1:-1].strip()
    return text


def env(name: str, default: str = "") -> str:
    """os.environ.get, with the paste damage undone."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    cleaned = clean(raw)
    return cleaned if cleaned else default
