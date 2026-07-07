#!/usr/bin/env python3
"""
Zoning Gap map builder — Indianapolis / Marion County.

Downloads zoning-district polygons and Land Use Plan typology polygons from
MapIndy, intersects them, classifies every piece via the crosswalk matrix
(scraper/crosswalk.csv), and writes:

  docs/data/gap.geojson      — web-ready classified polygons
  docs/data/gap_stats.json   — acreage totals for the stats panel
  data/gap_unmatched_values.json — zoning/typology values the crosswalk
                                   didn't recognize (calibration material)

Usage:
  python scraper/gap_map.py --inspect     # print field names + sample values
  python scraper/gap_map.py               # full build
  python scraper/gap_map.py --min-acres 0.5 --simplify 15

First run is diagnostic by design: check gap_unmatched_values.json and the
console report, extend the normalizers below if needed, rerun.

Requires: pip install geopandas requests
"""

import argparse
import csv
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CROSSWALK = ROOT / "scraper" / "crosswalk.csv"
OUT_GEOJSON = ROOT / "docs" / "data" / "gap.geojson"
OUT_STATS = ROOT / "docs" / "data" / "gap_stats.json"
OUT_UNMATCHED = ROOT / "data" / "gap_unmatched_values.json"

ZONING_URL = "https://gis.indy.gov/server/rest/services/MapIndy/Zoning/MapServer/6"
LUP_URL = "https://gis.indy.gov/server/rest/services/DMDPortal/LandUsePlanBase/MapServer/0"

HEADERS = {"User-Agent": "IndyEntitlementTracker/1.0 (civic mapping; IndyIMBY)"}
PAGE = 1000

# Known field names for the confirmed layers (auto-detect is the fallback)
ZONING_FIELD = "LABEL"
TYPOLOGY_FIELD = "USEDESC"

# Candidate attribute names for auto-detection; extend if --inspect shows others
ZONING_FIELD_CANDIDATES = ["ZONING", "ZONE_CLASS", "ZONECLASS", "ZONE", "CLASS",
                           "DISTRICT", "ZONING_1", "LABEL", "ZONING_TYPE"]
TYPOLOGY_FIELD_CANDIDATES = ["TYPOLOGY", "TYPE", "LU_TYP", "LANDUSE", "LU_PLAN",
                             "CATEGORY", "PLAN_TYP", "LABEL", "NAME", "DESCRIPTION"]


# ------------------------------------------------------------- REST helpers
def layer_fields(url):
    r = requests.get(url, params={"f": "json"}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return [f["name"] for f in r.json().get("fields", [])]


def fetch_layer(url, out_fields="*"):
    feats, offset = [], 0
    while True:
        r = requests.get(url + "/query", params={
            "where": "1=1", "outFields": out_fields, "outSR": 4326,
            "f": "geojson", "resultOffset": offset, "resultRecordCount": PAGE,
        }, headers=HEADERS, timeout=120)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        batch = data.get("features", [])
        feats.extend(batch)
        print(f"    {len(feats)} features…")
        if len(batch) < PAGE:
            return feats
        offset += PAGE
        time.sleep(0.4)


def sample_values(feats, field, n=12):
    vals = Counter(str(f["properties"].get(field, "")).strip()
                   for f in feats[:3000])
    return [v for v, _ in vals.most_common(n) if v]


def detect_field(feats, candidates, validator):
    """Pick the attribute whose values look like what we expect."""
    present = feats[0]["properties"].keys() if feats else []
    for c in candidates:
        for p in present:
            if p.upper() == c:
                vals = sample_values(feats, p)
                if vals and sum(validator(v) for v in vals) >= len(vals) * 0.5:
                    return p
    # fallback: any string field passing the validator
    for p in present:
        vals = sample_values(feats, p)
        if vals and sum(validator(v) for v in vals) >= len(vals) * 0.7:
            return p
    return None


# --------------------------------------------------------------- normalizers
def load_crosswalk():
    rows = list(csv.DictReader(CROSSWALK.open()))
    districts = [r["district"] for r in rows]
    typologies = [c for c in rows[0] if c not in
                  ("district", "use_family", "intensity_rank", "walkable")]
    matrix = {(r["district"], t): r[t] for r in rows for t in typologies}
    walkable = {r["district"] for r in rows if r["walkable"] == "yes"}
    return districts, typologies, matrix, walkable


def norm_district(raw, districts):
    """'d-5 (ff)' -> 'D-5'; 'C3' -> 'C-3'; SU-anything -> 'SU-*'."""
    v = str(raw).upper().strip().split("(")[0].strip().rstrip(".")
    v = v.replace(" ", "")
    if v.startswith("SU"):
        return "SU-*"
    if v.startswith("SZ"):
        return "SZ-*"
    if v.startswith("HP"):
        return "HP-*"
    # try exact, then hyphen-insertion (C3 -> C-3), longest match first
    cands = sorted(districts, key=len, reverse=True)
    for d in cands:
        if v == d.replace(" ", ""):
            return d
    for d in cands:
        if v == d.replace("-", ""):
            return d
    for d in cands:
        if v.startswith(d) or v.startswith(d.replace("-", "")):
            return d
    return None



def _tkey(v):
    return " ".join(str(v).upper().replace("-", " ").replace("/", " / ").split())

LEGACY_ALIASES = {}
def _reg(canon, *variants):
    for v in variants:
        LEGACY_ALIASES[_tkey(v)] = canon
_reg("Legacy: 0-1.75 du/ac", "0 - 1.75 Residential Units per Acre", "0-1.75 Dwelling Units/Acre",
     "Dwellings Less Than 1.75 Units per Acre")
_reg("Legacy: 1.75-3.5 du/ac", "1.75 - 3.5 Residential Units per Acre", "1.75-3.5 Dwelling Units/Acre")
_reg("Legacy: 3.5-5 du/ac", "3.5 - 5 Residential Units per Acre", "3.5-5 Dwelling Units/Acre",
     "Dwellings 3.5 - 5 Units per Acre")
_reg("Legacy: 5-8 du/ac", "5 - 8 Residential Units per Acre", "Dwellings 5 - 8 Units per Acre")
_reg("Legacy: 8-15 du/ac", "8 - 15 Residential Units per Acre", "8-15 Dwelling Units/Acre",
     "Dwellings 8 - 15 Units per Acre", "Residential 6 - 15 Dwelling Units per Acre")
_reg("Legacy: 15-26 du/ac", "Over 15 Residential Units per Acre",
     "Residential 16 - 26 Dwelling Units per Acre")
_reg("Legacy: 27-49 du/ac", "Residential 27-49 Dwelling Units per Acre")
_reg("Legacy: 50+ du/ac", "Residential 50+ Dwelling Units per Acre")
_reg("Legacy: Estate Residential", "Estate Residential")
_reg("Legacy: Single-Family", "Single Family Residential", "Single-Family Residential")
_reg("Legacy: Multi-Family", "Multi-Family Residential")
_reg("Legacy: Agricultural Preservation", "Agricultural Preservation")
_reg("Legacy: Office", "Commercial Office", "Non-Core Office")
_reg("Legacy: Commercial", "Commercial Retail and Service", "Commercial", "Non-Core Commercial")
_reg("Legacy: Auto Commercial", "Auto Related Commercial")
_reg("Legacy: Industrial", "General Industrial", "Industrial")
_reg("Legacy: Research/Technology", "Research and Technology")
_reg("Legacy: Institutional", "Institutional", "Public and Semi-Public")
_reg("Legacy: Plan-specifies D-4", "D4 Zoning")
_reg("Legacy: Plan-specifies D-5", "D5 Zoning")
_reg("Legacy: Plan-specifies D-6", "D6 Zoning")
_reg("Legacy: Plan-specifies D-8", "D8 Zoning")
_reg("Legacy: Plan-specifies C-1", "C1 Zoning")
_reg("Legacy: Plan-specifies C-2", "C2 Zoning")
_reg("Legacy: Plan-specifies MU-2", "C3C Zoning (Now MU2)", "C3C (Now MU2) or D5 Zoning",
     "C3C (Now MU2) or D8 Zoning")

# Deliberately unclassified -> ctx via "__CTX__" (parks, water, special, ambiguous)
CTX_VALUES = {_tkey(v) for v in [
  "Floodway", "Bodies of Water", "Park", "Parks and Open Space", "Park and Open Space",
  "Open Space", "Large-Scale Park", "Linear Park", "Cemetery",
  "Special Use", "Special Use - Church", "Regional Special Use",
  "Urban Conservation", "Airport Related Mixed Use", "Interchange Area Mixed-Use",
  "Medium-Density Mixed-Use", "Mixed Use", "Mixed-Use",
  "Planned Development, Primarily Residential", "Mobile Home Park",
  "DP Zoning", "SU1 Zoning", "SU2 Zoning", "SU9 Zoning", "SU10 Zoning", "SU37 Zoning",
  "PK1 Zoning", "PK1 Zoning (Linear Park)", "D5 or PK1 Zoning",
  "Speedway N Crawfordsville Commercial District SZ-5",
  "Speedway S Crawfordsville Commercial District SZ-4",
  "Speedway Neighborhood Commercial District SZ-6",
  "Speedway Regional Commercial District SZ-3",
]}

def norm_typology(raw, typologies):
    k = _tkey(raw)
    if k in LEGACY_ALIASES:
        return LEGACY_ALIASES[k]
    if k in CTX_VALUES:
        return "__CTX__"
    v = str(raw).upper().strip().replace("-", " ")
    for t in typologies:
        if v == t.upper().replace("-", " "):
            return t
    # keyword containment: 'CITY NEIGHBORHOOD TYPOLOGY' -> 'City Neighborhood'
    for t in sorted(typologies, key=len, reverse=True):
        key = t.upper().replace("-", " ")
        if key in v:
            return t
    # common aliases
    alias = {
        "RURAL OR ESTATE": "Rural/Estate Neighborhood",
        "ESTATE NEIGHBORHOOD": "Rural/Estate Neighborhood",
        "INSTITUTION ORIENTED": "Institution-Oriented MU/Campus",
        "INSTITUTION-ORIENTED": "Institution-Oriented MU/Campus",
        "OFFICE/INDUSTRIAL": "Office/Industrial Mixed-Use",
    }
    for k, t in alias.items():
        if k in v:
            return t
    return None


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="re-download layers, ignore cache")
    ap.add_argument("--overlays", action="store_true", help="also build overlays.geojson")
    ap.add_argument("--min-acres", type=float, default=0.25,
                    help="drop intersection slivers below this size")
    ap.add_argument("--simplify", type=float, default=10.0,
                    help="simplify tolerance in meters")
    args = ap.parse_args()

    if args.overlays and not args.inspect:
        build_overlays()

    if args.inspect:
        for name, url in (("ZONING (layer 6)", ZONING_URL),
                          ("LAND USE PLAN (layer 23)", LUP_URL)):
            print(f"\n=== {name} ===")
            print("fields:", layer_fields(url))
            print("  fetching a sample for values…")
            r = requests.get(url + "/query", params={
                "where": "1=1", "outFields": "*", "outSR": 4326,
                "f": "geojson", "resultRecordCount": 200,
            }, headers=HEADERS, timeout=60).json()
            feats = r.get("features", [])
            for fld in (feats[0]["properties"].keys() if feats else []):
                vals = sample_values(feats, fld, 6)
                if vals:
                    print(f"  {fld}: {vals}")
        return

    import geopandas as gpd  # deferred so --inspect works without it

    districts, typologies, matrix, walkable = load_crosswalk()

    def cached(name, url):
        cache = ROOT / "data" / f"gap_cache_{name}.json"
        if cache.exists() and not args.refresh:
            print(f"[cache] using {cache.name} (pass --refresh to re-download)")
            return json.loads(cache.read_text())
        feats = fetch_layer(url)
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps(feats))
        return feats

    print("[1/6] zoning polygons…")
    zfeats = cached("zoning", ZONING_URL)
    print("[2/6] Land Use Plan polygons…")
    lfeats = cached("lup", LUP_URL)

    zfield = ZONING_FIELD or detect_field(zfeats, ZONING_FIELD_CANDIDATES,
                          lambda v: norm_district(v, districts) is not None)
    tfield = TYPOLOGY_FIELD or detect_field(lfeats, TYPOLOGY_FIELD_CANDIDATES,
                          lambda v: norm_typology(v, typologies) is not None)
    if not zfield or not tfield:
        print(f"[stop] could not auto-detect fields (zoning={zfield}, "
              f"typology={tfield}). Run --inspect and extend the candidate "
              f"lists or normalizers.")
        return
    print(f"[info] zoning field: {zfield} | typology field: {tfield}")

    zgdf = gpd.GeoDataFrame.from_features(zfeats, crs=4326)[[zfield, "geometry"]]
    lgdf = gpd.GeoDataFrame.from_features(lfeats, crs=4326)[[tfield, "geometry"]]

    unmatched = defaultdict(Counter)
    zgdf["district"] = zgdf[zfield].map(lambda v: norm_district(v, districts))
    for v in zgdf.loc[zgdf["district"].isna(), zfield]:
        unmatched["zoning_values"][str(v)] += 1
    lgdf["typology"] = lgdf[tfield].map(lambda v: norm_typology(v, typologies))
    for v in lgdf.loc[lgdf["typology"].isna(), tfield]:
        unmatched["typology_values"][str(v)] += 1
    zgdf, lgdf = zgdf.dropna(subset=["district"]), lgdf.dropna(subset=["typology"])

    print("[3/6] repairing geometries…")
    # project to Indiana East (ft) for valid area math, repair invalids
    zgdf = zgdf.to_crs(2965); lgdf = lgdf.to_crs(2965)
    zgdf["geometry"] = zgdf.buffer(0); lgdf["geometry"] = lgdf.buffer(0)

    print("[4/6] intersecting (this is the slow step — minutes, not seconds)…")
    gap = gpd.overlay(zgdf[["district", "geometry"]],
                      lgdf[["typology", "geometry"]],
                      how="intersection", keep_geom_type=True)

    gap["acres"] = gap.geometry.area / 43560.0
    gap = gap[gap["acres"] >= args.min_acres]
    gap["code"] = gap.apply(
        lambda r: "ctx" if r.typology == "__CTX__"
        else matrix.get((r.district, r.typology), "ctx"), axis=1)
    gap["plan_gen"] = gap["typology"].map(
        lambda t: "legacy" if str(t).startswith("Legacy") or t == "__CTX__" else "pattern_book")
    # D-A / D-S "underzoned" is greenfield awaiting development, not urban
    # infill gap — honest to separate it so the U headline is unimpeachable.
    gap.loc[(gap.code == "U") & gap.district.isin(["D-A", "D-S"]), "code"] = "UG"
    gap["walkable"] = gap["district"].isin(walkable)

    print("[5/6] simplifying + writing…")
    gap["geometry"] = gap.geometry.simplify(args.simplify * 3.28084)  # m -> ft
    gap = gap.to_crs(4326)
    gap["acres"] = gap["acres"].round(1)
    OUT_GEOJSON.parent.mkdir(parents=True, exist_ok=True)
    gap["typology"] = gap["typology"].replace("__CTX__", "Unclassified plan category")
    gap[["district", "typology", "code", "acres", "walkable", "plan_gen", "geometry"]] \
        .to_file(OUT_GEOJSON, driver="GeoJSON")

    print("[6/6] stats…")
    stats = {
        "total_acres": round(float(gap["acres"].sum())),
        "by_code": {c: round(float(a)) for c, a in
                    gap.groupby("code")["acres"].sum().items()},
        "top_underzoned": [
            {"district": d, "typology": t, "acres": round(float(a))}
            for (d, t), a in gap[gap.code == "U"]
            .groupby(["district", "typology"])["acres"].sum()
            .sort_values(ascending=False).head(10).items()],
        "walkable_acres": round(float(gap.loc[gap.walkable, "acres"].sum())),
        "underzoned_by_plan": {g: round(float(a)) for g, a in
            gap[gap.code == "U"].groupby("plan_gen")["acres"].sum().items()},
        "acres_by_plan": {g: round(float(a)) for g, a in
            gap.groupby("plan_gen")["acres"].sum().items()},
    }
    OUT_STATS.write_text(json.dumps(stats, indent=1))
    OUT_UNMATCHED.parent.mkdir(exist_ok=True)
    OUT_UNMATCHED.write_text(json.dumps(
        {k: dict(v.most_common()) for k, v in unmatched.items()}, indent=1))

    size_mb = OUT_GEOJSON.stat().st_size / 1e6
    print(f"\n[done] {len(gap)} polygons, {size_mb:.1f} MB -> {OUT_GEOJSON}")
    print(f"       acres by code: {stats['by_code']}")
    if unmatched:
        print(f"       UNMATCHED values logged to {OUT_UNMATCHED} — "
              f"send these for calibration.")
    if size_mb > 25:
        print("       [note] file is heavy; rerun with --min-acres 0.5 "
              "--simplify 20, or we dissolve by code.")



# ---------------------------------------------------------------- overlays
OVERLAY_SOURCES = [
    {"service": "https://gis.indy.gov/server/rest/services/DMDPortal/LandUsePlanOverlaysAndSpecificAreas/MapServer",
     "layer_name": "Land Use Plan Overlays", "source": "Plan Overlay"},
    {"service": "https://gis.indy.gov/server/rest/services/DMDPortal/LandUsePlanOverlaysAndSpecificAreas/MapServer",
     "layer_name": "Specific Area Plan", "source": "Specific Area Plan"},
    {"service": "https://gis.indy.gov/server/rest/services/DMDOpenGov/DMDOpenGov/MapServer",
     "layer_name": "TOD Overlay", "source": "TOD Overlay"},
    {"service": "https://gis.indy.gov/server/rest/services/MapIndy/Zoning/MapServer",
     "layer_name": "Regional Center", "source": "Regional Center"},
]
OUT_OVERLAYS = ROOT / "docs" / "data" / "overlays.geojson"
LABEL_FIELDS = ["TYPENAME", "NAME", "PLAN_NAME", "PLANNAME", "TYPE", "LABEL",
                "SUBTYPE", "DESCRIPTION", "OVERLAY", "AREA_NAME"]


def find_layer_id(service_url, name):
    r = requests.get(service_url, params={"f": "json"}, headers=HEADERS, timeout=30)
    for lyr in r.json().get("layers", []):
        if lyr["name"].strip().lower() == name.strip().lower():
            return lyr["id"]
    return None


def pick_label(props):
    for f in LABEL_FIELDS:
        for k, v in props.items():
            if k.upper() == f and v not in (None, "", " "):
                return str(v)[:80]
    for k, v in props.items():
        if isinstance(v, str) and v.strip() and not k.lower().startswith(("shape", "objectid")):
            return v[:80]
    return ""


def build_overlays():
    import geopandas as gpd
    feats_out = []
    for src_def in OVERLAY_SOURCES:
        lid = find_layer_id(src_def["service"], src_def["layer_name"])
        if lid is None:
            print(f"[warn] overlay layer not found: {src_def['layer_name']}")
            continue
        url = f"{src_def['service']}/{lid}"
        print(f"[overlay] {src_def['source']} (layer {lid})…")
        try:
            raw = fetch_layer(url)
        except Exception as e:
            print(f"[warn] {src_def['source']} failed: {e}")
            continue
        for f in raw:
            g = f.get("geometry")
            if not g or g.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            feats_out.append({"type": "Feature", "geometry": g,
                              "properties": {"source": src_def["source"],
                                             "label": pick_label(f.get("properties") or {})}})
    if not feats_out:
        print("[warn] no overlay features collected")
        return
    gdf = gpd.GeoDataFrame.from_features(feats_out, crs=4326).to_crs(2965)
    gdf["geometry"] = gdf.buffer(0).simplify(30)
    gdf = gdf.to_crs(4326)
    gdf.to_file(OUT_OVERLAYS, driver="GeoJSON")
    print(f"[done] {len(gdf)} overlay polygons -> {OUT_OVERLAYS} "
          f"({OUT_OVERLAYS.stat().st_size/1e6:.1f} MB)")

if __name__ == "__main__":
    main()
