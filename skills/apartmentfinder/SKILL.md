---
name: apartmentfinder
description: Discover apartments and houses for rent via the RentCast API and direct listing URLs, rank them against private preferences and hard constraints, and maintain a private evidence and tracking ledger from interest through lease.
---

# apartmentfinder

Use this skill when asked to find rentals, apartments, or houses for rent as an ongoing search with tracking, or to maintain the person's rental ledger. For a one-off "show me a few places" question, the general home-search flow is enough; apartmentfinder is for the standing hunt.

## Purpose

Mirror of hiddenjob for housing: discover listings a person might miss, preserve what each listing said when it was captured, rank honestly against the person's own constraints, and track the pursuit. Never claim a listing is gone because a portal blocked a fetch — blocked means unmeasurable.

## Required workflow

1. Initialize private local configuration: `python3 apartmentfinder.py init`, then edit `config/preferences.json` (locations, budget, layout, must-haves, deal-breakers, household) and copy the automation/profile examples to their ignored real files. Keep all real config, evidence, and the ledger out of Git.
2. API key: RentCast key goes in the environment or `~/.config/apartmentfinder.env` (chmod 600) as `RENTCAST_API_KEY`. Never in the repo, never in chat. Without a key the rentcast source is skipped with a note — that is a setup gap, not an empty market.
3. Respect the API budget: RentCast's free Developer plan is 50 requests/month and sync spends one request per saved location. Sync deliberately (a few times a week at most on the free plan), not on a tight cron.
4. Run `python3 apartmentfinder.py sync --limit 50`, inspect with `python3 apartmentfinder.py listings`, and rank with `python3 tools/apartmentfinder_rank.py`. Open the saved evidence copy before staging a listing as `interested`.
5. The person reviews and stages; Muse never contacts a landlord/property manager, books a tour, submits an application, pays anything, or signs anything. Draft inquiry text for the person to send is fine. Record `applied`/`leased` only on the person's confirmation.
6. Direct URLs the person pastes (Zillow, Apartments.com, Redfin) go in `config/listing-urls.jsonl`; the importer keeps only the URL. Portal bot-walls are expected; on a blocked fetch, verify that one listing through the live browser (home-search skill) instead of treating it as delisted.

## Ranking rules

- Hard constraints exclude, with the reason printed. Missing facts (unknown beds, unknown rent) never exclude; they show as gaps.
- A hard constraint is the person's to relax, not Muse's: surface near-misses that fail exactly one gate when they are otherwise strong.
- Listed rent is not total cost: when a listing or its page states fees, deposits, or utilities, keep them in the note; never present a fee-laden total as the rent or vice versa.

## Privacy

Preferences may reveal income, household, and pets. They stay in the private workspace. Outreach drafts contain only what the person approves for that landlord.
