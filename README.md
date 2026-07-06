# Indy Entitlement Tracker

A self-updating map of development filings before the Indianapolis Department of
Metropolitan Development — MDC, Hearing Examiner, BZA I–III, and the Plat
Committee. Every Monday morning a GitHub Action pulls the newest agenda PDFs
from the [DMD meetings portal](https://indianapolis-in.municodemeetings.com/DMDmeetings),
extracts each petition, geocodes it, and republishes the map. Total hosting
cost: $0.

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
   Monday at 6 AM Eastern and commits the result. You can also trigger it
   manually anytime from the Actions tab.

## One-time setup (~10 minutes)

1. **Create the repo.** On github.com, make a new public repository (e.g.
   `indy-entitlement-tracker`) and push this folder to it:
   ```bash
   cd indy-filings-map
   git init && git add . && git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOURNAME/indy-entitlement-tracker.git
   git push -u origin main
   ```
2. **Set your contact email.** In `scraper/scrape.py`, edit the `User-Agent`
   string — Nominatim's usage policy asks for a real contact.
3. **Turn on GitHub Pages.** Repo → Settings → Pages → Source: *Deploy from a
   branch* → Branch: `main`, folder `/docs`. Your map will live at
   `https://YOURNAME.github.io/indy-entitlement-tracker/`.
4. **Allow the Action to push.** Repo → Settings → Actions → General →
   Workflow permissions → *Read and write permissions* → Save.
5. **Backfill.** Run the first ingest yourself so the map launches with
   history:
   ```bash
   pip install -r requirements.txt
   python scraper/scrape.py --backfill
   git add . && git commit -m "Backfill" && git push
   ```
   (Or just trigger the workflow from the Actions tab for a lighter first run.)

After that, it maintains itself. Check the Actions tab occasionally — if
Municode ever changes their page structure, the run log will tell you.

## Tuning and extending

- **Parser accuracy.** Agenda formats vary slightly by board. After the
  backfill, skim `data/unmatched.json` and a sample of popups; adjust the
  regexes at the top of `scrape.py` if a board uses a pattern the parser
  misses. `--dry-run` lets you test without writing.
- **Case outcomes.** The scraper currently reads agendas (what's *filed*).
  Minutes PDFs from the same portal contain outcomes (approved/denied/
  continued) — a natural v2 is a second parser that joins outcomes back to
  cases by case number.
- **Heat over time.** With a year or two of data, add a hex-bin or kernel
  density layer to show where entitlement activity concentrates — the
  GeoJSON is ready for kepler.gl or your OSMnx pipelines as-is.
- **Schedule.** Edit the cron line in `.github/workflows/update.yml`.

## Caveats

- Locations are geocoded from agenda address strings and may be approximate;
  every popup links to the source PDF for verification.
- This reads *public agendas*, which are published in advance of hearings —
  it shows what's filed and scheduled, not what's decided.
- Municode redesigns will occasionally require a scraper tweak. The listing
  parser is deliberately loose (it grabs any agenda PDF link) to minimize
  breakage.

## City GIS layers (v1.1)

`scraper/arcgis_pull.py` pulls point layers straight from the city's MapIndy
ArcGIS services into `docs/data/city_layers.geojson`, shown on the map as ring
markers with their own toggle group. Four zoning case layers (Variances,
Approvals, historical Rezonings, Legal Non-Conforming Use) ship enabled in
`scraper/layers_config.json`.

**Finding the building permits layer:** run `python scraper/probe_layers.py`
from your own machine. It walks the MapIndy and Accela services and prints
every layer's name, geometry, and fields, flagging anything permit-related.
Paste the winning layer's URL and field names into the "Building Permits"
entry in `layers_config.json`, set `enabled: true`, and the weekly workflow
takes it from there. If the probe finds nothing public, email
DigitalData@indy.gov and ask for the DBNS permits map service endpoint.

**Tip:** the shipped `where` clauses are `1=1` (everything). Once the probe
shows you each layer's date field, constrain them (e.g.
`"where": "ISSUE_DATE >= DATE '2024-01-01'"`) to keep the map fast.

## The Monday digest (v1.2)

`scraper/digest.py` drafts each week's IndyIMBY post from the filings
ingested in the last 7 days: counts by type and township, the five most
notable cases (rezonings first) with agenda links, and upcoming hearing
dates. The weekly workflow runs it automatically and commits the draft to
`digest_drafts/`.

Your Monday routine: open the draft, fill in the two TODO spots (the lede
and a one-line take per case), move the file to the IndyIMBY site repo's
`content/posts/`, push. The site rebuilds and the RSS feed carries it to
email. Ten minutes, every week, forever.
