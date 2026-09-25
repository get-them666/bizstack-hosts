#!/usr/bin/env python3
"""Decide whether StayingAPI is worth paying for, before paying for it.

Run this once with a free sandbox key (stayingapi.com/signup, no card). It spends a
small slice of the 300 free credits on a single Myrtle Beach whole-home search and
reports the only number that matters: how often a result carries something we can turn
into a contact.

Why this is the deciding test
-----------------------------
StayingAPI's OpenAPI spec has no email or phone field, and both `host` and `host.name`
are nullable. So the vendor is only worth a subscription if a useful share of Airbnb
results actually come back with a host name or an address to feed PDL. If that share is
near zero, no amount of downstream code saves the pipeline, and the cheaper path is a
seed list of listing IDs plus YouTube/Google Maps for owner names.

Usage
-----
    STAYINGAPI_KEY=stay_test_... python3 probe_stayingapi_fillrate.py
    STAYINGAPI_KEY=stay_live_... python3 probe_stayingapi_fillrate.py --limit 20
    python3 probe_stayingapi_fillrate.py --location "Hilton Head, SC" --dry-run

Costs: Airbnb search is 2 credits/result, min 5 per platform, so --limit 20 is ~40 credits.
A live call is async and may take a minute; the client polls for you.
"""

import argparse
import sys

import stayingapi_service as sa

DEFAULT_LOCATION = "Myrtle Beach, SC"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--location", default=DEFAULT_LOCATION,
                        help="Place name or lat,lng (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=20,
                        help="Results to fetch, 1-40 (default: %(default)s)")
    parser.add_argument("--check-in", dest="check_in", default=None)
    parser.add_argument("--check-out", dest="check_out", default=None)
    parser.add_argument("--platforms", default="airbnb",
                        help="Comma-separated (default: %(default)s)")
    parser.add_argument("--min-bedrooms", dest="min_bedrooms", type=int, default=2)
    parser.add_argument("--max-bedrooms", dest="max_bedrooms", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan and estimated credit cost, then exit")
    args = parser.parse_args()

    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]

    if args.dry_run:
        est = max(args.limit * 2, 5) if "airbnb" in platforms else max(args.limit, 5)
        print("Would search:")
        print(f"  location     {args.location}")
        print(f"  platforms    {', '.join(platforms)}")
        print(f"  bedrooms     {args.min_bedrooms}-{args.max_bedrooms}")
        print(f"  dates        {args.check_in or 'none'} -> {args.check_out or 'none'}")
        print(f"  limit        {args.limit} per platform")
        print(f"  est. credits ~{est}")
        return 0

    if not sa.is_configured():
        print("[FAIL] STAYINGAPI_KEY is not set. Get a free key at stayingapi.com/signup")
        return 1

    account = sa.account()
    if not account:
        print("[WARN] Could not read /v1/account - continuing, but you cannot see "
              "whether this key is live-verified.")
    else:
        plan = account.get("plan", {})
        key_env = (account.get("key") or {}).get("env", "?")
        credits = (account.get("credits") or {}).get("available", "?")
        print(f"Plan: {plan.get('name', plan.get('code', '?'))} | key: {key_env} "
              f"| credits available: {credits}")
        if key_env != "live":
            print("  (sandbox key: fixtures only - proves the integration, not the "
                  "data. Verify your email to unlock live credits for a real answer.)")

    print(f"\nSearching {args.location} for whole homes...\n")
    result = sa.discover(
        args.location,
        check_in=args.check_in, check_out=args.check_out,
        platforms=platforms, min_bedrooms=args.min_bedrooms,
        max_bedrooms=args.max_bedrooms, limit=args.limit,
    )

    if not result["ok"]:
        err = result.get("error") or {}
        print(f"[FAIL] {err.get('code', 'unknown')}: {err.get('message', 'no message')}")
        if err.get("code") == "email_unverified":
            print("  -> Click the verification link in your email, then re-run.")
        elif err.get("code") in ("invalid_api_key", "missing_api_key"):
            print("  -> Check STAYINGAPI_KEY.")
        elif err.get("code") == "insufficient_credits":
            print("  -> Out of credits; failed/empty calls are free, so check for a "
                  "repeating bad request.")
        return 1

    stats = result["fill_rate"]
    print(f"Fetched {result['raw_count']} results "
          f"({result['deduped_count']} unique, {result['duplicate_count']} dupes), "
          f"{result['credits_charged']} credits charged"
          + (", PARTIAL" if result["partial"] else ""))
    print(f"Qualified {result['qualified_count']} for cleaning fit "
          f"({result['rejected_count']} filtered out)\n")

    print("Contact-seed fill rate (decides whether this vendor is worth paying for):")
    print(f"  host name present  {stats['host_name']:>5}%")
    print(f"  address present    {stats['address']:>5}%")
    print(f"  either             {stats['either']:>5}%")
    print(f"  neither            {stats['neither']:>5}%")

    print("\nTop qualified listings:")
    for row in result["qualified"][:10]:
        rate = f"${row['nightly_rate']:.0f}" if row["nightly_rate"] else "no price"
        print(f"  [{row['score']}] {row['bedrooms']}BR sleeps {row['sleeps']} | "
              f"{row['rating_normalized'] or '?'}* ({row['review_count'] if row['review_count'] is not None else '?'} reviews) | "
              f"{rate} | host={row['host_name'] or '-'}")
        print(f"       {row['url']}")

    print("\nVERDICT")
    if stats["n"] == 0:
        print("  No results. Check the location string or enabled platforms.")
    elif stats["either"] >= 60:
        print(f"  {stats['either']}% of results carry a contact seed. Worth a paid plan: "
              "these feed PDL.")
    elif stats["either"] >= 25:
        print(f"  {stats['either']}% carry a contact seed. Marginal. Usable with a "
              "name+market PDL search rather than reverse-address.")
    else:
        print(f"  Only {stats['either']}% carry a contact seed. Not worth $19/mo for "
              "lead-gen.")
        print("  Fall back to a seed list of listing IDs + StayAPI for comps, and "
              "YouTube/Google Maps for owner names.")
    print("\nNothing was inserted into `leads` - `leads.email` is NOT NULL and these "
          "rows have no email. Promotion stays PDL-gated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
