"""
knowledge_synthesis.py

Selection and map-reduce for the knowledge book.

The original synthesiser joined every summary in a category, cut the
string at 15,000 characters and sent that to Claude. A real summary is
about 900 characters, so roughly 16 entries reached the model, and
because `get_all_analyses` orders by `analyzed_at` ascending they were
the 16 *oldest-analysed* ones. The "bug" section reported 266 source
tickets and was written from about sixteen of them. Mining more tickets
raised a counter and changed nothing else.

This module fixes the two halves of that:

  * `rank_and_select` orders by training value, then recency, then
    round-robins across modules, so a section covers the spread of a
    category instead of one arbitrary window of it.
  * `map_reduce_section` batches the selected entries under the same
    character budget, extracts observations from each batch, then merges
    the notes into one section. Coverage stops being capped by a single
    prompt window.

Small categories still take the single-call path, which is both cheaper
and exactly what they got before.
"""

import time
from collections import OrderedDict

# training_value from the analysis JSON, best first. Anything missing or
# unrecognised sorts last rather than being dropped.
TRAINING_VALUE_RANK = {"high": 0, "medium": 1, "low": 2}
_UNRANKED = 3

# Characters of summary text per Claude call. Matches the old whole-section
# budget, but it is now the size of one batch rather than the ceiling on
# the entire section.
MAP_BATCH_CHARS = 15000

# Ceiling on entries considered for one section. Today every category is
# comfortably under this, so nothing is dropped. It exists so the weekly
# job cannot grow without bound as the corpus does: at this size a large
# category costs about 25 map calls.
MAX_ENTRIES_PER_SECTION = 400

SUMMARY_SEPARATOR = "\n\n---\n\n"


def _value_rank(value: str | None) -> int:
    return TRAINING_VALUE_RANK.get((value or "").strip().lower(), _UNRANKED)


def rank_and_select(
    entries: list[dict],
    value_of,
    recency_of,
    module_of,
    limit: int = MAX_ENTRIES_PER_SECTION,
) -> list[dict]:
    """Pick the most instructive entries, spread across modules.

    `value_of`, `recency_of` and `module_of` are callables taking one
    entry. `recency_of` should return something sortable where larger is
    more recent (a ticket number and a closed-at timestamp both work);
    return 0 when unknown.

    Ranking runs first so that the round-robin hands back each module's
    best entries, not its oldest.
    """
    if not entries:
        return []

    ranked = sorted(
        entries,
        key=lambda e: (_value_rank(value_of(e)), -_as_number(recency_of(e))),
    )

    # Round-robin across modules so one noisy module cannot fill a
    # section on its own.
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for entry in ranked:
        key = (module_of(entry) or "unknown").strip().lower() or "unknown"
        groups.setdefault(key, []).append(entry)

    out: list[dict] = []
    while any(groups.values()):
        for key in list(groups):
            bucket = groups[key]
            if not bucket:
                continue
            out.append(bucket.pop(0))
            if limit and len(out) >= limit:
                return out
    return out


def _as_number(value) -> float:
    """Coerce a recency key to a number. Unknown sorts oldest."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        pass
    # A datetime, or anything else orderable by string, still needs a
    # number here. Fall back to its timestamp when it has one.
    timestamp = getattr(value, "timestamp", None)
    if callable(timestamp):
        try:
            return float(timestamp())
        except Exception:
            return 0.0
    return 0.0


def batch_summaries(
    summaries: list[str], max_chars: int = MAP_BATCH_CHARS
) -> list[list[str]]:
    """Group summaries into batches under `max_chars`.

    A summary is never split. One that exceeds the budget on its own gets
    a batch to itself and is truncated by the caller's prompt, which is
    the same outcome it had before, only now it does not consume the
    whole section's window.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    sep = len(SUMMARY_SEPARATOR)

    for summary in summaries:
        length = len(summary)
        if current and size + sep + length > max_chars:
            batches.append(current)
            current = [summary]
            size = length
        else:
            size += (sep + length) if current else length
            current.append(summary)

    if current:
        batches.append(current)
    return batches


def map_reduce_section(
    summaries: list[str],
    section_title: str,
    direct_prompt,
    map_prompt,
    reduce_prompt,
    client,
    model: str,
    delay: float = 2.0,
    max_chars: int = MAP_BATCH_CHARS,
    map_max_tokens: int = 2000,
    reduce_max_tokens: int = 4000,
) -> str | None:
    """Synthesise one section from any number of summaries.

    `direct_prompt(joined)` is used when everything fits in one call,
    which keeps small categories on exactly the path they had before.
    Otherwise `map_prompt(joined, index, total)` extracts observations
    per batch and `reduce_prompt(notes, batch_count)` merges them.

    Returns the section markdown, or None if every call failed.
    """
    if not summaries:
        return None

    batches = batch_summaries(summaries, max_chars=max_chars)

    if len(batches) == 1:
        return _call(
            client, model, direct_prompt(SUMMARY_SEPARATOR.join(batches[0])),
            reduce_max_tokens,
        )

    print(
        f"[BOOK] {section_title}: {len(summaries)} entries across "
        f"{len(batches)} batches"
    )

    notes: list[str] = []
    for i, batch in enumerate(batches, start=1):
        text = _call(
            client,
            model,
            map_prompt(SUMMARY_SEPARATOR.join(batch), i, len(batches)),
            map_max_tokens,
        )
        if text:
            notes.append(f"### Observations from batch {i}\n\n{text}")
            print(f"[BOOK]   batch {i}/{len(batches)} done")
        else:
            print(f"[BOOK]   batch {i}/{len(batches)} FAILED, skipping")
        time.sleep(delay)

    if not notes:
        return None

    return _call(
        client, model,
        reduce_prompt("\n\n".join(notes), len(batches)),
        reduce_max_tokens,
    )


def _call(client, model: str, prompt: str, max_tokens: int) -> str | None:
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        print(f"[BOOK] synthesis call failed: {e}")
        return None


def coverage_note(selected: int, total: int) -> str:
    """One line for the top of a section saying what it was built from."""
    if selected >= total:
        return f"*Synthesised from all {total} analysed entries.*"
    return (
        f"*Synthesised from the {selected} most instructive of {total} "
        f"analysed entries, ranked by training value and spread across "
        f"modules.*"
    )
