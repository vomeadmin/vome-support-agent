"""
Flatten landing-page/src/i18n/strings.ts into a markdown feature catalog
the support agent can reference when drafting replies.

Run manually whenever the landing-page marketing copy changes:
    python scripts/sync_landing_strings.py

Output:
    knowledge_book/feature_catalog.md  -- consumed by agent.py at startup
    knowledge_book/feature_catalog.json -- structured form for future use

The parser handles the TS file directly (no Node dependency). It expects the
regular `key: { en: "...", fr: "..." }` leaf shape used throughout strings.ts.
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LANDING_STRINGS = REPO_ROOT.parent / "landing-page" / "src" / "i18n" / "strings.ts"
OUT_DIR = REPO_ROOT / "knowledge_book"
OUT_MD = OUT_DIR / "feature_catalog.md"
OUT_JSON = OUT_DIR / "feature_catalog.json"

# Top-level sections that are pure UI chrome, not feature information.
EXCLUDE_TOP_LEVEL = {
    "common",
    "nav",
    "footer",
    "contact_form",
    "contact_page",
    "demo_page",
    "demo_confirmed",
    "video_page",
    "affiliates_page",
    "partners",
    "cta",
    "mockup",
}

# Within each kept section, drop nested objects whose key matches these patterns —
# these are decorative product-screenshot labels, not feature description.
SKIP_GROUP_PATTERNS = [
    re.compile(r"^mock$"),
    re.compile(r".*_mock$"),
    re.compile(r"^mock_.*"),
    re.compile(r"^mockup$"),
    re.compile(r"^preview_mock$"),
]

# Pretty labels for top-level sections.
SECTION_TITLES = {
    "hero": "Hero — homepage value proposition",
    "feature_tabs": "Feature tabs — homepage modules overview",
    "features_page": "Features page — modules overview",
    "module_recruitment": "Module: Recruitment (forms & application workflows)",
    "module_onboarding": "Module: Onboarding",
    "module_scheduling": "Module: Scheduling (shifts & reservations)",
    "module_hours": "Module: Hour tracking",
    "module_recognition": "Module: Recognition & awards",
    "module_comms": "Module: Communications (email, chat, notifications)",
    "module_data": "Module: Database & reporting",
    "module_app": "Module: Mobile app for volunteers",
    "module_integrations": "Module: Integrations (API, Zapier, Salesforce, etc.)",
    "mobile_app": "Mobile app (cross-cutting overview)",
    "enterprise": "Enterprise capabilities",
    "security": "Security & compliance",
    "testimonials": "Customer testimonials",
    "faq": "FAQ",
    "about_us": "About Vome",
    "plans": "Plans & pricing",
}


# ─── Parser ────────────────────────────────────────────────────────────────

def _tokenize(src: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c == '"':
            j = i + 1
            buf = ['"']
            while j < n:
                ch = src[j]
                if ch == "\\" and j + 1 < n:
                    buf.append(src[j])
                    buf.append(src[j + 1])
                    j += 2
                    continue
                if ch == '"':
                    buf.append('"')
                    j += 1
                    break
                buf.append(ch)
                j += 1
            tokens.append(("STR", "".join(buf)))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            tokens.append(("IDENT", src[i:j]))
            i = j
            continue
        if c in "{}:,":
            tokens.append(("PUNCT", c))
            i += 1
            continue
        # numbers / brackets / other — not used in data, skip silently
        i += 1
    return tokens


def _parse_object(tokens, pos):
    assert tokens[pos] == ("PUNCT", "{"), f"Expected {{ at {pos}, got {tokens[pos]}"
    pos += 1
    obj: dict = {}
    while True:
        if pos >= len(tokens):
            raise ValueError("Unexpected EOF inside object")
        tk = tokens[pos]
        if tk == ("PUNCT", "}"):
            return obj, pos + 1
        if tk == ("PUNCT", ","):
            pos += 1
            continue
        # key
        if tk[0] == "IDENT":
            key = tk[1]
        elif tk[0] == "STR":
            key = json.loads(tk[1])
        else:
            raise ValueError(f"Expected key, got {tk} at {pos}")
        pos += 1
        if tokens[pos] != ("PUNCT", ":"):
            raise ValueError(f"Expected : after key {key!r}, got {tokens[pos]}")
        pos += 1
        # value
        vtk = tokens[pos]
        if vtk[0] == "STR":
            obj[key] = json.loads(vtk[1])
            pos += 1
        elif vtk == ("PUNCT", "{"):
            sub, pos = _parse_object(tokens, pos)
            obj[key] = sub
        else:
            raise ValueError(f"Expected value for key {key!r}, got {vtk}")


def parse_strings_file(path: Path) -> dict:
    src = path.read_text(encoding="utf-8")
    marker = "export const strings"
    idx = src.find(marker)
    if idx < 0:
        raise ValueError(f"Could not find {marker!r} in {path}")
    brace = src.find("{", idx)
    if brace < 0:
        raise ValueError("Could not find opening brace of strings object")
    tokens = _tokenize(src[brace:])
    root, _ = _parse_object(tokens, 0)
    return root


# ─── Rendering ─────────────────────────────────────────────────────────────

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """Strip HTML tags and collapse whitespace for catalog display."""
    text = _HTML_TAG_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_leaf(value) -> bool:
    return (
        isinstance(value, dict)
        and "en" in value
        and isinstance(value["en"], str)
        and (len(value) <= 2)  # en + optional fr
    )


def _skip_group(key: str) -> bool:
    return any(p.match(key) for p in SKIP_GROUP_PATTERNS)


def _render_section(name: str, body: dict) -> list[str]:
    title = SECTION_TITLES.get(name, name.replace("_", " ").title())
    lines = [f"## {title}", ""]
    _render_into(body, lines, depth=3)
    return lines


def _render_into(node: dict, lines: list[str], depth: int) -> None:
    leaves: list[tuple[str, str]] = []
    groups: list[tuple[str, dict]] = []
    for key, value in node.items():
        if _is_leaf(value):
            cleaned = _clean(value["en"])
            if cleaned:
                leaves.append((key, cleaned))
        elif isinstance(value, dict):
            if _skip_group(key):
                continue
            groups.append((key, value))

    for _, text in leaves:
        lines.append(f"- {text}")
    if leaves:
        lines.append("")

    for key, value in groups:
        heading = "#" * min(depth, 6)
        pretty = key.replace("_", " ")
        lines.append(f"{heading} {pretty}")
        lines.append("")
        _render_into(value, lines, depth + 1)


def render_markdown(root: dict) -> str:
    today = date.today().isoformat()
    out: list[str] = [
        "# Vome feature catalog",
        "",
        f"_Generated {today} from `landing-page/src/i18n/strings.ts` (English copy only)._",
        "",
        "This catalog is the authoritative reference for what features Vome ships, ",
        "how they are described publicly, and what each plan tier includes. Use it ",
        "to answer customer questions about capabilities, pricing, and plan limits.",
        "",
        "---",
        "",
    ]

    ordered = [
        "hero",
        "feature_tabs",
        "features_page",
        "module_recruitment",
        "module_onboarding",
        "module_scheduling",
        "module_hours",
        "module_recognition",
        "module_comms",
        "module_data",
        "module_app",
        "module_integrations",
        "mobile_app",
        "enterprise",
        "security",
        "plans",
        "faq",
        "about_us",
        "testimonials",
    ]
    seen = set()
    for name in ordered:
        if name in EXCLUDE_TOP_LEVEL or name not in root:
            continue
        seen.add(name)
        out.extend(_render_section(name, root[name]))
        out.append("---")
        out.append("")

    # Catch any unlisted substantive sections.
    for name, body in root.items():
        if name in seen or name in EXCLUDE_TOP_LEVEL or not isinstance(body, dict):
            continue
        out.extend(_render_section(name, body))
        out.append("---")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_json(root: dict) -> dict:
    """Structured form: just the included sections, leaves as plain English strings."""
    def reduce_node(node: dict):
        result = {}
        for key, value in node.items():
            if _is_leaf(value):
                cleaned = _clean(value["en"])
                if cleaned:
                    result[key] = cleaned
            elif isinstance(value, dict):
                if _skip_group(key):
                    continue
                reduced = reduce_node(value)
                if reduced:
                    result[key] = reduced
        return result

    out = {}
    for name, body in root.items():
        if name in EXCLUDE_TOP_LEVEL or not isinstance(body, dict):
            continue
        out[name] = reduce_node(body)
    return out


# ─── Entry point ───────────────────────────────────────────────────────────

def main() -> int:
    if not LANDING_STRINGS.exists():
        print(f"ERROR: strings.ts not found at {LANDING_STRINGS}", file=sys.stderr)
        print("Expected the landing-page repo as a sibling of support-agent.", file=sys.stderr)
        return 1

    print(f"Reading {LANDING_STRINGS}")
    root = parse_strings_file(LANDING_STRINGS)
    print(f"Parsed {len(root)} top-level sections")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    md = render_markdown(root)
    OUT_MD.write_text(md, encoding="utf-8")
    print(f"Wrote {OUT_MD} ({len(md):,} chars)")

    structured = render_json(root)
    OUT_JSON.write_text(json.dumps(structured, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {OUT_JSON} ({sum(len(v) for v in structured.values()):,} keys across {len(structured)} sections)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
