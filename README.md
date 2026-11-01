# apartmentfinder

> ## Give this prompt to Muse
>
> ```text
> Set up this public apartmentfinder repository as my personal, private local apartment-discovery and tracking assistant.
>
> I will provide my search preferences and constraints in this private Muse conversation: locations, max rent, bedrooms/bathrooms, must-haves, deal-breakers, pets, move-in window. Create `config/preferences.json`, `config/profile.json`, and `config/automation.json` from the repository examples. Keep all of them, captured listings, evidence, and the ledger out of Git. Never use someone else's preferences or profile.
>
> First read `README.md`, `MUSE_SETUP_PROMPT.md`, and `skills/apartmentfinder/SKILL.md`. Add my RentCast API key (free at rentcast.io/api) only through the environment or `~/.config/apartmentfinder.env` (chmod 600), never into the repo. Or run keyless: I paste Zillow/Apartments.com links and you import those instead.
>
> Run discovery, evidence capture, ranking against my constraints, staging, and tracking automatically under the hands-off-with-final-approval policy in `config/automation.json`. Respect the RentCast free-plan budget: one API call per saved location per sync.
>
> Stop for any landlord/property-manager message, tour booking, account creation, password, one-time code, CAPTCHA, application, payment, lease, or acceptance of terms. Record `applied` or `leased` only after I confirm it actually happened. Treat blocked or incomplete sources as unmeasurable, not empty.
> ```
>
> ### Muse invitation
>
> Check out [Muse, your personal AI agent](https://muse.ai/join). Redeem this code in **Settings** within 48 hours of joining and we'll both get **1 billion Muse tokens**:
>
> Code: `YH7I4K`

The rental twin of [hiddenjob](https://github.com/miguelf-github/hiddenjob): a private, configuration-first toolkit for discovering apartments and houses for rent, preserving the source evidence, ranking listings against your own preferences and hard constraints, and organizing a review-first tracking ledger from first interest through tour, application, and lease.

It deliberately does **not** contact landlords or property managers, submit applications, create accounts, solve CAPTCHAs, or make payments. A blocked source is recorded as *unmeasurable*, never as "no apartments exist".

## What it does

- Pulls active long-term rental listings from the **RentCast API** (`/listings/rental/long-term`) — one call per saved location per sync, because the free Developer plan is 50 calls/month. Optionally pulls Realtor.com data via the RapidAPI Realty-in-US source.
- Optionally searches **Zillow directly through HasData** (free key, 1,000 credits/month), which returns structured for-rent records instead of fighting Zillow's bot wall.
- **Enriches any captured listing** (`apartmentfinder.py enrich`) with Walk/Transit/Bike scores, a RentCast market-rent estimate, HUD Fair Market Rent for its ZIP, census-tract median rent and income, FEMA flood zone, and current air quality. Every provider is optional and off until you add its free key; the Census and FEMA providers need no key at all. See `CHANGELOG.md` for the 0.2 details.
- Imports direct listing URLs you paste in (Zillow, Apartments.com, Redfin, a landlord's own page) and keeps only each URL from JSON/JSONL exports, exactly like hiddenjob's importer. Portal pages often bot-wall automated fetches (Zillow uses PerimeterX); those are logged as unmeasurable notes, not treated as delisted.
- Saves every listing's raw source record as local evidence (SHA-256 named files) with capture time, so a listing that disappears still has its rent/terms on record.
- Keeps a local tracking ledger: `interested`, `contacted`, `tour_scheduled`, `toured`, `applied`, `waitlisted`, `leased`, `passed`, `unavailable`, `closed`.
- Ranks with `tools/apartmentfinder_rank.py`: hard constraints in `config/preferences.json` (max rent, min beds/baths/sqft, property types, saved locations, deal-breaker keywords) exclude a listing *with the reason kept*; soft preferences (target rent, $/sqft value, freshness, nice-to-haves) set the score. Missing facts never exclude — they are shown as gaps.

## Quick start

```bash
git clone https://github.com/miguelf-github/apartmentfinder.git && cd apartmentfinder
python3 apartmentfinder.py init          # copies config examples to private config
# 1) Get a free RentCast key at https://www.rentcast.io/api
mkdir -p ~/.config && printf 'RENTCAST_API_KEY=your-key\n' >> ~/.config/apartmentfinder.env
chmod 600 ~/.config/apartmentfinder.env
# 2) Edit config/preferences.json — locations, budget, layout, must-haves, deal-breakers
python3 apartmentfinder.py sync --limit 50
python3 apartmentfinder.py listings --max-rent 2500 --min-beds 2
python3 tools/apartmentfinder_rank.py
python3 apartmentfinder.py stage 'rentcast:12345' --note 'Quiet street, in-unit laundry'
python3 apartmentfinder.py enrich --limit 25        # optional APIs: walk scores, rent estimate, HUD, Census, flood, air
python3 apartmentfinder.py track 'rentcast:12345' tour_scheduled --tour-at '2026-11-08 14:00' --contact 'Ana, property manager'
python3 apartmentfinder.py report
```

Listing keys: API listings are `source:id` (e.g. `rentcast:12345`); imported pages use their URL as the key. `stage` and `track` need the key — `listings` prints it when there is no URL.

Run the standard-library test suite:

```bash
python3 -m unittest discover -s tests -v
```

## Why not a Zillow/Apartments.com API?

There isn't one for the public: Zillow retired its public API (its remaining Bridge program needs MLS/broker affiliation), and Apartments.com publishes no developer API at all. RentCast is the self-serve route — it aggregates MLS and public listing sources nationwide and has a real free tier. The direct-URL importer plus the home-search browser flow covers anything RentCast misses.

All optional keys (RentCast, HasData, Walk Score, HUD, AirNow) live in the same place: the environment or `~/.config/apartmentfinder.env` (chmod 600), one `NAME=value` per line. Copy a source or provider in from the example config, add its key, and it turns on at the next sync/enrich — remove the key and it skips itself again. Nothing is ever required beyond the core tool.

## Evidence states

| State | Meaning |
| --- | --- |
| Captured | The tool fetched the listing now, saved a local copy and hash, and recorded a time. |
| Unmeasurable | The source returned a block, challenge, error, or no key was configured. Not evidence that no rentals exist. |
| Interested | A person reviewed the listing and wants to pursue it. Nothing was sent to anyone. |
| Applied / Leased | Record only after the person actually applied / signed, confirmed outside the tool. |

## Privacy boundary

Personal configuration, evidence, and the ledger live in gitignored files (`config/*.json` real copies and `.apartmentfinder/`). API keys live in the environment or `~/.config/apartmentfinder.env` (chmod 600) — never in the repo. Before a commit, run:

```bash
git grep -n -E -i '(@[^[:space:]]+\.(com|org|net)|phone|api[_-]?key|address)' || true
git status --short
```

## License

MIT
