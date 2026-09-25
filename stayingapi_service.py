"""StayingAPI client — cross-OTA STR discovery.

⚠️ Do not confuse with StayAPI (`stayapi.com`, `x-api-key`, per-listing only) which lives in
`stayapi_service.py`. StayingAPI is a different company: `Bearer stay_live_…` / `stay_test_…`,
base `https://api.stayingapi.com/v1`, unified cross-OTA schema.

Why this module exists: StayAPI has no Airbnb search endpoint and returns `host_name: null` on
real listings, so it can never produce a contactable lead. StayingAPI has `GET /v1/search` with
location + filters, which is the missing discovery half.

WHAT IT STILL CANNOT DO (from the OpenAPI spec, 2026-09-25 — do not design around it):
`host` and `host.name` are both nullable, `location.address` is nullable, and there is no email or
phone field anywhere in the schema. So search results are *qualified listings*, not *contactable
people*. Inserting one into `leads` (whose `email` is NOT NULL) still requires PDL.

Specs: https://stayingapi.com/docs  ·  openapi.json / the api-evangelist mirror for the contract.
Only runs when STAYINGAPI_KEY is set.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.stayingapi.com/v1"
USER_AGENT = "bizstack-stayingapi/1.0 (+bizstackperks.com)"
DEFAULT_TIMEOUT = 30
MAX_POLL_SECONDS = 300


def is_configured() -> bool:
    return bool((os.getenv("STAYINGAPI_KEY") or "").strip())


def _env_float(key, default):
    try:
        return float(os.getenv(key, ""))
    except (TypeError, ValueError):
        return default


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _request(path, params=None, method="GET", body=None):
    """One HTTP call. Returns (status_code, payload_dict, headers). Never raises."""
    if not is_configured():
        return 0, {"error": {"code": "missing_api_key", "message": "STAYINGAPI_KEY not set"}}, {}
    url = f"{API_BASE}/{path.lstrip('/')}"
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            # Repeat key for list params the spec declares as enum[]/array.
            pairs = []
            for k, v in clean.items():
                if isinstance(v, (list, tuple)):
                    pairs.extend((k, str(i)) for i in v)
                else:
                    pairs.append((k, str(v)))
            url += "?" + urllib.parse.urlencode(pairs)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {os.getenv('STAYINGAPI_KEY', '')}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {}), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"error": {"code": f"http_{e.code}", "message": raw[:200]}}
        return e.code, payload, dict(e.headers)
    except Exception as e:
        return 0, {"error": {"code": "transport_error", "message": str(e)}}, {}


# --- 202 async job handling -------------------------------------------------

def _unwrap(status, payload, headers, sleep=None, clock=None):
    """Collapse the 202+jobId shape into the synchronous {data, meta} envelope.

    Live calls return 202 when the scrape is projected past ~8s. Per the spec, credits are
    charged only on successful completion and polling is free, so a failed job costs nothing.

    `sleep`/`clock` default to None and resolve at call time rather than as default
    arguments — a `sleep=time.sleep` default binds at import and silently ignores both
    patching and injection, which is what made this untestable.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    if status != 202:
        return status, payload
    job_id = (payload.get("data") or {}).get("jobId")
    if not job_id:
        return status, payload

    poll_url = (payload.get("data") or {}).get("pollUrl") or f"/v1/jobs/{job_id}"
    poll_url = poll_url.lstrip("/")
    if not poll_url.startswith("v1/"):
        poll_url = f"v1/{poll_url}"

    started = clock()
    delay = 2.0
    while clock() - started < MAX_POLL_SECONDS:
        sleep(delay)
        p_status, p_payload, p_headers = _request(poll_url)
        if p_status != 200:
            return p_status, p_payload
        job = (p_payload.get("data") or {})
        state = job.get("status")
        if state == "completed":
            # Payload arrives at data.result; reconstructed meta sits at the top level.
            return 200, {
                "data": job.get("result"),
                "meta": {k: v for k, v in p_payload.items() if k != "data"},
            }
        if state in ("failed", "expired", "cancelled"):
            return 502, {"error": job.get("error") or {
                "code": "job_failed", "message": f"job {state}"}}
        retry_after = p_headers.get("Retry-After") or p_headers.get("retry-after")
        if retry_after:
            try:
                delay = max(1.0, float(retry_after))
            except (TypeError, ValueError):
                pass
        else:
            delay = min(delay * 1.5, 15.0)
    return 504, {"error": {"code": "job_timeout",
                           "message": f"job {job_id} exceeded {MAX_POLL_SECONDS}s"}}


def _search_params(location, check_in=None, check_out=None, adults=2, children=0,
                   platforms=("airbnb",), min_bedrooms=None, property_type=None,
                   price_min=None, price_max=None, min_guest_rating=None,
                   limit=20, cursor=None, sort=None, currency="USD"):
    return {
        "location": location,
        "checkIn": check_in,
        "checkOut": check_out,
        "adults": adults,
        "children": children or None,
        "platforms": list(platforms) if platforms else None,
        "minBedrooms": min_bedrooms,
        "propertyType": list(property_type) if property_type else None,
        "priceMin": price_min,
        "priceMax": price_max,
        "minGuestRating": min_guest_rating,
        "limit": limit,
        "cursor": cursor,
        "sort": sort,
        "currency": currency,
    }


def search(location, check_in=None, check_out=None, adults=2, children=0,
           platforms=("airbnb",), min_bedrooms=None, property_type=None,
           price_min=None, price_max=None, min_guest_rating=None,
           limit=20, cursor=None, sort=None, currency="USD"):
    """One discovery page. Returns (ok, payload) where payload is {data, meta} or an error dict.

    `location` is required by the spec. Dates are optional but recommended: without them a
    Property has no embedded price. Dated price is best-effort and may be null even on a
    billed call, so read it defensively.
    """
    if not location:
        return False, {"error": {"code": "missing_parameter", "message": "location required"}}
    status, payload, headers = _request("search", _search_params(
        location, check_in, check_out, adults, children, platforms, min_bedrooms,
        property_type, price_min, price_max, min_guest_rating, limit, cursor, sort, currency))
    status, payload = _unwrap(status, payload, headers)
    if status != 200:
        return False, payload
    return True, payload


def search_all_pages(location, max_results=100, **kwargs):
    """Follow nextCursor up to max_results. Returns (ok, {"data": [...], "meta": {...}}).

    Every page bills separately, so this is capped deliberately.
    """
    collected, meta, cursor = [], {}, None
    while len(collected) < max_results:
        ok, payload = search(location, cursor=cursor, **kwargs)
        if not ok:
            return False, {"error": payload.get("error", payload), "data": collected,
                           "meta": meta, "partial": True}
        page = payload.get("data") or []
        collected.extend(page)
        meta = payload.get("meta") or meta
        pagination = (meta.get("pagination") or {})
        cursor = pagination.get("nextCursor")
        if not pagination.get("hasMore") or not cursor or not page:
            break
    return True, {"data": collected, "meta": meta, "partial": False}


def account():
    """Free, read-only. Gives plan, credit balance and rate limit so you never infer from
    meta.creditsCharged. Works with both sandbox and live keys."""
    status, payload, _ = _request("account")
    if status != 200:
        return None
    return payload.get("data")


def credits_remaining():
    acct = account()
    if not acct:
        return None
    return (acct.get("credits") or {}).get("available")


# --- normalization ----------------------------------------------------------

def normalize(prop):
    """Flatten one Property into the dict shape the rest of the repo uses.

    Matches the keys stayapi_service.listing_profile() returns, so comps and cleaning
    quotes can treat either vendor's output interchangeably. Nullable per the spec:
    guestRating, reviewCount, bedrooms, bathrooms, maxOccupancy, host, price, location.*.
    """
    host = prop.get("host") or {}
    loc = prop.get("location") or {}
    price = prop.get("price") or {}
    guest_rating = _num(prop.get("guestRating"))
    scale = _num(prop.get("ratingScale")) or 10.0
    return {
        "id": prop.get("id"),
        "platform": prop.get("platform"),
        "platform_listing_id": prop.get("platformListingId"),
        "url": prop.get("url"),
        "title": prop.get("name"),
        "property_type": prop.get("propertyType"),
        "city": loc.get("city"),
        "region": loc.get("region"),
        "country": loc.get("country"),
        "address": loc.get("address"),
        "lat": _num(loc.get("lat")),
        "lng": _num(loc.get("lng")),
        "rating": round(guest_rating, 2) if guest_rating is not None else None,
        "rating_scale": scale,
        "rating_normalized": (round(guest_rating / scale * 5, 2)
                              if guest_rating is not None and scale else None),
        "review_count": prop.get("reviewCount"),
        "bedrooms": prop.get("bedrooms"),
        "bathrooms": _num(prop.get("bathrooms")),
        "sleeps": prop.get("maxOccupancy"),
        "amenities": prop.get("amenities") or [],
        # The whole point of the fill-rate test: how often are these actually present?
        "host_name": (host.get("name") or None),
        "is_superhost": host.get("isSuperhost"),
        "nightly_rate": _num(price.get("nightlyPrice")),
        "total_price": _num(price.get("totalPrice")),
        "currency": price.get("currency"),
        "nights": price.get("nights"),
    }


def fill_rate(props):
    """What fraction of results carry a usable contact seed.

    This is the number that decides whether the vendor is worth paying for. Run it on the
    first live page before committing to a plan.
    """
    rows = [normalize(p) for p in (props or [])]
    total = len(rows)
    if not total:
        return {"n": 0, "host_name": 0.0, "address": 0.0, "either": 0.0, "neither": 0.0}
    named = sum(1 for r in rows if r["host_name"])
    addressed = sum(1 for r in rows if r["address"])
    either = sum(1 for r in rows if r["host_name"] or r["address"])
    return {
        "n": total,
        "host_name": round(named / total * 100, 1),
        "address": round(addressed / total * 100, 1),
        "either": round(either / total * 100, 1),
        "neither": round((total - either) / total * 100, 1),
    }


def qualify(rows, min_bedrooms=2, max_bedrooms=4, min_rating=None, min_reviews=None,
            min_nightly=None, max_nightly=None, require_contact_seed=False,
            score_new_high_turnover=True):
    """Score normalized rows for cleaning-service fit.

    Deliberately does NOT treat a low review count as disqualifying. A brand-new 4BR that
    sleeps 10 has high turnover and no incumbent cleaner, which is a better prospect than a
    saturated listing with 300 reviews — verified against the live StayAPI probe, where a
    4BR/10-sleeper had zero reviews.
    """
    kept, rejected = [], []
    for row in rows or []:
        reasons = []
        bedrooms = row.get("bedrooms")
        if bedrooms is not None:
            if bedrooms < min_bedrooms or bedrooms > max_bedrooms:
                reasons.append(f"bedrooms {bedrooms} outside {min_bedrooms}-{max_bedrooms}")
        else:
            reasons.append("bedrooms unknown")

        rating = row.get("rating_normalized")
        if min_rating is not None and (rating is None or rating < min_rating):
            reasons.append("rating below floor")
        reviews = row.get("review_count")
        if min_reviews is not None and (reviews is None or reviews < min_reviews):
            reasons.append("reviews below floor")

        nightly = row.get("nightly_rate")
        if min_nightly is not None and (nightly is None or nightly < min_nightly):
            reasons.append("nightly below floor")
        if max_nightly is not None and (nightly is None or nightly > max_nightly):
            reasons.append("nightly above ceiling")

        contactable = bool(row.get("host_name") or row.get("address"))
        if require_contact_seed and not contactable:
            reasons.append("no host name or address")

        if reasons:
            rejected.append({**row, "reject_reasons": reasons})
            continue

        score = 0
        if bedrooms and bedrooms >= 3:
            score += 2
        if (row.get("sleeps") or 0) >= 8:
            score += 2
        if (row.get("bathrooms") or 0) >= 2:
            score += 1
        if reviews is not None and reviews == 0 and score_new_high_turnover:
            score += 2
        if nightly and nightly >= 200:
            score += 1
        kept.append({**row, "score": score, "contactable": contactable})
    kept.sort(key=lambda r: r["score"], reverse=True)
    return {"qualified": kept, "rejected": rejected,
            "qualified_count": len(kept), "rejected_count": len(rejected)}


def dedupe_key(row):
    """Stable identity for a listing across vendors and re-runs.

    Prefers platformListingId (stable per platform) over the URL, and never uses the
    vendor's internal `id` which can change between runs.
    """
    pid = row.get("platform_listing_id")
    if pid and row.get("platform"):
        return f"{row['platform']}:{pid}"
    if row.get("url"):
        return row["url"].split("?")[0].rstrip("/").lower()
    return None


def dedupe(rows, seen_keys=None):
    """Drop repeats, first occurrence wins. Returns (unique, duplicate_count)."""
    seen = set(seen_keys or [])
    unique, dupes = [], 0
    for row in rows or []:
        key = dedupe_key(row)
        if key and key in seen:
            dupes += 1
            continue
        if key:
            seen.add(key)
        unique.append(row)
    return unique, dupes


def discover(location, check_in=None, check_out=None, max_results=60, seen_keys=None,
             qualify_kw=None, **search_kw):
    """Search → normalize → dedupe → qualify. The funnel, without writing anything.

    Returns the qualifying rows plus the fill-rate and credit metadata so a single call
    answers "is this worth paying for". Deliberately returns data instead of inserting into
    `leads`: `leads.email` is NOT NULL and these rows have no email, so promotion is a
    separate, PDL-gated step.
    """
    ok, payload = search_all_pages(location, max_results=max_results, check_in=check_in,
                                   check_out=check_out, **search_kw)
    if not ok:
        return {"ok": False, "error": payload.get("error", payload), "data": []}
    raw = payload.get("data") or []
    rows = [normalize(p) for p in raw]
    unique, dupes = dedupe(rows, seen_keys)
    result = qualify(unique, **(qualify_kw or {}))
    result.update({
        "ok": True,
        "location": location,
        "raw_count": len(raw),
        "deduped_count": len(unique),
        "duplicate_count": dupes,
        "fill_rate": fill_rate(raw),
        "credits_charged": (payload.get("meta") or {}).get("creditsCharged"),
        "partial": bool((payload.get("meta") or {}).get("partial")),
    })
    return result
