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

CHANNEL_LABELS = {
    "airbnb": "Airbnb",
    "vrbo": "VRBO",
    "booking.com": "Booking.com",
    "booking": "Booking.com",
    "hospitable": "Hospitable",
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