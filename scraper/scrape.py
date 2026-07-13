#!/usr/bin/env python3
"""
Indy Entitlement Tracker — scraper
-----------------------------------
Pulls agenda PDFs from the Indianapolis DMD meetings portal
(https://indianapolis-in.municodemeetings.com/DMDmeetings), extracts every
petition (case number, address, township, council district, zoning request,
petitioner, description), geocodes the address via the free US Census
Geocoder, and writes a cumulative GeoJSON file that the Leaflet map in
/docs consumes.

Covers all DMD boards published on that page:
  Metropolitan Development Commission (MDC), Hearing Examiner,
  Boards of Zoning Appeals I / II / III, Plat Committee,
  Regional Center / IHPC-adjacent hearings when they appear.

State files (committed to the repo so runs are incremental):
  docs/data/filings.geojson   — cumulative mapped petitions
  data/processed.json         — agenda URLs already ingested
  data/geocode_cache.json     — address -> lat/lon cache
  data/unmatched.json         — petitions we couldn't geocode (for review)

Usage:
  python scraper/scrape.py              # ingest any agendas not yet processed
  python scraper/scrape.py --backfill   # same, but walk extra listing pages
  python scraper/scrape.py --dry-run    # parse but write nothing
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup

from agenda_items import parse_mdc_items, merge_and_save, score_item

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
GEOJSON_PATH = ROOT / "docs" / "data" / "filings.geojson"
PROCESSED_PATH = DATA_DIR / "processed.json"
CACHE_PATH = DATA_DIR / "geocode_cache.json"
UNMATCHED_PATH = DATA_DIR / "unmatched.json"

PORTAL = "https://indianapolis-in.municodemeetings.com"
LISTING_URL = PORTAL + "/DMDmeetings"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# e.g. 2026-ZON-045, 2026-DV1-014, 2026-UV2-003, 2026-CZN-812, 2026-PLT-22
CASE_RE = re.compile(r"\b(20\d{2})-([A-Z]{2,4}\d?)-(\d{1,4}[A-Z]?)\b")

STREET_WORDS = (
    "STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|BOULEVARD|BLVD|LANE|LN|WAY|"
    "COURT|CT|CIRCLE|CIR|PLACE|PL|PIKE|PKWY|PARKWAY|TRAIL|TRL|TERRACE|TER|"
    "SQUARE|SQ|HIGHWAY|HWY|ROW|ALLEY"
)
ADDRESS_RE = re.compile(
    r"\b\d{1,6}(?:\s*[-–]\s*\d{1,6})?\s+(?:[NSEW]\.?\s+|NORTH\s+|SOUTH\s+|EAST\s+|WEST\s+)?"
    r"[A-Z0-9'\.\- ]{2,40}?\s(?:%s)\b" % STREET_WORDS,
    re.IGNORECASE,
)
TOWNSHIP_RE = re.compile(r"\b([A-Z]+)\s+TOWNSHIP\b", re.IGNORECASE)
DISTRICT_RE = re.compile(r"COUNCIL\s+DISTRICT[S]?\s*#?\s*(\d{1,2})", re.IGNORECASE)
REZONE_RE = re.compile(
    r"\b(?:rezon\w+\s+(?:of\s+)?.{0,80}?from\s+)?"
    r"([A-Z]{1,3}[U]?-?[A-Z0-9]{0,4})\s+(?:district\s+)?to\s+(?:the\s+)?"
    r"([A-Z]{1,3}[U]?-?[A-Z0-9]{0,4})\b"
)

# Map case prefixes to human-readable filing types for the map legend.
TYPE_MAP = [
    (re.compile(r"^ZON|^CZN|^CZ"), "Rezoning"),
    (re.compile(r"^DP"), "Development Plan"),
    (re.compile(r"^MOD"), "Modification"),
    (re.compile(r"^VAR|^DV|^UV|^SE|^V\d?$"), "Variance / Special Exception"),
    (re.compile(r"^APP|^AP"), "Approval / Appeal"),
    (re.compile(r"^PLT|^PLAT|^SUB"), "Plat / Subdivision"),
    (re.compile(r"^CVR|^CV|^CA"), "Commitment / Covenant"),
    (re.compile(r"^HOV"), "Hospital / Overlay"),
]


def classify(prefix: str) -> str:
    for rx, label in TYPE_MAP:
        if rx.match(prefix):
            return label
    return "Other"


def guess_board(title: str) -> str:
    t = title.upper()
    if "HEARING EXAMINER" in t:
        return "Hearing Examiner"
    if "METROPOLITAN DEVELOPMENT" in t or "MDC" in t:
        return "Metropolitan Development Commission"
    if "ZONING APPEALS" in t:
        for n, roman in ((" III", "III"), (" II", "II"), (" I", "I")):
            if n in t or f"DIVISION {roman}" in t or f"BOARD {roman}" in t:
                return f"BZA {roman}"
        return "BZA"
    # Post-migration Municode titles say just "Division II" without
    # "Zoning Appeals" — those are still the BZAs. (Check III before II
    # before I: "DIVISION II" is a substring of "DIVISION III".)
    if "DIVISION" in t:
        for roman in ("III", "II", "I"):
            if f"DIVISION {roman}" in t:
                return f"BZA {roman}"
    if "PLAT" in t:
        return "Plat Committee"
    if "REGIONAL CENTER" in t:
        return "Regional Center"
    return title.strip()[:60] or "DMD Board"


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1))


# ---------------------------------------------------------------- listing --
def discover_agendas(session, backfill=False):
    """Return list of dicts: {url, title, date} for every agenda PDF found."""
    pages = range(0, 6) if backfill else range(0, 2)
    found, seen = [], set()
    for p in pages:
        url = LISTING_URL if p == 0 else f"{LISTING_URL}?page={p}"
        try:
            html = session.get(url, headers=HEADERS, timeout=30).text
        except requests.RequestException as e:
            print(f"[warn] could not fetch listing {url}: {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("tr") or soup.find_all(
            "div", class_=re.compile("meeting|views-row", re.I)
        )
        containers = rows if rows else [soup]
        for row in containers:
            row_text = row.get_text(" ", strip=True)
            date = extract_date(row_text)
            for a in row.find_all("a", href=True):
                href = a["href"]
                label = (a.get_text(" ", strip=True) or "").lower()
                is_agenda_pdf = href.lower().endswith(".pdf") and (
                    "agenda" in href.lower() or "agenda" in label
                )
                is_blob = "mccmeetings" in href and "Agenda" in href
                if not (is_agenda_pdf or is_blob):
                    continue
                full = urljoin(PORTAL, href)
                if full in seen:
                    continue
                seen.add(full)
                found.append({
                    "url": full,
                    "title": row_text[:120],
                    "date": date,
                })
        if not backfill:
            break
    print(f"[info] discovered {len(found)} agenda PDF link(s)")
    return found


def extract_date(text: str):
    m = re.search(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2}",
        text,
    )
    if m:
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%b. %d, %Y"):
            try:
                return datetime.strptime(m.group(0).replace(".", ""), fmt.replace(".", "")).date().isoformat()
            except ValueError:
                continue
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", text)
    if m:
        mo, d, y = map(int, m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


# ---------------------------------------------------------------- parsing --
def pdf_text(session, url: str) -> str:
    r = session.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    out = []
    with pdfplumber.open(BytesIO(r.content)) as pdf:
        for page in pdf.pages:
            out.append(page.extract_text() or "")
    return "\n".join(out)


def parse_petitions(text: str):
    """Split agenda text into petition blocks keyed on case numbers."""
    matches = list(CASE_RE.finditer(text))
    petitions = {}
    for i, m in enumerate(matches):
        case = m.group(0)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(text), start + 2500)
        block = text[start:end]
        # The same case number can appear multiple times (index + body).
        # Keep the longest block per case — it's the one with the details.
        if case not in petitions or len(block) > len(petitions[case]):
            petitions[case] = block

    results = []
    for case, block in petitions.items():
        year, prefix, _num = CASE_RE.match(case).groups()
        addr = ADDRESS_RE.search(block.replace(case, " "))
        township = TOWNSHIP_RE.search(block)
        district = DISTRICT_RE.search(block)
        rez = REZONE_RE.search(block)
        # Description: first sentence-ish chunk after the header lines
        desc = " ".join(block.split())
        desc = desc[:600]
        results.append({
            "case": case,
            "year": int(year),
            "prefix": prefix,
            "type": classify(prefix),
            "address": clean_address(addr.group(0)) if addr else None,
            "township": township.group(1).title() if township else None,
            "council_district": int(district.group(1)) if district else None,
            "zoning_from": rez.group(1) if rez else None,
            "zoning_to": rez.group(2) if rez else None,
            "summary": desc,
        })
    return results


def clean_address(raw: str) -> str:
    a = re.sub(r"\(.*?\)", "", raw)           # drop "(approx.)"
    a = re.sub(r"\s*[-–]\s*\d+", "", a, 1)     # "1234-1240 X St" -> "1234 X St"
    a = re.sub(r"\s+", " ", a).strip(" ,.;")
    return a.upper()


# --------------------------------------------------------------- geocoding --
def geocode(session, address: str, cache: dict):
    key = address.upper()
    if key in cache:
        return cache[key]
    coords = census_geocode(session, address)
    if coords is None:
        coords = nominatim_geocode(session, address)
    cache[key] = coords  # cache misses too, so we don't re-ask every week
    return coords


def census_geocode(session, address: str):
    try:
        r = session.get(
            "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
            params={
                "address": f"{address}, Indianapolis, IN",
                "benchmark": "Public_AR_Current",
                "format": "json",
            },
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        matches = r.json().get("result", {}).get("addressMatches", [])
        if matches:
            c = matches[0]["coordinates"]
            return [round(c["x"], 6), round(c["y"], 6)]  # lon, lat
    except Exception as e:
        print(f"[warn] census geocode failed for {address}: {e}")
    return None


def nominatim_geocode(session, address: str):
    try:
        time.sleep(1.1)  # Nominatim usage policy: max 1 req/sec
        r = session.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{address}, Indianapolis, Indiana", "format": "json", "limit": 1},
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        hits = r.json()
        if hits:
            return [round(float(hits[0]["lon"]), 6), round(float(hits[0]["lat"]), 6)]
    except Exception as e:
        print(f"[warn] nominatim geocode failed for {address}: {e}")
    return None


# -------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="walk extra listing pages")
    ap.add_argument("--dry-run", action="store_true", help="parse but write nothing")
    args = ap.parse_args()

    session = requests.Session()
    processed = set(load_json(PROCESSED_PATH, []))
    cache = load_json(CACHE_PATH, {})
    geojson = load_json(GEOJSON_PATH, {"type": "FeatureCollection", "features": []})
    unmatched = load_json(UNMATCHED_PATH, [])
    existing_keys = {
        (f["properties"]["case"], f["properties"].get("agenda_url"))
        for f in geojson["features"]
    }

    # Repair board names on records saved before guess_board learned the
    # post-migration "Division II" titles. Idempotent; persists on save.
    for f in geojson["features"]:
        props = f["properties"]
        b = props.get("board") or ""
        if "VIEW DETAILS" in b.upper() or "DIVISION" in b.upper():
            props["board"] = guess_board(b)
        # Resolution features saved without a year get one from their case
        # number so the map's year filter doesn't silently hide them.
        if props.get("kind") == "resolution" and not props.get("year"):
            c = (props.get("case") or "")[:4]
            if c.isdigit():
                props["year"] = int(c)

    agendas = discover_agendas(session, backfill=args.backfill)
    new_agendas = [a for a in agendas if a["url"] not in processed]
    print(f"[info] {len(new_agendas)} agenda(s) not yet processed")

    added = 0
    mdc_items = []
    for agenda in new_agendas:
        print(f"[info] processing: {agenda['title'][:80]}")
        try:
            text = pdf_text(session, agenda["url"])
        except Exception as e:
            print(f"[warn] could not read PDF {agenda['url']}: {e}")
            continue
        board = guess_board(agenda["title"])
        if board == "Metropolitan Development Commission":
            for item in parse_mdc_items(text, board, agenda["date"], agenda["url"]):
                mdc_items.append(item)
                # Geocode incentive/resolution items so they render on the map
                # alongside petitions (type = priority tier for the legend).
                key = (item["case"], agenda["url"])
                coords = geocode(session, item["address"], cache) \
                    if item.get("address") else None
                if coords and key not in existing_keys:
                    _, tier = score_item(item)
                    geojson["features"].append({
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": coords},
                        "properties": {**item,
                                       "type": tier or "MDC Resolution",
                                       "year": int(item["case"][:4])
                                           if item["case"][:4].isdigit() else None,
                                       "ingested": datetime.now(timezone.utc).date().isoformat()},
                    })
                    existing_keys.add(key)
                    added += 1
        for pet in parse_petitions(text):
            key = (pet["case"], agenda["url"])
            if key in existing_keys:
                continue
            coords = geocode(session, pet["address"], cache) if pet["address"] else None
            record = {
                **pet,
                "board": board,
                "meeting_date": agenda["date"],
                "agenda_url": agenda["url"],
                "ingested": datetime.now(timezone.utc).date().isoformat(),
            }
            if coords:
                geojson["features"].append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": coords},
                    "properties": record,
                })
                existing_keys.add(key)
                added += 1
            else:
                unmatched.append(record)
        processed.add(agenda["url"])

    print(f"[done] {added} petition(s) added; "
          f"{len(geojson['features'])} total on map; "
          f"{len(unmatched)} unmatched")

    if args.dry_run:
        return

    geojson["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_json(GEOJSON_PATH, geojson)
    save_json(PROCESSED_PATH, sorted(processed))
    n_res = merge_and_save(
        mdc_items, datetime.now(timezone.utc).date().isoformat())
    print(f"[info] {n_res} MDC resolution item(s) added")
    save_json(CACHE_PATH, cache)
    save_json(UNMATCHED_PATH, unmatched)

    # Compact feed of the most recent filings for the site homepage.
    recent = sorted(geojson["features"],
                    key=lambda f: (f["properties"].get("ingested") or "",
                                   f["properties"].get("meeting_date") or ""),
                    reverse=True)[:10]
    save_json(ROOT / "docs" / "data" / "latest.json",
              [{k: f["properties"].get(k) for k in
                ("case", "type", "address", "meeting_date", "board")}
               for f in recent])


if __name__ == "__main__":
    sys.exit(main())
