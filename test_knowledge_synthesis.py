"""Tests for knowledge book selection and map-reduce.

The bug these pin: the old synthesiser joined a category's summaries,
cut the string at 15,000 characters and sent that. At ~900 characters a
summary that is about 16 entries, and because the rows came back ordered
by `analyzed_at` ascending they were the 16 oldest-analysed. A section
reporting 266 source tickets was written from sixteen of them, and
mining more tickets changed nothing.
"""
import os

for _k in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLICKUP_API_TOKEN",
           "ZOHO_ORG_ID", "ZOHO_FROM_ADDRESS", "DATABASE_URL"):
    os.environ.setdefault(_k, "test-dummy")

import knowledge_synthesis as ks  # noqa: E402


def _entry(eid, value="medium", recency=0, module="general"):
    return {"id": eid, "value": value, "recency": recency, "module": module}


def _select(entries, **kw):
    return ks.rank_and_select(
        entries,
        value_of=lambda e: e["value"],
        recency_of=lambda e: e["recency"],
        module_of=lambda e: e["module"],
        **kw,
    )


# ---------------------------------------------------------------- ranking

def test_high_training_value_beats_low():
    got = _select([
        _entry("low", value="low"),
        _entry("high", value="high"),
        _entry("medium", value="medium"),
    ])
    assert [e["id"] for e in got] == ["high", "medium", "low"]


def test_recent_beats_old_at_equal_value():
    got = _select([
        _entry("old", value="high", recency=1),
        _entry("new", value="high", recency=99),
    ])
    assert [e["id"] for e in got] == ["new", "old"]


def test_unknown_training_value_sorts_last_but_is_not_dropped():
    got = _select([
        _entry("blank", value=None),
        _entry("weird", value="???"),
        _entry("good", value="high"),
    ])
    assert got[0]["id"] == "good"
    assert len(got) == 3, "an unrecognised value must not lose the entry"


def test_ticket_numbers_as_strings_still_order_by_recency():
    """analyzed_tickets stores ticket_number as text."""
    got = _select([
        _entry("older", value="high", recency="7001"),
        _entry("newer", value="high", recency="9002"),
    ])
    assert [e["id"] for e in got] == ["newer", "older"]


def test_a_missing_recency_key_does_not_raise():
    got = ks.rank_and_select(
        [{"id": "a"}, {"id": "b"}],
        value_of=lambda e: e.get("value"),
        recency_of=lambda e: e.get("nope"),
        module_of=lambda e: e.get("module"),
    )
    assert len(got) == 2


# ----------------------------------------------------------- stratifying

def test_one_noisy_module_cannot_fill_the_section():
    """The real failure mode: 'scheduling' dominating the bug section."""
    entries = [
        _entry(f"sched{i}", value="high", module="scheduling")
        for i in range(10)
    ] + [
        _entry("forms1", value="high", module="forms"),
        _entry("chat1", value="high", module="chat"),
    ]

    got = _select(entries, limit=4)
    modules = [e["module"] for e in got]

    assert "forms" in modules and "chat" in modules
    assert modules.count("scheduling") <= 2


def test_every_entry_survives_when_under_the_limit():
    entries = [_entry(str(i), module=f"m{i % 3}") for i in range(9)]
    got = _select(entries, limit=400)
    assert sorted(e["id"] for e in got) == sorted(str(i) for i in range(9))


def test_limit_is_respected():
    entries = [_entry(str(i), module=f"m{i % 5}") for i in range(100)]
    assert len(_select(entries, limit=7)) == 7


def test_empty_input_is_fine():
    assert _select([]) == []


# -------------------------------------------------------------- batching

def test_batches_stay_under_the_character_budget():
    summaries = ["x" * 900 for _ in range(50)]
    batches = ks.batch_summaries(summaries, max_chars=15000)

    for batch in batches:
        joined = ks.SUMMARY_SEPARATOR.join(batch)
        assert len(joined) <= 15000
    assert sum(len(b) for b in batches) == 50, "no summary may be lost"


def test_nothing_is_truncated_away_the_way_it_used_to_be():
    """50 summaries of 900 chars used to become 16. Now all 50 survive."""
    summaries = [f"summary {i} " + "x" * 890 for i in range(50)]
    batches = ks.batch_summaries(summaries, max_chars=15000)
    flat = [s for b in batches for s in b]
    assert flat == summaries


def test_an_oversized_summary_gets_its_own_batch():
    summaries = ["small", "y" * 40000, "also small"]
    batches = ks.batch_summaries(summaries, max_chars=15000)
    assert ["y" * 40000] in batches


def test_a_single_small_batch_stays_one_batch():
    assert len(ks.batch_summaries(["a", "b", "c"], max_chars=15000)) == 1


# ------------------------------------------------------------ map-reduce

class _FakeClient:
    def __init__(self, replies=None):
        self.calls = []
        self.messages = self
        self._replies = replies

    def create(self, **kwargs):
        self.calls.append(kwargs["messages"][0]["content"])

        class _Block:
            text = (
                self._replies.pop(0) if self._replies else "generated section"
            )

        class _Resp:
            content = [_Block()]

        return _Resp()


def _run(summaries, client, **kw):
    return ks.map_reduce_section(
        summaries,
        section_title="Bug Reports",
        direct_prompt=lambda joined: f"DIRECT::{joined}",
        map_prompt=lambda joined, i, n: f"MAP{i}of{n}::{joined}",
        reduce_prompt=lambda notes, b: f"REDUCE{b}::{notes}",
        client=client,
        model="test-model",
        delay=0,
        **kw,
    )


def test_a_small_category_takes_the_single_call_path():
    """Unchanged behaviour for the categories that always fitted."""
    client = _FakeClient()
    out = _run(["short one", "short two"], client)

    assert len(client.calls) == 1
    assert client.calls[0].startswith("DIRECT::")
    assert out == "generated section"


def test_a_large_category_maps_then_reduces():
    client = _FakeClient()
    summaries = ["x" * 900 for _ in range(50)]

    out = _run(summaries, client, max_chars=15000)

    map_calls = [c for c in client.calls if c.startswith("MAP")]
    reduce_calls = [c for c in client.calls if c.startswith("REDUCE")]
    assert len(map_calls) >= 4, "50 summaries must not fit one window"
    assert len(reduce_calls) == 1
    assert out == "generated section"


def test_every_summary_reaches_a_map_call():
    """The whole point of the change: full coverage, not a prefix."""
    client = _FakeClient()
    summaries = [f"MARKER{i}" + "x" * 890 for i in range(40)]

    _run(summaries, client, max_chars=15000)

    mapped = "".join(c for c in client.calls if c.startswith("MAP"))
    for i in range(40):
        assert f"MARKER{i}" in mapped, f"summary {i} never reached the model"


def test_a_failed_batch_does_not_sink_the_section():
    class _FlakyClient(_FakeClient):
        def create(self, **kwargs):
            self.calls.append(kwargs["messages"][0]["content"])
            if len(self.calls) == 2:
                raise RuntimeError("overloaded")
            return super().create(**kwargs)

    client = _FlakyClient()
    client.calls = []
    out = _run(["x" * 900 for _ in range(50)], client, max_chars=15000)

    assert out == "generated section"


def test_no_summaries_returns_none():
    assert _run([], _FakeClient()) is None


# -------------------------------------------------------- coverage note

def test_coverage_note_says_all_when_nothing_was_dropped():
    assert "all 266" in ks.coverage_note(266, 266)


def test_coverage_note_is_explicit_when_sampling():
    note = ks.coverage_note(400, 900)
    assert "400" in note and "900" in note
