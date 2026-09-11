import os
import secrets
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, Form, Response, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ai_agent import BusinessAIAgent

db_url = os.getenv("DATABASE_URL", "postgresql://shaun:secret@localhost:5432/bizstack")
templates = Jinja2Templates(directory="templates")
ai_agent = BusinessAIAgent()

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
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (name, email, phone, listing_url) VALUES (%s, %s, %s, %s) RETURNING id",
            (name, email, phone, url)
        )
        db.commit()
    return JSONResponse(content={"status": "success"})

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

        cur.execute("SELECT id, customer_name, phone, start_time, end_time, service_type FROM calendar_events WHERE start_time >= NOW() - INTERVAL '7 days' ORDER BY start_time DESC LIMIT 10")
        events = cur.fetchall()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": {"email": user_email},
            "stats": {"hosts": hosts_count, "bookings": bookings_count, "customers": customers_count, "leads": leads_count},
            "events": events
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

    return templates.TemplateResponse(request=request, name="hosts.html", context={"user": {"email": user_email}, "leads": leads})

@app.post("/api/hosts")
async def create_host(name: str = Form(...), property_name: str = Form(""), db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("INSERT INTO hosts (name, property_name) VALUES (%s, %s) RETURNING id", (name, property_name))
        host_id = cur.fetchone()["id"]
        db.commit()
    return RedirectResponse(url="/hosts", status_code=303)

@app.post("/api/customers")
async def create_customer(name: str = Form(...), email: str = Form(""), phone: str = Form(""), db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("INSERT INTO customers (name, email, phone) VALUES (%s, %s, %s) RETURNING id", (name, email, phone))
        customer_id = cur.fetchone()["id"]
        db.commit()
    return RedirectResponse(url="/hosts", status_code=303)

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

    return templates.TemplateResponse(request=request, name="comms.html", context={"user": {"email": user_email}, "comms": comms, "comms_count": comms_count})

# --- SETTINGS ---

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="settings.html", context={"user": {"email": user_email}})

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
    db=Depends(get_db)
):
    parsed_start = datetime.fromisoformat(start_time)
    parsed_end = parsed_start + timedelta(hours=1)

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM calendar_events WHERE start_time < %s AND end_time > %s;", (parsed_end, parsed_start))
        if cur.fetchone()['count'] > 0:
            raise HTTPException(status_code=400, detail="Requested timeframe collides with an active event.")
        cur.execute(
            "INSERT INTO calendar_events (customer_name, phone, start_time, end_time, service_type) VALUES (%s, %s, %s, %s, %s);",
            (customer_name, phone, parsed_start, parsed_end, service_type)
        )
        db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)

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

    ai_reply = ai_agent.process_inbound_text(f"Inbound SMS from {From}: {Body}")

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
            ai_reply = ai_agent.process_inbound_text(f"Voice transcription from {From}: {TranscriptionText}")
        except Exception:
            ai_reply = "Thank you for your call. Our team will follow up shortly."

        with db.cursor() as cur:
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('inbound', 'voice-transcription', %s, 'system', %s);", (From, TranscriptionText))
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('outbound', 'sms', 'system', %s, %s);", (From, ai_reply))
            db.commit()

    return Response(content="", status_code=204)
