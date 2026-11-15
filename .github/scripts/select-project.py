#!/usr/bin/env python3
"""Select today's project type and candidates from GitHub trending-by-stars.

Reads the latest runs/*.md for yesterday's type, picks a different type for
today (deterministic from the date), queries the GitHub Search API for
well-maintained repos in that type's language, and writes selection.json.

Env:
  TODAY     date YYYY-MM-DD (required)
  SLOT      morning | afternoon | evening (default: morning). The type and
            candidate hashes are seeded with "<TODAY>-<SLOT>-type" and
            "<TODAY>-<SLOT>-pick" so the three daily slots pick different
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
    logs = sorted(f for f in os.listdir(runs_dir) if re.match(r"^\d{4}-\d{2}-\d{2}\.md$", f))
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


def main() -> None:
    today = os.environ.get("TODAY", "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        print("TODAY env var must be set to YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    slot = os.environ.get("SLOT", "morning").strip() or "morning"
    if slot not in ("morning", "afternoon", "evening"):
        print(f"SLOT must be morning, afternoon or evening (got {slot!r})", file=sys.stderr)
        sys.exit(1)
    token = os.environ.get("GH_TOKEN") or None

    yesterday = read_yesterday_type()
    ptype = pick_type(today, yesterday, slot)
    language = TYPE_TO_LANGUAGE[ptype]
    since = (date.fromisoformat(today) - timedelta(days=75)).isoformat()

    items = search_repos(language, token, since)
    h = md5_int(f"{today}-{slot}-pick")
    candidates = []
    for i in range(3):
        item = items[(h + i) % len(items)]
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
