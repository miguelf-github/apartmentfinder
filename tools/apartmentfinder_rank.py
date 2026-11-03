#!/usr/bin/env python3
"""Rank captured rental listings against the person's preferences.

Mirrors tools/hiddenjob_rank.py: a gate layer and a score layer.

- Gate (hard constraints from preferences.json): max_rent, min bedrooms /
  bathrooms / sqft, allowed property types, saved locations, deal-breaker
  keywords, and must-have keywords when must_have_strict is on. A listing
  that fails any gate is `excluded` — with the reason kept, so a human can
  override a bad gate instead of never seeing the place.
- Score (soft preferences): rent headroom under target, space value
  ($/sqft), layout fit, freshness, and nice-to-have keyword hits.
- Commute (when preferences set commute_targets with an address): each
  printed listing gets a driving estimate to each target, geocoded with
  the Census geocoder and routed with OSRM. It is a road-network
  estimate with no live traffic, shown for comparison and flagged when
  over the target's max_minutes; it never excludes a listing.

A missing fact never excludes: a listing with unknown beds is not punished
for a min-beds gate it might pass; it is scored on what is known and the
gap is shown. That is the rental version of hiddenjob's rule that a blocked
source is unmeasurable, not empty.

Usage:
  python3 tools/apartmentfinder_rank.py [--config config/targets.json]
                                   [--preferences config/preferences.json]
                                   [--tier strong|good|weak|excluded]
                                   [--limit 50]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import apartmentfinder  # noqa: E402


def _contains(text: str, phrase: str) -> bool:
    return phrase.strip().lower() in text


def classify(listing: dict, prefs: dict) -> dict:
    """Return {tier, score, reasons, gaps} for one listing row."""
    reasons: list[str] = []
    gaps: list[str] = []
    text = (str(listing.get("description") or "") + " " + str(listing.get("title") or "")).lower()

    budget = prefs.get("budget") or {}
    max_rent = budget.get("max_rent")
    target_rent = budget.get("target_rent")
    layout = prefs.get("layout") or {}
    rent, beds, baths, sqft = (listing.get("rent"), listing.get("beds"),
                               listing.get("baths"), listing.get("sqft"))

    # --- Gates -------------------------------------------------------------
    if max_rent is not None:
        if rent is None:
            gaps.append("rent unknown vs budget")
        elif rent > max_rent:
            reasons.append(f"rent ${rent:,.0f} over max ${max_rent:,.0f}")
    for key, label, value in (("min_bedrooms", "bedrooms", beds),
                              ("min_bathrooms", "bathrooms", baths),
                              ("min_sqft", "sqft", sqft)):
        want = layout.get(key)
        if want is not None:
            if value is None:
                gaps.append(f"{label} unknown")
            elif value < want:
                reasons.append(f"{label} {value:g} below min {want:g}")
    allowed_types = [str(t).lower() for t in (layout.get("property_types") or [])]
    ptype = str(listing.get("property_type") or "").lower()
    if allowed_types and ptype and ptype not in allowed_types:
        reasons.append(f"property type '{listing.get('property_type')}' not in allowed types")
    locations = prefs.get("locations") or []
    if locations:
        city = str(listing.get("city") or "").lower()
        zip_code = str(listing.get("zip") or "")
        matched = False
        for loc in locations:
            if not isinstance(loc, dict):
                continue
            if loc.get("zip") and zip_code == str(loc["zip"]):
                matched = True
            if loc.get("city") and city == str(loc["city"]).lower():
                matched = True
        if (city or zip_code) and not matched:
            reasons.append("outside saved locations")
        elif not city and not zip_code:
            gaps.append("location unknown")
    for phrase in prefs.get("deal_breakers") or []:
        if phrase and _contains(text, str(phrase)):
            reasons.append(f"deal-breaker present: '{phrase}'")
    quality = prefs.get("location_quality") or {}
    min_walk = quality.get("min_walk_score")
    walk = listing.get("walk_score")
    if min_walk is not None:
        if walk is None:
            gaps.append("walk score unknown")
        elif walk < min_walk:
            reasons.append(f"walk score {walk:g} below min {min_walk:g}")
    must_have = [str(m) for m in (prefs.get("must_have") or []) if m]
    missing_must = [m for m in must_have if not _contains(text, m)]
    if missing_must and prefs.get("must_have_strict"):
        reasons.append("missing must-have(s): " + ", ".join(missing_must))

    if reasons:
        return {"tier": "excluded", "score": 0, "reasons": reasons, "gaps": gaps}

    # --- Score --------------------------------------------------------------
    score = 50
    if rent is not None and target_rent:
        if rent <= target_rent:
            score += 20
        elif max_rent and rent <= max_rent:
            score += 8
        else:
            score -= min(15, int((rent - (max_rent or target_rent)) / 100))
    if rent is not None and sqft:
        ppsf = rent / sqft
        if ppsf < 1.5:
            score += 12
        elif ppsf < 2.5:
            score += 6
        elif ppsf > 4:
            score -= 6
    min_beds = layout.get("min_bedrooms")
    if beds is not None and min_beds is not None and beds > min_beds:
        score += 4  # a spare room is worth something, not everything
    dom = listing.get("days_on_market")
    if isinstance(dom, (int, float)):
        if dom <= 3:
            score += 8
        elif dom <= 14:
            score += 4
        elif dom > 60:
            score -= 4  # stale: may be gone or have a catch; still shown
    for phrase in prefs.get("nice_to_have") or []:
        if phrase and _contains(text, str(phrase)):
            score += 5
    score -= 6 * len(missing_must)  # non-strict must-haves still cost points
    if walk is not None:
        if walk >= 80:
            score += 8
        elif walk >= 60:
            score += 4
        elif walk < 40:
            score -= 4
    estimate = listing.get("rent_estimate")
    over_note = ""
    if rent is not None and estimate:
        if rent > estimate * 1.10:
            score -= 10
            over_note = f"listed {round((rent / estimate - 1) * 100):g}% over RentCast estimate ${estimate:,.0f}"
        elif rent < estimate * 0.95:
            score += 8
    score = max(0, min(100, score))
    tier = "strong" if score >= 70 else "good" if score >= 45 else "weak"
    notes = ([f"missing must-have(s): {', '.join(missing_must)}"] if missing_must else []) + gaps
    if over_note:
        notes.append(over_note)
    return {"tier": tier, "score": score, "reasons": notes, "gaps": gaps}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(ROOT / "config/targets.json"))
    ap.add_argument("--preferences", default="")
    ap.add_argument("--tier", choices=("strong", "good", "weak", "excluded"))
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    cfg_path = Path(args.config).resolve()
    cfg = apartmentfinder.load_config(cfg_path)
    if args.preferences:
        prefs = apartmentfinder.load_json(Path(args.preferences), "Preferences")
    else:
        prefs = apartmentfinder.load_preferences(cfg, cfg_path)
    if not prefs:
        print("no preferences found: copy config/preferences.example.json to "
              "config/preferences.json and fill in your constraints first", file=sys.stderr)
        return 2
    root = apartmentfinder.data_root(cfg, cfg_path)
    con = apartmentfinder.database(root)
    rows = [dict(r) for r in con.execute("SELECT * FROM listings ORDER BY rent IS NULL, rent ASC")]
    con.close()

    ranked = []
    for row in rows:
        result = classify(row, prefs)
        if args.tier and result["tier"] != args.tier:
            continue
        ranked.append((result, row))
    ranked.sort(key=lambda pair: (-pair[0]["score"], pair[1].get("rent") or 1e18))

    targets = []
    for t in prefs.get("commute_targets") or []:
        if not isinstance(t, dict) or not str(t.get("address") or "").strip():
            continue
        try:
            t_lat, t_lon = apartmentfinder.geocode_address(str(t["address"]))
        except apartmentfinder.FetchBlocked as exc:
            print(f"commute: {exc}; target skipped", file=sys.stderr)
            continue
        targets.append({"label": str(t.get("label") or t["address"]), "lat": t_lat,
                        "lon": t_lon, "max_minutes": t.get("max_minutes")})
    commute_cache: dict = {}

    for result, row in ranked[: args.limit]:
        rent = f"${row['rent']:,.0f}/mo" if row.get("rent") is not None else "$?/mo"
        layout_bits = " ".join(b for b in [
            f"{row['beds']:g}bd" if row.get("beds") is not None else "",
            f"{row['baths']:g}ba" if row.get("baths") is not None else "",
            f"{row['sqft']:,.0f}sqft" if row.get("sqft") else "",
            f"walk {row['walk_score']:g}" if row.get("walk_score") is not None else "",
            f"est ${row['rent_estimate']:,.0f}" if row.get("rent_estimate") else ""] if b)
        print(f"[{result['tier']:8} {result['score']:3}] {rent:12} {layout_bits:18} "
              f"{row.get('title') or ''} ({row.get('source')})")
        for t in targets:
            if row.get("latitude") is None or row.get("longitude") is None:
                continue
            cache_key = (round(row["latitude"], 4), round(row["longitude"], 4), t["label"])
            if cache_key not in commute_cache:
                try:
                    commute_cache[cache_key] = apartmentfinder.osrm_drive(
                        row["latitude"], row["longitude"], t["lat"], t["lon"])
                except apartmentfinder.FetchBlocked:
                    commute_cache[cache_key] = None
            est = commute_cache[cache_key]
            if est:
                bit = (f"commute: ~{est['minutes']:.0f} min drive "
                       f"({est['miles']:.1f} mi) to {t['label']}")
                if t["max_minutes"] is not None and est["minutes"] > t["max_minutes"]:
                    bit += f" (over max {t['max_minutes']:g})"
                print(f"           - {bit}")
        for reason in result["reasons"]:
            print(f"           - {reason}")
    print(f"\n{len(ranked)} listings ranked"
          + (f" (tier={args.tier})" if args.tier else "")
          + f" of {len(rows)} captured"
          + ("; commute = OSRM driving estimate, no live traffic" if targets else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
