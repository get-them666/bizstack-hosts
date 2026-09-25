import unittest
from unittest import mock

import str_seed_source as src

# Real Myrtle Beach listing IDs verified live against StayAPI on 2026-09-25.
MYRTLE_3BR = 885003882744240148      # 3BR/5bd/2BA sleeps 7, 4.87*, 38 reviews, $284
MYRTLE_3BR_B = 1334048487609899272   # 3BR/4bd/3BA sleeps 8, 4.55*, 11 reviews, $156.67
MYRTLE_4BR = 1635558580321236737     # 4BR/5bd/3.5BA sleeps 10, no reviews, no price


class ParseSeedTests(unittest.TestCase):
    def test_bare_listing_id(self):
        seed = src.parse_seed("885003882744240148")
        self.assertEqual(seed["listing_id"], 885003882744240148)
        self.assertEqual(seed["listing_url"],
                         "https://www.airbnb.com/rooms/885003882744240148")
        self.assertEqual(seed["host_name"], "")

    def test_room_url(self):
        seed = src.parse_seed("https://www.airbnb.com/rooms/1334048487609899272")
        self.assertEqual(seed["listing_id"], 1334048487609899272)

    def test_pipe_delimited_carries_contact_hints(self):
        seed = src.parse_seed(
            "https://www.airbnb.com/rooms/885003882744240148|Dana McKay|"
            "1201 N Ocean Blvd, Myrtle Beach, SC 29577|Myrtle Beach, SC")
        self.assertEqual(seed["host_name"], "Dana McKay")
        self.assertIn("Ocean Blvd", seed["address"])
        self.assertEqual(seed["market"], "Myrtle Beach, SC")

    def test_comments_and_junk_are_skipped(self):
        for bad in ("", "   ", "# a comment", "not-a-listing", "https://airbnb.com/s/xyz"):
            self.assertIsNone(src.parse_seed(bad))

    def test_parse_seeds_dedupes_by_listing_id(self):
        seeds, skipped = src.parse_seeds([
            str(MYRTLE_3BR),
            f"https://www.airbnb.com/rooms/{MYRTLE_3BR}",
            str(MYRTLE_3BR_B),
            "junk",
        ])
        self.assertEqual(len(seeds), 2)
        self.assertEqual(skipped, 1)

    def test_blank_input_is_safe(self):
        seeds, skipped = src.parse_seeds(None)
        self.assertEqual((seeds, skipped), ([], 0))


class DateWindowTests(unittest.TestCase):
    def test_checkout_is_nights_after_checkin(self):
        self.assertEqual(src._checkout("2026-11-10", 3), "2026-11-13")

    def test_default_checkin_is_deterministic(self):
        self.assertEqual(src._next_checkin(), src._next_checkin())

    def test_checkin_is_in_the_future(self):
        from datetime import date
        self.assertGreater(src._next_checkin(), date.today().isoformat())


class ScoreTests(unittest.TestCase):
    def test_three_bedroom_qualifies(self):
        result = src.score_for_cleaning({
            "bedrooms": 3, "beds": 5, "baths": 2.0, "sleeps": 7,
            "nightly_rate": 284.0, "review_count": 38})
        self.assertTrue(result["qualified"])
        self.assertGreater(result["score"], 40)
        self.assertIn("3 bedrooms", result["reasons"])

    def test_unknown_bedrooms_is_not_qualified(self):
        result = src.score_for_cleaning({"nightly_rate": 300})
        self.assertFalse(result["qualified"])
        self.assertEqual(result["score"], 0)
        self.assertIn("bedrooms unknown - cannot size the clean", result["reasons"])

    def test_one_and_five_bedroom_are_out_of_band(self):
        for br in (1, 5):
            result = src.score_for_cleaning({"bedrooms": br, "sleeps": 6})
            self.assertFalse(result["qualified"], f"{br}BR should be out of band")
            self.assertIn("outside the 2-4BR target band", result["reasons"])

    def test_more_bedrooms_scores_higher(self):
        two = src.score_for_cleaning({"bedrooms": 2, "beds": 3, "baths": 1, "sleeps": 4,
                                      "nightly_rate": 120, "review_count": 50})["score"]
        four = src.score_for_cleaning({"bedrooms": 4, "beds": 6, "baths": 3, "sleeps": 10,
                                       "nightly_rate": 320, "review_count": 50})["score"]
        self.assertGreater(four, two)

    def test_new_listing_is_bonused_not_penalized(self):
        established = src.score_for_cleaning({
            "bedrooms": 3, "sleeps": 7, "nightly_rate": 284.0, "review_count": 38})
        brand_new = src.score_for_cleaning({
            "bedrooms": 3, "sleeps": 7, "nightly_rate": 284.0, "review_count": 0})
        self.assertGreater(brand_new["score"], established["score"])
        self.assertIn("new listing, no incumbent cleaner", brand_new["reasons"])

    def test_contact_hints_raise_the_score(self):
        base = src.score_for_cleaning({"bedrooms": 3, "sleeps": 7, "nightly_rate": 284.0,
                                       "review_count": 38})["score"]
        with_contact = src.score_for_cleaning(
            {"bedrooms": 3, "sleeps": 7, "nightly_rate": 284.0, "review_count": 38},
            seed={"host_name": "Dana", "address": "1201 N Ocean Blvd"})["score"]
        self.assertEqual(with_contact, base + 10)

    def test_score_is_capped_at_100(self):
        result = src.score_for_cleaning({
            "bedrooms": 5, "beds": 12, "baths": 4, "sleeps": 20,
            "nightly_rate": 900.0, "review_count": 0},
            seed={"host_name": "X", "address": "1 Main St"})
        self.assertLessEqual(result["score"], 100)

    def test_missing_price_is_called_out(self):
        result = src.score_for_cleaning({"bedrooms": 3, "sleeps": 7})
        self.assertIn("no dated price", result["reasons"])


class EnrichSeedTests(unittest.TestCase):
    def test_merges_seed_hints_over_api_nulls(self):
        """StayAPI returns host_name null; the seed list is the only source for it."""
        seed = {"listing_id": MYRTLE_3BR, "listing_url": "u",
                "host_name": "Dana McKay", "address": "1201 N Ocean Blvd",
                "market": "Myrtle Beach, SC"}
        with mock.patch.object(src.stay, "listing_profile", return_value={
                "ok": True, "bedrooms": 3, "baths": 2.0, "sleeps": 7,
                "nightly_rate": 284.0, "review_count": 38, "title": "Beachy Bungalow",
                "host_name": None}):
            row = src.enrich_seed(seed)
        self.assertTrue(row["ok"])
        self.assertEqual(row["host_name"], "Dana McKay")
        self.assertIn("Ocean Blvd", row["seed_address"])
        self.assertEqual(row["market"], "Myrtle Beach, SC")

    def test_lookup_failure_is_reported_not_raised(self):
        seed = {"listing_id": 1, "listing_url": "u", "host_name": "", "address": "", "market": ""}
        with mock.patch.object(src.stay, "listing_profile",
                               return_value={"ok": False, "error": "boom"}):
            row = src.enrich_seed(seed)
        self.assertFalse(row["ok"])
        self.assertEqual(row["error"], "boom")
        self.assertEqual(row["score"], 0)

    def test_deterministic_dates_are_passed_through(self):
        seen = {}

        def fake_profile(listing_id, check_in=None, check_out=None, adults=2):
            seen.update(check_in=check_in, check_out=check_out)
            return {"ok": True, "bedrooms": 2, "sleeps": 4}

        seed = {"listing_id": 1, "listing_url": "u", "host_name": "", "address": "", "market": ""}
        with mock.patch.object(src.stay, "listing_profile", fake_profile):
            src.enrich_seed(seed, check_in="2026-11-10", nights=5)
        self.assertEqual(seen["check_in"], "2026-11-10")
        self.assertEqual(seen["check_out"], "2026-11-15")


class QuoteTests(unittest.TestCase):
    def test_quote_uses_the_row_numbers(self):
        row = {"bedrooms": 3, "beds": 5, "baths": 2.0, "sleeps": 7,
               "nightly_rate": 284.0, "pets_allowed": False}
        quote = src.cleaning_quote_for(row)
        self.assertEqual(quote["mid"], 85.0)
        self.assertIn("3 bedroom", quote["pitch"])
        self.assertIn("$284 night", quote["pitch"])

    def test_quote_works_without_a_price(self):
        quote = src.cleaning_quote_for({"bedrooms": 4, "beds": 6, "baths": 3, "sleeps": 10})
        self.assertGreater(quote["low"], 0)
        self.assertNotIn("percent", quote["pitch"])


class ProcessSeedsTests(unittest.TestCase):
    def _row(self, listing_id, **kw):
        base = {"ok": True, "listing_id": listing_id, "bedrooms": 3, "baths": 2.0,
                "sleeps": 7, "nightly_rate": 284.0, "review_count": 38, "score": 60,
                "qualified": True, "reasons": ["3 bedrooms"], "title": "Beachy"}
        base.update(kw)
        return base

    def test_dry_run_by_default_writes_nothing(self):
        with mock.patch.object(src, "enrich_seed", return_value=self._row(MYRTLE_3BR)), \
             mock.patch.object(src, "commit_rows") as commit:
            report = src.process_seeds([str(MYRTLE_3BR)])
        commit.assert_not_called()
        self.assertNotIn("committed", report)
        self.assertEqual(report["enriched"], 1)
        self.assertEqual(report["qualified"], 1)

    def test_commit_inserts(self):
        with mock.patch.object(src, "enrich_seed", return_value=self._row(MYRTLE_3BR)), \
             mock.patch.object(src, "commit_rows",
                               return_value={"ok": True, "created": 1}) as commit:
            report = src.process_seeds([str(MYRTLE_3BR)], commit=True, db_url="postgres://x")
        commit.assert_called_once()
        self.assertEqual(report["committed"]["created"], 1)

    def test_min_score_filters(self):
        low = self._row(MYRTLE_3BR, score=10)
        with mock.patch.object(src, "enrich_seed", return_value=low):
            report = src.process_seeds([str(MYRTLE_3BR)], min_score=50)
        self.assertEqual(report["enriched"], 0)

    def test_failures_are_counted_separately(self):
        with mock.patch.object(src, "enrich_seed",
                               return_value={"ok": False, "error": "nope", "score": 0,
                                             "qualified": False, "reasons": []}):
            report = src.process_seeds([str(MYRTLE_3BR)])
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["enriched"], 0)

    def test_sorted_by_score(self):
        rows = {MYRTLE_3BR: self._row(MYRTLE_3BR, score=40),
                MYRTLE_4BR: self._row(MYRTLE_4BR, score=90)}
        with mock.patch.object(src, "enrich_seed",
                               side_effect=lambda s, **k: rows[s["listing_id"]]):
            report = src.process_seeds([str(MYRTLE_3BR), str(MYRTLE_4BR)])
        self.assertEqual([r["score"] for r in report["rows"]], [90, 40])

    def test_pd_ready_counts_contact_hints(self):
        with mock.patch.object(src, "enrich_seed", return_value=self._row(MYRTLE_3BR,
                                                                        host_name="Dana")):
            report = src.process_seeds([f"{MYRTLE_3BR}|Dana"])
        self.assertEqual(report["pd_ready"], 1)


class CommitRowsTests(unittest.TestCase):
    def test_refuses_without_database_url(self):
        result = src.commit_rows([{"listing_id": 1, "qualified": True}], db_url="")
        self.assertFalse(result["ok"])
        self.assertIn("DATABASE_URL", result["error"])

    def test_db_failure_is_returned_not_raised(self):
        with mock.patch.dict("sys.modules", {"psycopg": None}):
            result = src.commit_rows([{"listing_id": 1, "qualified": True}],
                                     db_url="postgres://x")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
