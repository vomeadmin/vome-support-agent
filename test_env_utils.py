"""
test_env_utils.py

The anchor incident: a Zoho CRM refresh token was pasted into the Railway
dashboard as

    ZOHO_CRM_REFRESH_TOKEN="1000.ea3a....62eb..."

Railway stored the quotes as part of the value. Every CRM write then failed
with an authentication error that said nothing about quoting, and the Slack
post just read "Could not create the CRM lead."

Run:  py -m pytest test_env_utils.py -q
"""

import pytest

from env_utils import clean, env


@pytest.mark.parametrize("raw, expected", [
    ('"1000.abc.def"', "1000.abc.def"),
    ("'1000.abc.def'", "1000.abc.def"),
    ("  1000.abc.def  ", "1000.abc.def"),
    ('"  1000.abc.def  "', "1000.abc.def"),
    ("1000.abc.def\n", "1000.abc.def"),
    ("1000.abc.def", "1000.abc.def"),
])
def test_paste_damage_is_undone(raw, expected):
    assert clean(raw) == expected


@pytest.mark.parametrize("raw", [
    '"unbalanced',
    "unbalanced\"",
    "'mismatched\"",
])
def test_unbalanced_quotes_are_left_alone(raw):
    """Only a matching pair is stripped. A stray quote may be real."""
    assert clean(raw) == raw.strip()


def test_only_one_pair_is_stripped():
    """A value that genuinely contains quotes keeps its inner pair."""
    assert clean('""quoted""') == '"quoted"'


def test_empty_and_none():
    assert clean("") == ""
    assert clean(None) == ""
    assert clean("   ") == ""


def test_env_reads_and_cleans(monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", '"abc123"')
    assert env("SOME_TOKEN") == "abc123"


def test_env_falls_back_when_missing(monkeypatch):
    monkeypatch.delenv("SOME_TOKEN", raising=False)
    assert env("SOME_TOKEN", "fallback") == "fallback"


def test_a_quoted_empty_string_uses_the_default(monkeypatch):
    """'""' is somebody clearing a variable, not setting it to two quotes."""
    monkeypatch.setenv("SOME_TOKEN", '""')
    assert env("SOME_TOKEN", "fallback") == "fallback"


def test_channel_ids_survive(monkeypatch):
    """The failure mode this prevents: channel_not_found on a quoted id."""
    monkeypatch.setenv("CHAN", '"C0C1MF2NMDJ"')
    assert env("CHAN") == "C0C1MF2NMDJ"
