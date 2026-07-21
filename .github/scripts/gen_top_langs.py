#!/usr/bin/env python3
"""Generate a "Most Used Languages" donut card from the GitHub API.

This reproduces what github-readme-stats' top-langs card does (sum the language
*byte* counts reported by GitHub across the user's non-fork, owned repositories)
but with three differences that make it more honest and fully under our control:

  1. Percentages are real shares of the counted total, not renormalised over the
     handful of languages that happen to be shown. Everything below the cut is
     folded into a single "Other" slice, so the ring always sums to 100%.
  2. Every knob (which languages to hide, which repos to skip, manual byte
     adjustments, colours, how many slices to show) lives in the CONFIG block
     below and is easy to tweak.
  3. No animation, so the SVG renders correctly in a static rasteriser too.

Caveat worth remembering: the GitHub `/languages` endpoint returns *whole-repo*
byte totals. It cannot exclude a subdirectory of generated/vendored code. To do
that you have two options:
  * add a `.gitattributes` with e.g. `dist/** linguist-generated` (or
    `linguist-vendored`) to the offending repo -- GitHub's Linguist then drops
    those paths from `/languages` automatically (this also fixes the GRS card);
  * or switch this script to a heavier "clone each repo and run Linguist
    locally" mode -- see the LOCAL_LINGUIST placeholder near fetch_languages().
As a lightweight stopgap without either, use LANG_BYTE_OVERRIDES below to
subtract known generated content by hand.

Usage:
    python3 gen_top_langs.py [output.svg]      # default: profile/top-langs.svg
Auth: set GITHUB_TOKEN (or GH_TOKEN) in the environment to raise the API rate
limit; without it the script still works via unauthenticated calls.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG -- everything you'll want to tweak lives here.
# ─────────────────────────────────────────────────────────────────────────────

USERNAME = "PierreSenellart"

# Repository selection (matches github-readme-stats defaults).
INCLUDE_FORKS = False        # forks are noise: someone else's code base.
INCLUDE_ARCHIVED = True

# Languages never worth showing: dominated by generated / embedded / markup
# bytes rather than authored code. Names must match GitHub's Linguist names.
HIDE_LANGUAGES = {
    "Jupyter Notebook",      # notebooks embed base64 image output -> huge, fake.
    "HTML", "CSS", "SCSS",   # markup / styling, usually generated or boilerplate.
    "Roff",                  # man pages, generated.
    # JavaScript is intentionally *not* hidden: repos have authored JS (e.g.
    # provsql/studio). Exclude generated/vendored JS per-repo via .gitattributes
    # instead (Linguist already auto-drops vendor/ and *.min.js).
}

# Whole repositories to drop from the tally (e.g. a repo that is mostly a
# vendored library and skews one language). Repo names, case-sensitive.
EXCLUDE_REPOS: set[str] = set()

# Manual byte adjustments as a stopgap for generated content the API can't see
# per-directory. Subtracts bytes for a (repo, language) pair; clamped at 0.
#   ("myrepo", "C++"): 500_000,
LANG_BYTE_OVERRIDES: dict[tuple[str, str], int] = {}

# How many language slices to show before folding the rest into "Other".
MAX_LANGS = 8
SHOW_OTHER = True            # add an "Other" slice for everything past the cut.

# Colours. Defaults are Linguist's; override anything (Linguist has none for
# Lean, so we give it a distinct orange that avoids the two blues on the ring).
COLORS = {
    "C++": "#f34b7d",
    "PLpgSQL": "#336790",
    "Python": "#3572A5",
    "C": "#555555",
    "Lean": "#E67E22",       # Linguist has no colour for Lean.
    "TeX": "#3D6117",
    "Scala": "#c22d40",
    "JavaScript": "#f1e05a",
    "Shell": "#89e051",
    "Java": "#b07219",
    "Makefile": "#427819",
    "PHP": "#4F5D95",
    "Lua": "#000080",
    "Other": "#bbbbbb",
}
# Fallback palette for any language not in COLORS (cycled in appearance order).
FALLBACK_COLORS = ["#8e44ad", "#16a085", "#d35400", "#2c3e50", "#7f8c8d"]

# Card appearance.
TITLE = "Most Used Languages"
CARD_BG = "#fffefe"
CARD_BORDER = "#e4e2e2"
TITLE_COLOR = "#2f80ed"
TEXT_COLOR = "#434d58"
LOC_COLOR = "#8b949e"       # muted grey for the secondary "loc" column.

# ─────────────────────────────────────────────────────────────────────────────
# DATA -- fetch and aggregate.
# ─────────────────────────────────────────────────────────────────────────────


def api(url: str):
    headers = {"User-Agent": "top-langs-script",
               "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def list_repos() -> list[dict]:
    repos, page = [], 1
    while True:
        batch = api(f"https://api.github.com/users/{USERNAME}/repos"
                    f"?per_page=100&type=owner&page={page}")
        if not batch:
            break
        repos += batch
        page += 1
    def keep(r):
        if r["name"] in EXCLUDE_REPOS:
            return False
        if r["fork"] and not INCLUDE_FORKS:
            return False
        if r.get("archived") and not INCLUDE_ARCHIVED:
            return False
        return True
    return [r for r in repos if keep(r)]


def fetch_languages(repo_name: str) -> dict[str, int]:
    # LOCAL_LINGUIST placeholder: to exclude generated *directories*, replace
    # this call with a shallow clone + `github-linguist --breakdown` parse.
    return api(f"https://api.github.com/repos/{USERNAME}/{repo_name}/languages")


def aggregate(repos: list[dict]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for r in repos:
        for lang, size in fetch_languages(r["name"]).items():
            size -= LANG_BYTE_OVERRIDES.get((r["name"], lang), 0)
            if size <= 0 or lang in HIDE_LANGUAGES:
                continue
            totals[lang] = totals.get(lang, 0) + size
    return totals


# Ruby helper that emits Linguist's per-language file list (honours each repo's
# .gitattributes, exactly like the /languages endpoint) so counted lines match
# the byte-based percentages.
LINGUIST_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "linguist_breakdown.rb")


def count_lines(repos: list[dict]) -> dict[str, int]:
    """LOC per language, by cloning each repo and running github-linguist.

    Best-effort: returns {} (card renders without the LOC column) if line
    counting is disabled or the tooling is unavailable, and silently skips any
    individual repo that fails to clone or parse. Never fatal -- the byte-based
    percentages are the card's source of truth; LOC is a secondary column.
    """
    if os.environ.get("TOPLANGS_NO_LINES"):
        return {}
    if shutil.which("ruby") is None or not os.path.exists(LINGUIST_HELPER):
        print("LOC: ruby/linguist helper unavailable; skipping line counts",
              file=sys.stderr)
        return {}

    lines: dict[str, int] = {}
    for r in repos:
        name = r["name"]
        url = r.get("clone_url") or f"https://github.com/{USERNAME}/{name}.git"
        tmp = tempfile.mkdtemp(prefix="toplangs_")
        try:
            subprocess.run(["git", "clone", "--depth", "1", "--quiet", url, tmp],
                           check=True, capture_output=True)
            out = subprocess.run(["ruby", LINGUIST_HELPER, tmp],
                                 check=True, capture_output=True, text=True)
            breakdown = json.loads(out.stdout or "{}")
            for lang, files in breakdown.items():
                if lang in HIDE_LANGUAGES:
                    continue
                n = 0
                for rel in files:
                    try:
                        with open(os.path.join(tmp, rel), "rb") as fh:
                            n += fh.read().count(b"\n")
                    except OSError:
                        pass
                if n:
                    lines[lang] = lines.get(lang, 0) + n
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            print(f"  LOC: skipped {name} ({type(e).__name__})", file=sys.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# RENDER -- build the donut SVG.
# ─────────────────────────────────────────────────────────────────────────────


def color_for(lang: str, seen: list[str]) -> str:
    if lang in COLORS:
        return COLORS[lang]
    return FALLBACK_COLORS[seen.index(lang) % len(FALLBACK_COLORS)]


def build_svg(totals: dict[str, int], lines: dict[str, int] | None = None) -> str:
    lines = lines or {}
    has_loc = bool(lines)
    grand = sum(totals.values())
    ordered = sorted(totals.items(), key=lambda kv: -kv[1])
    shown = ordered[:MAX_LANGS]
    rest = ordered[MAX_LANGS:]
    if SHOW_OTHER and rest:
        shown.append(("Other", sum(v for _, v in rest)))

    def loc_for(lang: str) -> int:
        if lang == "Other":
            return sum(lines.get(k, 0) for k, _ in rest)
        return lines.get(lang, 0)

    langs = [k for k, _ in shown]
    segments = [(k, v, 100 * v / grand, loc_for(k)) for k, v in shown]

    # Layout. With a LOC column the card is wider and the numeric columns and
    # donut shift right; without it, the original compact layout is kept.
    pad = 25
    title_y = 35
    if has_loc:
        W = 470
        donut_cx = W - 70
        pct_right, loc_right = 185, 320
    else:
        W = 340
        donut_cx = W - 80
        pct_right = pad + 125
    donut_cy, R, SW = 128, 45, 17
    row_h = 22
    legend_x, legend_top = pad, 62
    legend_bottom = legend_top + row_h * len(segments)
    H = max(legend_bottom, donut_cy + R + SW // 2) + pad

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" fill="none" role="img" '
        f'aria-label="{TITLE}">',
        f'<rect x="0.5" y="0.5" rx="4.5" width="{W-1}" height="{H-1}" '
        f'fill="{CARD_BG}" stroke="{CARD_BORDER}"/>',
        f'<text x="{pad}" y="{title_y}" font-family="Segoe UI, Ubuntu, '
        f'Sans-Serif" font-size="18" font-weight="600" '
        f'fill="{TITLE_COLOR}">{TITLE}</text>',
    ]

    # Donut: one stroked circle per segment, offset around the ring, rotated so
    # it starts at 12 o'clock.
    circ = 2 * math.pi * R
    offset = 0.0
    parts.append(f'<g transform="rotate(-90 {donut_cx} {donut_cy})">')
    for lang, _, pct, _loc in segments:
        seg = circ * pct / 100
        parts.append(
            f'<circle cx="{donut_cx}" cy="{donut_cy}" r="{R}" '
            f'stroke="{color_for(lang, langs)}" stroke-width="{SW}" '
            f'fill="none" stroke-dasharray="{seg:.3f} {circ - seg:.3f}" '
            f'stroke-dashoffset="{-offset:.3f}"/>')
        offset += seg
    parts.append('</g>')

    # Legend: language name left-aligned; percentage right-aligned so the
    # percent signs line up in a neat column set a little apart from the names.
    name_x = legend_x + 18
    font = 'font-family="Segoe UI, Ubuntu, Sans-Serif" font-size="12"'
    for i, (lang, _, pct, loc) in enumerate(segments):
        y = legend_top + i * row_h
        parts.append(
            f'<rect x="{legend_x}" y="{y}" width="11" height="11" rx="2.5" '
            f'fill="{color_for(lang, langs)}"/>')
        parts.append(
            f'<text x="{name_x}" y="{y + 10}" {font} '
            f'fill="{TEXT_COLOR}">{lang}</text>')
        parts.append(
            f'<text x="{pct_right}" y="{y + 10}" text-anchor="end" {font} '
            f'fill="{TEXT_COLOR}">{pct:.2f}%</text>')
        if has_loc:
            parts.append(
                f'<text x="{loc_right}" y="{y + 10}" text-anchor="end" {font} '
                f'fill="{LOC_COLOR}">{loc:,}'
                f'<tspan font-size="10" dx="2">loc</tspan></text>')

    parts.append('</svg>')
    return "\n".join(parts)


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "profile/top-langs.svg"
    repos = list_repos()
    totals = aggregate(repos)
    lines = count_lines(repos)
    svg = build_svg(totals, lines)
    with open(out, "w") as f:
        f.write(svg + "\n")
    # Diagnostics to stderr so the SVG on stdout path stays clean.
    grand = sum(totals.values())
    print(f"{len(repos)} repos counted; {len(totals)} languages; "
          f"{grand} bytes total; {sum(lines.values())} loc total", file=sys.stderr)
    for lang, size in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {lang:<16}{100*size/grand:6.2f}%  ({size} B, "
              f"{lines.get(lang, 0)} loc)", file=sys.stderr)
    print(f"wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
