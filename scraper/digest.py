#!/usr/bin/env python3
"""
IndyIMBY digest generator.

Reads docs/data/filings.geojson (built by scrape.py), pulls everything
ingested in the last N days, and drafts a Monday digest post in IndyIMBY's
markdown format. The draft lands in digest_drafts/ — edit for voice, then
drop it into the site repo's content/posts/ and push.

Usage:
  python scraper/digest.py               # last 7 days
  python scraper/digest.py --days 14
  python scraper/digest.py --out my.md
"""

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEOJSON_PATH = ROOT / "docs" / "data" / "filings.geojson"
DRAFTS_DIR = ROOT / "digest_drafts"

MAP_URL = "https://map.indyimby.com"


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def load_recent(days):
    gj = json.loads(GEOJSON_PATH.read_text())
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    recent = [f["properties"] for f in gj.get("features", [])
              if (f["properties"].get("ingested") or "") >= cutoff]
    return recent


def upcoming_hearings(records):
    today = datetime.now(timezone.utc).date().isoformat()
    dates = sorted({(r.get("meeting_date"), r.get("board"))
                    for r in records
                    if r.get("meeting_date") and r["meeting_date"] >= today})
    return dates


def notable(records, limit=5):
    """Rezonings first (they're the story), then anything with a rich summary."""
    rez = [r for r in records if r.get("type") == "Rezoning"]
    rest = [r for r in records if r.get("type") != "Rezoning"]
    ranked = sorted(rez, key=lambda r: len(r.get("summary") or ""), reverse=True) \
        + sorted(rest, key=lambda r: len(r.get("summary") or ""), reverse=True)
    return ranked[:limit]


def fmt_case(r):
    bits = [f"**{r['case']}** — {r.get('address') or 'address TBD'}"]
    loc = ", ".join(x for x in [
        f"{r['township']} Township" if r.get("township") else None,
        f"Council #{r['council_district']}" if r.get("council_district") else None,
    ] if x)
    if loc:
        bits.append(f"({loc})")
    line = " ".join(bits)
    if r.get("zoning_from") and r.get("zoning_to"):
        line += f". `{r['zoning_from']} → {r['zoning_to']}`"
    summary = (r.get("summary") or "").strip()
    if summary:
        # keep the first ~180 chars of the parsed block as a starting point
        line += f". {summary[:180].rstrip()}…"
    if r.get("agenda_url"):
        line += f" [Agenda]({r['agenda_url']})."
    line += "\n  <!-- TODO: your one-line take -->"
    return f"- {line}"


def build(days):
    records = load_recent(days)
    now = datetime.now(timezone.utc).date()
    title_date = now.strftime("%B %-d, %Y") if hasattr(now, "strftime") else str(now)

    if not records:
        body = (f"No new filings appeared on DMD agendas in the last {days} days.\n\n"
                f"Quiet weeks happen — hearings cluster around board calendars. "
                f"The [map]({MAP_URL}) stays live in the meantime.\n")
        summary = "A quiet week on the DMD dockets."
    else:
        types = Counter(r.get("type", "Other") for r in records)
        townships = Counter(r["township"] for r in records if r.get("township"))
        boards = Counter(r["board"] for r in records if r.get("board"))

        type_line = ", ".join(f"{n} {t.lower()}{'' if n == 1 else 's'}"
                              for t, n in types.most_common())
        twp_line = ", ".join(f"{t} ({n})" for t, n in townships.most_common(5))

        lines = [
            "<!-- TODO: 2–3 sentence editorial lede: what's the pattern this week? -->",
            "",
            f"**{plural(len(records), 'new filing')}** hit the DMD dockets this "
            f"week: {type_line}.",
        ]
        if twp_line:
            lines.append(f"Activity concentrated in {twp_line} — "
                         f"[see the map]({MAP_URL}).")
        lines += ["", "## Worth your attention", ""]
        lines += [fmt_case(r) for r in notable(records)]

        hearings = upcoming_hearings(records)
        if hearings:
            lines += ["", "## On the calendar", ""]
            for date, board in hearings:
                lines.append(f"- **{date}** — {board}")
            lines += ["", "If one of these is near you, "
                          "[here's how to testify](/how-to-testify/)."]

        lines += ["", "---", "",
                  f"*Every filing above is mapped on the "
                  f"[Entitlement Tracker]({MAP_URL}), compiled from public "
                  f"DMD agendas. See something we got wrong? Reply and "
                  f"tell us.*"]
        body = "\n".join(lines)
        top_type, top_n = types.most_common(1)[0]
        summary = (f"{len(records)} new filings this week, led by "
                   f"{top_n} {top_type.lower()}{'' if top_n == 1 else 's'}"
                   + (f"; busiest township: {townships.most_common(1)[0][0]}."
                      if townships else "."))

    front = "\n".join([
        "---",
        f"title: This week in Indy entitlement — {title_date}",
        f"date: {now.isoformat()}",
        f"summary: {summary}",
        "---",
    ])
    return front + "\n\n" + body + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    md = build(args.days)
    out = Path(args.out) if args.out else \
        DRAFTS_DIR / f"{datetime.now(timezone.utc).date().isoformat()}-this-week.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(f"[done] draft written to {out}")
    print("Edit for voice, then move it to the site repo's content/posts/ and push.")


if __name__ == "__main__":
    main()
