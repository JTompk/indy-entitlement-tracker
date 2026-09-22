#!/usr/bin/env python3
"""
IndyIMBY digest — EMAIL edition (Buttondown).

Same data and same use-case sections as scraper/digest.py, rendered as
email-safe HTML (single column, inline styles, tables for the item rows,
web fonts replaced by Georgia / Helvetica / Courier). Email clients strip
scripts, iframes and inline SVG, so every visual is a hosted image:

  docs/img/digest/YYYY-MM-DD.jpg   live-map screenshot (Playwright), or
  docs/img/digest/YYYY-MM-DD.png   schematic county map (Pillow) as fallback
  docs/img/loc/<CASE>.png          44 px township-grid locator per filing

Both folders are served by GitHub Pages at https://map.indyimby.com/img/...

Usage (the pipeline runs all three on digest days, in this order):
  python scraper/email_digest.py --screenshot     # needs playwright + chromium
  python scraper/email_digest.py                  # locators + email html draft
  python scraper/email_digest.py --push           # Buttondown draft via API

Env for --push:
  BUTTONDOWN_API_KEY   required
  BUTTONDOWN_STATUS    draft (default) | about_to_send
"""

import argparse
import html as htmlmod
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
from agenda_items import score_item  # noqa: E402
import digest as dg  # noqa: E402  (shared helpers + calibration)

IMG_DIR = ROOT / "docs" / "img"
LOC_DIR = IMG_DIR / "loc"
HERO_DIR = IMG_DIR / "digest"
DRAFTS_DIR = ROOT / "digest_drafts"
SITE_URL = "https://indyimby.com"
MAP_URL = dg.MAP_URL
IMG_URL = MAP_URL + "/img"

# palette (hex only — email clients don't do CSS variables)
INK, PAPER, ACCENT, TINT = "#232320", "#FBFBF8", "#FAD980", "#FDF2D7"
SOFT, MUTED, HAIR = "#6E6E68", "#8A8A84", "#E3E2DB"
SERIF = "Georgia,'Times New Roman',serif"
SANS = "Helvetica,Arial,sans-serif"
MONO = "'Courier New',Courier,monospace"

CAT_COLOR = {          # dot colours on the schematic map
    "housing": "#2378C3", "commerce": "#E5A000", "industrial": "#232320",
    "community": "#3B8F5A", "incentives": "#FAD980", "historic": "#8B5E3C",
    "site": "#9A9A94", "other": "#9A9A94",
}


def esc(s):
    return htmlmod.escape(str(s if s is not None else ""), quote=True)


# ------------------------------------------------------------ selection --
# Mirrors digest.build(): same window, same de-dup, same "new" rule. Keep
# these two in step if the rules ever change.

def select(lookahead, lookback):
    now = datetime.now(timezone.utc).date()
    today = now.isoformat()
    horizon = (now + timedelta(days=lookahead)).isoformat()
    cutoff = (now - timedelta(days=lookback)).isoformat()
    scored = [(r, *score_item(r)) for r in dg.load_all()]

    def dedupe(rows):
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
    fresh = dedupe((r, s, t) for r, s, t in scored
                   if (r.get("ingested") or "") >= cutoff
                   and r.get("case") not in up_cases
                   and (not r.get("meeting_date") or r["meeting_date"] > horizon))
    fresh.sort(key=lambda x: x[1], reverse=True)
    cats = {r.get("case"): dg.categorize(r, t) for r, s, t in upcoming + fresh}
    return now, today, horizon, upcoming, fresh, cats


# --------------------------------------------------------------- images --

def _hex(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _grid_geometry(size):
    """Pixel x/y split lines for a size×size county square."""
    xs = [dg._frac(x, dg.LON_MIN, dg.LON_MAX) * size for x in dg.X_SPLITS]
    ys = [(1 - dg._frac(y, dg.LAT_MIN, dg.LAT_MAX)) * size for y in dg.Y_SPLITS]
    return xs, ys


def render_locator(lonlat, out_path, px=44, scale=4):
    """Township-grid locator as a PNG (drawn at 4× then downsampled so the
    dot and lines are anti-aliased). Mirrors digest.locator_svg()."""
    from PIL import Image, ImageDraw
    S = px * scale
    im = Image.new("RGB", (S, S), _hex(PAPER))
    d = ImageDraw.Draw(im)
    xs, ys = _grid_geometry(S)
    dim = lonlat is None
    if lonlat:
        lon, lat = lonlat
        cx = dg._frac(lon, dg.LON_MIN, dg.LON_MAX) * S
        cy = (1 - dg._frac(lat, dg.LAT_MIN, dg.LAT_MAX)) * S
        col = sum(1 for x in xs if cx > x)
        row = sum(1 for y in ys if cy > y)
        ex = [0] + xs + [S]
        ey = [0] + ys + [S]
        d.rectangle([ex[col], ey[row], ex[col + 1], ey[row + 1]], fill=(250, 232, 182))
    grid_c = (226, 225, 219) if dim else (201, 200, 192)
    border_c = (170, 170, 166) if dim else _hex(INK)
    for x in xs:
        d.line([(x, 0), (x, S)], fill=grid_c, width=scale)
    for y in ys:
        d.line([(0, y), (S, y)], fill=grid_c, width=scale)
    bw = max(1, int(1.1 * scale))
    d.rectangle([bw // 2, bw // 2, S - bw // 2 - 1, S - bw // 2 - 1], outline=border_c, width=bw)
    if lonlat:
        r = 3.2 * scale
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_hex(ACCENT),
                  outline=_hex(INK), width=int(1.2 * scale))
    im = im.resize((px * 2, px * 2), Image.LANCZOS)   # 2× for retina, shown at 44 px
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path, optimize=True)


def render_schematic(upcoming, cats, out_path, px=552, scale=2):
    """Fallback hero: county square with township names and category-
    coloured dots for every mappable item this week."""
    from PIL import Image, ImageDraw, ImageFont
    S = px * scale
    pad = int(6 * scale)
    im = Image.new("RGB", (S, S), _hex(PAPER))
    d = ImageDraw.Draw(im)
    inner = S - 2 * pad
    xs, ys = _grid_geometry(inner)
    xs = [pad + x for x in xs]
    ys = [pad + y for y in ys]
    for x in xs:
        d.line([(x, pad), (x, S - pad)], fill=(201, 200, 192), width=scale)
    for y in ys:
        d.line([(pad, y), (S - pad, y)], fill=(201, 200, 192), width=scale)
    d.rectangle([pad, pad, S - pad, S - pad], outline=_hex(INK), width=scale)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", int(11 * scale))
    except OSError:
        font = ImageFont.load_default()
    ex = [pad] + xs + [S - pad]
    ey = [pad] + ys + [S - pad]
    for r in range(3):
        for c in range(3):
            d.text((ex[c] + 8 * scale, ey[r] + 6 * scale), dg.GRID_TWP[r][c].upper(),
                   fill=(160, 160, 154), font=font)
    # dots: low-priority categories first so housing/incentives sit on top
    order = ["other", "site", "historic", "community", "industrial", "commerce", "housing", "incentives"]
    rows = sorted(upcoming, key=lambda x: order.index(cats.get(x[0].get("case"), "other")))
    seen = Counter()
    for r, s, t in rows:
        ll = r.get("_lonlat")
        if not ll:
            continue
        cat = cats.get(r.get("case"), "other")
        cx = pad + dg._frac(ll[0], dg.LON_MIN, dg.LON_MAX) * inner
        cy = pad + (1 - dg._frac(ll[1], dg.LAT_MIN, dg.LAT_MAX)) * inner
        k = (round(cx / (6 * scale)), round(cy / (6 * scale)))   # fan out stacked sites
        n = seen[k]; seen[k] += 1
        cx += 7 * scale * n
        rad = (7 if cat == "incentives" else 5.5) * scale
        d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=_hex(CAT_COLOR[cat]),
                  outline=_hex(INK) if cat == "incentives" else _hex(PAPER), width=int(1.4 * scale))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path, optimize=True)


def screenshot_map(today, out_path, lookahead):
    """Screenshot the live docket map (served from docs/ locally so the
    data is this run's, basemap fetched from map.indyimby.com)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[email] playwright not installed — skipping screenshot")
        return False
    port = 8765
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(port),
                            "--directory", str(ROOT / "docs"), "--bind", "127.0.0.1"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        url = f"http://127.0.0.1:{port}/index.html?week={today}&embed=1"
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1104, "height": 640}, device_scale_factor=2)
            pg.goto(url, wait_until="networkidle", timeout=90_000)
            # markers exist only if Leaflet + the data actually loaded; if they
            # never appear, bail so the schematic fallback is used instead
            pg.wait_for_selector(".leaflet-interactive", timeout=30_000)
            pg.wait_for_timeout(6000)          # let vector tiles finish painting
            out_path.parent.mkdir(parents=True, exist_ok=True)
            pg.screenshot(path=str(out_path), type="jpeg", quality=82)
            b.close()
        print(f"[email] screenshot -> {out_path}")
        return True
    except Exception as e:      # noqa: BLE001 — never fail the pipeline over a picture
        print(f"[email] screenshot failed: {e}")
        return False
    finally:
        srv.terminate()


# --------------------------------------------------------------- html --

def item_html(r, tier, cat, today, week_link=True, show_tag=False):
    case = r.get("case") or ""
    addr = dg.item_addr(r)
    meta = [x for x in [
        dg.short_meeting_date(r.get("meeting_date")) if r.get("meeting_date") else None,
        r.get("board") or None,
        f"{r['township']} Twp" if r.get("township") in dg.KNOWN_TOWNSHIPS else None,
        f"Council #{r['council_district']}" if r.get("council_district") else None,
    ] if x]
    head = f'<span style="font-weight:700">{esc(case)}</span>'
    if addr:
        head += f" &mdash; {esc(addr)}"
    if r.get("zoning_from") and r.get("zoning_to"):
        head += (f' <span style="font-family:{MONO};font-size:12px;font-weight:400;'
                 f'background:{TINT};padding:1px 5px;white-space:nowrap">'
                 f'{esc(r["zoning_from"])} &rarr; {esc(r["zoning_to"])}</span>')
    if show_tag:
        head += (f' <span style="font-family:{SANS};font-size:10px;font-weight:700;'
                 f'letter-spacing:.14em;text-transform:uppercase;background:{ACCENT};'
                 f'padding:2px 6px;white-space:nowrap">{esc(dg.CAT_LABEL[cat])}</span>')
    plain = dg.plain_summary(r, tier)
    if not plain:
        raw = (r.get("summary") or "").strip()
        plain = (raw[:200].rstrip() + "…") if raw else ""
    link_style = (f'style="color:{INK};text-decoration:none;border-bottom:3px solid {ACCENT};'
                  f'padding-bottom:1px"')
    links = []
    if r.get("_lonlat") and case:
        q = f"case={case}" + (f"&week={today}" if week_link else "")
        links.append(f'<a href="{MAP_URL}/?{q}" {link_style}>See it on the map</a>')
    if r.get("agenda_url"):
        links.append(f'<a href="{esc(r["agenda_url"])}" {link_style}>Agenda (PDF)</a>')
    loc_src = f"{IMG_URL}/loc/{case}.png"
    loc_alt = "Map locator"
    ll = r.get("_lonlat")
    if ll:
        xs = [dg._frac(x, dg.LON_MIN, dg.LON_MAX) for x in dg.X_SPLITS]
        ys = [1 - dg._frac(y, dg.LAT_MIN, dg.LAT_MAX) for y in dg.Y_SPLITS]
        px = dg._frac(ll[0], dg.LON_MIN, dg.LON_MAX)
        py = 1 - dg._frac(ll[1], dg.LAT_MIN, dg.LAT_MAX)
        col = sum(1 for x in xs if px > x)
        row = sum(1 for y in ys if py > y)
        loc_alt = f"{dg.SIDE_NAME[row][col].capitalize()} — {dg.GRID_TWP[row][col]} Township"
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-top:1px solid {HAIR};border-collapse:collapse"><tr>'
        f'<td width="44" valign="top" style="padding:14px 12px 14px 0;width:44px">'
        f'<img src="{loc_src}" width="44" height="44" alt="{esc(loc_alt)}" '
        f'style="display:block;width:44px;height:44px;border:0"></td>'
        f'<td valign="top" style="padding:13px 0 14px">'
        + (f'<div style="font-family:{MONO};font-size:11px;line-height:1.4;color:{MUTED};'
           f'letter-spacing:.03em">{esc(" · ".join(meta))}</div>' if meta else "")
        + f'<div style="font-family:{SANS};font-size:15px;line-height:1.35;color:{INK};'
          f'margin-top:3px">{head}</div>'
        + (f'<div style="font-family:{SERIF};font-size:15px;line-height:1.5;color:{INK};'
           f'margin-top:5px">{esc(plain)}</div>' if plain else "")
        + (f'<div style="font-family:{SANS};font-size:12px;font-weight:700;letter-spacing:.02em;'
           f'margin-top:8px">{" &nbsp;·&nbsp; ".join(links)}</div>' if links else "")
        + "</td></tr></table>")


def section_h2(label, count, anchor):
    return (f'<h2 id="{anchor}" style="font-family:{SERIF};font-size:23px;font-weight:500;'
            f'line-height:1.2;color:{INK};margin:34px 0 4px;letter-spacing:-.01em">'
            f'<span style="background:{ACCENT};padding:0 6px 2px">{esc(label)}</span>'
            f'<span style="font-family:{MONO};font-size:13px;color:{SOFT};margin-left:8px">'
            f'{count}</span></h2>')


def build_email(lookahead, lookback, hero_url=None, hero_kind="live"):
    now, today, horizon, upcoming, fresh, cats = select(lookahead, lookback)
    subject = f"This week in Indy entitlement — {dg.nice_date(now)}"
    post_url = f"{SITE_URL}/digest/{today}-this-week/"
    n_up, n_new = len(upcoming), len(fresh)

    L = []
    L.append(f'<div style="background:{PAPER};color:{INK};margin:0;padding:0">')
    L.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
             f'style="border-collapse:collapse;background:{PAPER}"><tr><td align="center">')
    L.append('<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
             'style="width:600px;max-width:100%;border-collapse:collapse">')

    # masthead
    L.append(f'<tr><td style="padding:26px 24px 0"><table role="presentation" width="100%" '
             f'cellpadding="0" cellspacing="0" style="border-collapse:collapse"><tr>'
             f'<td style="font-family:{SANS};font-size:17px;font-weight:700;letter-spacing:.12em;'
             f'color:{INK}">INDY<span style="background:{ACCENT};padding:1px 5px;margin-left:3px">'
             f'IMBY</span></td>'
             f'<td align="right" style="font-family:{MONO};font-size:11px;letter-spacing:.08em;'
             f'color:{SOFT};text-transform:uppercase">Monday digest &middot; '
             f'{esc(dg.nice_date(now))}</td></tr></table></td></tr>')
    L.append(f'<tr><td style="padding:22px 24px 0;font-family:{SERIF};font-size:30px;'
             f'line-height:1.15;color:{INK};letter-spacing:-.01em">This week in Indy entitlement</td></tr>')

    if not upcoming:
        L.append(f'<tr><td style="padding:14px 24px 0;font-family:{SERIF};font-size:17px;'
                 f'line-height:1.6">No DMD hearings on the calendar in the next {lookahead} days. '
                 f'Quiet stretches happen &mdash; <a href="{MAP_URL}" style="color:{INK}">the map</a> '
                 f'stays live in the meantime.</td></tr>')
    else:
        top_r, top_s, top_t = max(upcoming, key=lambda x: x[1])
        n_days = len({r.get("meeting_date") for r, s, t in upcoming})
        lede = (f"{n_up} item{'s' if n_up != 1 else ''} on {n_days} hearing day"
                f"{'s' if n_days != 1 else ''} through {dg.nice_meeting_date(horizon)}, "
                f"sorted by what would get built.")
        if top_t:
            lede += f" The headline: a {top_t.lower()} item at the {top_r.get('board', 'MDC')}."
        L.append(f'<tr><td style="padding:14px 24px 0;font-family:{SERIF};font-size:17px;'
                 f'line-height:1.6;color:{INK}">{esc(lede)}</td></tr>')

        # at a glance
        bullets = dg.glance_bullets(upcoming, fresh, cats, horizon, (top_r, top_s, top_t))
        if bullets:
            rows = "".join(
                f'<tr><td width="14" valign="top" style="padding:7px 0 7px;width:14px">'
                f'<div style="width:8px;height:8px;background:{ACCENT};border:1px solid {INK};'
                f'margin-top:6px"></div></td>'
                f'<td style="padding:7px 0 7px 6px;font-family:{SANS};font-size:14.5px;'
                f'line-height:1.5;color:{INK};border-top:1px solid {HAIR}">{b}</td></tr>'
                for b in bullets).replace(f'border-top:1px solid {HAIR}', "", 1)
            L.append(f'<tr><td style="padding:20px 24px 0"><table role="presentation" width="100%" '
                     f'cellpadding="0" cellspacing="0" style="border-collapse:collapse;'
                     f'background:{TINT};border:1px solid {INK}"><tr><td style="padding:14px 18px 10px">'
                     f'<div style="font-family:{SANS};font-size:11px;font-weight:700;letter-spacing:.2em;'
                     f'text-transform:uppercase;color:{SOFT};margin-bottom:4px">The week at a glance</div>'
                     f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                     f'style="border-collapse:collapse">{rows}</table></td></tr></table></td></tr>')

        # hero map
        if hero_url:
            if hero_kind == "live":
                cap = (f"Every dot is a hearing in the next {lookahead} days — tap the map "
                       f"to explore it live.")
            else:
                legend = " &nbsp;".join(
                    f'<span style="color:{CAT_COLOR[cid]}">&#9679;</span> {esc(short)}'
                    for cid, short in [("housing", "Housing"), ("commerce", "Shops & offices"),
                                       ("industrial", "Industrial"), ("community", "Community"),
                                       ("incentives", "Tax incentives"), ("site", "Site tweaks")]
                    if any(cats.get(r.get("case")) == cid for r, s, t in upcoming))
                cap = "Where this week's hearings fall across the county. " + legend + "."
            L.append(f'<tr><td style="padding:22px 24px 0"><a href="{MAP_URL}/?week={today}">'
                     f'<img src="{hero_url}" width="552" alt="This week\'s docket map" '
                     f'style="display:block;width:100%;max-width:552px;height:auto;'
                     f'border:1px solid {INK}"></a>'
                     f'<div style="font-family:{SANS};font-size:12px;line-height:1.5;color:{SOFT};'
                     f'margin-top:8px">{cap if hero_kind != "live" else esc(cap)} <a href="{MAP_URL}/?week={today}" '
                     f'style="color:{INK}">Open the docket map &rarr;</a></div></td></tr>')
        else:
            L.append(f'<tr><td style="padding:22px 24px 0"><a href="{MAP_URL}/?week={today}" '
                     f'style="display:block;font-family:{SANS};font-size:13px;font-weight:700;'
                     f'letter-spacing:.06em;text-transform:uppercase;color:{INK};text-decoration:none;'
                     f'border:1px solid {INK};padding:14px;text-align:center">Open this week\'s docket map &rarr;</a></td></tr>')

        # jump line
        counts = Counter(cats[r.get("case")] for r, s, t in upcoming)
        jumps = [f'<a href="{post_url}#{cid}" style="color:{INK};text-decoration:none;'
                 f'border-bottom:2px solid {ACCENT}">{esc(label)}</a>'
                 f'<span style="font-family:{MONO};color:{SOFT}"> {counts[cid]}</span>'
                 for cid, label, _ in dg.CATEGORIES if counts.get(cid)]
        L.append(f'<tr><td style="padding:26px 24px 0;font-family:{SANS};font-size:13px;'
                 f'line-height:2;color:{INK}"><span style="font-weight:700;letter-spacing:.14em;'
                 f'text-transform:uppercase;font-size:11px;color:{SOFT}">On the docket</span>&nbsp;&nbsp; '
                 + " &nbsp;·&nbsp; ".join(jumps) + "</td></tr>")

        # sections
        by_cat = defaultdict(list)
        for r, s, t in upcoming:
            by_cat[cats[r.get("case")]].append((r, s, t))
        body = []
        for cid, label, desc in dg.CATEGORIES:
            items = by_cat.get(cid)
            if not items:
                continue
            items.sort(key=lambda x: (-x[1], x[0].get("meeting_date") or ""))
            body.append(section_h2(label, len(items), cid))
            body.append(f'<div style="font-family:{SANS};font-size:13px;line-height:1.5;color:{SOFT};'
                        f'margin:0 0 10px">{esc(desc)}</div>')
            for r, s, t in items[:dg.MAX_PER_SECTION]:
                body.append(item_html(r, t, cid, today))
            if len(items) > dg.MAX_PER_SECTION:
                more = len(items) - dg.MAX_PER_SECTION
                body.append(f'<div style="font-family:{SANS};font-size:13px;color:{SOFT};'
                            f'padding:10px 0 0 56px">…plus {more} more {esc(label.lower())} '
                            f'item{"s" if more != 1 else ""} on the '
                            f'<a href="{post_url}#{cid}" style="color:{INK}">web edition</a>.</div>')
        L.append('<tr><td style="padding:0 24px">' + "".join(body) + "</td></tr>")

        # testify CTA
        L.append(f'<tr><td style="padding:30px 24px 0"><table role="presentation" width="100%" '
                 f'cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:{ACCENT}">'
                 f'<tr><td style="padding:18px 20px;font-family:{SERIF};font-size:17px;line-height:1.45;'
                 f'color:{INK}">One of these near you? Neighbors who show up decide these votes. '
                 f'<a href="{SITE_URL}/how-to-testify/" style="color:{INK};font-weight:700">'
                 f'Here\'s how to testify &rarr;</a></td></tr></table></td></tr>')

    # new filings
    if fresh:
        body = [section_h2("New filings worth watching", len(fresh), "new"),
                f'<div style="font-family:{SANS};font-size:13px;line-height:1.5;color:{SOFT};'
                f'margin:0 0 10px">Fresh on the docket, with hearings beyond this week\'s window.</div>']
        for r, s, t in fresh[:dg.MAX_NEW]:
            body.append(item_html(r, t, cats[r.get("case")], today, week_link=False, show_tag=True))
        if len(fresh) > dg.MAX_NEW:
            more = len(fresh) - dg.MAX_NEW
            body.append(f'<div style="font-family:{SANS};font-size:13px;color:{SOFT};padding:10px 0 0 56px">'
                        f'…and {more} more new filing{"s" if more != 1 else ""} — all mapped on the '
                        f'<a href="{MAP_URL}" style="color:{INK}">tracker</a>.</div>')
        L.append('<tr><td style="padding:0 24px">' + "".join(body) + "</td></tr>")

    # footer
    types = Counter((t or r.get("type") or "Other") for r, s, t in upcoming + fresh)
    type_line = ", ".join(f"{t.lower()} ({n})" for t, n in types.most_common(5))
    L.append(f'<tr><td style="padding:34px 24px 30px;border-top:1px solid {INK};font-family:{SANS};'
             f'font-size:12.5px;line-height:1.6;color:{SOFT}">'
             f'<b style="color:{INK}">This week by the numbers:</b> {n_up} item{"s" if n_up != 1 else ""} '
             f'on upcoming agendas, {n_new} new filing{"s" if n_new != 1 else ""} since last Monday'
             + (f" ({esc(type_line)})" if type_line else "") + ". "
             f'Every mappable filing is on the <a href="{MAP_URL}" style="color:{INK}">Entitlement Tracker</a>, '
             f'compiled from public DMD agendas. Read the <a href="{post_url}" style="color:{INK}">web edition</a> '
             f'to filter by category, or <a href="{MAP_URL}/build" style="color:{INK}">check what you can build '
             f'on your own lot</a>. See something we got wrong? Reply and tell us.</td></tr>')
    L.append("</table></td></tr></table></div>")
    return subject, "\n".join(L), today, upcoming, fresh, cats


# ---------------------------------------------------------- buttondown --

def push_buttondown(subject, body):
    import requests
    key = os.environ.get("BUTTONDOWN_API_KEY", "").strip()
    if not key:
        print("[email] BUTTONDOWN_API_KEY not set — skipping push")
        return False
    status = os.environ.get("BUTTONDOWN_STATUS", "draft").strip() or "draft"
    H = {"Authorization": f"Token {key}", "X-API-Version": "2026-04-01"}
    # don't stack duplicate drafts if the pipeline is re-run
    try:
        r = requests.get("https://api.buttondown.com/v1/emails",
                         params={"status": "draft", "page_size": 50}, headers=H, timeout=30)
        if r.ok and any(e.get("subject") == subject for e in r.json().get("results", [])):
            print(f"[email] draft already exists in Buttondown: {subject!r} — not creating another")
            return True
    except Exception as e:      # noqa: BLE001
        print(f"[email] draft check skipped: {e}")
    if status == "about_to_send":
        H["X-Buttondown-Live-Dangerously"] = "true"
    r = requests.post("https://api.buttondown.com/v1/emails", headers=H, timeout=60,
                      json={"subject": subject, "body": body, "status": status})
    if r.status_code >= 300:
        print(f"[email] Buttondown {r.status_code}: {r.text[:300]}")
        return False
    print(f"[email] Buttondown {status} created: {r.json().get('id')}")
    return True


# ---------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookahead", type=int, default=dg.LOOKAHEAD_DAYS)
    ap.add_argument("--lookback", type=int, default=dg.LOOKBACK_DAYS)
    ap.add_argument("--screenshot", action="store_true", help="screenshot the live map and exit")
    ap.add_argument("--push", action="store_true", help="create the Buttondown email and exit")
    ap.add_argument("--out", type=str, default=None)
    a = ap.parse_args()

    today = datetime.now(timezone.utc).date().isoformat()
    hero_jpg = HERO_DIR / f"{today}.jpg"
    hero_png = HERO_DIR / f"{today}.png"

    if a.screenshot:
        screenshot_map(today, hero_jpg, a.lookahead)
        return

    draft = Path(a.out) if a.out else DRAFTS_DIR / f"{today}-email.html"

    if a.push:
        if not draft.exists():
            print(f"[email] no draft at {draft} — run without --push first")
            sys.exit(1)
        subject, body = draft.read_text(encoding="utf-8").split("\n", 1)
        push_buttondown(subject.removeprefix("Subject: ").strip(), body)
        return

    # 1. hero: live screenshot if this run made one, else a schematic
    now, _, _, upcoming, fresh, cats = select(a.lookahead, a.lookback)
    if hero_jpg.exists():
        hero_url, kind = f"{IMG_URL}/digest/{today}.jpg", "live"
    elif upcoming:
        render_schematic(upcoming, cats, hero_png)
        hero_url, kind = f"{IMG_URL}/digest/{today}.png", "schematic"
    else:
        hero_url, kind = None, None

    # 2. locators for every item in the email
    for r, s, t in upcoming + fresh[:dg.MAX_NEW]:
        if r.get("case"):
            render_locator(r.get("_lonlat"), LOC_DIR / f"{r['case']}.png")

    # 3. the email itself (first line = subject, rest = body)
    subject, body, _, _, _, _ = build_email(a.lookahead, a.lookback, hero_url, kind)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(f"Subject: {subject}\n{body}\n", encoding="utf-8")
    print(f"[done] email draft -> {draft}  (hero: {kind or 'none'}, "
          f"{len(upcoming)} docket + {min(len(fresh), dg.MAX_NEW)} new)")


if __name__ == "__main__":
    main()
