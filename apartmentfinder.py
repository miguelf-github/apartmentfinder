#!/usr/bin/env python3
"""A private, configuration-first rental discovery and review ledger.

The rental twin of hiddenjob: it finds apartment/house listings from a real
listings API (RentCast) plus direct URLs the person supplies, preserves the
source evidence locally, ranks listings against the person's own preferences
and hard constraints, and keeps a tracking ledger from first interest through
tour, application, and lease.

It deliberately does NOT contact landlords or property managers, submit
applications, create accounts, solve CAPTCHAs, or make payments. Those stay
deliberate human actions. Blocked or incomplete sources are reported as
unmeasurable, never as "no apartments exist".
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

UA = "Apartmentfinder/0.1 (research-and-review)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

TRACK_STATES = ("interested", "contacted", "tour_scheduled", "toured",
                "applied", "waitlisted", "leased", "passed", "unavailable",
                "closed")


def _secret_from_file(name: str) -> str:
    """Read a KEY=VALUE secret from ~/.config/apartmentfinder.env (chmod 600).

    Local fallback so API keys never have to be exported in the shell or
    hardcoded. Returns "" when the file or key is absent.
    """
    path = os.path.expanduser("~/.config/apartmentfinder.env")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == name:
                    return value.strip().strip("\"").strip("'")
    except OSError:
        pass
    return ""


def api_key_for(env_name: str) -> str:
    """Env var first, then the chmod-600 secrets file (hiddenjob pattern)."""
    return os.environ.get(env_name) or _secret_from_file(env_name)


class FetchBlocked(RuntimeError):
    """A source cannot be measured; it must not be treated as an empty result."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def clean_text(raw: str) -> str:
    raw = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def is_challenge_page(lower_body: str) -> bool:
    """Identify bot-wall signatures without rejecting ordinary page wording."""
    return ("just a moment" in lower_body or "challenge-platform" in lower_body or
            "cf-chl-" in lower_body or "perimeterx" in lower_body or
            ("hcaptcha" in lower_body and "challenge" in lower_body))


def fetch(url: str, timeout: int = 25) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status, final_url = response.status, response.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "", url
        raise FetchBlocked(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchBlocked(f"{url}: {exc}") from exc
    lower = body[:8000].lower()
    if status != 200 or not body.strip() or is_challenge_page(lower):
        raise FetchBlocked(f"{url}: unmeasurable response (HTTP {status}, {len(body)} bytes)")
    return body, final_url


def fetch_json(url: str, timeout: int = 25, headers: dict[str, str] | None = None) -> object:
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                raise FetchBlocked(f"{url}: HTTP {response.status}")
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise FetchBlocked(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise FetchBlocked(f"{url}: {exc}") from exc


# ---------------------------------------------------------------------------
# Source: RentCast (primary listings API)
# ---------------------------------------------------------------------------

RENTCAST_URL = "https://api.rentcast.io/v1/listings/rental/long-term"


def _num(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"[\d,]+(?:\.\d+)?", value)
        if m:
            try:
                return float(m.group(0).replace(",", ""))
            except ValueError:
                return None
    return None


def normalize_rentcast(item: dict) -> dict[str, object]:
    """Normalize one RentCast rental listing into the shared record shape.

    Defensive by design: RentCast fields vary by listing, so every field is
    read with a fallback and the raw record is preserved as evidence.
    """
    ext_id = str(item.get("id") or "")
    address = str(item.get("formattedAddress") or "").strip()
    if not address:
        parts = [str(item.get(k) or "") for k in ("addressLine1", "city", "state", "zipCode")]
        address = ", ".join(p for p in parts if p).strip()
    rent = _num(item.get("price"))
    beds = _num(item.get("bedrooms"))
    baths = _num(item.get("bathrooms"))
    sqft = _num(item.get("squareFootage"))
    agent = item.get("listingAgent") if isinstance(item.get("listingAgent"), dict) else {}
    office = item.get("listingOffice") if isinstance(item.get("listingOffice"), dict) else {}
    contact_bits = [str(agent.get("name") or ""), str(agent.get("phone") or ""),
                    str(office.get("name") or ""), str(office.get("phone") or "")]
    desc_parts = [
        address,
        f"${rent:,.0f}/mo" if rent else "",
        f"{beds:g} bed" if beds is not None else "",
        f"{baths:g} bath" if baths is not None else "",
        f"{sqft:,.0f} sqft" if sqft else "",
        str(item.get("propertyType") or ""),
        str(item.get("status") or ""),
        "Contact: " + ", ".join(b for b in contact_bits if b) if any(contact_bits) else "",
    ]
    description = clean_text(". ".join(p for p in desc_parts if p))
    return {
        "key": f"rentcast:{ext_id}" if ext_id else f"rentcast:{hashlib.sha256(address.encode()).hexdigest()[:16]}",
        "url": "",
        "source": "rentcast",
        "external_id": ext_id,
        "title": address or "Untitled listing",
        "address": address,
        "city": str(item.get("city") or ""),
        "state": str(item.get("state") or ""),
        "zip": str(item.get("zipCode") or ""),
        "rent": rent,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "property_type": str(item.get("propertyType") or ""),
        "status": str(item.get("status") or ""),
        "listed_date": str(item.get("listedDate") or item.get("createdDate") or ""),
        "days_on_market": item.get("daysOnMarket") if isinstance(item.get("daysOnMarket"), (int, float)) else None,
        "latitude": _num(item.get("latitude")),
        "longitude": _num(item.get("longitude")),
        "description": description,
        "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
        "evidence_text": json.dumps(item, indent=2),
        "evidence_suffix": ".json",
    }


def rentcast_listings(api_key: str, query: dict[str, object], limit: int = 50,
                      timeout: int = 25) -> list[dict[str, object]]:
    """Fetch active long-term rental listings from RentCast.

    One request per query (the free Developer plan is 50 requests/month, so
    sync stays frugal: one call per configured location, no pagination
    chasing). Query keys map straight onto the API: city, state, zipCode,
    address, latitude, longitude, radius, bedrooms, bathrooms, daysOld.
    """
    if not api_key:
        return []
    params: dict[str, object] = {"status": "Active", "limit": max(1, min(limit, 500))}
    for key in ("city", "state", "zipCode", "address", "latitude", "longitude",
                "radius", "bedrooms", "bathrooms", "propertyType", "daysOld"):
        if query.get(key) not in (None, ""):
            params[key] = query[key]
    url = RENTCAST_URL + "?" + urllib.parse.urlencode(params)
    data = fetch_json(url, timeout=timeout, headers={"X-Api-Key": api_key})
    items = data if isinstance(data, list) else []
    return [normalize_rentcast(it) for it in items if isinstance(it, dict)]


# ---------------------------------------------------------------------------
# Source: Realtor.com via RapidAPI (optional second source)
# ---------------------------------------------------------------------------

def normalize_realty_us(item: dict) -> dict[str, object]:
    """Normalize one apidojo Realty-in-US for-rent result (defensive shape)."""
    desc = item.get("description") if isinstance(item.get("description"), dict) else {}
    loc = item.get("location") if isinstance(item.get("location"), dict) else {}
    addr = loc.get("address") if isinstance(loc.get("address"), dict) else {}
    line = str(addr.get("line") or "")
    city = str(addr.get("city") or "")
    state = str(addr.get("state_code") or addr.get("state") or "")
    zip_code = str(addr.get("postal_code") or "")
    address = ", ".join(p for p in [line, city, f"{state} {zip_code}".strip()] if p)
    rent = _num(item.get("list_price"))
    beds = _num(desc.get("beds"))
    baths = _num(desc.get("baths"))
    sqft = _num(desc.get("sqft"))
    href = str(item.get("href") or "")
    prop_id = str(item.get("property_id") or "")
    text = clean_text(str(item.get("branding") and "" or "") + " " +
                      ", ".join(str(t) for t in (item.get("tags") or []) if t))
    description = clean_text(". ".join(p for p in [
        address, f"${rent:,.0f}/mo" if rent else "",
        f"{beds:g} bed" if beds is not None else "",
        f"{baths:g} bath" if baths is not None else "",
        f"{sqft:,.0f} sqft" if sqft else "",
        str(desc.get("type") or ""), text] if p))
    return {
        "key": href or f"realty-us:{prop_id}",
        "url": href,
        "source": "realty-us",
        "external_id": prop_id,
        "title": address or "Untitled listing",
        "address": address,
        "city": city,
        "state": state,
        "zip": zip_code,
        "rent": rent,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "property_type": str(desc.get("type") or ""),
        "status": str(item.get("status") or ""),
        "listed_date": str(item.get("list_date") or ""),
        "days_on_market": None,
        "latitude": _num((addr.get("coordinate") or {}).get("lat")) if isinstance(addr.get("coordinate"), dict) else None,
        "longitude": _num((addr.get("coordinate") or {}).get("lon")) if isinstance(addr.get("coordinate"), dict) else None,
        "description": description,
        "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
        "evidence_text": json.dumps(item, indent=2),
        "evidence_suffix": ".json",
    }


def realty_us_listings(api_key: str, query: dict[str, object], limit: int = 42,
                       timeout: int = 25) -> list[dict[str, object]]:
    """Fetch for-rent listings from Realtor.com data via RapidAPI (apidojo).

    Optional: skipped silently upstream when no RAPIDAPI_KEY is configured.
    Free Basic plan is ~500 requests/month. One page per sync.
    """
    if not api_key:
        return []
    params: dict[str, object] = {"limit": max(1, min(limit, 42)), "offset": 0,
                                 "status": "for_rent"}
    for src_key, dst_key in (("city", "city"), ("state", "state_code"),
                             ("zip", "postal_code"), ("min_beds", "beds_min"),
                             ("max_rent", "price_max"), ("min_baths", "baths_min")):
        if query.get(src_key) not in (None, ""):
            params[dst_key] = query[src_key]
    url = ("https://realty-in-us.p.rapidapi.com/properties/list-for-rent?"
           + urllib.parse.urlencode(params))
    data = fetch_json(url, timeout=timeout, headers={
        "X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "realty-in-us.p.rapidapi.com"})
    results: list = []
    if isinstance(data, dict):
        hs = data.get("data", {}).get("home_search", {}) if isinstance(data.get("data"), dict) else {}
        results = hs.get("results", []) if isinstance(hs, dict) else []
    return [normalize_realty_us(it) for it in results if isinstance(it, dict)]


# ---------------------------------------------------------------------------
# Source: direct listing URLs (Zillow / Apartments.com / landlord pages)
# ---------------------------------------------------------------------------

def jsonld_blocks(raw: str) -> list[dict]:
    out: list[dict] = []
    for block in re.findall(r"<script[^>]+(?:application/ld\+json|ld\+json)[^>]*>(.*?)</script>", raw, re.I | re.S):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        items = parsed if isinstance(parsed, list) else parsed.get("@graph", [parsed]) if isinstance(parsed, dict) else []
        out.extend(it for it in items if isinstance(it, dict))
    return out


def extract_listing(url: str, raw: str, source: str) -> dict[str, object]:
    """Extract a listing record from a fetched listing page (JSON-LD first).

    Portals bot-wall aggressively (Zillow uses PerimeterX), so a blocked page
    raises FetchBlocked upstream and is recorded as unmeasurable — never as
    "the apartment is gone". When a page does load, prefer its structured
    data; fall back to the first heading and a rent-shaped number.
    """
    item: dict = {}
    for cand in jsonld_blocks(raw):
        t = str(cand.get("@type") or "")
        if any(k in t for k in ("Apartment", "Residence", "House", "RealEstate", "Product", "Offer")):
            item = cand
            break
    addr = item.get("address") if isinstance(item.get("address"), dict) else {}
    offers = item.get("offers") if isinstance(item.get("offers"), dict) else {}
    title = str(item.get("name") or "")
    if not title:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.I | re.S)
        title = clean_text(m.group(1)) if m else "Untitled listing"
    description = clean_text(str(item.get("description") or raw))
    rent = _num(offers.get("price"))
    if rent is None:
        m = re.search(r"\$\s?([\d,]{3,6})\s*(?:/mo|per month|/month)?", description)
        rent = _num(m.group(1)) if m else None
    address = ", ".join(p for p in [
        str(addr.get("streetAddress") or ""), str(addr.get("addressLocality") or ""),
        str(addr.get("addressRegion") or ""), str(addr.get("postalCode") or "")] if p) or title
    return {
        "key": url,
        "url": url,
        "source": source,
        "external_id": "",
        "title": title[:300],
        "address": address[:300],
        "city": str(addr.get("addressLocality") or ""),
        "state": str(addr.get("addressRegion") or ""),
        "zip": str(addr.get("postalCode") or ""),
        "rent": rent,
        "beds": _num(item.get("numberOfRooms")),
        "baths": None,
        "sqft": _num((item.get("floorSize") or {}).get("value")) if isinstance(item.get("floorSize"), dict) else None,
        "property_type": str(item.get("@type") or ""),
        "status": str(offers.get("availability") or ""),
        "listed_date": "",
        "days_on_market": None,
        "latitude": None,
        "longitude": None,
        "description": description,
        "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
        "evidence_text": raw,
        "evidence_suffix": ".html",
    }


# ---------------------------------------------------------------------------
# Config helpers (hiddenjob pattern)
# ---------------------------------------------------------------------------

def is_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def urls_from_records(records: object, label: str) -> list[str]:
    if isinstance(records, dict):
        records = records.get("urls") if "urls" in records else [records]
    if isinstance(records, str):
        records = [records]
    if not isinstance(records, list):
        raise SystemExit(f"{label} must contain URL strings or objects with a url field.")
    urls: list[str] = []
    for index, record in enumerate(records, start=1):
        value = record.get("url") if isinstance(record, dict) else record
        if not is_http_url(value):
            raise SystemExit(f"{label} entry {index} is not an absolute HTTP(S) URL.")
        urls.append(str(value).strip())
    return unique(urls)


def read_url_list(path: Path) -> list[str]:
    """Read JSON, JSONL, or one-URL-per-line imports, retaining only URLs."""
    if not path.exists():
        raise SystemExit(f"URL list not found: {path}")
    content = path.read_text().strip()
    if not content:
        return []
    try:
        return urls_from_records(json.loads(content), str(path))
    except json.JSONDecodeError:
        pass
    lines = [ln.strip() for ln in content.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if lines and all(ln.startswith("{") for ln in lines):
        try:
            return urls_from_records([json.loads(ln) for ln in lines], str(path))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSONL in {path}: {exc}") from exc
    return urls_from_records(lines, str(path))


def config_relative_path(value: object, cfg_path: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"{label} must be a non-empty file path.")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else cfg_path.parent / path).resolve()


def load_json(path: Path, label: str) -> dict:
    if not path.exists():
        raise SystemExit(f"{label} not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{label} must be a JSON object: {path}")
    return data


def load_config(path: Path) -> dict:
    data = load_json(path, "Config")
    if not isinstance(data.get("sources"), list):
        raise SystemExit("Config must contain a sources list.")
    return data


def load_preferences(cfg: dict, cfg_path: Path) -> dict:
    """Preferences/constraints live in their own file, named by the config."""
    pref_file = cfg.get("preferences_file", "preferences.json")
    path = config_relative_path(pref_file, cfg_path, "preferences_file")
    if not path.exists():
        return {}
    return load_json(path, "Preferences")


def data_root(cfg: dict, cfg_path: Path) -> Path:
    root = Path(str(cfg.get("data_dir", ".apartmentfinder")))
    root = root if root.is_absolute() else cfg_path.parent.parent / root
    root.mkdir(parents=True, exist_ok=True)
    (root / "evidence").mkdir(exist_ok=True)
    return root


def database(root: Path) -> sqlite3.Connection:
    con = sqlite3.connect(root / "apartmentfinder.sqlite3")
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS listings (
      key TEXT PRIMARY KEY, url TEXT, source TEXT NOT NULL, external_id TEXT,
      title TEXT, address TEXT, city TEXT, state TEXT, zip TEXT,
      rent REAL, beds REAL, baths REAL, sqft REAL, property_type TEXT,
      status TEXT, listed_date TEXT, days_on_market REAL,
      latitude REAL, longitude REAL,
      description TEXT, description_sha256 TEXT, evidence_path TEXT,
      live INTEGER NOT NULL DEFAULT 1, last_checked TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tracker (
      listing_key TEXT PRIMARY KEY REFERENCES listings(key),
      state TEXT NOT NULL, note TEXT, updated_at TEXT NOT NULL,
      contact TEXT, tour_at TEXT, applied_at TEXT
    );
    """)
    return con


def save_evidence(root: Path, content: str, suffix: str) -> Path:
    digest = hashlib.sha256(content.encode()).hexdigest()
    path = root / "evidence" / f"{digest}{suffix}"
    if not path.exists():
        path.write_text(content)
    return path


def store_listing(con: sqlite3.Connection, root: Path, record: dict) -> None:
    evidence = save_evidence(root, str(record.get("evidence_text") or record.get("description") or ""),
                             str(record.get("evidence_suffix") or ".html"))
    con.execute("""
      INSERT INTO listings (key, url, source, external_id, title, address, city, state, zip,
        rent, beds, baths, sqft, property_type, status, listed_date, days_on_market,
        latitude, longitude, description, description_sha256, evidence_path, live, last_checked)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
      ON CONFLICT(key) DO UPDATE SET
        url=excluded.url, title=excluded.title, address=excluded.address,
        city=excluded.city, state=excluded.state, zip=excluded.zip,
        rent=excluded.rent, beds=excluded.beds, baths=excluded.baths,
        sqft=excluded.sqft, property_type=excluded.property_type,
        status=excluded.status, listed_date=excluded.listed_date,
        days_on_market=excluded.days_on_market,
        latitude=excluded.latitude, longitude=excluded.longitude,
        description=excluded.description,
        description_sha256=excluded.description_sha256,
        evidence_path=excluded.evidence_path, live=1, last_checked=excluded.last_checked
    """, (record["key"], record.get("url") or "", record["source"], record.get("external_id") or "",
          record.get("title") or "", record.get("address") or "", record.get("city") or "",
          record.get("state") or "", record.get("zip") or "", record.get("rent"),
          record.get("beds"), record.get("baths"), record.get("sqft"),
          record.get("property_type") or "", record.get("status") or "",
          record.get("listed_date") or "", record.get("days_on_market"),
          record.get("latitude"), record.get("longitude"),
          record.get("description") or "", record.get("description_sha256") or "",
          str(evidence), now()))
    con.commit()


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def preference_queries(prefs: dict) -> list[dict[str, object]]:
    """Build one RentCast-style query per saved location in preferences."""
    queries: list[dict[str, object]] = []
    for loc in prefs.get("locations") or []:
        if not isinstance(loc, dict):
            continue
        q: dict[str, object] = {}
        for key in ("city", "state", "zipCode", "zip", "address", "latitude", "longitude", "radius"):
            if loc.get(key) not in (None, ""):
                q["zipCode" if key == "zip" else key] = loc[key]
        if q:
            queries.append(q)
    return queries


def cmd_init(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config)
    cfg_dir = cfg_path.parent
    cfg_dir.mkdir(parents=True, exist_ok=True)
    examples = {
        "targets.json": "targets.example.json",
        "preferences.json": "preferences.example.json",
        "profile.json": "profile.example.json",
        "automation.json": "automation.example.json",
    }
    for target, example in examples.items():
        dst = cfg_dir / target
        src = cfg_dir / example
        if dst.exists() and not args.force:
            print(f"exists, kept: {dst}")
        elif src.exists():
            dst.write_text(src.read_text())
            print(f"created from example: {dst}")
        else:
            print(f"missing example, skipped: {src}")
    cfg = load_config(cfg_path) if cfg_path.exists() else {"data_dir": ".apartmentfinder"}
    root = data_root(cfg, cfg_path)
    database(root).close()
    print(f"data dir ready: {root}")
    print("Next: edit config/preferences.json with your real constraints, "
          "add RENTCAST_API_KEY to ~/.config/apartmentfinder.env (chmod 600), "
          "then run: python3 apartmentfinder.py sync --limit 50")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config).resolve()
    cfg = load_config(cfg_path)
    prefs = load_preferences(cfg, cfg_path)
    root = data_root(cfg, cfg_path)
    con = database(root)
    total, notes = 0, []
    for source in cfg["sources"]:
        if not isinstance(source, dict):
            continue
        name = str(source.get("name") or source.get("kind") or "source")
        kind = str(source.get("kind") or "")
        limit = int(source.get("limit") or args.limit)
        records: list[dict] = []
        try:
            if kind == "rentcast":
                key = api_key_for(str(source.get("api_key_env") or "RENTCAST_API_KEY"))
                if not key:
                    notes.append(f"{name}: skipped, no API key (set RENTCAST_API_KEY "
                                 f"in env or ~/.config/apartmentfinder.env)")
                    continue
                queries = [source.get("query") or {}]
                if source.get("from_preferences"):
                    queries = preference_queries(prefs) or queries
                for q in queries:
                    if not isinstance(q, dict):
                        continue
                    got = rentcast_listings(key, q, limit=limit)
                    records.extend(got)
                    print(f"{name}: rentcast query {q} -> {len(got)} listings")
            elif kind == "realty-us":
                key = api_key_for(str(source.get("api_key_env") or "RAPIDAPI_KEY"))
                if not key:
                    notes.append(f"{name}: skipped, no RAPIDAPI_KEY configured")
                    continue
                records = realty_us_listings(key, source.get("query") or {}, limit=limit)
                print(f"{name}: realty-us -> {len(records)} listings")
            else:
                if kind:
                    raise SystemExit(f"source {name}: unknown kind '{kind}'")
                if "urls" not in source and "url_list_file" not in source:
                    raise SystemExit(f"source {name}: no urls, url_list_file, or supported kind configured")
                urls: list[str] = []
                if "urls" in source:
                    urls.extend(urls_from_records(source["urls"], f"{name}.urls"))
                if "url_list_file" in source:
                    urls.extend(read_url_list(config_relative_path(source["url_list_file"], cfg_path, "url_list_file")))
                urls = unique(urls)
                if not urls:
                    print(f"{name}: url import -> 0 listings (no urls added yet)")
                    continue
                take = urls[-limit:] if source.get("take") == "tail" else urls[:limit]
                for url in take:
                    try:
                        body, final_url = fetch(url)
                    except FetchBlocked as exc:
                        notes.append(f"{name}: unmeasurable: {exc}")
                        continue
                    if not body:
                        notes.append(f"{name}: unmeasurable (empty/404): {url}")
                        continue
                    records.append(extract_listing(final_url, body, name))
                print(f"{name}: url import -> {len(records)} listings from {len(take)} urls")
        except FetchBlocked as exc:
            notes.append(f"{name}: unmeasurable: {exc}")
            continue
        seen: set[str] = set()
        for record in records:
            if record["key"] in seen:
                continue
            seen.add(record["key"])
            store_listing(con, root, record)
            total += 1
    con.close()
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    print(f"sync complete: {total} listings stored/updated; {len(notes)} source notes")
    return 0


# ---------------------------------------------------------------------------
# Ledger commands
# ---------------------------------------------------------------------------

def with_db(args: argparse.Namespace) -> tuple[sqlite3.Connection, Path]:
    cfg_path = Path(args.config).resolve()
    cfg = load_config(cfg_path)
    root = data_root(cfg, cfg_path)
    return database(root), root


def fmt_money(value: object) -> str:
    return f"${value:,.0f}" if isinstance(value, (int, float)) else "?"


def cmd_listings(args: argparse.Namespace) -> int:
    con, _ = with_db(args)
    sql = ("SELECT l.*, t.state AS track_state FROM listings l "
           "LEFT JOIN tracker t ON t.listing_key = l.key WHERE 1=1")
    params: list = []
    if args.state:
        sql += " AND t.state = ?"
        params.append(args.state)
    if args.untracked:
        sql += " AND t.state IS NULL"
    if args.max_rent is not None:
        sql += " AND l.rent IS NOT NULL AND l.rent <= ?"
        params.append(args.max_rent)
    if args.min_beds is not None:
        sql += " AND l.beds IS NOT NULL AND l.beds >= ?"
        params.append(args.min_beds)
    if args.city:
        sql += " AND l.city LIKE ?"
        params.append(f"%{args.city}%")
    sql += " ORDER BY (l.rent IS NULL), l.rent ASC LIMIT ?"
    params.append(args.limit)
    for row in con.execute(sql, params):
        bits = [fmt_money(row["rent"]) + "/mo",
                f"{row['beds']:g}bd" if row["beds"] is not None else "?bd",
                f"{row['baths']:g}ba" if row["baths"] is not None else "?ba",
                f"{row['sqft']:,.0f}sqft" if row["sqft"] else "?sqft",
                row["city"] or "", row["source"],
                f"[{row['track_state']}]" if row["track_state"] else ""]
        print(f"- {row['title']} | " + " | ".join(b for b in bits if b))
        link = row["url"] or row["key"]
        print(f"    {link}")
    con.close()
    return 0


def _require_listing(con: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM listings WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise SystemExit(f"listing not in the local ledger: {key} (run sync or import its URL first)")
    return row


def cmd_stage(args: argparse.Namespace) -> int:
    con, _ = with_db(args)
    _require_listing(con, args.key)
    con.execute("""
      INSERT INTO tracker (listing_key, state, note, updated_at) VALUES (?, 'interested', ?, ?)
      ON CONFLICT(listing_key) DO UPDATE SET note=excluded.note, updated_at=excluded.updated_at
    """, (args.key, args.note or "", now()))
    con.commit()
    print(f"staged as interested: {args.key}")
    con.close()
    return 0


def cmd_track(args: argparse.Namespace) -> int:
    if args.state not in TRACK_STATES:
        raise SystemExit(f"state must be one of: {', '.join(TRACK_STATES)}")
    con, _ = with_db(args)
    _require_listing(con, args.key)
    stamp = now()
    contact = args.contact or ""
    tour_at = args.tour_at or ""
    applied_at = stamp if args.state == "applied" else ""
    con.execute("""
      INSERT INTO tracker (listing_key, state, note, updated_at, contact, tour_at, applied_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(listing_key) DO UPDATE SET
        state=excluded.state, note=excluded.note, updated_at=excluded.updated_at,
        contact=CASE WHEN excluded.contact != '' THEN excluded.contact ELSE tracker.contact END,
        tour_at=CASE WHEN excluded.tour_at != '' THEN excluded.tour_at ELSE tracker.tour_at END,
        applied_at=CASE WHEN excluded.applied_at != '' THEN excluded.applied_at ELSE tracker.applied_at END
    """, (args.key, args.state, args.note or "", stamp, contact, tour_at, applied_at))
    con.commit()
    print(f"tracked: {args.key} -> {args.state}")
    con.close()
    return 0


def cmd_check_duplicate(args: argparse.Namespace) -> int:
    con, _ = with_db(args)
    row = con.execute("""
      SELECT l.key, l.title, l.rent, t.state FROM listings l
      LEFT JOIN tracker t ON t.listing_key = l.key
      WHERE l.key = ? OR l.url = ? OR (l.address != '' AND l.address = ?)
    """, (args.key, args.key, args.address or "")).fetchone()
    if row is None:
        print("not tracked: no matching listing in the ledger")
    else:
        print(f"found: {row['title']} | {fmt_money(row['rent'])}/mo | "
              f"state={row['state'] or 'untracked'} | key={row['key']}")
    con.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    con, _ = with_db(args)
    total = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    print(f"listings captured: {total}")
    for row in con.execute("SELECT source, COUNT(*) AS n FROM listings GROUP BY source ORDER BY n DESC"):
        print(f"  {row['source']}: {row['n']}")
    print("tracker by state:")
    any_state = False
    for row in con.execute("SELECT state, COUNT(*) AS n FROM tracker GROUP BY state ORDER BY n DESC"):
        print(f"  {row['state']}: {row['n']}")
        any_state = True
    if not any_state:
        print("  (nothing tracked yet)")
    stats = con.execute("SELECT MIN(rent), AVG(rent), MAX(rent), COUNT(rent) FROM listings").fetchone()
    if stats[3]:
        print(f"rent range (priced listings): {fmt_money(stats[0])} - {fmt_money(stats[2])}, "
              f"avg {fmt_money(stats[1])} across {stats[3]} listings")
    con.close()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="apartmentfinder", description=__doc__)
    p.add_argument("--config", default="config/targets.json", help="private runtime config path")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create private config from examples and prepare the data dir")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    sync = sub.add_parser("sync", help="pull listings from configured sources into the ledger")
    sync.add_argument("--limit", type=int, default=50)
    sync.set_defaults(func=cmd_sync)

    listings = sub.add_parser("listings", help="list captured listings")
    listings.add_argument("--state", choices=TRACK_STATES)
    listings.add_argument("--untracked", action="store_true")
    listings.add_argument("--max-rent", type=float)
    listings.add_argument("--min-beds", type=float)
    listings.add_argument("--city")
    listings.add_argument("--limit", type=int, default=50)
    listings.set_defaults(func=cmd_listings)

    stage = sub.add_parser("stage", help="mark a listing as interested (reviewed, worth pursuing)")
    stage.add_argument("key", help="listing key (URL, or source:id like rentcast:123)")
    stage.add_argument("--note")
    stage.set_defaults(func=cmd_stage)

    track = sub.add_parser("track", help="move a listing through the tracker")
    track.add_argument("key")
    track.add_argument("state", help=f"one of: {', '.join(TRACK_STATES)}")
    track.add_argument("--note")
    track.add_argument("--contact", help="landlord/manager name or contact note")
    track.add_argument("--tour-at", help="tour date/time note, e.g. 2026-11-08 14:00")
    track.set_defaults(func=cmd_track)

    dup = sub.add_parser("check-duplicate", help="check whether a listing is already in the ledger")
    dup.add_argument("key")
    dup.add_argument("--address")
    dup.set_defaults(func=cmd_check_duplicate)

    report = sub.add_parser("report", help="ledger summary")
    report.set_defaults(func=cmd_report)
    return p


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
