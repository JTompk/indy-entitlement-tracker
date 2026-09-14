#!/usr/bin/env python3
"""
IndyIMBY digest generator (v3).

Builds the Monday digest around what a reader can still act on, sorted by
WHAT IS BEING BUILT rather than which board hears it:

  1. Lede + "The week at a glance" — four or five plain-English bullets
     (headline item, housing, where the action is, incentive dollars,
     biggest site). This part is plain markdown so it survives email/RSS.
  2. Live docket map (iframe, ?week=) — unchanged.
  3. Filter chips + use-case sections: Housing / Shops, offices & mixed-use /
     Industrial & logistics / Community & institutional / Tax incentives &
     city deals / Historic districts / Land splits & site tweaks / Everything
     else. Each item carries a small county-locator SVG (3×3 township grid
     with a dot), the hearing day + board, a plain-language sentence, a
     "see it on the map" deep link (?case=) and the agenda PDF.
     Emitted as raw HTML so the site can filter it client-side; the chips
     are plain anchor links, so the list still reads fine in email.
  4. "New filings worth watching" — same item format, one flat list.
  5. Stats footer.

Reads BOTH data sources:
  docs/data/filings.geojson   — geocoded petitions (the map's data)
  data/agenda_items.json      — MDC resolutions etc. (no address needed)

Usage:
  python scraper/digest.py
  python scraper/digest.py --lookahead 10 --lookback 7
  python scraper/digest.py --out my.md
"""

import argparse
import html as htmlmod
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
from agenda_items import score_item, max_dollar, MF_DISTRICTS  # noqa: E402

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
LEAD_USE_RE = re.compile(
    r"^(?:the |an? )?(?:proposed )?(?:(?:construction|development|establishment|"
    r"operation|conversion|expansion) of (?:an? |the )?)?", re.IGNORECASE)
TRAIL_NOTE_RE = re.compile(r"\s*\((?:not permitted|prohibited)[^)]*\)\s*$", re.IGNORECASE)


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


def item_text(r):
    return " ".join(str(r.get(k) or "") for k in ("title", "summary"))


def item_addr(r):
    """Best display address for a record: the address field, else one
    pulled from the text, else None."""
    addr = nice_addr(r.get("address"))
    if not addr:
        m = ADDR_IN_TEXT_RE.search(item_text(r))
        addr = nice_addr(m.group(0)) if m else None
    return addr


def use_phrase(r):
    """'6 townhomes', 'a maintenance shop with office space' — the stated
    end use, cleaned of 'the construction of' boilerplate; None if absent."""
    text = item_text(r)
    use = PROVIDE_RE.search(text) or PERMIT_RE.search(text)
    if not use:
        return None
    u = use.group(1).strip().rstrip(".")
    u = TRAIL_NOTE_RE.sub("", LEAD_USE_RE.sub("", u)).strip()
    return u or None


def plain_summary(r, tier):
    """One neighbor-friendly sentence for a record; None to fall back to raw."""
    text = item_text(r)
    tl = text.lower()
    addr = item_addr(r) or "this site"
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
        return f"A filing at {addr} proposes {use_phrase(r) or use_txt}."
    return None


# ------------------------------------------------- use categories --
# The digest is sectioned by WHAT is being built. Order = display order.
# CALIBRATE: term lists are plain data; the classifier is zoning-first
# (the district asked for is the most honest signal), then keywords.
CATEGORIES = [
    ("housing", "Housing",
     "Homes, apartments, and townhomes — and the rezonings and plats that would create them."),
    ("commerce", "Shops, offices & mixed-use",
     "Retail, restaurants, offices, hotels, and mixed-use buildings."),
    ("industrial", "Industrial & logistics",
     "Warehouses, distribution, manufacturing, contractor yards, and outdoor storage."),
    ("community", "Community & institutional",
     "Churches, schools, child care, health care, parks, and public facilities."),
    ("incentives", "Tax incentives & city deals",
     "Abatements, revitalization-area resolutions, and contracts the MDC is asked to approve."),
    ("historic", "Historic districts",
     "Certificates of appropriateness and other IHPC business."),
    ("site", "Land splits & site tweaks",
     "Plats and development-standards variances where the filing doesn't say what gets built."),
    ("other", "Everything else",
     "Filings the parser couldn't sort — often worth a look."),
]
CAT_LABEL = {cid: label for cid, label, _ in CATEGORIES}
CAT_ORDER = {cid: i for i, (cid, _, _) in enumerate(CATEGORIES)}

HOUSING_TERMS = [
    "multi-family", "multifamily", "multi family", "apartment", "dwelling",
    "residential", "single-family", "single family", "two-family", "duplex",
    "townhome", "townhouse", "condominium", "accessory dwelling", "adu",
    "senior housing", "affordable housing", "workforce housing", "housing",
    "homes", "houses",
]
COMMERCE_TERMS = [
    "retail", "restaurant", "office", "commercial", "mixed-use", "mixed use",
    "hotel", "brewery", "car wash", "gas station", "fuel", "drive-through",
    "drive-thru", "bank", "salon", "store", "shop", "dispensary", "convenience",
    "grocery", "tavern", "event venue", "short-term rental", "home occupation",
    "self-storage", "self storage", "dealership", "automobile sales",
    "auto repair", "vehicle sales", "kennel", "veterinar", "fitness",
]
INDUSTRIAL_TERMS = [
    "warehouse", "industrial", "distribution", "manufactur", "logistics",
    "truck", "outdoor storage", "salvage", "recycling", "contractor",
    "data center", "solar", "freight", "towing", "junk",
]
COMMUNITY_TERMS = [
    "church", "worship", "religious", "school", "daycare", "day care",
    "child care", "childcare", "hospital", "clinic", "medical", "park",
    "library", "cemetery", "university", "college", "nonprofit",
    "community center", "shelter", "fire station", "police", "government",
    "cell tower", "telecommunication", "wireless",
]


def _hit(tl, terms):
    """True if any term matches on a word boundary (so 'adu' != 'graduate')."""
    for t in terms:
        if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", tl):
            return True
    return False


def categorize(r, tier):
    tl = " ".join(str(r.get(k) or "") for k in
                  ("title", "summary", "description", "type")).lower()
    board = (r.get("board") or "").lower()
    typ = r.get("type") or ""
    zt = (r.get("zoning_to") or "").upper().strip()

    if (tier in ("Tax incentive", "DMD contract") or r.get("kind") == "resolution"
            or typ in ("Tax incentive", "MDC Resolution")):
        return "incentives"
    if "ihpc" in board or typ.lower().startswith("historic"):
        return "historic"
    # zoning asked for is the most honest signal
    if zt.startswith("I-"):
        return "industrial"
    if zt.startswith("SU"):
        return "community"
    if zt.startswith("D-") or zt.startswith("DP"):
        return "housing"
    if zt in MF_DISTRICTS:   # MU-*/CBD-*: mixed unless the text says otherwise
        return "commerce" if (_hit(tl, COMMERCE_TERMS) and not _hit(tl, HOUSING_TERMS)) else "housing"
    if zt.startswith("C-"):
        return "commerce"
    # then the words in the filing
    if tier == "Multi-family" or _hit(tl, HOUSING_TERMS):
        return "housing"
    if _hit(tl, COMMUNITY_TERMS):
        return "community"
    if _hit(tl, INDUSTRIAL_TERMS):
        return "industrial"
    if _hit(tl, COMMERCE_TERMS):
        return "commerce"
    if typ in ("Plat / Subdivision", "Modification", "Commitment / Covenant",
               "Approval / Appeal") or "development standards" in tl:
        return "site"
    return "other"


# ---------------------------------------------------- locator svg --
# Marion County is close to a 3×3 grid of townships. Extents and split
# lines below were derived from ~2,300 geocoded filings (2nd/98th
# percentiles per township), so the glyph is a schematic, not a survey.
LON_MIN, LON_MAX = -86.328, -85.938
LAT_MIN, LAT_MAX = 39.633, 39.927
X_SPLITS = (-86.201, -86.084)     # west | center | east columns
Y_SPLITS = (39.825, 39.735)       # north | middle | south rows (top-down)
KNOWN_TOWNSHIPS = {"Pike", "Washington", "Lawrence", "Wayne", "Center",
                   "Warren", "Decatur", "Perry", "Franklin"}
GRID_TWP = [["Pike", "Washington", "Lawrence"],
            ["Wayne", "Center", "Warren"],
            ["Decatur", "Perry", "Franklin"]]
SIDE_NAME = [["northwest side", "north side", "northeast side"],
             ["west side", "center", "east side"],
             ["southwest side", "south side", "southeast side"]]

LOC_W = 40.0   # viewBox units; rendered size is set in CSS / attributes


def _frac(v, lo, hi):
    return min(1.0, max(0.0, (v - lo) / (hi - lo)))


def locator_svg(lonlat, township=None):
    """Inline SVG: county square, township hairlines, home cell shaded,
    accent dot at the site. No coordinates -> empty grid, dimmed."""
    xs = [round(_frac(x, LON_MIN, LON_MAX) * LOC_W, 1) for x in X_SPLITS]
    ys = [round((1 - _frac(y, LAT_MIN, LAT_MAX)) * LOC_W, 1) for y in Y_SPLITS]
    edges_x = [0.0] + xs + [LOC_W]
    edges_y = [0.0] + ys + [LOC_W]
    parts = []
    title = "Not mapped"
    dim = ' opacity=".45"'
    if lonlat:
        lon, lat = lonlat
        px = round(_frac(lon, LON_MIN, LON_MAX) * LOC_W, 1)
        py = round((1 - _frac(lat, LAT_MIN, LAT_MAX)) * LOC_W, 1)
        col = sum(1 for x in xs if px > x)
        row = sum(1 for y in ys if py > y)
        twp = township if township in KNOWN_TOWNSHIPS else GRID_TWP[row][col]
        title = f"{SIDE_NAME[row][col].capitalize()} — {twp} Township"
        cx0, cx1 = edges_x[col], edges_x[col + 1]
        cy0, cy1 = edges_y[row], edges_y[row + 1]
        parts.append(f'<rect x="{cx0}" y="{cy0}" width="{round(cx1 - cx0, 1)}" '
                     f'height="{round(cy1 - cy0, 1)}" fill="#FAD980" fill-opacity=".55"/>')
        dim = ""
    grid = "".join(f"M{x} 0V{LOC_W}" for x in xs) + "".join(f"M0 {y}H{LOC_W}" for y in ys)
    parts.append(f'<path d="{grid}" stroke="#C9C8C0" stroke-width=".8" fill="none"/>')
    parts.append(f'<rect x=".5" y=".5" width="{LOC_W - 1}" height="{LOC_W - 1}" '
                 f'fill="none" stroke="#232320" stroke-width="1"/>')
    if lonlat:
        parts.append(f'<circle cx="{px}" cy="{py}" r="3" fill="#FAD980" '
                     f'stroke="#232320" stroke-width="1.1"/>')
    return (f'<svg class="loc" viewBox="0 0 {LOC_W:g} {LOC_W:g}" width="44" height="44" '
            f'role="img"{dim}><title>{htmlmod.escape(title)}</title>'
            + "".join(parts) + "</svg>")


# ---------------------------------------------------------- knobs --
LOOKAHEAD_DAYS = 10   # hearings this far out count as "this week"
LOOKBACK_DAYS = 7     # "new" = first ingested within this window
MAX_PER_SECTION = 12  # items listed per use-case section (rest summarized)
MAX_NEW = 6           # items in "New filings worth watching"


def nice_date(d):
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def nice_meeting_date(iso):
    try:
        d = datetime.fromisoformat(iso).date()
        return f"{d.strftime('%A, %B')} {d.day}"
    except (ValueError, TypeError):
        return iso or "date TBD"


def short_meeting_date(iso):
    try:
        d = datetime.fromisoformat(iso).date()
        return f"{d.strftime('%a %b')} {d.day}"
    except (ValueError, TypeError):
        return iso or "date TBD"


def load_all():
    records = []
    if GEOJSON_PATH.exists():
        gj = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
        for f in gj.get("features", []):
            p = dict(f.get("properties") or {})
            g = f.get("geometry") or {}
            if g.get("type") == "Point" and g.get("coordinates"):
                p["_lonlat"] = tuple(g["coordinates"][:2])
            records.append(p)
    if ITEMS_PATH.exists():
        records += json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
    return records


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
        d = r.get("dollars") or max_dollar(item_text(r))
        if d:
            tot += d
    return cnt, tot


def item_acres(r):
    m = ACRES_RE.search(item_text(r))
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def esc(s):
    return htmlmod.escape(str(s if s is not None else ""), quote=True)


# ---------------------------------------------------- html items --

def fmt_item_html(r, tier, cat, week=None, show_tag=False):
    """One <li> for the docket lists. Raw HTML (not markdown) so the site
    can filter by data-cat and the locator SVG can ride along."""
    case = r.get("case") or ""
    addr = item_addr(r)
    meta = [x for x in [
        short_meeting_date(r.get("meeting_date")) if r.get("meeting_date") else None,
        r.get("board") or None,
        f"{r['township']} Twp" if r.get("township") in KNOWN_TOWNSHIPS else None,
        f"Council #{r['council_district']}" if r.get("council_district") else None,
    ] if x]
    head = f"<b>{esc(case)}</b>"
    if addr:
        head += f" — {esc(addr)}"
    if r.get("zoning_from") and r.get("zoning_to"):
        head += f' <code>{esc(r["zoning_from"])} → {esc(r["zoning_to"])}</code>'
    if show_tag:
        head += f' <span class="dk-tag">{esc(CAT_LABEL[cat])}</span>'
    plain = plain_summary(r, tier)
    if not plain:
        raw = (r.get("summary") or "").strip()
        plain = (raw[:200].rstrip() + "…") if raw else ""
    links = []
    if r.get("_lonlat") and case:
        q = f"case={case}" + (f"&week={week}" if week else "")
        links.append(f'<a href="{MAP_URL}/?{q}">See it on the map</a>')
    if r.get("agenda_url"):
        links.append(f'<a href="{esc(r["agenda_url"])}">Full agenda (PDF)</a>')
    cid = re.sub(r"[^A-Za-z0-9-]", "", case) or "item"
    return (f'<li class="dk" data-cat="{cat}" id="c-{cid}">'
            + locator_svg(r.get("_lonlat"), r.get("township"))
            + '<div class="dk-body">'
            + (f'<div class="dk-meta">{esc(" · ".join(meta))}</div>' if meta else "")
            + f'<div class="dk-head">{head}</div>'
            + (f"<p>{esc(plain)}</p>" if plain else "")
            + (f'<div class="dk-links">{" · ".join(links)}</div>' if links else "")
            + "</div></li>")


def glance_bullets(upcoming, fresh, cats, horizon, top):
    """Four or five one-sentence bullets for the top of the post.
    Only bullets with real data behind them are emitted."""
    out = []
    top_r, top_s, top_t = top
    n_up = len(upcoming)
    boards = defaultdict(list)
    for r, s, t in upcoming:
        boards[r.get("meeting_date") or ""].append(r.get("board") or "DMD Board")
    days = []
    for d in sorted(boards):
        bs = sorted(set(boards[d]))
        days.append(f"{short_meeting_date(d)} ({', '.join(bs)})" if d else ", ".join(bs))
    if days:
        out.append(f"<b>{n_up} item{'s' if n_up != 1 else ''}</b> across "
                   f"{len(days)} hearing day{'s' if len(days) != 1 else ''}: "
                   + "; ".join(esc(x) for x in days) + ".")

    # headline
    if top_r:
        hp = plain_summary(top_r, top_t) or (top_r.get("summary") or "")[:160]
        tag = ", ".join(x for x in [top_r.get("case"), top_r.get("board")] if x)
        out.append(f"<b>Headline:</b> {esc(hp.rstrip('.'))}"
                   + (f" ({esc(tag)})" if tag else "") + ".")

    # housing
    hous = [(r, s, t) for r, s, t in upcoming if cats.get(r.get("case")) == "housing"]
    if hous:
        hous.sort(key=lambda x: x[1], reverse=True)
        ex = []
        for r, s, t in hous:
            u, a = use_phrase(r), item_addr(r)
            if u and a and len(u) <= 72:
                ex.append(f"{u} at {a}")
            if len(ex) == 2:
                break
        line = (f"<b>Housing:</b> {len(hous)} filing{'s' if len(hous) != 1 else ''} "
                f"would add or enable homes")
        if ex:
            line += " — " + esc("; ".join(ex))
            rest = len(hous) - len(ex)
            if rest > 0:
                line += f"; and {rest} more"
        out.append(line + ".")

    # where
    twp = Counter(r.get("township") for r, s, t in upcoming
                  if r.get("township") in KNOWN_TOWNSHIPS)
    if twp:
        tops = twp.most_common(2)
        if len(tops) == 2 and tops[1][1] >= 2:
            out.append(f"<b>Where:</b> most of the action is in {esc(tops[0][0])} "
                       f"({tops[0][1]}) and {esc(tops[1][0])} ({tops[1][1]}) townships.")
        else:
            out.append(f"<b>Where:</b> {esc(tops[0][0])} Township leads with "
                       f"{tops[0][1]} item{'s' if tops[0][1] != 1 else ''}.")

    # money
    inc_n, inc_total = incentive_total(upcoming)
    if inc_n:
        out.append(f"<b>Public money:</b> {inc_n} tax-incentive item{'s' if inc_n != 1 else ''}"
                   + (f" tied to roughly {fmt_dollars(inc_total)} in project investment"
                      if inc_total else "") + ".")

    # biggest site
    sized = [(item_acres(r), r) for r, s, t in upcoming if item_acres(r)]
    if sized:
        ac, r = max(sized, key=lambda x: x[0])
        if ac >= 5:
            out.append(f"<b>Biggest site:</b> {ac:g} acres at "
                       f"{esc(item_addr(r) or r.get('case') or 'an unlisted address')} "
                       f"({esc(CAT_LABEL[cats.get(r.get('case'), 'other')].lower())}).")

    if fresh:
        out.append(f"<b>New since last Monday:</b> {len(fresh)} filing"
                   f"{'s' if len(fresh) != 1 else ''} with hearings further out — listed at the bottom.")
    return out[:6]


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
    fresh.sort(key=lambda x: x[1], reverse=True)

    cats = {r.get("case"): categorize(r, t) for r, s, t in upcoming + fresh}
    counts = Counter(cats[r.get("case")] for r, s, t in upcoming + fresh[:MAX_NEW])

    lines = []
    top_r = top_t = None
    inc_n = inc_total = 0

    # ------------------------------------------------------- lede --
    if upcoming:
        top_r, top_s, top_t = max(upcoming, key=lambda x: x[1])
        n_days = len({r.get("meeting_date") for r, s, t in upcoming})
        lede = (f"{len(upcoming)} item{'s' if len(upcoming) != 1 else ''} on "
                f"{n_days} hearing day{'s' if n_days != 1 else ''} through "
                f"{nice_meeting_date(horizon)}, sorted below by what would get built.")
        if top_t:
            lede += (f" The headline: a {top_t.lower()} item at the "
                     f"{top_r.get('board', 'MDC')}.")
        inc_n, inc_total = incentive_total(upcoming)
        lines += [lede, ""]

        # ------------------------------------------------ at a glance --
        bullets = glance_bullets(upcoming, fresh, cats, horizon, (top_r, top_s, top_t))
        if bullets:
            lines += ['<aside class="glance">',
                      '<div class="glance-head">The week at a glance</div>',
                      "<ul>"]
            lines += [f"<li>{b}</li>" for b in bullets]
            lines += ["</ul>", "</aside>", ""]

        # ---------------------------------------------------- live map --
        # (iframe passes through markdown untouched; email clients strip it,
        # so the plain link below stays.)
        lines += ['<figure class="docket-map">',
                  f'<iframe src="{MAP_URL}/?week={today}&embed=1" width="100%" height="420" '
                  f'style="border:1px solid #232320;display:block" loading="lazy" '
                  f'title="This week\'s docket map"></iframe>',
                  '<figcaption class="docket-map-cap">Every dot is a hearing in the next '
                  f'{lookahead} days. Click one for the filing; '
                  f'<a href="{MAP_URL}/?week={today}">open the full map</a> to filter and search.'
                  "</figcaption>", "</figure>", ""]

        # -------------------------------------------- chips + sections --
        lines += ["## On the docket this week", ""]
        chips = [f'<a class="chip is-on" href="#docket" data-cat="all">All '
                 f'<span>{len(upcoming)}</span></a>']
        for cid, label, _ in CATEGORIES:
            if counts.get(cid):
                chips.append(f'<a class="chip" href="#{cid}" data-cat="{cid}">'
                             f'{esc(label)} <span>{counts[cid]}</span></a>')
        lines += ['<nav class="chips" id="docket" aria-label="Filter by what is being built">'
                  + "".join(chips) + "</nav>",
                  '<p class="chips-note">Pick what you care about — the list narrows to match.</p>',
                  '<div class="docket-wrap">']
        by_cat = defaultdict(list)
        for r, s, t in upcoming:
            by_cat[cats[r.get("case")]].append((r, s, t))
        for cid, label, desc in CATEGORIES:
            items = by_cat.get(cid)
            if not items:
                continue
            items.sort(key=lambda x: (-x[1], x[0].get("meeting_date") or ""))
            lines.append(f'<h2 class="dk-h" id="{cid}" data-cat="{cid}">{esc(label)} '
                         f'<span class="dk-count">{len(items)}</span></h2>')
            lines.append(f'<p class="dk-desc" data-cat="{cid}">{esc(desc)}</p>')
            lines.append(f'<ul class="docket" data-cat="{cid}">')
            for r, s, t in items[:MAX_PER_SECTION]:
                lines.append(fmt_item_html(r, t, cid, week=today))
            lines.append("</ul>")
            if len(items) > MAX_PER_SECTION:
                more = len(items) - MAX_PER_SECTION
                lines.append(f'<p class="dk-more" data-cat="{cid}">…plus {more} more '
                             f'{esc(label.lower())} item{"s" if more != 1 else ""} — all on the '
                             f'<a href="{MAP_URL}/?week={today}">docket map</a>.</p>')
        lines += ['<p class="dk-empty">Nothing in that category this week.</p>',
                  "</div>", "",
                  "If one of these is near you, [here's how to testify](/how-to-testify/).", ""]
    else:
        lines += ["No DMD hearings on the calendar in the next "
                  f"{lookahead} days. Quiet stretches happen — the "
                  f"[map]({MAP_URL}) stays live in the meantime.", ""]

    # ------------------------------------------------- new filings --
    if fresh:
        lines += ["## New filings worth watching", "",
                  "Fresh on the docket, with hearings beyond this week's window.", "",
                  '<div class="docket-wrap">', '<ul class="docket" data-cat="new">']
        for r, s, t in fresh[:MAX_NEW]:
            lines.append(fmt_item_html(r, t, cats[r.get("case")], show_tag=True))
        lines.append("</ul>")
        if len(fresh) > MAX_NEW:
            more = len(fresh) - MAX_NEW
            lines.append(f'<p class="dk-more">…and {more} more new filing'
                         f'{"s" if more != 1 else ""} — all mapped on the '
                         f'<a href="{MAP_URL}">tracker</a>.</p>')
        lines += ["</div>", ""]

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
    hous_n = sum(1 for r, s, t in upcoming if cats.get(r.get("case")) == "housing")
    if upcoming and hous_n:
        summary = summary.rstrip(".") + f" — {hous_n} housing."

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
