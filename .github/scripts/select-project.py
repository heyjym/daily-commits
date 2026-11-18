#!/usr/bin/env python3
"""Select today's project type and candidates from GitHub trending-by-stars.

Reads the latest runs/*.md for yesterday's type, picks a different type for
today (deterministic from the date), queries the GitHub Search API for
well-maintained repos in that type's language, and writes selection.json.

Env:
  TODAY     date YYYY-MM-DD (required)
  SLOT      two-digit slot number 01..10 (default: 01). The type and
            candidate hashes are seeded with "<TODAY>-<SLOT>-type" and
            "<TODAY>-<SLOT>-pick" so the ten daily slots pick different
            types and projects.
  GH_TOKEN  GitHub token for the Search API (optional; works unauthenticated
            for a single test, but rate limits are much lower)
"""
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta

TYPES = ["Python 工具", "Web 应用", "数据分析", "自动化脚本", "命令行工具", "小游戏"]

TYPE_TO_LANGUAGE = {
    "Python 工具": "python",
    "Web 应用": "javascript",
    "数据分析": "python",
    "自动化脚本": "python",
    "命令行工具": "go",
    "小游戏": "javascript",
}


def md5_int(seed: str) -> int:
    return int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:8], 16)


def read_yesterday_type() -> str | None:
    """Return the Type line of the newest runs/*.md, or None if no logs yet."""
    runs_dir = "runs"
    if not os.path.isdir(runs_dir):
        return None
    logs = sorted(f for f in os.listdir(runs_dir) if re.match(r"^\d{4}-\d{2}-\d{2}(-\d{2})?\.md$", f))
    if not logs:
        return None
    with open(os.path.join(runs_dir, logs[-1]), encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"- \*\*Type:\*\*\s*(.+?)\s*$", line)
            if m:
                return m.group(1)
    return None


def pick_type(today: str, yesterday: str | None, slot: str) -> str:
    idx = md5_int(f"{today}-{slot}-type") % len(TYPES)
    # Advance while it would repeat yesterday's type (at most len(TYPES) steps).
    for _ in range(len(TYPES)):
        if TYPES[idx] != yesterday:
            return TYPES[idx]
        idx = (idx + 1) % len(TYPES)
    return TYPES[idx]


def search_repos(language: str, token: str | None, since: str) -> list[dict]:
    # Only '>' needs percent-encoding: the egress proxy drops requests with a
    # raw '>' in the URL, while fully urlencoding the query (urlencode) makes
    # GitHub's search parser return 422, so encode '>' alone.
    query = (
        f"language:{language}+stars:>500+pushed:>{since}"
        "&sort=stars&order=desc&per_page=20"
    ).replace(">", "%3E")
    url = f"https://api.github.com/search/repositories?q={query}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "daily-commits-workflow",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        print(f"GitHub Search API failed: HTTP {exc.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # network error etc.
        print(f"GitHub Search API failed: {exc}", file=sys.stderr)
        sys.exit(1)
    items = data.get("items", [])
    if not items:
        print("GitHub Search API returned no repositories", file=sys.stderr)
        sys.exit(1)
    return items



STACK_FILES = {"package.json", "go.mod", "requirements.txt", "pyproject.toml", "setup.py"}

def has_runnable_stack(full_name: str, token: str | None) -> bool:
    """True if the repo root has a file run-project.py can install from.

    List-only repos (awesome-* lists, tutorials, book collections) have none
    of these at the root, so run-project.py fails them with
    "no recognizable stack" before any probe runs.
    """
    url = f"https://api.github.com/repos/{full_name}/contents/"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "daily-commits-workflow",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            entries = json.load(resp)
    except Exception as exc:
        print(f"stack check failed for {full_name}: {exc} (keeping it)", file=sys.stderr)
        return True  # fail-open: an API blip must not empty the pool
    if not isinstance(entries, list):
        return True
    names = {e.get("name") for e in entries}
    return bool(names & STACK_FILES)


def _fetch_pyproject(full_name, token):
    """Return parsed pyproject.toml dict, or None if absent/unreadable (fail-open)."""
    import base64
    url = f"https://api.github.com/repos/{full_name}/contents/pyproject.toml"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "daily-commits-workflow",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        if not isinstance(data, dict) or not data.get("content"):
            return None
        import tomllib
        return tomllib.loads(base64.b64decode(data["content"]).decode("utf-8", "replace"))
    except Exception:
        return None

def _min_python(requires):
    """Lower bound (major, minor) from a requires-python string, or None."""
    if not requires:
        return None
    m = re.search(r">=\s*(\d+)\.(\d+)", requires)
    return (int(m.group(1)), int(m.group(2))) if m else None

def has_runnable_entrypoint(full_name, token):
    """Python-only check: run-project.py can only probe a Python repo via a
    console script (--help) or `python -m <module>` / import. A pyproject.toml
    with no [project.scripts] and no top-level module named after the repo
    (e.g. a list/website repo whose pyproject is only for its docs site)
    can never pass the probe, even though it passes has_runnable_stack.
    Also drop repos whose requires-python excludes the runner's Python 3.12
    (e.g. >=3.14 / >=3.15: pip install -e . fails before any probe runs).
    Fail-open: any check that cannot be read keeps the candidate.
    """
    proj = _fetch_pyproject(full_name, token)
    if proj is None:
        return True
    project = proj.get("project", {}) or {}
    req = project.get("requires-python") or ""
    lo = _min_python(req)
    if lo is not None and lo > (3, 12):
        print(f"  entrypoint check: {full_name} requires-python {req} excludes runner Python 3.12 (dropping)", file=sys.stderr)
        return False
    if project.get("scripts"):
        return True
    # no console scripts: need an importable top-level module/package
    repo = full_name.split("/")[-1].lower().replace("-", "_").split(".")[0]
    url = f"https://api.github.com/repos/{full_name}/contents/"
    req2 = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "daily-commits-workflow",
    })
    if token:
        req2.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req2, timeout=30) as resp:
            entries = json.load(resp)
        names = {e.get("name") for e in entries if isinstance(e, dict)}
    except Exception:
        return True
    if f"{repo}.py" in names or repo in names or "src" in names:
        return True  # src-layout packages (e.g. transformers, langflow) keep the module under src/
    print(f"  entrypoint check: {full_name} has no console script and no importable {repo} module (dropping)", file=sys.stderr)
    return False


def main() -> None:
    today = os.environ.get("TODAY", "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        print("TODAY env var must be set to YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    slot = os.environ.get("SLOT", "01").strip() or "01"
    if not re.match(r"^(0[1-9]|10)$", slot):
        print(f"SLOT must be a two-digit slot 01..10 (got {slot!r})", file=sys.stderr)
        sys.exit(1)
    token = os.environ.get("GH_TOKEN") or None

    yesterday = read_yesterday_type()
    ptype = pick_type(today, yesterday, slot)
    language = TYPE_TO_LANGUAGE[ptype]
    since = (date.fromisoformat(today) - timedelta(days=75)).isoformat()

    items = search_repos(language, token, since)
    runnable = [it for it in items if has_runnable_stack(it["full_name"], token)]
    if not runnable:
        print("no candidate has a runnable stack; keeping unfiltered list", file=sys.stderr)
        runnable = items
    else:
        print(f"stack filter: {len(runnable)}/{len(items)} candidates runnable")
    if language == "python":
        checked = [it for it in runnable if has_runnable_entrypoint(it["full_name"], token)]
        if checked:
            print(f"entrypoint filter: {len(checked)}/{len(runnable)} candidates runnable on Python 3.12")
            runnable = checked
        else:
            print("entrypoint filter would empty the pool; keeping stack-filtered list", file=sys.stderr)
    h = md5_int(f"{today}-{slot}-pick")
    candidates = []
    for i in range(3):
        item = runnable[(h + i) % len(runnable)]
        lic = item.get("license") or {}
        spdx = lic.get("spdx_id")
        if spdx in (None, "NOASSERTION"):
            spdx = lic.get("name") or "Unknown"
        candidates.append({
            "full_name": item["full_name"],
            "html_url": item["html_url"],
            "stars": item.get("stargazers_count", 0),
            "license": spdx,
            "description": item.get("description") or "",
        })

    selection = {
        "today": today,
        "slot": slot,
        "type": ptype,
        "language": language,
        "candidates": candidates,
    }
    with open("selection.json", "w", encoding="utf-8") as fh:
        json.dump(selection, fh, ensure_ascii=False, indent=2)
    print(f"today={today} slot={slot} type={ptype} language={language}")
    for c in candidates:
        print(f"  candidate: {c['full_name']} ({c['stars']} stars, {c['license']})")


if __name__ == "__main__":
    main()
