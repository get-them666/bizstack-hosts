"""StayAPI client — listing intelligence for short-term-rental comps and quotes.

Airbnb runs no public API, so StayAPI (https://stayapi.com) fronts it: `x-api-key`
in, normalized JSON out. Per-listing facts (overview / details / pricing / calendar /
reviews) plus `extract-id` to turn any Airbnb room URL into a listing_id.

WHAT THIS CANNOT DO (verified against the live API 2026-09-25, do not design around
the assumption that it can):
  * There is NO Airbnb search / discovery endpoint. `/v1/meta/search` is a
    cross-OTA property *matcher* and requires `hotel_name`; there is no
    search-by-location. Every Airbnb call needs a listing_id or room URL.
    → this module cannot discover new hosts on its own.
  * `host_name` comes back null on real listings (checked 4 whole-home Myrtle Beach
    rentals), and Airbnb never exposes a street address. So StayAPI output cannot
    be turned into a contactable lead: no person, no email, and nothing for the
    PDL reverse-address enrichment in enrichment.py to key on.
  → lead generation needs a different vendor (stayingapi.com has a real
    search endpoint) or a seed list of listing IDs. Kept as a seam, see
    discover_listing_ids().

WHAT IT IS GOOD FOR: facts and live pricing for listings whose IDs you already
have — comp benchmarking, and quoting a turnover clean off a host's own numbers
(cleaning_quote).

Only runs when STAYAPI_KEY is set.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.stayapi.com/v1/airbnb"
USER_AGENT = "bizstack-stayapi/1.0 (+bizstackperks.com)"
DEFAULT_TIMEOUT = 30


def is_configured() -> bool:
    return bool((os.getenv("STAYAPI_KEY") or "").strip())


def _env_float(key, default):
    try:
        return float(os.getenv(key, ""))
    except (TypeError, ValueError):
        return default


def _env_int(key, default):
    try:
        return int(float(os.getenv(key, "")))
    except (TypeError, ValueError):
        return default


def _get(path, params=None):
    """GET a StayAPI path. Returns (ok, payload). Never raises."""
    if not is_configured():
        return False, {"error": "STAYAPI_KEY not set"}
    url = f"{API_BASE}/{path.lstrip('/')}"
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    req = urllib.request.Request(url, headers={
        "x-api-key": os.getenv("STAYAPI_KEY", ""),
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
        return True, json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            detail = {"error": f"HTTP {e.code}"}
        return False, detail
    except Exception as e:
        return False, {"error": str(e)}


def parse_rating(raw):
    """'4.87 (38)' -> (4.87, 38). Returns (None, None) when absent."""
    if not raw:
        return None, None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:\(([0-9,]+)\))?", str(raw))
    if not m:
        return None, None
    score = float(m.group(1))
    count = int(m.group(2).replace(",", "")) if m.group(2) else None
    return score, count


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


# --- thin endpoint wrappers -------------------------------------------------

def extract_listing_id(url):
    """'https://www.airbnb.com/rooms/12345' or 12345 or '12345' -> 12345, else None.

    Returns None for anything that isn't a room URL or a bare digit id, so bad
    input fails here instead of becoming a pointless upstream API call.
    """
    if url is None or isinstance(url, bool):
        return None
    text = str(url).strip()
    if not text:
        return None
    m = re.search(r"/rooms/(\d+)", text)
    if m:
        return int(m.group(1))
    if text.isdigit():
        return int(text)
    return None


def fetch_overview(listing_id):
    ok, data = _get(f"listing/{listing_id}/overview")
    return data.get("overview") if ok and data.get("success") else None


def fetch_details(listing_id, check_in, check_out, adults=2, children=0):
    ok, data = _get(f"listing/{listing_id}/details", {
        "check_in": check_in, "check_out": check_out,
        "adults": adults, "children": children,
    })
    return data.get("details") if ok and data.get("success") else None


def fetch_pricing(listing_id, check_in, check_out, adults=2, children=0, currency="USD"):
    ok, data = _get(f"listing/{listing_id}/pricing", {
        "check_in": check_in, "check_out": check_out,
        "adults": adults, "children": children, "currency": currency,
    })
    return data.get("pricing") if ok and data.get("success") else None


def fetch_calendar(listing_id, months=3):
    ok, data = _get(f"listing/{listing_id}/calendar", {"months": months})
    return data if ok else None


def fetch_reviews(listing_id, limit=10, offset=0, sort_by="MOST_RECENT"):
    ok, data = _get(f"listing/{listing_id}/reviews", {
        "limit": limit, "offset": offset, "sort_by": sort_by,
    })
    return data if ok else None


# --- merged profile ---------------------------------------------------------

def listing_profile(listing_id_or_url, check_in=None, check_out=None, adults=2):
    """Merge overview + details + pricing into one dict of quoting signals.

    Keys: listing_id, url, title, bedrooms, beds, baths, sleeps, rating,
    review_count, badge, pets_allowed, nightly_rate, available, currency.
    Missing upstream fields are simply absent — callers must treat this as
    best-effort, never as proof a listing is or isn't bookable.
    """
    listing_id = extract_listing_id(listing_id_or_url)
    if not listing_id:
        return {"ok": False, "error": "no listing_id or room URL"}
    if not check_in or not check_out:
        return {"ok": False, "error": "check_in and check_out are required"}

    profile = {
        "ok": True,
        "listing_id": listing_id,
        "url": f"https://www.airbnb.com/rooms/{listing_id}",
        "bedrooms": None, "beds": None, "baths": None, "sleeps": None,
        "rating": None, "review_count": None, "badge": None,
        "pets_allowed": None, "nightly_rate": None, "available": None,
        "currency": "USD",
    }

    overview = fetch_overview(listing_id)
    if overview:
        profile["title"] = overview.get("title") or ""
        profile["bedrooms"] = overview.get("bedrooms")
        profile["beds"] = overview.get("beds")
        profile["baths"] = overview.get("baths")
        profile["sleeps"] = overview.get("guests")
        if profile["bedrooms"] is None:
            for item in overview.get("raw_items") or []:
                m = re.search(r"([0-9]+)\s*bedroom", str(item), re.I)
                if m:
                    profile["bedrooms"] = int(m.group(1))
                    break

    details = fetch_details(listing_id, check_in, check_out, adults=adults)
    if details:
        profile["title"] = profile.get("title") or details.get("title") or ""
        score, count = parse_rating(details.get("rating"))
        profile["rating"] = score
        profile["review_count"] = count if count is not None else details.get("rating_count")
        profile["badge"] = details.get("badge")
        profile["pets_allowed"] = details.get("pets_allowed")
        if profile["sleeps"] is None:
            profile["sleeps"] = details.get("max_guests")

    pricing = fetch_pricing(listing_id, check_in, check_out, adults=adults)
    profile["available"] = bool(pricing) if pricing is not None else None
    if pricing:
        profile["nightly_rate"] = _num(pricing.get("nightly_rate"))
        profile["currency"] = pricing.get("currency") or "USD"
        profile["accommodation_total"] = _num(pricing.get("accommodation_total"))
        profile["total"] = _num(pricing.get("total"))
        profile["nights"] = pricing.get("nights")

    return profile


# --- cleaning quote ---------------------------------------------------------

# Base turnover clean by bedroom count. Bigger homes = more bath/kitchen resets.
_CLEAN_BASE = {1: 55.0, 2: 70.0, 3: 85.0, 4: 95.0}
_CLEAN_BIG_HOME = 110.0


def cleaning_quote(profile, extra_bedrooms=0):
    """Price a turnover clean off a listing's own live numbers.

    Returns the band plus the reasoning, so the bot can defend the number out
    loud instead of quoting a bare price. Sized by bedrooms, then adjusted for
    bath count, bed count, pets, and finally expressed as a share of one night's
    revenue — the framing that actually lands with a host.
    """
    bedrooms = _num(profile.get("bedrooms")) or 0
    beds = _num(profile.get("beds")) or 0
    baths = _num(profile.get("baths")) or 0
    sleeps = _num(profile.get("sleeps")) or 0
    nightly = _num(profile.get("nightly_rate"))
    pets = bool(profile.get("pets_allowed"))

    rooms = int(bedrooms) + int(extra_bedrooms or 0)
    if rooms <= 0:
        base = _CLEAN_BASE[2]
    elif rooms in _CLEAN_BASE:
        base = _CLEAN_BASE[rooms]
    else:
        base = _CLEAN_BIG_HOME

    adjustments = []
    if baths >= 3:
        base += _env_float("CLEAN_MULTI_BATH_SURCHARGE", 15.0)
        adjustments.append(f"{int(baths)} baths")
    if beds >= 6:
        base += _env_float("CLEAN_MANY_BEDS_SURCHARGE", 10.0)
        adjustments.append(f"{int(beds)} beds")
    if pets:
        base += _env_float("CLEAN_PET_SURCHARGE", 20.0)
        adjustments.append("pets allowed")

    low = round(base * 0.9, 2)
    high = round(base * 1.15, 2)
    reasons = []
    if rooms:
        reasons.append(f"{rooms} bedroom" + ("s" if rooms != 1 else ""))
    if sleeps:
        reasons.append(f"sleeps {int(sleeps)}")
    reasons.extend(adjustments)

    quote = {
        "ok": True,
        "low": low,
        "high": high,
        "mid": round(base, 2),
        "bedrooms": rooms or None,
        "reasons": reasons,
    }
    if nightly:
        quote["nightly_rate"] = nightly
        quote["pct_of_nightly_low"] = round(low / nightly * 100, 1)
        quote["pct_of_nightly_high"] = round(high / nightly * 100, 1)
    return quote


def cleaning_quote_text(profile, quote):
    """One spoken sentence justifying the number off the host's own listing.

    This gets read aloud on a voice call, so the noun phrase has to be built as
    a phrase rather than stitched together — "a 3 bedroom, 7 sleeper turnover"
    reads fine, "a that size turnover" does not.
    """
    descriptors = []
    if quote.get("bedrooms"):
        # No plural: the head noun ("turnover clean") is singular, so
        # "a 3 bedroom, 7 sleeper" is correct and "a 3 bedrooms" is not.
        descriptors.append(f"{quote['bedrooms']} bedroom")
    if profile.get("sleeps"):
        descriptors.append(f"{int(profile['sleeps'])} sleeper")
    subject = f"{', '.join(descriptors)} " if descriptors else ""

    text = f"A {subject}turnover clean runs ${quote['low']:.0f} to ${quote['high']:.0f}"
    if quote.get("nightly_rate"):
        text += (f" — about {quote['pct_of_nightly_low']:.0f} to "
                 f"{quote['pct_of_nightly_high']:.0f} percent of one "
                 f"${quote['nightly_rate']:,.0f} night")
    return text + "."


# --- discovery seam (NOT implemented — see module docstring) ----------------

def discover_listing_ids(location, check_in=None, check_out=None, adults=2):
    """Deliberately unimplemented. StayAPI has no Airbnb search endpoint.

    Left as an explicit seam so swapping in a real discovery provider
    (stayingapi.com /v1/search, or any seed list of listing IDs) is a one-function
    change. Do not fake this by scraping Airbnb — that is exactly the failure mode
    StayAPI exists to replace, and it breaks on every listing-page redesign.
    """
    raise NotImplementedError(
        "StayAPI has no Airbnb discovery endpoint; supply listing IDs explicitly "
        "(see module docstring for why reverse-address enrichment cannot help here)."
    )
