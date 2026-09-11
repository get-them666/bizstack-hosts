import html
import hmac
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
APP_SECRET = os.getenv("APP_SECRET")
COOKIE_NAME = "bizstack_session"

if not APP_SECRET:
    APP_SECRET = secrets.token_urlsafe(32)
    print("WARNING: APP_SECRET is not set. Set it in Railway variables for persistent sessions.")


def sign_session(email: str) -> str:
    import hashlib
    payload = email.encode()
    digest = hmac.new(APP_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return f"{email}|{digest}"


def valid_session(value: str | None) -> str | None:
    if not value or "|" not in value:
        return None
    email, digest = value.rsplit("|", 1)
    expected = sign_session(email).rsplit("|", 1)[1]
    if hmac.compare_digest(digest, expected):
        return email
    return None


def connect_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DATABASE_URL, connect_timeout=8)


def ensure_schema() -> None:
    if not DATABASE_URL:
        print("DATABASE_URL not configured; starting without database features.")
        return
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS calendar_events (
                    id BIGSERIAL PRIMARY KEY,
                    customer_name VARCHAR(255) NOT NULL,
                    phone VARCHAR(50) NOT NULL,
                    start_time TIMESTAMPTZ NOT NULL,
                    end_time TIMESTAMPTZ NOT NULL,
                    service_type VARCHAR(100) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_calendar_events_time
                ON calendar_events (start_time, end_time)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS comms_logs (
                    id BIGSERIAL PRIMARY KEY,
                    direction VARCHAR(10) NOT NULL,
                    channel VARCHAR(20) NOT NULL,
                    sender VARCHAR(255) NOT NULL,
                    recipient VARCHAR(255) NOT NULL,
                    message_body TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id BIGSERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    email VARCHAR(320) NOT NULL,
                    phone VARCHAR(50) NOT NULL,
                    listing_url TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_schema()
        print("Database schema check complete.")
    except Exception as exc:
        print(f"Database startup check failed: {exc}")
    yield


app = FastAPI(title="BizStack Hosts", version="1.0.0", lifespan=lifespan)


def require_user(request: Request) -> str:
    email = valid_session(request.cookies.get(COOKIE_NAME))
    if not email:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return email


@app.get("/health")
async def health():
    db_ok = False
    if DATABASE_URL:
        try:
            with connect_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            db_ok = True
        except Exception:
            db_ok = False
    return {"status": "ok", "database": "connected" if db_ok else "unavailable"}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return TEMPLATES.TemplateResponse(request=request, name="index.html", context={})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return TEMPLATES.TemplateResponse(request=request, name="login.html", context={"configured": bool(ADMIN_EMAIL and ADMIN_PASSWORD)})


@app.post("/api/auth/login")
async def login(email: str = Form(...), password: str = Form(...)):
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        return RedirectResponse("/login?error=Login+is+not+configured", status_code=303)
    if not hmac.compare_digest(email.strip().lower(), ADMIN_EMAIL.strip().lower()) or not hmac.compare_digest(password, ADMIN_PASSWORD):
        return RedirectResponse("/login?error=Invalid+credentials", status_code=303)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(COOKIE_NAME, sign_session(ADMIN_EMAIL), httponly=True, secure=os.getenv("COOKIE_SECURE", "true").lower() == "true", samesite="lax", max_age=60 * 60 * 12)
    return response


@app.post("/api/auth/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, email: str = Depends(require_user)):
    return TEMPLATES.TemplateResponse(request=request, name="dashboard.html", context={"user": {"email": email}})


@app.get("/hosts", response_class=HTMLResponse)
async def hosts(request: Request, email: str = Depends(require_user)):
    return TEMPLATES.TemplateResponse(request=request, name="hosts.html", context={"user": {"email": email}})


@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request, email: str = Depends(require_user)):
    return TEMPLATES.TemplateResponse(request=request, name="settings.html", context={"user": {"email": email}})


@app.get("/api/calendar")
async def calendar_events(_: str = Depends(require_user)):
    try:
        with connect_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, customer_name, phone, start_time, end_time, service_type
                    FROM calendar_events ORDER BY start_time ASC
                """)
                rows = cur.fetchall()
        return [
            {"id": r[0], "customer_name": r[1], "phone": r[2], "start_time": r[3].isoformat(), "end_time": r[4].isoformat(), "service_type": r[5]}
            for r in rows
        ]
    except Exception as exc:
        raise HTTPException(503, f"Database unavailable: {exc}") from exc


@app.post("/api/calendar/book")
async def create_booking(
    customer_name: str = Form(...),
    phone: str = Form(...),
    start_time: str = Form(...),
    service_type: str = Form(...),
    _: str = Depends(require_user),
):
    try:
        parsed = datetime.fromisoformat(start_time)
    except ValueError as exc:
        raise HTTPException(400, "Invalid date/time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    end = parsed + timedelta(hours=1)

    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM calendar_events
                WHERE start_time < %s AND end_time > %s
                LIMIT 1
            """, (end, parsed))
            if cur.fetchone():
                raise HTTPException(409, "Requested timeframe overlaps an existing booking.")
            cur.execute("""
                INSERT INTO calendar_events (customer_name, phone, start_time, end_time, service_type)
                VALUES (%s, %s, %s, %s, %s)
            """, (customer_name.strip(), phone.strip(), parsed, end, service_type.strip()))
        conn.commit()
    return RedirectResponse("/dashboard?booked=1", status_code=303)


@app.post("/submit-lead")
async def submit_lead(
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    url: str = Form("")
):
    name, email, phone, url = name.strip(), email.strip(), phone.strip(), url.strip()
    if not name or not email or not phone:
        return JSONResponse({"status": "error", "message": "Name, email, and phone are required."}, status_code=400)
    if not DATABASE_URL:
        return JSONResponse({"status": "success", "message": "Lead received."})
    try:
        with connect_db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO leads (name, email, phone, listing_url) VALUES (%s, %s, %s, %s)", (name, email, phone, url or None))
            conn.commit()
        return JSONResponse({"status": "success"})
    except Exception as exc:
        print(f"Lead save failed: {exc}")
        return JSONResponse({"status": "error", "message": "We could not save your request right now."}, status_code=503)


@app.post("/comms/sms-webhook")
async def inbound_sms_webhook(From: str = Form(...), Body: str = Form(...)):
    body = Body.strip()
    if DATABASE_URL:
        try:
            with connect_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO comms_logs (direction, channel, sender, recipient, message_body)
                        VALUES ('inbound', 'sms', %s, 'system', %s)
                    """, (From.strip(), body))
                conn.commit()
        except Exception as exc:
            print(f"SMS log failed: {exc}")
    safe_from = html.escape(From.strip(), quote=True)
    safe_body = html.escape(body[:500])
    reply = f"BizStack Hosts received your message: {safe_body}"
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message to="{safe_from}">{html.escape(reply)}</Message></Response>'
    return Response(content=xml, media_type="application/xml")
