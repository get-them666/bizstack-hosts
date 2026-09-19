"""Channel Manager Sync — imports reservations from Hospitable (which aggregates
Airbnb, Vrbo, Booking.com, etc.) into calendar_events so crew + host portal see them.

Base: https://public.api.hospitable.com/v2  (Hospitable Public API v2)
Auth: Bearer Personal Access Token (PAT), created under my.hospitable.com → Apps → API access.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HOSPITABLE_BASE_URL = "https://public.api.hospitable.com/v2"
HOSPITABLE_CHANNEL = "hospitable"

TURNO_BASE_URL = "https://api.turnoverbnb.com/v2"
TURNO_CHANNEL = "turno"

CHANNEL_LABELS = {
    "airbnb": "Airbnb",
    "vrbo": "VRBO",
    "booking.com": "Booking.com",
    "booking": "Booking.com",
    "hospitable": "Hospitable",
    "turno": "Turno",
    "direct": "Direct",
}

CANCELLED_STATUSES = {"cancelled", "canceled", "expired", "declined"}


def channel_label(source):
    source = (source or "").lower()
    return CHANNEL_LABELS.get(source, source or "Channel")


class HospitableService:
    def __init__(self, pat="", base_url=HOSPITABLE_BASE_URL, timeout=30):
        self.pat = (pat or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def configured(self):
        return bool(self.pat)

    def _get(self, path, params=None, multi=None):
        if not self.configured:
            raise RuntimeError("Hospitable API token is not configured.")
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        if multi:
            sep = "&" if "?" in url else "?"
            url += sep + "&".join(f"{key}={urllib.parse.quote(str(v), safe='')}" for key, values in multi.items() for v in values)
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.pat}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise RuntimeError(f"Hospitable API error {e.code}: {detail or e.reason}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Could not reach Hospitable API: {e.reason}") from None

    def get_properties(self, page=1, per_page=100):
        data = self._get("/properties", {"include": "listings,details", "page": page, "per_page": per_page})
        return data.get("data") or []

    def get_reservations(self, property_uuids, start_date, end_date, include="guest,properties,financials"):
        params = {"start_date": start_date, "end_date": end_date, "include": include}
        multi = None
        if property_uuids:
            multi = {"properties[]": property_uuids}
        data = self._get("/reservations", params, multi=multi)
        return data.get("data") or []


def _parse_ts(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _list_ids(value):
    ids = []
    for item in value or []:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
    return ids


def extract_reservation(res):
    guest = res.get("guest") or {}
    if not isinstance(guest, dict):
        guest = {}
    financials = res.get("financials") or {}
    if not isinstance(financials, dict):
        financials = {}
    total = financials.get("total") or financials.get("total_price") or financials.get("amount")
    if isinstance(total, dict):
        total = total.get("amount")
    total_cents = None
    try:
        total_cents = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_cents = None
    return {
        "id": str(res.get("id") or ""),
        "status": (res.get("status") or "").lower(),
        "source": (res.get("source") or "").lower(),
        "check_in": _parse_ts(res.get("check_in")),
        "check_out": _parse_ts(res.get("check_out")),
        "guest_name": (guest.get("name") or res.get("guest_name") or "Channel Guest").strip() or "Channel Guest",
        "guest_phone": (guest.get("phone") or "").strip(),
        "guest_email": (guest.get("email") or "").strip(),
        "property_uuids": _list_ids(res.get("properties")),
        "total_cents": total_cents,
        "raw": res,
    }


def _upsert_reservation(db, r, uuid_to_local, mapped_source):
    """Create or update one calendar_events row from a parsed reservation dict."""
    if not r["id"] or not r["check_in"] or not r["check_out"]:
        return "skipped_no_dates"

    local = None
    for pu in r["property_uuids"]:
        if pu in uuid_to_local:
            local = uuid_to_local[pu]
            break
    if local is None:
        return "skipped_unmatched"

    status_val = "cancelled" if r["status"] in CANCELLED_STATUSES else "active"

    with db.cursor() as cur:
        cur.execute(
            """SELECT id, payment_status FROM calendar_events
               WHERE channel_source = %s AND channel_booking_id = %s FOR UPDATE;""",
            (mapped_source, r["id"]),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """UPDATE calendar_events
                   SET customer_name = %s, phone = %s, start_time = %s, end_time = %s,
                       host_id = %s, property_id = %s, channel_status = %s,
                       channel_guest_email = %s, amount_cents = COALESCE(%s, amount_cents)
                   WHERE id = %s;""",
                (
                    r["guest_name"],
                    r["guest_phone"] or "(channel)",
                    r["check_in"],
                    r["check_out"],
                    local["host_id"],
                    local["id"],
                    status_val,
                    r["guest_email"],
                    r["total_cents"],
                    existing["id"],
                ),
            )
            if status_val == "cancelled" and existing["payment_status"] != "paid":
                cur.execute(
                    "UPDATE calendar_events SET payment_status = 'cancelled' WHERE id = %s;",
                    (existing["id"],),
                )
        else:
            cur.execute(
                """INSERT INTO calendar_events
                   (customer_name, phone, start_time, end_time, service_type,
                    payment_status, host_id, property_id, channel_source, channel_booking_id,
                    channel_status, channel_guest_email, amount_cents)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);""",
                (
                    r["guest_name"],
                    r["guest_phone"] or "(channel)",
                    r["check_in"],
                    r["check_out"],
                    "Channel Booking",
                    "cancelled" if status_val == "cancelled" else "unpaid",
                    local["host_id"],
                    local["id"],
                    mapped_source,
                    r["id"],
                    status_val,
                    r["guest_email"],
                    r["total_cents"],
                ),
            )
    db.commit()
    return "updated" if existing else "created"


def sync_hospitable_webhook(db, pat, res):
    """Upsert a single reservation (from a webhook payload) into calendar_events.
    Returns a summary dict.
    """
    with db.cursor() as cur:
        cur.execute("SELECT id, host_id, name, channel_property_uuid FROM properties WHERE channel_property_uuid IS NOT NULL;")
        link_rows = cur.fetchall()
    uuid_to_local = {str(r["channel_property_uuid"]): r for r in link_rows}
    r = extract_reservation(res)
    source = r["source"] or "direct"
    mapped_source = source if source in CHANNEL_LABELS else "hospitable"
    result = _upsert_reservation(db, r, uuid_to_local, mapped_source)
    if result == "skipped_no_dates":
        return {"status": "skipped", "reason": "reservation missing id or dates in webhook payload"}
    if result == "skipped_unmatched":
        return {"status": "skipped", "reason": "reservation property is not linked to a local property"}
    return {"status": "success", "result": result, "booking_id": r["id"]}


def sync_hospitable(db, pat):
    """Pull reservations from Hospitable and upsert them into calendar_events.
    Returns a summary dict and writes a channel_sync_logs row.
    """
    started = datetime.now(timezone.utc)
    svc = HospitableService(pat)
    summary = {
        "created": 0,
        "updated": 0,
        "cancelled": 0,
        "skipped_unmatched": 0,
        "skipped_no_dates": 0,
        "unlinked_properties": [],
    }

    def log_step(status, msg):
        cur = db.cursor()
        try:
            cur.execute(
                """INSERT INTO channel_sync_logs (channel, status, summary, details, started_at, finished_at)
                   VALUES (%s, %s, %s, %s, %s, %s);""",
                (HOSPITABLE_CHANNEL, status, msg, json.dumps(summary, default=str), started, datetime.now(timezone.utc)),
            )
            db.commit()
        finally:
            cur.close()

    if not pat:
        log_step("failed", "Hospitable API token is not configured.")
        return {"status": "failed", "message": "Hospitable API token not configured.", **summary}

    try:
        hosp_props = svc.get_properties()
    except RuntimeError as e:
        log_step("failed", str(e))
        return {"status": "failed", "message": str(e), **summary}

    # Map Hospitable property uuid -> local property row by channel_property_uuid
    with db.cursor() as cur:
        cur.execute("SELECT id, host_id, name, channel_property_uuid FROM properties WHERE channel_property_uuid IS NOT NULL;")
        link_rows = cur.fetchall()
    uuid_to_local = {str(r["channel_property_uuid"]): r for r in link_rows}

    for hp in hosp_props:
        hp_id = str(hp.get("id") or "")
        if hp_id and hp_id not in uuid_to_local:
            summary["unlinked_properties"].append(hp.get("name") or hp_id)

    # Sync window: a year back (to capture status changes) through a year ahead
    today = datetime.now(timezone.utc).date()
    start_date = (today - timedelta(days=365)).isoformat()
    end_date = (today + timedelta(days=365)).isoformat()

    try:
        reservations = svc.get_reservations(sorted(uuid_to_local.keys()), start_date, end_date)
    except RuntimeError as e:
        log_step("failed", str(e))
        return {"status": "failed", "message": str(e), **summary}

    for res in reservations:
        r = extract_reservation(res)
        if not r["id"] or not r["check_in"] or not r["check_out"]:
            summary["skipped_no_dates"] += 1
            continue

        local = None
        for pu in r["property_uuids"]:
            if pu in uuid_to_local:
                local = uuid_to_local[pu]
                break
        if local is None:
            summary["skipped_unmatched"] += 1
            continue

        source = r["source"] or "direct"
        mapped_source = source if source in CHANNEL_LABELS else "hospitable"
        status_val = "cancelled" if r["status"] in CANCELLED_STATUSES else "active"

        with db.cursor() as cur:
            # Upsert keyed on (channel_source, channel_booking_id)
            cur.execute(
                """SELECT id, payment_status FROM calendar_events
                   WHERE channel_source = %s AND channel_booking_id = %s FOR UPDATE;""",
                (mapped_source, r["id"]),
            )
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    """UPDATE calendar_events
                       SET customer_name = %s, phone = %s, start_time = %s, end_time = %s,
                           host_id = %s, property_id = %s, channel_status = %s,
                           channel_guest_email = %s, amount_cents = COALESCE(%s, amount_cents)
                       WHERE id = %s;""",
                    (
                        r["guest_name"],
                        r["guest_phone"] or "(channel)",
                        r["check_in"],
                        r["check_out"],
                        local["host_id"],
                        local["id"],
                        status_val,
                        r["guest_email"],
                        r["total_cents"],
                        existing["id"],
                    ),
                )
                if status_val == "cancelled" and existing["payment_status"] != "paid":
                    cur.execute(
                        "UPDATE calendar_events SET payment_status = 'cancelled' WHERE id = %s;",
                        (existing["id"],),
                    )
                summary["updated"] += 1
            else:
                cur.execute(
                    """INSERT INTO calendar_events
                       (customer_name, phone, start_time, end_time, service_type,
                        payment_status, host_id, property_id, channel_source, channel_booking_id,
                        channel_status, channel_guest_email, amount_cents)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);""",
                    (
                        r["guest_name"],
                        r["guest_phone"] or "(channel)",
                        r["check_in"],
                        r["check_out"],
                        "Channel Booking",
                        "cancelled" if status_val == "cancelled" else "unpaid",
                        local["host_id"],
                        local["id"],
                        mapped_source,
                        r["id"],
                        status_val,
                        r["guest_email"],
                        r["total_cents"],
                    ),
                )
                summary["created"] += 1
        db.commit()

    message = (
        f"Synced {len(reservations)} reservations "
        f"({summary['created']} new, {summary['updated']} updated). "
        f"{summary['skipped_unmatched']} unmatched, {len(summary['unlinked_properties'])} unlinked properties."
    )
    log_step("success", message)
    summary["status"] = "success"
    summary["message"] = message
    summary["reservation_count"] = len(reservations)
    summary["property_count"] = len(hosp_props)
    return summary


# ---------------------------------------------------------------------------
# Turno (formerly TurnoverBnB) — cleaning/calendar platform that aggregates
# Airbnb, Vrbo and friends. API v2 at https://api.turnoverbnb.com/v2.
#
# Auth is unusual: every request needs BOTH `Authorization: Bearer <secret-key>`
# and `TBNB-Partner-ID: <partner-uuid>`. Both come from Turno → API → Tokens
# ("Here is your Partner ID:" is shown at the bottom of that page).
#
# Turno sits behind Cloudflare and has been observed to challenge non-browser
# TLS fingerprints (a 403 HTML interstitial rather than a 401 JSON). We detect
# that case explicitly so an operator doesn't rotate valid credentials chasing
# a fingerprint block.
# ---------------------------------------------------------------------------


def _as_list(data, *keys):
    """Pull a list out of a response that may be bare or wrapped in an envelope."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for container in (data, data.get("data")):
            if isinstance(container, dict):
                for key in keys:
                    value = container.get(key)
                    if isinstance(value, list):
                        return value
    return []


def _looks_like_cloudflare(body, headers=None):
    if headers is not None:
        try:
            if (headers.get("cf-mitigated") or "").lower() == "challenge":
                return True
        except Exception:
            pass
    text = (body or "").lower()
    return "_cf_chl_opt" in text or "challenges.cloudflare.com" in text or "just a moment" in text


class TurnoService:
    def __init__(self, token="", partner_id="", base_url=TURNO_BASE_URL, timeout=30):
        self.token = (token or "").strip()
        self.partner_id = (partner_id or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def configured(self):
        return bool(self.token)

    def _get(self, path, params=None):
        if not self.configured:
            raise RuntimeError("Turno API token is not configured.")
        url = self.base_url + path
        cleaned = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        if cleaned:
            url += "?" + urllib.parse.urlencode(cleaned, doseq=True)
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if self.partner_id:
            headers["TBNB-Partner-ID"] = self.partner_id
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code == 403 and _looks_like_cloudflare(detail, e.headers):
                raise RuntimeError(
                    "Turno blocked the request with a Cloudflare challenge, which is a TLS-fingerprint "
                    "block rather than a credential problem. Do not rotate the Secret Key. Route Turno "
                    "requests through a Chrome-impersonating client (e.g. curl_cffi)."
                ) from None
            if e.code == 401:
                raise RuntimeError(
                    "Turno rejected the credentials (401). Check the API Secret Key and that the "
                    "TBNB-Partner-ID matches the same Turno account (both are on the Tokens page)."
                ) from None
            raise RuntimeError(f"Turno API error {e.code}: {detail or e.reason}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Could not reach Turno API: {e.reason}") from None

    def get_properties(self, page=1, limit=100):
        data = self._get("/properties", {"page": page, "limit": limit})
        return _as_list(data, "properties", "data", "results", "items")

    def get_bookings(self, checkin_from=None, checkin_to=None, checkout_from=None,
                     checkout_to=None, property_ids=None, page=1, limit=100):
        params = {
            "page": page,
            "limit": limit,
            "checkin_from": checkin_from,
            "checkin_to": checkin_to,
            "checkout_from": checkout_from,
            "checkout_to": checkout_to,
        }
        if property_ids:
            params["properties[]"] = [str(p) for p in property_ids]
        data = self._get("/bookings", params)
        return _as_list(data, "bookings", "data", "results", "items")


def extract_turno_property(p):
    return {
        "id": str(p.get("id") or ""),
        "name": (p.get("alias") or p.get("name") or p.get("address") or "Turno Property").strip(),
    }


def extract_turno_booking(b):
    guest = b.get("guest_name") or b.get("summary") or b.get("description") or ""
    return {
        "id": str(b.get("id") or ""),
        "external_ref": (str(b.get("external_booking_id") or "").strip() or None),
        "status": (b.get("status") or "").strip().lower(),
        "check_in": _parse_ts(b.get("checkin")),
        "check_out": _parse_ts(b.get("checkout")),
        "guest_name": (guest or "Channel Guest").strip() or "Channel Guest",
        "property_ids": [str(b.get("property_id"))] if b.get("property_id") else [],
        "raw": b,
    }


def sync_turno(db, token, partner_id=""):
    """Pull properties + bookings from Turno and upsert into calendar_events.

    Bookings are deduplicated by Turno's `external_booking_id` (the underlying
    OTA reservation id): if a calendar event already carries that external ref
    from any source, it is updated instead of duplicated. Otherwise the row is
    keyed on (channel_source='turno', channel_booking_id=<turno id>).
    """
    started = datetime.now(timezone.utc)
    svc = TurnoService(token, partner_id)
    summary = {
        "created": 0,
        "updated": 0,
        "adopted": 0,
        "cancelled": 0,
        "skipped_unmatched": 0,
        "skipped_no_dates": 0,
        "unlinked_properties": [],
    }

    def log_step(status, msg):
        cur = db.cursor()
        try:
            cur.execute(
                """INSERT INTO channel_sync_logs (channel, status, summary, details, started_at, finished_at)
                   VALUES (%s, %s, %s, %s, %s, %s);""",
                (TURNO_CHANNEL, status, msg, json.dumps(summary, default=str), started, datetime.now(timezone.utc)),
            )
            db.commit()
        finally:
            cur.close()

    if not svc.configured:
        log_step("failed", "Turno API token is not configured.")
        return {"status": "failed", "message": "Turno API token not configured.", **summary}

    try:
        turno_props = svc.get_properties()
    except RuntimeError as e:
        log_step("failed", str(e))
        return {"status": "failed", "message": str(e), **summary}

    with db.cursor() as cur:
        cur.execute("SELECT id, host_id, name, turno_property_id FROM properties WHERE turno_property_id IS NOT NULL;")
        link_rows = cur.fetchall()
    turno_to_local = {str(r["turno_property_id"]): r for r in link_rows}

    for tp in turno_props:
        tid = str(tp.get("id") or "")
        if tid and tid not in turno_to_local:
            summary["unlinked_properties"].append(extract_turno_property(tp)["name"] or tid)

    today = datetime.now(timezone.utc).date()
    start_date = (today - timedelta(days=365)).isoformat()
    end_date = (today + timedelta(days=365)).isoformat()

    try:
        bookings = svc.get_bookings(
            checkin_from=start_date,
            checkin_to=end_date,
            checkout_from=start_date,
            checkout_to=end_date,
        )
    except RuntimeError as e:
        log_step("failed", str(e))
        return {"status": "failed", "message": str(e), **summary}

    for booking in bookings:
        r = extract_turno_booking(booking)
        if not r["id"] or not r["check_in"] or not r["check_out"]:
            summary["skipped_no_dates"] += 1
            continue

        local = None
        for pid in r["property_ids"]:
            if pid in turno_to_local:
                local = turno_to_local[pid]
                break
        if local is None:
            summary["skipped_unmatched"] += 1
            continue

        status_val = "cancelled" if r["status"] in CANCELLED_STATUSES else "active"
        if status_val == "cancelled":
            summary["cancelled"] += 1

        with db.cursor() as cur:
            cur.execute(
                """SELECT id, payment_status FROM calendar_events
                   WHERE channel_source = %s AND channel_booking_id = %s FOR UPDATE;""",
                (TURNO_CHANNEL, r["id"]),
            )
            existing = cur.fetchone()
            adopted = False
            if not existing and r["external_ref"]:
                cur.execute(
                    """SELECT id, payment_status FROM calendar_events
                       WHERE channel_external_ref = %s AND channel_booking_id IS NOT NULL
                       ORDER BY id LIMIT 1 FOR UPDATE;""",
                    (r["external_ref"],),
                )
                existing = cur.fetchone()
                adopted = existing is not None

            if existing:
                cur.execute(
                    """UPDATE calendar_events
                       SET customer_name = %s, phone = COALESCE(NULLIF(phone, ''), %s),
                           start_time = %s, end_time = %s, host_id = %s, property_id = %s,
                           channel_status = %s, channel_external_ref = COALESCE(channel_external_ref, %s)
                       WHERE id = %s;""",
                    (
                        r["guest_name"],
                        "(channel)",
                        r["check_in"],
                        r["check_out"],
                        local["host_id"],
                        local["id"],
                        status_val,
                        r["external_ref"],
                        existing["id"],
                    ),
                )
                if status_val == "cancelled" and existing["payment_status"] != "paid":
                    cur.execute(
                        "UPDATE calendar_events SET payment_status = 'cancelled' WHERE id = %s;",
                        (existing["id"],),
                    )
                if adopted:
                    summary["adopted"] += 1
                else:
                    summary["updated"] += 1
            else:
                cur.execute(
                    """INSERT INTO calendar_events
                       (customer_name, phone, start_time, end_time, service_type,
                        payment_status, host_id, property_id, channel_source, channel_booking_id,
                        channel_status, channel_external_ref)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);""",
                    (
                        r["guest_name"],
                        "(channel)",
                        r["check_in"],
                        r["check_out"],
                        "Channel Booking",
                        "cancelled" if status_val == "cancelled" else "unpaid",
                        local["host_id"],
                        local["id"],
                        TURNO_CHANNEL,
                        r["id"],
                        status_val,
                        r["external_ref"],
                    ),
                )
                summary["created"] += 1
        db.commit()

    message = (
        f"Synced {len(bookings)} Turno bookings "
        f"({summary['created']} new, {summary['updated']} updated, {summary['adopted']} merged by external id). "
        f"{summary['skipped_unmatched']} unmatched, {len(summary['unlinked_properties'])} unlinked properties."
    )
    log_step("success", message)
    summary["status"] = "success"
    summary["message"] = message
    summary["reservation_count"] = len(bookings)
    summary["property_count"] = len(turno_props)
    return summary