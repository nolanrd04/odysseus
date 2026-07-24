#!/usr/bin/env python3
"""
derive_patterns.py — knowledge-pack derivation core (lives in the app tree).

Reads case library JSONs (src/quick_proposal/case_library/ by default) and outputs:
  knowledge_pack.json  — machine-readable patterns for System B
  patterns_review.md   — human-readable sanity-check for Nolan

Callable two ways:
  CLI:  python -m src.quick_proposal.knowledge_pack.derive_patterns [--holdout FOLDER] [--out-dir DIR]
        (default output is THIS directory, i.e. the live KP path the app reads)
  App:  build_kp_for_jobs(job_names, out_dir) — per-run dynamic KP scoped to a job selection.

Hand-authored rules in manual_rules.json (not derivable from the case library) are
merged into every pack built here.
"""

import argparse
import json
import statistics
from datetime import date
from pathlib import Path

CASE_LIBRARY_DIR = Path(__file__).parent.parent / "case_library"
BASE_OUTPUT_DIR  = Path(__file__).parent          # always used for input files (label map, etc.)
PAVING_LABEL_MAP_PATH = BASE_OUTPUT_DIR / "paving_label_map.json"
MANUAL_RULES_PATH     = BASE_OUTPUT_DIR / "manual_rules.json"

MEASURABLE_UNITS = {"EA", "LF", "SF", "SY", "CY", "HR"}

US_STATE_ABBREVIATIONS = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI",
    "WYOMING": "WY",
}

MARKET_CONTEXT = {
    "market": "Kootenai County ID / Bonner County ID / Spokane County WA",
    "common_cities": [
        "Coeur d'Alene", "Hayden", "Post Falls", "Rathdrum", "Athol", "Spirit Lake",
        "Sandpoint", "Spokane",
    ],
    "caveat": (
        "All rates are calibrated to this specific tri-county market and time window. "
        "Do NOT apply directly to the Treasure Valley (Ada/Canyon County ID), "
        "western WA, or other markets without adjustment."
    ),
    "fuel_sensitive_items": [
        "strip topsoil", "exc to embank", "haul-off items", "mobilization", "BORROW/IMPORT"
    ],
}


def resolve_holdout(raw: str) -> tuple[str, str]:
    """
    Accept a folder path (e.g. 'data/26001-1_WOODMAN' or '26001-1_WOODMAN')
    and return (job_name, folder_slug) by scanning the case library for a
    matching identity.local_folder. Raises SystemExit if not found.
    """
    folder_name = Path(raw.rstrip("/")).name  # strip leading path components
    for f in sorted(CASE_LIBRARY_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        if data.get("identity", {}).get("local_folder", "").lower() == folder_name.lower():
            job_name = data["job_name"]
            slug = folder_name.lower()
            print(f"  [holdout] Resolved '{raw}' → job_name='{job_name}', slug='{slug}'")
            return job_name, slug
    raise SystemExit(
        f"ERROR: No case library entry found with local_folder='{folder_name}'.\n"
        f"Available folders: " +
        ", ".join(
            json.loads(f.read_text(encoding="utf-8")).get("identity", {}).get("local_folder", "")
            for f in sorted(CASE_LIBRARY_DIR.glob("*.json"))
        )
    )


def load_cases(holdout_job_name: str | None = None,
               include_jobs: list[str] | None = None,
               case_dir: Path | None = None):
    """Load case library records. `include_jobs` (job_name match, case-insensitive)
    scopes the library to a selection; `holdout_job_name` excludes one job."""
    include_lower = {j.lower() for j in include_jobs} if include_jobs is not None else None
    cases = []
    for f in sorted((case_dir or CASE_LIBRARY_DIR).glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        name = data.get("job_name", "")
        if holdout_job_name and name.lower() == holdout_job_name.lower():
            print(f"  [holdout] Skipping: {name}")
            continue
        if include_lower is not None and name.lower() not in include_lower:
            continue
        cases.append(data)
    return cases


def load_cases_from_db(holdout_job_name: str | None = None,
                       include_jobs: list[str] | None = None):
    """Load case records from the DB-backed case library (TODO_YY) instead of the
    on-disk JSON files. Used by the CLI's --from-db mode so the committed static
    knowledge pack can be rebuilt from the authoritative store after jobs are
    edited in the brain UI. Same filtering semantics as load_cases()."""
    from core.database import SessionLocal
    from src.quick_proposal.case_store import load_all_records
    include_lower = {j.lower() for j in include_jobs} if include_jobs is not None else None
    db = SessionLocal()
    try:
        records = load_all_records(db)
    finally:
        db.close()
    cases = []
    for data in records:
        name = data.get("job_name", "")
        if holdout_job_name and name.lower() == holdout_job_name.lower():
            print(f"  [holdout] Skipping: {name}")
            continue
        if include_lower is not None and name.lower() not in include_lower:
            continue
        cases.append(data)
    return cases


def get_primary_proposal(case):
    if not case.get("proposals"):
        return None
    idx = case.get("primary_proposal_index", 0)
    p = case["proposals"][idx]
    if not p.get("line_items"):
        return None
    return p


def percentile(sorted_vals, p):
    """p in [0, 1]. Returns interpolated value."""
    n = len(sorted_vals)
    if n == 0:
        return None
    idx = p * (n - 1)
    lo, hi = int(idx), min(int(idx) + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def dist_stats(values):
    """Compute distribution stats for a list of numeric values."""
    n = len(values)
    if n == 0:
        return None
    s = sorted(values)
    return {
        "n": n,
        "min": round(s[0], 2),
        "p25": round(percentile(s, 0.25), 2) if n >= 4 else None,
        "median": round(statistics.median(s), 2),
        "p75": round(percentile(s, 0.75), 2) if n >= 4 else None,
        "max": round(s[-1], 2),
        "mean": round(statistics.mean(s), 2),
        "std": round(statistics.stdev(s), 2) if n >= 2 else None,
    }


def pearson_r(x, y):
    n = len(x)
    if n < 2:
        return None
    mx, my = sum(x) / n, sum(y) / n
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    den = (sum((xi - mx) ** 2 for xi in x) * sum((yi - my) ** 2 for yi in y)) ** 0.5
    return round(num / den, 3) if den > 0 else None


def linear_slope(x, y):
    n = len(x)
    if n < 2:
        return None
    mx, my = sum(x) / n, sum(y) / n
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    den = sum((xi - mx) ** 2 for xi in x)
    return num / den if den > 0 else None


# ---------------------------------------------------------------------------
# Section 1: $/unit distributions
# ---------------------------------------------------------------------------

def derive_unit_price_distributions(cases):
    from collections import defaultdict
    obs_by_desc = defaultdict(list)

    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        job = case["job_name"]
        date_str = p.get("proposal_date", "")
        jtype = case["classification"]["job_type"]

        for item in p["line_items"]:
            if item.get("is_optional"):
                continue
            if item["unit_price"] is None or item["unit_price"] <= 0:
                continue
            obs_by_desc[item["description"]].append({
                "job": job,
                "date": date_str,
                "job_type": jtype,
                "unit": item["unit"],
                "unit_price": item["unit_price"],
                "qty": item["qty"],
                "ext_price": item["ext_price"],
                "category": item.get("category", ""),
            })

    distributions = {}
    for desc, obs in sorted(obs_by_desc.items()):
        prices = [o["unit_price"] for o in obs]
        n = len(obs)
        units = sorted({o["unit"] for o in obs})
        categories = sorted({o["category"] for o in obs if o["category"]})
        is_measurable = all(o["unit"] in MEASURABLE_UNITS for o in obs)

        entry = {
            "unit": units[0] if len(units) == 1 else units,
            "category": categories[0] if len(categories) == 1 else categories,
            "n_jobs": n,
            "is_measurable": is_measurable,
            "reliable": n >= 3,
        }
        entry.update(dist_stats(prices))
        entry["date_range"] = {
            "earliest": min((o["date"] for o in obs if o["date"]), default=None),
            "latest": max((o["date"] for o in obs if o["date"]), default=None),
        }
        entry["observations"] = [
            {
                "job": o["job"],
                "date": o["date"],
                "unit_price": o["unit_price"],
                "qty": o["qty"],
                "ext_price": o["ext_price"],
            }
            for o in sorted(obs, key=lambda x: x["date"])
        ]
        distributions[desc] = entry

    return distributions


# ---------------------------------------------------------------------------
# Section 2: Derivation rules
# ---------------------------------------------------------------------------

def _find_item(line_items, *keywords):
    """Return first line item whose description contains ALL keywords (case-insensitive)."""
    kw = [k.upper() for k in keywords]
    return next((i for i in line_items if all(k in i["description"].upper() for k in kw)), None)


def _ratio_stats(ratios):
    if not ratios:
        return {}
    return {
        "median": round(statistics.median(ratios), 3),
        "min": round(min(ratios), 3),
        "max": round(max(ratios), 3),
    }


def _rule_qty_ratio_vs_lots(cases, desc_keywords, rule_name, description, note,
                             target_ratio, tolerance=0.50):
    """Rule: qty ≈ round(lot_count × target_ratio). Match = within tolerance of predicted."""
    obs = []
    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        lots = case["derived"]["scale_metrics"].get("lot_count")
        if lots is None:
            continue
        item = _find_item(p["line_items"], *desc_keywords)
        if not item:
            continue
        predicted = max(1, round(lots * target_ratio))
        ratio = round(item["qty"] / lots, 3) if lots > 0 else None
        match = abs(item["qty"] - predicted) / predicted <= tolerance if predicted > 0 else False
        obs.append({
            "job": case["job_name"],
            "lots": lots,
            "actual_qty": item["qty"],
            "predicted_qty": predicted,
            "ratio_actual_to_lots": ratio,
            "match": match,
        })

    n = len(obs)
    matches = sum(1 for o in obs if o["match"])
    conf = "high" if n > 0 and matches / n >= 0.75 else "medium" if n > 0 and matches / n >= 0.50 else "low"
    return {
        "name": rule_name,
        "description": description,
        "formula": f"qty = max(1, round(lot_count × {target_ratio}))",
        "target_ratio": target_ratio,
        "applies_when": f"job has a {' '.join(desc_keywords)} line item and lot_count is known",
        "observed_support": f"{matches}/{n} jobs within {int(tolerance * 100)}%",
        "confidence": conf,
        "observations": obs,
        "exceptions_note": note,
    }


def _rule_lf_from_road_lf(cases, desc_keywords, rule_name, description, note,
                           target_ratio, tolerance=0.35):
    """Rule: item_LF ≈ road_LF × target_ratio."""
    obs = []
    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        road_lf = case["derived"]["scale_metrics"].get("road_LF")
        if not road_lf:
            continue
        item = _find_item(p["line_items"], *desc_keywords)
        if not item or item["unit"] != "LF":
            continue
        predicted = road_lf * target_ratio
        ratio = round(item["qty"] / road_lf, 3)
        match = abs(item["qty"] - predicted) / predicted <= tolerance if predicted > 0 else False
        obs.append({
            "job": case["job_name"],
            "road_LF": round(road_lf, 1),
            "actual_LF": item["qty"],
            "predicted_LF": round(predicted, 0),
            "ratio_to_road_LF": ratio,
            "match": match,
        })

    n = len(obs)
    matches = sum(1 for o in obs if o["match"])
    conf = "high" if n > 0 and matches / n >= 0.75 else "medium" if n > 0 and matches / n >= 0.50 else "low"
    ratios = [o["ratio_to_road_LF"] for o in obs]
    return {
        "name": rule_name,
        "description": description,
        "formula": f"LF = road_LF × {target_ratio}",
        "target_ratio": target_ratio,
        "applies_when": f"job has {' '.join(desc_keywords)} LF item and road_LF is known",
        "observed_support": f"{matches}/{n} jobs within {int(tolerance * 100)}%",
        "confidence": conf,
        "ratio_stats": _ratio_stats(ratios),
        "observations": obs,
        "exceptions_note": note,
    }


def _rule_lf_from_lot_count(cases, desc_keywords, rule_name, description, note,
                             target_ratio, tolerance=0.35):
    """Rule: item_LF ≈ lot_count × target_ratio."""
    obs = []
    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        lots = case["derived"]["scale_metrics"].get("lot_count")
        if not lots:
            continue
        item = _find_item(p["line_items"], *desc_keywords)
        if not item or item["unit"] != "LF":
            continue
        predicted = lots * target_ratio
        ratio = round(item["qty"] / lots, 3)
        match = abs(item["qty"] - predicted) / predicted <= tolerance if predicted > 0 else False
        obs.append({
            "job": case["job_name"],
            "lot_count": lots,
            "actual_LF": item["qty"],
            "predicted_LF": round(predicted, 0),
            "ratio_to_lot_count": ratio,
            "match": match,
        })

    n = len(obs)
    matches = sum(1 for o in obs if o["match"])
    conf = "high" if n > 0 and matches / n >= 0.75 else "medium" if n > 0 and matches / n >= 0.50 else "low"
    ratios = [o["ratio_to_lot_count"] for o in obs]
    return {
        "name": rule_name,
        "description": description,
        "formula": f"LF = lot_count × {target_ratio}",
        "target_ratio": target_ratio,
        "applies_when": f"job has {' '.join(desc_keywords)} LF item and lot_count is known",
        "observed_support": f"{matches}/{n} jobs within {int(tolerance * 100)}%",
        "confidence": conf,
        "ratio_stats": _ratio_stats(ratios),
        "observations": obs,
        "exceptions_note": note,
    }


def _rule_qty_vs_lots(cases, desc_keywords, rule_name, description, note):
    obs = []
    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        lots = case["derived"]["scale_metrics"].get("lot_count")
        if lots is None:
            continue
        item = _find_item(p["line_items"], *desc_keywords)
        if not item:
            continue
        ratio = round(item["qty"] / lots, 3) if lots > 0 else None
        obs.append({
            "job": case["job_name"],
            "lots": lots,
            "actual_qty": item["qty"],
            "ratio_actual_to_lots": ratio,
            "match": abs(ratio - 1.0) <= 0.10 if ratio is not None else False,
        })

    n = len(obs)
    matches = sum(1 for o in obs if o["match"])
    conf = "high" if n > 0 and matches / n >= 0.75 else "medium"
    return {
        "name": rule_name,
        "description": description,
        "formula": "qty = lot_count",
        "applies_when": f"job has a {' '.join(desc_keywords)} line item and lot_count is known",
        "observed_support": f"{matches}/{n} jobs within 10%",
        "confidence": conf,
        "observations": obs,
        "exceptions_note": note,
    }


def derive_derivation_rules(cases):
    rules = []

    # Rule 1: 4" SEWER SERVICES = lot_count
    rules.append(_rule_qty_vs_lots(
        cases, ['4"', "SEWER", "SERVICE"],
        "sewer_services_from_lot_count",
        '4" SEWER SERVICES qty equals lot_count',
        "Phase 2 subdivisions may have fewer services if Phase 1 already stubbed some lots",
    ))

    # Rule 2: 1" WATER SERVICES = lot_count
    rules.append(_rule_qty_vs_lots(
        cases, ['1"', "WATER", "SERVICE"],
        "water_services_from_lot_count",
        '1" WATER SERVICES qty equals lot_count',
        "Same phase-2 caveat as sewer services",
    ))

    # Rule 3: SINGLE DRYWELLS ≈ lot_count × 0.23  (NOT 1:1 — prior rule was wrong)
    # EDA Section 4: r=0.931 vs lot_count, r=0.955 vs ROW_SF. Median ratio=0.23.
    # Small jobs (≤20 lots) tend to floor at 2 regardless; formula works better for large jobs.
    rules.append(_rule_qty_ratio_vs_lots(
        cases, ["DRYWELL"],
        "drywells_from_lot_count",
        "SINGLE DRYWELLS qty ≈ max(1, round(lot_count × 0.23)). "
        "Prior rule used 1:1 ratio which was wrong — correct ratio is ~0.23. "
        "Small jobs floor at 1-2; prefer ROW_SF analog scaling (r=0.955) when available.",
        "Not all jobs use drywells. Small jobs (≤20 lots) often have exactly 2 regardless of lots. "
        "ROW_SF is a better driver (r=0.955 vs r=0.931 for lot_count) — use analog scaling when possible.",
        target_ratio=0.23,
        tolerance=0.50,
    ))

    # Rule 4: 48" SEWER MANHOLE ≈ sewer_main_LF / 250
    rule4_obs = []
    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        mh = _find_item(p["line_items"], "SEWER MANHOLE")
        # Sum all sewer main LF lines (there can be multiple diameters)
        sewer_lf_items = [i for i in p["line_items"]
                          if "SEWER MAIN" in i["description"].upper() and i["unit"] == "LF"]
        if not mh or not sewer_lf_items:
            continue
        total_sewer_lf = sum(i["qty"] for i in sewer_lf_items)
        predicted = total_sewer_lf / 250
        actual = mh["qty"]
        ratio = round(actual / predicted, 2) if predicted > 0 else None
        rule4_obs.append({
            "job": case["job_name"],
            "sewer_LF_total": total_sewer_lf,
            "sewer_mh_actual": actual,
            "sewer_mh_predicted_at_250": round(predicted, 1),
            "ratio_actual_to_predicted": ratio,
            "match": abs(ratio - 1.0) <= 0.25 if ratio is not None else False,
        })

    n4 = len(rule4_obs)
    m4 = sum(1 for o in rule4_obs if o["match"])
    rules.append({
        "name": "sewer_manholes_from_sewer_LF",
        "description": '48" SEWER MANHOLE qty ≈ sewer_main_LF / 250',
        "formula": "qty = round(total_sewer_main_LF / 250)",
        "applies_when": "8\" SEWER MAIN (and any other diameter mains) LF is known",
        "observed_support": f"{m4}/{n4} jobs within 25%",
        "confidence": "medium",
        "observations": rule4_obs,
        "exceptions_note": "Sum all sewer main LF lines — Settlement Mtn has both 8\" and 12\"",
    })

    # Rule 5: 8" WATER MAIN PVC LF ≈ road_LF × 0.85
    # EDA Section 6c: r=0.934, median_ratio=0.853, range=[0.46, 1.06]. Strong.
    rules.append(_rule_lf_from_road_lf(
        cases, ["WATER MAIN"],
        "water_main_LF_from_road_LF",
        "8\" WATER MAIN PVC LF ≈ road_LF × 0.85",
        "Does not include water service laterals. Loops or dead-ends can push ratio above 1.0.",
        target_ratio=0.85,
    ))

    # Rule 6: ROLLED CURB LF ≈ road_LF × 1.43 (conditional — only when job uses rolled curb)
    # EDA Section 6c: r=0.912, median_ratio=1.434, range=[0.79, 1.93]. Strong, N=5.
    # Some jobs use standard CURB AND GUTTER instead — check typical section first.
    rules.append(_rule_lf_from_road_lf(
        cases, ["ROLLED CURB"],
        "rolled_curb_LF_from_road_LF",
        "ROLLED CURB LF ≈ road_LF × 1.43 (applies only when job uses rolled curb, not C&G)",
        "Mutually exclusive with CURB AND GUTTER. Read curb type from typical section sheet. "
        "N=5 jobs only — confirm against analog before applying.",
        target_ratio=1.43,
    ))

    # Rule 7: IRRIGATION SLEEVES LF ≈ lot_count × 13
    # EDA Section 5: r(water_services, irr_sleeves)=0.999. Section 4: r_ROW_SF=0.989.
    # Physical: one sleeve crosses the ROW per lot at ~10-15 LF each.
    rules.append(_rule_lf_from_lot_count(
        cases, ["IRRIGATION", "SLEEVE"],
        "irrigation_sleeves_LF_from_lot_count",
        "IRRIGATION SLEEVES LF ≈ lot_count × 13",
        "Not all jobs include irrigation sleeves. Verify item is in the scope before applying.",
        target_ratio=13.0,
    ))

    return rules


# ---------------------------------------------------------------------------
# Section 3: Typical section defaults
# ---------------------------------------------------------------------------

def derive_typical_section_defaults(cases):
    from collections import defaultdict
    by_type = defaultdict(lambda: {"stripping_depths": [], "row_widths": [], "jobs": []})

    for case in cases:
        jtype = case["classification"]["job_type"]
        sm = case["derived"]["scale_metrics"]
        by_type[jtype]["jobs"].append(case["job_name"])

        sd = sm.get("stripping_depth_in")
        if sd is not None and sd > 2:  # exclude Expo's anomalous value of 2
            by_type[jtype]["stripping_depths"].append({"job": case["job_name"], "value_in": sd})

        row_sf = sm.get("ROW_SF")
        road_lf = sm.get("road_LF")
        if row_sf and road_lf and road_lf > 0:
            width_ft = round(row_sf / road_lf, 1)
            by_type[jtype]["row_widths"].append({
                "job": case["job_name"],
                "ROW_SF": row_sf,
                "road_LF": road_lf,
                "implied_width_ft": width_ft,
            })

    defaults = {}
    for jtype, data in by_type.items():
        entry = {"n_jobs": len(data["jobs"]), "jobs": data["jobs"]}

        if data["stripping_depths"]:
            vals = [d["value_in"] for d in data["stripping_depths"]]
            entry["stripping_depth_in"] = {
                "n": len(vals),
                "median": statistics.median(vals),
                "min": min(vals),
                "max": max(vals),
                "observations": data["stripping_depths"],
                "note": "Expo Private Drive (depth=2) excluded as likely unit error",
            }

        if data["row_widths"]:
            widths = [w["implied_width_ft"] for w in data["row_widths"]]
            entry["row_width_ft"] = {
                **dist_stats(widths),
                "observations": data["row_widths"],
            }

        defaults[jtype] = entry

    return defaults


# ---------------------------------------------------------------------------
# Section 4: Job-type signatures
# ---------------------------------------------------------------------------

def derive_job_type_signatures(cases):
    from collections import defaultdict
    by_type = defaultdict(list)

    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        total = p.get("total_reconciled") or p.get("line_items_sum")
        if not total:
            continue
        jtype = case["classification"]["job_type"]
        sm = case["derived"]["scale_metrics"]
        rollups = case["derived"]["rollups"]

        by_type[jtype].append({
            "job": case["job_name"],
            "date": p.get("proposal_date", ""),
            "total": total,
            "lot_count": sm.get("lot_count"),
            "road_LF": sm.get("road_LF"),
            "dollar_per_lot": rollups.get("dollar_per_lot"),
            "dollar_per_road_LF": rollups.get("dollar_per_road_LF"),
            "dollar_per_ROW_SF": rollups.get("dollar_per_ROW_SF"),
        })

    signatures = {}
    for jtype, jobs in by_type.items():
        def _s(key):
            vals = [j[key] for j in jobs if j.get(key) is not None]
            if not vals:
                return None
            st = dist_stats(vals)
            # round to dollars for readability
            for k in ("min", "p25", "median", "p75", "max", "mean", "std"):
                if st.get(k) is not None:
                    st[k] = round(st[k], 0)
            return st

        entry = {
            "n_jobs": len(jobs),
            "jobs": [
                {"job": j["job"], "date": j["date"], "total": j["total"],
                 "lot_count": j["lot_count"], "road_LF": j["road_LF"]}
                for j in sorted(jobs, key=lambda x: x["date"])
            ],
            "dollar_per_lot": _s("dollar_per_lot"),
            "dollar_per_road_LF": _s("dollar_per_road_LF"),
            "dollar_per_ROW_SF": _s("dollar_per_ROW_SF"),
        }
        if len(jobs) == 1:
            entry["note"] = "Only 1 job — ranges not statistically meaningful"

        signatures[jtype] = entry

    return signatures


# ---------------------------------------------------------------------------
# Section 5: Item pair co-occurrence and quantity correlation
# ---------------------------------------------------------------------------

def derive_item_pairs(cases, min_r=0.85, min_shared=4):
    """
    For every pair of measurable items (EA, LF, SF, SY) that appear together
    in >= min_shared jobs, compute Pearson r on quantities and co-occurrence rate.
    High-r pairs tell Claude which items travel together so it can include the
    partner without a hardcoded rule.
    """
    from collections import defaultdict

    units = {"EA", "LF", "SF", "SY"}
    job_items     = defaultdict(dict)   # job -> {desc: qty}
    item_units    = {}                  # desc -> unit
    item_job_sets = defaultdict(set)    # desc -> set of jobs

    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        job = case["job_name"]
        for item in p["line_items"]:
            if item.get("is_optional"):
                continue
            if item["unit"] not in units:
                continue
            if not item["qty"] or item["qty"] <= 0:
                continue
            desc = item["description"]
            job_items[job][desc] = item["qty"]
            item_units[desc] = item["unit"]
            item_job_sets[desc].add(job)

    eligible = sorted(desc for desc, jobs in item_job_sets.items() if len(jobs) >= min_shared)

    pairs = []
    for i in range(len(eligible)):
        for j in range(i + 1, len(eligible)):
            a, b = eligible[i], eligible[j]
            shared = [job for job in job_items if a in job_items[job] and b in job_items[job]]
            if len(shared) < min_shared:
                continue
            qa = [job_items[job][a] for job in shared]
            qb = [job_items[job][b] for job in shared]
            r = pearson_r(qa, qb)
            if r is None or abs(r) < min_r:
                continue
            ratios = [va / vb for va, vb in zip(qa, qb) if vb > 0]
            med_ratio = round(statistics.median(ratios), 4) if ratios else None
            n_a = len(item_job_sets[a])
            n_b = len(item_job_sets[b])
            pairs.append({
                "item_a":              a,
                "item_b":              b,
                "unit_a":              item_units[a],
                "unit_b":              item_units[b],
                "r":                   r,
                "n_shared_jobs":       len(shared),
                "n_jobs_a":            n_a,
                "n_jobs_b":            n_b,
                "co_occurrence_rate":  round(len(shared) / max(n_a, n_b), 3),
                "median_ratio_a_to_b": med_ratio,
                "shared_jobs":         sorted(shared),
            })

    pairs.sort(key=lambda x: -abs(x["r"]))
    return {
        "description": (
            "Item pairs with high quantity correlation (|r| >= 0.85, N >= 4 shared jobs). "
            "EA, LF, SF, and SY units included. "
            "co_occurrence_rate = n_shared_jobs / max(n_jobs_a, n_jobs_b). "
            "When computing any line item, check this list for a high-co_occurrence partner "
            "and include it if not already in the estimate."
        ),
        "pairs": pairs,
    }


# ---------------------------------------------------------------------------
# Section 6: LS earthwork rates (qty-sheet-backed $/CY)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Section 6: Paving & subgrade rates (label-map-backed $/SY)
# ---------------------------------------------------------------------------

def derive_paving_rates(cases):
    if not PAVING_LABEL_MAP_PATH.exists():
        print("  WARNING: paving_label_map.json not found — skipping paving rates")
        return {}

    label_map = json.loads(PAVING_LABEL_MAP_PATH.read_text(encoding="utf-8"))

    subgrade_obs = []
    paving_obs   = []
    ballast_obs  = []

    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        sm = (case.get("derived") or {}).get("scale_metrics", {})

        # Sum proposal costs by canonical category for this job
        costs = {}
        for item in p["line_items"]:
            info = label_map.get(item["description"])
            if not info:
                continue
            cat = info["canonical"]
            costs[cat] = costs.get(cat, 0) + (item["ext_price"] or 0)

        job = case["job_name"]

        # ── road subgrade $/SY (LS proposal cost / qty-sheet SY) ──
        sub_cost = costs.get("road_subgrade")
        sub_sy   = sm.get("road_subgrade_SY")
        if sub_cost and sub_sy:
            subgrade_obs.append({
                "job": job,
                "cost": sub_cost,
                "road_subgrade_SY": sub_sy,
                "per_SY": round(sub_cost / sub_sy, 2),
            })

        # ── road paving blended $/SY (sum of main-road paving costs / qty-sheet SY) ──
        pav_cost = costs.get("road_paving_main")
        pav_sy   = sm.get("road_paving_SY")
        if pav_cost and pav_sy:
            paving_obs.append({
                "job": job,
                "cost": pav_cost,
                "road_paving_SY": pav_sy,
                "per_SY": round(pav_cost / pav_sy, 2),
            })

        # ── ballast $/SY (proposal costs / proposal SY qty, no qty-sheet needed) ──
        bal_cost = costs.get("ballast")
        if bal_cost:
            # sum SY quantities for ballast items directly from proposal
            bal_sy = sum(
                (item["qty"] or 0)
                for item in p["line_items"]
                if label_map.get(item["description"], {}).get("canonical") == "ballast"
                and item["unit"] == "SY"
            )
            if bal_sy:
                ballast_obs.append({
                    "job": job,
                    "cost": bal_cost,
                    "ballast_SY": bal_sy,
                    "per_SY": round(bal_cost / bal_sy, 2),
                })

    return {
        "road_subgrade_per_SY": {
            "description": "SUBGRADE ROAD — LS proposal cost ÷ qty-sheet road_subgrade_SY",
            "base_unit": "SY",
            "note": "LS item back-calculated to $/SY using qty-sheet takeoff",
            "rate_distribution": dist_stats([o["per_SY"] for o in subgrade_obs]),
            "observations": sorted(subgrade_obs, key=lambda x: x["job"]),
        },
        "road_paving_main_per_SY": {
            "description": "Main road paving — blended $/SY (all road_paving_main items ÷ qty-sheet road_paving_SY)",
            "base_unit": "SY",
            "note": "Blended across all paving specs on the job; excludes pathway, widening, patching",
            "rate_distribution": dist_stats([o["per_SY"] for o in paving_obs]),
            "observations": sorted(paving_obs, key=lambda x: x["job"]),
        },
        "ballast_per_SY": {
            "description": "Structural ballast / base rock — $/SY from proposal",
            "base_unit": "SY",
            "note": "Only jobs with an explicit ballast line item",
            "rate_distribution": dist_stats([o["per_SY"] for o in ballast_obs]),
            "observations": sorted(ballast_obs, key=lambda x: x["job"]),
        },
    }

def derive_ls_earthwork_rates(cases):
    DEFAULT_STRIP_DEPTH_IN = 18  # assumed when qty-sheet depth is implausibly small (<6")

    observations = []
    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        sm = (case.get("derived") or {}).get("scale_metrics", {})
        row_sf = sm.get("ROW_SF")
        depth_in = sm.get("stripping_depth_in")
        if not row_sf or not depth_in:
            continue

        depth_assumed = False
        if depth_in < 6:
            depth_in = DEFAULT_STRIP_DEPTH_IN
            depth_assumed = True

        strip_item = None
        for item in p["line_items"]:
            d = item["description"].upper()
            if ("STRIP" in d and "SIGN" not in d and
                    "SEWER" not in d and "STRIPING" not in d):
                haul_off = any(k in d for k in ["HAUL OFF", "HAUL-OFF"])
                strip_item = {
                    "description": item["description"],
                    "cost": item["ext_price"],
                    "haul_off": haul_off,
                }
                break

        if not strip_item:
            continue

        cy = row_sf * (depth_in / 12) / 27
        per_cy = round(strip_item["cost"] / cy, 2)
        obs = {
            "job": case["job_name"],
            "ROW_SF": row_sf,
            "depth_in": depth_in,
            "CY": round(cy),
            "cost": strip_item["cost"],
            "per_CY": per_cy,
            "haul_off": strip_item["haul_off"],
            "strip_description": strip_item["description"],
        }
        if depth_assumed:
            obs["depth_note"] = f"qty-sheet depth <6\" — assumed {DEFAULT_STRIP_DEPTH_IN}\""
        observations.append(obs)

    stockpile = sorted([o for o in observations if not o["haul_off"]], key=lambda x: x["job"])
    haul_off  = sorted([o for o in observations if     o["haul_off"]], key=lambda x: x["job"])

    return {
        "row_stripping_stockpile": {
            "description": "ROW stripping — onsite stockpile",
            "base_unit": "CY",
            "formula": "CY = ROW_SF × (depth_in / 12) / 27",
            "rate_distribution": dist_stats([o["per_CY"] for o in stockpile]),
            "observations": stockpile,
        },
        "row_stripping_haul_off": {
            "description": "ROW stripping — haul off to disposal",
            "base_unit": "CY",
            "formula": "CY = ROW_SF × (depth_in / 12) / 27",
            "rate_distribution": dist_stats([o["per_CY"] for o in haul_off]),
            "note": f"{len(haul_off)} job(s) only — haul-off is site-specific; directional only",
            "observations": haul_off,
        },
    }


def derive_earthwork_balance_prior(cases):
    """
    Empirical balanced/import/export frequency per job_type, derived from each case's real
    ROW-level cut/fill bank-yard quantities (quantities_qty_sheet — Terra's own internal bid
    takeoff), not a formula or a Gemini classification.

    This exists because Gemini's own earthwork_balance categorization (roughly_balanced /
    import_needed / export_needed) has no reliable signal to work from beyond scanning for
    explicit plan notes — when no note is found, it should fall back to whatever is actually
    typical for jobs of this type, not to an unweighted guess. See system_prompt.txt's
    Excavation to Embankment section and TODO_B_NEW.

    BALANCED_THRESHOLD_CY is a reasonable cut given the observed net-CY distribution across
    the current portfolio (jobs range from ~60 CY to ~10,400 CY net) — not a precisely fit
    value. Revisit once more validated jobs accumulate.
    """
    BALANCED_THRESHOLD_CY = 500

    by_job_type = {}
    for case in cases:
        job_type = (case.get("classification") or {}).get("job_type")
        if not job_type:
            continue
        rows = ((case.get("quantities_qty_sheet") or {}).get("parsed") or {}).get("rows", [])
        cut = fill = None
        for r in rows:
            wt = (r.get("work_type") or "").upper()
            if "BANK" not in wt or "LOT" in wt or r.get("qty") is None:
                continue
            # "group" is not a reliable ROW-vs-LOT discriminator — some jobs tag lot-level
            # cut/fill rows as group="GRADING" too (e.g. 6th Ave), others use group="LOTS"
            # (e.g. Solara). Match on work_type text instead and exclude anything "LOT".
            # Last match wins on purpose: some jobs list a raw figure then a corrected one
            # (e.g. Settlement Mountain's "... AFTER 21" STRIP" variant) — the later row is
            # the refined engineering number.
            if "CUT" in wt:
                cut = r["qty"]
            elif "FILL" in wt:
                fill = r["qty"]
        if cut is None or fill is None:
            continue

        net_cy = cut - fill
        if abs(net_cy) <= BALANCED_THRESHOLD_CY:
            outcome = "roughly_balanced"
        elif net_cy < 0:
            outcome = "import_needed"
        else:
            outcome = "export_needed"

        entry = by_job_type.setdefault(job_type, {
            "n": 0, "roughly_balanced": 0, "import_needed": 0, "export_needed": 0,
            "observations": [],
        })
        entry["n"] += 1
        entry[outcome] += 1
        entry["observations"].append({
            "job": case["job_name"], "cut_CY": cut, "fill_CY": fill, "net_CY": net_cy,
            "outcome": outcome,
        })

    for job_type, entry in by_job_type.items():
        dominant = max(
            ("roughly_balanced", "import_needed", "export_needed"),
            key=lambda k: entry[k],
        )
        entry["dominant_outcome"] = dominant
        entry["dominant_share"] = round(entry[dominant] / entry["n"], 3) if entry["n"] else None
        entry["thin_data"] = entry["n"] < 5

    return by_job_type


def _parse_state_abbr(location):
    """Best-effort 2-letter state abbreviation from a 'City, State' string — accepts both
    abbreviations ('Spokane, WA') and full names ('Post Falls, Idaho')."""
    if not location or "," not in location:
        return None
    tail = location.rsplit(",", 1)[-1].strip().upper()
    if len(tail) == 2 and tail.isalpha():
        return tail
    return US_STATE_ABBREVIATIONS.get(tail)


def _resolve_job_state(case):
    """Job-site state — this is what determines which state's sales-tax convention applies,
    NOT the client's billing address, so job_location is tried first (identity.client_location
    is a fallback only, and can disagree — e.g. Painted Rock has a WA client but an ID job site)."""
    identity = case.get("identity") or {}
    return (_parse_state_abbr(identity.get("job_location"))
            or _parse_state_abbr(identity.get("client_location")))


def derive_wa_tax_scope(cases):
    """
    Section: wa_tax_scope — per-state, per-item sales-tax classification derived from line
    items that carry an explicit tax_rate (entered via the Jobs tab's tax % column).

    Grouped by the job-site state rather than hardcoded to Washington: every taxed observation
    in the portfolio today is WA, but if the company ever bids a job in another state with its
    own tax convention, that state gets its own bucket automatically instead of silently
    merging into (and polluting) the Washington rates.

    Each item reports `typical_rate` (median of its non-zero observations) and `rates` — every
    observed tax_rate for that description in that state, in job order, zeros included (e.g.
    SUBGRADE ROAD: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.089, 0.0] — untaxed on 7 WA jobs, taxed on
    one). A 0 in `rates` means "billed untaxed on at least one job" — per the Sales Tax rule in
    system_prompt.txt, that's the signal the manager uses to NOT auto-tax an item, flagging it
    for a human call instead of guessing. Items never observed taxed in a state aren't included
    at all — an item absent from this section already defaults to untaxed (see kp_lookup rule
    3), so there's nothing this section needs to say about them.
    """
    by_state_desc = {}
    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        job = case["job_name"]
        state = _resolve_job_state(case) or "_UNKNOWN"
        for item in p["line_items"]:
            if item.get("is_optional"):
                continue
            tax_rate = item.get("tax_rate")
            if tax_rate is None:
                continue
            by_state_desc.setdefault(state, {}).setdefault(item["description"], []).append({
                "job": job, "tax_rate": tax_rate,
            })

    def _build_state_entry(by_desc):
        items = {}
        all_taxed_rates = []
        taxed_jobs = set()
        for desc, obs in sorted(by_desc.items()):
            rates = [o["tax_rate"] for o in obs]
            taxed = [r for r in rates if r]
            if not taxed:
                continue
            all_taxed_rates.extend(taxed)
            taxed_jobs.update(o["job"] for o in obs if o["tax_rate"])
            items[desc] = {
                "typical_rate": round(statistics.median(taxed), 4),
                "rates": [o["tax_rate"] for o in sorted(obs, key=lambda o: o["job"])],
            }
        if not items:
            return None
        return {
            "_typical_rate": round(statistics.median(all_taxed_rates), 4),
            "_n_jobs_with_taxed_items": len(taxed_jobs),
            "items": items,
        }

    result = {}
    for state, by_desc in sorted(by_state_desc.items()):
        if state == "_UNKNOWN":
            continue
        entry = _build_state_entry(by_desc)
        if entry:
            result[state] = entry

    if "_UNKNOWN" in by_state_desc:
        entry = _build_state_entry(by_state_desc["_UNKNOWN"])
        if entry:
            entry["note"] = ("Non-zero tax_rate observations whose job-site state couldn't be "
                              "parsed from identity.job_location/client_location — spot-check "
                              "before trusting.")
            result["_unresolved_state"] = entry

    return result


# ---------------------------------------------------------------------------
# Section 7: Item quantity vs scale metric correlations
# ---------------------------------------------------------------------------

def derive_qty_scale_correlations(cases):
    """
    For every measurable proposal item with N>=4 jobs, compute Pearson r vs
    lot_count, road_LF, and ROW_SF, plus the observed median ratio.

    Key finding: ROW_SF is the single best predictor for most items. Use it as
    the primary analog-matching metric and for proportional scaling when no
    formula rule applies.
    """
    from collections import defaultdict

    obs_by_desc = defaultdict(list)
    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        sm = case["derived"]["scale_metrics"]
        lot_count = sm.get("lot_count")
        road_lf   = sm.get("road_LF")
        row_sf    = sm.get("ROW_SF")

        fronting_lf = sm.get("fronting_LF")
        for item in p["line_items"]:
            if item.get("is_optional") or item["unit"] not in MEASURABLE_UNITS:
                continue
            if not item["qty"] or item["qty"] <= 0:
                continue
            obs_by_desc[item["description"]].append({
                "job":         case["job_name"],
                "qty":         item["qty"],
                "lot_count":   lot_count,
                "road_LF":     road_lf,
                "ROW_SF":      row_sf,
                "fronting_LF": fronting_lf,
            })

    correlations = {}
    for desc, obs_list in sorted(obs_by_desc.items()):
        if len(obs_list) < 4:
            continue
        entry = {"n": len(obs_list)}

        best_r, best_metric = None, None
        for metric in ["lot_count", "road_LF", "ROW_SF", "fronting_LF"]:
            pairs = [(o[metric], o["qty"]) for o in obs_list if o[metric] is not None]
            if len(pairs) < 4:
                continue
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            r = pearson_r(xs, ys)
            slope = linear_slope(xs, ys)
            ratios = [y / x for x, y in zip(xs, ys) if x > 0]
            entry[metric] = {
                "n": len(pairs),
                "r": r,
                "slope": round(slope, 5) if slope is not None else None,
                "median_ratio": round(statistics.median(ratios), 4) if ratios else None,
                "min_ratio":    round(min(ratios), 4) if ratios else None,
                "max_ratio":    round(max(ratios), 4) if ratios else None,
            }
            if r is not None and (best_r is None or abs(r) > abs(best_r)):
                best_r, best_metric = r, metric

        entry["best_driver"] = best_metric
        entry["best_r"] = best_r
        correlations[desc] = entry

    # Sort by best_r descending
    correlations = dict(sorted(
        correlations.items(),
        key=lambda x: -(abs(x[1].get("best_r") or 0)),
    ))

    return {
        "description": (
            "Pearson r between item quantity and each scale metric (N>=4 jobs). "
            "ROW_SF dominates for most items. "
            "Use ROW_SF as primary analog-matching metric and for proportional scaling "
            "when no specific formula rule applies."
        ),
        "analog_retrieval_note": (
            "When scaling a quantity from an analog job without a formula rule, "
            "prefer ROW_SF ratio: est_qty = analog_qty × (new_ROW_SF / analog_ROW_SF). "
            "This outperforms lot_count or road_LF scaling for nearly all items."
        ),
        "correlations": correlations,
    }


# ---------------------------------------------------------------------------
# Section 8: Unit price trends over time
# ---------------------------------------------------------------------------

def derive_price_trends(cases):
    """
    For measurable items with N>=6 observations, compute the linear price trend
    ($/unit/year) and Pearson r between date and unit_price.
    Items with |r|>=0.50 warrant recency-weighting in M3 costing.
    """
    from collections import defaultdict
    from datetime import datetime

    obs_by_desc = defaultdict(list)
    for case in cases:
        p = get_primary_proposal(case)
        if not p or not p.get("proposal_date"):
            continue
        for item in p["line_items"]:
            if item.get("is_optional") or item["unit"] not in MEASURABLE_UNITS:
                continue
            if not item["unit_price"] or item["unit_price"] <= 0:
                continue
            obs_by_desc[item["description"]].append({
                "date": p["proposal_date"],
                "unit_price": item["unit_price"],
                "unit": item["unit"],
                "job": case["job_name"],
            })

    trends = {}
    for desc, obs_list in sorted(obs_by_desc.items()):
        if len(obs_list) < 6:
            continue
        dated = []
        for o in obs_list:
            try:
                d = datetime.strptime(o["date"], "%Y-%m-%d")
                dated.append((d, o["unit_price"], o["job"]))
            except ValueError:
                continue
        dated.sort()
        if len(dated) < 4:
            continue

        base = dated[0][0]
        days   = [(d - base).days for d, _, _ in dated]
        prices = [p for _, p, _ in dated]
        r = pearson_r(days, prices)
        slope = linear_slope(days, prices)
        annual = slope * 365 if slope else 0
        med_price = statistics.median(prices)
        pct_yr = annual / med_price * 100 if med_price else 0
        unit = obs_list[0]["unit"]

        conf = ("strong"   if abs(r) >= 0.70 else
                "moderate" if abs(r) >= 0.40 else "weak")
        recency_weight = abs(r) >= 0.50

        trends[desc] = {
            "unit": unit,
            "n": len(dated),
            "r": r,
            "annual_change_per_unit": round(annual, 2),
            "annual_pct_change": round(pct_yr, 1),
            "direction": "up" if annual > 0 else "down",
            "confidence": conf,
            "recency_weight": recency_weight,
            "median_unit_price": round(med_price, 2),
            "observations": [
                {"job": j, "date": d.strftime("%Y-%m-%d"), "unit_price": p}
                for d, p, j in dated
            ],
        }

    # Sort by absolute annual change descending
    trends = dict(sorted(
        trends.items(),
        key=lambda x: -abs(x[1]["annual_change_per_unit"]),
    ))

    return {
        "description": (
            "Unit price trends 2023-2026. Items with recency_weight=true have "
            "|r|>=0.50 — use most recent jobs' prices rather than the overall median."
        ),
        "trends": trends,
    }


# ---------------------------------------------------------------------------
# Section 9: Item prevalence (how many jobs include each line item)
# ---------------------------------------------------------------------------

def derive_item_prevalence(cases):
    """
    For every distinct line item in the portfolio, compute overall prevalence and
    conditional prevalence split at the median ROW_SF and median lot_count.
    Thresholds are portfolio-derived — they update automatically as jobs are added.
    Only jobs with proposals are counted (they're the only jobs whose item presence
    can be known).
    """
    from collections import defaultdict

    norm_to_data = defaultdict(lambda: {"original": None, "jobs_present": set()})
    all_jobs_info = []

    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        job = case["job_name"]
        sm  = case["derived"]["scale_metrics"]
        all_jobs_info.append({
            "job":       job,
            "ROW_SF":    sm.get("ROW_SF"),
            "lot_count": sm.get("lot_count"),
        })
        seen_norms = set()
        for item in p["line_items"]:
            if item.get("is_optional"):
                continue
            desc = item["description"]
            norm = desc.lower().strip().rstrip(".,;:")
            if norm in seen_norms:
                continue
            seen_norms.add(norm)
            if norm_to_data[norm]["original"] is None:
                norm_to_data[norm]["original"] = desc
            norm_to_data[norm]["jobs_present"].add(job)

    total_jobs = len(all_jobs_info)

    row_sf_vals = sorted(j["ROW_SF"]    for j in all_jobs_info if j["ROW_SF"])
    lot_vals    = sorted(j["lot_count"] for j in all_jobs_info if j["lot_count"])
    row_sf_median = statistics.median(row_sf_vals) if row_sf_vals else None
    lot_median    = statistics.median(lot_vals)    if lot_vals    else None

    large_row = {j["job"] for j in all_jobs_info
                 if j["ROW_SF"] and row_sf_median and j["ROW_SF"] > row_sf_median}
    small_row  = {j["job"] for j in all_jobs_info
                  if j["ROW_SF"] and row_sf_median and j["ROW_SF"] <= row_sf_median}
    large_lot  = {j["job"] for j in all_jobs_info
                  if j["lot_count"] and lot_median and j["lot_count"] > lot_median}
    small_lot  = {j["job"] for j in all_jobs_info
                  if j["lot_count"] and lot_median and j["lot_count"] <= lot_median}

    def _cond(jobs_present, bucket):
        n   = len(bucket)
        n_p = len(jobs_present & bucket)
        return {
            "jobs_present": n_p,
            "jobs_total":   n,
            "prevalence":   round(n_p / n, 3) if n > 0 else None,
        }

    result = {
        "_thresholds": {
            "ROW_SF_median":    round(row_sf_median, 0) if row_sf_median else None,
            "lot_count_median": round(lot_median,    1) if lot_median    else None,
            "note": "Splits computed from portfolio medians — auto-update as jobs are added",
        },
    }
    for _norm, data in sorted(
        norm_to_data.items(), key=lambda x: (x[1]["original"] or "").upper()
    ):
        original = data["original"]
        jp       = data["jobs_present"]
        result[original] = {
            "jobs_present": len(jp),
            "jobs_total":   total_jobs,
            "prevalence":   round(len(jp) / total_jobs, 3) if total_jobs > 0 else 0,
            "conditional": {
                "ROW_SF_above_median":          _cond(jp, large_row),
                "ROW_SF_at_or_below_median":    _cond(jp, small_row),
                "lot_count_above_median":       _cond(jp, large_lot),
                "lot_count_at_or_below_median": _cond(jp, small_lot),
            },
        }
    return result


# ---------------------------------------------------------------------------
# Section 10: LS item variance (CV and regression for LS line items)
# ---------------------------------------------------------------------------

def derive_ls_item_variance(cases):
    """
    For LS items with N>=3 jobs, compute coefficient of variation and OLS
    regression of ext_price vs ROW_SF and vs lot_count.
    CV > 0.7 = high uncertainty; CV > 1.0 = analog scaling unreliable.
    """
    from collections import defaultdict

    obs_by_desc = defaultdict(list)
    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        sm = case["derived"]["scale_metrics"]
        for item in p["line_items"]:
            if item.get("is_optional"):
                continue
            if item["unit"] != "LS":
                continue
            if not item["ext_price"] or item["ext_price"] <= 0:
                continue
            obs_by_desc[item["description"]].append({
                "job":         case["job_name"],
                "ext_price":   item["ext_price"],
                "ROW_SF":      sm.get("ROW_SF"),
                "lot_count":   sm.get("lot_count"),
                "fronting_LF": sm.get("fronting_LF"),
            })

    variance = {}
    for desc, obs_list in sorted(obs_by_desc.items()):
        if len(obs_list) < 3:
            continue
        prices = [o["ext_price"] for o in obs_list]
        n      = len(prices)
        mean_p = statistics.mean(prices)
        std_p  = statistics.stdev(prices) if n >= 2 else 0.0
        cv     = round(std_p / mean_p, 3) if mean_p > 0 else None

        def _regress(metric_key):
            pairs = [(o[metric_key], o["ext_price"])
                     for o in obs_list if o[metric_key] is not None]
            if len(pairs) < 3:
                return None
            xs = [pt[0] for pt in pairs]
            ys = [pt[1] for pt in pairs]
            r     = pearson_r(xs, ys)
            slope = linear_slope(xs, ys)
            if r is None or slope is None:
                return None
            return {"slope": round(slope, 4), "r2": round(r ** 2, 3)}

        variance[desc] = {
            "n":              n,
            "mean":           round(mean_p, 2),
            "std":            round(std_p, 2),
            "cv":             cv,
            "min":            round(min(prices), 2),
            "max":            round(max(prices), 2),
            "per_ROW_SF":     _regress("ROW_SF"),
            "per_lot":        _regress("lot_count"),
            "per_fronting_LF": _regress("fronting_LF"),
        }
    return variance


# ---------------------------------------------------------------------------
# Section 11: Item scaling (OLS regression for measurable items)
# ---------------------------------------------------------------------------

def derive_item_scaling(cases):
    """
    For measurable items (N>=3), run OLS regression of qty vs ROW_SF, lot_count,
    and road_LF. Report best-fit formula and r².
    These formulas replace hard-coded ratios in the system prompt — recompute
    automatically by running derive_patterns.py when new jobs are added.
    """
    from collections import defaultdict

    obs_by_desc = defaultdict(list)
    for case in cases:
        p = get_primary_proposal(case)
        if not p:
            continue
        sm = case["derived"]["scale_metrics"]
        for item in p["line_items"]:
            if item.get("is_optional"):
                continue
            if item["unit"] not in MEASURABLE_UNITS:
                continue
            if not item["qty"] or item["qty"] <= 0:
                continue
            obs_by_desc[item["description"]].append({
                "job":         case["job_name"],
                "qty":         item["qty"],
                "unit":        item["unit"],
                "ROW_SF":      sm.get("ROW_SF"),
                "lot_count":   sm.get("lot_count"),
                "road_LF":     sm.get("road_LF"),
                "fronting_LF": sm.get("fronting_LF"),
            })

    scaling = {}
    for desc, obs_list in sorted(obs_by_desc.items()):
        if len(obs_list) < 3:
            continue

        unit = sorted({o["unit"] for o in obs_list})[0]

        best_r2, best_driver, best_slope, best_rmse = None, None, None, None
        driver_results = {}

        for metric in ["ROW_SF", "lot_count", "road_LF", "fronting_LF"]:
            pairs = [(o[metric], o["qty"]) for o in obs_list if o[metric] is not None]
            if len(pairs) < 3:
                continue
            xs = [pt[0] for pt in pairs]
            ys = [pt[1] for pt in pairs]
            r     = pearson_r(xs, ys)
            slope = linear_slope(xs, ys)
            if r is None or slope is None:
                continue
            r2  = r ** 2
            mx  = sum(xs) / len(xs)
            my  = sum(ys) / len(ys)
            intercept = my - slope * mx
            rmse = (sum((y - (slope * x + intercept)) ** 2
                        for x, y in zip(xs, ys)) / len(ys)) ** 0.5
            driver_results[metric] = {
                "r2":    round(r2, 3),
                "slope": round(slope, 5),
                "n":     len(pairs),
                "rmse":  round(rmse, 1),
            }
            if best_r2 is None or r2 > best_r2:
                best_r2, best_driver, best_slope, best_rmse = r2, metric, slope, rmse

        if best_driver is None:
            continue

        secondary = next(
            (m for m, res in driver_results.items()
             if m != best_driver and abs(res["r2"] - best_r2) <= 0.05),
            None,
        )

        entry = {
            "unit":        unit,
            "best_driver": best_driver,
            "r2":          round(best_r2, 3),
            "n":           driver_results[best_driver]["n"],
            "slope":       round(best_slope, 5),
            "formula":     f"qty = {best_driver} × {round(best_slope, 5)}",
            "rmse":        round(best_rmse, 1),
        }
        if secondary:
            entry["secondary_driver"] = secondary
        if best_r2 >= 0.99:
            entry["note"] = "derivation rule — near-perfect fit"

        scaling[desc] = entry

    return scaling


# ---------------------------------------------------------------------------
# Markdown review builder
# ---------------------------------------------------------------------------

def build_patterns_review(pack):
    lines = []
    lines.append("# Patterns Review — Phase 1 M2 Knowledge Pack")
    s = pack["stats"]
    lines.append(f"\nBuilt: {pack['built_at']}  ")
    lines.append(f"Jobs: {s['total_jobs']} total, {s['jobs_with_proposals']} with proposals  ")
    lines.append(f"Date range: {s['date_range']['earliest']} → {s['date_range']['latest']}")

    # --- Market context ---
    mc = pack.get("market_context", {})
    if mc:
        lines.append("\n## Market Context\n")
        lines.append(f"**Market:** {mc['market']}  ")
        lines.append(f"**Cities:** {', '.join(mc['common_cities'])}  ")
        lines.append(f"**Date range:** {mc['date_range']['earliest']} → {mc['date_range']['latest']}  ")
        lines.append(f"**Caveat:** _{mc['caveat']}_  ")
        lines.append(f"**Fuel-sensitive items:** {', '.join(mc['fuel_sensitive_items'])}\n")

    # --- Unit price distributions (measurable, N>=3) ---
    lines.append("\n## $/Unit Distributions — Measurable Items (N≥3)\n")
    lines.append("| Description | Cat | Unit | N | Min | P25 | Median | P75 | Max | Std |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for desc, d in sorted(pack["unit_price_distributions"].items()):
        if not d["reliable"] or not d["is_measurable"]:
            continue
        unit = d["unit"] if isinstance(d["unit"], str) else "/".join(d["unit"])
        cat = d["category"] if isinstance(d["category"], str) else "/".join(d["category"])
        p25 = f"${d['p25']:,.2f}" if d.get("p25") else "—"
        p75 = f"${d['p75']:,.2f}" if d.get("p75") else "—"
        std = f"${d['std']:,.2f}" if d.get("std") else "—"
        lines.append(
            f"| {desc} | {cat} | {unit} | {d['n_jobs']}"
            f" | ${d['min']:,.2f} | {p25} | ${d['median']:,.2f} | {p75}"
            f" | ${d['max']:,.2f} | {std} |"
        )

    # --- LS items (N>=3) ---
    lines.append("\n## LS Item Costs — Per Job (N≥3)\n")
    lines.append("| Description | Cat | N | Min | Median | Max | Std |")
    lines.append("|---|---|---|---|---|---|---|")
    for desc, d in sorted(pack["unit_price_distributions"].items()):
        if not d["reliable"] or d["is_measurable"]:
            continue
        cat = d["category"] if isinstance(d["category"], str) else "/".join(d["category"])
        std = f"${d['std']:,.0f}" if d.get("std") else "—"
        lines.append(
            f"| {desc} | {cat} | {d['n_jobs']}"
            f" | ${d['min']:,.0f} | ${d['median']:,.0f} | ${d['max']:,.0f} | {std} |"
        )

    # --- Item pairs ---
    ip = pack.get("item_pairs", {})
    if ip.get("pairs"):
        lines.append("\n## Item Pairs — Co-occurrence (|r|≥0.85, N≥4)\n")
        lines.append(f"_{ip.get('description', '')}_\n")
        lines.append("| item_a | item_b | r | N shared | co_occur_rate | median_ratio (a/b) |")
        lines.append("|---|---|---|---|---|---|")
        for p in ip["pairs"]:
            lines.append(
                f"| {p['item_a']} | {p['item_b']} | {p['r']:.3f}"
                f" | {p['n_shared_jobs']} | {p['co_occurrence_rate']:.2f}"
                f" | {p['median_ratio_a_to_b']} |"
            )
        lines.append("")

    # --- Derivation rules ---
    lines.append("\n## Derivation Rules\n")
    for rule in pack["derivation_rules"]:
        if rule.get("manual"):
            # hand-authored rules (manual_rules.json) don't share the derived
            # observation schema — render header fields defensively + a generic table
            lines.append(f"### {rule['name']} (manual)")
            lines.append(f"**Formula:** `{rule.get('formula', '—')}`  ")
            lines.append(f"**Support:** {rule.get('observed_support', '—')}  ")
            lines.append(f"**Confidence:** {rule.get('confidence', '—')}  ")
            lines.append(f"**When to apply:** {rule.get('applies_when', '—')}  ")
            lines.append(f"**Exceptions:** {rule.get('exceptions_note', '—')}\n")
            obs = rule.get("observations") or []
            if obs:
                cols = list(obs[0].keys())
                lines.append("| " + " | ".join(cols) + " |")
                lines.append("|" + "---|" * len(cols))
                for o in obs:
                    lines.append("| " + " | ".join(str(o.get(c, "")) for c in cols) + " |")
            lines.append("")
            continue
        lines.append(f"### {rule['name']}")
        lines.append(f"**Formula:** `{rule['formula']}`  ")
        lines.append(f"**Support:** {rule['observed_support']}  ")
        lines.append(f"**Confidence:** {rule['confidence']}  ")
        lines.append(f"**When to apply:** {rule['applies_when']}  ")
        lines.append(f"**Exceptions:** {rule['exceptions_note']}\n")

        # rule-specific table columns
        name = rule["name"]
        if "predicted_qty" in (rule["observations"][0] if rule["observations"] else {}):
            # ratio-vs-lots rules (drywell fix)
            lines.append("| Job | Lots | Actual qty | Predicted qty | Ratio | Match |")
            lines.append("|---|---|---|---|---|---|")
            for o in rule["observations"]:
                lines.append(
                    f"| {o['job']} | {o['lots']} | {o['actual_qty']:.0f}"
                    f" | {o['predicted_qty']} | {o['ratio_actual_to_lots']:.3f}"
                    f" | {'✓' if o['match'] else '✗'} |"
                )
        elif name in ("sewer_services_from_lot_count", "water_services_from_lot_count"):
            lines.append("| Job | Lots | Actual qty | Ratio | Match |")
            lines.append("|---|---|---|---|---|")
            for o in rule["observations"]:
                lines.append(
                    f"| {o['job']} | {o['lots']} | {o['actual_qty']:.0f}"
                    f" | {o['ratio_actual_to_lots']:.3f} | {'✓' if o['match'] else '✗'} |"
                )
        elif name == "sewer_manholes_from_sewer_LF":
            lines.append("| Job | Sewer LF | Actual MH | Predicted (÷250) | Ratio | Match |")
            lines.append("|---|---|---|---|---|---|")
            for o in rule["observations"]:
                lines.append(
                    f"| {o['job']} | {o['sewer_LF_total']:.0f} | {o['sewer_mh_actual']:.0f}"
                    f" | {o['sewer_mh_predicted_at_250']:.1f} | {o['ratio_actual_to_predicted']:.2f}"
                    f" | {'✓' if o['match'] else '✗'} |"
                )
        elif "ratio_to_road_LF" in (rule["observations"][0] if rule["observations"] else {}):
            rs = rule.get("ratio_stats", {})
            lines.append(f"_Ratio stats: median={rs.get('median')}, range=[{rs.get('min')}, {rs.get('max')}]_\n")
            lines.append("| Job | road_LF | Actual LF | Predicted LF | Ratio | Match |")
            lines.append("|---|---|---|---|---|---|")
            for o in rule["observations"]:
                lines.append(
                    f"| {o['job']} | {o['road_LF']} | {o['actual_LF']:.0f}"
                    f" | {o['predicted_LF']:.0f} | {o['ratio_to_road_LF']:.3f}"
                    f" | {'✓' if o['match'] else '✗'} |"
                )
        elif "ratio_to_lot_count" in (rule["observations"][0] if rule["observations"] else {}):
            rs = rule.get("ratio_stats", {})
            lines.append(f"_Ratio stats: median={rs.get('median')}, range=[{rs.get('min')}, {rs.get('max')}]_\n")
            lines.append("| Job | lot_count | Actual LF | Predicted LF | Ratio | Match |")
            lines.append("|---|---|---|---|---|---|")
            for o in rule["observations"]:
                lines.append(
                    f"| {o['job']} | {o['lot_count']} | {o['actual_LF']:.0f}"
                    f" | {o['predicted_LF']:.0f} | {o['ratio_to_lot_count']:.3f}"
                    f" | {'✓' if o['match'] else '✗'} |"
                )
        lines.append("")

    # --- Typical section defaults ---
    lines.append("## Typical Section Defaults\n")
    for jtype, entry in pack["typical_section_defaults"].items():
        lines.append(f"### {jtype} (N={entry['n_jobs']})\n")
        if "stripping_depth_in" in entry:
            sd = entry["stripping_depth_in"]
            obs_str = ", ".join(f"{o['job']}={o['value_in']}\"" for o in sd["observations"])
            lines.append(
                f"**Stripping depth (inches):** median={sd['median']}\", "
                f"range=[{sd['min']}\"–{sd['max']}\"], N={sd['n']}  \n"
                f"_{obs_str}_\n"
            )
        if "row_width_ft" in entry:
            rw = entry["row_width_ft"]
            lines.append(
                f"**Implied ROW width (ft):** median={rw['median']} ft, "
                f"range=[{rw['min']}–{rw['max']}], N={rw['n']}\n"
            )
            lines.append("| Job | ROW_SF | road_LF | Width (ft) |")
            lines.append("|---|---|---|---|")
            for obs in sorted(entry["row_width_ft"]["observations"], key=lambda x: x["implied_width_ft"]):
                lines.append(
                    f"| {obs['job']} | {obs['ROW_SF']:,} | {obs['road_LF']:,} | {obs['implied_width_ft']} |"
                )
        lines.append("")

    # --- Job-type signatures ---
    lines.append("## Job-Type Signatures\n")
    for jtype, sig in pack["job_type_signatures"].items():
        lines.append(f"### {jtype} (N={sig['n_jobs']})\n")
        if sig.get("note"):
            lines.append(f"_{sig['note']}_\n")

        for label, key in [
            ("$/lot", "dollar_per_lot"),
            ("$/road LF", "dollar_per_road_LF"),
            ("$/ROW SF", "dollar_per_ROW_SF"),
        ]:
            d = sig.get(key)
            if not d:
                continue
            p25_s = f"${d['p25']:,.0f}" if d.get("p25") else "—"
            p75_s = f"${d['p75']:,.0f}" if d.get("p75") else "—"
            lines.append(
                f"**{label}:** N={d['n']}, min=${d['min']:,.0f}, "
                f"P25={p25_s}, median=${d['median']:,.0f}, "
                f"P75={p75_s}, max=${d['max']:,.0f}  "
            )
        lines.append("")
        lines.append("| Job | Date | Total | Lots | road LF | $/lot | $/road LF |")
        lines.append("|---|---|---|---|---|---|---|")
        for j in sig["jobs"]:
            total_s = f"${j['total']:,.0f}" if j.get("total") else "—"
            dpl = round(j["total"] / j["lot_count"], 0) if j.get("total") and j.get("lot_count") else None
            dplf = round(j["total"] / j["road_LF"], 0) if j.get("total") and j.get("road_LF") else None
            dpl_s = f"${dpl:,.0f}" if dpl is not None else "—"
            dplf_s = f"${dplf:,.0f}" if dplf is not None else "—"
            lines.append(
                f"| {j['job']} | {j['date']} | {total_s}"
                f" | {j.get('lot_count', '—')} | {j.get('road_LF', '—')} | {dpl_s} | {dplf_s} |"
            )
        lines.append("")

    # --- LS earthwork rates ---
    lines.append("## LS Earthwork Rates (Qty-Sheet-Backed $/CY)\n")
    for key, entry in pack.get("ls_earthwork_rates", {}).items():
        rd = entry.get("rate_distribution")
        if not rd:
            continue
        lines.append(f"### {key}\n")
        lines.append(f"**Formula:** `{entry['formula']}`  ")
        if entry.get("note"):
            lines.append(f"**Note:** {entry['note']}  ")
        p25_s = f"${rd['p25']:,.2f}" if rd.get("p25") else "—"
        p75_s = f"${rd['p75']:,.2f}" if rd.get("p75") else "—"
        lines.append(
            f"**Rate:** N={rd['n']}, min=${rd['min']:,.2f}, P25={p25_s}, "
            f"median=${rd['median']:,.2f}, P75={p75_s}, max=${rd['max']:,.2f}/CY\n"
        )
        lines.append("| Job | ROW_SF | Depth | CY | Cost | $/CY | Note |")
        lines.append("|---|---|---|---|---|---|---|")
        for o in entry["observations"]:
            note = o.get("depth_note", "")
            lines.append(
                f"| {o['job']} | {o['ROW_SF']:,} | {o['depth_in']}\" "
                f"| {o['CY']:,} | ${o['cost']:,.0f} | ${o['per_CY']:,.2f} | {note} |"
            )
        lines.append("")

    # --- Paving & subgrade rates ---
    if pack.get("paving_rates"):
        lines.append("## Paving & Subgrade Rates ($/SY)\n")
        for key, entry in pack["paving_rates"].items():
            rd = entry.get("rate_distribution")
            if not rd:
                continue
            lines.append(f"### {key}\n")
            lines.append(f"**{entry['description']}**  ")
            if entry.get("note"):
                lines.append(f"_{entry['note']}_  ")
            p25_s = f"${rd['p25']:,.2f}" if rd.get("p25") else "—"
            p75_s = f"${rd['p75']:,.2f}" if rd.get("p75") else "—"
            lines.append(
                f"**Rate:** N={rd['n']}, min=${rd['min']:,.2f}, P25={p25_s}, "
                f"median=${rd['median']:,.2f}, P75={p75_s}, max=${rd['max']:,.2f}/SY\n"
            )
            obs = entry.get("observations", [])
            if obs:
                qty_key = [k for k in obs[0] if k not in ("job", "cost", "per_SY")][0] if obs else "qty"
                lines.append(f"| Job | {qty_key} | Cost | $/SY |")
                lines.append("|---|---|---|---|")
                for o in obs:
                    qty_val = o.get(qty_key, "—")
                    lines.append(
                        f"| {o['job']} | {qty_val:,} | ${o['cost']:,.0f} | ${o['per_SY']:,.2f} |"
                    )
            lines.append("")

    # --- Qty/scale correlations ---
    qsc = pack.get("qty_scale_correlations", {})
    if qsc:
        lines.append("## Item Qty vs Scale Metric Correlations (Pearson r)\n")
        lines.append(f"_{qsc.get('description', '')}_\n")
        lines.append(f"> **Analog retrieval:** {qsc.get('analog_retrieval_note', '')}\n")
        lines.append("| Item | N | best driver | best r | r_lot_count | r_road_LF | r_ROW_SF | med_ratio (best) |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for desc, entry in qsc.get("correlations", {}).items():
            bd = entry.get("best_driver", "")
            br = entry.get("best_r")
            br_s = f"{br:.3f}" if br is not None else "—"
            def _r(m):
                v = entry.get(m, {}).get("r")
                return f"{v:.3f}" if v is not None else "—"
            med = entry.get(bd, {}).get("median_ratio") if bd else None
            med_s = f"{med:.4f}" if med is not None else "—"
            lines.append(
                f"| {desc} | {entry['n']} | {bd} | {br_s}"
                f" | {_r('lot_count')} | {_r('road_LF')} | {_r('ROW_SF')} | {med_s} |"
            )
        lines.append("")

    # --- Price trends ---
    pt = pack.get("price_trends", {})
    if pt:
        lines.append("## Unit Price Trends 2023–2026\n")
        lines.append(f"_{pt.get('description', '')}_\n")
        lines.append("| Item | Unit | N | r | $/yr | %/yr | dir | recency_weight |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for desc, t in pt.get("trends", {}).items():
            rw = "**yes**" if t["recency_weight"] else "no"
            lines.append(
                f"| {desc} | {t['unit']} | {t['n']} | {t['r']:.3f}"
                f" | {t['annual_change_per_unit']:+.2f} | {t['annual_pct_change']:+.1f}%"
                f" | {t['direction']} | {rw} |"
            )
        lines.append("")

    # --- Item prevalence ---
    ip = pack.get("item_prevalence", {})
    thresh = ip.get("_thresholds", {})
    if ip:
        lines.append("## Item Prevalence (≥50% of portfolio)\n")
        lines.append(
            f"_Median splits: ROW_SF={thresh.get('ROW_SF_median'):,} SF, "
            f"lot_count={thresh.get('lot_count_median')}_\n"
        )
        lines.append("| Item | N | Total | Overall | ROW_SF>med | ROW_SF≤med | lots>med | lots≤med |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for desc, entry in sorted(ip.items(), key=lambda x: -x[1].get("prevalence", 0)
                                   if not x[0].startswith("_") else 2):
            if desc.startswith("_") or entry["prevalence"] < 0.5:
                continue
            c = entry["conditional"]
            def _p(key):
                v = c.get(key, {}).get("prevalence")
                return f"{v:.2f}" if v is not None else "—"
            lines.append(
                f"| {desc} | {entry['jobs_present']} | {entry['jobs_total']}"
                f" | {entry['prevalence']:.2f}"
                f" | {_p('ROW_SF_above_median')} | {_p('ROW_SF_at_or_below_median')}"
                f" | {_p('lot_count_above_median')} | {_p('lot_count_at_or_below_median')} |"
            )
        lines.append("")

    # --- LS item variance ---
    lsv = pack.get("ls_item_variance", {})
    if lsv:
        lines.append("## LS Item Variance (CV)\n")
        lines.append("_CV < 0.3 = low; CV 0.3–0.7 = moderate; CV > 0.7 = high; CV > 1.0 = analog scaling unreliable_\n")
        lines.append("| Item | N | Mean | Std | CV | Min | Max | ROW_SF r² | lot r² |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for desc, v in sorted(lsv.items(), key=lambda x: -(x[1]["cv"] or 0)):
            r2_row = (v.get("per_ROW_SF") or {}).get("r2", "—")
            r2_lot = (v.get("per_lot")    or {}).get("r2", "—")
            cv_tag = ("**HIGH**" if v["cv"] and v["cv"] > 0.7
                      else "mod" if v["cv"] and v["cv"] > 0.3 else "low")
            lines.append(
                f"| {desc} | {v['n']} | ${v['mean']:,.0f} | ${v['std']:,.0f}"
                f" | {v['cv']} ({cv_tag})"
                f" | ${v['min']:,.0f} | ${v['max']:,.0f}"
                f" | {r2_row} | {r2_lot} |"
            )
        lines.append("")

    # --- Item scaling ---
    iscaling = pack.get("item_scaling", {})
    if iscaling:
        lines.append("## Item Scaling — OLS Regressions (N≥3)\n")
        lines.append("| Item | Unit | Best Driver | r² | N | Slope | Formula | RMSE | Secondary |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for desc, entry in sorted(iscaling.items(), key=lambda x: -x[1]["r2"]):
            secondary = entry.get("secondary_driver", "—")
            lines.append(
                f"| {desc} | {entry['unit']} | {entry['best_driver']} | {entry['r2']:.3f}"
                f" | {entry['n']} | {entry['slope']}"
                f" | `{entry['formula']}` | {entry['rmse']}"
                f" | {secondary} |"
            )
        lines.append("")

    lines.append("---\n_Generated by `phase1/knowledge_pack/derive_patterns.py`_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _merge_manual_rules(pack):
    """Fold hand-authored rules from manual_rules.json into the pack (in place).
    These rules are not derivable from the case library, so every generated pack
    (static or per-run dynamic) must carry them or they silently vanish."""
    if not MANUAL_RULES_PATH.exists():
        return
    manual = json.loads(MANUAL_RULES_PATH.read_text(encoding="utf-8"))
    rules = pack.setdefault("derivation_rules", [])
    existing = {r.get("name") for r in rules}
    added = [r for r in manual.get("derivation_rules", []) if r.get("name") not in existing]
    for r in added:
        r.setdefault("manual", True)
    rules.extend(added)
    if added:
        print(f"  merged {len(added)} manual rule(s): {', '.join(r['name'] for r in added)}")


def build_pack(cases):
    """Derive the full knowledge pack dict from a list of case records."""
    print("Deriving unit price distributions...")
    distributions = derive_unit_price_distributions(cases)
    n_reliable = sum(1 for d in distributions.values() if d["reliable"])
    n_measurable = sum(1 for d in distributions.values() if d["reliable"] and d["is_measurable"])
    print(f"  {len(distributions)} unique descriptions, {n_reliable} with N>=3 ({n_measurable} measurable)")

    print("Checking derivation rules...")
    rules = derive_derivation_rules(cases)
    for r in rules:
        print(f"  {r['name']}: {r['observed_support']} ({r['confidence']})")

    print("Computing typical section defaults...")
    defaults = derive_typical_section_defaults(cases)

    print("Computing job-type signatures...")
    signatures = derive_job_type_signatures(cases)

    print("Deriving LS earthwork rates...")
    ls_rates = derive_ls_earthwork_rates(cases)
    for key, val in ls_rates.items():
        n = val["rate_distribution"]["n"] if val["rate_distribution"] else 0
        med = val["rate_distribution"]["median"] if val["rate_distribution"] else "—"
        print(f"  {key}: N={n}, median=${med}/CY")

    print("Deriving earthwork balance prior...")
    earthwork_balance_prior = derive_earthwork_balance_prior(cases)
    for job_type, entry in earthwork_balance_prior.items():
        thin = " (thin data)" if entry["thin_data"] else ""
        print(f"  {job_type}: N={entry['n']}, dominant={entry['dominant_outcome']} "
              f"({entry['dominant_share']:.0%}){thin}")

    print("Deriving WA tax scope (Section 6b)...")
    wa_tax_scope = derive_wa_tax_scope(cases)
    for state, entry in wa_tax_scope.items():
        print(f"  {state}: {len(entry['items'])} taxed item(s), "
              f"typical_rate={entry['_typical_rate']}, n_jobs={entry['_n_jobs_with_taxed_items']}")

    print("Deriving paving & subgrade rates...")
    paving_rates = derive_paving_rates(cases)
    for key, val in paving_rates.items():
        rd = val.get("rate_distribution") or {}
        print(f"  {key}: N={rd.get('n', 0)}, median=${rd.get('median', '—')}/SY")

    print("Deriving item pairs (co-occurrence, Section 5)...")
    item_pairs = derive_item_pairs(cases)
    print(f"  {len(item_pairs['pairs'])} pairs with |r|>=0.85, N>=4")

    print("Deriving qty/scale correlations (Section 7)...")
    qty_correlations = derive_qty_scale_correlations(cases)
    n_corr = len(qty_correlations.get("correlations", {}))
    print(f"  {n_corr} items with N>=4")

    print("Deriving price trends (Section 8)...")
    price_trends = derive_price_trends(cases)
    n_trends = len(price_trends.get("trends", {}))
    rw = sum(1 for t in price_trends.get("trends", {}).values() if t["recency_weight"])
    print(f"  {n_trends} items with N>=6, {rw} warrant recency-weighting")

    print("Deriving item prevalence (Section 9)...")
    item_prevalence = derive_item_prevalence(cases)
    n_items = len([k for k in item_prevalence if not k.startswith("_")])
    thresh = item_prevalence.get("_thresholds", {})
    print(f"  {n_items} unique descriptions; ROW_SF median={thresh.get('ROW_SF_median')}, lot median={thresh.get('lot_count_median')}")

    print("Deriving LS item variance (Section 10)...")
    ls_item_variance_data = derive_ls_item_variance(cases)
    n_high_cv = sum(1 for v in ls_item_variance_data.values() if v["cv"] and v["cv"] > 0.7)
    print(f"  {len(ls_item_variance_data)} LS items with N>=3, {n_high_cv} with CV>0.7")

    print("Deriving item scaling regressions (Section 11)...")
    item_scaling_data = derive_item_scaling(cases)
    n_near_perfect = sum(1 for v in item_scaling_data.values() if v.get("note") == "derivation rule — near-perfect fit")
    print(f"  {len(item_scaling_data)} measurable items with N>=3, {n_near_perfect} near-perfect (r²≥0.99)")

    print("Building knowledge pack...")
    dates = [
        p["proposal_date"]
        for case in cases
        if (p := get_primary_proposal(case)) and p.get("proposal_date")
    ]
    pack = {
        "schema_version": "1.0",
        "built_at": str(date.today()),
        "description": "Phase 1 M2 knowledge pack — derived patterns from case library",
        "market_context": {
            **MARKET_CONTEXT,
            "date_range": {
                "earliest": min(dates) if dates else None,
                "latest": max(dates) if dates else None,
            },
        },
        "stats": {
            "total_jobs": len(cases),
            "jobs_with_proposals": sum(1 for c in cases if get_primary_proposal(c)),
            "date_range": {
                "earliest": min(dates) if dates else None,
                "latest": max(dates) if dates else None,
            },
        },
        "unit_price_distributions": distributions,
        "item_pairs": item_pairs,
        "derivation_rules": rules,
        "typical_section_defaults": defaults,
        "job_type_signatures": signatures,
        "ls_earthwork_rates": ls_rates,
        "earthwork_balance_prior": earthwork_balance_prior,
        "wa_tax_scope": wa_tax_scope,
        "paving_rates": paving_rates,
        "qty_scale_correlations": qty_correlations,
        "price_trends": price_trends,
        "item_prevalence": item_prevalence,
        "ls_item_variance": ls_item_variance_data,
        "item_scaling": item_scaling_data,
    }
    _merge_manual_rules(pack)
    return pack


def build_kp_for_jobs(job_names, out_dir, cases=None, case_dir=None):
    """Build a knowledge pack scoped to `job_names` (dynamic per-run KP).
    Writes knowledge_pack.json + patterns_review.md into `out_dir` and returns
    the knowledge_pack.json path.

    `cases`, when given, is the full set of case records (e.g. loaded from the DB
    by the app — TODO_YY); it is filtered to `job_names` here. When omitted, the
    records are read from the on-disk case library (CLI / standalone use)."""
    if cases is not None:
        wanted = {j.lower() for j in job_names}
        cases = [c for c in cases if (c.get("job_name") or "").lower() in wanted]
    else:
        cases = load_cases(include_jobs=list(job_names), case_dir=case_dir)
    if not cases:
        raise ValueError(f"no case library entries match selection: {sorted(job_names)}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pack = build_pack(cases)
    kp_path = out_dir / "knowledge_pack.json"
    kp_path.write_text(json.dumps(pack, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "patterns_review.md").write_text(build_patterns_review(pack), encoding="utf-8")
    return kp_path


def main():
    parser = argparse.ArgumentParser(description="Derive knowledge pack from case library")
    parser.add_argument(
        "--holdout", default=None, metavar="FOLDER",
        help="Data folder of the job to exclude (e.g. 'data/26001-1_WOODMAN' or '26001-1_WOODMAN'). "
             "Output goes to knowledge_pack/holdout_<folder>/ so the main pack is not overwritten.",
    )
    parser.add_argument(
        "--out-dir", default=None, metavar="DIR",
        help="Override output directory (default: knowledge_pack/ or knowledge_pack/holdout_<folder>/)",
    )
    parser.add_argument(
        "--from-db", action="store_true",
        help="Read the case library from the DB-backed store (TODO_YY) instead of the "
             "on-disk JSON seed files, so the static pack reflects jobs edited in the brain UI.",
    )
    args = parser.parse_args()

    # Resolve holdout job name from folder path
    holdout_job_name = None
    holdout_slug = None
    if args.holdout:
        holdout_job_name, holdout_slug = resolve_holdout(args.holdout)

    # Resolve output directory
    if args.out_dir:
        out_dir = Path(args.out_dir)
    elif holdout_slug:
        out_dir = BASE_OUTPUT_DIR / f"holdout_{holdout_slug}"
    else:
        out_dir = BASE_OUTPUT_DIR

    out_dir.mkdir(parents=True, exist_ok=True)
    kp_path     = out_dir / "knowledge_pack.json"
    review_path = out_dir / "patterns_review.md"

    if holdout_job_name:
        print(f"Holdout mode: excluding '{holdout_job_name}'")
        print(f"Output dir:   {out_dir}/\n")

    print("Loading case library..." + (" (from DB)" if args.from_db else ""))
    if args.from_db:
        cases = load_cases_from_db(holdout_job_name=holdout_job_name)
    else:
        cases = load_cases(holdout_job_name=holdout_job_name)
    print(f"  {len(cases)} cases loaded")

    pack = build_pack(cases)

    kp_path.write_text(json.dumps(pack, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Written: {kp_path}")

    print("Writing patterns review...")
    review = build_patterns_review(pack)
    review_path.write_text(review, encoding="utf-8")
    print(f"  Written: {review_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()