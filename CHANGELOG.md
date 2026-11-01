# Changelog

## 0.2.0 — 2026-11-01

**The option layer: every API beyond the core sources is wired in, optional, and off until you add a free key.**

- New source: **HasData Zillow** (`kind: "hasdata-zillow"`). Real Zillow for-rent search with beds/price filters, returned as structured records — the answer to portal bot-walls. Free key (1,000 credits/month, no card); skipped with a note when no `HASDATA_API_KEY` is set.
- New command: **`apartmentfinder.py enrich`** adds per-listing context from six providers, each skipping cleanly when its key or the listing's coordinates are missing:
  - `walkscore` — Walk / Transit / Bike scores (free key, 5,000 calls/day).
  - `rentcast-avm` — RentCast market rent estimate, using the listings key you already have.
  - `hud-fmr` — HUD Fair Market Rent for the listing's ZIP and bedroom count (free HUD token).
  - `census-acs` — census-tract median gross rent and household income (keyless).
  - `fema-flood` — FEMA flood zone for the coordinates (keyless).
  - `airnow` — current air quality at the listing (free key).
- Enrichment is stored as provider payloads in a new `enrichment` table, with walk scores, rent estimate, FMR, and flood zone also denormalized onto the listing. Existing 0.1 ledgers migrate in place (new columns added automatically).
- Ranker: new hard constraint `location_quality.min_walk_score` (unknown scores never exclude), walkability scoring, and a rent-vs-estimate signal — listings priced more than 10% over the RentCast estimate lose points and carry the reason; listings under estimate gain points.
- `listings` and `report` surface walk scores, estimates, and enrichment coverage.
- 32 tests (12 new), all standard library.

Not live-tested against real API keys: the integrations are covered by mocked-response tests, and each provider reports its own error as a note rather than failing the run. Add keys to `~/.config/apartmentfinder.env` whenever you want a provider live.

## 0.1.0 — 2026-11-01

Initial release. RentCast listings API + optional Realtor.com via RapidAPI + manual URL import (only URLs retained), SHA-256 evidence capture, SQLite tracking ledger (interested → contacted → tour → applied → leased), and the preferences/constraints ranker. Private config and ledger gitignored; keys live outside the repo.
