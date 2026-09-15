"""
test_kb_search_sql.py

The anchor incident: every knowledge-base lookup failed silently for an
unknown stretch of time, with this in the logs on each ticket:

    [KB CONTEXT] article search failed: (psycopg2.errors.SyntaxError)
    syntax error at or near ":"
    ... plainto_tsquery(:cfg::regconfig, %(q)s) ...
    [parameters: {'q': '...', 'limit': 3}]

Note what is missing from those parameters: cfg. SQLAlchemy's text() bind
regex ends with a negative lookahead that rejects a name followed by a colon,
so a Postgres cast written directly after a bind name stops the name being a
bind parameter. It was handed to Postgres literally.

The agent kept answering tickets, just without any KB context, so nothing
crashed and no test failed. These tests make that class of mistake loud.

Run:  py -m pytest test_kb_search_sql.py -q
"""

import re

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from database import _kb_search_sql


@pytest.mark.parametrize("language", [None, "en", "fr", "es"])
def test_cfg_is_actually_a_bind_parameter(language):
    """The exact failure. cfg must survive as a bind, not become literal SQL."""
    bound = text(_kb_search_sql(language))._bindparams
    assert "cfg" in bound, "cfg stopped being a bind parameter"
    assert "q" in bound
    assert "limit" in bound


@pytest.mark.parametrize("language, expected", [
    ("en", True), ("fr", True), (None, False), ("es", False),
])
def test_lang_bind_matches_the_language_filter(language, expected):
    """A bind with no value raises at execute time, so the two must agree."""
    sql = _kb_search_sql(language)
    assert ("a.language = :lang" in sql) is expected
    assert ("lang" in text(sql)._bindparams) is expected


@pytest.mark.parametrize("language", [None, "en", "fr"])
def test_no_bind_name_leaks_into_the_compiled_sql(language):
    compiled = str(
        text(_kb_search_sql(language)).compile(
            dialect=postgresql.psycopg2.dialect()
        )
    )
    for name in (":cfg", ":q", ":limit", ":lang"):
        assert name not in compiled, f"{name} reached Postgres literally"


def test_database_module_has_no_cast_directly_after_a_bind():
    """The general form of the bug, anywhere in the module.

    ":name::type" is the trap. Use CAST(:name AS type).
    """
    src = open("database.py", encoding="utf-8").read()
    offenders = re.findall(r":\w+::\w+", src)
    assert not offenders, (
        f"bind name followed by a cast, these are not bind parameters: "
        f"{offenders}. Use CAST(:name AS type)."
    )


def test_the_regconfig_cast_is_still_there():
    """Guard the fix itself: stemming must stay language-aware.

    Dropping the cast to make the bind work would silently fall back to the
    default text search config and break French stemming.
    """
    sql = _kb_search_sql("fr")
    assert sql.count("CAST(:cfg AS regconfig)") == 2
