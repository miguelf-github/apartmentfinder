import argparse
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("apartmentfinder", Path(__file__).parents[1] / "apartmentfinder.py")
apartmentfinder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apartmentfinder)

RANK_SPEC = importlib.util.spec_from_file_location("apartmentfinder_rank", Path(__file__).parents[1] / "tools" / "apartmentfinder_rank.py")
ranker = importlib.util.module_from_spec(RANK_SPEC)
RANK_SPEC.loader.exec_module(ranker)


class RentCastTests(unittest.TestCase):
    def test_normalize_full_record(self):
        item = {
            "id": "abc123", "formattedAddress": "1 Main St, Springfield, IL 62701",
            "city": "Springfield", "state": "IL", "zipCode": "62701",
            "price": 2400, "bedrooms": 2, "bathrooms": 1.5, "squareFootage": 900,
            "propertyType": "Apartment", "status": "Active", "daysOnMarket": 4,
            "listedDate": "2026-10-28T00:00:00Z", "latitude": 37.47, "longitude": -122.14,
            "listingAgent": {"name": "Ana", "phone": "555-0100"},
        }
        rec = apartmentfinder.normalize_rentcast(item)
        self.assertEqual(rec["key"], "rentcast:abc123")
        self.assertEqual(rec["rent"], 2400.0)
        self.assertEqual(rec["beds"], 2.0)
        self.assertEqual(rec["zip"], "62701")
        self.assertIn("Ana", rec["description"])
        self.assertTrue(rec["evidence_text"].startswith("{"))

    def test_normalize_sparse_record(self):
        rec = apartmentfinder.normalize_rentcast({"addressLine1": "2 Oak Ave", "city": "Springfield"})
        self.assertTrue(rec["key"].startswith("rentcast:"))
        self.assertIsNone(rec["rent"])
        self.assertIn("2 Oak Ave", rec["title"])

    def test_rentcast_requires_key(self):
        self.assertEqual(apartmentfinder.rentcast_listings("", {"city": "X"}), [])

    def test_rentcast_parses_list_response(self):
        payload = [{"id": "1", "formattedAddress": "1 A St", "price": 2000}]
        with mock.patch.object(apartmentfinder, "fetch_json", return_value=payload) as fj:
            records = apartmentfinder.rentcast_listings("k", {"city": "Springfield", "state": "IL"}, limit=10)
        url = fj.call_args[0][0]
        self.assertIn("api.rentcast.io/v1/listings/rental/long-term", url)
        self.assertIn("city=Springfield", url)
        self.assertEqual(fj.call_args.kwargs["headers"]["X-Api-Key"], "k")
        self.assertEqual(len(records), 1)


class ExtractTests(unittest.TestCase):
    def test_jsonld_apartment(self):
        page = ('<script type="application/ld+json">{"@type":"Apartment","name":"Maple Court 2BR",'
                '"description":"Bright unit with in-unit laundry","numberOfRooms":2,'
                '"address":{"streetAddress":"9 Maple Ct","addressLocality":"Springfield",'
                '"addressRegion":"IL","postalCode":"62701"},'
                '"offers":{"price":2650,"availability":"https://schema.org/InStock"}}</script>')
        rec = apartmentfinder.extract_listing("https://example.test/listing/1", page, "manual-urls")
        self.assertEqual(rec["key"], "https://example.test/listing/1")
        self.assertEqual(rec["rent"], 2650.0)
        self.assertEqual(rec["city"], "Springfield")
        self.assertEqual(rec["beds"], 2.0)

    def test_fallback_heading_and_rent(self):
        page = "<h1>Sunny studio near downtown</h1><p>Only $1,895/mo, available now.</p>"
        rec = apartmentfinder.extract_listing("https://example.test/x", page, "manual-urls")
        self.assertEqual(rec["title"], "Sunny studio near downtown")
        self.assertEqual(rec["rent"], 1895.0)

    def test_challenge_detection(self):
        self.assertTrue(apartmentfinder.is_challenge_page("<title>just a moment</title> cf-chl-1"))
        self.assertFalse(apartmentfinder.is_challenge_page("this apartment mentions a captcha-free application"))


class ImportAndSyncTests(unittest.TestCase):
    def test_read_url_list_discards_other_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "listing-urls.jsonl"
            path.write_text('{"url":"https://example.test/r/1","phone":"private"}\n{"url":"https://example.test/r/2","note":"x"}\n')
            self.assertEqual(apartmentfinder.read_url_list(path),
                             ["https://example.test/r/1", "https://example.test/r/2"])

    def test_sync_url_import_and_dedup(self):
        page = "<h1>Cozy 1BR</h1><p>$2,100/mo</p>"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir()
            config = root / "config" / "targets.json"
            config.write_text(json.dumps({"data_dir": "ledger", "sources": [
                {"name": "manual-urls", "urls": ["https://example.test/r/1", "https://example.test/r/1"]}]}))
            args = argparse.Namespace(config=str(config), limit=10)
            with mock.patch.object(apartmentfinder, "fetch", side_effect=lambda url: (page, url)) as fetch:
                self.assertEqual(apartmentfinder.cmd_sync(args), 0)
            self.assertEqual(fetch.call_count, 1)
            con = sqlite3.connect(root / "ledger" / "apartmentfinder.sqlite3")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 1)

    def test_sync_rentcast_without_key_is_a_note_not_a_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir()
            config = root / "config" / "targets.json"
            config.write_text(json.dumps({"data_dir": "ledger", "sources": [
                {"name": "rentcast", "kind": "rentcast"}]}))
            args = argparse.Namespace(config=str(config), limit=10)
            with mock.patch.dict("os.environ", {}, clear=True), \
                 mock.patch.object(apartmentfinder, "_secret_from_file", return_value=""):
                self.assertEqual(apartmentfinder.cmd_sync(args), 0)
            con = sqlite3.connect(root / "ledger" / "apartmentfinder.sqlite3")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 0)

    def test_stage_and_track_flow(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir()
            config = root / "config" / "targets.json"
            config.write_text(json.dumps({"data_dir": "ledger", "sources": []}))
            (root / "ledger" / "evidence").mkdir(parents=True)
            con = apartmentfinder.database(root / "ledger")
            rec = apartmentfinder.normalize_rentcast({"id": "z9", "formattedAddress": "3 Pine St", "price": 2300})
            apartmentfinder.store_listing(con, root / "ledger", rec)
            con.close()
            stage = argparse.Namespace(config=str(config), key="rentcast:z9", note="liked it")
            self.assertEqual(apartmentfinder.cmd_stage(stage), 0)
            track = argparse.Namespace(config=str(config), key="rentcast:z9", state="tour_scheduled",
                                       note="", contact="Ana", tour_at="2026-11-08 14:00")
            self.assertEqual(apartmentfinder.cmd_track(track), 0)
            con = sqlite3.connect(root / "ledger" / "apartmentfinder.sqlite3")
            row = con.execute("SELECT state, contact, tour_at FROM tracker").fetchone()
            self.assertEqual(row, ("tour_scheduled", "Ana", "2026-11-08 14:00"))

    def test_stage_unknown_listing_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir()
            config = root / "config" / "targets.json"
            config.write_text(json.dumps({"data_dir": "ledger", "sources": []}))
            args = argparse.Namespace(config=str(config), key="rentcast:nope", note="")
            with self.assertRaises(SystemExit):
                apartmentfinder.cmd_stage(args)


class RankerGateTests(unittest.TestCase):
    PREFS = {"budget": {"max_rent": 2500, "target_rent": 2200},
             "layout": {"min_bedrooms": 2, "min_bathrooms": 1, "min_sqft": 700,
                        "property_types": []},
             "locations": [{"city": "Springfield", "state": "IL", "zip": "62701"}],
             "deal_breakers": ["no pets"], "must_have": [], "nice_to_have": ["in-unit laundry"]}

    def listing(self, **kw):
        base = {"rent": 2300.0, "beds": 2.0, "baths": 1.0, "sqft": 850.0,
                "city": "Springfield", "zip": "62701", "property_type": "Apartment",
                "days_on_market": 2, "description": "Bright, in-unit laundry", "title": "x"}
        base.update(kw)
        return base

    def test_over_budget_excluded(self):
        self.assertEqual(ranker.classify(self.listing(rent=2600.0), self.PREFS)["tier"], "excluded")

    def test_under_min_beds_excluded(self):
        self.assertEqual(ranker.classify(self.listing(beds=1.0), self.PREFS)["tier"], "excluded")

    def test_unknown_beds_not_excluded(self):
        result = ranker.classify(self.listing(beds=None), self.PREFS)
        self.assertNotEqual(result["tier"], "excluded")
        self.assertIn("bedrooms unknown", result["gaps"])

    def test_deal_breaker_excluded(self):
        result = ranker.classify(self.listing(description="Sorry, no pets allowed"), self.PREFS)
        self.assertEqual(result["tier"], "excluded")
        self.assertTrue(any("no pets" in r for r in result["reasons"]))

    def test_outside_locations_excluded(self):
        self.assertEqual(ranker.classify(self.listing(city="San Jose", zip="95112"), self.PREFS)["tier"], "excluded")

    def test_good_listing_scores_strong(self):
        result = ranker.classify(self.listing(), self.PREFS)
        self.assertEqual(result["tier"], "strong")

    def test_strict_must_have_excludes(self):
        prefs = dict(self.PREFS, must_have=["garage"], must_have_strict=True)
        self.assertEqual(ranker.classify(self.listing(), prefs)["tier"], "excluded")
        prefs_soft = dict(self.PREFS, must_have=["garage"], must_have_strict=False)
        self.assertNotEqual(ranker.classify(self.listing(), prefs_soft)["tier"], "excluded")


class HasDataTests(unittest.TestCase):
    def test_normalize_row(self):
        item = {"zpid": "12345", "url": "https://www.zillow.com/homedetails/12345_zpid/",
                "unformattedPrice": 2100, "beds": 2, "baths": 1, "area": 880,
                "address": {"streetAddress": "5 Elm St", "city": "Springfield", "state": "IL", "zipcode": "62701"},
                "latLong": {"latitude": 39.78, "longitude": -89.65},
                "homeType": "APARTMENT", "daysOnZillow": 3, "statusType": "FOR_RENT"}
        rec = apartmentfinder.normalize_hasdata(item)
        self.assertEqual(rec["source"], "hasdata-zillow")
        self.assertEqual(rec["rent"], 2100.0)
        self.assertEqual(rec["zip"], "62701")
        self.assertEqual(rec["latitude"], 39.78)
        self.assertEqual(rec["days_on_market"], 3)

    def test_requires_key_and_parses(self):
        self.assertEqual(apartmentfinder.hasdata_listings("", {"keyword": "X"}), [])
        payload = {"listings": [{"zpid": "9", "price": "$1,900/mo", "beds": 1,
                                 "address": "7 Oak Ave, Springfield, IL 62701"}]}
        with mock.patch.object(apartmentfinder, "fetch_json", return_value=payload) as fj:
            records = apartmentfinder.hasdata_listings("k", {"keyword": "Springfield, IL"}, limit=5)
        self.assertIn("api.hasdata.com/scrape/zillow/listing", fj.call_args[0][0])
        self.assertEqual(fj.call_args.kwargs["headers"]["x-api-key"], "k")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["rent"], 1900.0)


class EnrichmentProviderTests(unittest.TestCase):
    def test_walkscore_parse(self):
        payload = {"status": 1, "walkscore": 82, "transit": {"score": 55}, "bike": {"score": 70}}
        with mock.patch.object(apartmentfinder, "fetch_json", return_value=payload):
            out = apartmentfinder.walkscore_scores("k", 39.7, -89.6, "1 Main St")
        self.assertEqual(out["walk_score"], 82.0)
        self.assertEqual(out["transit_score"], 55.0)
        with self.assertRaises(apartmentfinder.FetchBlocked):
            apartmentfinder.walkscore_scores("", 39.7, -89.6, "1 Main St")

    def test_rentcast_avm_parse(self):
        payload = {"rent": 2350, "rentRangeLow": 2200, "rentRangeHigh": 2500}
        listing = {"address": "1 Main St, Springfield, IL 62701", "beds": 2.0, "baths": 1.0, "sqft": 900.0}
        with mock.patch.object(apartmentfinder, "fetch_json", return_value=payload):
            out = apartmentfinder.rentcast_avm("k", listing)
        self.assertEqual(out["rent_estimate"], 2350.0)

    def test_hud_fmr_finds_bedroom_figure(self):
        payload = {"data": {"basicdata": [{"zipcode": "62701", "Two-Bedroom": 1425, "One-Bedroom": 1180}]}}
        with mock.patch.object(apartmentfinder, "fetch_json", return_value=payload):
            out = apartmentfinder.hud_fmr("tok", "62701", 2.0)
        self.assertEqual(out["fmr_rent"], 1425.0)

    def test_census_acs_flow(self):
        geo = {"result": {"geographies": {"Census Tracts": [
            {"STATE": "17", "COUNTY": "167", "TRACT": "010000", "GEOID": "17167010000"}]}}}
        acs = [["B25064_001E", "B19013_001E", "state", "county", "tract"],
               ["1550", "72000", "17", "167", "010000"]]
        with mock.patch.object(apartmentfinder, "fetch_json", side_effect=[geo, acs]):
            out = apartmentfinder.census_acs(39.78, -89.65)
        self.assertEqual(out["tract_median_gross_rent"], 1550.0)
        self.assertEqual(out["tract_median_household_income"], 72000.0)

    def test_fema_zone(self):
        payload = {"features": [{"attributes": {"FLD_ZONE": "AE", "ZONE_SUBTY": ""}}]}
        with mock.patch.object(apartmentfinder, "fetch_json", return_value=payload):
            out = apartmentfinder.fema_flood(39.78, -89.65)
        self.assertEqual(out["flood_zone"], "AE")

    def test_airnow_worst_aqi(self):
        payload = [{"ParameterName": "PM2.5", "AQI": 42, "Category": {"Name": "Good"}},
                   {"ParameterName": "O3", "AQI": 61, "Category": {"Name": "Moderate"}}]
        with mock.patch.object(apartmentfinder, "fetch_json", return_value=payload):
            out = apartmentfinder.airnow_aqi("k", 39.78, -89.65)
        self.assertEqual(out["aqi"], 61)
        self.assertEqual(out["aqi_category"], "Moderate")


class EnrichFlowTests(unittest.TestCase):
    def test_migration_adds_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            con = apartmentfinder.database(Path(temp))
            cols = {r[1] for r in con.execute("PRAGMA table_info(listings)")}
            for col in ("walk_score", "rent_estimate", "fmr_rent", "flood_zone"):
                self.assertIn(col, cols)
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("enrichment", tables)
            con.close()

    def test_enrich_keyless_providers_and_skip_notes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "evidence").mkdir()
            con = apartmentfinder.database(root)
            rec = apartmentfinder.normalize_hasdata({
                "zpid": "77", "url": "https://www.zillow.com/homedetails/77_zpid/",
                "unformattedPrice": 2000, "beds": 2, "baths": 1, "area": 850,
                "address": "1 Main St, Springfield, IL 62701",
                "latLong": {"latitude": 39.78, "longitude": -89.65}})
            apartmentfinder.store_listing(con, root, rec)
            listing = dict(con.execute("SELECT * FROM listings").fetchone())

            def dispatch(url, timeout=25, headers=None):
                if "geocoding.geo.census.gov" in url:
                    return {"result": {"geographies": {"Census Tracts": [
                        {"STATE": "17", "COUNTY": "167", "TRACT": "010000", "GEOID": "17167010000"}]}}}
                if "api.census.gov" in url:
                    return [["B25064_001E", "B19013_001E"], ["1500", "70000"]]
                if "hazards.fema.gov" in url:
                    return {"features": [{"attributes": {"FLD_ZONE": "X", "ZONE_SUBTY": ""}}]}
                raise AssertionError(f"unexpected url {url}")

            with mock.patch.object(apartmentfinder, "fetch_json", side_effect=dispatch):
                notes = apartmentfinder.enrich_listing(
                    con, listing, ["walkscore", "census-acs", "fema-flood"], {})
            joined = "; ".join(notes)
            self.assertIn("walkscore: no WALKSCORE_API_KEY", joined)
            self.assertIn("census-acs: ok", joined)
            self.assertIn("fema-flood: ok", joined)
            row = con.execute("SELECT flood_zone FROM listings").fetchone()
            self.assertEqual(row[0], "X")
            providers = {r[0] for r in con.execute("SELECT provider FROM enrichment")}
            self.assertEqual(providers, {"census-acs", "fema-flood"})
            con.close()


class RankerEnrichmentTests(unittest.TestCase):
    def test_walk_gate_excludes_low_score_only_when_known(self):
        prefs = dict(RankerGateTests.PREFS, location_quality={"min_walk_score": 70})
        base = RankerGateTests().listing()
        self.assertEqual(ranker.classify(dict(base, walk_score=45.0), prefs)["tier"], "excluded")
        result = ranker.classify(dict(base, walk_score=None), prefs)
        self.assertNotEqual(result["tier"], "excluded")
        self.assertIn("walk score unknown", result["gaps"])

    def test_over_estimate_penalty_and_note(self):
        base = RankerGateTests().listing()
        plain = ranker.classify(base, RankerGateTests.PREFS)
        over = ranker.classify(dict(base, rent_estimate=1800.0), RankerGateTests.PREFS)
        self.assertLess(over["score"], plain["score"])
        self.assertTrue(any("over RentCast estimate" in n for n in over["reasons"]))


class PreferenceQueryTests(unittest.TestCase):
    def test_queries_from_locations(self):
        prefs = {"locations": [{"city": "Springfield", "state": "IL", "zip": "62701"},
                               {"city": "Shelbyville", "state": "IL"}]}
        queries = apartmentfinder.preference_queries(prefs)
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0]["zipCode"], "62701")
        self.assertEqual(queries[1]["city"], "Shelbyville")


if __name__ == "__main__":
    unittest.main()
