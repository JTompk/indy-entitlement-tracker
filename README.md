# Indy Entitlement Tracker

A self-updating map of development filings before the Indianapolis Department of
Metropolitan Development — MDC, Hearing Examiner, BZA I–III, and the Plat
Committee. Every Monday morning a GitHub Action pulls the newest agenda PDFs
from the [DMD meetings portal](https://indianapolis-in.municodemeetings.com/DMDmeetings),
extracts each petition, geocodes it, and republishes the map. 

## How the pipeline works

1. **Discover** — `scraper/scrape.py` loads the DMD meetings listing page and
   collects every agenda PDF link it hasn't seen before (tracked in
   `data/processed.json`, so runs are incremental).
2. **Parse** — each PDF is read with `pdfplumber`. The text is split into
   petition blocks anchored on case numbers (`2026-ZON-045`, `2026-DV1-014`,
   etc.). From each block it extracts the address, township, council district,
   requested rezoning ("D-8 to MU-2"), and a summary, and classifies the
   filing type from the case prefix.
3. **Geocode** — addresses go to the free **US Census Geocoder** (no API key),
   with OpenStreetMap's Nominatim as a fallback. Results are cached in
   `data/geocode_cache.json` so an address is only ever geocoded once.
   Petitions that fail to geocode land in `data/unmatched.json` for manual
   review — nothing is silently dropped.
4. **Publish** — everything is written to `docs/data/filings.geojson`, which
   GitHub Pages serves alongside `docs/index.html`, a Leaflet map with filters
   for filing type, board, year, and free-text search.
5. **Automate** — `.github/workflows/update.yml` runs the whole thing every
   Monday at 6 AM Eastern and commits the result. 


## Caveats

- Locations are geocoded from agenda address strings and may be approximate;
  every popup links to the source PDF for verification.
- This reads *public agendas*, which are published in advance of hearings —
  it shows what's filed and scheduled, not what's decided.


## City GIS layers (v1.1)

`scraper/arcgis_pull.py` pulls point layers straight from the city's MapIndy
ArcGIS services into `docs/data/city_layers.geojson`, shown on the map as ring
markers with their own toggle group. Four zoning case layers (Variances,
Approvals, historical Rezonings, Legal Non-Conforming Use) ship enabled in
`scraper/layers_config.json`.



