# Muse setup prompt for apartmentfinder

> Set up this apartmentfinder repository as my personal, private local apartment-discovery and tracking assistant.
>
> I will provide my search preferences and constraints in this private Muse conversation: locations, max rent, bedrooms/bathrooms, must-haves, deal-breakers, pets, move-in window. Create `config/preferences.json`, `config/profile.json`, and `config/automation.json` from the repository examples. Keep all of them, captured listings, evidence, and the ledger out of Git. Never use someone else's preferences or profile.
>
> First read `README.md`, this file, and `skills/apartmentfinder/SKILL.md`. Add my RentCast API key only through the environment or `~/.config/apartmentfinder.env` (chmod 600) — never write it into the repo or chat-visible files.
>
> Run discovery, evidence capture, ranking, staging, and tracking automatically under the hands-off-with-final-approval policy in `config/automation.json`. Respect the RentCast free-plan budget: one API call per saved location per sync, no tight polling loops.
>
> Stop for any landlord/property-manager message, tour booking, account creation, password, one-time code, CAPTCHA, application, payment, lease, or acceptance of terms. Record `applied` or `leased` only after I confirm it actually happened. Treat blocked or incomplete sources as unmeasurable, not empty.
