"""STR seed-list lead source — StayAPI facts for listings whose IDs you already have.

Why this exists and what it deliberately is not
----------------------------------------------
The obvious version of this was "search Airbnb for whole homes in Myrtle Beach and email
the hosts". Two things kill it, both verified against the live StayAPI API rather than assumed:

  1. `stayapi.com` has **no search endpoint** (`/v1/search` 404s, `/v1/meta/search` wants a
     `hotel_name`), so it cannot discover listings.
  2. It returns `host_name: null` on real listings and never exposes a street address, so
     there is nothing to contact and nothing for PDL reverse-address to key on.

So discovery stays a human problem: a seed list. This module takes listing IDs/URLs you
supply, pulls the facts that *are* reliable, scores them for cleaning-service fit, and stages
them. It does not claim to find hosts.

Contact is still gated. Rows land with the repo's standard `{source}-{ext}@lead.local`
placeholder — `auto_reply.py`, `email_bot.py` and `enrichment.py` all already skip and
backfill that, so it is safe — and are promoted only once a real address or name is
available for PDL. `enrich_pending_leads()` currently only handles `source='permit_finder'`
and only via address, so STR rows need their own enrichment path (see `enrich_str_leads`).

Nothing here sends email or writes to `leads` unless `commit=True`.
"""

import json
import os
import re

import stayapi_service as stay

SOURCE = "str_seed"
CAMPAIGN = "str-seed-scan"
# StayAPI bills per result, so a default window is set rather than leaving dates open —
# an undated call is a per-result charge for facts we can already get.
DEFAULT_NIGHTS = 3


def _next_checkin(nights=DEFAULT_NIGHTS):
    """A check-in a fixed distance out, so a rerun prices comparable dates.

    Deterministic on purpose: a moving target window makes two runs of the same seed list
    look like price changes when they are just different weekends.
    """
    from datetime import date, timedelta
    return (date.today() + timedelta(days=45)).isoformat()


def _checkout(check_in, nights=DEFAULT_NIGHTS):
    from datetime import date, timedelta
    y, m, d = (int(x) for x in check_in.split("-"))
    return (date(y, m, d) + timedelta(days=nights)).isoformat()


def parse_seed(raw):
    """Accept a listing ID, a room URL, or `url|name|address|market` and return one dict.

    Host name and address are optional because they are usually unknown up front — that is
    the whole reason promotion is a separate step. `market` ("Myrtle Beach, SC") is what
    makes a name-based PDL lookup possible later.
    """
    if not raw:
        return None
    line = str(raw).strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in line.split("|")]
    target = parts[0]
    listing_id = stay.extract_listing_id(target)
    if not listing_id:
        return None
    return {
        "listing_id": listing_id,
        "listing_url": f"https://www.airbnb.com/rooms/{listing_id}",
        "host_name": parts[1] if len(parts) > 1 else "",
        "address": parts[2] if len(parts) > 2 else "",
        "market": parts[3] if len(parts) > 3 else "",
    }


def parse_seeds(raw_lines):
    """Parse a seed list, de-duplicating by listing id.

    Returns (seeds, skipped) where `skipped` counts only lines that look like they were
    *meant* to be a listing but aren't parseable. Blank lines and `#` comments are
    structure, not errors — counting them would make a well-commented seed file look
    broken (the bundled str_seed_list.txt has 27 comment lines and 4 listings).
    """
    seeds, seen, skipped = [], set(), 0
    for line in (raw_lines or []):
        text = str(line).strip()
        if not text or text.startswith("#"):
            continue
        seed = parse_seed(text)
        if not seed:
            skipped += 1
            continue
        if seed["listing_id"] in seen:
            continue
        seen.add(seed["listing_id"])
        seeds.append(seed)
    return seeds, skipped


def score_for_cleaning(profile, seed=None):
    """Cleaning-service fit, 0-100, plus the reasons a human can argue with.

    Weighted toward bedrooms and occupancy because those drive cleaning hours, and toward
    nightly rate because that is what the host is actually earning per turnover. A low
    review count is *not* penalized: a new high-occupancy home has no incumbent cleaner,
    which is the easiest sale in this market.
    """
    bedrooms = profile.get("bedrooms") or 0
    sleeps = profile.get("sleeps") or 0
    baths = profile.get("baths") or 0
    nightly = profile.get("nightly_rate")
    reviews = profile.get("review_count")

    if not bedrooms:
        return {"score": 0, "qualified": False,
                "reasons": ["bedrooms unknown - cannot size the clean"]}

    reasons = [f"{bedrooms} bedroom" + ("s" if bedrooms != 1 else "")]
    score = 0
    score += min(bedrooms, 5) * 10
    if sleeps:
        reasons.append(f"sleeps {int(sleeps)}")
        score += min(int(sleeps), 12) * 2
    if baths and baths >= 2:
        reasons.append(f"{baths:g} baths")
        score += 5
    if nightly:
        reasons.append(f"${nightly:,.0f}/night")
        score += min(int(nightly // 50), 12)
    else:
        reasons.append("no dated price")
    if reviews == 0:
        reasons.append("new listing, no incumbent cleaner")
        score += 8

    seed = seed or {}
    if seed.get("host_name"):
        reasons.append(f"host known: {seed['host_name']}")
        score += 5
    if seed.get("address"):
        reasons.append("address known - PDL-ready")
        score += 5

    # 2-4BR is the sweet spot: enough work to be worth a crew, small enough to do in one visit.
    qualified = 2 <= bedrooms <= 4
    if not qualified:
        reasons.append("outside the 2-4BR target band")
    return {"score": min(score, 100), "qualified": qualified, "reasons": reasons}


def enrich_seed(seed, check_in=None, nights=DEFAULT_NIGHTS, adults=4):
    """Pull one listing's facts and score it. Never raises; unusable listings come back flagged."""
    check_in = check_in or _next_checkin(nights)
    profile = stay.listing_profile(seed["listing_id"], check_in=check_in,
                                   check_out=_checkout(check_in, nights), adults=adults)
    if not profile.get("ok"):
        return {"ok": False, "listing_id": seed["listing_id"], "error": profile.get("error"),
                "score": 0, "qualified": False, "reasons": [f"lookup failed: {profile.get('error')}"]}
    scored = score_for_cleaning(profile, seed)
    return {**profile, "ok": True, "host_name": seed.get("host_name") or None,
            "seed_address": seed.get("address") or None, "market": seed.get("market") or None,
            **scored}


def cleaning_quote_for(row):
    """Turnover-clean price band off the row's own numbers, as share of one night."""
    profile = {k: row.get(k) for k in
               ("bedrooms", "beds", "baths", "sleeps", "nightly_rate", "pets_allowed")}
    quote = stay.cleaning_quote(profile)
    return {**quote, "pitch": stay.cleaning_quote_text(profile, quote)}


def process_seeds(raw_lines, check_in=None, nights=DEFAULT_NIGHTS, min_score=0,
                  commit=False, db_url=None):
    """Seed list → facts → score → dedupe. Returns a report; writes only if commit=True.

    `commit=True` inserts into `leads` using the repo's existing conventions: the
    `{source}-{ext}@lead.local` placeholder email and a `lead_source_seen` row for dedupe.
    It still will not send anything, because every auto-send path skips `@lead.local`.
    """
    seeds, skipped = parse_seeds(raw_lines)
    rows, failures = [], []
    for seed in seeds:
        row = enrich_seed(seed, check_in=check_in, nights=nights)
        if not row.get("ok"):
            failures.append(row)
            continue
        if row.get("score", 0) < min_score:
            continue
        rows.append(row)

    rows.sort(key=lambda r: r.get("score", 0), reverse=True)
    report = {
        "seeds": len(seeds),
        "unusable_lines": skipped,
        "enriched": len(rows),
        "failed": len(failures),
        "qualified": sum(1 for r in rows if r.get("qualified")),
        "pd_ready": sum(1 for r in rows if r.get("seed_address") or r.get("host_name")),
        "credits_note": "StayAPI bills per result; one call per seed listing",
        "rows": rows,
        "failures": failures,
    }
    if commit and rows:
        report["committed"] = commit_rows(rows, db_url=db_url)
    return report


def commit_rows(rows, db_url=None):
    """Insert qualifying rows into `leads`. Mirrors _ingest_public_source_leads() in main.py."""
    db_url = db_url or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return {"ok": False, "error": "DATABASE_URL not set"}
    # Import after the guards so a missing driver reports a connection problem rather
    # than blowing up before we can say why.
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as e:
        return {"ok": False, "error": f"psycopg not installed: {e}"}

    created, seen = [], 0
    try:
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                for row in rows:
                    if not row.get("qualified"):
                        continue
                    ext = str(row.get("listing_id"))
                    cur.execute(
                        "SELECT 1 FROM lead_source_seen WHERE source = %s AND external_id = %s;",
                        (SOURCE, ext),
                    )
                    if cur.fetchone():
                        seen += 1
                        continue

                    host = row.get("host_name") or ""
                    title = row.get("title") or f"Airbnb listing {ext}"
                    name = f"{title} · {host}" if host else title
                    email = f"{SOURCE}-{ext}@lead.local"
                    address = row.get("seed_address") or ""
                    zmatch = re.findall(r"\b\d{5}(?:-\d{4})?\b", address)
                    analysis = {
                        "str_seed": True,
                        "listing_id": row.get("listing_id"),
                        "bedrooms": row.get("bedrooms"),
                        "baths": row.get("baths"),
                        "sleeps": row.get("sleeps"),
                        "rating": row.get("rating"),
                        "review_count": row.get("review_count"),
                        "nightly_rate": row.get("nightly_rate"),
                        "currency": row.get("currency"),
                        "market": row.get("market"),
                        "score": row.get("score"),
                        "reasons": row.get("reasons"),
                        "cleaning_quote": cleaning_quote_for(row),
                        "enriched": bool(host or address),
                    }
                    cur.execute(
                        "INSERT INTO leads (name, email, phone, listing_url, status, source, "
                        "zip, address, description, project_type, company, campaign, analysis_json) "
                        "VALUES (%s, %s, %s, %s, 'new', %s, %s, %s, %s, %s, 'broom', %s, %s) "
                        "RETURNING id;",
                        (
                            name[:250], email, row.get("phone") or "",
                            row.get("url") or row.get("listing_url") or "",
                            SOURCE, (zmatch[-1][:5] if zmatch else ""), address[:255],
                            f"STR seed candidate, score {row.get('score')}. "
                            + "; ".join(row.get("reasons") or [])[:1500],
                            "vacation-rental turnover cleaning",
                            CAMPAIGN, json.dumps(analysis),
                        ),
                    )
                    lead_id = cur.fetchone()["id"]
                    cur.execute(
                        "INSERT INTO lead_source_seen (source, external_id, lead_id) "
                        "VALUES (%s, %s, %s) ON CONFLICT (source, external_id) DO NOTHING;",
                        (SOURCE, ext, lead_id),
                    )
                    created.append({"lead_id": lead_id, "listing_id": ext,
                                    "name": name, "score": row.get("score")})
            conn.commit()
        return {"ok": True, "created": len(created), "already_seen": seen, "leads": created}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
