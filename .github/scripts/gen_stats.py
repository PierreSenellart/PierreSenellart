#!/usr/bin/env python3
"""Generate the "GitHub Stats" summary card from the GitHub REST API.

This replaces the github-readme-stats (GRS) stats card, which stopped working on
2026-07-23: GitHub's GraphQL API now refuses the `stargazers` field to any token
without an explicit grant on the target repository, so every cross-repo star
lookup came back FORBIDDEN ("Resource not accessible by integration") and GRS
rendered its error card instead. GRS is GraphQL-only, hence fatal for it; the
REST endpoints used here still serve the same public data to a plain
GITHUB_TOKEN, exactly as they do for gen_top_langs.py.

Two deliberate differences from the card this replaces:

  1. No "rank" badge. GRS derived a letter grade from a percentile model of
     commits/PRs/issues/stars/followers. The inputs are public but the grade is
     invented, so the numbers are shown on their own.
  2. Failures are fatal. GRS caught API errors and wrote an error card, which
     the workflow then committed as if nothing were wrong; that is how the
     breakage above went unnoticed for five days. Here an API failure raises,
     the step goes red, and the previous good SVG is left untouched.

Metric sources (all REST, all verified against the last card GRS produced):
    Total Stars Earned          sum of stargazers_count over non-fork owned repos
    Total Commits               search/commits?q=author:USERNAME
    Total PRs                   search/issues?q=author:USERNAME+is:pr
    Total Issues                search/issues?q=author:USERNAME+is:issue
    Contributed to (last year)  owned repos with a commit in the window (asked
                                repo by repo, so exact) plus repos owned by
                                others found in search/commits over the window

Usage:
    python3 gen_stats.py [output.svg]         # default: profile/stats.svg
Auth: set GITHUB_TOKEN (or GH_TOKEN) in the environment. The search endpoints
used here require authentication, so unlike gen_top_langs.py this is mandatory.
"""

from __future__ import annotations

import datetime
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG -- everything you'll want to tweak lives here.
# ─────────────────────────────────────────────────────────────────────────────

USERNAME = "PierreSenellart"

# Repository selection for the star tally (matches github-readme-stats
# defaults, and gen_top_langs.py).
INCLUDE_FORKS = False        # forks are noise: someone else's stars.
INCLUDE_ARCHIVED = True

# "Contributed to" looks at commits authored in the trailing window. The search
# API only ever returns its first 1000 matches, so with more commits than that
# in the window we count distinct repositories over the most recent 1000 and say
# so on stderr. A repo touched only by an older commit could then be missed.
CONTRIB_WINDOW_DAYS = 365
CONTRIB_MAX_PAGES = 10       # 10 x 100 = the API's hard 1000-result ceiling.

# Card appearance. Shared with gen_top_langs.py so the two cards read as a set.
# The title takes the account's display name ("Pierre Senellart"), falling back
# to the login if the profile has none.
TITLE_TEMPLATE = "{name}’s GitHub Stats"
CARD_BG = "#fffefe"
CARD_BORDER = "#e4e2e2"
TITLE_COLOR = "#2f80ed"
TEXT_COLOR = "#434d58"
ICON_COLOR = "#4c71f2"

# Octicons (16x16 viewBox), one per row, drawn at 0.9 scale next to the label.
ICONS = {
    "star": "M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 "
            ".416 1.279l-3.046 2.97.719 4.192a.751.751 0 0 1-1.088.791L8 "
            "12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L.818 "
            "6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 "
            "8 .25Z",
    "commit": "M11.93 8.5a4.002 4.002 0 0 1-7.86 0H.75a.75.75 0 0 1 "
              "0-1.5h3.32a4.002 4.002 0 0 1 7.86 0h3.32a.75.75 0 0 1 0 "
              "1.5Zm-1.43-.75a2.5 2.5 0 1 0-5 0 2.5 2.5 0 0 0 5 0Z",
    "pr": "M1.5 3.25a2.25 2.25 0 1 1 3 2.122v5.256a2.251 2.251 0 1 1-1.5 "
          "0V5.372A2.25 2.25 0 0 1 1.5 3.25Zm5.677-.177L9.573.677A.25.25 0 0 "
          "1 10 .854V2.5h1A2.5 2.5 0 0 1 13.5 5v5.628a2.251 2.251 0 1 1-1.5 "
          "0V5a1 1 0 0 0-1-1h-1v1.646a.25.25 0 0 1-.427.177L7.177 "
          "3.427a.25.25 0 0 1 0-.354ZM3.75 2.5a.75.75 0 1 0 0 1.5.75.75 0 0 "
          "0 0-1.5Zm0 9.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm8.25.75a.75"
          ".75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Z",
    "issue": "M8 9.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3ZM8 0a8 8 0 1 1 0 "
             "16A8 8 0 0 1 8 0ZM1.5 8a6.5 6.5 0 1 0 13 0 6.5 6.5 0 0 0-13 0Z",
    "repo": "M2 2.5A2.5 2.5 0 0 1 4.5 0h8.75a.75.75 0 0 1 .75.75v12.5a.75.75 "
            "0 0 1-.75.75h-2.5a.75.75 0 0 1 0-1.5h1.75v-2h-8a1 1 0 0 0-.714 "
            "1.7.75.75 0 1 1-1.072 1.05A2.495 2.495 0 0 1 2 11.5Zm10.5-1h-8a1 "
            "1 0 0 0-1 1v6.708A2.486 2.486 0 0 1 4.5 9h8ZM5 12.25a.25.25 0 0 "
            "1 .25-.25h3.5a.25.25 0 0 1 .25.25v3.25a.25.25 0 0 1-.4.2l-1.45"
            "-1.087a.249.249 0 0 0-.3 0L5.4 15.7a.25.25 0 0 1-.4-.2Z",
}

# ─────────────────────────────────────────────────────────────────────────────
# DATA -- fetch and aggregate.
# ─────────────────────────────────────────────────────────────────────────────


def api(url: str):
    headers = {"User-Agent": "stats-script",
               "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # Surface the API's own message: a 403 here is the difference between
        # "rate limited, retry later" and "this token may no longer read that".
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"{e.code} {e.reason} for {url}\n  {body}") from None


def search_count(query: str, endpoint: str = "issues") -> int:
    """Total number of matches for a search query (results themselves unused)."""
    q = urllib.parse.quote(query, safe=":+")
    return api(f"https://api.github.com/search/{endpoint}?q={q}&per_page=1"
               )["total_count"]


def list_repos() -> list[dict]:
    """Owned repositories, filtered exactly as gen_top_langs.py filters them."""
    repos, page = [], 1
    while True:
        batch = api(f"https://api.github.com/users/{USERNAME}/repos"
                    f"?per_page=100&type=owner&page={page}")
        if not batch:
            break
        repos += batch
        page += 1

    def keep(r):
        if r["fork"] and not INCLUDE_FORKS:
            return False
        if r.get("archived") and not INCLUDE_ARCHIVED:
            return False
        return True

    return [r for r in repos if keep(r)]


def contributed_to(repos: list[dict]) -> tuple[int, bool]:
    """(distinct repos committed to in the window, whether the cap was hit).

    Owned repos are asked directly, one listing each, so that half of the count
    is exact. Repos owned by *other* people can only come from the commit search,
    which stops at 1000 matches -- hence the flag: with more commits than that in
    the window, an external repo touched by an older commit can be missed.
    """
    since = (datetime.date.today()
             - datetime.timedelta(days=CONTRIB_WINDOW_DAYS)).isoformat()

    seen = {f"{USERNAME}/{r['name']}" for r in repos
            if api(f"https://api.github.com/repos/{USERNAME}/{r['name']}"
                   f"/commits?author={USERNAME}&since={since}T00:00:00Z"
                   f"&per_page=1")}

    query = urllib.parse.quote(f"author:{USERNAME} author-date:>{since}",
                               safe=":+>")
    capped = False
    for page in range(1, CONTRIB_MAX_PAGES + 1):
        res = api(f"https://api.github.com/search/commits?q={query}"
                  f"&per_page=100&page={page}")
        items = res.get("items", [])
        seen.update(i["repository"]["full_name"] for i in items
                    if not i["repository"]["full_name"].startswith(
                        f"{USERNAME}/"))
        if len(items) < 100:
            break
        if page == CONTRIB_MAX_PAGES:
            capped = res["total_count"] > CONTRIB_MAX_PAGES * 100
    return len(seen), capped


def collect() -> list[tuple[str, str, int]]:
    """The card's rows, as (icon key, label, value)."""
    repos = list_repos()
    print(f"{len(repos)} repos counted for stars", file=sys.stderr)
    contrib, capped = contributed_to(repos)
    if capped:
        print(f"  contributed-to: repos owned by others were looked for in the "
              f"most recent {CONTRIB_MAX_PAGES * 100} commits only (search API "
              f"ceiling); owned repos are exact", file=sys.stderr)
    return [
        ("star", "Total Stars Earned",
         sum(r["stargazers_count"] for r in repos)),
        ("commit", "Total Commits", search_count(f"author:{USERNAME}",
                                                 endpoint="commits")),
        ("pr", "Total PRs", search_count(f"author:{USERNAME} is:pr")),
        ("issue", "Total Issues", search_count(f"author:{USERNAME} is:issue")),
        ("repo", "Contributed to (last year)", contrib),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# RENDER -- build the card SVG.
# ─────────────────────────────────────────────────────────────────────────────


def display_name() -> str:
    name = api(f"https://api.github.com/users/{USERNAME}").get("name") or USERNAME
    return html.escape(name)   # it lands in both an attribute and a text node.


def build_svg(rows: list[tuple[str, str, int]], title: str) -> str:
    # Width matches the language card so the two sit level in the README;
    # labels are left-aligned and values right-aligned in their own column,
    # mirroring that card's legend.
    W = 440
    pad = 25
    title_y = 35
    rows_top, row_h = 62, 25
    value_right = W - pad
    H = rows_top + row_h * len(rows) + pad

    font = 'font-family="Segoe UI, Ubuntu, Sans-Serif" font-size="14"'
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" fill="none" role="img" '
        f'aria-label="{title}">',
        f'<rect x="0.5" y="0.5" rx="4.5" width="{W-1}" height="{H-1}" '
        f'fill="{CARD_BG}" stroke="{CARD_BORDER}"/>',
        f'<text x="{pad}" y="{title_y}" font-family="Segoe UI, Ubuntu, '
        f'Sans-Serif" font-size="18" font-weight="600" '
        f'fill="{TITLE_COLOR}">{title}</text>',
    ]

    for i, (icon, label, value) in enumerate(rows):
        y = rows_top + i * row_h
        parts.append(
            f'<g transform="translate({pad} {y}) scale(0.9)" '
            f'fill="{ICON_COLOR}"><path d="{ICONS[icon]}"/></g>')
        parts.append(
            f'<text x="{pad + 26}" y="{y + 12}" {font} '
            f'fill="{TEXT_COLOR}">{label}:</text>')
        parts.append(
            f'<text x="{value_right}" y="{y + 12}" text-anchor="end" {font} '
            f'font-weight="600" fill="{TEXT_COLOR}">{value:,}</text>')

    parts.append('</svg>')
    return "\n".join(parts)


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "profile/stats.svg"
    rows = collect()
    svg = build_svg(rows, TITLE_TEMPLATE.format(name=display_name()))
    with open(out, "w") as f:
        f.write(svg + "\n")
    # Diagnostics to stderr, so a red step says which number went wrong.
    for _, label, value in rows:
        print(f"  {label + ':':<30}{value:>8,}", file=sys.stderr)
    print(f"wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
