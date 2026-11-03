# Changelog

## 0.5.0 — 2026-11-03

- **Commute estimates in the ranker.** When `commute_targets` in preferences has an address (for example a workplace), the ranker now geocodes it with the keyless Census geocoder and prints a driving estimate for every listing it presents: OSRM road-network time and distance, cached per building. The estimate carries no live traffic, says so in the output, and is flagged when it exceeds the target's `max_minutes`; it never excludes a listing. Listings without coordinates, or a target that will not geocode, are skipped cleanly. Verified live: East Palo Alto listings to a work address across the bay came back at roughly 22 to 26 minutes (12.7 to 14.6 miles) off-peak.
- 47 tests (4 new for the geocoding and routing primitives).

## 0.4.0 — 2026-11-03

- **HUD FMR provider fixed to the real API, and connected through Muse.** Live testing caught a design error that dated back to v0.2 (which was only ever mock-tested): the provider passed the listing's ZIP code as the HUD entity id, but the HUD API has no by-ZIP endpoint and rejects it with HTTP 400. The provider now resolves the listing's county from its coordinates (Census geocoder, keyless), queries that county's FMR entity, picks the listing's own ZIP row when the area publishes small-area (per-ZIP) figures, and parses figures that arrive as strings. It also accepts the HUD token from Muse (Secure Vault, `custom.hud`) when no raw `HUD_API_TOKEN` is set. Verified live: a San Mateo County listing enriched with the San Francisco HUD Metro FMR Area two-bedroom figure of $3,697.
- 43 tests (HUD tests rewritten for the real flow, plus a small-area ZIP-row test).

## 0.3.1 — 2026-11-03

- The HasData Zillow source now also works with a **credential connected in Muse** (Secure Vault, `custom.hasdata`), completing the pattern: all three keyed sync sources (RentCast, RealtyAPI, HasData) accept either a raw key in the env/secrets file or the Muse vault credential, picked up automatically. Verified live the day it shipped: 15 Zillow listings stored on the first vault-backed sync.
- 41 tests (1 new for the HasData connector fallback).

## 0.3.0 — 2026-11-03

- **New source: Apartments.com via RealtyAPI** (`kind: "realtyapi-apartments"`), the first real Apartments.com search feed for the tool (previously only pasted links). One credit per location search; free tier is 250 credits/month. Works with a raw `REALTYAPI_API_KEY` or with RealtyAPI connected in Muse (Secure Vault, `custom.realtyapi`), picked up automatically. Verified live the day it shipped: 50 listings stored from one ZIP search.
- Honest data shape: RealtyAPI search results are building-level placards, so rent records the low end of the building's rent range and beds/baths/sqft stay unknown (missing facts never exclude in the ranker). Unit-level detail sits behind RealtyAPI's `/details/*` endpoints, keyed by `listingKey`, and is not wired yet.
- The connector is registered for six US portal hosts (apartments, realtor, redfin, zillow, trulia, homes), so the other portals can be wired later without re-entering the key.
- 40 tests (5 new for the RealtyAPI source).

## 0.2.1 — 2026-11-03

- RentCast now also works with a **credential connected in Muse** (Secure Vault, `custom.rentcast`), not just a raw key in the environment or secrets file — the same vault-fallback pattern hiddenjob uses for JSearch. Sync and the `rentcast-avm` enrichment try the raw key first, then the connector CLI at `~/workspace/custom-connectors/custom.rentcast/bin/rentcast_api.py`. Nothing about the ledger or config changes; connect the key in Muse and the next sync picks it up.
- The connector CLI surfaces the provider's own error body, so a rejected call explains itself (for example `billing/subscription-inactive`, which means the key is valid but the RentCast account has no active API plan — subscribe the free Developer plan on the API dashboard; do not create a new key).
- 35 tests (3 new for the connector fallback).

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
