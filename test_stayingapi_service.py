import unittest
from unittest import mock

import stayingapi_service as sa

# Fixtures copied from the documented StayingAPI response shapes
# (stayingapi.com/docs/endpoints/search, Property schema), including the null cases the
# OpenAPI spec explicitly allows.
AIRBNB_PROP = {
    "id": "stays_airbnb_885003882744240148",
    "platform": "airbnb",
    "platformListingId": "885003882744240148",
    "url": "https://www.airbnb.com/rooms/885003882744240148",
    "name": "Bowers Beachy Bungalow",
    "propertyType": "house",
    "location": {"lat": 33.79, "lng": -78.88, "city": "North Myrtle Beach",
                 "region": "South Carolina", "country": "US", "address": None},
    "starRating": None,
    "guestRating": 4.87,
    "ratingScale": 5,
    "reviewCount": 38,
    "maxOccupancy": 7,
    "bedrooms": 3,
    "bathrooms": 2.0,
    "amenities": ["kitchen", "wifi", "pool"],
    "host": {"name": "Dana", "isSuperhost": False},
    "price": {"currency": "USD", "nightlyPrice": 284.0, "totalPrice": 954.24, "nights": 3},
}

NO_HOST_PROP = {
    "id": "stays_airbnb_1635558580321236737",
    "platform": "airbnb",
    "platformListingId": "1635558580321236737",
    "url": "https://www.airbnb.com/rooms/1635558580321236737",
    "name": "New 4 bedroom",
    "propertyType": "house",
    "location": {"lat": None, "lng": None, "city": "Myrtle Beach", "region": None,
                 "country": "US", "address": None},
    "starRating": None,
    "guestRating": None,
    "ratingScale": 5,
    "reviewCount": 0,
    "maxOccupancy": 10,
    "bedrooms": 4,
    "bathrooms": 3.5,
    "amenities": None,
    "host": None,
    "price": {"currency": "USD", "nightlyPrice": None, "totalPrice": None, "nights": 3},
}

BOOKING_PROP = {
    "id": "stays_booking_abramovic2",
    "platform": "booking",
    "platformListingId": "abramovic2",
    "url": "https://www.booking.com/hotel/hr/abramovic2.html",
    "name": "Apartments Abramovic",
    "propertyType": "apartment",
    "location": {"lat": 43.51, "lng": 16.44, "city": "Split", "region": None,
                 "country": "HR", "address": "Ulica Obala 12"},
    "starRating": None,
    "guestRating": 9.1,
    "ratingScale": 10,
    "reviewCount": 142,
    "maxOccupancy": 4,
    "bedrooms": 2,
    "bathrooms": 1,
    "amenities": ["pool"],
    "host": {"name": "Marko", "isSuperhost": False},
    "price": {"currency": "USD", "nightlyPrice": 303, "totalPrice": 2122, "nights": 7},
}


def _page(props, next_cursor=None, has_more=False, credits=0, partial=False):
    return (True, {
        "data": props,
        "meta": {
            "requestId": "req_test", "creditsCharged": credits, "partial": partial,
            "pagination": {"limit": 20, "cursor": None, "nextCursor": next_cursor,
                           "hasMore": has_more},
            "platformResults": [{"platform": "airbnb", "status": "ok", "count": len(props)}],
            "warnings": [],
        },
    })


class NormalizeTests(unittest.TestCase):
    def test_airbnb_row_normalizes(self):
        row = sa.normalize(AIRBNB_PROP)
        self.assertEqual(row["platform"], "airbnb")
        self.assertEqual(row["platform_listing_id"], "885003882744240148")
        self.assertEqual(row["bedrooms"], 3)
        self.assertEqual(row["sleeps"], 7)
        self.assertEqual(row["nightly_rate"], 284.0)
        self.assertEqual(row["host_name"], "Dana")
        self.assertEqual(row["city"], "North Myrtle Beach")
        self.assertIsNone(row["address"])

    def test_null_host_becomes_none_not_an_error(self):
        row = sa.normalize(NO_HOST_PROP)
        self.assertIsNone(row["host_name"])
        self.assertIsNone(row["rating"])
        self.assertIsNone(row["nightly_rate"])
        self.assertEqual(row["amenities"], [])
        self.assertEqual(row["bedrooms"], 4)

    def test_ten_point_scale_is_normalized_to_five(self):
        row = sa.normalize(BOOKING_PROP)
        self.assertEqual(row["rating"], 9.1)
        self.assertEqual(row["rating_scale"], 10)
        self.assertEqual(row["rating_normalized"], 4.55)

    def test_five_point_scale_is_unchanged(self):
        row = sa.normalize(AIRBNB_PROP)
        self.assertEqual(row["rating_normalized"], 4.87)

    def test_address_is_read_when_present(self):
        self.assertEqual(sa.normalize(BOOKING_PROP)["address"], "Ulica Obala 12")

    def test_empty_object_does_not_crash(self):
        row = sa.normalize({})
        self.assertIsNone(row["host_name"])
        self.assertIsNone(row["nightly_rate"])
        self.assertEqual(row["amenities"], [])


class FillRateTests(unittest.TestCase):
    def test_measures_contact_seed_coverage(self):
        stats = sa.fill_rate([AIRBNB_PROP, NO_HOST_PROP, BOOKING_PROP])
        self.assertEqual(stats["n"], 3)
        # Dana + Marko named, one has no host at all
        self.assertEqual(stats["host_name"], 66.7)
        # Only Booking carries an address; NO_HOST_PROP carries neither
        self.assertEqual(stats["address"], 33.3)
        self.assertEqual(stats["either"], 66.7)
        self.assertEqual(stats["neither"], 33.3)

    def test_reports_the_unsafe_case(self):
        stats = sa.fill_rate([NO_HOST_PROP])
        self.assertEqual(stats["either"], 0.0)
        self.assertEqual(stats["neither"], 100.0)

    def test_empty_input_does_not_divide_by_zero(self):
        stats = sa.fill_rate([])
        self.assertEqual(stats["n"], 0)
        self.assertEqual(stats["either"], 0.0)


class QualifyTests(unittest.TestCase):
    def test_keeps_a_good_whole_home(self):
        result = sa.qualify([AIRBNB_PROP] and [sa.normalize(AIRBNB_PROP)])
        self.assertEqual(result["qualified_count"], 1)
        self.assertEqual(result["qualified"][0]["bedrooms"], 3)

    def test_rejects_out_of_band_bedrooms(self):
        rows = [dict(sa.normalize(BOOKING_PROP), bedrooms=6)]
        result = sa.qualify(rows, min_bedrooms=2, max_bedrooms=4)
        self.assertEqual(result["qualified_count"], 0)
        self.assertIn("outside", result["rejected"][0]["reject_reasons"][0])

    def test_rejects_apartments_outside_bedroom_floor(self):
        result = sa.qualify([sa.normalize(BOOKING_PROP)], min_bedrooms=3)
        self.assertEqual(result["qualified_count"], 0)

    def test_zero_review_new_listing_still_qualifies(self):
        """A new high-turnover 4BR is a better cleaning prospect than a saturated one."""
        row = sa.normalize(NO_HOST_PROP)
        self.assertEqual(row["review_count"], 0)
        result = sa.qualify([row], min_reviews=None)
        self.assertEqual(result["qualified_count"], 1)
        self.assertGreaterEqual(result["qualified"][0]["score"], 4)

    def test_bigger_and_sleeper_scores_higher(self):
        result = sa.qualify([sa.normalize(AIRBNB_PROP), sa.normalize(NO_HOST_PROP)])
        scores = {r["platform_listing_id"]: r["score"] for r in result["qualified"]}
        self.assertGreater(scores["1635558580321236737"], scores["885003882744240148"])

    def test_nightly_ceilings_are_enforced(self):
        result = sa.qualify([sa.normalize(AIRBNB_PROP)], max_nightly=200)
        self.assertEqual(result["qualified_count"], 0)

    def test_nightly_floor_is_enforced(self):
        result = sa.qualify([sa.normalize(AIRBNB_PROP)], min_nightly=500)
        self.assertEqual(result["qualified_count"], 0)

    def test_contact_seed_can_be_required(self):
        rows = [sa.normalize(AIRBNB_PROP), sa.normalize(NO_HOST_PROP)]
        result = sa.qualify(rows, require_contact_seed=True)
        self.assertEqual(result["qualified_count"], 1)
        self.assertEqual(result["qualified"][0]["platform_listing_id"], "885003882744240148")
        self.assertIn("no host name or address",
                      result["rejected"][0]["reject_reasons"])

    def test_unknown_bedrooms_is_rejected_not_guessed(self):
        row = dict(sa.normalize(AIRBNB_PROP), bedrooms=None)
        result = sa.qualify([row])
        self.assertEqual(result["qualified_count"], 0)
        self.assertIn("bedrooms unknown", result["rejected"][0]["reject_reasons"])

    def test_sorted_by_score_descending(self):
        rows = [sa.normalize(AIRBNB_PROP), sa.normalize(NO_HOST_PROP),
                sa.normalize(BOOKING_PROP)]
        scores = [r["score"] for r in sa.qualify(rows)["qualified"]]
        self.assertEqual(scores, sorted(scores, reverse=True))


class DedupeTests(unittest.TestCase):
    def test_same_listing_twice_collapses(self):
        rows = [sa.normalize(AIRBNB_PROP), sa.normalize(AIRBNB_PROP)]
        unique, dupes = sa.dedupe(rows)
        self.assertEqual(len(unique), 1)
        self.assertEqual(dupes, 1)

    def test_cross_platform_ids_do_not_collide(self):
        rows = [sa.normalize(AIRBNB_PROP), sa.normalize(BOOKING_PROP)]
        unique, dupes = sa.dedupe(rows)
        self.assertEqual(len(unique), 2)
        self.assertEqual(dupes, 0)

    def test_seen_keys_from_a_previous_run_are_honored(self):
        rows = [sa.normalize(AIRBNB_PROP), sa.normalize(BOOKING_PROP)]
        unique, dupes = sa.dedupe(rows, seen_keys={"airbnb:885003882744240148"})
        self.assertEqual(len(unique), 1)
        self.assertEqual(unique[0]["platform"], "booking")

    def test_key_prefers_platform_id_over_url(self):
        self.assertEqual(sa.dedupe_key(sa.normalize(AIRBNB_PROP)),
                         "airbnb:885003882744240148")

    def test_key_falls_back_to_url_without_query(self):
        self.assertEqual(sa.dedupe_key({"platform_listing_id": None, "platform": None,
                                        "url": "https://x.com/rooms/1?foo=bar"}),
                         "https://x.com/rooms/1")

    def test_unidentifiable_row_still_survives_dedupe(self):
        unique, dupes = sa.dedupe([{"url": None, "platform_listing_id": None}])
        self.assertEqual(len(unique), 1)
        self.assertEqual(dupes, 0)


class SearchParamTests(unittest.TestCase):
    def test_location_is_required(self):
        ok, payload = sa.search("")
        self.assertFalse(ok)
        self.assertEqual(payload["error"]["code"], "missing_parameter")

    def test_defaults_target_airbnb_only(self):
        params = sa._search_params("Myrtle Beach, SC")
        self.assertEqual(params["platforms"], ["airbnb"])
        self.assertEqual(params["limit"], 20)
        self.assertEqual(params["currency"], "USD")

    def test_list_params_become_lists(self):
        params = sa._search_params("Split, HR", platforms=("airbnb", "booking"),
                                   property_type=("house", "villa"))
        self.assertEqual(params["platforms"], ["airbnb", "booking"])
        self.assertEqual(params["propertyType"], ["house", "villa"])

    def test_zero_children_is_omitted(self):
        self.assertIsNone(sa._search_params("X", children=0)["children"])


class SearchTests(unittest.TestCase):
    def test_successful_page(self):
        with mock.patch.object(sa, "_request",
                               return_value=(200, _page([AIRBNB_PROP], credits=2)[1], {})):
            ok, payload = sa.search("Myrtle Beach, SC")
        self.assertTrue(ok)
        self.assertEqual(len(payload["data"]), 1)

    def test_error_is_returned_not_raised(self):
        err = (401, {"error": {"type": "authentication_error",
                               "code": "invalid_api_key", "message": "bad key",
                               "retryable": False}}, {})
        with mock.patch.object(sa, "_request", return_value=err):
            ok, payload = sa.search("Myrtle Beach, SC")
        self.assertFalse(ok)
        self.assertEqual(payload["error"]["code"], "invalid_api_key")

    def test_unverified_email_surfaces_clearly(self):
        err = (403, {"error": {"code": "email_unverified",
                               "message": "verify your email"}}, {})
        with mock.patch.object(sa, "_request", return_value=err):
            ok, payload = sa.search("Myrtle Beach, SC")
        self.assertFalse(ok)
        self.assertEqual(payload["error"]["code"], "email_unverified")


class PaginationTests(unittest.TestCase):
    def test_follows_cursor_until_exhausted(self):
        pages = [
            (200, _page([AIRBNB_PROP], next_cursor="c1", has_more=True)[1], {}),
            (200, _page([BOOKING_PROP], has_more=False)[1], {}),
        ]
        with mock.patch.object(sa, "_request", side_effect=pages):
            ok, payload = sa.search_all_pages("Myrtle Beach, SC")
        self.assertTrue(ok)
        self.assertEqual(len(payload["data"]), 2)

    def test_respects_max_results(self):
        pages = [(200, _page([AIRBNB_PROP], next_cursor="c1", has_more=True)[1], {})]
        with mock.patch.object(sa, "_request", side_effect=pages * 5):
            ok, payload = sa.search_all_pages("Myrtle Beach, SC", max_results=2)
        self.assertTrue(ok)
        self.assertEqual(len(payload["data"]), 2)

    def test_mid_pagination_failure_keeps_what_we_have(self):
        pages = [
            (200, _page([AIRBNB_PROP], next_cursor="c1", has_more=True)[1], {}),
            (500, {"error": {"code": "internal_error", "message": "boom"}}, {}),
        ]
        with mock.patch.object(sa, "_request", side_effect=pages):
            ok, payload = sa.search_all_pages("Myrtle Beach, SC")
        self.assertFalse(ok)
        self.assertTrue(payload["partial"])
        self.assertEqual(len(payload["data"]), 1)


class AsyncJobTests(unittest.TestCase):
    def _job(self, state, result=None, error=None):
        return (200, {"data": {"jobId": "job_1", "status": state,
                               **({"result": result} if result else {}),
                               **({"error": error} if error else {})},
                     "meta": {"creditsCharged": 0}}, {})

    def test_202_is_polled_to_completion(self):
        accepted = (202, {"data": {"jobId": "job_1", "status": "pending",
                                   "pollUrl": "/v1/jobs/job_1",
                                   "estimatedSeconds": 30},
                          "meta": {"requestId": "r", "creditsCharged": 0}}, {})
        done = self._job("completed", result=[AIRBNB_PROP])
        slept = []
        with mock.patch.object(sa, "_request", side_effect=[accepted, done]), \
             mock.patch.object(sa.time, "sleep", slept.append):
            ok, payload = sa.search("Myrtle Beach, SC")
        self.assertTrue(ok)
        self.assertEqual(len(payload["data"]), 1)
        self.assertTrue(slept)

    def test_reconstructed_meta_survives_polling(self):
        accepted = (202, {"data": {"jobId": "job_1", "status": "pending"}}, {})
        done = (200, {"data": {"status": "completed", "result": [AIRBNB_PROP]},
                      "creditsCharged": 4, "requestId": "req_x"}, {})
        with mock.patch.object(sa, "_request", side_effect=[accepted, done]), \
             mock.patch.object(sa.time, "sleep", lambda s: None):
            ok, payload = sa.search("Myrtle Beach, SC")
        self.assertTrue(ok)
        self.assertEqual(payload["meta"]["creditsCharged"], 4)

    def test_failed_job_becomes_an_error_not_a_crash(self):
        accepted = (202, {"data": {"jobId": "job_1", "status": "pending"}}, {})
        failed = self._job("failed", error={"code": "actor_blocked",
                                             "message": "airbnb blocked"})
        with mock.patch.object(sa, "_request", side_effect=[accepted, failed]), \
             mock.patch.object(sa.time, "sleep", lambda s: None):
            ok, payload = sa.search("Myrtle Beach, SC")
        self.assertFalse(ok)
        self.assertEqual(payload["error"]["code"], "actor_blocked")

    def test_poll_gives_up_rather_than_looping_forever(self):
        accepted = (202, {"data": {"jobId": "job_1", "status": "pending"}}, {})
        pending = self._job("pending")
        with mock.patch.object(sa, "_request", side_effect=[accepted] + [pending] * 200), \
             mock.patch.object(sa.time, "sleep", lambda s: None), \
             mock.patch.object(sa, "MAX_POLL_SECONDS", 0):
            ok, payload = sa.search("Myrtle Beach, SC")
        self.assertFalse(ok)
        self.assertEqual(payload["error"]["code"], "job_timeout")

    def test_synchronous_response_never_sleeps(self):
        """Guards the import-time default-arg bug that made this untestable."""
        slept = []
        with mock.patch.object(sa, "_request",
                               return_value=(200, _page([AIRBNB_PROP])[1], {})), \
             mock.patch.object(sa.time, "sleep", slept.append):
            sa.search("Myrtle Beach, SC")
        self.assertEqual(slept, [])

    def test_202_without_a_job_id_is_passed_through(self):
        accepted = (202, {"data": {}}, {})
        with mock.patch.object(sa, "_request", return_value=accepted):
            ok, payload = sa.search("Myrtle Beach, SC")
        self.assertFalse(ok)


class AccountTests(unittest.TestCase):
    def test_reports_credit_balance(self):
        body = {"data": {"plan": {"code": "free"}, "key": {"env": "sandbox"},
                         "credits": {"balance": 300, "available": 300, "held": 0},
                         "rateLimit": {"requestsPerMinute": 30}}}
        with mock.patch.object(sa, "_request", return_value=(200, body, {})):
            self.assertEqual(sa.credits_remaining(), 300)

    def test_missing_key_returns_none_rather_than_calling(self):
        with mock.patch.object(sa, "is_configured", return_value=False):
            self.assertIsNone(sa.account())
            self.assertIsNone(sa.credits_remaining())


class DiscoverTests(unittest.TestCase):
    def test_end_to_end_reports_fill_rate_and_credits(self):
        payload = _page([AIRBNB_PROP, NO_HOST_PROP], credits=4)[1]
        with mock.patch.object(sa, "search_all_pages", return_value=(True, payload)):
            result = sa.discover("Myrtle Beach, SC", check_in="2026-11-10",
                                 check_out="2026-11-13", platforms=("airbnb",))
        self.assertTrue(result["ok"])
        self.assertEqual(result["raw_count"], 2)
        self.assertEqual(result["fill_rate"]["host_name"], 50.0)
        self.assertEqual(result["credits_charged"], 4)
        # 3BR and 4BR are both inside the 2-4 band, so both qualify.
        self.assertEqual(result["qualified_count"], 2)
        self.assertEqual(result["fill_rate"]["neither"], 50.0)

    def test_dedupes_across_pages(self):
        payload = _page([AIRBNB_PROP, AIRBNB_PROP], credits=4)[1]
        with mock.patch.object(sa, "search_all_pages", return_value=(True, payload)):
            result = sa.discover("Myrtle Beach, SC")
        self.assertEqual(result["raw_count"], 2)
        self.assertEqual(result["deduped_count"], 1)
        self.assertEqual(result["duplicate_count"], 1)

    def test_search_failure_propagates(self):
        with mock.patch.object(sa, "search_all_pages",
                               return_value=(False, {"error": {"code": "x"},
                                                     "data": [], "meta": {}})):
            result = sa.discover("Myrtle Beach, SC")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "x")

    def test_never_returns_an_email_field(self):
        """leads.email is NOT NULL — nothing here may look contactable by accident."""
        payload = _page([AIRBNB_PROP], credits=2)[1]
        with mock.patch.object(sa, "search_all_pages", return_value=(True, payload)):
            result = sa.discover("Myrtle Beach, SC")
        for row in result["qualified"]:
            self.assertNotIn("email", row)
            self.assertTrue(row["contactable"] is False or row["host_name"])


if __name__ == "__main__":
    unittest.main()
