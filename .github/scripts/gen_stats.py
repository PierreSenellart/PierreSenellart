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

The rows deliberately measure the *maintainer* side. The card GRS rendered showed
"Total PRs: 7" and "Total Issues: 14", which count what this account opened in
other people's repositories -- a number that would look the same whether provsql
had 70 stars or none. What people did with these repositories (24 merged PRs, 92
issues filed, 62 forks, 15 outside contributors) is the story worth telling.

Metric sources (all REST):
    Total Stars Earned          sum of stargazers_count over the counted repos
    Forks of my repos           sum of forks_count over the counted repos
    PRs merged from others      search/issues, is:pr is:merged, minus this
                                account and minus bots (32 of the 56 non-self
                                merges were dependabot, so this filter matters)
    Issues filed in my repos    search/issues, is:issue, minus this account/bots
    Contributors                distinct non-bot logins over the counted repos,
                                excluding this account
    Total Commits               search/commits?q=author:USERNAME

Caveat on scope: the `user:` search qualifier spans every repo this account owns,
including forks, whereas the star/fork tallies use the non-fork list. Today the
two forks (hotcrp, notmuch-addrlookup-c) hold no issues or PRs at all, so the
counts coincide; a fork that acquires them would drift.

Usage:
    python3 gen_stats.py [output.svg]         # default: profile/stats.svg
Auth: set GITHUB_TOKEN (or GH_TOKEN) in the environment. The search endpoints
used here require authentication, so unlike gen_top_langs.py this is mandatory.
"""

from __future__ import annotations

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

# Repository selection for the star and fork tallies (matches github-readme-stats
# defaults, and gen_top_langs.py).
INCLUDE_FORKS = False        # forks are noise: someone else's stars.
INCLUDE_ARCHIVED = True

# Bots to keep out of the "from others" counts. Dependabot alone accounts for
# more merged PRs than every human contributor combined, so counting it would
# turn a collaboration figure into a dependency-bump figure.
EXCLUDE_BOT_AUTHORS = ["app/dependabot"]

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
    "issue": "M8 9.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3ZM8 0a8 8 0 1 1 0 "
             "16A8 8 0 0 1 8 0ZM1.5 8a6.5 6.5 0 1 0 13 0 6.5 6.5 0 0 0-13 0Z",
    "fork": "M5 5.372v.878c0 .414.336.75.75.75h4.5a.75.75 0 0 0 .75-.75v-.878a"
            "2.25 2.25 0 1 1 1.5 0v.878a2.25 2.25 0 0 1-2.25 2.25h-1.5v2.128a"
            "2.251 2.251 0 1 1-1.5 0V8.5h-1.5A2.25 2.25 0 0 1 3.5 6.25v-.878a"
            "2.25 2.25 0 1 1 1.5 0ZM5 3.25a.75.75 0 1 0-1.5 0 .75.75 0 0 0 "
            "1.5 0Zm6.75.75a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Zm-3 8.75a.75"
            ".75 0 1 0-1.5 0 .75.75 0 0 0 1.5 0Z",
    "merge": "M5.45 5.154A4.25 4.25 0 0 0 9.25 7.5h1.378a2.251 2.251 0 1 1 0 "
             "1.5H9.25A5.734 5.734 0 0 1 5 7.123v3.505a2.25 2.25 0 1 1-1.5 "
             "0V5.372a2.25 2.25 0 1 1 1.95-.218ZM4.25 13.5a.75.75 0 1 0 0-1.5"
             ".75.75 0 0 0 0 1.5Zm8.5-4.5a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 "
             "1.5ZM5 3.25a.75.75 0 1 0-1.5 0 .75.75 0 0 0 1.5 0Z",
    "people": "M2 5.5a3.5 3.5 0 1 1 5.898 2.549 5.508 5.508 0 0 1 3.034 "
              "4.084.75.75 0 1 1-1.482.235 4 4 0 0 0-7.9 0 .75.75 0 0 "
              "1-1.482-.236A5.507 5.507 0 0 1 3.102 8.05 3.493 3.493 0 0 1 2 "
              "5.5ZM11 4a3.001 3.001 0 0 1 2.22 5.018 5.01 5.01 0 0 1 2.56 "
              "3.012.749.749 0 0 1-.885.954.752.752 0 0 1-.549-.514 3.507 "
              "3.507 0 0 0-2.522-2.372.75.75 0 0 1-.574-.73v-.352a.75.75 0 0 "
              "1 .416-.672A1.5 1.5 0 0 0 11 5.5.75.75 0 0 1 11 4Zm-5.5-.5a2 2 "
              "0 1 0-.001 3.999A2 2 0 0 0 5.5 3.5Z",
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


def from_others(*qualifiers: str) -> int:
    """Matches in this account's repos, excluding its own and the bots'."""
    exclusions = " ".join(f"-author:{a}"
                          for a in [USERNAME, *EXCLUDE_BOT_AUTHORS])
    return search_count(f"user:{USERNAME} {' '.join(qualifiers)} {exclusions}")


def contributors(repos: list[dict]) -> int:
    """Distinct humans who committed to the counted repos, minus this account.

    The endpoint pages at 100 per repo, which no repo here approaches; bots
    arrive with type "Bot" and are dropped, so dependabot cannot inflate this.
    """
    people: set[str] = set()
    for r in repos:
        for c in api(f"https://api.github.com/repos/{USERNAME}/{r['name']}"
                     f"/contributors?per_page=100"):
            if c.get("type") == "User":
                people.add(c["login"])
    return len(people - {USERNAME})


def collect() -> list[tuple[str, str, int]]:
    """The card's rows, as (icon key, label, value)."""
    repos = list_repos()
    print(f"{len(repos)} repos counted for stars and forks", file=sys.stderr)
    return [
        ("star", "Total Stars Earned",
         sum(r["stargazers_count"] for r in repos)),
        ("fork", "Forks of my repos", sum(r["forks_count"] for r in repos)),
        ("merge", "PRs merged from others", from_others("is:pr", "is:merged")),
        ("issue", "Issues filed in my repos", from_others("is:issue")),
        ("people", "Contributors", contributors(repos)),
        ("commit", "Total Commits", search_count(f"author:{USERNAME}",
                                                 endpoint="commits")),
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
