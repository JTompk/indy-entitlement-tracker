#!/usr/bin/env python3
"""
IndyIMBY — MDC agenda item parser + priority scoring
-----------------------------------------------------
Extracts NON-PETITION items (resolutions, contract authorizations, plan
adoptions) from MDC agenda text. These items usually have no geocodable
street address, so they never enter filings.geojson — this module writes
them to data/agenda_items.json so the digest can see them anyway.

Also provides score_item(), used by digest.py to rank BOTH resolutions
and petitions by editorial priority:

  Tier 1 (score 100+): tax incentives (ERA resolutions, abatements)
  Tier 2 (score  80+): new multi-family development
  Tier 3 (score  60+): DMD contracts authorized through the MDC
  Tier 4 (score  40+): rezonings generally
  Tier 5 (score  <40): everything else

CALIBRATION LIVES IN THE CONSTANTS BELOW. Edit the term lists and
district list to match real agenda language — they are deliberately
plain data, not logic.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ITEMS_PATH = ROOT / "data" / "agenda_items.json"

# ---------------------------------------------------------------- calibrate --

# Tier 1: tax incentives. Matched case-insensitively against item text.
# DMD phrasing per JT: "Preliminary Economic Revitalization Area Resolution",
# "Economic Revitalization Area Resolution".
INCENTIVE_TERMS = [
    "economic revitalization area",          # catches Preliminary/Confirmatory/plain ERA resolutions
    "tax abatement",
    "declaratory resolution",
    "confirmatory resolution",
    "real property abatement",
    "personal property abatement",
    "cf-1",                                  # abatement compliance filings
    "sb-1",                                  # statement of benefits (form code)
    "statement of benefits",                 # spelled-out form, incl. compliance items
    "allocation area",                       # TIF allocation-area actions
]

# Tier 2: multi-family development signals.
# District list = apartment-capable districts (Gap Map intensity logic).
MF_DISTRICTS = {
    "D-8", "D-9", "D-10", "D-11", "D-P",
    "MU-1", "MU-2", "MU-3", "MU-4",
    "CBD-1", "CBD-2", "CBD-3", "CBD-S",
}
MF_TERMS = [
    "multi-family", "multifamily", "multi family",
    "apartment", "apartments",
    "dwelling units", "residential units",
    "mixed-use", "mixed use",
    "senior housing", "affordable housing", "workforce housing",
    "townhome", "townhouse", "condominium",
]

# Tier 3: DMD contracts / agreements authorized through the MDC.
CONTRACT_TERMS = [
    "professional services",
    "authorize the department",
    "authorizing the department",
    "contract",
    "agreement",
    "amendment no",
    "not-to-exceed", "not to exceed",
    "grant agreement",
    "interlocal",
]

# Resolution item detection on MDC agendas. Tolerant on purpose:
# catches "Resolution No. 26-045", "MDC Resolution 2026-12",
# "Res. No. 26-CV-101", etc.  CALIBRATE against a real agenda PDF.
RESOLUTION_RE = re.compile(
    r"(?:MDC\s+)?RES(?:OLUTION|\.)\s*(?:NO\.?\s*)?"
    r"([0-9]{2,4}[-–][A-Z0-9]{1,4}(?:[-–][0-9A-Z]{1,5})?)",
    re.IGNORECASE,
)

# Street address inside resolution/abatement text, so incentive items can
# be geocoded and mapped even though they aren't petitions.
ITEM_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+|North\s+|South\s+|East\s+|West\s+)?"
    r"[A-Za-z0-9'. -]{2,40}?\s(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|"
    r"Boulevard|Blvd|Lane|Ln|Way|Court|Ct|Circle|Cir|Place|Pl|Pike|Parkway|"
    r"Pkwy|Trail|Terrace|Highway|Hwy)\b", re.IGNORECASE)

DOLLAR_RE = re.compile(r"\$\s?([\d]{1,3}(?:,\d{3})+|\d{4,})(?:\.\d+)?")


def max_dollar(text):
    """Largest dollar figure in a text block, as int, or None."""
    vals = [int(m.group(1).replace(",", "")) for m in DOLLAR_RE.finditer(text or "")]
    return max(vals) if vals else None


# Petition case numbers — used to AVOID double-counting blocks that the
# petition parser already handles. Mirrors CASE_RE in scrape.py.
CASE_RE = re.compile(r"\b(20\d{2})-([A-Z]{2,4}\d?)-(\d{1,4}[A-Z]?)\b")

# MDC incentive/abatement items use single-letter prefixes that CASE_RE
# (2-4 letters) does not match, e.g. "2026-A-033 (For Public Hearing)
# Final Economic Revitalization Area Resolution ...". CALIBRATE the
# prefix set if other single-letter series appear on real agendas.
MDC_ITEM_RE = re.compile(r"\b(20\d{2})-(A)-(\d{1,4}[A-Z]?)\b")

# ------------------------------------------------------------------ scoring --

def _hits(text, terms):
    t = text.lower()
    return [term for term in terms if term in t]


def score_item(item):
    """
    Score a digest record (resolution OR petition dict) by editorial priority.
    Returns (score, tier_label). Higher = more newsworthy.
    """
    text = " ".join(str(item.get(k) or "") for k in
                    ("title", "summary", "description", "zoning_to", "type"))
    tl = text.lower()

    inc = _hits(tl, INCENTIVE_TERMS)
    if inc:
        score = 100 + 2 * len(inc)
        if "preliminary" in tl:
            score += 3   # preliminary ERA = the moment public input matters most
        return score, "Tax incentive"

    zoning_to = (item.get("zoning_to") or "").upper().strip()
    mf = _hits(tl, MF_TERMS)
    if zoning_to in MF_DISTRICTS or mf:
        bonus = 5 if zoning_to in MF_DISTRICTS else 0
        return 80 + bonus + len(mf), "Multi-family"

    con = _hits(tl, CONTRACT_TERMS)
    if item.get("kind") == "resolution" and con:
        return 60 + len(con), "DMD contract"

    if item.get("type") == "Rezoning":
        return 40, "Rezoning"

    return 10 + min(len(item.get("summary") or "") // 100, 9), None


# ------------------------------------------------------------------ parsing --

def parse_mdc_items(text, board, meeting_date, agenda_url):
    """
    Extract resolution items from MDC agenda text.
    Skips blocks that contain a petition case number (scrape.py owns those).
    Returns list of dicts with kind='resolution'.
    """
    res_matches = list(RESOLUTION_RE.finditer(text))
    item_matches = list(MDC_ITEM_RE.finditer(text))
    # Block boundaries: next resolution, A-number item, or petition case
    # number — so one item's keywords can't bleed into another's block.
    boundaries = sorted({m.start() for m in res_matches}
                        | {m.start() for m in item_matches}
                        | {m.start() for m in CASE_RE.finditer(text)})

    # Words that signal a cross-reference to another resolution, not a new item.
    xref_re = re.compile(
        r"(?:confirm\w*|amend\w*|pursuant\s+to|referenced?\s+in|"
        r"supersed\w*|rescind\w*)\s*(?:\w+\s+){0,3}$", re.IGNORECASE)

    items = {}
    for m in res_matches:
        res_no = m.group(1).replace("–", "-")
        start = m.start()
        if xref_re.search(text[max(0, start - 60):start]):
            continue  # "...confirming Declaratory Resolution 25-089" is a citation
        nxt = [b for b in boundaries if b > start]
        end = min(len(text), start + 900, nxt[0] if nxt else len(text))
        block = " ".join(text[start:end].split())
        if len(block) < 60:
            continue  # index-line stub; the body block carries the details

        # Keep the longest block per resolution number (index vs. body).
        if res_no not in items or len(block) > len(items[res_no]):
            items[res_no] = block

    # A-number items (abatements etc.) — same block logic, keyed on case no.
    for m in item_matches:
        case_no = m.group(0)
        start = m.start()
        nxt = [b for b in boundaries if b > start]
        end = min(len(text), start + 900, nxt[0] if nxt else len(text))
        block = " ".join(text[start:end].split())
        if len(block) < 60:
            continue
        if case_no not in items or len(block) > len(items[case_no]):
            items[case_no] = block

    out = []
    for res_no, block in items.items():
        label = res_no if MDC_ITEM_RE.fullmatch(res_no) else f"Res. {res_no}"
        addr = ITEM_ADDRESS_RE.search(block)
        out.append({
            "kind": "resolution",
            "case": label,
            "title": block[:160],
            "summary": block[:600],
            "address": addr.group(0) if addr else None,
            "dollars": max_dollar(block),
            "board": board,
            "meeting_date": meeting_date,
            "agenda_url": agenda_url,
        })
    return out


def merge_and_save(new_items, ingested_date):
    """Append new items to data/agenda_items.json, dedup on (case, agenda_url)."""
    existing = []
    if ITEMS_PATH.exists():
        existing = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
    seen = {(i["case"], i.get("agenda_url")) for i in existing}
    added = 0
    for item in new_items:
        key = (item["case"], item.get("agenda_url"))
        if key in seen:
            continue
        item["ingested"] = ingested_date
        existing.append(item)
        seen.add(key)
        added += 1
    ITEMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ITEMS_PATH.write_text(json.dumps(existing, indent=1), encoding="utf-8")
    return added


# ---------------------------------------------------------------- test mode --

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        # python scraper/agenda_items.py some_agenda.txt  -> parse + print
        raw = Path(sys.argv[1]).read_text(encoding="utf-8", errors="ignore")
        found = parse_mdc_items(raw, "Metropolitan Development Commission",
                                "TEST", "test://agenda")
        for it in found:
            s, tier = score_item(it)
            print(f"[{s:>3}] {tier or '—':<14} {it['case']}: {it['title'][:90]}")
        print(f"\n{len(found)} resolution item(s) parsed.")
