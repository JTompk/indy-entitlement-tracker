#!/usr/bin/env python3
"""
IndyIMBY digest generator (v2).

Builds the Monday digest around what a reader can still act on:

  1. "On the docket this week" — hearings with meeting_date in the next
     LOOKAHEAD_DAYS, grouped by meeting, items ranked by editorial priority
     (tax incentives > multi-family > DMD contracts > rezonings > rest).
  2. "New filings worth watching" — items first ingested in the last
     LOOKBACK_DAYS whose hearings are further out.
  3. Stats footer. Counts are the footnote now, not the lede.

Reads BOTH data sources:
  docs/data/filings.geojson   — geocoded petitions (the map's data)
  data/agenda_items.json      — MDC resolutions etc. (no address needed)

Usage:
  python scraper/digest.py
  python scraper/digest.py --lookahead 10 --lookback 7
  python scraper/digest.py --out my.md
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
from agenda_items import score_item  # noqa: E402

GEOJSON_PATH = ROOT / "docs" / "data" / "filings.geojson"
ITEMS_PATH = ROOT / "data" / "agenda_items.json"
DRAFTS_DIR = ROOT / "digest_drafts"
MAP_URL = "https://map.indyimby.com"

LOOKAHEAD_DAYS = 10   # hearings this far out count as "this week"
LOOKBACK_DAYS = 7     # "new" = first ingested within this window
MAX_PER_MEETING = 6   # items listed per meeting (rest summarized as a count)
MAX_NEW = 6           # items in "New filings worth watching"


def nice_date(d):
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def nice_meeting_date(iso):
    try:
        d = datetime.fromisoformat(iso).date()
        return f"{d.strftime('%A, %B')} {d.day}"
    except (ValueError, TypeError):
        return iso or "date TBD"


def load_all():
    records = []
    if GEOJSON_PATH.exists():
        gj = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
        records += [f["properties"] for f in gj.get("features", [])]
    if ITEMS_PATH.exists():
        records += json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
    return records


def fmt_item(r, tier):
    tag = f"**[{tier}]** " if tier else ""
    head = f"{tag}**{r['case']}**"
    where = r.get("address")
    if where:
        head += f" — {where}"
    loc = ", ".join(x for x in [
        f"{r['township']} Township" if r.get("township") else None,
        f"Council #{r['council_district']}" if r.get("council_district") else None,
    ] if x)
    if loc:
        head += f" ({loc})"
    if r.get("zoning_from") and r.get("zoning_to"):
        head += f". `{r['zoning_from']} → {r['zoning_to']}`"
    summary = (r.get("summary") or "").strip()
    if summary:
        head += f". {summary[:200].rstrip()}…"
    if r.get("agenda_url"):
        head += f" [Agenda]({r['agenda_url']})."
    return f"- {head}"


def build(lookahead, lookback):
    now = datetime.now(timezone.utc).date()
    today = now.isoformat()
    horizon = (now + timedelta(days=lookahead)).isoformat()
    cutoff = (now - timedelta(days=lookback)).isoformat()

    records = load_all()
    scored = [(r, *score_item(r)) for r in records]

    def dedupe(rows):
        """One entry per case; keep the record with the newest meeting_date."""
        best = {}
        for r, s, t in rows:
            k = r.get("case")
            if k not in best or (r.get("meeting_date") or "") > \
                    (best[k][0].get("meeting_date") or ""):
                best[k] = (r, s, t)
        return list(best.values())

    upcoming = dedupe((r, s, t) for r, s, t in scored
                      if today <= (r.get("meeting_date") or "") <= horizon)
    up_cases = {r.get("case") for r, s, t in upcoming}
    # "New" = recently ingested AND not already on this week's docket AND not
    # a past hearing (a new filing whose hearing already happened is history,
    # not news — this also keeps initial-backfill records out of the digest).
    fresh = dedupe((r, s, t) for r, s, t in scored
                   if (r.get("ingested") or "") >= cutoff
                   and r.get("case") not in up_cases
                   and (not r.get("meeting_date")
                        or r["meeting_date"] > horizon))

    lines = []

    # ------------------------------------------------ on the docket --
    if upcoming:
        meetings = defaultdict(list)
        for r, s, t in upcoming:
            meetings[(r.get("meeting_date"), r.get("board") or "DMD Board")].append((r, s, t))

        # Lede from the single highest-priority upcoming item.
        top_r, top_s, top_t = max(upcoming, key=lambda x: x[1])
        n_meetings = len(meetings)
        lede = (f"{n_meetings} DMD hearing{'s' if n_meetings != 1 else ''} "
                f"on the calendar through {nice_meeting_date(horizon)}.")
        if top_t:
            lede += (f" The headline: a {top_t.lower()} item at the "
                     f"{top_r.get('board', 'MDC')} — details below.")
        lines += [lede, "", "## On the docket this week", ""]

        for (mdate, board), items in sorted(meetings.items()):
            items.sort(key=lambda x: x[1], reverse=True)
            lines.append(f"### {nice_meeting_date(mdate)} — {board}")
            lines.append("")
            for r, s, t in items[:MAX_PER_MEETING]:
                lines.append(fmt_item(r, t))
            if len(items) > MAX_PER_MEETING:
                lines.append(f"- …plus {len(items) - MAX_PER_MEETING} more "
                             f"item{'s' if len(items) - MAX_PER_MEETING != 1 else ''} "
                             f"on this agenda.")
            lines.append("")
        lines += ["If one of these is near you, "
                  "[here's how to testify](/how-to-testify/).", ""]
    else:
        lines += ["No DMD hearings on the calendar in the next "
                  f"{lookahead} days. Quiet stretches happen — the "
                  f"[map]({MAP_URL}) stays live in the meantime.", ""]

    # ------------------------------------------------- new filings --
    fresh.sort(key=lambda x: x[1], reverse=True)
    if fresh:
        lines += ["## New filings worth watching", ""]
        for r, s, t in fresh[:MAX_NEW]:
            lines.append(fmt_item(r, t))
        if len(fresh) > MAX_NEW:
            lines.append(f"- …and {len(fresh) - MAX_NEW} more new "
                         f"filing{'s' if len(fresh) - MAX_NEW != 1 else ''} — "
                         f"all mapped on the [tracker]({MAP_URL}).")
        lines.append("")

    # ------------------------------------------------------ footer --
    n_up = len(upcoming)
    n_new = len(fresh)
    types = Counter((t or r.get("type") or "Other")
                    for r, s, t in upcoming + fresh)
    type_line = ", ".join(f"{t.lower()} ({n})"
                          for t, n in types.most_common(5))
    lines += ["---", "",
              f"*This week by the numbers: {n_up} item{'s' if n_up != 1 else ''} "
              f"on upcoming agendas, {n_new} new filing{'s' if n_new != 1 else ''} "
              f"since last Monday"
              + (f" ({type_line})" if type_line else "") + ". "
              f"Every mappable filing is on the "
              f"[Entitlement Tracker]({MAP_URL}), compiled from public DMD "
              f"agendas. See something we got wrong? Reply and tell us.*"]

    # -------------------------------------------------- frontmatter --
    if upcoming and top_t:
        summary = (f"{top_t} at the {top_r.get('board', 'MDC')}, "
                   f"{n_up} item{'s' if n_up != 1 else ''} on this week's agendas, "
                   f"{n_new} new filing{'s' if n_new != 1 else ''}.")
    elif upcoming:
        summary = f"{n_up} items on this week's DMD agendas; {n_new} new filings."
    else:
        summary = "A quiet week on the DMD dockets."

    front = "\n".join([
        "---",
        f"title: This week in Indy entitlement — {nice_date(now)}",
        f"date: {today}",
        f"summary: {summary}",
        "---",
    ])
    return front + "\n\n" + "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookahead", type=int, default=LOOKAHEAD_DAYS)
    ap.add_argument("--lookback", type=int, default=LOOKBACK_DAYS)
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    md = build(args.lookahead, args.lookback)
    out = Path(args.out) if args.out else \
        DRAFTS_DIR / f"{datetime.now(timezone.utc).date().isoformat()}-this-week.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"[done] draft written to {out}")


if __name__ == "__main__":
    main()
