"""
Flatten the in-app Setup Guide into a markdown digest the support agent
can turn into knowledge.

Run manually whenever the guide content changes:
    python scripts/sync_setup_guide.py

Output:
    knowledge_book/setup_guide_source.md   -- readable digest
    knowledge_book/setup_guide_source.json -- structured form, consumed by
                                              setup_guide.py

Why this exists rather than just syncing the help center:

The Setup Guide lives in two places and neither is complete on its own.
The Zoho help center holds the articles it links to, which kb_sync
already indexes. But the guide's own substance, the stage ordering, the
decision questions and the tips and warnings attached to each section,
is hardcoded in vome-react: the structure in setupGuideContent.js and
every string in translations/postlogin.js under `sg_*` keys. None of
that is in Zoho, so none of it ever reached the agent.

This mirrors scripts/sync_landing_strings.py, which does the same job for
the landing page marketing copy.

The parser reads the JS directly (no Node dependency). It relies on the
regular shapes in setupGuideContent.js:

    export const SETUP_GUIDE_STAGES = [ { key: STAGE.X, labelKey: '...',
                                          sections: xSections }, ... ]
    const xSections = [ { key: '...', titleKey: '...', introKey: '...',
                          articles: [{ slug: '...' }], blocks: [...] }, ]

If those shapes change the counts printed at the end will drop, which is
the signal to come back here.
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REACT_ROOT = REPO_ROOT.parent / "web-app" / "vome-react" / "src"
CONTENT_JS = REACT_ROOT / "views" / "org" / "setupGuide" / "setupGuideContent.js"
TRANSLATIONS = REACT_ROOT / "translations" / "postlogin.js"

OUT_DIR = REPO_ROOT / "knowledge_book"
OUT_MD = OUT_DIR / "setup_guide_source.md"
OUT_JSON = OUT_DIR / "setup_guide_source.json"

# Block types that carry advice worth teaching. Structural blocks
# (hierarchy diagrams, embeds) carry no prose.
ADVICE_BLOCK_TYPES = {"tip", "warning", "note", "callout", "decision"}


# ---------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------

def load_english_strings() -> dict[str, str]:
    """Map `sg_*` translation keys to their English text.

    postlogin.js holds English first and French later in the same file,
    so the FIRST occurrence of a key is the English one.
    """
    text = TRANSLATIONS.read_text(encoding="utf-8", errors="replace")
    out: dict[str, str] = {}
    # key: "value" or key: 'value', tolerating escaped quotes.
    pattern = re.compile(
        r"^\s*(sg_[A-Za-z0-9_]+)\s*:\s*"
        r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')',
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        key, raw = match.group(1), match.group(2)
        if key in out:
            continue  # first wins, which is English
        out[key] = _unquote(raw)
    return out


def _unquote(raw: str) -> str:
    body = raw[1:-1]
    body = body.replace('\\"', '"').replace("\\'", "'")
    body = body.replace("\\n", " ").replace("\\t", " ")
    return re.sub(r"\s+", " ", body).strip()


# ---------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------

def _sections_block(js: str, var_name: str) -> str:
    """Return the source of `const <var_name> = [ ... ];`."""
    start = js.find(f"const {var_name} = [")
    if start == -1:
        return ""
    # The arrays are top level, so the terminator is `];` at column 0.
    end = js.find("\n];", start)
    return js[start:end] if end != -1 else js[start:]


_SECTION_START = re.compile(r"^  \{\s*$", re.MULTILINE)


def parse_sections(block: str) -> list[dict]:
    """Split a sections array into one dict per section."""
    if not block:
        return []
    # Sections are the two-space indented objects inside the array.
    starts = [m.start() for m in _SECTION_START.finditer(block)]
    sections = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(block)
        chunk = block[start:end]
        key = _first(chunk, r"key:\s*'([^']+)'")
        if not key:
            continue
        advice_keys = _advice_keys(chunk)
        # Article entries carry their own labelKey. Those are link text
        # ("Mastering the Vome fundamentals"), not guidance, so they must
        # not end up in the prose bucket.
        article_label_keys = set(
            re.findall(r"slug:\s*'[^']+',\s*labelKey:\s*'(sg_[^']+)'", chunk)
        )
        excluded = set(advice_keys) | article_label_keys
        sections.append({
            "key": key,
            "title_key": _first(chunk, r"titleKey:\s*'([^']+)'"),
            "intro_key": _first(chunk, r"introKey:\s*'([^']+)'"),
            "articles": re.findall(r"slug:\s*'([^']+)'", chunk),
            "block_keys": advice_keys,
            "question_keys": [
                k for k in dict.fromkeys(
                    re.findall(
                        r"(?:textKey|labelKey|helpKey):\s*'(sg_[^']+)'", chunk
                    )
                )
                if k not in excluded
            ],
        })
    return sections


def _advice_keys(chunk: str) -> list[str]:
    """Translation keys for tip/warning style blocks in a section."""
    keys = []
    for match in re.finditer(r"\{\s*type:\s*'([a-z]+)'(.*?)\n      \}",
                             chunk, re.DOTALL):
        block_type, body = match.group(1), match.group(2)
        if block_type not in ADVICE_BLOCK_TYPES:
            continue
        for key in re.findall(r"(?:titleKey|textKey):\s*'(sg_[^']+)'", body):
            keys.append(key)
    return keys


def _first(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    return match.group(1) if match else ""


def parse_stages(js: str) -> list[dict]:
    """Ordered stages, each with its sections array name."""
    start = js.find("export const SETUP_GUIDE_STAGES = [")
    if start == -1:
        return []
    end = js.find("\n];", start)
    block = js[start:end if end != -1 else len(js)]

    stages = []
    for match in re.finditer(
        r"key:\s*STAGE\.([A-Z_]+),\s*labelKey:\s*'([^']+)',"
        r"\s*sections:\s*([A-Za-z0-9_]+)",
        block,
    ):
        stages.append({
            "key": match.group(1).lower(),
            "label_key": match.group(2),
            "sections_var": match.group(3),
        })
    return stages


# ---------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------

def build() -> dict:
    js = CONTENT_JS.read_text(encoding="utf-8", errors="replace")
    strings = load_english_strings()

    def s(key: str) -> str:
        return strings.get(key, "") if key else ""

    stages = parse_stages(js)
    if not stages:
        raise SystemExit(
            "No stages parsed. SETUP_GUIDE_STAGES shape changed; see the "
            "module docstring."
        )

    out_stages = []
    all_slugs: list[str] = []
    for stage in stages:
        sections = parse_sections(_sections_block(js, stage["sections_var"]))
        out_sections = []
        for section in sections:
            all_slugs.extend(section["articles"])
            advice = [s(k) for k in section["block_keys"]]
            advice_text = {a for a in advice if a}
            questions = [
                s(k) for k in section["question_keys"]
                # Short strings are button labels and headings, not
                # guidance. The threshold is what separates "Continue"
                # from a sentence that teaches something.
                if s(k) and len(s(k)) > 40 and s(k) not in advice_text
            ]
            out_sections.append({
                "key": section["key"],
                "title": s(section["title_key"]) or section["key"],
                "intro": s(section["intro_key"]),
                "articles": section["articles"],
                "advice": [a for a in advice if a],
                "prompts": questions[:20],
            })
        out_stages.append({
            "key": stage["key"],
            "label": s(stage["label_key"]) or stage["key"],
            "sections": out_sections,
        })

    return {
        "generated": date.today().isoformat(),
        "source": str(CONTENT_JS.relative_to(REACT_ROOT.parent.parent)),
        "stages": out_stages,
        "all_article_slugs": sorted(set(all_slugs)),
    }


def to_markdown(data: dict) -> str:
    lines = [
        "# Vome Setup Guide (in-app content)",
        "",
        f"*Generated {data['generated']} from {data['source']}. "
        f"Do not edit by hand: run scripts/sync_setup_guide.py.*",
        "",
        "The stages an admin works through in the in-app Setup Guide, the "
        "decisions each section asks them to settle, the tips attached to "
        "each, and the help center articles it links for detail.",
        "",
    ]
    for stage in data["stages"]:
        lines.append(f"## Stage: {stage['label']}")
        lines.append("")
        for section in stage["sections"]:
            lines.append(f"### {section['title']}")
            if section["intro"]:
                lines.append("")
                lines.append(section["intro"])
            if section["prompts"]:
                lines.append("")
                lines.append("Key points:")
                lines.extend(f"- {p}" for p in section["prompts"])
            if section["advice"]:
                lines.append("")
                lines.append("Tips and warnings:")
                lines.extend(f"- {a}" for a in section["advice"])
            if section["articles"]:
                lines.append("")
                lines.append(
                    "Articles: " + ", ".join(section["articles"])
                )
            lines.append("")
    return "\n".join(lines)


def main():
    if not CONTENT_JS.exists():
        raise SystemExit(f"Not found: {CONTENT_JS}")
    if not TRANSLATIONS.exists():
        raise SystemExit(f"Not found: {TRANSLATIONS}")

    data = build()
    OUT_DIR.mkdir(exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    markdown = to_markdown(data)
    OUT_MD.write_text(markdown, encoding="utf-8")

    sections = sum(len(s["sections"]) for s in data["stages"])
    advice = sum(
        len(sec["advice"]) for st in data["stages"] for sec in st["sections"]
    )
    prompts = sum(
        len(sec["prompts"]) for st in data["stages"] for sec in st["sections"]
    )
    print(f"Stages:   {len(data['stages'])}")
    print(f"Sections: {sections}")
    print(f"Decisions captured: {prompts}")
    print(f"Tips/warnings captured: {advice}")
    print(f"Linked articles: {len(data['all_article_slugs'])}")
    print(f"Markdown: {len(markdown)} chars -> {OUT_MD}")
    print(f"JSON: {OUT_JSON}")


if __name__ == "__main__":
    if sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
