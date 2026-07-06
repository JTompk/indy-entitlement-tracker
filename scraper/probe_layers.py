#!/usr/bin/env python3
"""
Permits layer hunt. Walks candidate Indy ArcGIS services and prints every
layer's ID, name, geometry type, and fields, so you can spot the building
permits layer and paste its URL into layers_config.json.

Run from your own machine (unrestricted network):
  python scraper/probe_layers.py

When you find something like "Structural Permits" or "Permits" with
esriGeometryPoint, its endpoint is  <service>/MapServer/<id>.
"""

import json
import requests

HEADERS = {"User-Agent": "IndyEntitlementTracker/1.0 (civic mapping project; contact: jeffery@proformus.com)"}

SERVICES = [
    "https://gis.indy.gov/server/rest/services/MapIndy/MapIndyProperty/MapServer",
    "https://gis.indy.gov/server/rest/services/MapIndy/Zoning/MapServer",
    "https://gis.indy.gov/server/rest/services/Accela/AGIS_INDIANAPOLIS/MapServer",
    "https://gis.indy.gov/server/rest/services/Accela/ACCELA_XAPO_ADDRESS/MapServer",
    "https://gis.indy.gov/server/rest/services/BNSDPW",  # folder: expands below
]

KEYWORDS = ("permit", "structural", "improvement", "wreck", "demol", "violation")


def get_json(url):
    r = requests.get(url, params={"f": "json"}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def probe_service(url):
    try:
        info = get_json(url)
    except Exception as e:
        print(f"[warn] {url}: {e}")
        return
    # Folder? Recurse into its services.
    if "services" in info:
        for svc in info.get("services", []):
            probe_service(
                url.rsplit("/rest/services", 1)[0]
                + "/rest/services/" + svc["name"] + "/" + svc["type"]
            )
        return
    layers = info.get("layers", []) + info.get("tables", [])
    print(f"\n=== {url} ({len(layers)} layers/tables) ===")
    for lyr in layers:
        lid, name = lyr["id"], lyr["name"]
        flag = " <-- CHECK THIS" if any(k in name.lower() for k in KEYWORDS) else ""
        try:
            detail = get_json(f"{url}/{lid}")
            geom = detail.get("geometryType", "table")
            fields = [f["name"] for f in detail.get("fields", [])][:12]
            print(f"  [{lid:>3}] {name} | {geom}{flag}")
            if flag:
                print(f"        fields: {', '.join(fields)}")
                print(f"        url:    {url}/{lid}")
        except Exception as e:
            print(f"  [{lid:>3}] {name} | (detail failed: {e}){flag}")


if __name__ == "__main__":
    for s in SERVICES:
        probe_service(s)
    print("\nDone. Paste any permits layer URL + field names into "
          "scraper/layers_config.json and set enabled=true.")
