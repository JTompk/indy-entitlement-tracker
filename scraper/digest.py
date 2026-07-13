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
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
from agenda_items import score_item, max_dollar  # noqa: E402

GEOJSON_PATH = ROOT / "docs" / "data" / "filings.geojson"
ITEMS_PATH = ROOT / "data" / "agenda_items.json"
DRAFTS_DIR = ROOT / "digest_drafts"
MAP_URL = "https://map.indyimby.com"

# ------------------------------------------------ plain language --
# Plain-English names for zoning districts, used in auto-summaries.
# CALIBRATE: adjust wording to match how you'd explain each to a neighbor.
DISTRICT_NAMES = {
    "D-1": "large-lot single-family", "D-2": "single-family", "D-3": "single-family",
    "D-4": "single-family", "D-5": "compact single-family/two-family",
    "D-6": "small-lot residential", "D-7": "townhome-scale residential",
    "D-8": "low-rise apartment", "D-9": "apartment", "D-10": "apartment",
    "D-11": "high-rise residential", "D-P": "planned residential",
    "D-S": "suburban residential", "D-A": "agricultural/estate residential",
    "C-1": "office", "C-2": "neighborhood commercial", "C-3": "general commercial",
    "C-4": "community commercial", "C-5": "heavy commercial",
    "C-6": "high-intensity commercial", "C-7": "high-intensity commercial",
    "C-S": "planned commercial",
    "MU-1": "mixed-use", "MU-2": "mixed-use", "MU-3": "mixed-use", "MU-4": "mixed-use",
    "CBD-1": "downtown", "CBD-2": "downtown", "CBD-3": "downtown", "CBD-S": "downtown",
    "I-1": "light industrial", "I-2": "industrial", "I-3": "industrial",
    "I-4": "heavy industrial", "PK-1": "park", "HD-1": "hospital", "HD-2": "hospital",
}

ACRES_RE = re.compile(r"([\d.]+)[\s-]*acres?", re.IGNORECASE)
PROVIDE_RE = re.compile(r"to provide for (?:an? |the )?(.{5,90}?)(?:[,.]|$)", re.IGNORECASE)
PERMIT_RE = re.compile(r"to (?:permit|allow) (?:an? |the )?(.{5,90}?)(?:[,.]|$)", re.IGNORECASE)
AMOUNT_RE = re.compile(r"not[- ]to[- ]exceed\s*\$?([\d,]+)", re.IGNORECASE)
BENEFITS_FOR_RE = re.compile(
    r"(?:statement of benefits|abatement)\s+for\s+(?:an? |the )?(.{5,90}?)(?:[,.]|$)",
    re.IGNORECASE)
ADDR_IN_TEXT_RE = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+|North\s+|South\s+|East\s+|West\s+)?"
    r"[A-Za-z0-9'. -]{2,40}?\s(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|"
    r"Boulevard|Blvd|Lane|Ln|Way|Court|Ct|Circle|Cir|Place|Pl|Pike|Parkway|"
    r"Pkwy|Trail|Terrace|Highway|Hwy)\b", re.IGNORECASE)


def nice_addr(raw):
    """Title-case an address without mangling ordinals (18Th -> 18th)."""
    if not raw:
        return None
    t = str(raw).title()
    t = re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), t)
    return t.replace(" And ", " and ")


def plain_district(code):
    code = (code or "").upper().strip()
    base = DISTRICT_NAMES.get(code)
    if base:
        return f"{base} ({code})"
    return code or "its current zoning"


def plain_summary(r, tier):
    """One neighbor-friendly sentence for a record; None to fall back to raw."""
    text = " ".join(str(r.get(k) or "") for k in ("title", "summary"))
    tl = text.lower()
    addr = nice_addr(r.get("address"))
    if not addr:  # resolutions carry no address field; pull one from the text
        m = ADDR_IN_TEXT_RE.search(text)
        addr = nice_addr(m.group(0)) if m else "this site"
    acres = ACRES_RE.search(text)
    size = f"{acres.group(1)}-acre site" if acres else "property"
    use = PROVIDE_RE.search(text) or PERMIT_RE.search(text)
    use_txt = use.group(1).strip().rstrip(".") if use else None

    if tier == "Tax incentive":
        if "compliance" in tl:
            return (f"The city is checking whether the tax-break recipient at {addr} "
                    f"is keeping the job and investment promises attached to its abatement.")
        detail = use_txt
        if not detail:
            b = BENEFITS_FOR_RE.search(text)
            detail = b.group(1).strip().rstrip(".") if b else None
        return (f"The city is considering a property-tax break (abatement) for a "
                f"project at {addr}"
                + (f" — {detail}" if detail else "") + ".")

    if tier == "DMD contract":
        amt = AMOUNT_RE.search(text)
        return ("The MDC would authorize the planning department to sign a contract"
                + (f" worth up to ${amt.group(1)}" if amt else "") + ".")

    if r.get("zoning_from") and r.get("zoning_to"):
        line = (f"A property owner wants to rezone the {size} at {addr} from "
                f"{plain_district(r['zoning_from'])} to "
                f"{plain_district(r['zoning_to'])} zoning")
        if use_txt:
            line += f" to build {use_txt}"
        return line + "."

    if "variance of use" in tl:
        return (f"The owner of {addr} is asking permission for a use the current "
                f"zoning doesn't allow"
                + (f": {use_txt}" if use_txt else "") + ".")
    if "variance of development standards" in tl or "development standards" in tl:
        return (f"The owner of {addr} is asking to bend the site rules "
                f"(things like setbacks, height, or parking) for a project there.")
    if r.get("type") == "Plat / Subdivision":
        return f"A landowner wants to split or replat the {size} at {addr} into new lots."
    if use_txt:
        return f"A filing at {addr} proposes {use_txt}."
    return None

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
    plain = plain_summary(r, tier)
    if plain:
        head += f". {plain}"
    else:
        raw = (r.get("summary") or "").strip()
        if raw:
            head += f". {raw[:200].rstrip()}…"
    if r.get("agenda_url"):
        head += f" [Full agenda]({r['agenda_url']})."
    return f"- {head}"


def fmt_dollars(n):
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"${v:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"${n/1_000:.0f}K"
    return f"${n:,}"


def incentive_total(rows):
    """(count, summed dollars) across tax-incentive items; dollars best-effort."""
    tot, cnt = 0, 0
    for r, s, t in rows:
        if t != "Tax incentive":
            continue
        cnt += 1
        d = r.get("dollars") or max_dollar(
            " ".join(str(r.get(k) or "") for k in ("title", "summary")))
        if d:
            tot += d
    return cnt, tot


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
        inc_n, inc_total = incentive_total(upcoming)
        if inc_n and inc_total:
            lede += (f" This week's agendas carry {inc_n} tax-incentive "
                     f"item{'s' if inc_n != 1 else ''} tied to roughly "
                     f"{fmt_dollars(inc_total)} in project investment.")
        elif inc_n:
            lede += (f" This week's agendas carry {inc_n} tax-incentive "
                     f"item{'s' if inc_n != 1 else ''}.")
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
        # Live embedded map of this week's docket (iframe passes through
        # markdown untouched; email clients strip it, so keep the link too).
        lines += [f'<iframe src="{MAP_URL}/?week={today}&embed=1" '
                  f'width="100%" height="420" '
                  f'style="border:1px solid #ddd;border-radius:6px" '
                  f'loading="lazy" title="This week\'s docket map"></iframe>', "",
                  f"[**Open this week's docket map in full →**]"
                  f"({MAP_URL}/?week={today})", "",
                  "If one of these is near you, "
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
    if upcoming and top_t == "Tax incentive" and inc_total:
        summary = (f"{inc_n} tax-incentive item{'s' if inc_n != 1 else ''} "
                   f"(~{fmt_dollars(inc_total)}) at the {top_r.get('board', 'MDC')}, "
                   f"{n_up} item{'s' if n_up != 1 else ''} on this week's agendas, "
                   f"{n_new} new filing{'s' if n_new != 1 else ''}.")
    elif upcoming and top_t:
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
