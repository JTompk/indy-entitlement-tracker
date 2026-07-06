#!/usr/bin/env python3
"""
Pulls point layers from the City of Indianapolis ArcGIS REST services
(MapIndy) and writes them to docs/data/city_layers.geojson in the same
schema the map front-end uses.

Layers are defined in scraper/layers_config.json. Each entry:
  {
    "name":  "Legal Non-Conforming Use",      # legend label
    "url":   ".../MapServer/2",               # layer endpoint
    "where": "1=1",                           # SQL filter (dates, etc.)
    "fields": {"case": "CASE_NUM"},           # attribute -> schema mapping
    "enabled": true
  }

Usage:
  python scraper/arcgis_pull.py
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "scraper" / "layers_config.json"
OUT_PATH = ROOT / "docs" / "data" / "city_layers.geojson"

HEADERS = {"User-Agent": "IndyEntitlementTracker/1.0 (civic mapping project; contact: jeffery@proformus.com)"}
PAGE_SIZE = 1000


def query_layer(layer):
    """Page through an ArcGIS layer query, returning normalized features."""
    feats, offset = [], 0
    while True:
        params = {
            "where": layer.get("where", "1=1"),
            "outFields": "*",
            "outSR": 4326,           # ask the server to reproject to WGS84
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
        }
        r = requests.get(layer["url"] + "/query", params=params,
                         headers=HEADERS, timeout=60)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"{layer['name']}: {data['error']}")
        batch = data.get("features", [])
        feats.extend(batch)
        print(f"  [info] {layer['name']}: {len(feats)} feature(s) so far")
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.5)
    return feats


def normalize(layer, raw):
    """Map raw ArcGIS attributes into the map's property schema."""
    fmap = layer.get("fields", {})
    out = []
    for f in raw:
        geom = f.get("geometry")
        if not geom or geom.get("type") != "Point":
            continue
        a = f.get("properties", {}) or {}

        def pick(key, default=None):
            src = fmap.get(key)
            return a.get(src, default) if src else default

        date = pick("date")
        if isinstance(date, (int, float)):  # esri epoch ms
            date = datetime.fromtimestamp(date / 1000, tz=timezone.utc)\
                           .date().isoformat()
        case = pick("case") or ""
        year = None
        for token in str(case).replace("-", " ").split():
            if token.isdigit() and 1950 <= int(token) <= 2100:
                year = int(token)
                break
        if year is None and date:
            year = int(str(date)[:4])

        summary = pick("summary")
        if not summary:
            # fall back to a compact dump of non-null attributes
            summary = "; ".join(f"{k}: {v}" for k, v in a.items()
                                if v not in (None, "", " ")
                                and not k.lower().startswith(("shape", "objectid")))[:600]

        out.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "case": case or None,
                "type": layer["name"],
                "group": "city",
                "address": pick("address"),
                "board": "MapIndy (city GIS)",
                "meeting_date": date,
                "year": year,
                "summary": summary,
                "source_url": layer["url"],
            },
        })
    return out


def main():
    config = json.loads(CONFIG_PATH.read_text())
    all_feats = []
    for layer in config["layers"]:
        if not layer.get("enabled", True):
            print(f"[skip] {layer['name']} (disabled)")
            continue
        print(f"[info] pulling: {layer['name']}")
        try:
            raw = query_layer(layer)
            all_feats.extend(normalize(layer, raw))
        except Exception as e:
            print(f"[warn] {layer['name']} failed: {e}")
    gj = {
        "type": "FeatureCollection",
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "features": all_feats,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(gj))
    print(f"[done] wrote {len(all_feats)} feature(s) to {OUT_PATH}")


if __name__ == "__main__":
    main()
