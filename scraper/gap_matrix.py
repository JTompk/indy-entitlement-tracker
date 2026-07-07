#!/usr/bin/env python3
"""
Zoning Gap crosswalk generator — Indianapolis / Marion County.

Derives the full zoning-district x Pattern Book-typology congruence matrix
from two small calibration tables (district intensity ranks, typology
intensity ranges) plus use-family logic. Edit the tables, rerun, and the
whole matrix regenerates — the tables ARE the methodology.

Cell codes:
  C   congruent   — zoning intensity within the typology's intended range
  C*  congruent w/ form caveat — intensity fits but district is auto-oriented
                    in a walkability-oriented typology
  U   underzoned  — plan intends MORE intensity than zoning allows (the gap)
  O   overzoned   — zoning allows more intensity than the plan intends
  X   use mismatch — district's use family isn't contemplated by the typology
  ctx evaluate individually (planned/special districts)

Output: crosswalk.csv
"""

import csv
from pathlib import Path

# ---------------------------------------------------------------------------
# CALIBRATION TABLE 1 — districts: (use_family, intensity_rank 1-10 or None=ctx)
# Families: res, mu, com, cbd, ind, inst, special
# ---------------------------------------------------------------------------
DISTRICTS = {
    # Dwelling
    "D-A":   ("res", 1), "D-S":  ("res", 1), "D-1": ("res", 2),
    "D-2":   ("res", 2), "D-3":  ("res", 3), "D-4": ("res", 4),
    "D-5":   ("res", 5), "D-5II":("res", 5), "D-6": ("res", 5),
    "D-6II": ("res", 6), "D-7":  ("res", 6), "D-8": ("res", 7),
    "D-9":   ("res", 8), "D-10": ("res", 8),
    "D-11":  ("special", None), "D-12": ("special", None),
    "D-P":   ("special", None),
    # Mixed-use
    "MU-1": ("mu", 5), "MU-2": ("mu", 6), "MU-3": ("mu", 7), "MU-4": ("mu", 8),
    # Commercial
    "C-1": ("com", 4), "C-2": ("com", 5), "C-3": ("com", 5),
    "C-4": ("com", 6), "C-5": ("com", 7), "C-7": ("com", 6),
    "C-S": ("special", None),
    # CBD
    "CBD-1": ("cbd", 10), "CBD-2": ("cbd", 9), "CBD-3": ("cbd", 8),
    "CBD-S": ("special", None),
    # Industrial
    "I-1": ("ind", 4), "I-2": ("ind", 5), "I-3": ("ind", 6), "I-4": ("ind", 7),
    # Institutional / other
    "HD": ("inst", 8), "UQ-1": ("inst", 7), "UQ-2": ("inst", 8),
    "PK-1": ("special", None), "PK-2": ("special", None),
    "SU-*": ("special", None), "SZ-*": ("special", None), "HP-*": ("special", None),
}

# Auto-oriented districts: intensity may fit, but form typically doesn't
AUTO_ORIENTED = {"C-4", "C-5", "C-7"}

# Walkable-district whitelist for the standalone walkability layer
WALKABLE_DISTRICTS = {
    "D-5", "D-5II", "D-6", "D-6II", "D-7", "D-8", "D-9", "D-10",
    "MU-1", "MU-2", "MU-3", "MU-4",
    "CBD-1", "CBD-2", "CBD-3", "HD", "UQ-1", "UQ-2",
}

# ---------------------------------------------------------------------------
# CALIBRATION TABLE 2 — typologies: (allowed_families, (min_rank, max_rank))
# Fifteen typologies of the Marion County Land Use Pattern Book.
# ---------------------------------------------------------------------------
TYPOLOGIES = {
    # Living
    "Rural/Estate Neighborhood":  ({"res"},                       (1, 2)),
    "Suburban Neighborhood":      ({"res"},                       (2, 4)),
    "Traditional Neighborhood":   ({"res", "mu"},                 (4, 6)),
    "City Neighborhood":          ({"res", "mu"},                 (6, 8)),
    # Mixed-Use
    "Village Mixed-Use":          ({"mu", "res", "com"},          (5, 7)),
    "Urban Mixed-Use":            ({"mu", "res", "com", "cbd"},   (6, 9)),
    "Core Mixed-Use":             ({"mu", "cbd", "res"},          (9, 10)),
    "Institution-Oriented MU/Campus": ({"inst", "mu"},            (6, 9)),
    # ---- Legacy plan categories (pre-Pattern Book, still governing in
    # ---- parts of the county). Density buckets map natively to ranks.
    "Legacy: 0-1.75 du/ac":       ({"res"},                       (1, 2)),
    "Legacy: 1.75-3.5 du/ac":     ({"res"},                       (2, 3)),
    "Legacy: 3.5-5 du/ac":        ({"res"},                       (3, 4)),
    "Legacy: 5-8 du/ac":          ({"res"},                       (4, 5)),
    "Legacy: 8-15 du/ac":         ({"res"},                       (6, 7)),
    "Legacy: 15-26 du/ac":        ({"res"},                       (7, 8)),
    "Legacy: 27-49 du/ac":        ({"res"},                       (8, 9)),
    "Legacy: 50+ du/ac":          ({"res"},                       (9, 10)),
    "Legacy: Estate Residential": ({"res"},                       (1, 2)),
    "Legacy: Single-Family":      ({"res"},                       (2, 4)),
    "Legacy: Multi-Family":       ({"res"},                       (6, 8)),
    "Legacy: Agricultural Preservation": ({"res"},                (1, 1)),
    "Legacy: Office":             ({"com"},                       (4, 6)),
    "Legacy: Commercial":         ({"com"},                       (5, 7)),
    "Legacy: Auto Commercial":    ({"com"},                       (5, 7)),
    "Legacy: Industrial":         ({"ind"},                       (5, 7)),
    "Legacy: Research/Technology": ({"com", "ind"},               (5, 7)),
    "Legacy: Institutional":      ({"inst"},                      (4, 8)),
    "Legacy: Plan-specifies D-4": ({"res"},                       (4, 4)),
    "Legacy: Plan-specifies D-5": ({"res"},                       (5, 5)),
    "Legacy: Plan-specifies D-6": ({"res"},                       (5, 5)),
    "Legacy: Plan-specifies D-8": ({"res"},                       (7, 7)),
    "Legacy: Plan-specifies C-1": ({"com"},                       (4, 4)),
    "Legacy: Plan-specifies C-2": ({"com"},                       (5, 5)),
    "Legacy: Plan-specifies MU-2": ({"mu", "res"},                (6, 6)),
    # Working
    "Office Commercial":          ({"com"},                       (4, 6)),
    "Community Commercial":       ({"com", "mu"},                 (5, 7)),
    "Regional Commercial":        ({"com"},                       (6, 8)),
    "Heavy Commercial":           ({"com", "ind"},                (5, 7)),
    "Office/Industrial Mixed-Use":({"com", "ind"},                (5, 7)),
    "Light Industrial":           ({"ind"},                       (4, 6)),
    "Heavy Industrial":           ({"ind"},                       (6, 8)),
}

WALKABILITY_TYPOLOGIES = {
    "Traditional Neighborhood", "City Neighborhood",
    "Village Mixed-Use", "Urban Mixed-Use", "Core Mixed-Use",
}


def classify(district, typology):
    family, rank = DISTRICTS[district]
    families, (lo, hi) = TYPOLOGIES[typology]
    if family == "special" or rank is None:
        return "ctx"
    if family not in families:
        return "X"
    if rank < lo:
        return "U"
    if rank > hi:
        return "O"
    if district in AUTO_ORIENTED and typology in WALKABILITY_TYPOLOGIES:
        return "C*"
    return "C"


def main():
    out = Path(__file__).resolve().parent / "crosswalk.csv"
    typ_names = list(TYPOLOGIES)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["district", "use_family", "intensity_rank", "walkable"]
                   + typ_names)
        for d, (fam, rank) in DISTRICTS.items():
            w.writerow([d, fam, rank if rank is not None else "ctx",
                        "yes" if d in WALKABLE_DISTRICTS else "no"]
                       + [classify(d, t) for t in typ_names])
    # quick stats
    cells = [classify(d, t) for d in DISTRICTS for t in TYPOLOGIES]
    from collections import Counter
    print("cell distribution:", dict(Counter(cells)))
    print(f"[done] wrote {out}")


if __name__ == "__main__":
    main()
