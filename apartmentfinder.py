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
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

UA = "Apartmentfinder/0.2 (research-and-review)"
VERSION = "0.4.0"
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


RENTCAST_CLI = os.path.expanduser("~/workspace/custom-connectors/custom.rentcast/bin/rentcast_api.py")


def _rentcast_cli(args: list[str], timeout: int = 90) -> object:
    """Run the custom.rentcast connector CLI; returns parsed JSON or None.

    The CLI calls RentCast with the Secure Vault credential (surrogate
    exchange), so no raw key exists in this process. A {"error": ...}
    payload is printed to stderr and reported as None.
    """
    if not os.path.exists(RENTCAST_CLI):
        return None
    try:
        proc = subprocess.run([sys.executable, RENTCAST_CLI, *args],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"rentcast connector CLI: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"rentcast connector CLI failed: {proc.stdout.strip()[:200]}", file=sys.stderr)
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("rentcast connector CLI: unparseable output", file=sys.stderr)
        return None
    if isinstance(data, dict) and data.get("error"):
        print(f"rentcast connector: {data['error']}", file=sys.stderr)
        return None
    return data


def rentcast_listings_via_connector(query: dict[str, object], limit: int = 50) -> list[dict[str, object]]:
    """Fetch RentCast listings via the stored Muse credential (no raw key).

    Fallback used when no RENTCAST_API_KEY is in the env/secrets file but
    the user connected RentCast in Muse (custom.rentcast). Same pattern
    as hiddenjob's JSearch vault fallback.
    """
    args = ["listings", "--limit", str(max(1, min(limit, 500)))]
    for src, flag in (("city", "--city"), ("state", "--state"), ("zipCode", "--zip"),
                      ("address", "--address"), ("latitude", "--latitude"),
                      ("longitude", "--longitude"), ("radius", "--radius"),
                      ("bedrooms", "--bedrooms"), ("bathrooms", "--bathrooms"),
                      ("propertyType", "--property-type"), ("daysOld", "--days-old")):
        if query.get(src) not in (None, ""):
            args += [flag, str(query[src])]
    data = _rentcast_cli(args)
    items = data if isinstance(data, list) else []
    return [normalize_rentcast(it) for it in items if isinstance(it, dict)]


def rentcast_avm_via_connector(listing: dict) -> dict:
    """RentCast AVM via the stored Muse credential. Raises FetchBlocked."""
    address = str(listing.get("address") or "")
    if not address:
        raise FetchBlocked("rentcast-avm: listing has no address")
    args = ["avm", "--address", address]
    for src, flag in (("beds", "--bedrooms"), ("baths", "--bathrooms"),
                      ("sqft", "--sqft"), ("property_type", "--property-type")):
        if listing.get(src) not in (None, ""):
            args += [flag, str(listing[src])]
    data = _rentcast_cli(args)
    if not isinstance(data, dict) or data.get("rent") is None:
        raise FetchBlocked("rentcast-avm: connector returned no estimate")
    return {"rent_estimate": _num(data.get("rent")),
            "rent_range_low": _num(data.get("rentRangeLow")),
            "rent_range_high": _num(data.get("rentRangeHigh")), "raw": data}


# ---------------------------------------------------------------------------
# Source: Apartments.com via RealtyAPI (realtyapi.io), raw key or Muse vault
# ---------------------------------------------------------------------------

REALTYAPI_APARTMENTS_URL = "https://apartments.realtyapi.io/search/bylocation"
REALTYAPI_CLI = os.path.expanduser("~/workspace/custom-connectors/custom.realtyapi/bin/realtyapi_api.py")


def _parse_money_range(text: object) -> tuple[float | None, float | None]:
    """Parse a portal range string like "$2,000 - 3,355" into (low, high)."""
    if not isinstance(text, str):
        return None, None
    nums = [_num(p.replace("$", "").replace(",", "").strip())
            for p in text.split("-")]
    nums = [n for n in nums if n is not None]
    if not nums:
        return None, None
    return nums[0], nums[-1]


def normalize_realtyapi_apartments(item: dict) -> dict[str, object]:
    """Normalize one RealtyAPI Apartments search placard.

    Search results are building-level placards: rent and beds come back as
    ranges ("$2,000 - 3,355", "Studio - 4 Beds"), not unit facts, so rent
    records the range's low end (also what a searcher sees first) and
    beds/baths/sqft stay unknown. Missing facts never exclude in the
    ranker. Unit-level detail lives behind the /details endpoints, which
    are not wired here. The raw placard is preserved as evidence.
    """
    ext_id = str(item.get("listingKey") or "")
    addr = item.get("address") if isinstance(item.get("address"), dict) else {}
    address = str(item.get("oneLineAddress") or "").strip()
    if not address:
        parts = [str(addr.get("lineOne") or ""), str(addr.get("city") or ""),
                 str(addr.get("state") or ""), str(addr.get("postalCode") or "")]
        address = ", ".join(p for p in parts if p).strip()
    rent_low, rent_high = _parse_money_range(item.get("rentRange") or item.get("priceRange"))
    manager = item.get("propertyManager") if isinstance(item.get("propertyManager"), dict) else {}
    amenities = [str(a) for a in item.get("amenityNames") or [] if a]
    desc_parts = [
        str(item.get("name") or ""),
        address,
        f"Rents {item.get('rentRange')}" if item.get("rentRange") else "",
        str(item.get("bedRange") or ""),
        str(item.get("propertyType") or ""),
        str(item.get("availabilityText") or ""),
        "Amenities: " + ", ".join(amenities) if amenities else "",
        f"Manager: {manager.get('name')}" if manager.get("name") else "",
        f"Phone: {item.get('phone')}" if item.get("phone") else "",
    ]
    description = clean_text(". ".join(p for p in desc_parts if p))
    return {
        "key": f"realtyapi-apartments:{ext_id}" if ext_id else
               f"realtyapi-apartments:{hashlib.sha256(address.encode()).hexdigest()[:16]}",
        "url": "",
        "source": "realtyapi-apartments",
        "external_id": ext_id,
        "title": str(item.get("name") or "") or address or "Untitled listing",
        "address": address,
        "city": str(addr.get("city") or ""),
        "state": str(addr.get("state") or ""),
        "zip": str(addr.get("postalCode") or ""),
        "rent": rent_low,
        "beds": None,
        "baths": None,
        "sqft": None,
        "property_type": str(item.get("propertyType") or ""),
        "status": str(item.get("availabilityText") or ""),
        "listed_date": "",
        "days_on_market": None,
        "latitude": _num(item.get("latitude")),
        "longitude": _num(item.get("longitude")),
        "description": description,
        "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
        "evidence_text": json.dumps(item, indent=2),
        "evidence_suffix": ".json",
    }


def _realtyapi_cli(args: list[str], timeout: int = 120) -> object:
    """Run the custom.realtyapi connector CLI; parsed JSON or None.

    Same vault pattern as _rentcast_cli: the credential is swapped in by
    the egress proxy, so no raw key exists in this process.
    """
    if not os.path.exists(REALTYAPI_CLI):
        return None
    try:
        proc = subprocess.run([sys.executable, REALTYAPI_CLI, *args],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"realtyapi connector CLI: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"realtyapi connector CLI failed: {proc.stdout.strip()[:200]}", file=sys.stderr)
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("realtyapi connector CLI: unparseable output", file=sys.stderr)
        return None
    if isinstance(data, dict) and data.get("error"):
        print(f"realtyapi connector: {data['error']}", file=sys.stderr)
        return None
    return data


def _realtyapi_apartments_params(query: dict[str, object], limit: int) -> dict[str, object]:
    params: dict[str, object] = {"location": str(query.get("location") or ""),
                                 "resultCount": max(1, min(limit, 500))}
    for key in ("sortOrder", "propertyType", "priceRange", "bedRange", "bathRange",
                "squareFeetRange", "petPolicy", "amenities", "specialties"):
        if query.get(key) not in (None, ""):
            params[key] = query[key]
    return params


def realtyapi_apartments_listings(api_key: str, query: dict[str, object],
                                  limit: int = 50, timeout: int = 60) -> list[dict[str, object]]:
    """Search Apartments.com rentals via RealtyAPI with a raw key.

    One request per location (each call costs 1 credit; free tier is
    250/month). Query: {"location": "City, ST" or a ZIP, plus any of the
    API's filter params such as priceRange "min:1500,max:4000"}.
    """
    if not api_key or not query.get("location"):
        return []
    url = REALTYAPI_APARTMENTS_URL + "?" + urllib.parse.urlencode(
        _realtyapi_apartments_params(query, limit))
    data = fetch_json(url, timeout=timeout, headers={"x-realtyapi-key": api_key})
    items = data.get("searchResults") if isinstance(data, dict) else None
    return [normalize_realtyapi_apartments(it) for it in (items or []) if isinstance(it, dict)]


def realtyapi_apartments_via_connector(query: dict[str, object],
                                       limit: int = 50) -> list[dict[str, object]]:
    """Same search via the stored Muse credential (custom.realtyapi)."""
    if not query.get("location"):
        return []
    args = ["--provider", "apartments", "--path", "/search/bylocation"]
    for k, v in _realtyapi_apartments_params(query, limit).items():
        args += ["--param", f"{k}={v}"]
    data = _realtyapi_cli(args)
    items = data.get("searchResults") if isinstance(data, dict) else None
    return [normalize_realtyapi_apartments(it) for it in (items or []) if isinstance(it, dict)]


def realtyapi_locations(prefs: dict) -> list[str]:
    """Build RealtyAPI location strings from preferences (ZIP wins)."""
    out: list[str] = []
    for loc in prefs.get("locations") or []:
        if not isinstance(loc, dict):
            continue
        zip_code = str(loc.get("zipCode") or loc.get("zip") or "")
        city, state = str(loc.get("city") or ""), str(loc.get("state") or "")
        if zip_code:
            out.append(zip_code)
        elif city and state:
            out.append(f"{city}, {state}")
        elif city:
            out.append(city)
    return out


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
    CREATE TABLE IF NOT EXISTS enrichment (
      listing_key TEXT NOT NULL REFERENCES listings(key),
      provider TEXT NOT NULL, payload TEXT NOT NULL, fetched_at TEXT NOT NULL,
      PRIMARY KEY (listing_key, provider)
    );
    """)
    # v0.2 migration: enrichment columns land on existing ledgers in place.
    existing = {row[1] for row in con.execute("PRAGMA table_info(listings)")}
    for column, ddl in (("walk_score", "REAL"), ("transit_score", "REAL"),
                        ("bike_score", "REAL"), ("rent_estimate", "REAL"),
                        ("fmr_rent", "REAL"), ("flood_zone", "TEXT")):
        if column not in existing:
            con.execute(f"ALTER TABLE listings ADD COLUMN {column} {ddl}")
    con.commit()
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
# Source: HasData Zillow search (optional, free key)
# ---------------------------------------------------------------------------

def normalize_hasdata(item: dict) -> dict[str, object]:
    """Normalize one HasData Zillow listing row (defensive shape).

    HasData rows carry zpid/url/price/beds/baths/area/address plus a
    latLong-style coordinate pair in some shapes; every field is read with
    a fallback and the raw row is kept as evidence.
    """
    zpid = str(item.get("zpid") or item.get("id") or "")
    url = str(item.get("url") or (f"https://www.zillow.com/homedetails/{zpid}_zpid/" if zpid else ""))
    addr = item.get("address")
    if isinstance(addr, dict):
        address = ", ".join(p for p in [str(addr.get("streetAddress") or ""),
                                        str(addr.get("city") or ""),
                                        f"{addr.get('state') or ''} {addr.get('zipcode') or ''}".strip()] if p)
        city, state, zip_code = (str(addr.get("city") or ""), str(addr.get("state") or ""),
                                 str(addr.get("zipcode") or ""))
    else:
        address = str(addr or "")
        city = str(item.get("city") or "")
        state = str(item.get("state") or "")
        zip_code = str(item.get("zipcode") or item.get("zip") or "")
    lat = _num(item.get("latitude"))
    lon = _num(item.get("longitude"))
    latlong = item.get("latLong") if isinstance(item.get("latLong"), dict) else {}
    if lat is None:
        lat = _num(latlong.get("latitude"))
    if lon is None:
        lon = _num(latlong.get("longitude"))
    rent = _num(item.get("unformattedPrice") if item.get("unformattedPrice") is not None else item.get("price"))
    beds = _num(item.get("beds") if item.get("beds") is not None else item.get("bedrooms"))
    baths = _num(item.get("baths") if item.get("baths") is not None else item.get("bathrooms"))
    sqft = _num(item.get("area") if item.get("area") is not None else item.get("livingArea"))
    dom = item.get("daysOnZillow")
    description = clean_text(". ".join(p for p in [
        address, f"${rent:,.0f}/mo" if rent else "",
        f"{beds:g} bed" if beds is not None else "",
        f"{baths:g} bath" if baths is not None else "",
        f"{sqft:,.0f} sqft" if sqft else "",
        str(item.get("homeType") or ""), str(item.get("description") or "")] if p))
    return {
        "key": url or f"hasdata:{zpid}",
        "url": url,
        "source": "hasdata-zillow",
        "external_id": zpid,
        "title": address or "Untitled listing",
        "address": address,
        "city": city,
        "state": state,
        "zip": zip_code,
        "rent": rent,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "property_type": str(item.get("homeType") or ""),
        "status": str(item.get("status") or item.get("statusType") or ""),
        "listed_date": "",
        "days_on_market": dom if isinstance(dom, (int, float)) else None,
        "latitude": lat,
        "longitude": lon,
        "description": description,
        "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
        "evidence_text": json.dumps(item, indent=2),
        "evidence_suffix": ".json",
    }


def hasdata_listings(api_key: str, query: dict[str, object], limit: int = 41,
                     timeout: int = 60) -> list[dict[str, object]]:
    """Search Zillow for-rent listings through HasData's scraper API.

    GET https://api.hasdata.com/scrape/zillow/listing with an x-api-key
    header. Free plan: 1,000 credits/month (a search call costs credits per
    row returned), no card. One page per sync, like every other source.
    Skipped upstream when no HASDATA_API_KEY is configured.
    """
    if not api_key:
        return []
    params: dict[str, object] = {"type": str(query.get("type") or "forRent")}
    keyword = str(query.get("keyword") or "").strip()
    if not keyword:
        keyword = ", ".join(p for p in [str(query.get("city") or ""), str(query.get("state") or "")] if p)
    if keyword:
        params["keyword"] = keyword
    for src_key, dst_key in (("min_beds", "beds_min_"), ("max_rent", "price_max_"),
                             ("min_baths", "baths_min_")):
        if query.get(src_key) not in (None, ""):
            params[dst_key] = query[src_key]
    url = "https://api.hasdata.com/scrape/zillow/listing?" + urllib.parse.urlencode(params)
    data = fetch_json(url, timeout=timeout, headers={"x-api-key": api_key})
    return [normalize_hasdata(it) for it in _hasdata_rows(data)[:limit]
            if isinstance(it, dict)]


def _hasdata_rows(data: object) -> list:
    """Pull the listing rows out of a HasData response envelope."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("listings", "results", "properties", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


HASDATA_CLI = os.path.expanduser("~/workspace/custom-connectors/custom.hasdata/bin/hasdata_api.py")


def _hasdata_cli(args: list[str], timeout: int = 120) -> object:
    """Run the custom.hasdata connector CLI; parsed JSON or None.

    Same vault pattern as _rentcast_cli: the credential is swapped in by
    the egress proxy, so no raw key exists in this process.
    """
    if not os.path.exists(HASDATA_CLI):
        return None
    try:
        proc = subprocess.run([sys.executable, HASDATA_CLI, *args],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"hasdata connector CLI: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"hasdata connector CLI failed: {proc.stdout.strip()[:200]}", file=sys.stderr)
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("hasdata connector CLI: unparseable output", file=sys.stderr)
        return None
    if isinstance(data, dict) and data.get("error"):
        print(f"hasdata connector: {data['error']}", file=sys.stderr)
        return None
    return data


def hasdata_listings_via_connector(query: dict[str, object],
                                   limit: int = 41) -> list[dict[str, object]]:
    """HasData Zillow search via the stored Muse credential (custom.hasdata)."""
    keyword = str(query.get("keyword") or "").strip()
    if not keyword:
        keyword = ", ".join(p for p in [str(query.get("city") or ""),
                                        str(query.get("state") or "")] if p)
    if not keyword:
        return []
    args = ["zillow-listing", "--keyword", keyword,
            "--type", str(query.get("type") or "forRent")]
    for src, flag in (("min_beds", "--min-beds"), ("max_rent", "--max-rent"),
                      ("min_baths", "--min-baths")):
        if query.get(src) not in (None, ""):
            args += [flag, str(query[src])]
    data = _hasdata_cli(args)
    return [normalize_hasdata(it) for it in _hasdata_rows(data)[:limit]
            if isinstance(it, dict)]


# ---------------------------------------------------------------------------
# Enrichment providers (v0.2) — all optional; a missing key or coordinate
# skips that provider with a note, never a crash and never a fake value.
# ---------------------------------------------------------------------------

ENRICH_PROVIDERS = ("walkscore", "rentcast-avm", "hud-fmr", "census-acs", "fema-flood", "airnow")


def walkscore_scores(api_key: str, lat: float | None, lon: float | None,
                     address: str, timeout: int = 25) -> dict:
    """Walk/Transit/Bike scores from api.walkscore.com (free 5,000 calls/day)."""
    if not api_key:
        raise FetchBlocked("walkscore: no WALKSCORE_API_KEY configured")
    params = {"format": "json", "transit": 1, "bike": 1, "wsapikey": api_key,
              "address": address}
    if lat is not None and lon is not None:
        params["lat"] = lat
        params["lon"] = lon
    data = fetch_json("https://api.walkscore.com/score?" + urllib.parse.urlencode(params), timeout=timeout)
    if not isinstance(data, dict) or data.get("status") != 1:
        raise FetchBlocked(f"walkscore: status {data.get('status') if isinstance(data, dict) else '?'}")
    out = {"walk_score": _num(data.get("walkscore")), "raw": data}
    transit = data.get("transit") if isinstance(data.get("transit"), dict) else {}
    bike = data.get("bike") if isinstance(data.get("bike"), dict) else {}
    out["transit_score"] = _num(transit.get("score"))
    out["bike_score"] = _num(bike.get("score"))
    return out


def rentcast_avm(api_key: str, listing: dict, timeout: int = 25) -> dict:
    """Market rent estimate from RentCast AVM (same key as the listings API).

    With no raw key, falls back to the stored Muse credential
    (custom.rentcast connector), raising FetchBlocked if that fails too.
    """
    if not api_key:
        return rentcast_avm_via_connector(listing)
    address = str(listing.get("address") or "")
    if not address:
        raise FetchBlocked("rentcast-avm: listing has no address")
    params: dict[str, object] = {"address": address}
    for src, dst in (("beds", "bedrooms"), ("baths", "bathrooms"),
                     ("sqft", "squareFootage"), ("property_type", "propertyType")):
        if listing.get(src) not in (None, ""):
            params[dst] = listing[src]
    url = "https://api.rentcast.io/v1/avm/rent/long-term?" + urllib.parse.urlencode(params)
    data = fetch_json(url, timeout=timeout, headers={"X-Api-Key": api_key})
    if not isinstance(data, dict) or data.get("rent") is None:
        raise FetchBlocked("rentcast-avm: no estimate returned")
    return {"rent_estimate": _num(data.get("rent")),
            "rent_range_low": _num(data.get("rentRangeLow")),
            "rent_range_high": _num(data.get("rentRangeHigh")), "raw": data}


_HUD_BED_LABELS = {0: "Efficiency", 1: "One-Bedroom", 2: "Two-Bedroom",
                   3: "Three-Bedroom", 4: "Four-Bedroom"}


def _county_fips(lat: float | None, lon: float | None, timeout: int = 25) -> tuple[str, str]:
    """Resolve coordinates to (state, county) FIPS via the Census geocoder.

    Keyless; the same geocoder the census-acs provider uses. Raises
    FetchBlocked when there are no coordinates or no county is found.
    """
    if lat is None or lon is None:
        raise FetchBlocked("hud-fmr: listing has no coordinates to resolve its county")
    geo_url = ("https://geocoding.geo.census.gov/geocoder/geographies/coordinates?"
               + urllib.parse.urlencode({"x": lon, "y": lat, "benchmark": "Public_AR_Current",
                                         "vintage": "Current_Current", "format": "json"}))
    geo = fetch_json(geo_url, timeout=timeout)
    counties = ((((geo.get("result") or {}).get("geographies") or {}).get("Counties") or [])
                if isinstance(geo, dict) else [])
    if not counties:
        raise FetchBlocked("hud-fmr: Census geocoder found no county for these coordinates")
    return str(counties[0].get("STATE") or ""), str(counties[0].get("COUNTY") or "")


def hud_fmr(token: str, listing: dict, timeout: int = 25) -> dict:
    """HUD Fair Market Rent for a listing's FMR area (free HUD token).

    The HUD API has no by-ZIP endpoint: FMRs are published per county or
    metro entity, and ZIP-level figures exist only inside small-area
    metro responses. So this resolves the listing's county from its
    coordinates (Census geocoder, keyless), queries that county's FMR
    entity, and when the response carries per-ZIP small-area rows it
    picks the listing's own ZIP. (The v0.2 version passed the ZIP as the
    entity id, which HUD rejects with HTTP 400; live testing in v0.4
    caught it.) With no raw token, falls back to the stored Muse
    credential (custom.hud), raising FetchBlocked if that fails too.
    """
    zip_code = str(listing.get("zip") or "")
    if not zip_code:
        raise FetchBlocked("hud-fmr: listing has no ZIP")
    state, county = _county_fips(listing.get("latitude"), listing.get("longitude"),
                                 timeout=timeout)
    entity = f"{state}{county}99999"
    if not token:
        return hud_fmr_via_connector(entity, listing)
    url = f"https://www.huduser.gov/hudapi/public/fmr/data/{entity}"
    data = fetch_json(url, timeout=timeout, headers={"Authorization": f"Bearer {token}"})
    return _extract_fmr(data, listing.get("beds"), zip_code)


def _extract_fmr(data: object, beds: float | None, zip_code: str) -> dict:
    """Pick the bedroom figure out of an FMR entity response.

    basicdata is a dict of area-level figures for most areas, or a list
    of per-ZIP rows for small-area metros (then the listing's ZIP wins,
    with the "MSA level" row as fallback). Figures may be strings
    ("948.0") or numbers, so they go through _num either way.
    """
    label = _HUD_BED_LABELS.get(int(beds) if beds is not None else 2, "Two-Bedroom")
    node = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
    if not isinstance(node, dict):
        raise FetchBlocked("hud-fmr: unexpected response shape")
    basic = node.get("basicdata")
    row: object = basic
    smallarea = False
    if isinstance(basic, list):
        smallarea = True
        row = next((r for r in basic if isinstance(r, dict)
                    and str(r.get("zip_code")) == zip_code), None)
        if row is None:
            row = next((r for r in basic if isinstance(r, dict)
                        and str(r.get("zip_code")) == "MSA level"), None)
        if row is None and basic:
            row = basic[0]
    if not isinstance(row, dict):
        raise FetchBlocked("hud-fmr: no basicdata in response")
    value = _num(row.get(label))
    if value is None:
        raise FetchBlocked(f"hud-fmr: no {label} figure found in response for ZIP {zip_code}")
    return {"fmr_rent": value, "bedroom_label": label,
            "area_name": str(node.get("area_name") or node.get("county_name")
                             or node.get("metro_name") or ""),
            "smallarea": smallarea, "raw": data}


HUD_CLI = os.path.expanduser("~/workspace/custom-connectors/custom.hud/bin/hud_api.py")


def _hud_cli(args: list[str], timeout: int = 90) -> object:
    """Run the custom.hud connector CLI; parsed JSON or None.

    Same vault pattern as _rentcast_cli: the token is swapped in by the
    egress proxy, so no raw token exists in this process.
    """
    if not os.path.exists(HUD_CLI):
        return None
    try:
        proc = subprocess.run([sys.executable, HUD_CLI, *args],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"hud connector CLI: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"hud connector CLI failed: {proc.stdout.strip()[:200]}", file=sys.stderr)
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("hud connector CLI: unparseable output", file=sys.stderr)
        return None
    if isinstance(data, dict) and data.get("error"):
        print(f"hud connector: {data['error']}", file=sys.stderr)
        return None
    return data


def hud_fmr_via_connector(entity: str, listing: dict) -> dict:
    """HUD FMR via the stored Muse credential. Raises FetchBlocked."""
    data = _hud_cli(["fmr-data", "--entity", str(entity)])
    if data is None:
        raise FetchBlocked("hud-fmr: Muse connector returned nothing "
                           "(connect HUD in Muse or set HUD_API_TOKEN)")
    return _extract_fmr(data, listing.get("beds"), str(listing.get("zip") or ""))


def census_acs(lat: float | None, lon: float | None, timeout: int = 25) -> dict:
    """Neighborhood medians from the Census ACS (keyless).

    Geocoder (keyless) resolves coordinates to a census tract, then ACS
    5-year variables: B25064 median gross rent, B19013 median household
    income. Figures describe the tract, not the property.
    """
    if lat is None or lon is None:
        raise FetchBlocked("census-acs: listing has no coordinates")
    geo_url = ("https://geocoding.geo.census.gov/geocoder/geographies/coordinates?"
               + urllib.parse.urlencode({"x": lon, "y": lat, "benchmark": "Public_AR_Current",
                                         "vintage": "Current_Current", "format": "json"}))
    geo = fetch_json(geo_url, timeout=timeout)
    tracts = (((geo.get("result") or {}).get("geographies") or {}).get("Census Tracts") or []) if isinstance(geo, dict) else []
    if not tracts:
        raise FetchBlocked("census-acs: geocoder found no tract for these coordinates")
    tract = tracts[0]
    state, county, tract_code = str(tract.get("STATE") or ""), str(tract.get("COUNTY") or ""), str(tract.get("TRACT") or "")
    acs_url = ("https://api.census.gov/data/2023/acs/acs5?get=B25064_001E,B19013_001E"
               f"&for=tract:{tract_code}&in=state:{state}&in=county:{county}")
    rows = fetch_json(acs_url, timeout=timeout)
    if not isinstance(rows, list) or len(rows) < 2:
        raise FetchBlocked("census-acs: no ACS row for this tract")
    header, values = rows[0], rows[1]
    out = {"tract_geoid": str(tract.get("GEOID") or ""), "raw": rows}
    for var, name in (("B25064_001E", "tract_median_gross_rent"), ("B19013_001E", "tract_median_household_income")):
        if var in header:
            value = _num(values[header.index(var)])
            if value is not None and value >= 0:
                out[name] = value
    return out


def fema_flood(lat: float | None, lon: float | None, timeout: int = 25) -> dict:
    """Flood zone from FEMA's NFHL map service (keyless ArcGIS REST)."""
    if lat is None or lon is None:
        raise FetchBlocked("fema-flood: listing has no coordinates")
    params = {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
              "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
              "outFields": "FLD_ZONE,ZONE_SUBTY", "returnGeometry": "false", "f": "json"}
    url = ("https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query?"
           + urllib.parse.urlencode(params))
    data = fetch_json(url, timeout=timeout)
    features = data.get("features") if isinstance(data, dict) else None
    if not features:
        return {"flood_zone": "X (unmapped)", "raw": data}
    attrs = features[0].get("attributes") or {}
    zone = str(attrs.get("FLD_ZONE") or "").strip()
    subty = str(attrs.get("ZONE_SUBTY") or "").strip()
    return {"flood_zone": f"{zone} ({subty})" if zone and subty else (zone or "unknown"), "raw": data}


def airnow_aqi(api_key: str, lat: float | None, lon: float | None, timeout: int = 25) -> dict:
    """Current air quality (max AQI + category) from AirNow (free key)."""
    if not api_key:
        raise FetchBlocked("airnow: no AIRNOW_API_KEY configured")
    if lat is None or lon is None:
        raise FetchBlocked("airnow: listing has no coordinates")
    from datetime import date
    params = {"format": "application/json", "latitude": lat, "longitude": lon,
              "date": date.today().isoformat(), "distance": 25, "API_KEY": api_key}
    data = fetch_json("https://www.airnowapi.org/aq/observation/latLong/current/?"
                      + urllib.parse.urlencode(params), timeout=timeout)
    if not isinstance(data, list) or not data:
        raise FetchBlocked("airnow: no observations near these coordinates")
    worst = max(data, key=lambda obs: obs.get("AQI", 0) if isinstance(obs, dict) else 0)
    category = worst.get("Category") if isinstance(worst.get("Category"), dict) else {}
    return {"aqi": worst.get("AQI"), "aqi_category": str(category.get("Name") or ""),
            "aqi_parameter": str(worst.get("ParameterName") or ""), "raw": data}


def store_enrichment(con: sqlite3.Connection, key: str, provider: str, payload: dict) -> None:
    con.execute("""
      INSERT INTO enrichment (listing_key, provider, payload, fetched_at) VALUES (?, ?, ?, ?)
      ON CONFLICT(listing_key, provider) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at
    """, (key, provider, json.dumps(payload), now()))
    updates = {col: payload[col] for col in
               ("walk_score", "transit_score", "bike_score", "rent_estimate", "fmr_rent", "flood_zone")
               if payload.get(col) is not None}
    if updates:
        sets = ", ".join(f"{col}=?" for col in updates)
        con.execute(f"UPDATE listings SET {sets} WHERE key=?", (*updates.values(), key))
    con.commit()


def enrich_listing(con: sqlite3.Connection, listing: dict, providers: list[str],
                   keys: dict[str, str], force: bool = False) -> list[str]:
    """Run the chosen providers for one listing; returns human-readable notes."""
    notes: list[str] = []
    key = str(listing["key"])
    existing = set()
    if not force:
        existing = {r[0] for r in con.execute(
            "SELECT provider FROM enrichment WHERE listing_key = ?", (key,))}
    lat, lon = listing.get("latitude"), listing.get("longitude")
    for provider in providers:
        if provider in existing:
            notes.append(f"{provider}: already enriched (use --force to refresh)")
            continue
        try:
            if provider == "walkscore":
                payload = walkscore_scores(keys.get("walkscore", ""), lat, lon, str(listing.get("address") or ""))
            elif provider == "rentcast-avm":
                payload = rentcast_avm(keys.get("rentcast", ""), listing)
            elif provider == "hud-fmr":
                payload = hud_fmr(keys.get("hud", ""), listing)
            elif provider == "census-acs":
                payload = census_acs(lat, lon)
            elif provider == "fema-flood":
                payload = fema_flood(lat, lon)
            elif provider == "airnow":
                payload = airnow_aqi(keys.get("airnow", ""), lat, lon)
            else:
                notes.append(f"{provider}: unknown provider, skipped")
                continue
        except FetchBlocked as exc:
            notes.append(str(exc))
            continue
        store_enrichment(con, key, provider, payload)
        notes.append(f"{provider}: ok")
    return notes


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
                queries = [source.get("query") or {}]
                if source.get("from_preferences"):
                    queries = preference_queries(prefs) or queries
                for q in queries:
                    if not isinstance(q, dict):
                        continue
                    if key:
                        got = rentcast_listings(key, q, limit=limit)
                    else:
                        # No raw key: use the credential connected in Muse
                        # (custom.rentcast), if any. Empty result + a stderr
                        # note when neither route is available.
                        got = rentcast_listings_via_connector(q, limit=limit)
                    records.extend(got)
                    print(f"{name}: rentcast query {q} -> {len(got)} listings")
                if not key and not records:
                    notes.append(f"{name}: no raw RENTCAST_API_KEY and the Muse "
                                 f"connector returned nothing (see stderr); add a key "
                                 f"to ~/.config/apartmentfinder.env or connect RentCast in Muse")
            elif kind == "realtyapi-apartments":
                key = api_key_for(str(source.get("api_key_env") or "REALTYAPI_API_KEY"))
                base_query = source.get("query") or {}
                locations = [str(base_query["location"])] if base_query.get("location") else []
                if source.get("from_preferences"):
                    locations = realtyapi_locations(prefs) or locations
                for loc in locations:
                    q = {**base_query, "location": loc}
                    if key:
                        got = realtyapi_apartments_listings(key, q, limit=limit)
                    else:
                        got = realtyapi_apartments_via_connector(q, limit=limit)
                    records.extend(got)
                    print(f"{name}: realtyapi-apartments location {loc!r} -> {len(got)} listings")
                if not locations:
                    notes.append(f"{name}: no location configured (set query.location "
                                 f"or from_preferences with locations in preferences.json)")
                elif not key and not records:
                    notes.append(f"{name}: no raw REALTYAPI_API_KEY and the Muse "
                                 f"connector returned nothing (see stderr); add a key "
                                 f"to ~/.config/apartmentfinder.env or connect RealtyAPI in Muse")
            elif kind == "realty-us":
                key = api_key_for(str(source.get("api_key_env") or "RAPIDAPI_KEY"))
                if not key:
                    notes.append(f"{name}: skipped, no RAPIDAPI_KEY configured")
                    continue
                records = realty_us_listings(key, source.get("query") or {}, limit=limit)
                print(f"{name}: realty-us -> {len(records)} listings")
            elif kind == "hasdata-zillow":
                key = api_key_for(str(source.get("api_key_env") or "HASDATA_API_KEY"))
                query = source.get("query") or {}
                if key:
                    records = hasdata_listings(key, query, limit=limit)
                else:
                    records = hasdata_listings_via_connector(query, limit=limit)
                print(f"{name}: hasdata-zillow -> {len(records)} listings")
                if not key and not records:
                    notes.append(f"{name}: no raw HASDATA_API_KEY and the Muse "
                                 f"connector returned nothing (see stderr); add a key "
                                 f"to ~/.config/apartmentfinder.env or connect HasData in Muse")
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
                f"walk {row['walk_score']:g}" if "walk_score" in row.keys() and row["walk_score"] is not None else "",
                (f"est {fmt_money(row['rent_estimate'])}" if "rent_estimate" in row.keys() and row["rent_estimate"] else ""),
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


def cmd_enrich(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config).resolve()
    cfg = load_config(cfg_path)
    root = data_root(cfg, cfg_path)
    con = database(root)
    enrich_cfg = cfg.get("enrichment") if isinstance(cfg.get("enrichment"), dict) else {}
    providers = args.provider or enrich_cfg.get("providers") or list(ENRICH_PROVIDERS)
    providers = [str(p) for p in providers]
    keys = {
        "walkscore": api_key_for("WALKSCORE_API_KEY"),
        "rentcast": api_key_for("RENTCAST_API_KEY"),
        "hud": api_key_for("HUD_API_TOKEN"),
        "airnow": api_key_for("AIRNOW_API_KEY"),
    }
    if args.key:
        rows = [dict(r) for r in con.execute("SELECT * FROM listings WHERE key = ?", (args.key,))]
        if not rows:
            raise SystemExit(f"listing not in the local ledger: {args.key}")
    else:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM listings ORDER BY last_checked DESC LIMIT ?", (args.limit,))]
    done = 0
    for listing in rows:
        notes = enrich_listing(con, listing, providers, keys, force=args.force)
        ok = sum(1 for n in notes if n.endswith(": ok"))
        if ok:
            done += 1
        print(f"- {listing['title']}: " + "; ".join(notes))
    con.close()
    print(f"enrich complete: {done} of {len(rows)} listings gained new data")
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
    enriched = con.execute("SELECT COUNT(DISTINCT listing_key) FROM enrichment").fetchone()[0]
    if enriched:
        print(f"enriched listings: {enriched} (run the ranker to use walk scores and rent estimates)")
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

    enrich = sub.add_parser("enrich", help="add Walk Score / rent estimate / HUD / Census / flood / air data to captured listings")
    enrich.add_argument("--key", help="only this listing key (default: all, newest first)")
    enrich.add_argument("--limit", type=int, default=25)
    enrich.add_argument("--provider", action="append", choices=ENRICH_PROVIDERS,
                        help="repeatable; default is the config's enrichment.providers list")
    enrich.add_argument("--force", action="store_true", help="re-fetch providers already recorded")
    enrich.set_defaults(func=cmd_enrich)

    report = sub.add_parser("report", help="ledger summary")
    report.set_defaults(func=cmd_report)
    return p


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
