"""
clickup_codebase_scan_handler.py

When Vic creates a ClickUp task for a new Zoho ticket (default status
"queued"), this runs a one-pass codebase scan and posts a context note as a
ClickUp comment, signed as Vic, so the engineer who later picks the task up has
a running start instead of reading the thread cold and hunting through the code.

Triggered from agent.py's new-ticket creation path on a background thread (so it
never blocks ticket processing). Read-only and additive: it only *fetches* repo
code (shallow tarball, extracted to a temp dir and discarded) and *adds* a
single comment. It changes no statuses and sends no client email.

Conservative by design (per Sam): the note gives starting points only —
suspected files, relevant snippets, anomaly flags — and NEVER claims a root
cause or proposes a fix. Everything is framed as "unverified, please confirm".

Self-contained: imports only stdlib + httpx + anthropic + model_config, never
agent.py (which imports this module — avoids a circular import).
"""

import json
import os
import re
import shutil
import tarfile
import tempfile
import traceback
from pathlib import Path

import anthropic
import httpx

from model_config import SUPPORT_MODEL

# ---------------------------------------------------------------------------
# Config / credentials (all read-only)
# ---------------------------------------------------------------------------

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_BASE = "https://api.clickup.com/api/v2"

GITHUB_RO_TOKEN = os.environ.get("GITHUB_RO_TOKEN", "")
BITBUCKET_USER = os.environ.get("BITBUCKET_USER", "")
BITBUCKET_APP_PASSWORD = os.environ.get("BITBUCKET_APP_PASSWORD", "")

_anthropic = anthropic.Anthropic()

# ---------------------------------------------------------------------------
# Repo registry — canonical platform -> repos to fetch + search.
#
# Sam: replace the PLACEHOLDER slugs/branches with the real repos.
#   - GitHub slug form:    "owner/repo"      (e.g. "vomeadmin/web-app")
#   - Bitbucket slug form: "workspace/repo"  (e.g. "vomevolunteer/django-core")
# The one-line "desc" is shown to Claude so it knows what each repo contains.
# Repos whose slug still contains "PLACEHOLDER" are skipped (with a log line),
# so the feature degrades gracefully until the registry is filled in.
# ---------------------------------------------------------------------------

REPO_REGISTRY: dict[str, list[dict]] = {
    "backend": [
        {"host": "bitbucket", "slug": "PLACEHOLDER/backend-repo-1",
         "branch": "master", "desc": "Django backend (PLACEHOLDER)"},
        {"host": "bitbucket", "slug": "PLACEHOLDER/backend-repo-2",
         "branch": "master", "desc": "Django backend (PLACEHOLDER)"},
        {"host": "bitbucket", "slug": "PLACEHOLDER/backend-repo-3",
         "branch": "master", "desc": "Django backend (PLACEHOLDER)"},
    ],
    "web": [
        {"host": "github", "slug": "PLACEHOLDER/web-app",
         "branch": "main", "desc": "Web app frontend (PLACEHOLDER)"},
    ],
    "mobile": [
        {"host": "github", "slug": "PLACEHOLDER/mobile-app",
         "branch": "main", "desc": "Mobile app, React Native (PLACEHOLDER)"},
    ],
}

# Cap repos fetched per scan (latency / token budget guard).
MAX_REPOS = 3

# Comment marker — also used for the idempotency check.
SCAN_MARKER = "🔎 Vic — codebase scan"

# Categories we scan (matches the categories that get a ClickUp task at all).
SCAN_CATEGORIES = {"bug", "investigation", "auth"}

# In-process guard against a double-fire of the creation path.
_scanned_task_ids: set[str] = set()

# ---------------------------------------------------------------------------
# Search config
# ---------------------------------------------------------------------------

IGNORE_DIRS = {
    ".git", "node_modules", "dist", "build", ".next", "out", "venv", ".venv",
    "__pycache__", "coverage", ".idea", ".vscode", "vendor", "Pods",
    ".gradle", "migrations", "staticfiles", ".expo", "ios/Pods",
}
IGNORE_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mov", ".zip", ".gz",
    ".lock", ".map", ".min.js", ".min.css", ".pyc", ".so", ".class",
    ".jar", ".keystore", ".jks",
}
MAX_FILE_BYTES = 1_000_000
MAX_SEARCH_RESULTS = 40
MAX_READ_LINES = 120


# ---------------------------------------------------------------------------
# ClickUp helpers (own copies — handlers keep their own to stay decoupled)
# ---------------------------------------------------------------------------

def _add_clickup_comment(task_id: str, text: str) -> bool:
    """Post a comment on a ClickUp task. Returns True on success."""
    if not CLICKUP_API_TOKEN or not task_id:
        return False
    try:
        r = httpx.post(
            f"{CLICKUP_BASE}/task/{task_id}/comment",
            json={"comment_text": text, "notify_all": False},
            headers={
                "Authorization": CLICKUP_API_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        print(f"[CODEBASE SCAN] Comment posted to task {task_id}")
        return True
    except Exception as e:
        print(f"[CODEBASE SCAN] ClickUp comment failed ({task_id}): {e}")
        return False


def _already_scanned(task_id: str) -> bool:
    """True if a Vic scan comment is already on the task (defensive idempotency)."""
    if not CLICKUP_API_TOKEN or not task_id:
        return False
    try:
        r = httpx.get(
            f"{CLICKUP_BASE}/task/{task_id}/comment",
            headers={"Authorization": CLICKUP_API_TOKEN},
            timeout=15,
        )
        r.raise_for_status()
        for c in r.json().get("comments", []):
            text = c.get("comment_text", "") or ""
            if SCAN_MARKER in text:
                return True
    except Exception as e:
        print(f"[CODEBASE SCAN] Comment check failed ({task_id}): {e}")
    return False


# ---------------------------------------------------------------------------
# Platform -> repo resolution
# ---------------------------------------------------------------------------

def _resolve_platforms(analysis: dict) -> list[str]:
    """Map classification to canonical platform keys in REPO_REGISTRY.

    engineer_type (frontend|mobile|backend) is the primary signal; the parsed
    platform field (web|mobile|both) is the fallback. Returns [] when nothing
    resolves (e.g. engineer_type 'unclear' and no platform) -> no scan.
    """
    eng = (analysis.get("engineer_type") or "").strip().lower()
    if eng == "backend":
        return ["backend"]
    if eng == "frontend":
        return ["web"]
    if eng == "mobile":
        return ["mobile"]

    # auth tickets route to the backend engineer regardless of engineer_type
    if (analysis.get("category") or "").strip().lower() == "auth":
        return ["backend"]

    platform = (analysis.get("platform") or "").strip().lower()
    if "both" in platform:
        return ["web", "mobile"]
    if "mobile" in platform:
        return ["mobile"]
    if "web" in platform:
        return ["web"]
    return []


def _repos_for(platforms: list[str]) -> list[dict]:
    """Collect, dedupe, and cap the repos for the resolved platforms.

    Skips repos whose slug is still a PLACEHOLDER so the feature is a no-op
    (rather than an error) until the registry is filled in.
    """
    repos: list[dict] = []
    seen: set[str] = set()
    for p in platforms:
        for repo in REPO_REGISTRY.get(p, []):
            slug = repo.get("slug", "")
            if "PLACEHOLDER" in slug:
                print(f"[CODEBASE SCAN] Skipping placeholder repo for '{p}'")
                continue
            if slug in seen:
                continue
            seen.add(slug)
            repos.append(repo)
    return repos[:MAX_REPOS]


# ---------------------------------------------------------------------------
# Repo fetch (shallow tarball -> temp dir, discarded after the scan)
# ---------------------------------------------------------------------------

def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract a tarball, refusing any member that escapes dest (Zip Slip)."""
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            raise RuntimeError(f"Unsafe path in archive: {member.name}")
    # Python 3.12+ supports a data filter; fall back gracefully otherwise.
    try:
        tar.extractall(dest, filter="data")
    except TypeError:
        tar.extractall(dest)


def _download_and_extract(repo: dict, parent: Path) -> Path | None:
    """Download a repo's branch tarball and extract it. Returns the repo root.

    Tokens are read from env and never logged. Returns None on any failure.
    """
    host = repo["host"]
    slug = repo["slug"]
    branch = repo.get("branch") or "main"

    if host == "github":
        if not GITHUB_RO_TOKEN:
            print("[CODEBASE SCAN] GITHUB_RO_TOKEN not set — skipping GitHub repo")
            return None
        url = f"https://api.github.com/repos/{slug}/tarball/{branch}"
        headers = {
            "Authorization": f"Bearer {GITHUB_RO_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        auth = None
    elif host == "bitbucket":
        if not (BITBUCKET_USER and BITBUCKET_APP_PASSWORD):
            print("[CODEBASE SCAN] Bitbucket creds not set — skipping Bitbucket repo")
            return None
        url = f"https://bitbucket.org/{slug}/get/{branch}.tar.gz"
        headers = {}
        auth = (BITBUCKET_USER, BITBUCKET_APP_PASSWORD)
    else:
        print(f"[CODEBASE SCAN] Unknown host '{host}' for {slug}")
        return None

    dest = parent / slug.replace("/", "__")
    dest.mkdir(parents=True, exist_ok=True)
    tar_path = parent / f"{slug.replace('/', '__')}.tar.gz"

    try:
        with httpx.stream(
            "GET", url, headers=headers, auth=auth,
            follow_redirects=True, timeout=60,
        ) as resp:
            if resp.status_code != 200:
                print(
                    f"[CODEBASE SCAN] Download failed for {slug} "
                    f"(HTTP {resp.status_code})"
                )
                return None
            with open(tar_path, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)

        with tarfile.open(tar_path, mode="r:gz") as tar:
            _safe_extract(tar, dest)
    except Exception as e:
        print(f"[CODEBASE SCAN] Fetch/extract error for {slug}: {e}")
        return None
    finally:
        try:
            tar_path.unlink(missing_ok=True)
        except Exception:
            pass

    # Both GitHub and Bitbucket wrap everything in a single top-level dir.
    children = [p for p in dest.iterdir() if p.is_dir()]
    root = children[0] if len(children) == 1 else dest
    print(f"[CODEBASE SCAN] Fetched {slug}@{branch}")
    return root


# ---------------------------------------------------------------------------
# Code search / read tools (pure Python — no git or ripgrep binary needed)
# ---------------------------------------------------------------------------

def _iter_text_files(root: Path):
    """Yield candidate text files under root, skipping noise/binaries."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext in IGNORE_EXT or name.endswith((".min.js", ".min.css")):
                continue
            fp = Path(dirpath) / name
            try:
                if fp.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield fp


def _search_code(root: Path, repo_name: str, query: str) -> list[dict]:
    """Regex/substring search; returns capped [{file, line, text}] hits."""
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(query), re.IGNORECASE)

    results: list[dict] = []
    for fp in _iter_text_files(root):
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, start=1):
                    if "\x00" in line:  # binary-ish; stop reading this file
                        break
                    if pattern.search(line):
                        rel = str(fp.relative_to(root)).replace("\\", "/")
                        results.append({
                            "file": rel,
                            "line": i,
                            "text": line.strip()[:200],
                        })
                        if len(results) >= MAX_SEARCH_RESULTS:
                            return results
        except OSError:
            continue
    return results


def _read_file(root: Path, path: str, start: int, end: int) -> dict:
    """Read a bounded line window from a repo file (traversal-safe)."""
    target = (root / path).resolve()
    if not str(target).startswith(str(root.resolve())):
        return {"error": "path outside repo"}
    if not target.is_file():
        return {"error": f"not a file: {path}"}

    start = max(1, int(start or 1))
    end = int(end or (start + MAX_READ_LINES))
    if end - start > MAX_READ_LINES:
        end = start + MAX_READ_LINES

    try:
        with open(target, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError as e:
        return {"error": str(e)}

    window = lines[start - 1:end]
    numbered = "".join(
        f"{start + idx}\t{ln.rstrip(chr(10))}\n" for idx, ln in enumerate(window)
    )
    return {"file": path, "start": start, "end": start + len(window) - 1,
            "content": numbered[:6000]}


# ---------------------------------------------------------------------------
# Tool definitions for the agentic loop
# ---------------------------------------------------------------------------

def _build_tools(repo_names: list[str]) -> list[dict]:
    repo_enum = {"type": "string", "enum": repo_names,
                 "description": "Which repo to act on."}
    return [
        {
            "name": "search_code",
            "description": (
                "Search a repo for a regex/substring (case-insensitive). "
                "Returns file paths, line numbers, and the matching lines. "
                "Use specific identifiers from the ticket (error text, field "
                "names, endpoints, component names)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo": repo_enum,
                    "query": {"type": "string",
                              "description": "Regex or literal to search for."},
                },
                "required": ["repo", "query"],
            },
        },
        {
            "name": "read_file",
            "description": (
                f"Read up to {MAX_READ_LINES} lines from a repo file to confirm "
                f"relevance before citing it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo": repo_enum,
                    "path": {"type": "string",
                             "description": "Repo-relative file path."},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["repo", "path"],
            },
        },
        {
            "name": "finish_scan",
            "description": (
                "Call exactly once when done to return the context note. If "
                "nothing relevant was found, return empty lists and say so in "
                "the rationale."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "suspected_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Repo-relative paths worth checking first, most "
                            "likely first. Prefix with the repo if ambiguous."
                        ),
                    },
                    "relevant_snippets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Short 'file:line — why it's relevant' notes. No "
                            "fixes, no root-cause claims — just pointers."
                        ),
                    },
                    "flags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Anomalies worth a look (TODO/FIXME nearby, "
                            "suspicious patterns, obvious edge cases). Optional."
                        ),
                    },
                    "one_line_rationale": {
                        "type": "string",
                        "description": "One sentence on where to start and why.",
                    },
                },
                "required": ["one_line_rationale"],
            },
        },
    ]


def _system_prompt(repos: list[dict]) -> str:
    repo_lines = "\n".join(
        f"  - {r['slug']}: {r.get('desc', '')}" for r in repos
    )
    return (
        "You are Vic, Vome's support engineer assistant. A new support ticket "
        "has just been filed and a ClickUp task created. Before a human "
        "engineer picks it up, do a quick READ-ONLY pass over the codebase to "
        "leave them a head start.\n\n"
        "Available repos (use the slug as the `repo` argument):\n"
        f"{repo_lines}\n\n"
        "Approach: pull concrete identifiers from the ticket (error messages, "
        "field/endpoint/component names, modules) and search for them. Read a "
        "few of the most promising files to confirm they're relevant. Then "
        "call finish_scan.\n\n"
        "STRICT RULES:\n"
        "- This is CONTEXT ONLY. Do NOT assert a root cause and do NOT propose "
        "a fix or diff. You are pointing the engineer at starting points.\n"
        "- Only cite files/lines you actually found via the tools. Never invent "
        "paths.\n"
        "- If you can't find anything clearly relevant, that's fine — call "
        "finish_scan with empty lists and say so. Do not pad.\n"
        "- Be concise. Budget a handful of tool calls, not dozens."
    )


# ---------------------------------------------------------------------------
# Comment formatting
# ---------------------------------------------------------------------------

def _format_comment(scan: dict, repos: list[dict]) -> str:
    repo_names = ", ".join(r["slug"] for r in repos)
    lines = [
        f"{SCAN_MARKER} (automated starting points — unverified, please confirm)",
        "",
    ]
    rationale = (scan.get("one_line_rationale") or "").strip()
    if rationale:
        lines.append(rationale)
        lines.append("")

    suspected = scan.get("suspected_files") or []
    snippets = scan.get("relevant_snippets") or []
    flags = scan.get("flags") or []

    if suspected:
        lines.append("*Files to check:*")
        lines.extend(f"• {s}" for s in suspected)
        lines.append("")
    if snippets:
        lines.append("*Relevant code:*")
        lines.extend(f"• {s}" for s in snippets)
        lines.append("")
    if flags:
        lines.append("*Flags:*")
        lines.extend(f"• {s}" for s in flags)
        lines.append("")

    if not (suspected or snippets or flags):
        lines.append(
            f"Scanned {repo_names} but found no clear match — over to you."
        )
        lines.append("")

    lines.append(f"_Scanned: {repo_names}_")
    lines.append("— Vic")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------

def _run_scan_loop(repo_roots: dict[str, Path], context: str,
                   repos: list[dict]) -> dict | None:
    """Drive Claude with search/read tools until it calls finish_scan."""
    tools = _build_tools(list(repo_roots.keys()))
    messages = [{"role": "user", "content": context}]
    max_iterations = 8

    for _ in range(max_iterations):
        try:
            response = _anthropic.messages.create(
                model=SUPPORT_MODEL,
                max_tokens=1500,
                system=_system_prompt(repos),
                tools=tools,
                messages=messages,
            )
        except Exception as e:
            print(f"[CODEBASE SCAN] Claude API call failed: {e}")
            return None

        tool_results = []
        finished: dict | None = None

        for block in response.content:
            if block.type != "tool_use":
                continue
            name, tool_input, tool_id = block.name, block.input, block.id
            print(
                f"[CODEBASE SCAN] tool {name}"
                f"({json.dumps(tool_input, default=str)[:200]})"
            )
            try:
                if name == "search_code":
                    root = repo_roots.get(tool_input.get("repo", ""))
                    out = (
                        {"results": _search_code(
                            root, tool_input.get("repo", ""),
                            tool_input.get("query", ""))}
                        if root else {"error": "unknown repo"}
                    )
                elif name == "read_file":
                    root = repo_roots.get(tool_input.get("repo", ""))
                    out = (
                        _read_file(root, tool_input.get("path", ""),
                                   tool_input.get("start_line", 1),
                                   tool_input.get("end_line", 0))
                        if root else {"error": "unknown repo"}
                    )
                elif name == "finish_scan":
                    finished = dict(tool_input)
                    out = {"ok": True}
                else:
                    out = {"error": f"unknown tool: {name}"}
            except Exception as e:
                print(f"[CODEBASE SCAN] tool {name} failed: {e}")
                out = {"error": str(e)}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": json.dumps(out, default=str)[:8000],
            })

        if finished is not None:
            return finished

        if tool_results:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        # No tools called and no finish — nudge once toward finishing.
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": "Call finish_scan now with whatever you have.",
        })

    print("[CODEBASE SCAN] loop exhausted without finish_scan")
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_codebase_scan(task_id: str, analysis: dict, conversations_text: str,
                      subject: str = "", issue_summary: str = "",
                      ticket_number: str = "") -> None:
    """Scan the relevant repo(s) for a freshly created task and post a Vic note.

    Called on a background thread from agent.py. Never raises — all failures
    are logged and swallowed so the ticket pipeline is unaffected.
    """
    try:
        analysis = analysis or {}
        category = (analysis.get("category") or "").strip().lower()
        if category not in SCAN_CATEGORIES:
            print(f"[CODEBASE SCAN] task {task_id}: category "
                  f"'{category}' not scanned — skipping")
            return

        # Idempotency: in-process guard + comment marker check.
        if task_id in _scanned_task_ids:
            print(f"[CODEBASE SCAN] task {task_id}: already scanned this process")
            return
        _scanned_task_ids.add(task_id)
        if _already_scanned(task_id):
            print(f"[CODEBASE SCAN] task {task_id}: scan comment already present")
            return

        platforms = _resolve_platforms(analysis)
        repos = _repos_for(platforms)
        if not repos:
            print(f"[CODEBASE SCAN] task {task_id}: no repos resolved "
                  f"(platforms={platforms}) — skipping")
            return

        print(f"[CODEBASE SCAN] task {task_id}: scanning "
              f"{[r['slug'] for r in repos]}")

        context = (
            f"Ticket #{ticket_number}: {subject}\n\n"
            f"Issue summary:\n{issue_summary or '(none)'}\n\n"
            f"Full Zoho conversation thread:\n{conversations_text[:12000]}\n\n"
            f"Classification: {json.dumps(analysis, default=str)}"
        )

        tmp_parent = Path(tempfile.mkdtemp(prefix="vic_scan_"))
        repo_roots: dict[str, Path] = {}
        try:
            for repo in repos:
                root = _download_and_extract(repo, tmp_parent)
                if root:
                    repo_roots[repo["slug"]] = root

            if not repo_roots:
                print(f"[CODEBASE SCAN] task {task_id}: no repos fetched "
                      f"— skipping (no comment)")
                return

            scan = _run_scan_loop(repo_roots, context, repos)
            if scan is None:
                print(f"[CODEBASE SCAN] task {task_id}: no scan result — "
                      f"skipping comment")
                return

            comment = _format_comment(scan, repos)
            _add_clickup_comment(task_id, comment)
        finally:
            shutil.rmtree(tmp_parent, ignore_errors=True)

    except Exception as e:
        print(f"[CODEBASE SCAN] task {task_id}: unexpected error: {e}")
        traceback.print_exc()
