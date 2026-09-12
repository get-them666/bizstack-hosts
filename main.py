import os
import json
import math
import secrets
import hashlib
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, Form, Response, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ai_agent import BusinessAIAgent
from analysis_service import RentalAnalysisService
from stripe_service import StripeService

db_url = os.getenv("DATABASE_URL", "postgresql://shaun:secret@localhost:5432/bizstack")
templates = Jinja2Templates(directory="templates")
stripe_svc = StripeService()
rental_analysis = RentalAnalysisService()


def _money(value):
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


templates.env.filters["money"] = _money

APP_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "America/New_York"))
BOOKING_HOURS = int(os.getenv("BOOKING_HOURS", "1"))


def parse_booking_time(raw: str) -> datetime:
    raw = (raw or "").strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=APP_TZ)
    return parsed.astimezone(APP_TZ)


def build_tool_handlers(db, stripe_svc):
    """DB/Stripe tool handlers that let the AI agent run the business end-to-end."""

    def check_booking_availability(start_time: str):
        try:
            requested = parse_booking_time(start_time)
        except (ValueError, TypeError):
            return {"ok": False, "error": "Could not parse the date/time. Ask the guest for a specific date and time (e.g. Friday at 2pm)."}

        window_end = requested + timedelta(hours=BOOKING_HOURS)
        conflict_query = "SELECT COUNT(*) FROM calendar_events WHERE start_time < %s AND end_time > %s;"
        with db.cursor() as cur:
            cur.execute(conflict_query, (window_end, requested))
            conflict = cur.fetchone()["count"] > 0

        open_slots = []
        for offset in (-2, -1, 1, 2):
            cand = requested + timedelta(hours=offset)
            cand_end = cand + timedelta(hours=BOOKING_HOURS)
            cur.execute(conflict_query, (cand_end, cand))
            if cur.fetchone()["count"] == 0:
                open_slots.append(cand.isoformat())

        if conflict:
            return {
                "ok": True,
                "available": False,
                "requested": requested.isoformat(),
                "message": f"{requested:%A, %B %-d at %-I:%M %p} is already booked.",
                "nearest_open_slots": open_slots,
            }
        return {
            "ok": True,
            "available": True,
            "requested": requested.isoformat(),
            "message": f"{requested:%A, %B %-d at %-I:%M %p} is open.",
            "nearest_open_slots": open_slots,
        }

    def create_booking(customer_name: str, phone: str, service_type: str, start_time: str):
        if not all([customer_name, phone, service_type, start_time]):
            return {"ok": False, "error": "Missing booking details. Need name, phone, service, and start time."}
        try:
            requested = parse_booking_time(start_time)
        except (ValueError, TypeError):
            return {"ok": False, "error": "Could not parse the start time. Confirm the exact date and time with the guest."}
        parsed_end = requested + timedelta(hours=BOOKING_HOURS)

        with db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE start_time < %s AND end_time > %s;",
                (parsed_end, requested),
            )
            if cur.fetchone()["count"] > 0:
                return {
                    "ok": False,
                    "available": False,
                    "message": f"{requested:%A, %B %-d at %-I:%M %p} conflicts with an existing booking.",
                }
            cur.execute(
                "INSERT INTO calendar_events (customer_name, phone, start_time, end_time, service_type) VALUES (%s, %s, %s, %s, %s) RETURNING id;",
                (customer_name, phone, requested, parsed_end, service_type),
            )
            event_id = cur.fetchone()["id"]
            amount_cents = stripe_svc.get_price(service_type)
            cur.execute("UPDATE calendar_events SET amount_cents = %s WHERE id = %s;", (amount_cents, event_id))
            db.commit()

        payment_url = ""
        stripe_error = ""
        try:
            payment_url = stripe_svc.create_checkout_session(
                event_id=event_id,
                customer_name=customer_name,
                customer_email="",
                service_type=service_type,
                start_time=requested,
            )
            session_id = None
            if "session_id=" in payment_url:
                session_id = payment_url.split("session_id=")[-1].split("&")[0]
            if session_id:
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE calendar_events SET stripe_session_id = %s WHERE id = %s;",
                        (session_id, event_id),
                    )
                    db.commit()
        except Exception as e:
            print(f"⚠️ Stripe checkout creation skipped: {e}")
            stripe_error = str(e)

        return {
            "ok": True,
            "booking_id": event_id,
            "customer_name": customer_name,
            "service_type": service_type,
            "start_time": requested.isoformat(),
            "price_usd": f"{amount_cents / 100:.2f}",
            "payment_url": payment_url,
            "stripe_error": stripe_error or None,
        }

    def lookup_bookings(phone: str):
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, customer_name, service_type, start_time, end_time, payment_status, amount_cents FROM calendar_events WHERE phone = %s ORDER BY start_time DESC LIMIT 10;",
                (phone,),
            )
            rows = cur.fetchall()
            for row in rows:
                row["start_time"] = row["start_time"].isoformat()
                row["end_time"] = row["end_time"].isoformat()
        return {"ok": True, "phone": phone, "bookings": rows}

    def register_customer(name: str, email: str = "", phone: str = ""):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO customers (name, email, phone, source) VALUES (%s, %s, %s, 'ai-assistant') RETURNING id;",
                (name, email, phone),
            )
            customer_id = cur.fetchone()["id"]
            db.commit()
        return {"ok": True, "customer_id": customer_id}

    def get_business_summary():
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM hosts"); hosts = cur.fetchone()["count"]
            cur.execute("SELECT COUNT(*) FROM customers"); customers = cur.fetchone()["count"]
            cur.execute("SELECT COUNT(*) FROM calendar_events"); bookings = cur.fetchone()["count"]
            cur.execute("SELECT COUNT(*) FROM leads"); leads = cur.fetchone()["count"]
        return {"ok": True, "hosts": hosts, "customers": customers, "bookings": bookings, "leads": leads}

    return {
        "check_booking_availability": check_booking_availability,
        "create_booking": create_booking,
        "lookup_bookings": lookup_bookings,
        "register_customer": register_customer,
        "get_business_summary": get_business_summary,
    }

@asynccontextmanager
async def lifecycle(app: FastAPI):
    print("📡 Initing cloud-native table migration layer check...")
    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                CREATE TABLE IF NOT EXISTS calendar_events (
                    id SERIAL PRIMARY KEY,
                    customer_name VARCHAR(255) NOT NULL,
                    phone VARCHAR(50) NOT NULL,
                    start_time TIMESTAMP WITH TIME ZONE NOT NULL,
                    end_time TIMESTAMP WITH TIME ZONE NOT NULL,
                    service_type VARCHAR(100) NOT NULL,
                    payment_status VARCHAR(50) DEFAULT 'unpaid',
                    stripe_session_id VARCHAR(255),
                    amount_cents INTEGER,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS payment_status VARCHAR(50) DEFAULT 'unpaid';")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS stripe_session_id VARCHAR(255);")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS amount_cents INTEGER;")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    booking_id INTEGER NOT NULL UNIQUE REFERENCES calendar_events(id),
                    stripe_session_id VARCHAR(255),
                    stripe_payment_intent_id VARCHAR(255),
                    amount_cents INTEGER NOT NULL,
                    currency VARCHAR(10) DEFAULT 'usd',
                    status VARCHAR(50) NOT NULL DEFAULT 'paid',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS comms_logs (
                    id SERIAL PRIMARY KEY,
                    direction VARCHAR(10) NOT NULL,
                    channel VARCHAR(10) NOT NULL,
                    sender VARCHAR(255) NOT NULL,
                    recipient VARCHAR(255) NOT NULL,
                    message_body TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(50) DEFAULT 'customer',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS hosts (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    property_name VARCHAR(255),
                    status VARCHAR(50) DEFAULT 'active',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("ALTER TABLE hosts ADD COLUMN IF NOT EXISTS email VARCHAR(255) UNIQUE;")
                cur.execute("ALTER TABLE hosts ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);")
                cur.execute("ALTER TABLE hosts ADD COLUMN IF NOT EXISTS host_token VARCHAR(255);")
                cur.execute("ALTER TABLE hosts ADD COLUMN IF NOT EXISTS phone VARCHAR(50);")
                cur.execute("ALTER TABLE hosts ADD COLUMN IF NOT EXISTS login_token VARCHAR(255);")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS properties (
                    id SERIAL PRIMARY KEY,
                    host_id INTEGER REFERENCES hosts(id),
                    name VARCHAR(255) NOT NULL,
                    address VARCHAR(500),
                    lat DOUBLE PRECISION,
                    lng DOUBLE PRECISION,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS customers (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    email VARCHAR(255),
                    phone VARCHAR(50),
                    source VARCHAR(100) DEFAULT 'manual',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS bookings (
                    id SERIAL PRIMARY KEY,
                    customer_name VARCHAR(255) NOT NULL,
                    service_type VARCHAR(100) NOT NULL,
                    status VARCHAR(50) DEFAULT 'pending',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    email VARCHAR(255) NOT NULL,
                    phone VARCHAR(50),
                    listing_url TEXT,
                    status VARCHAR(50) DEFAULT 'new',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS zip VARCHAR(10);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS analysis_json TEXT;")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS worker_id INTEGER;")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS worker_pay_cents INTEGER;")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS worker_status VARCHAR(50) DEFAULT 'assigned';")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS job_lat DOUBLE PRECISION;")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS job_lng DOUBLE PRECISION;")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS job_address VARCHAR(500);")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS host_id INTEGER;")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS property_id INTEGER REFERENCES properties(id);")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS workers (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    phone VARCHAR(50),
                    email VARCHAR(255),
                    pay_rate_cents INTEGER NOT NULL DEFAULT 0,
                    pin_hash VARCHAR(255),
                    worker_token VARCHAR(255),
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS worker_paychecks (
                    id SERIAL PRIMARY KEY,
                    worker_id INTEGER NOT NULL REFERENCES workers(id),
                    period_start DATE NOT NULL,
                    period_end DATE NOT NULL,
                    job_count INTEGER NOT NULL DEFAULT 0,
                    gross_cents INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS worker_timeclocks (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES calendar_events(id),
                    worker_id INTEGER NOT NULL REFERENCES workers(id),
                    action VARCHAR(10) NOT NULL,
                    lat DOUBLE PRECISION,
                    lng DOUBLE PRECISION,
                    accuracy_m DOUBLE PRECISION,
                    distance_m DOUBLE PRECISION,
                    in_fence BOOLEAN,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("ALTER TABLE hosts ADD COLUMN IF NOT EXISTS property_address VARCHAR(500);")
                conn.commit()
        print("🚀 Database connectivity and tables validated successfully.")
    except Exception as e:
        print(f"❌ Structural database connection failure: {e}")
    yield

app = FastAPI(lifespan=lifecycle)

def get_db():
    conn = psycopg.connect(db_url, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()

def require_auth(request: Request):
    token = request.cookies.get("session_token")
    return bool(token), request.cookies.get("user_email")

# --- PUBLIC LANDING & AUTH ---

@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/login", response_class=HTMLResponse)
async def read_login(request: Request):
    error = request.query_params.get("error")
    return templates.TemplateResponse(request=request, name="login.html", context={"error": error})

@app.post("/api/auth/login")
async def api_login(email: str = Form(...), password: str = Form(...)):
    admin_email = os.getenv("ADMIN_EMAIL", "shaun@example.com")
    admin_password = os.getenv("ADMIN_PASSWORD", "password123")
    if email == admin_email and password == admin_password:
        token = secrets.token_urlsafe(32)
        response = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key="session_token", value=token, httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "false").lower() == "true")
        response.set_cookie(key="user_email", value=email, httponly=True, samesite="lax")
        return response
    return RedirectResponse(url="/login?error=Invalid+Credentials", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/api/auth/logout")
async def api_logout():
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("session_token")
    response.delete_cookie("user_email")
    return response

# --- LEAD CAPTURE (LANDING PAGE FORM) ---

@app.post("/submit-lead")
async def submit_lead(
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    url: str = Form(""),
    db=Depends(get_db)
):
    result = rental_analysis.analyze(url) if url else {"ok": False, "error": "No property address provided."}
    analysis_json = json.dumps(result)
    status_value = "analyzed" if result.get("ok") else "new"
    zip_code = result.get("zip", "")

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (name, email, phone, listing_url, status, zip, analysis_json) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (name, email, phone, url, status_value, zip_code, analysis_json)
        )
        lead_id = cur.fetchone()["id"]
        db.commit()

    response = {"status": "success", "lead_id": lead_id}
    if result.get("ok"):
        response["analysis_ready"] = True
        response["analysis_url"] = f"/analysis/{lead_id}"
    else:
        response["analysis_ready"] = False
    return JSONResponse(content=response)

@app.get("/analysis/{lead_id}", response_class=HTMLResponse)
async def view_analysis(lead_id: int, request: Request, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM leads WHERE id = %s;", (lead_id,))
        lead = cur.fetchone()
    if not lead:
        return templates.TemplateResponse(request=request, name="analysis.html", context={"lead": None, "data": None, "map_url": ""}, status_code=404)

    try:
        analysis = json.loads(lead["analysis_json"]) if lead.get("analysis_json") else {}
    except (TypeError, ValueError):
        analysis = {}

    data = analysis.get("data") if analysis else None
    map_url = rental_analysis.map_embed_url(lead["listing_url"] or "")

    return templates.TemplateResponse(
        request=request,
        name="analysis.html",
        context={
            "lead": lead,
            "data": data,
            "map_url": map_url,
            "analysis_ok": bool(analysis.get("ok") and data),
            "error": analysis.get("error") if not analysis.get("ok") else "",
        },
    )

# --- DASHBOARD ---

@app.get("/dashboard", response_class=HTMLResponse)
async def read_dashboard(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM hosts")
        hosts_count = cur.fetchone()['count']
        cur.execute("SELECT COUNT(*) FROM calendar_events")
        bookings_count = cur.fetchone()['count']
        cur.execute("SELECT COUNT(*) FROM customers")
        customers_count = cur.fetchone()['count']
        cur.execute("SELECT COUNT(*) FROM leads")
        leads_count = cur.fetchone()['count']

        cur.execute("SELECT id, customer_name, phone, start_time, end_time, service_type, payment_status, amount_cents FROM calendar_events WHERE start_time >= NOW() - INTERVAL '7 days' ORDER BY start_time DESC LIMIT 10")
        events = cur.fetchall()

        cur.execute("SELECT p.id, p.name, COALESCE(h.name, 'Unassigned host') AS host_name FROM properties p LEFT JOIN hosts h ON h.id = p.host_id ORDER BY p.name;")
        properties = cur.fetchall()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": {"email": user_email},
            "stats": {"hosts": hosts_count, "bookings": bookings_count, "customers": customers_count, "leads": leads_count},
            "events": events,
            "properties": properties,
        }
    )

# --- HOSTS MANAGEMENT ---

@app.get("/hosts", response_class=HTMLResponse)
async def hosts_page(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    with db.cursor() as cur:
        cur.execute("SELECT * FROM leads ORDER BY created_at DESC LIMIT 50")
        leads = cur.fetchall()
        cur.execute("SELECT * FROM hosts ORDER BY created_at DESC")
        host_accounts = cur.fetchall()
        cur.execute("""
            SELECT id, customer_name, start_time, service_type
            FROM calendar_events
            WHERE host_id IS NULL AND start_time >= NOW() - INTERVAL '30 days'
            ORDER BY start_time DESC;
        """)
        unlinked_events = cur.fetchall()

    return templates.TemplateResponse(
        request=request,
        name="hosts.html",
        context={
            "user": {"email": user_email},
            "leads": leads,
            "host_accounts": host_accounts,
            "unlinked_events": unlinked_events,
            "generated_pw": request.query_params.get("pw"),
            "form_error": request.query_params.get("error"),
        },
    )

@app.post("/api/hosts")
async def create_host(
    name: str = Form(...),
    property_name: str = Form(""),
    property_address: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    password: str = Form(""),
    db=Depends(get_db),
):
    generated = ""
    if email.strip() and not password.strip():
        generated = _generate_password()
        password = generated
    pw_hash = _hash_password(password) if email.strip() else None

    if email.strip():
        with db.cursor() as cur:
            cur.execute("SELECT id FROM hosts WHERE LOWER(email) = LOWER(%s);", (email.strip(),))
            if cur.fetchone():
                return RedirectResponse(url="/hosts?error=That+email+already+has+an+account", status_code=303)

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO hosts (name, property_name, property_address, email, phone, password_hash) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (name, property_name, property_address, email.strip(), phone, pw_hash),
        )
        host_id = cur.fetchone()["id"]
        db.commit()

    query = f"?pw={urllib.parse.quote(generated)}" if generated else ""
    return RedirectResponse(url=f"/hosts{query}", status_code=303)

@app.post("/api/hosts/{host_id}/set-login")
async def set_host_login(host_id: int, email: str = Form(...), password: str = Form(""), db=Depends(get_db)):
    generated = ""
    if not password.strip():
        generated = _generate_password()
        password = generated
    with db.cursor() as cur:
        cur.execute(
            "UPDATE hosts SET email = %s, password_hash = %s, status = 'active' WHERE id = %s;",
            (email.strip(), _hash_password(password), host_id),
        )
        db.commit()
    query = f"?pw={urllib.parse.quote(generated)}" if generated else ""
    return RedirectResponse(url=f"/hosts{query}", status_code=303)

@app.post("/api/hosts/from-lead/{lead_id}")
async def lead_to_host(lead_id: int, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM leads WHERE id = %s;", (lead_id,))
        lead = cur.fetchone()
        if not lead:
            return RedirectResponse(url="/hosts?error=Lead+not+found", status_code=303)
        if lead.get("email") and cur.execute(
            "SELECT id FROM hosts WHERE LOWER(email) = LOWER(%s);", (lead["email"],)
        ).fetchone():
            return RedirectResponse(url="/hosts?error=That+email+already+has+an+account", status_code=303)
        password = _generate_password()
        cur.execute(
            "INSERT INTO hosts (name, email, phone, password_hash, status) VALUES (%s, %s, %s, %s, 'active') RETURNING id;",
            (lead["name"], lead.get("email") or "", lead.get("phone") or "", _hash_password(password)),
        )
        cur.execute("UPDATE leads SET status = 'host' WHERE id = %s;", (lead_id,))
        db.commit()
    return RedirectResponse(url=f"/hosts?pw={urllib.parse.quote(password)}", status_code=303)

@app.post("/api/hosts/{host_id}/link-booking")
async def link_host_booking(host_id: int, event_id: int = Form(...), db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("UPDATE calendar_events SET host_id = %s WHERE id = %s;", (host_id, event_id))
        db.commit()
    return RedirectResponse(url="/hosts", status_code=303)

@app.post("/api/customers")
async def create_customer(name: str = Form(...), email: str = Form(""), phone: str = Form(""), db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("INSERT INTO customers (name, email, phone) VALUES (%s, %s, %s) RETURNING id", (name, email, phone))
        customer_id = cur.fetchone()["id"]
        db.commit()
    return RedirectResponse(url="/hosts", status_code=303)

# --- HOST PORTAL & PORTFOLIO ---

@app.get("/host-login", response_class=HTMLResponse)
async def read_host_login(request: Request):
    error = request.query_params.get("error")
    return templates.TemplateResponse(request=request, name="host_login.html", context={"error": error, "user": None})

@app.post("/api/host/auth/login")
async def host_login(email: str = Form(...), password: str = Form(...), db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM hosts WHERE email ILIKE %s AND status = 'active' AND password_hash IS NOT NULL;", (email.strip(),))
        host = cur.fetchone()
    if not host or host["password_hash"] != _hash_password(password):
        return RedirectResponse(url="/host-login?error=Invalid+email+or+password", status_code=status.HTTP_303_SEE_OTHER)

    token = secrets.token_urlsafe(32)
    with db.cursor() as cur:
        cur.execute("UPDATE hosts SET login_token = %s WHERE id = %s;", (token, host["id"]))
        db.commit()

    redirect = RedirectResponse(url="/host", status_code=status.HTTP_303_SEE_OTHER)
    redirect.set_cookie(key="host_session", value=token, httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "false").lower() == "true")
    redirect.set_cookie(key="host_id", value=str(host["id"]), httponly=True, samesite="lax")
    return redirect

@app.get("/api/host/auth/logout")
async def host_logout():
    redirect = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie("host_session")
    redirect.delete_cookie("host_id")
    return redirect

@app.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    with db.cursor() as cur:
        cur.execute("""
            SELECT h.*,
                   COUNT(DISTINCT p.id) AS property_count,
                   COUNT(DISTINCT ce.id) AS booking_count
            FROM hosts h
            LEFT JOIN properties p ON p.host_id = h.id
            LEFT JOIN calendar_events ce ON ce.host_id = h.id
            GROUP BY h.id
            ORDER BY h.created_at DESC;
        """)
        hosts = cur.fetchall()
        cur.execute("SELECT p.*, h.name AS host_name FROM properties p LEFT JOIN hosts h ON h.id = p.host_id ORDER BY p.created_at DESC;")
        properties = cur.fetchall()
        cur.execute("""
            SELECT ce.id, ce.customer_name, ce.start_time, ce.service_type, ce.payment_status,
                   p.name AS property_name, h.name AS host_name
            FROM calendar_events ce
            LEFT JOIN properties p ON p.id = ce.property_id
            LEFT JOIN hosts h ON h.id = ce.host_id
            WHERE ce.host_id IS NULL AND ce.start_time >= NOW() - INTERVAL '90 days'
            ORDER BY ce.start_time ASC;
        """)
        unlinked = cur.fetchall()

    return templates.TemplateResponse(
        request=request,
        name="portfolio.html",
        context={
            "user": {"email": user_email},
            "hosts": hosts,
            "properties": properties,
            "unlinked": unlinked,
            "host_choices": [{"id": h["id"], "name": h["name"]} for h in hosts],
        },
    )

@app.post("/api/hosts/create-account")
async def create_host_account(
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    property_name: str = Form(""),
    property_address: str = Form(""),
    db=Depends(get_db),
):
    password = secrets.token_urlsafe(9)
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO hosts (name, email, phone, password_hash) VALUES (%s, %s, %s, %s) RETURNING id;",
                        (name, email.strip(), phone, _hash_password(password)))
            host_id = cur.fetchone()["id"]
            if (property_name or "").strip():
                lat, lng = None, None
                if (property_address or "").strip():
                    coords = _geocode(property_address)
                    if coords:
                        lat, lng = coords[0], coords[1]
                cur.execute("INSERT INTO properties (host_id, name, address, lat, lng) VALUES (%s, %s, %s, %s, %s);",
                            (host_id, property_name.strip(), (property_address or "").strip(), lat, lng))
            db.commit()
    except psycopg.errors.UniqueViolation as e:
        db.rollback()
        print(f"⚠️ create_host_account UniqueViolation: {e}")
        return JSONResponse(status_code=400, content={"status": "error", "message": "A host with that email already exists."})
    return JSONResponse(content={"status": "success", "host_id": host_id, "password": password})

@app.post("/api/hosts/{host_id}/properties")
async def add_host_property(host_id: int, property_name: str = Form(...), property_address: str = Form(""), db=Depends(get_db)):
    lat, lng = None, None
    if (property_address or "").strip():
        coords = _geocode(property_address)
        if coords:
            lat, lng = coords[0], coords[1]
    with db.cursor() as cur:
        cur.execute("INSERT INTO properties (host_id, name, address, lat, lng) VALUES (%s, %s, %s, %s, %s);",
                    (host_id, property_name.strip(), (property_address or "").strip(), lat, lng))
        db.commit()
    return RedirectResponse(url="/portfolio", status_code=303)

@app.post("/api/hosts/{host_id}/reset-password")
async def reset_host_password(host_id: int, db=Depends(get_db)):
    password = secrets.token_urlsafe(9)
    with db.cursor() as cur:
        cur.execute("UPDATE hosts SET password_hash = %s, login_token = NULL WHERE id = %s;", (_hash_password(password), host_id))
        db.commit()
    return JSONResponse(content={"status": "success", "host_id": host_id, "password": password})

@app.post("/api/hosts/{host_id}/deactivate")
async def deactivate_host(host_id: int, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("UPDATE hosts SET status = 'inactive', login_token = NULL WHERE id = %s;", (host_id,))
        db.commit()
    return RedirectResponse(url="/portfolio", status_code=303)

@app.post("/api/events/{event_id}/link-property")
async def link_event_property(event_id: int, property_id: int = Form(...), db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT host_id FROM properties WHERE id = %s;", (property_id,))
        prop = cur.fetchone()
        if not prop:
            raise HTTPException(status_code=404, detail="Property not found")
        if prop["host_id"] is None:
            raise HTTPException(status_code=400, detail="Property has no host")
        cur.execute("UPDATE calendar_events SET property_id = %s, host_id = %s WHERE id = %s;", (property_id, prop["host_id"], event_id))
        db.commit()
    return RedirectResponse(url="/portfolio", status_code=303)

@app.get("/host", response_class=HTMLResponse)
async def host_portal(request: Request, db=Depends(get_db)):
    host = _require_host(request, db)
    if not host:
        return RedirectResponse(url="/host-login", status_code=status.HTTP_303_SEE_OTHER)

    with db.cursor() as cur:
        cur.execute("SELECT * FROM properties WHERE host_id = %s ORDER BY created_at;", (host["id"],))
        properties = cur.fetchall()
        cur.execute("""
            SELECT ce.id, ce.customer_name, ce.start_time, ce.end_time, ce.service_type, ce.payment_status,
                   ce.job_address, ce.job_lat, ce.job_lng, ce.worker_status,
                   p.name AS property_name, p.address AS property_address,
                   w.id AS worker_id, w.name AS worker_name, w.phone AS worker_phone
            FROM calendar_events ce
            LEFT JOIN properties p ON p.id = ce.property_id
            LEFT JOIN workers w ON w.id = ce.worker_id
            WHERE ce.host_id = %s
            ORDER BY ce.start_time DESC LIMIT 60;
        """, (host["id"],))
        raw_events = cur.fetchall()

    event_ids = [e["id"] for e in raw_events]
    clock_latest = {}
    clock_counts = {}
    if event_ids:
        with db.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (event_id) event_id, action, lat, lng, created_at, distance_m
                FROM worker_timeclocks
                WHERE event_id = ANY(%s)
                ORDER BY event_id, created_at DESC;
            """, (event_ids,))
            for row in cur.fetchall():
                clock_latest[row["event_id"]] = row
            cur.execute("SELECT event_id, COUNT(*) AS how_many FROM worker_timeclocks WHERE event_id = ANY(%s) GROUP BY event_id;", (event_ids,))
            for row in cur.fetchall():
                clock_counts[row["event_id"]] = row["how_many"]

    events = []
    for ev in raw_events:
        latest = clock_latest.get(ev["id"])
        on_clock = bool(latest and latest["action"] in ("in", "update"))
        last_seen_mins = None
        if latest:
            try:
                last_seen_mins = int((datetime.now() - latest["created_at"]).total_seconds() // 60)
            except TypeError:
                pass
        map_url = None
        if on_clock and latest and latest["lat"] is not None and latest["lng"] is not None:
            map_url = _map_embed_url(latest["lat"], latest["lng"])
        elif ev["job_lat"] is not None and ev["job_lng"] is not None:
            map_url = _map_embed_url(ev["job_lat"], ev["job_lng"])
        events.append({
            **ev,
            "on_clock": on_clock,
            "last_seen_mins": last_seen_mins,
            "map_url": map_url,
            "clocks_recorded": clock_counts.get(ev["id"], 0),
            "is_future": ev["start_time"] >= datetime.now(APP_TZ),
        })

    future = [e for e in events if e["is_future"]]
    past = [e for e in events if not e["is_future"]]

    return templates.TemplateResponse(
        request=request,
        name="host_portal.html",
        context={
            "user": None,
            "host": host,
            "properties": properties,
            "events": events,
            "future": future[:15],
            "past": past[:30],
            "next_event": future[0] if future else None,
        },
    )

# --- CREW ACCOUNTS & PAYCHECKS ---

FENCE_METERS = 100.584  # 1/16 mile

def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

def _geocode(address: str):
    """Best-effort geocode via OpenStreetMap Nominatim. Returns (lat, lng) or None."""
    if not (address or "").strip():
        return None
    query = urllib.parse.quote(address.strip())
    url = f"https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q={query}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "BizStackHostsOps/1.0 (bizstackperks.com; hello@bizstackperks.com)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        if rows:
            return float(rows[0]["lat"]), float(rows[0]["lon"])
    except Exception as e:
        print(f"⚠️ Geocode skipped: {e}")
    return None

def _event_job_location(cur, event_id: int):
    cur.execute("SELECT job_lat, job_lng, job_address FROM calendar_events WHERE id = %s;", (event_id,))
    row = cur.fetchone()
    if not row:
        return None
    return {"lat": row["job_lat"], "lng": row["job_lng"], "address": row["job_address"]}

def _map_embed_url(lat: float, lng: float, zoom: int = 16) -> str:
    return f"https://maps.google.com/maps?q={lat},{lng}&z={zoom}&output=embed"

def _hash_pin(pin: str) -> str:
    return hashlib.sha256(f"{pin}:{os.getenv('APP_SECRET', 'bizstack')}".encode()).hexdigest()

def _hash_password(password: str) -> str:
    return hashlib.sha256(f"host:{password}:{os.getenv('APP_SECRET', 'bizstack')}".encode()).hexdigest()

def _generate_password(length: int = 8) -> str:
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(chars) for _ in range(length))

def _require_host(request: Request, db):
    token = request.cookies.get("host_session")
    host_id = request.cookies.get("host_id")
    if not token or not host_id:
        return None
    with db.cursor() as cur:
        cur.execute("SELECT * FROM hosts WHERE id = %s AND login_token = %s AND status = 'active';", (host_id, token))
        return cur.fetchone()

def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())

def _require_worker(request: Request, db):
    token = request.cookies.get("worker_session")
    worker_id = request.cookies.get("worker_id")
    if not token or not worker_id:
        return None
    with db.cursor() as cur:
        cur.execute("SELECT * FROM workers WHERE id = %s AND worker_token = %s AND is_active = TRUE;", (worker_id, token))
        return cur.fetchone()

def _can_view_paycheck(request: Request, worker_id: int) -> bool:
    is_authed, _ = require_auth(request)
    if is_authed:
        return True
    return request.cookies.get("worker_id") == str(worker_id)

@app.get("/crew", response_class=HTMLResponse)
async def crew_page(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    with db.cursor() as cur:
        cur.execute("""
            SELECT w.*,
                   COUNT(ce.id) FILTER (WHERE ce.worker_id = w.id AND ce.worker_status = 'completed') AS jobs_done,
                   COALESCE(SUM(ce.worker_pay_cents) FILTER (WHERE ce.worker_id = w.id AND ce.worker_status = 'completed'), 0) AS earned_cents,
                   COUNT(ce.id) FILTER (WHERE ce.worker_id = w.id AND ce.worker_status <> 'completed') AS jobs_assigned
            FROM workers w
            LEFT JOIN calendar_events ce ON ce.worker_id = w.id
            GROUP BY w.id
            ORDER BY w.created_at DESC;
        """)
        workers = cur.fetchall()
        cur.execute("""
            SELECT id, customer_name, start_time, service_type, amount_cents
            FROM calendar_events
            WHERE worker_id IS NULL AND start_time >= NOW() - INTERVAL '60 days'
            ORDER BY start_time ASC;
        """)
        unassigned = cur.fetchall()
        cur.execute("""
            SELECT ce.id, ce.customer_name, ce.start_time, ce.service_type, ce.worker_status, ce.worker_pay_cents,
                   w.id AS worker_id, w.name AS worker_name
            FROM calendar_events ce
            LEFT JOIN workers w ON w.id = ce.worker_id
            WHERE ce.worker_id IS NOT NULL AND ce.start_time >= NOW() - INTERVAL '90 days'
            ORDER BY ce.start_time DESC LIMIT 60;
        """)
        scheduled = cur.fetchall()
        cur.execute("""
            SELECT tc.id, tc.action, tc.lat, tc.lng, tc.accuracy_m, tc.distance_m, tc.in_fence, tc.created_at,
                   w.name AS worker_name, ce.customer_name, ce.job_lat, ce.job_lng
            FROM worker_timeclocks tc
            JOIN workers w ON w.id = tc.worker_id
            JOIN calendar_events ce ON ce.id = tc.event_id
            ORDER BY tc.created_at DESC LIMIT 30;
        """)
        timeclocks = cur.fetchall()
        cur.execute("""
            SELECT DISTINCT ON (tc.worker_id, tc.event_id)
                   tc.worker_id, tc.event_id, tc.action, tc.lat, tc.lng, tc.accuracy_m, tc.distance_m, tc.in_fence,
                   tc.created_at AS last_seen, w.name AS worker_name, ce.customer_name,
                   ce.job_lat, ce.job_lng, ce.start_time
            FROM worker_timeclocks tc
            JOIN workers w ON w.id = tc.worker_id
            JOIN calendar_events ce ON ce.id = tc.event_id
            ORDER BY tc.worker_id, tc.event_id, tc.created_at DESC;
        """)
        on_clock_rows = [r for r in cur.fetchall() if r["action"] in ("in", "update")]

    clock_rows = []
    for tc in timeclocks:
        clock_rows.append({
            **tc,
            "map_url": _map_embed_url(tc["lat"], tc["lng"]) if tc["lat"] is not None and tc["lng"] is not None else None,
            "status": "In zone" if tc["in_fence"] else ("Off-site" if tc["in_fence"] is False else "No fence"),
        })

    live_rows = []
    for tc in on_clock_rows:
        mins = None
        try:
            mins = int((datetime.now() - tc["last_seen"]).total_seconds() // 60)
        except TypeError:
            pass
        live_rows.append({
            **tc,
            "map_url": _map_embed_url(tc["lat"], tc["lng"]) if tc["lat"] is not None and tc["lng"] is not None else None,
            "status": "In zone" if tc["in_fence"] else ("Off-site" if tc["in_fence"] is False else "No fence"),
            "last_seen_mins": mins,
        })

    return templates.TemplateResponse(
        request=request,
        name="crew.html",
        context={
            "user": {"email": user_email},
            "workers": workers,
            "unassigned": unassigned,
            "scheduled": scheduled,
            "timeclocks": clock_rows,
            "on_clock": live_rows,
            "worker_choices": [{"id": w["id"], "name": w["name"]} for w in workers],
            "worker_rates": {w["id"]: w["pay_rate_cents"] for w in workers},
        },
    )

@app.post("/api/workers")
async def create_worker(
    name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    pay_rate: float = Form(0),
    db=Depends(get_db),
):
    pin = f"{secrets.randbelow(10000):04d}"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO workers (name, phone, email, pay_rate_cents, pin_hash) VALUES (%s, %s, %s, %s, %s) RETURNING id;",
            (name, phone, email, int(round(pay_rate * 100)), _hash_pin(pin)),
        )
        worker_id = cur.fetchone()["id"]
        db.commit()
    return JSONResponse(content={"status": "success", "worker_id": worker_id, "pin": pin})

@app.post("/api/workers/{worker_id}/reset-pin")
async def reset_worker_pin(worker_id: int, db=Depends(get_db)):
    pin = f"{secrets.randbelow(10000):04d}"
    with db.cursor() as cur:
        cur.execute("UPDATE workers SET pin_hash = %s WHERE id = %s;", (_hash_pin(pin), worker_id))
        db.commit()
    return JSONResponse(content={"status": "success", "pin": pin})

@app.post("/api/workers/{worker_id}/deactivate")
async def deactivate_worker(worker_id: int, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("UPDATE workers SET is_active = FALSE, worker_token = NULL WHERE id = %s;", (worker_id,))
        db.commit()
    return RedirectResponse(url="/crew", status_code=303)

@app.post("/api/workers/{worker_id}/assign")
async def assign_worker_job(worker_id: int, event_id: int = Form(...), pay_rate: str = Form(""), job_address: str = Form(""), db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT pay_rate_cents FROM workers WHERE id = %s;", (worker_id,))
        worker = cur.fetchone()
        if not worker:
            raise HTTPException(status_code=404, detail="Worker not found")
        if pay_rate and pay_rate.strip():
            pay_cents = int(round(float(pay_rate.strip()) * 100))
        else:
            pay_cents = worker["pay_rate_cents"]
        job_lat, job_lng = None, None
        if (job_address or "").strip():
            coords = _geocode(job_address)
            if coords:
                job_lat, job_lng = coords[0], coords[1]
        cur.execute(
            "UPDATE calendar_events SET worker_id = %s, worker_pay_cents = %s, worker_status = 'assigned', job_address = %s, job_lat = %s, job_lng = %s WHERE id = %s;",
            (worker_id, pay_cents, job_address, job_lat, job_lng, event_id),
        )
        db.commit()
    return RedirectResponse(url="/crew", status_code=303)

@app.post("/api/events/{event_id}/complete")
async def complete_worker_job(event_id: int, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("UPDATE calendar_events SET worker_status = 'completed' WHERE id = %s;", (event_id,))
        db.commit()
    return RedirectResponse(url="/crew", status_code=303)

@app.post("/api/workers/{worker_id}/paycheck")
async def generate_paycheck(worker_id: int, period_start: str = Form(...), period_end: str = Form(...), db=Depends(get_db)):
    period_start_d = date.fromisoformat(period_start)
    period_end_d = date.fromisoformat(period_end)
    with db.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS job_count, COALESCE(SUM(worker_pay_cents), 0) AS gross_cents
            FROM calendar_events
            WHERE worker_id = %s AND worker_status = 'completed'
              AND start_time::date BETWEEN %s AND %s;
        """, (worker_id, period_start_d, period_end_d))
        totals = cur.fetchone()
        cur.execute(
            "INSERT INTO worker_paychecks (worker_id, period_start, period_end, job_count, gross_cents) VALUES (%s, %s, %s, %s, %s) RETURNING id;",
            (worker_id, period_start_d, period_end_d, totals["job_count"], totals["gross_cents"]),
        )
        paycheck_id = cur.fetchone()["id"]
        db.commit()
    return RedirectResponse(url=f"/crew/paycheck/{paycheck_id}", status_code=303)

@app.get("/crew/paycheck/{paycheck_id}", response_class=HTMLResponse)
@app.get("/worker/paycheck/{paycheck_id}", response_class=HTMLResponse)
async def view_paycheck(paycheck_id: int, request: Request, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("""
            SELECT p.*, w.name AS worker_name, w.email AS worker_email, w.phone AS worker_phone, w.pay_rate_cents
            FROM worker_paychecks p JOIN workers w ON w.id = p.worker_id
            WHERE p.id = %s;
        """, (paycheck_id,))
        row = cur.fetchone()
    if not row or not _can_view_paycheck(request, row["worker_id"]):
        return RedirectResponse(url="/login", status_code=303)
    is_authed, user_email = require_auth(request)
    return templates.TemplateResponse(
        request=request,
        name="paycheck.html",
        context={
            "user": {"email": user_email} if is_authed else None,
            "stub": row,
            "gross": row["gross_cents"] / 100,
            "per_job": (row["gross_cents"] / row["job_count"] / 100) if row["job_count"] else None,
        },
    )

# --- WORKER PORTAL ---

@app.get("/worker-login", response_class=HTMLResponse)
async def read_worker_login(request: Request):
    error = request.query_params.get("error")
    return templates.TemplateResponse(request=request, name="worker_login.html", context={"error": error, "user": None})

@app.post("/api/worker/auth/login")
async def worker_login(phone: str = Form(...), pin: str = Form(...), db=Depends(get_db)):
    phone_digits = _digits(phone)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM workers WHERE is_active = TRUE;")
        candidates = cur.fetchall()
    match = None
    for w in candidates:
        if (_digits(w.get("phone")) and _digits(w["phone"])[-10:] == phone_digits[-10:] and _digits(w["phone"])
                and w["pin_hash"] == _hash_pin(pin.strip())):
            match = w
            break
    if not match:
        return RedirectResponse(url="/worker-login?error=Invalid+phone+or+PIN", status_code=status.HTTP_303_SEE_OTHER)

    token = secrets.token_urlsafe(32)
    with db.cursor() as cur:
        cur.execute("UPDATE workers SET worker_token = %s WHERE id = %s;", (token, match["id"]))
        db.commit()

    redirect = RedirectResponse(url="/worker", status_code=status.HTTP_303_SEE_OTHER)
    redirect.set_cookie(key="worker_session", value=token, httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "false").lower() == "true")
    redirect.set_cookie(key="worker_id", value=str(match["id"]), httponly=True, samesite="lax")
    return redirect

@app.get("/api/worker/auth/logout")
async def worker_logout():
    redirect = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie("worker_session")
    redirect.delete_cookie("worker_id")
    return redirect

@app.get("/worker", response_class=HTMLResponse)
async def worker_portal(request: Request, db=Depends(get_db)):
    worker = _require_worker(request, db)
    if not worker:
        return RedirectResponse(url="/worker-login", status_code=status.HTTP_303_SEE_OTHER)

    with db.cursor() as cur:
        cur.execute("""
            SELECT id, customer_name, phone, start_time, service_type, worker_status, worker_pay_cents, job_lat, job_lng, job_address
            FROM calendar_events
            WHERE worker_id = %s AND start_time >= NOW() - INTERVAL '30 days'
            ORDER BY start_time DESC;
        """, (worker["id"],))
        raw_jobs = cur.fetchall()
        jobs = []
        for j in raw_jobs:
            cur.execute(
                "SELECT action, created_at FROM worker_timeclocks WHERE event_id = %s AND worker_id = %s ORDER BY created_at ASC;",
                (j["id"], worker["id"]),
            )
            clocks = cur.fetchall()
            clocked_in = bool(clocks and clocks[-1]["action"] == "in")
            site_seconds = 0
            pin_time = None
            for c in clocks:
                if c["action"] == "in":
                    pin_time = c["created_at"]
                elif c["action"] == "out" and pin_time:
                    site_seconds += (c["created_at"] - pin_time).total_seconds()
                    pin_time = None
            if clocked_in and pin_time:
                site_seconds += (datetime.now(pin_time.tzinfo) - pin_time).total_seconds()
            jobs.append({
                **j,
                "clocked_in": clocked_in,
                "site_seconds": int(site_seconds),
                "job_coords": j["job_lat"] is not None and j["job_lng"] is not None,
                "job_map_url": _map_embed_url(j["job_lat"], j["job_lng"]) if j["job_lat"] is not None and j["job_lng"] is not None else None,
            })
        cur.execute("""
            SELECT COALESCE(SUM(worker_pay_cents), 0) AS earned_cents, COUNT(*) AS jobs_done
            FROM calendar_events WHERE worker_id = %s AND worker_status = 'completed';
        """, (worker["id"],))
        totals = cur.fetchone()
        cur.execute("""
            SELECT id, period_start, period_end, job_count, gross_cents
            FROM worker_paychecks WHERE worker_id = %s ORDER BY created_at DESC;
        """, (worker["id"],))
        paychecks = cur.fetchall()

    return templates.TemplateResponse(
        request=request,
        name="worker_portal.html",
        context={
            "user": None,
            "worker": worker,
            "jobs": jobs,
            "totals": totals,
            "paychecks": paychecks,
            "job_coords_map": {
                j["id"]: {"lat": j["job_lat"], "lng": j["job_lng"]} for j in jobs if j["job_lat"] is not None and j["job_lng"] is not None
            },
        },
    )

@app.post("/api/worker/jobs/{event_id}/complete")
async def worker_complete_job(event_id: int, request: Request, db=Depends(get_db)):
    worker = _require_worker(request, db)
    if not worker:
        raise HTTPException(status_code=401, detail="Not authenticated")
    with db.cursor() as cur:
        cur.execute("UPDATE calendar_events SET worker_status = 'completed' WHERE id = %s AND worker_id = %s;", (event_id, worker["id"]))
        updated = cur.rowcount
        db.commit()
    if updated == 0:
        raise HTTPException(status_code=404, detail="Job not found")
    return RedirectResponse(url="/worker", status_code=303)

@app.post("/api/worker/clock")
async def worker_clock(
    request: Request,
    event_id: int = Form(...),
    action: str = Form(...),
    lat: float = Form(...),
    lng: float = Form(...),
    accuracy: float = Form(None),
    db=Depends(get_db),
):
    worker = _require_worker(request, db)
    if not worker:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if action not in ("in", "update", "out"):
        raise HTTPException(status_code=400, detail="Action must be 'in', 'update' or 'out'")

    distance_m = None
    in_fence = None
    with db.cursor() as cur:
        cur.execute("SELECT id, job_lat, job_lng FROM calendar_events WHERE id = %s AND worker_id = %s;", (event_id, worker["id"]))
        job = cur.fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found or not assigned to you")
        cur.execute(
            "SELECT action FROM worker_timeclocks WHERE event_id = %s AND worker_id = %s ORDER BY created_at DESC LIMIT 1;",
            (event_id, worker["id"]),
        )
        last = cur.fetchone()
    on_clock = last is not None and last["action"] in ("in", "update")

    if action == "in":
        if on_clock:
            raise HTTPException(status_code=400, detail="You're already clocked in for this job.")
        if job["job_lat"] is not None and job["job_lng"] is not None:
            distance_m = _haversine_m(lat, lng, job["job_lat"], job["job_lng"])
            in_fence = distance_m <= FENCE_METERS
            if not in_fence:
                return JSONResponse(status_code=400, content={
                    "status": "error",
                    "message": f"You're {int(distance_m)} m ({int(distance_m * 3.28084)} ft) from the job — must be within 100 m (1/16 mile) to clock in.",
                    "distance_m": int(distance_m),
                })
    else:
        if not on_clock:
            if action == "out":
                return JSONResponse(status_code=400, content={"status": "error", "message": "You're not clocked in for this job."})
            raise HTTPException(status_code=400, detail="Not on the clock — location tracking only runs while you're clocked in.")

    if action in ("update", "in") and job["job_lat"] is not None and job["job_lng"] is not None:
        distance_m = _haversine_m(lat, lng, job["job_lat"], job["job_lng"])
        in_fence = distance_m <= FENCE_METERS

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO worker_timeclocks (event_id, worker_id, action, lat, lng, accuracy_m, distance_m, in_fence) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);",
            (event_id, worker["id"], action, lat, lng, accuracy, distance_m, in_fence),
        )
        db.commit()

    if action == "in":
        msg = f"Clocked in. Your location is now shared with your manager until you clock out. In zone: {int(distance_m)} m from job." if distance_m is not None else "Clocked in. Your location is now shared with your manager until you clock out."
    elif action == "update":
        msg = f"Location updated ({int(distance_m)} m from job)." if distance_m is not None else "Location updated."
    else:
        msg = "Clocked out. Location sharing stopped."
    return JSONResponse(content={"status": "ok", "action": action, "event_id": event_id, "message": msg, "distance_m": distance_m})

# --- MESSAGING / COMMS CENTER ---

@app.get("/comms", response_class=HTMLResponse)
async def comms_page(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM comms_logs")
        comms_count = cur.fetchone()['count']
        cur.execute("SELECT * FROM comms_logs ORDER BY created_at DESC LIMIT 50")
        comms = cur.fetchall()

    return templates.TemplateResponse(request=request, name="comms.html", context={"user": {"email": user_email}, "comms": comms, "comms_count": comms_count, "signalwire_phone": os.getenv("SIGNALWIRE_PHONE", "")})

# --- SETTINGS ---

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="settings.html", context={"user": {"email": user_email}, "stripe_configured": stripe_svc.is_configured(), "signalwire_phone": os.getenv("SIGNALWIRE_PHONE", "")})

# --- CALENDAR API ---

@app.get("/api/calendar")
async def fetch_calendar_data(db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT id, customer_name, phone, start_time, end_time, service_type FROM calendar_events ORDER BY start_time ASC;")
        rows = cur.fetchall()
        for row in rows:
            row['start_time'] = row['start_time'].isoformat()
            row['end_time'] = row['end_time'].isoformat()
        return rows

@app.post("/api/calendar/book")
async def create_manual_booking(
    customer_name: str = Form(...),
    phone: str = Form(...),
    start_time: str = Form(...),
    service_type: str = Form(...),
    property_id: int = Form(0),
    db=Depends(get_db)
):
    parsed_start = datetime.fromisoformat(start_time)
    parsed_end = parsed_start + timedelta(hours=1)

    host_id = None
    if property_id:
        with db.cursor() as cur:
            cur.execute("SELECT host_id FROM properties WHERE id = %s;", (property_id,))
            prop = cur.fetchone()
            if prop and prop["host_id"]:
                host_id = prop["host_id"]
            else:
                property_id = None
    else:
        property_id = None

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM calendar_events WHERE start_time < %s AND end_time > %s;", (parsed_end, parsed_start))
        if cur.fetchone()['count'] > 0:
            raise HTTPException(status_code=400, detail="Requested timeframe collides with an active event.")
        cur.execute(
            "INSERT INTO calendar_events (customer_name, phone, start_time, end_time, service_type, host_id, property_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;",
            (customer_name, phone, parsed_start, parsed_end, service_type, host_id, property_id)
        )
        event_id = cur.fetchone()['id']
        amount_cents = stripe_svc.get_price(service_type)
        cur.execute(
            "UPDATE calendar_events SET amount_cents = %s WHERE id = %s;",
            (amount_cents, event_id)
        )
        db.commit()

    # Create Stripe Checkout session so the guest can pay for this booking
    try:
        checkout_url = stripe_svc.create_checkout_session(
            event_id=event_id,
            customer_name=customer_name,
            customer_email="",
            service_type=service_type,
            start_time=parsed_start,
        )
        session_id = None
        if "session_id=" in checkout_url:
            session_id = checkout_url.split("session_id=")[-1].split("&")[0]
        if session_id:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE calendar_events SET stripe_session_id = %s WHERE id = %s;",
                    (session_id, event_id)
                )
                db.commit()
        return RedirectResponse(url=checkout_url, status_code=303)
    except Exception as e:
        print(f"⚠️ Stripe checkout creation skipped: {e}")
        return RedirectResponse(url="/dashboard", status_code=303)

@app.get("/api/payments/create-link/{event_id}")
async def create_payment_link(event_id: int, db=Depends(get_db)):
    """Create (or re-create) a Stripe Checkout link for an unpaid booking."""
    with db.cursor() as cur:
        cur.execute("SELECT * FROM calendar_events WHERE id = %s;", (event_id,))
        event = cur.fetchone()
    if not event:
        return JSONResponse(content={"error": "Booking not found"}, status_code=404)
    if event["payment_status"] == "paid":
        return JSONResponse(content={"error": "Booking already paid"}, status_code=400)

    try:
        checkout_url = stripe_svc.create_checkout_session(
            event_id=event["id"],
            customer_name=event["customer_name"],
            customer_email="",
            service_type=event["service_type"],
            start_time=event["start_time"],
        )
        return JSONResponse(content={"url": checkout_url})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/payments/success", response_class=HTMLResponse)
async def payments_success(request: Request, db=Depends(get_db)):
    session_id = request.query_params.get("session_id")
    booking = None
    if session_id:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM calendar_events WHERE stripe_session_id = %s;", (session_id,))
            booking = cur.fetchone()
    return templates.TemplateResponse(
        request=request,
        name="payment_success.html",
        context={"booking": booking}
    )

@app.get("/payments/cancel", response_class=HTMLResponse)
async def payments_cancel(request: Request):
    return templates.TemplateResponse(request=request, name="payment_cancel.html", context={})

@app.post("/api/payments/webhook")
async def payments_webhook(request: Request, db=Depends(get_db)):
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        event = stripe_svc.construct_webhook_event(payload, signature)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook signature verification failed: {e}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        event_id = session.get("metadata", {}).get("event_id")
        payment_intent = session.get("payment_intent")
        amount_total = session.get("amount_total", 0)
        currency = session.get("currency", "usd")

        if event_id:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE calendar_events SET payment_status = 'paid', stripe_session_id = %s, amount_cents = %s WHERE id = %s;",
                    (session.get("id"), amount_total, int(event_id))
                )
                cur.execute(
                    """INSERT INTO payments (booking_id, stripe_session_id, stripe_payment_intent_id, amount_cents, currency)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (booking_id) DO NOTHING;""",
                    (int(event_id), session.get("id"), payment_intent, amount_total, currency)
                )
                db.commit()

    return Response(content='{"received": true}', media_type="application/json")

# --- BUSINESS API ---

@app.get("/api/business/summary")
async def business_summary(db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM hosts"); hosts = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) FROM customers"); customers = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) FROM calendar_events"); bookings = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) FROM leads"); leads = cur.fetchone()["count"]
    return {"hosts": hosts, "customers": customers, "bookings": bookings, "leads": leads}

# --- SIGNALWIRE SMS WEBHOOK ---

@app.post("/comms/sms-webhook")
async def inbound_sms_webhook(
    request: Request,
    db=Depends(get_db)
):
    form = await request.form()
    From = form.get("From", "")
    Body = form.get("Body", "")

    agent = BusinessAIAgent(tool_handlers=build_tool_handlers(db, stripe_svc))
    ai_reply = agent.process_inbound_text(f"Inbound SMS from {From}: {Body}")

    with db.cursor() as cur:
        cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('inbound', 'sms', %s, 'system', %s);", (From, Body))
        db.commit()

    sxml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Message to="{From}">{ai_reply}</Message></Response>"""
    return Response(content=sxml_payload, media_type="application/xml")

# --- SIGNALWIRE VOICE WEBHOOK ---

@app.post("/comms/voice-webhook")
async def voice_webhook(request: Request, db=Depends(get_db)):
    form = await request.form()
    CallSid = form.get("CallSid", "unknown")
    From = form.get("From", "unknown")
    To = form.get("To", "unknown")

    with db.cursor() as cur:
        cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('inbound', 'voice', %s, %s, %s);", (From, To, f"Voice call received - SID: {CallSid}"))
        db.commit()

    greeting = "Thank you for calling BizStack Hosts! Our automated assistant is ready to help with bookings, house rules, or checkout instructions. How can I assist you today?"
    twiml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">{greeting}</Say>
    <Record maxLength="30" action="/comms/voice-action" transcribe="true" transcribeCallback="/comms/voice-transcribe"/>
</Response>"""
    return Response(content=twiml_payload, media_type="application/xml")

@app.post("/comms/voice-action")
async def voice_action(request: Request, db=Depends(get_db)):
    form = await request.form()
    RecordingUrl = form.get("RecordingUrl", "")
    RecordingSid = form.get("RecordingSid", "")

    ai_response = "Thank you for your message. Our team will follow up with you shortly via text. Have a great day!"

    twiml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">{ai_response}</Say>
    <Hangup/>
</Response>"""
    return Response(content=twiml_payload, media_type="application/xml")

@app.post("/comms/voice-transcribe")
async def voice_transcribe(request: Request, db=Depends(get_db)):
    form = await request.form()
    TranscriptionText = form.get("TranscriptionText", "")
    From = form.get("From", "unknown")
    CallSid = form.get("CallSid", "")

    if TranscriptionText:
        try:
            agent = BusinessAIAgent(tool_handlers=build_tool_handlers(db, stripe_svc))
            ai_reply = agent.process_inbound_text(f"Voice transcription from {From}: {TranscriptionText}")
        except Exception:
            ai_reply = "Thank you for your call. Our team will follow up shortly."

        with db.cursor() as cur:
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('inbound', 'voice-transcription', %s, 'system', %s);", (From, TranscriptionText))
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('outbound', 'sms', 'system', %s, %s);", (From, ai_reply))
            db.commit()

    return Response(content="", status_code=204)
