import unittest
from unittest import mock

import stayapi_service as stay


class ParseRatingTests(unittest.TestCase):
    def test_parses_score_and_count(self):
        self.assertEqual(stay.parse_rating("4.87 (38)"), (4.87, 38))

    def test_parses_thousands_separator(self):
        self.assertEqual(stay.parse_rating("4.62 (1,204)"), (4.62, 1204))

    def test_score_without_count(self):
        self.assertEqual(stay.parse_rating("4.9"), (4.9, None))

    def test_missing_or_junk_is_none(self):
        self.assertEqual(stay.parse_rating(None), (None, None))
        self.assertEqual(stay.parse_rating("Guest favorite"), (None, None))


class ExtractListingIdTests(unittest.TestCase):
    def test_extracts_from_url(self):
        self.assertEqual(
            stay.extract_listing_id("https://www.airbnb.com/rooms/885003882744240148"),
            885003882744240148,
        )

    def test_extracts_from_regional_domain(self):
        self.assertEqual(
            stay.extract_listing_id("https://www.airbnb.ie/rooms/754999772573993689"),
            754999772573993689,
        )

    def test_passes_through_bare_id(self):
        self.assertEqual(stay.extract_listing_id(12345), 12345)

    def test_non_url_returns_none(self):
        self.assertIsNone(stay.extract_listing_id("Myrtle Beach"))
        self.assertIsNone(stay.extract_listing_id(""))


class CleaningQuoteTests(unittest.TestCase):
    def test_three_bedroom_base(self):
        quote = stay.cleaning_quote({"bedrooms": 3, "beds": 5, "baths": 2, "sleeps": 7})
        self.assertTrue(quote["ok"])
        self.assertEqual(quote["bedrooms"], 3)
        self.assertEqual(quote["mid"], 85.0)
        self.assertLess(quote["low"], quote["high"])

    def test_band_brackets_the_midpoint(self):
        quote = stay.cleaning_quote({"bedrooms": 2})
        self.assertLess(quote["low"], quote["mid"])
        self.assertLess(quote["mid"], quote["high"])

    def test_unknown_size_does_not_return_zero(self):
        quote = stay.cleaning_quote({})
        self.assertGreater(quote["mid"], 0)
        self.assertIsNone(quote["bedrooms"])

    def test_extra_bedrooms_raise_the_price(self):
        base = stay.cleaning_quote({"bedrooms": 2, "baths": 1})
        bigger = stay.cleaning_quote({"bedrooms": 2, "baths": 1}, extra_bedrooms=3)
        self.assertGreater(bigger["mid"], base["mid"])

    def test_many_baths_and_beds_add_surcharges(self):
        plain = stay.cleaning_quote({"bedrooms": 3, "baths": 1, "beds": 4})
        loaded = stay.cleaning_quote({"bedrooms": 3, "baths": 4, "beds": 8})
        self.assertGreater(loaded["mid"], plain["mid"])
        self.assertIn("4 baths", loaded["reasons"])
        self.assertIn("8 beds", loaded["reasons"])

    def test_pets_add_a_surcharge(self):
        plain = stay.cleaning_quote({"bedrooms": 3, "baths": 2, "beds": 5})
        pets = stay.cleaning_quote({"bedrooms": 3, "baths": 2, "beds": 5, "pets_allowed": True})
        self.assertGreater(pets["mid"], plain["mid"])
        self.assertIn("pets allowed", pets["reasons"])

    def test_nightly_rate_gives_share_of_revenue(self):
        quote = stay.cleaning_quote({"bedrooms": 3, "baths": 2, "beds": 5, "sleeps": 7,
                                     "nightly_rate": 284.0})
        self.assertEqual(quote["nightly_rate"], 284.0)
        self.assertGreater(quote["pct_of_nightly_low"], 0)
        self.assertLess(quote["pct_of_nightly_high"], 100)

    def test_missing_nightly_rate_omits_share(self):
        quote = stay.cleaning_quote({"bedrooms": 3})
        self.assertNotIn("pct_of_nightly_low", quote)


class QuoteTextTests(unittest.TestCase):
    def test_text_states_size_and_band(self):
        profile = {"bedrooms": 3, "sleeps": 7, "baths": 2, "beds": 5, "nightly_rate": 284.0}
        text = stay.cleaning_quote_text(profile, stay.cleaning_quote(profile))
        self.assertIn("3 bedroom", text)
        self.assertIn("7 sleeper", text)
        self.assertIn("$", text)
        self.assertIn("percent of one $284 night", text)
        self.assertNotIn("a that size", text)
        self.assertTrue(text.endswith("."))

    def test_text_works_without_pricing(self):
        profile = {"bedrooms": 2}
        text = stay.cleaning_quote_text(profile, stay.cleaning_quote(profile))
        self.assertIn("2 bedroom", text)
        self.assertNotIn("percent", text)

    def test_text_never_breaks_on_empty_profile(self):
        profile = {}
        text = stay.cleaning_quote_text(profile, stay.cleaning_quote(profile))
        self.assertTrue(text.endswith("."))
        self.assertIn("$", text)
        self.assertNotIn("a that size", text)


class ListingProfileTests(unittest.TestCase):
    def test_requires_dates(self):
        result = stay.listing_profile("885003882744240148")
        self.assertFalse(result["ok"])
        self.assertIn("check_in", result["error"])

    def test_requires_a_listing(self):
        result = stay.listing_profile("not-a-url", check_in="2026-11-10", check_out="2026-11-13")
        self.assertFalse(result["ok"])

    def test_accepts_a_room_url(self):
        captured = {}

        def fake_overview(listing_id):
            captured["id"] = listing_id
            return None

        with mock.patch.object(stay, "fetch_overview", fake_overview), \
             mock.patch.object(stay, "fetch_details", return_value=None), \
             mock.patch.object(stay, "fetch_pricing", return_value=None):
            profile = stay.listing_profile(
                "https://www.airbnb.com/rooms/885003882744240148",
                check_in="2026-11-10", check_out="2026-11-13",
            )
        self.assertTrue(profile["ok"])
        self.assertEqual(captured["id"], 885003882744240148)
        self.assertIn("885003882744240148", profile["url"])

    def test_merges_the_three_sources(self):
        overview = {"bedrooms": 3, "beds": 5, "baths": 2.0, "guests": 7, "raw_items": []}
        details = {"title": "Beachy Bungalow", "rating": "4.87 (38)",
                   "badge": "Guest favorite", "max_guests": 7, "pets_allowed": True}
        pricing = {"nightly_rate": 284.0, "currency": "USD", "nights": 3}

        with mock.patch.object(stay, "fetch_overview", return_value=overview), \
             mock.patch.object(stay, "fetch_details", return_value=details), \
             mock.patch.object(stay, "fetch_pricing", return_value=pricing):
            profile = stay.listing_profile(
                "885003882744240148", check_in="2026-11-10", check_out="2026-11-13")

        self.assertEqual(profile["bedrooms"], 3)
        self.assertEqual(profile["rating"], 4.87)
        self.assertEqual(profile["review_count"], 38)
        self.assertEqual(profile["nightly_rate"], 284.0)
        self.assertEqual(profile["sleeps"], 7)
        self.assertTrue(profile["available"])

    def test_bedrooms_fall_back_to_raw_items(self):
        overview = {"guests": 10, "raw_items": ["10 guests", "3 bedrooms", "6 beds"]}
        with mock.patch.object(stay, "fetch_overview", return_value=overview), \
             mock.patch.object(stay, "fetch_details", return_value=None), \
             mock.patch.object(stay, "fetch_pricing", return_value=None):
            profile = stay.listing_profile(
                "1592085093831227473", check_in="2026-11-10", check_out="2026-11-13")
        self.assertEqual(profile["bedrooms"], 3)
        self.assertEqual(profile["sleeps"], 10)

    def test_missing_host_name_is_not_invented(self):
        details = {"title": "Rental", "rating": "4.55 (11)", "host_name": None,
                   "max_guests": 8}
        with mock.patch.object(stay, "fetch_overview", return_value=None), \
             mock.patch.object(stay, "fetch_details", return_value=details), \
             mock.patch.object(stay, "fetch_pricing", return_value=None):
            profile = stay.listing_profile(
                "1334048487609899272", check_in="2026-11-10", check_out="2026-11-13")
        self.assertEqual(profile["review_count"], 11)
        self.assertIsNone(profile.get("host_name"))


class DiscoverySeamTests(unittest.TestCase):
    def test_discovery_is_explicitly_unimplemented(self):
        with self.assertRaises(NotImplementedError):
            stay.discover_listing_ids("Myrtle Beach, SC")


if __name__ == "__main__":
    unittest.main()
