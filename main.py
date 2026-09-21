import os
import json
import math
import secrets
import hashlib
import asyncio
import hmac
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape as xml_escape
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from pathlib import Path
import threading
from contextvars import ContextVar
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, Form, Response, Depends, HTTPException, status, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import ai_agent
from ai_agent import BusinessAIAgent
from analysis_service import RentalAnalysisService
from reddit_radar import outreach_draft, scan_reddit
from linkedin_radar import scan_linkedin, outreach_draft as linkedin_outreach_draft
from stripe_service import StripeService
import stripe
import site_theme
from signalwire_service import SignalWireService
import channel_sync
import legal_forms
import documents_service
import auth_service
import training_service
import auto_reply

db_url = os.getenv("DATABASE_URL", "postgresql://shaun:secret@localhost:5432/bizstack")
templates = Jinja2Templates(directory="templates")
stripe_svc = StripeService()
signalwire = SignalWireService()
rental_analysis = RentalAnalysisService()

_company_var: ContextVar[str] = ContextVar("company_key", default="broom")


def _company_config(key: str) -> dict:
    """Identity block for the Broom service."""
    return {
        "key": "broom",
        "name": os.getenv("COMPANY_NAME", "Broom Service"),
        "phone": os.getenv("COMPANY_PHONE", "+1 (757) 846-9275"),
        "phone_e164": os.getenv("SIGNALWIRE_PHONE", "+17578469275"),
        "email": os.getenv("COMPANY_EMAIL", "hello@bizstackperks.com"),
        "domain": os.getenv("COMPANY_DOMAIN", "bizstackperks.com"),
        "license": os.getenv("CONTRACTOR_LICENSE", ""),
        "service_area": os.getenv(
            "SERVICE_AREA",
            "Williamsburg–Hampton Roads, VA",
        ),
        "founded": "2026",
    }


def company() -> dict:
    return _company_config(_company_var.get())


def _map_embed(address: str | None) -> str:
    return f"https://maps.google.com/maps?q={urllib.parse.quote_plus(address or '')}&output=embed"


def _map_directions(address: str | None) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={urllib.parse.quote_plus(address or '')}"


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

    def _fmt_dt(value):
        return value.isoformat() if hasattr(value, "isoformat") else value

    def list_upcoming_schedule(days: int = 7):
        window = datetime.now(APP_TZ) + timedelta(days=int(days))
        with db.cursor() as cur:
            cur.execute("""
                SELECT e.id, e.customer_name, e.phone, e.service_type, e.start_time, e.end_time,
                       e.payment_status, e.worker_status, e.amount_cents, w.name AS worker_name
                FROM calendar_events e LEFT JOIN workers w ON w.id = e.worker_id
                WHERE e.start_time BETWEEN NOW() AND %s ORDER BY e.start_time ASC LIMIT 100;
            """, (window,))
            rows = cur.fetchall()
        for r in rows:
            r["start_time"] = _fmt_dt(r["start_time"])
            r["end_time"] = _fmt_dt(r["end_time"])
        return {"ok": True, "events": rows}

    def list_customers(search: str = ""):
        with db.cursor() as cur:
            if search:
                cur.execute(
                    "SELECT * FROM customers WHERE name ILIKE %s OR phone ILIKE %s OR email ILIKE %s ORDER BY created_at DESC LIMIT 50;",
                    (f"%{search}%", f"%{search}%", f"%{search}%"),
                )
            else:
                cur.execute("SELECT * FROM customers ORDER BY created_at DESC LIMIT 50;")
            rows = cur.fetchall()
        return {"ok": True, "customers": rows}

    def list_leads(status: str = ""):
        with db.cursor() as cur:
            if status:
                cur.execute("SELECT * FROM leads WHERE status = %s ORDER BY created_at DESC LIMIT 100;", (status,))
            else:
                cur.execute("SELECT * FROM leads ORDER BY created_at DESC LIMIT 100;")
            rows = cur.fetchall()
        return {"ok": True, "leads": rows}

    def update_lead_status(lead_id: int, status: str):
        with db.cursor() as cur:
            cur.execute("UPDATE leads SET status = %s WHERE id = %s RETURNING id;", (status, lead_id))
            updated = cur.fetchone()
            db.commit()
        if not updated:
            return {"ok": False, "error": "Lead not found."}
        return {"ok": True, "lead_id": lead_id, "status": status}

    def add_host(name: str, email: str = "", phone: str = "", property_name: str = ""):
        try:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO hosts (name, property_name, email, phone) VALUES (%s, %s, NULLIF(%s,''), NULLIF(%s,'')) RETURNING id;",
                    (name, property_name, email, phone),
                )
                host_id = cur.fetchone()["id"]
                db.commit()
        except psycopg.errors.UniqueViolation as e:
            db.rollback()
            return {"ok": False, "error": f"A host already exists with email '{email}'.", "unique_violation": True}
        return {"ok": True, "host_id": host_id, "name": name}

    def list_hosts():
        with db.cursor() as cur:
            cur.execute("SELECT * FROM hosts ORDER BY created_at DESC LIMIT 100;")
            rows = cur.fetchall()
        return {"ok": True, "hosts": rows}

    def add_worker(name: str, phone: str = "", email: str = "", pay_rate_dollars: float = 0.0):
        pay_cents = int(round(float(pay_rate_dollars or 0) * 100))
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO workers (name, phone, email, pay_rate_cents) VALUES (%s, NULLIF(%s,''), NULLIF(%s,''), %s) RETURNING id;",
                (name, phone, email, pay_cents),
            )
            worker_id = cur.fetchone()["id"]
            db.commit()
        return {"ok": True, "worker_id": worker_id, "name": name, "pay_rate_dollars": pay_rate_dollars}

    def list_workers(active_only: bool = True):
        with db.cursor() as cur:
            if active_only:
                cur.execute("SELECT * FROM workers WHERE is_active = TRUE ORDER BY created_at DESC;")
            else:
                cur.execute("SELECT * FROM workers ORDER BY created_at DESC;")
            rows = cur.fetchall()
        for r in rows:
            r["pay_rate_dollars"] = round((r.get("pay_rate_cents") or 0) / 100, 2)
        return {"ok": True, "workers": rows}

    def update_worker(worker_id: int, name: str = "", phone: str = "", email: str = "", pay_rate_dollars: float = 0.0, is_active: bool = True):
        fields, values = [], []
        if name:
            fields.append("name = %s"); values.append(name)
        if phone:
            fields.append("phone = %s"); values.append(phone)
        if email:
            fields.append("email = %s"); values.append(email)
        if pay_rate_dollars:
            fields.append("pay_rate_cents = %s"); values.append(int(round(float(pay_rate_dollars) * 100)))
        if not fields:
            return {"ok": False, "error": "Nothing to update."}
        fields.append("is_active = %s"); values.append(bool(is_active))
        values.append(worker_id)
        with db.cursor() as cur:
            cur.execute(f"UPDATE workers SET {', '.join(fields)} WHERE id = %s RETURNING id;", values)
            updated = cur.fetchone()
            db.commit()
        if not updated:
            return {"ok": False, "error": "Worker not found."}
        return {"ok": True, "worker_id": worker_id}

    def assign_worker_to_job(event_id: int, worker_id: int):
        with db.cursor() as cur:
            cur.execute("SELECT pay_rate_cents FROM workers WHERE id = %s;", (worker_id,))
            w = cur.fetchone()
            if not w:
                return {"ok": False, "error": "Worker not found."}
            job_site_address, job_site_lat, job_site_lng = _resolve_job_site(db, event_id)
            cur.execute(
                "UPDATE calendar_events SET worker_id = %s, worker_pay_cents = %s, worker_status = 'assigned', job_address = %s, job_lat = %s, job_lng = %s WHERE id = %s RETURNING id;",
                (worker_id, w["pay_rate_cents"], job_site_address or None, job_site_lat, job_site_lng, event_id),
            )
            ev = cur.fetchone()
            db.commit()
        if not ev:
            return {"ok": False, "error": "Job/booking not found."}
        return {"ok": True, "event_id": event_id, "worker_id": worker_id}

    def list_worker_jobs(worker_id: int, days: int = 7):
        window = datetime.now(APP_TZ) + timedelta(days=int(days))
        with db.cursor() as cur:
            cur.execute("SELECT name FROM workers WHERE id = %s;", (worker_id,))
            w = cur.fetchone()
            if not w:
                return {"ok": False, "error": "Worker not found."}
            cur.execute("""
                SELECT id, customer_name, service_type, start_time, end_time, worker_status, job_address
                FROM calendar_events WHERE worker_id = %s AND start_time BETWEEN NOW() AND %s
                ORDER BY start_time ASC;
            """, (worker_id, window))
            rows = cur.fetchall()
        for r in rows:
            r["start_time"] = _fmt_dt(r["start_time"])
            r["end_time"] = _fmt_dt(r["end_time"])
        return {"ok": True, "worker": w["name"], "jobs": rows}

    def generate_paycheck(worker_id: int, period_start: str, period_end: str):
        try:
            period_start_d = date.fromisoformat(period_start)
            period_end_d = date.fromisoformat(period_end)
        except ValueError:
            return {"ok": False, "error": "Dates must be YYYY-MM-DD."}
        with db.cursor() as cur:
            cur.execute("SELECT name FROM workers WHERE id = %s;", (worker_id,))
            w = cur.fetchone()
            if not w:
                return {"ok": False, "error": "Worker not found."}
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
            cur.execute(
                "INSERT INTO ledger_entries (tx_type, ref_type, ref_id, description, amount_cents) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (ref_type, ref_id) DO NOTHING;",
                ("expense", "paycheck", paycheck_id,
                 f"Worker payroll — {w['name']} ({period_start_d.strftime('%b %d')} – {period_end_d.strftime('%b %d, %Y')})",
                 totals["gross_cents"]),
            )
            db.commit()
        return {"ok": True, "paycheck_id": paycheck_id, "worker": w["name"],
                "job_count": totals["job_count"],
                "gross_dollars": round(totals["gross_cents"] / 100, 2)}

    def list_paychecks(worker_id: int = 0):
        with db.cursor() as cur:
            if worker_id:
                cur.execute("SELECT * FROM worker_paychecks WHERE worker_id = %s ORDER BY created_at DESC;", (worker_id,))
            else:
                cur.execute("SELECT * FROM worker_paychecks ORDER BY created_at DESC LIMIT 100;")
            rows = cur.fetchall()
        for r in rows:
            r["gross_dollars"] = round((r.get("gross_cents") or 0) / 100, 2)
        return {"ok": True, "paychecks": rows}

    def get_accounting_summary(period_days: int = 30):
        since = datetime.now(APP_TZ) - timedelta(days=int(period_days))
        with db.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(amount_cents) FILTER (WHERE tx_type IN ('income','revenue')), 0) AS revenue, COALESCE(SUM(amount_cents) FILTER (WHERE tx_type = 'expense'), 0) AS expense FROM ledger_entries;")
            totals = cur.fetchone()
            cur.execute("SELECT * FROM ledger_entries WHERE created_at >= %s ORDER BY created_at DESC LIMIT 50;", (since,))
            ledger = cur.fetchall()
        revenue = float(totals["revenue"] or 0) / 100
        expense = float(totals["expense"] or 0) / 100
        return {"ok": True, "revenue_dollars": round(revenue, 2), "expense_dollars": round(expense, 2),
                "balance_dollars": round(revenue - expense, 2), "recent_ledger": ledger}

    def add_ledger_entry(tx_type: str, description: str, amount_dollars: float):
        tx = (tx_type or "").strip().lower()
        if tx in ("income", "revenue"):
            tx = "income"
        elif tx != "expense":
            return {"ok": False, "error": "tx_type must be 'income' or 'expense'."}
        amount_cents = int(round(float(amount_dollars) * 100))
        if amount_cents <= 0:
            return {"ok": False, "error": "Amount must be positive."}
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO ledger_entries (tx_type, description, amount_cents) VALUES (%s, %s, %s) RETURNING id;",
                (tx, description, amount_cents),
            )
            entry_id = cur.fetchone()["id"]
            db.commit()
        return {"ok": True, "entry_id": entry_id, "tx_type": tx, "amount_dollars": amount_dollars}

    def send_sms_message(to: str, body: str):
        if not signalwire.is_configured():
            return {"ok": False, "error": "SignalWire SMS is not configured."}
        ok = signalwire.send_sms(to, str(body)[:1600])
        return {"ok": ok, "to": to, "body": body}

    def send_email_message(to: str, subject: str, body: str):
        cfg = documents_service.smtp_config_from_env()
        if not documents_service.smtp_configured(cfg):
            return {"ok": False, "error": "SMTP email is not configured."}
        try:
            import asyncio
            asyncio.run(documents_service.send_email(cfg, to, subject, body))
            return {"ok": True, "to": to, "subject": subject}
        except Exception as e:
            return {"ok": False, "error": f"Email send failed: {e}"}

    def run_site_health_check():
        results = {}
        try:
            with db.cursor() as cur:
                cur.execute("SELECT 1;")
                results["database"] = {"ok": True, "checked": _fmt_dt(datetime.now(APP_TZ))}
        except Exception as e:
            results["database"] = {"ok": False, "error": str(e)}
        for key in ("OPENAI_API_KEY", "STRIPE_SECRET_KEY", "SIGNALWIRE_PROJECT_ID", "SIGNALWIRE_API_TOKEN"):
            results[key] = {"ok": bool(os.getenv(key))}
        smtp = documents_service.smtp_config_from_env()
        results["SMTP_EMAIL"] = {"ok": documents_service.smtp_configured(smtp), "hint": "Check SMTP_HOST/SMTP_PORT/SMTP_PASS if this fails."}
        ok_count = sum(1 for v in results.values() if isinstance(v, bool) and v or isinstance(v, dict) and v.get("ok"))
        return {"ok": True, "healthy": ok_count == len(results), "checks": results}

    def generate_training_deck(kind: str):
        try:
            data = training_service.build_deck(kind)
        except ImportError:
            return {"ok": False, "error": "python-pptx not installed. Add it to requirements and redeploy."}
        label = "Worker Orientation" if kind == "worker" else "Host & Lead Onboarding"
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO generated_documents (title, category, file_name, file_type, file_data) VALUES (%s, %s, %s, %s, %s) RETURNING id;",
                (label, "training", f"{kind}-orientation.pptx", "pptx", data),
            )
            doc_id = cur.fetchone()["id"]
            db.commit()
        return {"ok": True, "doc_id": doc_id, "title": label, "download_url": f"/docs/download/{doc_id}"}

    def get_rental_analysis(address: str):
        if not rental_analysis.is_configured():
            return {"ok": False, "error": "Rental analysis API not configured."}
        try:
            result = rental_analysis.analyze(address)
            if "error" in result:
                return {"ok": False, "error": result["error"]}
            summary = {
                "address": address,
                "home_value": result.get("home_value"),
                "suggested_monthly_rent": result.get("suggested_rent"),
                "fair_market_rent": result.get("fmr"),
                "airbnb_estimate": result.get("airbnb_estimate"),
                "analysis_link": result.get("analysis_link") or "",
            }
            return {"ok": True, "analysis": summary}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def list_documents(category: str = ""):
        with db.cursor() as cur:
            if category:
                cur.execute("SELECT id, title, category, file_name, file_type, created_at FROM generated_documents WHERE category = %s ORDER BY created_at DESC LIMIT 100;", (category,))
            else:
                cur.execute("SELECT id, title, category, file_name, file_type, created_at FROM generated_documents ORDER BY created_at DESC LIMIT 100;")
            rows = cur.fetchall()
        for r in rows:
            r["created_at"] = _fmt_dt(r["created_at"])
        return {"ok": True, "documents": rows}

    def list_funding_ready_leads():
        with db.cursor() as cur:
            cur.execute("""
                SELECT l.*, p.name AS partner_name FROM leads l
                LEFT JOIN partners p ON p.id = l.partner_id
                WHERE l.funding_needed = TRUE ORDER BY l.created_at DESC;
            """)
            rows = cur.fetchall()
        for r in rows:
            r["funding_amount_dollars"] = round((r.get("funding_amount_cents") or 0) / 100, 2)
        return {"ok": True, "leads": rows}

    return {
        "check_booking_availability": check_booking_availability,
        "create_booking": create_booking,
        "lookup_bookings": lookup_bookings,
        "register_customer": register_customer,
        "get_business_summary": get_business_summary,
        "list_upcoming_schedule": list_upcoming_schedule,
        "list_customers": list_customers,
        "list_leads": list_leads,
        "update_lead_status": update_lead_status,
        "add_host": add_host,
        "list_hosts": list_hosts,
        "add_worker": add_worker,
        "list_workers": list_workers,
        "update_worker": update_worker,
        "assign_worker_to_job": assign_worker_to_job,
        "list_worker_jobs": list_worker_jobs,
        "generate_paycheck": generate_paycheck,
        "list_paychecks": list_paychecks,
        "get_accounting_summary": get_accounting_summary,
        "add_ledger_entry": add_ledger_entry,
        "send_sms_message": send_sms_message,
        "send_email_message": send_email_message,
        "run_site_health_check": run_site_health_check,
        "generate_training_deck": generate_training_deck,
        "get_rental_analysis": get_rental_analysis,
        "list_documents": list_documents,
        "list_funding_ready_leads": list_funding_ready_leads,
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
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS channel_source VARCHAR(20);")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS channel_booking_id VARCHAR(64);")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS channel_status VARCHAR(30);")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS channel_guest_email VARCHAR(255);")
                cur.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS channel_external_ref VARCHAR(128);")
                cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_calendar_channel_booking
                ON calendar_events (channel_source, channel_booking_id)
                WHERE channel_source IS NOT NULL AND channel_booking_id IS NOT NULL;
                """)
                cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_calendar_external_ref
                ON calendar_events (channel_external_ref)
                WHERE channel_external_ref IS NOT NULL;
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_sync_logs (
                    id SERIAL PRIMARY KEY,
                    channel VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    summary TEXT,
                    details TEXT,
                    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    finished_at TIMESTAMP WITH TIME ZONE
                );
                """)
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
                cur.execute("ALTER TABLE properties ADD COLUMN IF NOT EXISTS channel_property_uuid VARCHAR(64);")
                cur.execute("ALTER TABLE properties ADD COLUMN IF NOT EXISTS turno_property_id VARCHAR(64);")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS devices (
                    id SERIAL PRIMARY KEY,
                    host_id INTEGER REFERENCES hosts(id),
                    property_id INTEGER REFERENCES properties(id),
                    kind VARCHAR(50) NOT NULL DEFAULT 'other',
                    custom_kind VARCHAR(100),
                    name VARCHAR(255) NOT NULL,
                    vendor VARCHAR(255),
                    model VARCHAR(255),
                    device_ref VARCHAR(255),
                    access_code VARCHAR(255),
                    access_instructions TEXT,
                    status VARCHAR(20) DEFAULT 'active',
                    meta_json TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_devices_host ON devices (host_id);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_devices_property ON devices (property_id);")
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
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS source VARCHAR(100) DEFAULT 'website';")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS referral_code VARCHAR(50);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS campaign VARCHAR(100);")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS lead_posts (
                    post_id VARCHAR(32) PRIMARY KEY,
                    subreddit VARCHAR(100),
                    created_utc BIGINT,
                    lead_id INTEGER,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
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
                cur.execute("ALTER TABLE workers ADD COLUMN IF NOT EXISTS magic_token VARCHAR(64);")
                cur.execute("ALTER TABLE workers ADD COLUMN IF NOT EXISTS magic_token_expires_at TIMESTAMP WITH TIME ZONE;")
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
                cur.execute("""
                CREATE TABLE IF NOT EXISTS custom_forms (
                    id SERIAL PRIMARY KEY,
                    title VARCHAR(255) NOT NULL,
                    category VARCHAR(50),
                    doc_def TEXT NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS generated_documents (
                    id SERIAL PRIMARY KEY,
                    title VARCHAR(255) NOT NULL,
                    category VARCHAR(50),
                    file_name VARCHAR(255) NOT NULL,
                    file_type VARCHAR(10) NOT NULL,
                    file_data BYTEA NOT NULL,
                    values_json TEXT,
                    sent_email VARCHAR(255),
                    sent_sms VARCHAR(50),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS ledger_entries (
                    id SERIAL PRIMARY KEY,
                    tx_type VARCHAR(20) NOT NULL,
                    ref_type VARCHAR(50),
                    ref_id INTEGER,
                    description TEXT,
                    amount_cents BIGINT NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_unique_ref ON ledger_entries (ref_type, ref_id);""")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS checks (
                    id SERIAL PRIMARY KEY,
                    check_number VARCHAR(20) NOT NULL,
                    paycheck_id INTEGER REFERENCES worker_paychecks(id),
                    payee VARCHAR(255) NOT NULL,
                    amount_cents BIGINT NOT NULL,
                    memo TEXT,
                    status VARCHAR(20) DEFAULT 'draft',
                    file_data BYTEA,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS login_otps (
                    id SERIAL PRIMARY KEY,
                    role VARCHAR(20) NOT NULL,
                    actor_id INTEGER,
                    email VARCHAR(255) NOT NULL,
                    code_hash VARCHAR(128) NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    consumed BOOLEAN DEFAULT FALSE,
                    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS funding_needed BOOLEAN DEFAULT FALSE;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS funding_amount_cents BIGINT;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS funding_use VARCHAR(500);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS partner_id INTEGER;")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS partners (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    kind VARCHAR(50) DEFAULT 'bank',
                    contact_email VARCHAR(255),
                    contact_phone VARCHAR(50),
                    website VARCHAR(500),
                    notes TEXT,
                    active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS referral_codes (
                    id SERIAL PRIMARY KEY,
                    code VARCHAR(50) NOT NULL UNIQUE,
                    ref_type VARCHAR(20) NOT NULL,
                    ref_id INTEGER,
                    label VARCHAR(255),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("ALTER TABLE partners ADD COLUMN IF NOT EXISTS referral_code VARCHAR(50);")
                cur.execute("SELECT id, name FROM partners WHERE referral_code IS NULL;")
                for prow in cur.fetchall():
                    new_code = _gen_referral_code()
                    cur.execute("INSERT INTO referral_codes (code, ref_type, ref_id, label) VALUES (%s, 'partner', %s, %s) ON CONFLICT (code) DO NOTHING;",
                                (new_code, prow["id"], prow["name"]))
                    cur.execute("UPDATE partners SET referral_code = %s WHERE id = %s AND referral_code IS NULL;",
                                (new_code, prow["id"]))
                cur.execute("""
                CREATE TABLE IF NOT EXISTS worker_quiz_results (
                    id SERIAL PRIMARY KEY,
                    worker_id INTEGER,
                    worker_name VARCHAR(255) NOT NULL,
                    email VARCHAR(255),
                    score INTEGER NOT NULL,
                    total INTEGER NOT NULL,
                    passed BOOLEAN NOT NULL,
                    answers_json TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS job_photos (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES calendar_events(id),
                    worker_id INTEGER REFERENCES workers(id),
                    room VARCHAR(120),
                    category VARCHAR(20) NOT NULL DEFAULT 'clean',
                    caption TEXT,
                    file_type VARCHAR(20),
                    file_data BYTEA,
                    verified BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_job_photos_event ON job_photos (event_id);")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    author_role VARCHAR(20) NOT NULL,
                    author_id INTEGER,
                    author_name VARCHAR(255),
                    audience VARCHAR(30) NOT NULL DEFAULT 'office',
                    body TEXT NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_created ON messages (created_at DESC);")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS active_sessions (
                    id SERIAL PRIMARY KEY,
                    role VARCHAR(20) NOT NULL,
                    actor_id INTEGER NOT NULL,
                    device_id VARCHAR(64) NOT NULL,
                    token_ref VARCHAR(64) NOT NULL,
                    ua VARCHAR(300) DEFAULT '',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    last_active_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    active BOOLEAN DEFAULT TRUE
                );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_active_sessions_actor ON active_sessions (role, actor_id);")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id SERIAL PRIMARY KEY,
                    kind VARCHAR(50) NOT NULL DEFAULT 'info',
                    severity VARCHAR(20) NOT NULL DEFAULT 'warning',
                    message TEXT NOT NULL,
                    details TEXT DEFAULT '',
                    read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                # ---- Unified multi-company schema (Broom Service + Buildstack Construction) ----
                # Tag every shared row with a company so one backend serves both brands.
                for _shared_tbl in ("calendar_events", "payments", "comms_logs", "users", "leads",
                                    "customers", "messages", "job_photos", "hosts", "workers"):
                    cur.execute(f'ALTER TABLE {_shared_tbl} ADD COLUMN IF NOT EXISTS company VARCHAR(20) NOT NULL DEFAULT \'broom\';')
                cur.execute("CREATE INDEX IF NOT EXISTS idx_leads_company ON leads (company);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_company ON payments (company);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_events_company ON calendar_events (company);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_company ON messages (company);")
                # Relax parent-only NOT NULLs so construction rows coexist (booking_id / event_id / email).
                cur.execute("ALTER TABLE payments ALTER COLUMN booking_id DROP NOT NULL;")
                cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS lead_id INTEGER REFERENCES leads(id);")
                cur.execute("ALTER TABLE calendar_events ALTER COLUMN customer_name DROP NOT NULL;")
                cur.execute("ALTER TABLE calendar_events ALTER COLUMN service_type DROP NOT NULL;")
                cur.execute("ALTER TABLE leads ALTER COLUMN email DROP NOT NULL;")
                # Construction lead columns ride on the same leads table.
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS project_type VARCHAR(120);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS address VARCHAR(255);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS budget VARCHAR(120);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS timeline VARCHAR(120);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS description TEXT;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS deposit_status VARCHAR(50) DEFAULT 'none';")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS deposit_cents INTEGER;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS stripe_session_id VARCHAR(255);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS notes TEXT;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS property_sqft INTEGER;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS estimate_low_cents INTEGER;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS estimate_high_cents INTEGER;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS estimate_json TEXT;")
                # Construction-only tables.
                cur.execute("""
                CREATE TABLE IF NOT EXISTS crew (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    phone VARCHAR(50) NOT NULL,
                    email VARCHAR(255),
                    role VARCHAR(50) DEFAULT 'crew',
                    pay_type VARCHAR(20) DEFAULT 'hourly',
                    pay_rate NUMERIC(10,2) DEFAULT 0,
                    pin_hash VARCHAR(255),
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    company VARCHAR(20) NOT NULL DEFAULT 'construction'
                );
                """)
                cur.execute("ALTER TABLE crew ADD COLUMN IF NOT EXISTS stripe_account_id VARCHAR(255);")
                cur.execute("ALTER TABLE crew ADD COLUMN IF NOT EXISTS bank_status VARCHAR(20) DEFAULT 'none';")
                cur.execute("ALTER TABLE crew ADD COLUMN IF NOT EXISTS worker_token VARCHAR(255);")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id SERIAL PRIMARY KEY,
                    lead_id INTEGER REFERENCES leads(id),
                    name VARCHAR(255) NOT NULL,
                    address VARCHAR(255),
                    summary TEXT,
                    start_date DATE,
                    end_date DATE,
                    status VARCHAR(50) DEFAULT 'planned',
                    notes TEXT,
                    company VARCHAR(20) NOT NULL DEFAULT 'construction',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_projects_company ON projects (company);")
                cur.execute("ALTER TABLE job_photos ALTER COLUMN event_id DROP NOT NULL;")
                cur.execute("ALTER TABLE job_photos ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id);")
                cur.execute("ALTER TABLE job_photos ADD COLUMN IF NOT EXISTS crew_id INTEGER REFERENCES crew(id);")
                cur.execute("ALTER TABLE job_photos ADD COLUMN IF NOT EXISTS is_punchlist BOOLEAN DEFAULT FALSE;")
                cur.execute("ALTER TABLE job_photos ADD COLUMN IF NOT EXISTS filename VARCHAR(500);")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS project_assignments (
                    id SERIAL PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    crew_id INTEGER NOT NULL REFERENCES crew(id) ON DELETE CASCADE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (project_id, crew_id)
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS clock_events (
                    id SERIAL PRIMARY KEY,
                    crew_id INTEGER NOT NULL REFERENCES crew(id),
                    project_id INTEGER REFERENCES projects(id),
                    punch_in_at TIMESTAMP WITH TIME ZONE,
                    punch_out_at TIMESTAMP WITH TIME ZONE,
                    punch_in_lat NUMERIC(9,6),
                    punch_in_lng NUMERIC(9,6),
                    punch_out_lat NUMERIC(9,6),
                    punch_out_lng NUMERIC(9,6),
                    work_type VARCHAR(20) DEFAULT 'regular',
                    notes TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS timesheets (
                    id SERIAL PRIMARY KEY,
                    crew_id INTEGER NOT NULL REFERENCES crew(id),
                    project_id INTEGER REFERENCES projects(id),
                    work_date DATE NOT NULL,
                    start_time VARCHAR(10),
                    end_time VARCHAR(10),
                    hours NUMERIC(6,2) NOT NULL,
                    work_type VARCHAR(20) DEFAULT 'regular',
                    source VARCHAR(20) DEFAULT 'manual',
                    status VARCHAR(20) DEFAULT 'submitted',
                    notes TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS payroll_runs (
                    id SERIAL PRIMARY KEY,
                    period_start DATE NOT NULL,
                    period_end DATE NOT NULL,
                    status VARCHAR(20) DEFAULT 'open',
                    note TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS payroll_lines (
                    id SERIAL PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES payroll_runs(id) ON DELETE CASCADE,
                    crew_id INTEGER NOT NULL REFERENCES crew(id),
                    hours NUMERIC(6,2) DEFAULT 0,
                    overtime_hours NUMERIC(6,2) DEFAULT 0,
                    gross_cents INTEGER DEFAULT 0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (run_id, crew_id)
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS job_leads (
                    id SERIAL PRIMARY KEY,
                    permit_number VARCHAR(255),
                    address VARCHAR(255),
                    city VARCHAR(120),
                    state VARCHAR(5),
                    work_type VARCHAR(120),
                    job_description TEXT,
                    contractor_name VARCHAR(255),
                    issue_date VARCHAR(40),
                    estimated_value NUMERIC(14,2),
                    owner_contact VARCHAR(50),
                    status VARCHAR(20) DEFAULT 'new',
                    source VARCHAR(50) DEFAULT 'permits',
                    is_demo BOOLEAN DEFAULT FALSE,
                    company VARCHAR(20) NOT NULL DEFAULT 'construction',
                    found_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                # Private messaging: kind ('dm'|'announcement'), recipient + scope.
                cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'announcement';")
                cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS recipient_role VARCHAR(20);")
                cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS recipient_id INTEGER;")
                cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS recipient_name VARCHAR(255);")
                cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS scope VARCHAR(20) NOT NULL DEFAULT 'all';")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_dm ON messages (kind, recipient_role, recipient_id);")
                conn.commit()
        print("🚀 Database connectivity and tables validated successfully.")
    except Exception as e:
        print(f"❌ Structural database connection failure: {e}")

    if not os.getenv("DISABLE_LOAN_OUTREACH"):
        try:
            from loan_outreach import start_outreach_tasks
            app.state.outreach_tasks = start_outreach_tasks()
        except Exception as e:
            print(f"⚠️ Loan outreach startup skipped: {e}")

    if not os.getenv("DISABLE_EMAIL_BOT"):
        try:
            from email_bot import start_email_bot
            start_email_bot()
        except Exception as e:
            print(f"⚠️ Email bot startup skipped: {e}")

    yield

app = FastAPI(lifespan=lifecycle, docs_url="/swagger", redoc_url="/redoc")
app.mount("/static", StaticFiles(directory="static"), name="static")


def resolve_company(request: Request) -> str:
    """Pick the company; this (Broom) service owns its own brand rows."""
    return os.getenv("COMPANY_KEY", "broom")


@app.middleware("http")
async def _company_middleware(request: Request, call_next):
    token = _company_var.set(resolve_company(request))
    try:
        return await call_next(request)
    finally:
        _company_var.reset(token)

def get_db():
    conn = psycopg.connect(db_url, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()

def current_actor(request: Request):
    """Return {role, id, email, name} for a signed-in session, or None."""
    return auth_service.actor_from_token(request.cookies.get(auth_service.SESSION_COOKIE))

def _worker_session_actor(request: Request) -> dict | None:
    """Return a worker actor authenticated via the crew app's worker_session cookie.

    The same cookie is used by both companies, so we check the Broom `workers`
    table first, then the construction `crew` table, stamping the company.
    """
    token = request.cookies.get("worker_session")
    worker_id = request.cookies.get("worker_id")
    if not token or not worker_id:
        return None
    conn = psycopg.connect(db_url, row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, email, company FROM workers WHERE id = %s AND is_active = TRUE AND worker_token = %s;",
                (int(worker_id), token),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "SELECT id, name, email, company FROM crew WHERE id = %s AND is_active = TRUE AND worker_token = %s;",
                    (int(worker_id), token),
                )
                row = cur.fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if not row:
        return None
    return {"role": "worker", "id": row["id"], "email": row.get("email"), "name": row.get("name"),
            "company": row.get("company") or "broom"}

def _session_actor(request: Request) -> dict | None:
    """The signed-in session actor, or a worker authenticated via the crew app."""
    return current_actor(request) or _worker_session_actor(request)

def require_auth(request: Request):
    actor = current_actor(request)
    is_admin = bool(actor and actor.get("role") == "admin")
    return is_admin, (actor.get("email") if actor else request.cookies.get("user_email"))

def require_admin(request: Request) -> None:
    if not require_auth(request)[0]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin login required")

DEVICE_COOKIE = "device_id"

def _get_device_id(request: Request, resp) -> str:
    did = request.cookies.get(DEVICE_COOKIE)
    if did and len(did) <= 64:
        return did
    did = secrets.token_urlsafe(16)
    resp.set_cookie(key=DEVICE_COOKIE, value=did, httponly=True, samesite="lax",
                    secure=_secure_cookies(), max_age=365 * 24 * 3600)
    return did

def _token_ref(sid: str) -> str:
    return hashlib.sha256(("bzsid|" + (sid or "")).encode()).hexdigest()

def _register_device_session(db, actor: dict, token: str, request: Request, resp) -> tuple:
    """Record the active device for an actor's new session.

    Returns (new_device: bool, new_device_id, old_device_id) where
    new_device is True when this login came from a device different
    from the previously active one."""
    sid = auth_service.session_id_from_token(token)
    if not sid:
        return False, "", ""
    device_id = _get_device_id(request, resp)
    with db.cursor() as cur:
        cur.execute(
            "SELECT device_id FROM active_sessions WHERE role = %s AND actor_id = %s AND active = TRUE ORDER BY created_at DESC LIMIT 1;",
            (actor["role"], actor.get("id")),
        )
        prior = cur.fetchone()
        old_device_id = prior["device_id"] if prior else ""
        new_device = bool(prior and prior["device_id"] != device_id)
        cur.execute("UPDATE active_sessions SET active = FALSE WHERE role = %s AND actor_id = %s;", (actor["role"], actor.get("id")))
        cur.execute(
            "INSERT INTO active_sessions (role, actor_id, device_id, token_ref, ua) VALUES (%s, %s, %s, %s, %s);",
            (actor["role"], actor.get("id"), device_id, _token_ref(sid), (request.headers.get("user-agent") or "")[:300]),
        )
        db.commit()
    return new_device, device_id, old_device_id

def _device_session_ok(db, actor: dict, request: Request) -> bool:
    token = request.cookies.get(auth_service.SESSION_COOKIE)
    sid = auth_service.session_id_from_token(token) if token else None
    if not sid:
        return False
    with db.cursor() as cur:
        cur.execute(
            "SELECT id FROM active_sessions WHERE role = %s AND actor_id = %s AND active = TRUE AND token_ref = %s;",
            (actor["role"], actor.get("id"), _token_ref(sid)),
        )
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE active_sessions SET last_active_at = NOW() WHERE id = %s;", (row["id"],))
            db.commit()
    return bool(row)

def _log_alert(db, kind: str, severity: str, message: str, details: str = "") -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO alerts (kind, severity, message, details) VALUES (%s, %s, %s, %s) RETURNING id;",
            (kind, severity, message, details[:2000]),
        )
        alert_id = cur.fetchone()["id"]
        db.commit()
    return alert_id

def _is_private_egress_ip(host: str) -> bool:
    """Skip new-device alerts for private/egress IPs (loopback, RFC1918, CGNAT 100.64.0.0/10)."""
    if not host:
        return True
    try:
        parts = host.split(".")
        if len(parts) != 4:
            return False
        a, b = int(parts[0]), int(parts[1])
        if a == 100 and 64 <= b <= 127:
            return True
        if a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or a == 127 or a == 0:
            return True
    except Exception:
        return False
    return False

async def _notify_new_device(actor: dict, new_device_id: str, old_device_id: str, request: Request):
    """Record + push a new-device login alert to the admin console, email, and SMS."""
    ip = request.client.host if request.client else ""
    if _is_private_egress_ip(ip):
        print(f"[SECURITY] new-device alert suppressed for private/egress IP {ip or '(none)'}")
        return
    conn = psycopg.connect(db_url, row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alerts (kind, severity, message, details) VALUES ('new_device_login', 'warning', %s, %s) RETURNING id;",
                (
                    f"New device signed in to {actor.get('role')} account {actor.get('name') or actor.get('email') or '?'}",
                    json.dumps({
                        "role": actor.get("role"), "name": actor.get("name"), "email": actor.get("email"),
                        "new_device": new_device_id, "previous_device": old_device_id,
                        "ip": ip,
                        "ua": (request.headers.get("user-agent") or "")[:300],
                        "time": datetime.now(APP_TZ).isoformat(),
                    }),
                ),
            )
            conn.commit()

        to_email = (os.getenv("ADMIN_ALERT_EMAIL") or os.getenv("ADMIN_OTP_EMAIL") or os.getenv("ADMIN_EMAIL") or "").strip()
        to_phone = (os.getenv("ADMIN_ALERT_PHONE") or "").strip()
        subject = "⚠️ New device signed in — Broom Service"
        msg_text = (
            f"A new device just signed in to the {actor.get('role')} account "
            f"{actor.get('name') or actor.get('email') or '?'}.\n\n"
            f"Role: {actor.get('role')}\nEmail: {actor.get('email')}\n"
            f"Device ID: {new_device_id}\nPrevious device: {old_device_id or 'none'}\n"
            f"IP: {ip or 'unknown'}\n"
            f"Time: {datetime.now(APP_TZ).strftime('%b %d, %Y %I:%M %p %Z')}\n\n"
            f"If this wasn't you, change the login credentials and review active sessions."
        )
        if to_email:
            cfg = documents_service.smtp_config_from_env()
            if documents_service.smtp_configured(cfg):
                try:
                    await documents_service.send_email(cfg, to_email, subject, msg_text)
                except Exception as e:
                    print(f"[ALERT] email failed: {e}")
        if to_phone:
            await asyncio.to_thread(signalwire.send_sms, to_phone, ("[ALERT] " + msg_text)[:1600])
    finally:
        conn.close()

def _features(db, role: str) -> dict:
    return auth_service.features_for(role, _get_setting(db, f"perms_{role}", ""))

def _feature_enabled(db, role: str, feature: str) -> bool:
    return bool(_features(db, role).get(feature))

def _require_feature(db, role: str, feature: str):
    if not _feature_enabled(db, role, feature):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This tool is turned off for your account")

def _template_actor(request: Request):
    return current_actor(request)

def _template_features(request: Request):
    actor = current_actor(request)
    if not actor or actor.get("role") not in ("worker", "host"):
        return {}
    try:
        conn = psycopg.connect(db_url, row_factory=dict_row)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_settings WHERE key = %s;", (f"perms_{actor['role']}",))
                row = cur.fetchone()
            return auth_service.features_for(actor["role"], row["value"] if row else "")
        finally:
            conn.close()
    except Exception:
        return auth_service.permissions_defaults(actor["role"])

templates.env.globals["current_actor"] = _template_actor
templates.env.globals["current_features"] = _template_features
templates.env.globals["site_theme_state"] = lambda: site_theme.state()
templates.env.globals["promo_code"] = lambda: "FIRSTCLEAN"

for _env in (templates.env,):
    _env.globals["company"] = company
    _env.globals["map_embed"] = _map_embed
    _env.globals["map_directions"] = _map_directions

# --- PUBLIC LANDING & AUTH ---

@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})

@app.get("/health")
async def health_check():
    return {"status": "ok"}

def _site_base(request: Request) -> str:
    return os.getenv("APP_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")

@app.get("/robots.txt", response_class=Response)
async def robots_txt(request: Request):
    base = _site_base(request)
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /dashboard\n"
        "Disallow: /portfolio\n"
        "Disallow: /hosts\n"
        "Disallow: /crew\n"
        "Disallow: /comms\n"
        "Disallow: /settings\n"
        "Disallow: /copilot\n"
        "Disallow: /training\n"
        "Disallow: /partners\n"
        "Disallow: /accounting\n"
        "Disallow: /worker\n"
        "Disallow: /devices\n"
        "\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain")

@app.get("/sitemap.xml", response_class=Response)
async def sitemap_xml(request: Request):
    base = _site_base(request)
    today = date.today().isoformat()
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{base}/</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>1.0</priority></url>\n"
        f"  <url><loc>{base}/book</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.8</priority></url>\n"
        f"  <url><loc>{base}/host-login</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.3</priority></url>\n"
        f"  <url><loc>{base}/login</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.2</priority></url>\n"
        "</urlset>\n"
    )
    return Response(content=body, media_type="application/xml")

@app.get("/login", response_class=HTMLResponse)
async def read_login(request: Request):
    actor = current_actor(request)
    if actor:
        return RedirectResponse(url=_login_redirect(actor.get("role")), status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="login.html", context={
        "error": request.query_params.get("error"),
    })

def _secure_cookies() -> bool:
    return os.getenv("COOKIE_SECURE", "false").lower() == "true"

def _login_redirect(role: str) -> str:
    return {"admin": "/dashboard", "worker": "/worker", "host": "/host"}.get(role or "", "/")

def _otp_global(db) -> bool:
    env = os.getenv("OTP_ENABLED", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    return (_get_setting(db, "otp_enabled", "true").strip().lower() not in ("0", "false", "no", "off"))

def _otp_role_enabled(db, role: str) -> bool:
    """Whether an email code is switched on for this role (ignores delivery)."""
    raw = _get_setting(db, f"otp_role_{role}", "").strip().lower()
    if raw:
        return raw not in ("0", "false", "no", "off")
    return role == "admin"

def _otp_enabled(db, role: str = "admin") -> bool:
    """Whether a one-time code is actually required for this role right now.

    OTP is only enforced when it is switched on *and* email delivery is
    configured. Without SMTP the code can never arrive, so requiring one would
    lock every user out; in that case we fall back to the password/PIN step
    alone. Workers and hosts default to no OTP so their sign-in stays quick,
    while admins default to OTP.
    """
    if not _otp_global(db) or not _otp_role_enabled(db, role):
        return False
    return documents_service.smtp_configured(_smtp_cfg(db))

def _resolve_credentials(db, identifier: str, secret: str):
    ident = (identifier or "").strip()
    secret = (secret or "").strip()
    admin_email = os.getenv("ADMIN_EMAIL", "shaun@example.com")
    admin_password = os.getenv("ADMIN_PASSWORD", "password123")
    if ident.lower() == admin_email.lower() and secret == admin_password:
        return {
            "role": "admin", "id": 0,
            "email": (os.getenv("ADMIN_OTP_EMAIL") or admin_email),
            "name": os.getenv("ADMIN_NAME", "Owner"),
        }
    if "@" in ident:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM hosts WHERE email ILIKE %s AND status = 'active' AND password_hash IS NOT NULL;", (ident,))
            host = cur.fetchone()
        if host and host["password_hash"] == _hash_password(secret):
            return {"role": "host", "id": host["id"], "email": host["email"], "name": host["name"]}
    digits = _digits(ident)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM workers WHERE is_active = TRUE;")
        candidates = cur.fetchall()
    for w in candidates:
        email_match = w.get("email") and w["email"].lower() == ident.lower()
        phone = _digits(w.get("phone"))
        phone_match = bool(digits and phone and phone[-10:] == digits[-10:])
        if (email_match or phone_match) and w["pin_hash"] and w["pin_hash"] == _hash_pin(secret):
            return {"role": "worker", "id": w["id"], "email": w["email"] or "", "name": w["name"]}
    return None

def _store_otp(db, role: str, actor_id, email: str, code: str):
    with db.cursor() as cur:
        cur.execute("UPDATE login_otps SET consumed = TRUE WHERE role = %s AND actor_id IS NOT DISTINCT FROM %s AND consumed = FALSE;", (role, actor_id))
        cur.execute(
            "INSERT INTO login_otps (role, actor_id, email, code_hash, expires_at) VALUES (%s, %s, %s, %s, NOW() + INTERVAL '10 minutes');",
            (role, actor_id, email, auth_service.hash_otp(code)),
        )
        db.commit()

async def _deliver_otp(db, email: str, code: str, actor: dict):
    cfg = _smtp_cfg(db)
    if documents_service.smtp_configured(cfg):
        body = (
            f"Hi {actor.get('name') or 'there'},\n\n"
            f"Your Broom Service verification code is:\n\n    {code}\n\n"
            f"It expires in 10 minutes. If you didn't try to sign in, you can ignore this email.\n\n"
            f"— Broom Service"
        )
        try:
            await documents_service.send_email(cfg, email, "Your Broom Service login code", body)
            return True, ""
        except Exception as e:
            return False, f"Could not send email: {e}"
    print(f"[OTP] SMTP not configured — code for {email}: {code}")
    return False, "Email delivery is not configured yet. Ask the office to set up SMTP on the Documents page."

def _finish_login(actor: dict, request: Request | None = None) -> RedirectResponse:
    token = auth_service.issue_session(actor["role"], actor.get("id"), actor.get("email"), actor.get("name"))
    resp = RedirectResponse(url=_login_redirect(actor["role"]), status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(key=auth_service.SESSION_COOKIE, value=token, httponly=True, samesite="lax", secure=_secure_cookies())
    resp.set_cookie(key="user_email", value=actor.get("email") or "", httponly=True, samesite="lax")
    resp.set_cookie(key="user_name", value=actor.get("name") or "", httponly=True, samesite="lax")
    resp.set_cookie(key="role", value=actor["role"], httponly=True, samesite="lax")
    resp.set_cookie(key="actor_id", value=str(actor.get("id")), httponly=True, samesite="lax")
    resp.delete_cookie(auth_service.OTP_COOKIE)
    if request is not None:
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            new_device, new_device_id, old_device_id = _register_device_session(conn, actor, token, request, resp)
        if new_device:
            try:
                asyncio.create_task(_notify_new_device(actor, new_device_id, old_device_id, request))
            except Exception as e:
                print(f"[SECURITY] could not raise new-device alert: {e}")
    return resp

@app.post("/api/auth/login")
async def api_login(identifier: str = Form(...), secret: str = Form(...), request: Request = None, db=Depends(get_db)):
    actor = _resolve_credentials(db, identifier, secret)
    if not actor:
        return RedirectResponse(url="/login?error=Invalid+email%2Fphone+or+password%2FPIN", status_code=status.HTTP_303_SEE_OTHER)
    email = (actor.get("email") or "").strip()
    if _otp_enabled(db, actor["role"]) and not email:
        return RedirectResponse(url="/login?error=No+email+on+file+%E2%80%94+ask+the+office+to+add+one", status_code=status.HTTP_303_SEE_OTHER)

    if not _otp_enabled(db, actor["role"]):
        return _finish_login(actor, request)

    code = auth_service.generate_otp()
    _store_otp(db, actor["role"], actor.get("id"), email, code)
    ok, err = await _deliver_otp(db, email, code, actor)
    if not ok:
        return RedirectResponse(url=f"/login?error={urllib.parse.quote(err)}", status_code=status.HTTP_303_SEE_OTHER)
    resp = RedirectResponse(url="/login/otp", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        key=auth_service.OTP_COOKIE,
        value=auth_service.issue_pending(actor["role"], actor.get("id"), email, actor.get("name")),
        httponly=True, samesite="lax", secure=_secure_cookies(), max_age=auth_service.OTP_TTL_SECONDS,
    )
    return resp

@app.get("/login/otp", response_class=HTMLResponse)
async def read_otp(request: Request):
    pending = auth_service.pending_from_token(request.cookies.get(auth_service.OTP_COOKIE))
    if not pending:
        return RedirectResponse(url="/login?error=Verification+expired+%E2%80%94+please+sign+in+again", status_code=status.HTTP_303_SEE_OTHER)
    masked = _mask_email(pending.get("email") or "")
    return templates.TemplateResponse(request=request, name="otp.html", context={
        "error": request.query_params.get("error"),
        "resent": request.query_params.get("resent"),
        "email": pending.get("email"),
        "masked_email": masked,
        "name": pending.get("name"),
    })

@app.post("/api/auth/verify-otp")
async def api_verify_otp(request: Request, code: str = Form(...), db=Depends(get_db)):
    pending = auth_service.pending_from_token(request.cookies.get(auth_service.OTP_COOKIE))
    if not pending:
        return RedirectResponse(url="/login?error=Verification+expired+%E2%80%94+please+sign+in+again", status_code=status.HTTP_303_SEE_OTHER)
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM login_otps WHERE role = %s AND actor_id IS NOT DISTINCT FROM %s AND consumed = FALSE ORDER BY created_at DESC LIMIT 1;",
            (pending["role"], pending.get("id")),
        )
        row = cur.fetchone()
    if not row or (row["expires_at"] and row["expires_at"] < datetime.now(APP_TZ)):
        return RedirectResponse(url="/login/otp?error=Code+expired+%E2%80%94+resend+below", status_code=status.HTTP_303_SEE_OTHER)
    if row["attempts"] >= auth_service.MAX_OTP_ATTEMPTS:
        return RedirectResponse(url="/login?error=Too+many+attempts+%E2%80%94+please+sign+in+again", status_code=status.HTTP_303_SEE_OTHER)
    if not auth_service.verify_otp((code or "").strip(), row["code_hash"]):
        with db.cursor() as cur:
            cur.execute("UPDATE login_otps SET attempts = attempts + 1 WHERE id = %s;", (row["id"],))
            db.commit()
        return RedirectResponse(url="/login/otp?error=Incorrect+code", status_code=status.HTTP_303_SEE_OTHER)
    with db.cursor() as cur:
        cur.execute("UPDATE login_otps SET consumed = TRUE WHERE id = %s;", (row["id"],))
        db.commit()
    return _finish_login({
        "role": pending["role"], "id": pending.get("id"),
        "email": pending.get("email"), "name": pending.get("name"),
    }, request)

@app.post("/api/auth/resend-otp")
async def api_resend_otp(request: Request, db=Depends(get_db)):
    pending = auth_service.pending_from_token(request.cookies.get(auth_service.OTP_COOKIE))
    if not pending:
        return RedirectResponse(url="/login?error=Verification+expired+%E2%80%94+please+sign+in+again", status_code=status.HTTP_303_SEE_OTHER)
    code = auth_service.generate_otp()
    _store_otp(db, pending["role"], pending.get("id"), pending.get("email"), code)
    ok, err = await _deliver_otp(db, pending.get("email"), code, {"name": pending.get("name")})
    if not ok:
        return RedirectResponse(url=f"/login?error={urllib.parse.quote(err)}", status_code=status.HTTP_303_SEE_OTHER)
    resp = RedirectResponse(url="/login/otp?resent=1", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        key=auth_service.OTP_COOKIE,
        value=auth_service.issue_pending(pending["role"], pending.get("id"), pending.get("email"), pending.get("name")),
        httponly=True, samesite="lax", secure=_secure_cookies(), max_age=auth_service.OTP_TTL_SECONDS,
    )
    return resp

def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email or ""
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        shown = name[:1] + "*"
    else:
        shown = name[0] + "*" * (len(name) - 2) + name[-1]
    return f"{shown}@{domain}"

@app.get("/api/auth/logout")
async def api_logout():
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(auth_service.SESSION_COOKIE)
    response.delete_cookie("user_email")
    response.delete_cookie("user_name")
    response.delete_cookie("role")
    response.delete_cookie("actor_id")
    response.delete_cookie("host_session")
    response.delete_cookie("host_id")
    response.delete_cookie("worker_session")
    response.delete_cookie("worker_id")
    return response

# --- LEAD CAPTURE (LANDING PAGE FORM) ---

@app.post("/submit-lead")
async def submit_lead(
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    url: str = Form(""),
    funding_needed: str = Form("off"),
    funding_use: str = Form(""),
    source: str = Form(""),
    ref: str = Form(""),
    campaign: str = Form(""),
    db=Depends(get_db)
):
    result = rental_analysis.analyze(url) if url else {"ok": False, "error": "No property address provided."}
    analysis_json = json.dumps(result)
    status_value = "analyzed" if result.get("ok") else "new"
    zip_code = result.get("zip", "")
    needed = funding_needed.lower() in ("on", "true", "1", "yes")

    src = (source or "").strip().lower() or "website"
    ref_code = (ref or "").strip().lower()
    campaign_name = (campaign or "").strip().lower()[:100]

    partner_id = None
    with db.cursor() as cur:
        if ref_code:
            cur.execute("SELECT id FROM partners WHERE referral_code = %s;", (ref_code,))
            prow = cur.fetchone()
            if prow:
                partner_id = prow["id"]
            else:
                cur.execute("SELECT id FROM referral_codes WHERE code = %s;", (ref_code,))
                if not cur.fetchone():
                    ref_code = ""
            if ref_code:
                if src in ("", "website"):
                    src = "referral"
        elif src in ("", "website"):
            src = "website"

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (name, email, phone, listing_url, status, zip, analysis_json, funding_needed, funding_use, source, referral_code, partner_id, campaign) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULLIF(%s,''), %s, NULLIF(%s,''), %s, NULLIF(%s,'')) RETURNING id",
            (name, email, phone, url, status_value, zip_code, analysis_json, needed, funding_use, src, ref_code, partner_id, campaign_name)
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

# --- Public financing application (both companies share one page) -----------
@app.get("/finance", response_class=HTMLResponse)
async def finance_page(request: Request):
    return templates.TemplateResponse(request=request, name="finance.html", context={})


@app.post("/finance")
async def finance_apply(
    request: Request,
    full_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    service_type: str = Form(""),
    project_type: str = Form(""),
    address: str = Form(""),
    amount_needed: str = Form(""),
    how_soon: str = Form(""),
    when_start: str = Form(""),
    message: str = Form(""),
    ref: str = Form(""),
    db=Depends(get_db),
):
    name = (full_name or "").strip()
    phone = (phone or "").strip()
    email = (email or "").strip()
    if not name or not phone:
        return RedirectResponse(url="/finance?error=Please+add+your+name+and+phone", status_code=status.HTTP_303_SEE_OTHER)
    if not email:
        return RedirectResponse(url="/finance?error=Please+add+your+email+%E2%80%94+we%27ll+send+your+quote+there", status_code=status.HTTP_303_SEE_OTHER)
    company_key = resolve_company(request)
    budget = (amount_needed or "").strip()
    timeline = (when_start or how_soon or "").strip()
    use_note = (message or "").strip()[:2000]
    ref_code = (ref or request.query_params.get("ref") or "").strip().lower()[:50]

    partner_id = None
    if ref_code:
        with db.cursor() as cur:
            cur.execute("SELECT id FROM partners WHERE referral_code = %s;", (ref_code,))
            prow = cur.fetchone()
            if prow:
                partner_id = prow["id"]
            else:
                cur.execute("SELECT id FROM referral_codes WHERE code = %s;", (ref_code,))
                if not cur.fetchone():
                    ref_code = ""

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (name, email, phone, project_type, address, budget, timeline, description, "
            "source, status, funding_needed, funding_use, referral_code, partner_id, campaign, company) "
            "VALUES (%s, %s, %s, NULLIF(%s,''), NULLIF(%s,''), NULLIF(%s,''), NULLIF(%s,''), %s, "
            "'finance', 'new', TRUE, %s, NULLIF(%s,''), %s, NULLIF(%s,''), %s) RETURNING id;",
(name, (email or "").strip(), phone,
             "", address, budget, timeline, use_note,
             use_note, ref_code, partner_id, "finance-link", company_key),
        )
        lead_id = cur.fetchone()["id"]
        db.commit()

    summary = (
        f"💰 New financing application (#{lead_id}) — {name}, {phone}"
        + (f", {email.strip()}" if (email or "").strip() else "")
        + (f"\nNeeds: {service_type or project_type}" if (service_type or project_type) else "")
        + (f"\nAmount: ${budget}" if budget else "")
        + (f"\nWhen: {timeline}" if timeline else "")
        + (f"\n{use_note[:200]}" if use_note else "")
        + (f"\nRef: {ref_code}" if ref_code else "")
    )
    owner_phone = _company_config("broom")["phone_e164"]
    try:
        if os.getenv("OWNER_SMS_ENABLED", "0").lower() in ("1", "true", "yes") and signalwire.is_configured():
            signalwire.send_sms(owner_phone, summary[:1500])
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                    "VALUES (%s, %s, %s, %s, %s);",
                    ("outbound", "sms", "system", owner_phone, summary),
                )
                db.commit()
    except Exception as e:
        print(f"⚠️ finance owner SMS failed: {e}", flush=True)

    try:
        cfg = documents_service.smtp_config_from_env()
        if documents_service.smtp_configured(cfg):
            documents_service.send_email(cfg, _company_config("broom")["email"],
                f"New financing application #{lead_id}", summary)
    except Exception as e:
        print(f"⚠️ finance owner email failed: {e}", flush=True)

    try:
        auto_reply.auto_reply_to_lead(
            db, company_key, name=name, phone=phone, email=(email or "").strip(),
            service=(service_type or project_type or ""),
            address=(address or ""), budget=(budget or ""), timeline=(timeline or ""),
            message=(use_note or ""), source="finance", funding=True,
            sqft=0, lead_id=lead_id,
        )
    except Exception as e:
        print(f"⚠️ finance auto-reply failed: {e}", flush=True)

    return RedirectResponse(url="/finance?sent=1", status_code=status.HTTP_303_SEE_OTHER)

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

# --- PUBLIC BOOKING ---

@app.get("/book", response_class=HTMLResponse)
async def public_book_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="book.html",
        context={
            "today": date.today().isoformat(),
            "error": "",
            "payment_link": "",
            "customer_name": "",
            "phone": "",
            "email": "",
            "service_type": "Turnover Cleaning",
            "date": "",
            "time": "",
            "job_address": "",
        },
    )

@app.post("/book", response_class=HTMLResponse)
async def public_book_submit(
    request: Request,
    customer_name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    service_type: str = Form(...),
    date: str = Form(...),
    time: str = Form(...),
    job_address: str = Form(""),
    db=Depends(get_db),
):
    ctx = {
        "today": date.today().isoformat(),
        "error": "",
        "payment_link": "",
        "customer_name": customer_name,
        "phone": phone,
        "email": email,
        "service_type": service_type,
        "date": date,
        "time": time,
        "job_address": job_address,
        "service_name": service_type,
    }
    try:
        parsed_start = datetime.fromisoformat(f"{date}T{time}")
    except ValueError:
        ctx["error"] = "Please pick a valid date and time."
        return templates.TemplateResponse(request=request, name="book.html", context=ctx)

    duration_min = {
        "Turnover Cleaning": 150,
        "Deep Cleaning": 240,
        "Linen Restock": 60,
        "Inspection": 60,
    }.get(service_type, 60)
    parsed_end = parsed_start + timedelta(minutes=duration_min)

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM calendar_events WHERE start_time < %s AND end_time > %s;", (parsed_end, parsed_start))
        if cur.fetchone()["count"] > 0:
            ctx["error"] = "That time just got taken — pick another slot and try again."
            return templates.TemplateResponse(request=request, name="book.html", context=ctx)
        cur.execute(
            "INSERT INTO calendar_events (customer_name, phone, start_time, end_time, service_type, job_address) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;",
            (customer_name.strip(), phone.strip(), parsed_start, parsed_end, service_type, job_address.strip() or None),
        )
        event_id = cur.fetchone()["id"]
        amount_cents = stripe_svc.get_price(service_type)
        cur.execute("UPDATE calendar_events SET amount_cents = %s WHERE id = %s;", (amount_cents, event_id))
        db.commit()

    ctx["booking_date"] = parsed_start.strftime("%B %d, %Y")
    ctx["booking_time"] = parsed_start.strftime("%I:%M %p")
    ctx["amount_usd"] = f"{amount_cents / 100:.2f}"
    try:
        ctx["payment_link"] = stripe_svc.create_checkout_session(
            event_id, customer_name, email, service_type, parsed_start,
            discount_coupon=_first_clean_coupon(db, event_id, customer_name.strip(), phone.strip()),
        )
    except Exception:
        ctx["payment_link"] = ""
        ctx["payment_note"] = "Payment link could not be generated right now — we'll text you a secure checkout link shortly."
    return templates.TemplateResponse(request=request, name="book.html", context=ctx)

# --- HOST LEAD RADAR ---

_radar_jobs: dict = {}
_radar_jobs_lock = threading.Lock()


def _radar_status(source: str) -> dict:
    with _radar_jobs_lock:
        return dict(_radar_jobs.get(source, {}))


def _run_radar_scan(source: str, scan_fn):
    """Run a radar scan in a background thread. Returns True if a scan was started,
    False if one is already running for this source."""
    with _radar_jobs_lock:
        if _radar_jobs.get(source, {}).get("running"):
            return False
        _radar_jobs[source] = {"running": True, "started_at": datetime.utcnow(), "created": 0, "seen": 0, "errors": ""}
    threading.Thread(target=_radar_worker, args=(source, scan_fn), daemon=True).start()
    return True


def _radar_worker(source: str, scan_fn):
    """Scan + ingest in a background thread so user-facing requests stay fast
    (Cloudflare drops requests that run past ~100s)."""
    created = 0
    seen = 0
    errors = []
    try:
        result = scan_fn()
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                for m in result.get("matches", []):
                    cur.execute("SELECT 1 FROM lead_posts WHERE post_id = %s;", (m["post_id"],))
                    if cur.fetchone():
                        seen += 1
                        continue
                    meta = json.dumps({"title": m["title"], "subreddit": m["subreddit"]})
                    cur.execute(
                        "INSERT INTO leads (name, email, listing_url, status, source, funding_needed, funding_use, referral_code, campaign, analysis_json) "
                        "VALUES (%s, %s, %s, 'new', %s, %s, %s, %s, 'host-lead-radar', %s) RETURNING id;",
                        (
                            f"{source.title()} · {m['author']}",
                            f"{source}-{m['post_id']}@lead.local",
                            m["permalink"],
                            source,
                            m["funding"],
                            (m.get("funding_use") or "")[:500],
                            m["post_id"],
                            meta,
                        ),
                    )
                    lead_id = cur.fetchone()["id"]
                    cur.execute(
                        "INSERT INTO lead_posts (post_id, subreddit, created_utc, lead_id) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (post_id) DO NOTHING;",
                        (m["post_id"], m["subreddit"], m["created_utc"], lead_id),
                    )
                    created += 1
                conn.commit()
        errors = list(result.get("errors", []))
    except Exception as exc:
        errors.append(f"scan failed: {exc}"[:280])
    with _radar_jobs_lock:
        _radar_jobs[source] = {
            "running": False,
            "created": created,
            "seen": seen,
            "errors": "; ".join(errors)[:300],
        }

@app.get("/radar", response_class=HTMLResponse)
async def host_lead_radar(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM leads WHERE source IN ('reddit', 'linkedin') ORDER BY created_at DESC LIMIT 200;")
        rows = cur.fetchall()
    leads = []
    for l in rows:
        meta = {}
        try:
            meta = json.loads(l.get("analysis_json") or "{}")
        except (TypeError, ValueError):
            pass
        source = l.get("source") or "reddit"
        author = (l.get("name") or "").split("·", 1)[-1].strip() or f"{source} user"
        mins = None
        try:
            mins = int((datetime.utcnow() - l["created_at"]).total_seconds() // 60)
        except Exception:
            pass
        draft_fn = outreach_draft if source == "reddit" else linkedin_outreach_draft
        leads.append({
            "id": l["id"],
            "author": author,
            "subreddit": meta.get("subreddit", ""),
            "title": meta.get("title", ""),
            "permalink": l.get("listing_url", ""),
            "funding": bool(l.get("funding_needed")),
            "minutes_ago": mins,
            "status": l.get("status", "new"),
            "source": source,
            "draft": draft_fn({
                "author": author,
                "subreddit": meta.get("subreddit", ""),
                "title": meta.get("title", ""),
                "funding": bool(l.get("funding_needed")),
            }),
        })
    return templates.TemplateResponse(
        request=request,
        name="radar.html",
        context={
            "user": {"email": user_email},
            "leads": leads,
            "scanned": request.query_params.get("scanned"),
            "seen": request.query_params.get("seen"),
            "errors": request.query_params.get("errors", ""),
            "scan_started": request.query_params.get("scan_started"),
            "scan_running": request.query_params.get("scan_running"),
            "radar_status": {
                "reddit": _radar_status("reddit"),
                "linkedin": _radar_status("linkedin"),
            },
        },
    )

@app.post("/radar/scan")
async def radar_run_scan(request: Request):
    return await _radar_start(request, source="reddit", scan_fn=scan_reddit)


@app.post("/radar/scan/linkedin")
async def radar_run_linkedin_scan(request: Request):
    return await _radar_start(request, source="linkedin", scan_fn=scan_linkedin)


async def _radar_start(request, source, scan_fn):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    if not _run_radar_scan(source, scan_fn):
        return RedirectResponse(url=f"/radar?scan_running={source}", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url=f"/radar?scan_started={source}", status_code=status.HTTP_303_SEE_OTHER)

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

        cur.execute("""
            SELECT h.id, h.name, h.property_name, h.property_address, h.email, h.phone,
                   COALESCE(COUNT(DISTINCT p.id)::int, 0) AS property_count
            FROM hosts h
            LEFT JOIN properties p ON p.host_id = h.id
            GROUP BY h.id
            ORDER BY h.created_at DESC;
        """)
        hosts = cur.fetchall()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": {"email": user_email},
            "stats": {"hosts": hosts_count, "bookings": bookings_count, "customers": customers_count, "leads": leads_count},
            "events": events,
            "hosts": hosts,
        }
    )

# --- HOSTS MANAGEMENT ---

@app.get("/hosts", response_class=HTMLResponse)
async def hosts_page(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    source_filter = (request.query_params.get("source") or "").strip().lower()

    with db.cursor() as cur:
        if source_filter:
            cur.execute("SELECT * FROM leads WHERE LOWER(COALESCE(source, 'website')) = %s ORDER BY created_at DESC LIMIT 100", (source_filter,))
        else:
            cur.execute("SELECT * FROM leads ORDER BY created_at DESC LIMIT 100")
        leads = cur.fetchall()

        cur.execute("SELECT COALESCE(NULLIF(source, ''), 'website') AS channel, COUNT(*)::int AS cnt, COUNT(*) FILTER (WHERE status = 'host')::int AS converted FROM leads GROUP BY channel ORDER BY cnt DESC;")
        channel_stats = cur.fetchall()
        cur.execute("SELECT COUNT(*)::int AS total, COUNT(*) FILTER (WHERE status = 'host')::int AS converted FROM leads;")
        lead_totals = cur.fetchone()

        cur.execute("""
            SELECT r.*, COUNT(l.id)::int AS led_count,
                   COUNT(l.id) FILTER (WHERE l.status = 'host')::int AS converted
            FROM referral_codes r
            LEFT JOIN leads l ON l.referral_code = r.code
            GROUP BY r.id
            ORDER BY r.created_at DESC;
        """)
        referral_codes = cur.fetchall()
        cur.execute("SELECT * FROM partners ORDER BY created_at DESC;")
        partners = cur.fetchall()

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
            "channel_stats": channel_stats,
            "lead_totals": lead_totals,
            "referral_codes": referral_codes,
            "partners": partners,
            "active_source": source_filter,
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

@app.post("/api/leads/{lead_id}/delete")
async def delete_lead(lead_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT id FROM projects WHERE lead_id = %s;", (lead_id,))
        project_ids = [r["id"] for r in cur.fetchall()]
        if project_ids:
            cur.execute("DELETE FROM job_photos WHERE project_id = ANY(%s);", (project_ids,))
            cur.execute("DELETE FROM clock_events WHERE project_id = ANY(%s);", (project_ids,))
            cur.execute("DELETE FROM timesheets WHERE project_id = ANY(%s);", (project_ids,))
            cur.execute("DELETE FROM projects WHERE id = ANY(%s);", (project_ids,))
        cur.execute("DELETE FROM payments WHERE lead_id = %s;", (lead_id,))
        cur.execute("DELETE FROM leads WHERE id = %s;", (lead_id,))
        db.commit()
    return RedirectResponse(url="/hosts", status_code=303)

@app.post("/api/customers")
async def create_customer(name: str = Form(...), email: str = Form(""), phone: str = Form(""), source: str = Form("manual"), db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("INSERT INTO customers (name, email, phone, source) VALUES (%s, %s, %s, %s) RETURNING id", (name, email, phone, source.strip() or "manual"))
        customer_id = cur.fetchone()["id"]
        db.commit()
    return RedirectResponse(url="/hosts", status_code=303)

# --- REFERRAL CODES ---

@app.post("/api/referrals")
async def create_referral(
    request: Request,
    ref_type: str = Form(...),
    ref_id: str = Form(""),
    label: str = Form(""),
    db=Depends(get_db),
):
    require_admin(request)
    ref_type = (ref_type or "").strip().lower()
    if ref_type not in ("partner", "host", "worker", "admin"):
        return JSONResponse({"ok": False, "error": "Invalid referral type."})
    rid = int(ref_id) if (ref_id or "").strip().isdigit() else None

    wanted = label.strip()
    if not wanted:
        if ref_type == "partner" and rid:
            cur = db.cursor()
            cur.execute("SELECT name FROM partners WHERE id = %s;", (rid,))
            row = cur.fetchone()
            wanted = row["name"] if row else f"Partner #{rid}"
    display = wanted or f"{ref_type.capitalize()} code"

    with db.cursor() as cur:
        code = _gen_referral_code(ref_type)
        for _ in range(5):
            try:
                cur.execute(
                    "INSERT INTO referral_codes (code, ref_type, ref_id, label) VALUES (%s, %s, %s, %s) RETURNING id;",
                    (code, ref_type, rid, display),
                )
                db.commit()
                break
            except psycopg.errors.UniqueViolation:
                db.rollback()
                code = _gen_referral_code(ref_type)
    return JSONResponse({"ok": True, "code": code, "url": f"{os.getenv('APP_BASE_URL', 'https://bizstackperks.com')}/?ref={urllib.parse.quote(code)}&src=referral"})

@app.post("/api/referrals/{referral_id}/regen")
async def regen_referral(referral_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM referral_codes WHERE id = %s;", (referral_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Referral code not found.")
        needs_link = row["ref_type"] == "partner" and row["ref_id"]
        new_code = _gen_referral_code(row["ref_type"])
        cur.execute("UPDATE referral_codes SET code = %s WHERE id = %s RETURNING code;", (new_code, referral_id))
        if needs_link:
            cur.execute("UPDATE partners SET referral_code = %s WHERE id = %s;", (new_code, row["ref_id"]))
        db.commit()
    return JSONResponse({"ok": True, "code": new_code, "url": f"{os.getenv('APP_BASE_URL', 'https://bizstackperks.com')}/?ref={urllib.parse.quote(new_code)}&src=referral"})

@app.post("/api/referrals/{referral_id}/cancel")
async def cancel_referral(referral_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT ref_type, ref_id FROM referral_codes WHERE id = %s;", (referral_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Referral code not found.")
        if row["ref_type"] == "partner" and row["ref_id"]:
            cur.execute("UPDATE partners SET referral_code = NULL WHERE id = %s;", (row["ref_id"],))
        cur.execute("DELETE FROM referral_codes WHERE id = %s;", (referral_id,))
        db.commit()
    return JSONResponse({"ok": True})

# --- HOST PORTAL & PORTFOLIO ---

@app.get("/host-login", response_class=HTMLResponse)
async def read_host_login(request: Request):
    error = request.query_params.get("error")
    return templates.TemplateResponse(request=request, name="host_login.html", context={"error": error, "user": None})

@app.post("/api/host/auth/login")
async def host_login(email: str = Form(...), password: str = Form(...), request: Request = None, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM hosts WHERE email ILIKE %s AND status = 'active' AND password_hash IS NOT NULL;", (email.strip(),))
        host = cur.fetchone()
    if not host or host["password_hash"] != _hash_password(password):
        return RedirectResponse(url="/host-login?error=Invalid+email+or+password", status_code=status.HTTP_303_SEE_OTHER)

    actor = {"role": "host", "id": host["id"], "email": host["email"], "name": host["name"]}
    if _otp_enabled(db, "host") and not (host.get("email") or "").strip():
        return RedirectResponse(url="/host-login?error=No+email+on+file+%E2%80%94+ask+the+office+to+add+one", status_code=status.HTTP_303_SEE_OTHER)
    if not _otp_enabled(db, "host"):
        return _finish_login(actor, request)

    code = auth_service.generate_otp()
    _store_otp(db, "host", host["id"], host["email"], code)
    ok, err = await _deliver_otp(db, host["email"], code, actor)
    if not ok:
        return RedirectResponse(url=f"/host-login?error={urllib.parse.quote(err)}", status_code=status.HTTP_303_SEE_OTHER)
    resp = RedirectResponse(url="/login/otp", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        key=auth_service.OTP_COOKIE,
        value=auth_service.issue_pending("host", host["id"], host["email"], host["name"]),
        httponly=True, samesite="lax", secure=_secure_cookies(), max_age=auth_service.OTP_TTL_SECONDS,
    )
    return resp

@app.get("/api/host/auth/logout")
async def host_logout():
    redirect = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie("host_session")
    redirect.delete_cookie("host_id")
    redirect.delete_cookie(auth_service.SESSION_COOKIE)
    redirect.delete_cookie("user_email")
    redirect.delete_cookie("user_name")
    redirect.delete_cookie("role")
    redirect.delete_cookie("actor_id")
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

@app.post("/api/hosts/{host_id}/delete")
async def delete_host(host_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("UPDATE calendar_events SET host_id = NULL, property_id = NULL WHERE host_id = %s;", (host_id,))
        cur.execute("DELETE FROM devices WHERE host_id = %s OR property_id IN (SELECT id FROM properties WHERE host_id = %s);", (host_id, host_id))
        cur.execute("DELETE FROM properties WHERE host_id = %s;", (host_id,))
        cur.execute("DELETE FROM hosts WHERE id = %s;", (host_id,))
        db.commit()
    return RedirectResponse(url="/hosts", status_code=303)

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
                   ce.job_address, ce.job_lat, ce.job_lng, ce.worker_status, ce.channel_source,
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
            "channel_label": channel_sync.channel_label(ev.get("channel_source")),
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
            "features": _features(db, "host"),
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
        "User-Agent": "Broom ServiceHostsOps/1.0 (bizstackperks.com; hello@bizstackperks.com)",
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

def _resolve_job_site(db, event_id: int, provided_address: str = ""):
    """Resolve a cleaner's job site for an event. Precedence:
    submitted address → linked property address → host's first property.
    Returns (address, lat, lng)."""
    address = (provided_address or "").strip()
    lat, lng = None, None
    if address:
        with db.cursor() as cur:
            cur.execute("SELECT lat, lng FROM properties WHERE address = %s LIMIT 1;", (address,))
            row = cur.fetchone()
        if row and row["lat"] is not None:
            return address, row["lat"], row["lng"]
        coords = _geocode(address)
        if coords:
            lat, lng = coords[0], coords[1]
        return address, lat, lng
    with db.cursor() as cur:
        cur.execute(
            """SELECT ce.property_id, p.address AS paddr, p.lat AS plat, p.lng AS plng, h.name AS hname
               FROM calendar_events ce
               LEFT JOIN properties p ON p.id = ce.property_id
               LEFT JOIN hosts h ON h.id = ce.host_id
               WHERE ce.id = %s;""",
            (event_id,),
        )
        ev = cur.fetchone()
    if not ev:
        return "", None, None
    if ev["paddr"]:
        return ev["paddr"], ev["plat"], ev["plng"]
    if ev["hname"]:
        with db.cursor() as cur:
            cur.execute(
                "SELECT address, lat, lng FROM properties WHERE host_id = (SELECT id FROM hosts WHERE name = %s LIMIT 1) ORDER BY id LIMIT 1;",
                (ev["hname"],),
            )
            pr = cur.fetchone()
        if pr:
            return pr["address"], pr["lat"], pr["lng"]
    return "", None, None

def _hash_pin(pin: str) -> str:
    return hashlib.sha256(f"{pin}:{os.getenv('APP_SECRET', 'bizstack')}".encode()).hexdigest()

def _hash_password(password: str) -> str:
    return hashlib.sha256(f"host:{password}:{os.getenv('APP_SECRET', 'bizstack')}".encode()).hexdigest()

def _generate_password(length: int = 8) -> str:
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(chars) for _ in range(length))

_REFERRAL_PREFIXES = {"partner": "lender", "host": "host", "worker": "crew", "admin": "biz"}

def _gen_referral_code(ref_type: str = "partner") -> str:
    prefix = _REFERRAL_PREFIXES.get(ref_type, "ref")
    return f"{prefix}-{secrets.token_hex(3)}"

def _require_host(request: Request, db):
    actor = current_actor(request)
    if actor and actor.get("role") == "host":
        with db.cursor() as cur:
            cur.execute("SELECT * FROM hosts WHERE id = %s AND status = 'active';", (actor["id"],))
            row = cur.fetchone()
            if row:
                return row
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
    actor = current_actor(request)
    if actor and actor.get("role") == "worker":
        with db.cursor() as cur:
            cur.execute("SELECT * FROM workers WHERE id = %s AND is_active = TRUE;", (actor["id"],))
            row = cur.fetchone()
            if row:
                return row
    token = request.cookies.get("worker_session")
    worker_id = request.cookies.get("worker_id")
    if not token or not worker_id:
        return None
    with db.cursor() as cur:
        cur.execute("SELECT * FROM workers WHERE id = %s AND worker_token = %s AND is_active = TRUE;", (worker_id, token))
        return cur.fetchone()

def _can_view_paycheck(request: Request, worker_id: int) -> bool:
    actor = current_actor(request)
    if actor and actor.get("role") == "admin":
        return True
    if actor and actor.get("role") == "worker" and str(actor.get("id")) == str(worker_id):
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
        # Precompute each host's default property address (fallback when an event
        # isn't explicitly linked to a property).
        cur.execute("""
            SELECT h.name, p.address, p.lat, p.lng
            FROM hosts h
            JOIN properties p ON p.host_id = h.id
            ORDER BY h.name;
        """)
        host_props = {}
        for r in cur.fetchall():
            key = (r["name"] or "").strip().lower()
            host_props.setdefault(key, (r["address"], r["lat"], r["lng"]))
        # Unassigned cleaning jobs only — Full-Service Management isn't a cleaner task.
        cur.execute("""
            SELECT ce.id, ce.customer_name, ce.start_time, ce.service_type, ce.amount_cents, ce.worker_pay_cents,
                   ce.property_id, ce.job_address, ce.job_lat, ce.job_lng,
                   p.address AS property_address, p.lat AS property_lat, p.lng AS property_lng
            FROM calendar_events ce
            LEFT JOIN properties p ON p.id = ce.property_id
            WHERE ce.worker_id IS NULL AND ce.start_time >= NOW() - INTERVAL '60 days'
              AND ce.service_type IN ('Turnover Cleaning','Deep Cleaning','Linen Restock','Inspection')
            ORDER BY ce.start_time ASC;
        """)
        unassigned = []
        for ev in cur.fetchall():
            # Auto-fill the job site from the host's (unique) property when possible.
            site_addr = ev["property_address"] or ev["job_address"] or ""
            site_lat = ev["property_lat"] or ev["job_lat"]
            site_lng = ev["property_lng"] or ev["job_lng"]
            if not site_addr:
                hp = host_props.get((ev["customer_name"] or "").strip().lower())
                if hp:
                    site_addr, site_lat, site_lng = hp[0], hp[1], hp[2]
            unassigned.append({
                **ev,
                "default_pay_cents": int(round(ev["amount_cents"] / 2)) if ev["amount_cents"] else 5000,
                "job_site_address": site_addr or "",
                "job_site_lat": site_lat,
                "job_site_lng": site_lng,
            })
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
        cur.execute("""
            SELECT DISTINCT ON (worker_id) worker_id, score, total, passed, created_at
            FROM worker_quiz_results ORDER BY worker_id, created_at DESC;
        """)
        quiz_map = {r["worker_id"]: r for r in cur.fetchall()}

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
            "default_pay_cents": 5000,
            "worker_choices": [{"id": w["id"], "name": w["name"]} for w in workers],
            "worker_rates": {w["id"]: w["pay_rate_cents"] for w in workers},
            "quiz_map": quiz_map,
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

@app.post("/api/workers/{worker_id}/rate")
async def set_worker_rate(worker_id: int, pay_rate: float = Form(...), db=Depends(get_db)):
    if pay_rate < 0:
        raise HTTPException(status_code=400, detail="Pay rate cannot be negative")
    with db.cursor() as cur:
        cur.execute("UPDATE workers SET pay_rate_cents = %s WHERE id = %s;", (int(round(pay_rate * 100)), worker_id))
        db.commit()
    return JSONResponse(content={"status": "success", "pay_rate_cents": int(round(pay_rate * 100))})

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

@app.post("/api/workers/{worker_id}/delete")
async def delete_worker(worker_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("DELETE FROM checks WHERE paycheck_id IN (SELECT id FROM worker_paychecks WHERE worker_id = %s);", (worker_id,))
        cur.execute("DELETE FROM worker_paychecks WHERE worker_id = %s;", (worker_id,))
        cur.execute("DELETE FROM worker_timeclocks WHERE worker_id = %s;", (worker_id,))
        cur.execute("DELETE FROM worker_quiz_results WHERE worker_id = %s;", (worker_id,))
        cur.execute("UPDATE job_photos SET worker_id = NULL WHERE worker_id = %s;", (worker_id,))
        cur.execute("UPDATE calendar_events SET worker_id = NULL WHERE worker_id = %s;", (worker_id,))
        cur.execute("DELETE FROM workers WHERE id = %s;", (worker_id,))
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
        # Auto-resolve the job site address from the host's property if left blank.
        job_site_address, job_lat, job_lng = _resolve_job_site(db, event_id, job_address)
        cur.execute(
            "UPDATE calendar_events SET worker_id = %s, worker_pay_cents = %s, worker_status = 'assigned', job_address = %s, job_lat = %s, job_lng = %s WHERE id = %s;",
            (worker_id, pay_cents, job_site_address or None, job_lat, job_lng, event_id),
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
        cur.execute("SELECT name FROM workers WHERE id = %s;", (worker_id,))
        w = cur.fetchone()
        cur.execute(
            "INSERT INTO ledger_entries (tx_type, ref_type, ref_id, description, amount_cents) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (ref_type, ref_id) DO NOTHING;",
            ("expense", "paycheck", paycheck_id,
             f"Worker payroll — {w['name']} ({period_start_d.strftime('%b %d')} – {period_end_d.strftime('%b %d, %Y')})",
             totals["gross_cents"]),
        )
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
    actor = current_actor(request)
    can_stub_pdf = bool(
        (actor and actor.get("role") == "admin")
        or ((actor and actor.get("role") == "worker") and _feature_enabled(db, "worker", "paystubs"))
    )
    return templates.TemplateResponse(
        request=request,
        name="paycheck.html",
        context={
            "user": {"email": user_email} if is_authed else None,
            "stub": row,
            "gross": row["gross_cents"] / 100,
            "per_job": (row["gross_cents"] / row["job_count"] / 100) if row["job_count"] else None,
            "can_stub_pdf": can_stub_pdf,
        },
    )

# --- WORKER PORTAL ---

@app.get("/worker-login", response_class=HTMLResponse)
async def read_worker_login(request: Request):
    error = request.query_params.get("error")
    return templates.TemplateResponse(request=request, name="worker_login.html", context={"error": error, "user": None})

@app.post("/api/worker/auth/login")
async def worker_login(phone: str = Form(...), pin: str = Form(...), request: Request = None, db=Depends(get_db)):
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

    actor = {"role": "worker", "id": match["id"], "email": match.get("email") or "", "name": match["name"]}
    if _otp_enabled(db, "worker") and not (match.get("email") or "").strip():
        return RedirectResponse(url="/worker-login?error=No+email+on+file+%E2%80%94+ask+the+office+to+add+one", status_code=status.HTTP_303_SEE_OTHER)
    if not _otp_enabled(db, "worker"):
        return _finish_login(actor, request)

    code = auth_service.generate_otp()
    _store_otp(db, "worker", match["id"], match["email"], code)
    ok, err = await _deliver_otp(db, match["email"], code, actor)
    if not ok:
        return RedirectResponse(url=f"/worker-login?error={urllib.parse.quote(err)}", status_code=status.HTTP_303_SEE_OTHER)
    resp = RedirectResponse(url="/login/otp", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        key=auth_service.OTP_COOKIE,
        value=auth_service.issue_pending("worker", match["id"], match["email"], match["name"]),
        httponly=True, samesite="lax", secure=_secure_cookies(), max_age=auth_service.OTP_TTL_SECONDS,
    )
    return resp

@app.get("/api/worker/auth/logout")
async def worker_logout():
    redirect = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie("worker_session")
    redirect.delete_cookie("worker_id")
    redirect.delete_cookie(auth_service.SESSION_COOKIE)
    redirect.delete_cookie("user_email")
    redirect.delete_cookie("user_name")
    redirect.delete_cookie("role")
    redirect.delete_cookie("actor_id")
    return redirect

MAGIC_LINK_TTL_DAYS = int(os.getenv("WORKER_MAGIC_LINK_DAYS", "30"))


def _magic_link_url(request: Request, token: str) -> str:
    return str(request.base_url).rstrip("/") + f"/w/{token}"


def _issue_magic_token(db, worker_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE workers SET magic_token = %s, magic_token_expires_at = NOW() + (%s || ' days')::INTERVAL WHERE id = %s;",
            (token, str(MAGIC_LINK_TTL_DAYS), worker_id),
        )
        db.commit()
    return token


@app.get("/w/{token}")
async def worker_magic_login(token: str, request: Request, db=Depends(get_db)):
    """One-tap crew sign-in from a texted/emailed link."""
    with db.cursor() as cur:
        cur.execute("SELECT * FROM workers WHERE is_active = TRUE AND magic_token = %s;", (token,))
        worker = cur.fetchone()
    if not worker:
        return RedirectResponse(url="/worker-login?error=Link+is+invalid+or+was+reset", status_code=status.HTTP_303_SEE_OTHER)
    expires = worker.get("magic_token_expires_at")
    if expires and expires < datetime.now(APP_TZ):
        return RedirectResponse(url="/worker-login?error=Link+expired+%E2%80%94+ask+the+office+for+a+new+one", status_code=status.HTTP_303_SEE_OTHER)
    actor = {"role": "worker", "id": worker["id"], "email": worker.get("email") or "", "name": worker["name"]}
    return _finish_login(actor, request)


@app.post("/api/workers/{worker_id}/magic-link")
async def worker_magic_link(worker_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT id FROM workers WHERE id = %s;", (worker_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Worker not found")
    token = _issue_magic_token(db, worker_id)
    return JSONResponse({
        "status": "success",
        "url": _magic_link_url(request, token),
        "expires_days": MAGIC_LINK_TTL_DAYS,
    })


@app.get("/worker", response_class=HTMLResponse)
async def worker_portal(request: Request, db=Depends(get_db)):
    worker = _require_worker(request, db)
    if not worker:
        return RedirectResponse(url="/worker-login", status_code=status.HTTP_303_SEE_OTHER)

    features = _features(db, "worker")

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
            if not features.get("jobs"):
                continue
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
            if not features.get("map"):
                j = {**j, "job_lat": None, "job_lng": None, "job_address": None}
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
        if not features.get("pay"):
            paychecks = []
        cur.execute("""
            SELECT score, total, passed, created_at
            FROM worker_quiz_results WHERE worker_id = %s ORDER BY created_at DESC LIMIT 1;
        """, (worker["id"],))
        quiz = cur.fetchone()

    return templates.TemplateResponse(
        request=request,
        name="worker_portal.html",
        context={
            "user": None,
            "worker": worker,
            "jobs": jobs,
            "totals": totals,
            "paychecks": paychecks,
            "quiz": quiz,
            "features": features,
            "job_coords_map": {
                j["id"]: {"lat": j["job_lat"], "lng": j["job_lng"]} for j in jobs if j["job_lat"] is not None and j["job_lng"] is not None
            },
        },
    )

@app.get("/worker/stub/{paycheck_id}/pdf", response_class=Response)
async def worker_stub_pdf(paycheck_id: int, request: Request, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT worker_id FROM worker_paychecks WHERE id = %s;", (paycheck_id,))
        row = cur.fetchone()
    if not row or not _can_view_paycheck(request, row["worker_id"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not allowed")
    _require_feature(db, "worker", "paystubs")
    with db.cursor() as cur:
        cur.execute(
            "SELECT p.*, w.name AS worker_name FROM worker_paychecks p JOIN workers w ON w.id = p.worker_id WHERE p.id = %s;",
            (paycheck_id,),
        )
        row = cur.fetchone()
    stub = {
        "id": row["id"],
        "worker_name": row["worker_name"],
        "period_start": row["period_start"].strftime("%b %d, %Y"),
        "period_end": row["period_end"].strftime("%b %d, %Y"),
        "job_count": row["job_count"],
        "gross_cents": row["gross_cents"],
        "pay_date": date.today().strftime("%b %d, %Y"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fed_cents": 0,
        "state_cents": 0,
        "fica_cents": 0,
        "other_cents": 0,
    }
    pdf = documents_service.render_pay_stub_pdf(stub)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=\"stub-{row['id']}.pdf\""})

@app.post("/api/worker/jobs/{event_id}/complete")
async def worker_complete_job(event_id: int, request: Request, db=Depends(get_db)):
    _require_feature(db, "worker", "jobs")
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
    _require_feature(db, "worker", "timeclock")
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

# --- WORKER PHOTO FINISH (photo-verified cleaning) ---

def _worker_owns_event(db, event_id: int, worker_id: int) -> bool:
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM calendar_events WHERE id = %s AND worker_id = %s;", (event_id, worker_id))
        return cur.fetchone() is not None

def _serialize_photo(p: dict, request: Request) -> dict:
    return {
        "id": p["id"],
        "event_id": p["event_id"],
        "room": p["room"] or "",
        "category": p["category"] or "clean",
        "caption": p["caption"] or "",
        "file_type": p["file_type"] or "image/jpeg",
        "verified": bool(p["verified"]),
        "worker_name": p.get("worker_name") or "",
        "created_at": p["created_at"].isoformat() if p["created_at"] else "",
        "url": f"/api/photos/{p['id']}/image",
    }

def _can_access_photo(request: Request, db, photo_id: int) -> dict:
    """Return the photo row if the current actor may view it (worker-owned job,
    matching host, or admin), else None."""
    with db.cursor() as cur:
        cur.execute("""
            SELECT jp.*, w.name AS worker_name, e.host_id AS event_host_id
            FROM job_photos jp
            JOIN calendar_events e ON e.id = jp.event_id
            LEFT JOIN workers w ON w.id = jp.worker_id
            WHERE jp.id = %s;
        """, (photo_id,))
        row = cur.fetchone()
    if not row:
        return None
    actor = _session_actor(request)
    if actor and actor.get("role") == "admin":
        return row
    if actor and actor.get("role") == "worker" and actor.get("id") == row["worker_id"]:
        return row
    if actor and actor.get("role") == "host" and row["event_host_id"] == actor.get("id"):
        return row
    return None

@app.get("/photos", response_class=HTMLResponse)
async def photos_page(request: Request, db=Depends(get_db)):
    actor = current_actor(request)
    if not actor:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    is_admin = actor["role"] == "admin"
    with db.cursor() as cur:
        if is_admin:
            cur.execute("""
                SELECT jp.id, jp.event_id, jp.room, jp.category, jp.caption, jp.file_type,
                       jp.verified, jp.created_at, w.name AS worker_name,
                       e.customer_name, e.service_type, e.start_time, e.job_address
                FROM job_photos jp
                JOIN calendar_events e ON e.id = jp.event_id
                LEFT JOIN workers w ON w.id = jp.worker_id
                ORDER BY jp.created_at DESC
                LIMIT 300;
            """)
        elif actor["role"] == "host":
            cur.execute("""
                SELECT jp.id, jp.event_id, jp.room, jp.category, jp.caption, jp.file_type,
                       jp.verified, jp.created_at, w.name AS worker_name,
                       e.customer_name, e.service_type, e.start_time, e.job_address
                FROM job_photos jp
                JOIN calendar_events e ON e.id = jp.event_id
                LEFT JOIN workers w ON w.id = jp.worker_id
                WHERE e.host_id = %s
                ORDER BY jp.created_at DESC
                LIMIT 300;
            """, (actor["id"],))
        elif actor["role"] == "worker":
            cur.execute("""
                SELECT jp.id, jp.event_id, jp.room, jp.category, jp.caption, jp.file_type,
                       jp.verified, jp.created_at, w.name AS worker_name,
                       e.customer_name, e.service_type, e.start_time, e.job_address
                FROM job_photos jp
                JOIN calendar_events e ON e.id = jp.event_id
                LEFT JOIN workers w ON w.id = jp.worker_id
                WHERE jp.worker_id = %s
                ORDER BY jp.created_at DESC
                LIMIT 300;
            """, (actor["id"],))
        else:
            raise HTTPException(status_code=403, detail="Access denied")
        photos = cur.fetchall()
    return templates.TemplateResponse(request=request, name="photos.html", context={
        "user": None,
        "actor": actor,
        "photos": photos,
        "is_admin": is_admin,
    })

@app.post("/api/photos/upload")
async def photo_upload(
    request: Request,
    event_id: int = Form(...),
    room: str = Form(""),
    category: str = Form("clean"),
    caption: str = Form(""),
    photo: UploadFile = File(...),
    db=Depends(get_db),
):
    worker = _require_worker(request, db)
    if not worker:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _require_feature(db, "worker", "photos")
    if not _worker_owns_event(db, event_id, worker["id"]):
        raise HTTPException(status_code=404, detail="Job not found or not assigned to you")
    if category not in ("clean", "damage", "other"):
        category = "clean"
    data = await photo.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Photo too large (max 15 MB)")
    file_type = (photo.content_type or "image/jpeg").split(";")[0]
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO job_photos (event_id, worker_id, room, category, caption, file_type, file_data) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;",
            (event_id, worker["id"], room or None, category, caption or None, file_type, data),
        )
        photo_id = cur.fetchone()["id"]
        db.commit()
    return JSONResponse(content={"ok": True, "photo_id": photo_id, "url": f"/api/photos/{photo_id}/image"})

@app.get("/api/photos/{photo_id}/image", response_class=Response)
async def photo_image(photo_id: int, request: Request, db=Depends(get_db)):
    row = _can_access_photo(request, db, photo_id)
    if not row:
        raise HTTPException(status_code=404, detail="Photo not found")
    return Response(
        content=bytes(row["file_data"] or b""),
        media_type=row["file_type"] or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )

@app.get("/api/photos", response_class=JSONResponse)
async def photos_list(request: Request, event_id: int | None = None, db=Depends(get_db)):
    actor = _session_actor(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Login required")
    with db.cursor() as cur:
        if actor["role"] == "admin":
            if event_id:
                cur.execute("SELECT jp.*, w.name AS worker_name FROM job_photos jp LEFT JOIN workers w ON w.id = jp.worker_id WHERE jp.event_id = %s ORDER BY jp.created_at DESC;", (event_id,))
            else:
                cur.execute("SELECT jp.*, w.name AS worker_name FROM job_photos jp LEFT JOIN workers w ON w.id = jp.worker_id ORDER BY jp.created_at DESC LIMIT 300;")
        elif actor["role"] == "host":
            cur.execute("""
                SELECT jp.*, w.name AS worker_name FROM job_photos jp
                JOIN calendar_events e ON e.id = jp.event_id
                LEFT JOIN workers w ON w.id = jp.worker_id
                WHERE e.host_id = %s ORDER BY jp.created_at DESC LIMIT 300;
            """, (actor["id"],))
        elif actor["role"] == "worker":
            if event_id:
                cur.execute("""
                    SELECT jp.*, w.name AS worker_name FROM job_photos jp
                    LEFT JOIN workers w ON w.id = jp.worker_id
                    JOIN calendar_events e ON e.id = jp.event_id
                    WHERE jp.worker_id = %s AND e.id = %s ORDER BY jp.created_at DESC;
                """, (actor["id"], event_id))
            else:
                cur.execute("SELECT jp.*, w.name AS worker_name FROM job_photos jp LEFT JOIN workers w ON w.id = jp.worker_id WHERE jp.worker_id = %s ORDER BY jp.created_at DESC;", (actor["id"],))
        else:
            raise HTTPException(status_code=403, detail="Access denied")
        rows = cur.fetchall()
    return JSONResponse(content={"ok": True, "photos": [_serialize_photo(p, request) for p in rows]})

@app.post("/api/photos/{photo_id}/verify")
async def photo_verify(photo_id: int, request: Request, verified: bool = Form(True), db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("UPDATE job_photos SET verified = %s WHERE id = %s;", (verified, photo_id))
        db.commit()
    return JSONResponse(content={"ok": True, "verified": bool(verified)})

@app.get("/photos/export/{event_id}.pdf", response_class=Response)
async def photos_export_pdf(event_id: int, request: Request, db=Depends(get_db)):
    actor = current_actor(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Login required")
    with db.cursor() as cur:
        if actor["role"] == "admin":
            cur.execute("SELECT * FROM calendar_events WHERE id = %s;", (event_id,))
        elif actor["role"] == "host":
            cur.execute("SELECT * FROM calendar_events WHERE id = %s AND host_id = %s;", (event_id, actor["id"]))
        else:
            cur.execute("SELECT * FROM calendar_events WHERE id = %s AND worker_id = %s;", (event_id, actor["id"]))
        event = cur.fetchone()
        cur.execute("""
            SELECT jp.*, w.name AS worker_name FROM job_photos jp
            LEFT JOIN workers w ON w.id = jp.worker_id
            WHERE jp.event_id = %s ORDER BY jp.category, jp.room, jp.created_at ASC;
        """, (event_id,))
        photos = cur.fetchall()
    if not event or not photos:
        raise HTTPException(status_code=404, detail="No photos for this job")
    from fpdf import FPDF
    import io
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 9, "Broom Service - Photo Verification Report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Job: {event['customer_name'] or ''}  |  {event['service_type'] or ''}  |  {event['start_time']}", new_x="LMARGIN", new_y="NEXT")
    if event.get("job_address"):
        pdf.cell(0, 6, f"Address: {event['job_address']}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Export ready for Airbnb checkout / guest turnover handoff. Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    for p in photos:
        if len(bytes(p["file_data"] or b"")) > 3 * 1024 * 1024:
            continue
        room = p["room"] or "Room"
        cat = {"clean": "CLEAN", "damage": "DAMAGE", "other": "OTHER"}.get(p["category"], "NOTE")
        label = f"{cat} - {room}"
        if p.get("caption"):
            label += f" - {p['caption']}"
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, label, new_x="LMARGIN", new_y="NEXT")
        try:
            pdf.image(io.BytesIO(bytes(p["file_data"])), w=150)
            pdf.ln(2)
        except Exception as e:
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(0, 6, f"(Image could not be embedded: {e})", new_x="LMARGIN", new_y="NEXT")
    pdf_bytes = bytes(pdf.output())
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=\"photos-{event_id}.pdf\""})

# --- SMART DEVICES (LOCKS, ALARMS, LIGHTING & MORE) ---

DEVICE_KINDS = ("lock", "alarm", "lighting", "thermostat", "camera", "garage", "other")


def _property_host(db, property_id):
    with db.cursor() as cur:
        cur.execute("SELECT host_id FROM properties WHERE id = %s;", (property_id,))
        row = cur.fetchone()
    return row["host_id"] if row else None


def _host_owns_property(db, host_id, property_id) -> bool:
    with db.cursor() as cur:
        cur.execute("SELECT id FROM properties WHERE id = %s AND host_id = %s;", (property_id, host_id))
        return cur.fetchone() is not None


def _owns_device(db, device_id, host_id) -> bool:
    with db.cursor() as cur:
        cur.execute("SELECT host_id FROM devices WHERE id = %s;", (device_id,))
        row = cur.fetchone()
    return bool(row and row["host_id"] == host_id)


def _clean_device_form(form) -> dict:
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Device name is required")
    kind = (form.get("kind") or "other").strip()
    if kind not in DEVICE_KINDS:
        kind = "other"
    custom_kind = (form.get("custom_kind") or "").strip()
    if kind == "other" and not custom_kind:
        custom_kind = "Other"
    elif kind != "other":
        custom_kind = None
    try:
        property_id = int(form.get("property_id") or 0) or None
    except (TypeError, ValueError):
        property_id = None
    status = (form.get("status") or "active").strip()
    if status not in ("active", "disabled"):
        status = "active"
    return {
        "name": name,
        "kind": kind,
        "custom_kind": custom_kind,
        "vendor": (form.get("vendor") or "").strip(),
        "model": (form.get("model") or "").strip(),
        "device_ref": (form.get("device_ref") or "").strip(),
        "property_id": property_id,
        "access_code": (form.get("access_code") or "").strip(),
        "access_instructions": (form.get("access_instructions") or "").strip(),
        "status": status,
    }


@app.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request, db=Depends(get_db)):
    actor = current_actor(request)
    if not actor:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    role = actor.get("role")

    with db.cursor() as cur:
        if role == "admin":
            cur.execute("""
                SELECT d.*, p.name AS property_name, p.address AS property_address,
                       h.name AS host_name
                FROM devices d
                LEFT JOIN properties p ON p.id = d.property_id
                LEFT JOIN hosts h ON h.id = d.host_id
                ORDER BY p.name NULLS LAST, d.kind, d.name;
            """)
            devices = cur.fetchall()
            cur.execute("SELECT id, name, address, host_id FROM properties ORDER BY name;")
            properties = cur.fetchall()
        elif role == "host":
            host = _require_host(request, db)
            if not host:
                return RedirectResponse(url="/host-login", status_code=status.HTTP_303_SEE_OTHER)
            cur.execute("""
                SELECT d.*, p.name AS property_name, p.address AS property_address
                FROM devices d
                LEFT JOIN properties p ON p.id = d.property_id
                WHERE d.host_id = %s
                ORDER BY p.name NULLS LAST, d.kind, d.name;
            """, (host["id"],))
            devices = cur.fetchall()
            cur.execute("SELECT id, name, address FROM properties WHERE host_id = %s ORDER BY name;", (host["id"],))
            properties = cur.fetchall()
        elif role == "worker":
            worker = _require_worker(request, db)
            if not worker:
                return RedirectResponse(url="/worker-login", status_code=status.HTTP_303_SEE_OTHER)
            cur.execute("""
                SELECT DISTINCT d.*, p.name AS property_name, p.address AS property_address,
                       h.name AS host_name
                FROM devices d
                JOIN properties p ON p.id = d.property_id
                LEFT JOIN hosts h ON h.id = d.host_id
                WHERE d.status = 'active'
                  AND d.kind IN ('lock', 'alarm', 'garage')
                  AND d.property_id IN (
                      SELECT ce.property_id FROM calendar_events ce
                      WHERE ce.worker_id = %s AND ce.property_id IS NOT NULL
                  )
                ORDER BY p.name, d.kind;
            """, (worker["id"],))
            devices = cur.fetchall()
            properties = []
        else:
            raise HTTPException(status_code=403, detail="Access denied")

    groups = []
    by_name = {}
    for d in devices:
        key = d.get("property_name") or "Unassigned"
        if key not in by_name:
            by_name[key] = {"name": key, "address": d.get("property_address"), "host": d.get("host_name"), "devices": []}
            groups.append(by_name[key])
        by_name[key]["devices"].append(d)

    return templates.TemplateResponse(
        request=request,
        name="devices.html",
        context={
            "user": None,
            "actor": actor,
            "role": role,
            "groups": groups,
            "properties": properties,
            "flash": (request.query_params.get("added") or request.query_params.get("edited") or request.query_params.get("deleted")),
        },
    )


@app.post("/api/devices")
async def devices_add(request: Request, db=Depends(get_db)):
    actor = current_actor(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Login required")
    role = actor.get("role")
    form = await request.form()
    data = _clean_device_form(form)
    if role == "admin":
        host_id = data["property_id"] and _property_host(db, data["property_id"])
    elif role == "host":
        host = _require_host(request, db)
        if not host:
            raise HTTPException(status_code=401, detail="Host login required")
        host_id = host["id"]
        if data["property_id"] and not _host_owns_property(db, host_id, data["property_id"]):
            raise HTTPException(status_code=403, detail="That property does not belong to you")
    else:
        raise HTTPException(status_code=403, detail="Workers cannot add devices")

    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO devices (host_id, property_id, kind, custom_kind, name, vendor, model,
                                 device_ref, access_code, access_instructions, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
        """, (
            host_id, data["property_id"], data["kind"], data["custom_kind"], data["name"],
            data["vendor"], data["model"], data["device_ref"], data["access_code"],
            data["access_instructions"], data["status"],
        ))
        db.commit()
    return RedirectResponse(url="/devices?added=1", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/devices/{device_id}/edit")
async def devices_edit(device_id: int, request: Request, db=Depends(get_db)):
    actor = current_actor(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Login required")
    role = actor.get("role")
    form = await request.form()
    data = _clean_device_form(form)
    if role == "admin":
        with db.cursor() as cur:
            cur.execute("SELECT id FROM devices WHERE id = %s;", (device_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Device not found")
        host_id = data["property_id"] and _property_host(db, data["property_id"])
    elif role == "host":
        host = _require_host(request, db)
        if not host or not _owns_device(db, device_id, host["id"]):
            raise HTTPException(status_code=403, detail="You can only edit your own devices")
        host_id = host["id"]
        if data["property_id"] and not _host_owns_property(db, host_id, data["property_id"]):
            raise HTTPException(status_code=403, detail="That property does not belong to you")
    else:
        raise HTTPException(status_code=403, detail="Workers cannot edit devices")

    with db.cursor() as cur:
        cur.execute("""
            UPDATE devices SET host_id = %s, property_id = %s, kind = %s, custom_kind = %s,
                   name = %s, vendor = %s, model = %s, device_ref = %s,
                   access_code = %s, access_instructions = %s, status = %s
            WHERE id = %s;
        """, (
            host_id, data["property_id"], data["kind"], data["custom_kind"], data["name"],
            data["vendor"], data["model"], data["device_ref"], data["access_code"],
            data["access_instructions"], data["status"], device_id,
        ))
        db.commit()
    return RedirectResponse(url="/devices?edited=1", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/devices/{device_id}/toggle")
async def devices_toggle(device_id: int, request: Request, db=Depends(get_db)):
    actor = current_actor(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Login required")
    role = actor.get("role")
    with db.cursor() as cur:
        if role == "host":
            host = _require_host(request, db)
            if not host or not _owns_device(db, device_id, host["id"]):
                raise HTTPException(status_code=403, detail="Not allowed")
            cur.execute("SELECT status FROM devices WHERE id = %s AND host_id = %s;", (device_id, host["id"]))
        elif role == "admin":
            cur.execute("SELECT status FROM devices WHERE id = %s;", (device_id,))
        else:
            raise HTTPException(status_code=403, detail="Not allowed")
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Device not found")
    new_status = "disabled" if row["status"] == "active" else "active"
    with db.cursor() as cur:
        cur.execute("UPDATE devices SET status = %s WHERE id = %s;", (new_status, device_id))
        db.commit()
    return RedirectResponse(url="/devices", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/devices/{device_id}/delete")
async def devices_delete(device_id: int, request: Request, db=Depends(get_db)):
    actor = current_actor(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Login required")
    role = actor.get("role")
    if role == "admin":
        with db.cursor() as cur:
            cur.execute("DELETE FROM devices WHERE id = %s;", (device_id,))
            db.commit()
    elif role == "host":
        host = _require_host(request, db)
        if not host or not _owns_device(db, device_id, host["id"]):
            raise HTTPException(status_code=403, detail="Not allowed")
        with db.cursor() as cur:
            cur.execute("DELETE FROM devices WHERE id = %s;", (device_id,))
            db.commit()
    else:
        raise HTTPException(status_code=403, detail="Not allowed")
    return RedirectResponse(url="/devices?deleted=1", status_code=status.HTTP_303_SEE_OTHER)

# --- INTERNAL OFFICE MESSAGING ---

def _message_actor(request: Request):
    """Current session actor, or a worker authenticated via the crew app's worker_session.

    Returns {role, id, email, name, company} where company is 'broom' or
    'construction' (admin belongs to both and reports 'all')."""
    actor = current_actor(request)
    if actor:
        actor = dict(actor)
        actor["company"] = "all" if actor.get("role") == "admin" else resolve_company(request)
        return actor
    worker = _worker_session_actor(request)
    if worker:
        return worker
    token = request.cookies.get("host_session")
    if token:
        actor = auth_service.actor_from_token(token)
        if actor and actor.get("role") == "host":
            actor = dict(actor)
            actor["company"] = "broom"
            return actor
    return None

def _message_visible(m: dict, actor: dict) -> bool:
    """A message is visible to an actor when it's an announcement they're in
    scope for, or a DM where they are the author or the recipient."""
    if not actor:
        return False
    kind = m.get("kind") or "announcement"
    if kind == "dm":
        author_match = m.get("author_role") == actor.get("role") and m.get("author_id") == actor.get("id")
        recipient_role = m.get("recipient_role")
        recipient_id = m.get("recipient_id")
        recipient_match = recipient_role == actor.get("role") and (
            recipient_id == actor.get("id")
            or (recipient_id is None and recipient_role == "admin")
        )
        return bool(author_match or recipient_match)
    scope = m.get("scope") or "all"
    if actor.get("role") == "admin" or actor.get("company") == "all":
        return True
    return scope == "all" or scope == actor.get("company")


def _serialize_message(m: dict) -> dict:
    return {
        "id": m["id"],
        "kind": m.get("kind") or "announcement",
        "author_role": m["author_role"],
        "author_id": m["author_id"],
        "author_name": m["author_name"],
        "author_company": m.get("company") or "broom",
        "audience": m.get("audience"),
        "scope": m.get("scope") or "all",
        "recipient_role": m.get("recipient_role"),
        "recipient_id": m.get("recipient_id"),
        "recipient_name": m.get("recipient_name"),
        "body": m["body"],
        "created_at": m["created_at"].isoformat(),
    }


@app.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request, db=Depends(get_db)):
    actor = current_actor(request)
    if not actor:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="messages.html", context={
        "user": None,
        "actor": actor,
    })

@app.get("/api/messages", response_class=JSONResponse)
async def messages_list(request: Request, after: int = 0, db=Depends(get_db)):
    actor = _message_actor(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Login required")
    if actor.get("role") != "worker" and not _device_session_ok(db, actor, request):
        raise HTTPException(status_code=403, detail="Messaging is locked to this account's active device. Log in again on this device to take over.")
    with db.cursor() as cur:
        cur.execute("""
            SELECT * FROM messages WHERE id > %s
            ORDER BY id ASC LIMIT 500;
        """, (int(after),))
        rows = cur.fetchall()
    msgs = [_serialize_message(m) for m in rows if _message_visible(m, actor)]
    return JSONResponse(content={"ok": True, "messages": msgs})

@app.get("/api/messages/roster", response_class=JSONResponse)
async def messages_roster(request: Request, db=Depends(get_db)):
    """Unified roster of every reachable user across both companies."""
    actor = _message_actor(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Login required")
    roster = []
    with db.cursor() as cur:
        cur.execute("SELECT id, email FROM users WHERE role = 'admin';")
        for u in cur.fetchall():
            roster.append({"role": "admin", "id": u["id"], "name": u["email"], "company": "all"})
        cur.execute("SELECT id, name, email, company FROM workers WHERE is_active = TRUE;")
        for w in cur.fetchall():
            roster.append({"role": "worker", "id": w["id"], "name": w["name"], "company": w.get("company") or "broom"})
        cur.execute("SELECT id, name, email, company FROM crew WHERE is_active = TRUE;")
        for c in cur.fetchall():
            roster.append({"role": "worker", "id": c["id"], "name": c["name"], "company": c.get("company") or "construction"})
        cur.execute("SELECT id, name, email, company FROM hosts WHERE status = 'active';")
        for h in cur.fetchall():
            roster.append({"role": "host", "id": h["id"], "name": h["name"], "company": h.get("company") or "broom"})
    return JSONResponse(content={"ok": True, "roster": roster})

@app.post("/api/messages", response_class=JSONResponse)
async def messages_send(request: Request, body: str = Form(...), audience: str = Form("office"),
                        kind: str = Form("dm"), to_role: str = Form(""), to_id: str = Form(""),
                        scope: str = Form("all"), db=Depends(get_db)):
    actor = _message_actor(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Login required")
    if actor.get("role") != "worker" and not _device_session_ok(db, actor, request):
        raise HTTPException(status_code=403, detail="Messaging is locked to this account's active device. Log in again on this device to take over.")
    kind = kind if kind in ("dm", "announcement") else "dm"
    if kind == "announcement":
        if actor.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Only the owner can announce to everyone")
        scope = scope if scope in ("all", "broom", "construction") else "all"
    elif actor.get("role") == "worker":
        # Crew/workers can't broadcast; they send DMs to the office/admin instead.
        to_role, to_id, scope = "admin", "", "all"
    if not body.strip():
        raise HTTPException(status_code=400, detail="Message is empty")
    recipient_name = None
    recipient_role = None
    recipient_id = None
    if kind == "dm" and to_role:
        if to_role == "admin":
            recipient_role, recipient_name = "admin", "Office"
        else:
            recipient_id = int(to_id) if (to_id or "").isdigit() else None
            if recipient_id is not None:
                recipient_role = to_role
                with db.cursor() as cur:
                    cur.execute(f"SELECT name FROM {to_role + 's'} WHERE id = %s LIMIT 1;", (recipient_id,))
                    row = cur.fetchone()
                    recipient_name = row["name"] if row else None
    author_company = actor.get("company")
    if author_company == "all":
        author_company = resolve_company(request)
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO messages (author_role, author_id, author_name, company, kind, audience, scope,
                                      recipient_role, recipient_id, recipient_name, body)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;""",
            (actor["role"], actor["id"], actor["name"], author_company, kind,
             audience if kind == "announcement" else "office",
             scope if kind == "announcement" else "all",
             recipient_role, recipient_id, recipient_name, body[:4000]),
        )
        new_id = cur.fetchone()["id"]
        db.commit()
    new_row = {
        "id": new_id,
        "kind": kind,
        "author_role": actor["role"],
        "author_id": actor["id"],
        "author_name": actor["name"],
        "author_company": author_company,
        "audience": audience if kind == "announcement" else "office",
        "scope": scope if kind == "announcement" else "all",
        "recipient_role": recipient_role,
        "recipient_id": recipient_id,
        "recipient_name": recipient_name,
        "body": body[:4000],
        "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
    }
    for ws in list(msg_clients):
        viewer = getattr(ws.state, "actor", None)
        try:
            if _message_visible(new_row, viewer):
                await ws.send_json(new_row)
        except Exception:
            msg_clients.discard(ws)
    return JSONResponse(content={"ok": True})

@app.post("/api/messages/clear", response_class=JSONResponse)
async def messages_clear(request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("DELETE FROM messages;")
        db.commit()
    for ws in list(msg_clients):
        try:
            await ws.send_json({"clear": True})
        except Exception:
            msg_clients.discard(ws)
    return JSONResponse(content={"ok": True, "cleared": True})

msg_clients: set = set()

@app.websocket("/ws/messages")
async def ws_messages(websocket: WebSocket):
    actor = _message_actor(websocket)
    if not actor:
        await websocket.close(code=1008)
        return
    if actor.get("role") != "worker":
        conn = psycopg.connect(db_url, row_factory=dict_row)
        try:
            device_ok = _device_session_ok(conn, actor, websocket)
        finally:
            conn.close()
        if not device_ok:
            await websocket.close(code=1008)
            return
    await websocket.accept()
    msg_clients.add(websocket)
    websocket.state.actor = actor
    try:
        while True:
            id_text = await websocket.receive_text()
            try:
                after_id = int(id_text)
            except (TypeError, ValueError):
                after_id = 0
            conn = psycopg.connect(db_url, row_factory=dict_row)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM messages WHERE id > %s ORDER BY id ASC LIMIT 100;", (after_id,))
                    rows = cur.fetchall()
                for m in rows:
                    if _message_visible(m, actor):
                        await websocket.send_json(_serialize_message(m))
            finally:
                conn.close()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        msg_clients.discard(websocket)

@app.get("/api/alerts", response_class=JSONResponse)
async def alerts_list(request: Request, db=Depends(get_db)):
    actor = current_actor(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Login required")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 30;")
        rows = cur.fetchall()
    return JSONResponse(content={"ok": True, "alerts": [
        {
            "id": a["id"], "kind": a["kind"], "severity": a["severity"],
            "message": a["message"], "details": a["details"],
            "read": bool(a["read"]), "created_at": a["created_at"].isoformat(),
        }
        for a in rows
    ]})

@app.post("/api/alerts/read")
async def alerts_mark_read(request: Request, ids: str = Form(""), db=Depends(get_db)):
    actor = current_actor(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Login required")
    with db.cursor() as cur:
        if (ids or "").strip() and (ids or "").strip() != "all":
            wanted = [int(x) for x in ids.split(",") if x.strip().isdigit()]
            if wanted:
                cur.execute("UPDATE alerts SET read = TRUE WHERE id = ANY(%s);", (wanted,))
        else:
            cur.execute("UPDATE alerts SET read = TRUE;")
        db.commit()
    return JSONResponse(content={"ok": True})

# The /app installable phone app reuses the exact same worker auth, clock,
# geofence, location-tracking and paycheck backend as the web portal above.

@app.get("/app", response_class=HTMLResponse)
async def worker_app_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="app.html",
        context={},
        headers={"Cache-Control": "no-cache, max-age=0"},
    )

@app.get("/manifest.webmanifest", response_class=Response)
async def web_manifest():
    content = Path(__file__).with_name("static") / "manifest.webmanifest"
    return Response(content=content.read_text(), media_type="application/manifest+json")

@app.get("/sw.js", response_class=Response)
async def service_worker():
    content = Path(__file__).with_name("static") / "sw.js"
    return Response(
        content=content.read_text(),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, max-age=0"},
    )

@app.post("/api/worker/app/login")
async def worker_app_login(phone: str = Form(...), pin: str = Form(...), db=Depends(get_db)):
    phone_digits = _digits(phone)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM workers WHERE is_active = TRUE;")
        candidates = cur.fetchall()
    match = None
    for w in candidates:
        wp = _digits(w.get("phone") or "")
        if wp and wp[-10:] == phone_digits[-10:] and w["pin_hash"] == _hash_pin(pin.strip()):
            match = w
            break
    if not match:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Invalid phone or PIN"})

    if _otp_enabled(db, "worker"):
        email = (match.get("email") or "").strip()
        if not email:
            return JSONResponse(status_code=400, content={"status": "error", "message": "No email on file — ask the office to add one so we can send your login code"})
        code = auth_service.generate_otp()
        _store_otp(db, "worker", match["id"], email, code)
        ok, err = await _deliver_otp(db, email, code, {"name": match["name"]})
        if not ok:
            return JSONResponse(status_code=500, content={"status": "error", "message": err})
        resp = JSONResponse(content={"status": "otp_required", "email": email})
        resp.set_cookie(
            key=auth_service.OTP_COOKIE,
            value=auth_service.issue_pending("worker", match["id"], email, match["name"]),
            httponly=True, samesite="lax", secure=_secure_cookies(), max_age=auth_service.OTP_TTL_SECONDS,
        )
        return resp

    token = secrets.token_urlsafe(32)
    with db.cursor() as cur:
        cur.execute("UPDATE workers SET worker_token = %s WHERE id = %s;", (token, match["id"]))
        db.commit()

    resp = JSONResponse(content={"status": "ok", "worker_id": match["id"], "name": match["name"]})
    resp.set_cookie(key="worker_session", value=token, httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "false").lower() == "true")
    resp.set_cookie(key="worker_id", value=str(match["id"]), httponly=True, samesite="lax")
    return resp

@app.post("/api/worker/app/verify")
async def worker_app_verify(request: Request, code: str = Form(...), db=Depends(get_db)):
    pending = auth_service.pending_from_token(request.cookies.get(auth_service.OTP_COOKIE))
    if not pending or pending.get("role") != "worker":
        return JSONResponse(status_code=401, content={"status": "error", "message": "Verification expired - sign in again"})
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM login_otps WHERE role = 'worker' AND actor_id IS NOT DISTINCT FROM %s AND consumed = FALSE ORDER BY created_at DESC LIMIT 1;",
            (pending.get("id"),),
        )
        row = cur.fetchone()
    if not row or (row["expires_at"] and row["expires_at"] < datetime.now(APP_TZ)):
        return JSONResponse(status_code=401, content={"status": "error", "message": "Code expired - sign in again"})
    if row["attempts"] >= auth_service.MAX_OTP_ATTEMPTS or not auth_service.verify_otp((code or "").strip(), row["code_hash"]):
        with db.cursor() as cur:
            cur.execute("UPDATE login_otps SET attempts = attempts + 1 WHERE id = %s;", (row["id"],))
            db.commit()
        return JSONResponse(status_code=401, content={"status": "error", "message": "Incorrect code"})
    with db.cursor() as cur:
        cur.execute("UPDATE login_otps SET consumed = TRUE WHERE id = %s;", (row["id"],))
        db.commit()
    with db.cursor() as cur:
        cur.execute("SELECT id, name, phone, email FROM workers WHERE id = %s;", (pending.get("id"),))
        w = cur.fetchone()
    resp = JSONResponse(content={"status": "ok", "name": w["name"] if w else ""})
    actor = {"role": "worker", "id": pending.get("id"), "email": pending.get("email"), "name": pending.get("name")}
    token = auth_service.issue_session(actor["role"], actor["id"], actor["email"], actor["name"])
    resp.set_cookie(key=auth_service.SESSION_COOKIE, value=token, httponly=True, samesite="lax", secure=_secure_cookies())
    resp.set_cookie(key="user_email", value=actor["email"] or "", httponly=True, samesite="lax")
    resp.set_cookie(key="user_name", value=actor["name"] or "", httponly=True, samesite="lax")
    resp.set_cookie(key="role", value="worker", httponly=True, samesite="lax")
    resp.set_cookie(key="actor_id", value=str(actor["id"]), httponly=True, samesite="lax")
    resp.delete_cookie(auth_service.OTP_COOKIE)
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        new_device, new_device_id, old_device_id = _register_device_session(conn, actor, token, request, resp)
    if new_device:
        try:
            asyncio.create_task(_notify_new_device(actor, new_device_id, old_device_id, request))
        except Exception as e:
            print(f"[SECURITY] could not raise new-device alert: {e}")
    return resp

@app.post("/api/worker/app/resend")
async def worker_app_resend(request: Request, db=Depends(get_db)):
    pending = auth_service.pending_from_token(request.cookies.get(auth_service.OTP_COOKIE))
    if not pending or pending.get("role") != "worker":
        return JSONResponse(status_code=401, content={"status": "error", "message": "Verification expired - sign in again"})
    code = auth_service.generate_otp()
    _store_otp(db, "worker", pending.get("id"), pending.get("email"), code)
    ok, err = await _deliver_otp(db, pending.get("email"), code, {"name": pending.get("name")})
    if not ok:
        return JSONResponse(status_code=500, content={"status": "error", "message": err})
    return JSONResponse(content={"status": "ok"})

@app.post("/api/worker/app/logout")
async def worker_app_logout():
    resp = JSONResponse(content={"status": "ok"})
    resp.delete_cookie("worker_session")
    resp.delete_cookie("worker_id")
    resp.delete_cookie(auth_service.SESSION_COOKIE)
    resp.delete_cookie("user_email")
    resp.delete_cookie("user_name")
    resp.delete_cookie("role")
    resp.delete_cookie("actor_id")
    return resp

def _worker_app_data(db, worker_id: int):
    features = _features(db, "worker")
    with db.cursor() as cur:
        cur.execute("SELECT id, name, phone, pay_rate_cents, email FROM workers WHERE id = %s;", (worker_id,))
        worker = cur.fetchone()
        if not worker:
            return None
        cur.execute(
            """SELECT id, customer_name, phone, start_time, service_type, worker_status,
                      worker_pay_cents, job_lat, job_lng, job_address
               FROM calendar_events
               WHERE worker_id = %s AND start_time >= NOW() - INTERVAL '30 days'
               ORDER BY start_time DESC;""",
            (worker_id,),
        )
        raw_jobs = cur.fetchall()
        jobs = []
        for j in raw_jobs:
            cur.execute(
                "SELECT action, created_at FROM worker_timeclocks WHERE event_id = %s AND worker_id = %s ORDER BY created_at ASC;",
                (j["id"], worker_id),
            )
            clocks = cur.fetchall()
            clocked_in = bool(clocks and clocks[-1]["action"] in ("in", "update"))
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
                "id": j["id"],
                "customer_name": j["customer_name"],
                "service_type": j["service_type"],
                "worker_status": j["worker_status"],
                "worker_pay_cents": j["worker_pay_cents"],
                "start_time": j["start_time"].isoformat(),
                "job_lat": j["job_lat"],
                "job_lng": j["job_lng"],
                "job_address": j["job_address"],
                "clocked_in": clocked_in,
                "site_seconds": int(site_seconds),
                "has_coords": j["job_lat"] is not None and j["job_lng"] is not None,
            })
        cur.execute(
            "SELECT COALESCE(SUM(worker_pay_cents), 0) AS earned_cents, COUNT(*) AS jobs_done FROM calendar_events WHERE worker_id = %s AND worker_status = 'completed';",
            (worker_id,),
        )
        totals = cur.fetchone()
        cur.execute(
            "SELECT id, period_start, period_end, job_count, gross_cents FROM worker_paychecks WHERE worker_id = %s ORDER BY created_at DESC;",
            (worker_id,),
        )
        paychecks = []
        if features.get("pay"):
            for p in cur.fetchall():
                paychecks.append({
                    "id": p["id"],
                    "job_count": p["job_count"],
                    "gross_cents": p["gross_cents"],
                    "period_start": p["period_start"].isoformat(),
                    "period_end": p["period_end"].isoformat(),
                })
    if not features.get("map"):
        for j in jobs:
            j["job_lat"] = None
            j["job_lng"] = None
            j["has_coords"] = False
            j["job_address"] = None
    if not features.get("jobs"):
        jobs = []
    return {
        "worker": {
            "id": worker["id"],
            "name": worker["name"],
            "phone": worker["phone"],
            "pay_rate_cents": worker["pay_rate_cents"],
        },
        "features": features,
        "totals": {"earned_cents": totals["earned_cents"], "jobs_done": totals["jobs_done"]},
        "jobs": jobs,
        "paychecks": paychecks,
    }

@app.get("/api/worker/app/me")
async def worker_app_me(request: Request, db=Depends(get_db)):
    worker = _require_worker(request, db)
    if not worker:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Not logged in"})
    data = _worker_app_data(db, worker["id"])
    if not data:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Not logged in"})
    return JSONResponse(content={"status": "ok", **data})

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

def _get_setting(db, key, default=""):
    with db.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s;", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def _set_setting(db, key, value):
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, NOW())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();""",
            (key, value),
        )
        db.commit()


def _del_setting(db, key):
    with db.cursor() as cur:
        cur.execute("DELETE FROM app_settings WHERE key = %s;", (key,))
        db.commit()


def _channel_pat(db):
    return (os.getenv("HOSPITABLE_PAT", "") or _get_setting(db, "hospitable_pat") or "").strip()


def _turno_cfg(db):
    token = (os.getenv("TURNO_API_TOKEN", "") or _get_setting(db, "turno_api_token") or "").strip()
    partner_id = (os.getenv("TURNO_PARTNER_ID", "") or _get_setting(db, "turno_partner_id") or "").strip()
    return token, partner_id


def _auto_dispatch_enabled(db) -> bool:
    return _get_setting(db, "auto_dispatch", "true").strip().lower() not in ("0", "false", "no", "off")


def _auto_dispatch_assign(db, event_ids=None) -> int:
    """Assign unassigned jobs to an active worker so crew land on a job already done.

    Honours a preferred crew member when one is configured; otherwise balances
    load by picking the worker with the fewest upcoming assigned jobs. Mirrors
    the manual assign route: the worker's pay rate becomes the job pay and the
    job site address is resolved from the linked property. Returns the number
    of jobs assigned.
    """
    if not _auto_dispatch_enabled(db):
        return 0
    with db.cursor() as cur:
        cur.execute("SELECT id, pay_rate_cents FROM workers WHERE is_active = TRUE ORDER BY id;")
        workers = cur.fetchall()
        if not workers:
            return 0
        by_id = {w["id"]: w for w in workers}
        preferred = None
        pref_raw = _get_setting(db, "auto_dispatch_worker_id", "")
        if pref_raw:
            try:
                preferred = int(pref_raw)
            except (TypeError, ValueError):
                preferred = None
        if event_ids:
            cur.execute(
                "SELECT id FROM calendar_events WHERE id = ANY(%s) AND worker_id IS NULL AND start_time >= NOW() - INTERVAL '1 day' ORDER BY start_time;",
                (list(event_ids),),
            )
        else:
            cur.execute(
                "SELECT id FROM calendar_events WHERE worker_id IS NULL AND start_time >= NOW() - INTERVAL '1 day' ORDER BY start_time;"
            )
        targets = [r["id"] for r in cur.fetchall()]
        if not targets:
            return 0
        cur.execute(
            "SELECT worker_id, COUNT(*) AS n FROM calendar_events WHERE worker_id IS NOT NULL AND start_time >= NOW() - INTERVAL '1 day' GROUP BY worker_id;"
        )
        load = {r["worker_id"]: r["n"] for r in cur.fetchall()}
        assigned = 0
        for event_id in targets:
            if preferred and preferred in by_id:
                pick = by_id[preferred]
            else:
                pick = min(workers, key=lambda w: (load.get(w["id"], 0), w["id"]))
            job_site_address, job_lat, job_lng = _resolve_job_site(db, event_id)
            cur.execute(
                "UPDATE calendar_events SET worker_id = %s, worker_pay_cents = %s, worker_status = 'assigned', job_address = COALESCE(job_address, %s), job_lat = COALESCE(job_lat, %s), job_lng = COALESCE(job_lng, %s) WHERE id = %s AND worker_id IS NULL;",
                (pick["id"], pick["pay_rate_cents"], job_site_address or None, job_lat, job_lng, event_id),
            )
            if cur.rowcount:
                load[pick["id"]] = load.get(pick["id"], 0) + 1
                assigned += 1
        db.commit()
    return assigned


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    pat = _channel_pat(db)
    ch_configured = bool(pat)
    ch_error = ""
    ch_hosp = []
    if ch_configured:
        svc = channel_sync.HospitableService(pat)
        try:
            ch_hosp = svc.get_properties()
        except RuntimeError as e:
            ch_error = str(e)
            ch_hosp = []

    turno_token, turno_partner = _turno_cfg(db)
    turno_configured = bool(turno_token)
    turno_error = ""
    turno_props = []
    if turno_configured:
        try:
            turno_props = channel_sync.TurnoService(turno_token, turno_partner).get_properties()
        except RuntimeError as e:
            turno_error = str(e)
            turno_props = []

    hosp_by_id = {str(hp.get("id") or ""): hp.get("name") or "Unnamed" for hp in ch_hosp}
    turno_by_id = {channel_sync.extract_turno_property(tp)["id"]: channel_sync.extract_turno_property(tp)["name"] for tp in turno_props}
    ch_links = []
    ch_applied = 0
    turno_applied = 0
    ch_last = None
    with db.cursor() as cur:
        cur.execute(
            """SELECT p.id, p.name, p.channel_property_uuid, p.turno_property_id, h.name AS host_name
               FROM properties p LEFT JOIN hosts h ON h.id = p.host_id ORDER BY p.name;"""
        )
        for row in cur.fetchall():
            row["hosp_name"] = hosp_by_id.get(str(row["channel_property_uuid"] or ""), "")
            row["turno_name"] = turno_by_id.get(str(row["turno_property_id"] or ""), "")
            ch_links.append(row)
        cur.execute("SELECT COUNT(*) FROM properties WHERE channel_property_uuid IS NOT NULL;")
        ch_applied = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) FROM properties WHERE turno_property_id IS NOT NULL;")
        turno_applied = cur.fetchone()["count"]
        cur.execute("SELECT status, summary, finished_at FROM channel_sync_logs ORDER BY id DESC LIMIT 1;")
        row = cur.fetchone()
        if row:
            ch_last = {"status": row["status"], "summary": row["summary"], "finished_at": row["finished_at"]}
        cur.execute("SELECT id, name FROM workers WHERE is_active = TRUE ORDER BY name;")
        crews = cur.fetchall()

    webhook_url = str(request.base_url).rstrip("/") + "/api/channels/hospitable/webhook"
    turno_webhook_url = str(request.base_url).rstrip("/") + "/api/channels/turno/webhook"
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "user": {"email": user_email},
            "stripe_configured": stripe_svc.is_configured(),
            "signalwire_phone": os.getenv("SIGNALWIRE_PHONE", ""),
            "channel_configured": ch_configured,
            "channel_error": ch_error,
            "channel_hosp_props": ch_hosp,
            "channel_links": ch_links,
            "channel_linked_count": ch_applied,
            "channel_last_sync": ch_last,
            "channel_webhook_url": webhook_url,
            "turno_configured": turno_configured,
            "turno_error": turno_error,
            "turno_props": turno_props,
            "turno_linked_count": turno_applied,
            "turno_webhook_url": turno_webhook_url,
            "saved": request.query_params.get("saved"),
            "otp_global": _otp_global(db),
            "otp_roles": {r: _otp_role_enabled(db, r) for r in ("admin", "worker", "host")},
            "smtp_ready": documents_service.smtp_configured(_smtp_cfg(db)),
            "auto_dispatch": _auto_dispatch_enabled(db),
            "auto_dispatch_worker_id": _get_setting(db, "auto_dispatch_worker_id", ""),
            "crews": crews,
        },
    )


@app.get("/appearance", response_class=HTMLResponse)
async def appearance_page(request: Request):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    state = site_theme.state(force=True)
    schedule = []
    for skin, (sm, sd), (em, ed) in site_theme.SEASON_SCHEDULE:
        label = site_theme.PRESETS.get(skin, {}).get("label", skin)
        schedule.append((label, f"{sm:02d}-{sd:02d}", f"{em:02d}-{ed:02d}"))
    return templates.TemplateResponse(
        request=request,
        name="appearance.html",
        context={
            "user": {"email": user_email},
            "state": state,
            "presets": site_theme.PRESETS,
            "schedule": schedule,
            "fonts": [("modern", "Modern (Inter)"), ("serif", "Serif (Georgia)"), ("rounded", "Rounded"), ("mono", "Monospace")],
            "promo_code": "FIRSTCLEAN",
        },
    )


@app.post("/appearance", response_class=HTMLResponse)
async def appearance_save(
    request: Request,
    skin: str = Form(""),
    bg_color: str = Form(""),
    accent_color: str = Form(""),
    font: str = Form(""),
    emoji: str = Form(""),
    auto: str = Form("off"),
    promo_first_clean: str = Form("off"),
    db=Depends(get_db),
):
    is_authed, _ = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    auto_on = auto == "on"
    if auto_on:
        site_theme.save_theme(db, auto=True)
    else:
        site_theme.save_theme(
            db,
            skin=str(skin or "").strip() or None,
            bg=str(bg_color or "").strip() or None,
            accent=str(accent_color or "").strip() or None,
            font=str(font or "").strip() or None,
            emoji=str(emoji or "").strip() or None,
            auto=False,
        )
    site_theme.set_promo(db, promo_first_clean == "on")
    return RedirectResponse(url="/appearance?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/labor", response_class=HTMLResponse)
async def labor_page(request: Request):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="labor.html",
        context={"user": {"email": user_email}},
    )


@app.post("/api/settings/channel")
async def save_channel_settings(request: Request, db=Depends(get_db)):
    is_authed, _ = require_auth(request)
    if not is_authed:
        return JSONResponse({"status": "error", "message": "Not authorized"}, status_code=401)
    form = await request.form()
    pat = (form.get("hospitable_pat") or "").strip()
    secret = (form.get("hospitable_webhook_secret") or "").strip()
    if pat:
        _set_setting(db, "hospitable_pat", pat)
    if form.get("clear_hospitable_webhook_secret"):
        _del_setting(db, "hospitable_webhook_secret")
    elif secret:
        _set_setting(db, "hospitable_webhook_secret", secret)

    turno_token = (form.get("turno_api_token") or "").strip()
    turno_partner = (form.get("turno_partner_id") or "").strip()
    if turno_token:
        _set_setting(db, "turno_api_token", turno_token)
    if turno_partner:
        _set_setting(db, "turno_partner_id", turno_partner)
    if form.get("clear_turno_credentials"):
        _del_setting(db, "turno_api_token")
        _del_setting(db, "turno_partner_id")

    link_targets = (("turno_link_", "turno_property_id"), ("link_", "channel_property_uuid"))
    for key, value in form.items():
        for prefix, column in link_targets:
            if not key.startswith(prefix):
                continue
            try:
                pid = int(key[len(prefix):])
            except ValueError:
                break
            val = value.strip() if value else None
            with db.cursor() as cur:
                cur.execute(f"UPDATE properties SET {column} = %s WHERE id = %s;", (val, pid))
                db.commit()
            break
    return RedirectResponse(url="/settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/settings/security")
async def save_security_settings(request: Request, db=Depends(get_db)):
    require_admin(request)
    form = await request.form()
    _set_setting(db, "otp_enabled", "true" if form.get("otp_global") == "on" else "false")
    for role in ("admin", "worker", "host"):
        _set_setting(db, f"otp_role_{role}", "true" if form.get(f"otp_role_{role}") == "on" else "false")
    _set_setting(db, "auto_dispatch", "true" if form.get("auto_dispatch") == "on" else "false")
    default_worker = (form.get("auto_dispatch_worker_id") or "").strip()
    if default_worker:
        _set_setting(db, "auto_dispatch_worker_id", default_worker)
    else:
        _del_setting(db, "auto_dispatch_worker_id")
    return RedirectResponse(url="/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/channels/sync")
async def run_channel_sync(request: Request, db=Depends(get_db)):
    is_authed, _ = require_auth(request)
    if not is_authed:
        return JSONResponse({"status": "error", "message": "Not authorized"}, status_code=401)
    pat = _channel_pat(db)
    if not pat:
        return JSONResponse({"status": "failed", "message": "Hospitable API token not configured."})
    result = channel_sync.sync_hospitable(db, pat)
    try:
        result["dispatched"] = _auto_dispatch_assign(db)
    except Exception as e:
        result["dispatched"] = 0
        result["dispatch_error"] = str(e)
    return JSONResponse(result)


@app.post("/api/channels/hospitable/webhook")
async def hospitable_webhook(request: Request, db=Depends(get_db)):
    body_bytes = await request.body()
    secret = (os.getenv("HOSPITABLE_WEBHOOK_SECRET", "") or _get_setting(db, "hospitable_webhook_secret") or "").strip()
    if secret:
        sig = (
            request.headers.get("Signature")
            or request.headers.get("x-hospitable-signature")
            or request.headers.get("x-signature")
            or ""
        )
        expected = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return JSONResponse({"status": "error", "message": "invalid signature"}, status_code=403)
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        payload = {}
    action = payload.get("action") or ""
    wh_id = str(payload.get("id") or "")
    data = payload.get("data")
    try:
        result = None
        if action.startswith("reservation.") and isinstance(data, dict):
            pat = _channel_pat(db)
            if pat:
                result = channel_sync.sync_hospitable_webhook(db, pat, data)
        elif action.startswith("reservation."):
            pat = _channel_pat(db)
            if pat:
                channel_sync.sync_hospitable(db, pat)
        dispatched = 0
        if action.startswith("reservation."):
            try:
                dispatched = _auto_dispatch_assign(db)
            except Exception as dispatch_err:
                print(f"[DISPATCH] auto-dispatch failed: {dispatch_err}", flush=True)
        summary = f"Webhook {action}"
        details = {"webhook_id": wh_id, "dispatched": dispatched}
        if result is not None:
            details = {**result, "webhook_id": wh_id, "dispatched": dispatched}
        try:
            with db.cursor() as cur:
                cur.execute(
                    """INSERT INTO channel_sync_logs (channel, status, summary, details, started_at, finished_at)
                       VALUES (%s, %s, %s, %s, %s, %s);""",
                    (
                        channel_sync.HOSPITABLE_CHANNEL,
                        (result or {}).get("status", "success"),
                        summary,
                        json.dumps(details, default=str),
                        datetime.now(timezone.utc),
                        datetime.now(timezone.utc),
                    ),
                )
            db.commit()
        except Exception as _log_e:
            db.rollback()
            print(f"WEBHOOK LOG INSERT FAILED ({action}): {_log_e}", flush=True)
        return JSONResponse({"status": "ok", "received": True, "action": action})
    except Exception as e:
        try:
            with db.cursor() as cur:
                cur.execute(
                    """INSERT INTO channel_sync_logs (channel, status, summary, details, started_at, finished_at)
                       VALUES (%s, %s, %s, %s, %s, %s);""",
                    (
                        channel_sync.HOSPITABLE_CHANNEL,
                        "failed",
                        f"Webhook {action}",
                        json.dumps({"error": str(e), "webhook_id": wh_id}, default=str),
                        datetime.now(timezone.utc),
                        datetime.now(timezone.utc),
                    ),
                )
            db.commit()
        except Exception:
            db.rollback()
        return JSONResponse({"status": "ok", "received": True, "action": action, "error": str(e)})


@app.post("/api/channels/turno/sync")
async def run_turno_sync(request: Request, db=Depends(get_db)):
    is_authed, _ = require_auth(request)
    if not is_authed:
        return JSONResponse({"status": "error", "message": "Not authorized"}, status_code=401)
    token, partner_id = _turno_cfg(db)
    if not token:
        return JSONResponse({"status": "failed", "message": "Turno API token not configured."})
    result = channel_sync.sync_turno(db, token, partner_id)
    try:
        result["dispatched"] = _auto_dispatch_assign(db)
    except Exception as e:
        result["dispatched"] = 0
        result["dispatch_error"] = str(e)
    return JSONResponse(result)


@app.post("/api/channels/turno/webhook")
async def turno_webhook(request: Request, db=Depends(get_db)):
    body_bytes = await request.body()
    secret = (os.getenv("TURNO_WEBHOOK_SECRET", "") or _get_setting(db, "turno_webhook_secret") or "").strip()
    if secret:
        sig = (
            request.headers.get("Signature")
            or request.headers.get("x-turno-signature")
            or request.headers.get("x-signature")
            or ""
        )
        expected = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return JSONResponse({"status": "error", "message": "invalid signature"}, status_code=403)
    try:
        payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except Exception:
        payload = {}
    action = payload.get("action") or payload.get("event") or "turno.event"
    token, partner_id = _turno_cfg(db)
    if not token:
        return JSONResponse({"status": "ok", "received": True, "skipped": "Turno API token not configured"})
    try:
        result = channel_sync.sync_turno(db, token, partner_id)
        try:
            result["dispatched"] = _auto_dispatch_assign(db)
        except Exception as dispatch_err:
            result["dispatched"] = 0
            result["dispatch_error"] = str(dispatch_err)
        return JSONResponse({"status": "ok", "received": True, "action": action, "sync": result})
    except Exception as e:
        try:
            with db.cursor() as cur:
                cur.execute(
                    """INSERT INTO channel_sync_logs (channel, status, summary, details, started_at, finished_at)
                       VALUES (%s, %s, %s, %s, %s, %s);""",
                    (
                        channel_sync.TURNO_CHANNEL,
                        "failed",
                        f"Webhook {action}",
                        json.dumps({"error": str(e)}, default=str),
                        datetime.now(timezone.utc),
                        datetime.now(timezone.utc),
                    ),
                )
            db.commit()
        except Exception:
            db.rollback()
        return JSONResponse({"status": "ok", "received": True, "action": action, "error": str(e)})

# --- DOCUMENTS, FORMS & DELIVERY (ADMIN ONLY) ---

def _smtp_cfg(db) -> dict:
    cfg = documents_service.smtp_config_from_env()
    for key in documents_service.SMTP_KEYS:
        if not cfg.get(key):
            stored = _get_setting(db, key)
            if stored:
                cfg[key] = stored
    return cfg


@app.get("/docs", response_class=HTMLResponse)
async def docs_page(request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM generated_documents ORDER BY created_at DESC LIMIT 50;")
        recent = cur.fetchall()
        cur.execute("SELECT * FROM custom_forms ORDER BY created_at DESC;")
        custom_forms_rows = cur.fetchall()
    return templates.TemplateResponse(
        request=request,
        name="docs.html",
        context={
            "user": {"email": request.cookies.get("user_email")},
            "categories": legal_forms.form_categories(),
            "custom_forms": custom_forms_rows,
            "recent": recent,
            "smtp": _smtp_cfg(db),
            "smtp_configured": documents_service.smtp_configured(_smtp_cfg(db)),
        },
    )


@app.get("/docs/new", response_class=HTMLResponse)
async def docs_new(request: Request, db=Depends(get_db)):
    require_admin(request)
    custom_id = request.query_params.get("custom")
    form_key = request.query_params.get("form")
    if custom_id:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM custom_forms WHERE id = %s;", (int(custom_id),))
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Custom form not found")
        try:
            doc = json.loads(row["doc_def"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Custom form definition is corrupt")
        return templates.TemplateResponse(
            request=request, name="doc_form.html",
            context={"user": {"email": request.cookies.get("user_email")}, "doc": doc, "custom_id": row["id"]},
        )
    doc = legal_forms.FORMS_BY_KEY.get(form_key or "")
    if not doc:
        raise HTTPException(status_code=404, detail="Form not found")
    return templates.TemplateResponse(
        request=request, name="doc_form.html",
        context={"user": {"email": request.cookies.get("user_email")}, "doc": doc, "custom_id": None},
    )


@app.post("/docs/generate")
async def docs_generate(request: Request, db=Depends(get_db)):
    require_admin(request)
    data = await request.form()
    fmt = (data.get("format") or "pdf").lower()
    if fmt not in ("pdf", "docx"):
        fmt = "pdf"

    custom_id = data.get("custom_id")
    form_key = data.get("form_key")
    if custom_id:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM custom_forms WHERE id = %s;", (int(custom_id),))
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Custom form not found")
        try:
            doc = json.loads(row["doc_def"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Custom form definition is corrupt")
        title, category = row["title"], row["category"]
    else:
        doc = legal_forms.FORMS_BY_KEY.get(form_key or "")
        if not doc:
            raise HTTPException(status_code=404, detail="Form not found")
        title, category = doc["title"], doc["category"]

    values = {k[2:]: v for k, v in data.items() if k.startswith("f_")}
    raw = documents_service.render_document(doc, values, fmt)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "document"
    file_name = f"{slug}.{fmt}"

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO generated_documents (title, category, file_name, file_type, file_data, values_json) VALUES (%s, %s, %s, %s, %s, %s);",
            (title, category, file_name, fmt, raw, json.dumps(values)),
        )
        db.commit()
    return RedirectResponse(url="/docs?gen=1", status_code=303)


@app.get("/docs/view/{doc_id}", response_class=Response)
async def docs_view(doc_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM generated_documents WHERE id = %s;", (doc_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")
    media = "application/pdf" if row["file_type"] == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    headers = {"Content-Disposition": f"inline; filename=\"{row['file_name']}\""}
    return Response(content=bytes(row["file_data"]), media_type=media, headers=headers)


@app.get("/docs/download/{doc_id}", response_class=Response)
async def docs_download(doc_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM generated_documents WHERE id = %s;", (doc_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")
    media = "application/pdf" if row["file_type"] == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    headers = {"Content-Disposition": f"attachment; filename=\"{row['file_name']}\""}
    return Response(content=bytes(row["file_data"]), media_type=media, headers=headers)


@app.post("/docs/send/{doc_id}")
async def docs_send(doc_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM generated_documents WHERE id = %s;", (doc_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")

    data = await request.form()
    email_to = (data.get("email_to") or "").strip()
    sms_to = (data.get("sms_to") or "").strip()
    subject = (data.get("subject") or "").strip() or f"Document: {row['title']}"
    cfg = _smtp_cfg(db)
    feedback = []

    if email_to:
        if not documents_service.smtp_configured(cfg):
            feedback.append("Email skipped — SMTP not configured.")
        else:
            try:
                await documents_service.send_email(
                    cfg, email_to, subject, f"Please find the attached document: {row['file_name']}.",
                    bytes(row["file_data"]), row["file_name"],
                )
                with db.cursor() as cur:
                    cur.execute("UPDATE generated_documents SET sent_email = %s WHERE id = %s;", (email_to, doc_id))
                    db.commit()
                feedback.append(f"Emailed to {email_to}")
            except Exception as e:
                feedback.append(f"Email failed: {e}")

    if sms_to:
        if signalwire.is_configured():
            base = _site_base(request)
            link = f"{base}/docs/view/{row['id']}"
            try:
                ok = signalwire.send_sms(sms_to, f"Your document '{row['title']}' is ready to view/download: {link}")
                feedback.append("Text sent" if ok else "Text send failed")
                if ok:
                    with db.cursor() as cur:
                        cur.execute("UPDATE generated_documents SET sent_sms = %s WHERE id = %s;", (sms_to, doc_id))
                        db.commit()
            except Exception as e:
                feedback.append(f"Text failed: {e}")
        else:
            feedback.append("Text skipped — SignalWire not configured.")

    return RedirectResponse(url=f"/docs?delivered={'; '.join(feedback)}", status_code=303)


@app.post("/docs/custom")
async def docs_custom(request: Request, db=Depends(get_db)):
    require_admin(request)
    data = await request.form()
    title = (data.get("title") or "").strip()
    category = (data.get("category") or "Legal").strip()
    raw_def = (data.get("doc_def") or "").strip()
    if not title or not raw_def:
        raise HTTPException(status_code=400, detail="Title and form definition are required")
    try:
        parsed = json.loads(raw_def)
        assert isinstance(parsed, dict) and parsed.get("title")
    except Exception:
        raise HTTPException(status_code=400, detail="Form definition must be valid JSON with a title")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO custom_forms (title, category, doc_def) VALUES (%s, %s, %s);",
            (parsed.get("title", title), category, json.dumps(parsed)),
        )
        db.commit()
    return RedirectResponse(url="/docs?custom_saved=1", status_code=303)


@app.post("/api/settings/email")
async def save_email_settings(request: Request, db=Depends(get_db)):
    require_admin(request)
    form = await request.form()
    for key in documents_service.SMTP_KEYS:
        val = (form.get(key) or "").strip()
        _set_setting(db, key, val)
    return RedirectResponse(url="/docs?saved=1", status_code=303)


# --- Back-log ---------------------------------------------------------------
# Record real work that happened before it was entered, with its true date, so
# revenue, bookings, and host relationships reflect reality.
def _backlog_when(value: str) -> datetime:
    try:
        d = date.fromisoformat((value or "").strip())
    except ValueError:
        d = date.today()
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=APP_TZ)


def _backlog_cents(value: str) -> int:
    try:
        return max(0, int(round(float(value) * 100)))
    except (TypeError, ValueError):
        return 0


@app.get("/backlog", response_class=HTMLResponse)
async def backlog_page(request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute(
            "SELECT l.id, l.tx_type, l.description, l.amount_cents, l.created_at, "
            "b.customer_name, b.service_type "
            "FROM ledger_entries l "
            "LEFT JOIN bookings b ON b.id = l.ref_id AND l.ref_type = 'backlog_job' "
            "WHERE l.ref_type IN ('backlog_job', 'backlog_expense') "
            "ORDER BY l.created_at DESC LIMIT 200;"
        )
        entries = cur.fetchall()
        cur.execute(
            "SELECT COALESCE(SUM(amount_cents) FILTER (WHERE tx_type = 'income'), 0) AS income, "
            "COALESCE(SUM(amount_cents) FILTER (WHERE tx_type = 'expense'), 0) AS expense "
            "FROM ledger_entries WHERE ref_type IN ('backlog_job', 'backlog_expense');"
        )
        totals = cur.fetchone()
    return templates.TemplateResponse(
        request=request, name="backlog.html",
        context={"entries": entries, "totals": totals, "today": date.today().isoformat()},
    )


@app.post("/api/backlog/job")
async def backlog_job(
    request: Request,
    entry_date: str = Form(""),
    customer_name: str = Form(""),
    service_type: str = Form(""),
    description: str = Form(""),
    amount_dollars: str = Form(""),
    host_name: str = Form(""),
    property_name: str = Form(""),
    property_address: str = Form(""),
    db=Depends(get_db),
):
    require_admin(request)
    when = _backlog_when(entry_date)
    cents = _backlog_cents(amount_dollars)
    if cents <= 0:
        return RedirectResponse(url="/backlog?err=amount", status_code=303)
    customer = customer_name or "Back-logged customer"
    with db.cursor() as cur:
        cur.execute("INSERT INTO customers (name, source, created_at) VALUES (%s, 'backlog', %s);",
                    (customer, when))
        cur.execute(
            "INSERT INTO bookings (customer_name, service_type, status, created_at) "
            "VALUES (%s, %s, 'completed', %s) RETURNING id;",
            (customer, service_type or "Turnover", when),
        )
        booking_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO ledger_entries (tx_type, ref_type, ref_id, description, amount_cents, created_at) "
            "VALUES ('income', 'backlog_job', %s, %s, %s, %s) "
            "ON CONFLICT (ref_type, ref_id) DO NOTHING;",
            (booking_id, description or f"{service_type or 'Job'} — {customer}", cents, when),
        )
        if host_name:
            cur.execute("SELECT id FROM hosts WHERE name = %s LIMIT 1;", (host_name,))
            row = cur.fetchone()
            if row:
                host_id = row["id"]
            else:
                cur.execute(
                    "INSERT INTO hosts (name, property_name, created_at) VALUES (%s, %s, %s) RETURNING id;",
                    (host_name, property_name or None, when),
                )
                host_id = cur.fetchone()["id"]
            if property_name or property_address:
                cur.execute(
                    "INSERT INTO properties (host_id, name, address, created_at) VALUES (%s, %s, %s, %s);",
                    (host_id, property_name or "Managed property", property_address, when),
                )
        db.commit()
    return RedirectResponse(url="/backlog?ok=job", status_code=303)


@app.post("/api/backlog/expense")
async def backlog_expense(
    request: Request,
    entry_date: str = Form(""),
    description: str = Form(""),
    amount_dollars: str = Form(""),
    db=Depends(get_db),
):
    require_admin(request)
    when = _backlog_when(entry_date)
    cents = _backlog_cents(amount_dollars)
    if cents <= 0:
        return RedirectResponse(url="/backlog?err=amount", status_code=303)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO ledger_entries (tx_type, ref_type, description, amount_cents, created_at) "
            "VALUES ('expense', 'backlog_expense', %s, %s, %s);",
            (description or "Back-logged expense", cents, when),
        )
        db.commit()
    return RedirectResponse(url="/backlog?ok=expense", status_code=303)


@app.post("/api/backlog/{entry_id}/delete")
async def backlog_delete(entry_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT ref_type, ref_id FROM ledger_entries WHERE id = %s;", (entry_id,))
        row = cur.fetchone()
        cur.execute("DELETE FROM ledger_entries WHERE id = %s;", (entry_id,))
        if row and row.get("ref_type") == "backlog_job" and row.get("ref_id"):
            cur.execute("DELETE FROM bookings WHERE id = %s;", (row["ref_id"],))
        db.commit()
    return RedirectResponse(url="/backlog?ok=deleted", status_code=303)


# --- ACCESS CONTROL (ADMIN) ---

def _save_perms(db, role: str, form):
    features = auth_service.permissions_defaults(role)
    for key in features:
        features[key] = (form.get(f"feature_{role}_{key}") == "on")
    _set_setting(db, f"perms_{role}", json.dumps(features))


@app.get("/access", response_class=HTMLResponse)
async def access_page(request: Request, db=Depends(get_db)):
    require_admin(request)
    is_authed, user_email = require_auth(request)
    return templates.TemplateResponse(
        request=request,
        name="access.html",
        context={
            "user": {"email": user_email},
            "saved": request.query_params.get("saved"),
            "worker_features": _features(db, "worker"),
            "host_features": _features(db, "host"),
            "worker_catalog": auth_service.WORKER_FEATURES,
            "host_catalog": auth_service.HOST_FEATURES,
        },
    )


@app.post("/api/access")
async def save_access(request: Request, db=Depends(get_db)):
    require_admin(request)
    form = await request.form()
    _save_perms(db, "worker", form)
    _save_perms(db, "host", form)
    return RedirectResponse(url="/access?saved=1", status_code=status.HTTP_303_SEE_OTHER)


# --- ACCOUNTING (ADMIN ONLY — QUICKBOOKS-STYLE) ---

def _bank_cfg() -> dict:
    return {
        "holder": os.getenv("CHECK_HOLDER", "Broom Service"),
        "address": os.getenv("CHECK_HOLDER_ADDRESS", ""),
        "city_state_zip": os.getenv("CHECK_CITY_STATE_ZIP", ""),
        "routing": os.getenv("CHECK_ROUTING", "000000000"),
        "account": os.getenv("CHECK_ACCOUNT", "000000000000"),
    }


def _next_check_number(db) -> int:
    try:
        return int(_get_setting(db, "check_counter") or os.getenv("CHECK_START_NUMBER", "1001"))
    except (TypeError, ValueError):
        return 1001


def _bump_check_counter(db, value: int):
    _set_setting(db, "check_counter", str(value + 1))


@app.get("/accounting", response_class=HTMLResponse)
async def accounting_page(request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(amount_cents) FILTER (WHERE tx_type = 'income'), 0) AS income, "
            "COALESCE(SUM(amount_cents) FILTER (WHERE tx_type = 'expense'), 0) AS expense FROM ledger_entries;"
        )
        totals = cur.fetchone()
        net = (totals["income"] or 0) - (totals["expense"] or 0)
        cur.execute("SELECT * FROM ledger_entries ORDER BY created_at DESC LIMIT 200;")
        ledger = cur.fetchall()
        cur.execute(
            "SELECT p.*, w.name AS worker_name, w.email AS worker_email, w.phone AS worker_phone "
            "FROM worker_paychecks p JOIN workers w ON w.id = p.worker_id ORDER BY p.created_at DESC LIMIT 100;"
        )
        paychecks = cur.fetchall()
        cur.execute("SELECT * FROM checks ORDER BY created_at DESC LIMIT 50;")
        checks = cur.fetchall()

    return templates.TemplateResponse(
        request=request,
        name="accounting.html",
        context={
            "user": {"email": request.cookies.get("user_email")},
            "income": totals["income"] or 0,
            "expense": totals["expense"] or 0,
            "net": net,
            "ledger": ledger,
            "paychecks": paychecks,
            "checks": checks,
            "bank": _bank_cfg(),
        },
    )


@app.get("/accounting/stub/{paycheck_id}/pdf", response_class=Response)
async def accounting_stub_pdf(paycheck_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute(
            "SELECT p.*, w.name AS worker_name FROM worker_paychecks p JOIN workers w ON w.id = p.worker_id WHERE p.id = %s;",
            (paycheck_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Paycheck not found")
    stub = {
        "id": row["id"],
        "worker_name": row["worker_name"],
        "period_start": row["period_start"].strftime("%b %d, %Y"),
        "period_end": row["period_end"].strftime("%b %d, %Y"),
        "job_count": row["job_count"],
        "gross_cents": row["gross_cents"],
        "pay_date": date.today().strftime("%b %d, %Y"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fed_cents": 0,
        "state_cents": 0,
        "fica_cents": 0,
        "other_cents": 0,
    }
    pdf = documents_service.render_pay_stub_pdf(stub)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=\"stub-{row['id']}.pdf\""})


@app.get("/accounting/check/{paycheck_id}/pdf", response_class=Response)
async def accounting_check_pdf(paycheck_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute(
            "SELECT p.*, w.name AS worker_name FROM worker_paychecks p JOIN workers w ON w.id = p.worker_id WHERE p.id = %s;",
            (paycheck_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Paycheck not found")

    with db.cursor() as cur:
        cur.execute("SELECT * FROM checks WHERE paycheck_id = %s;", (paycheck_id,))
        existing = cur.fetchone()

    bank = _bank_cfg()
    if existing:
        check_number = existing["check_number"]
        amount = existing["amount_cents"]
    else:
        check_number = f"{_next_check_number(db):05d}"
        _bump_check_counter(db, _next_check_number(db))
        amount = row["gross_cents"]

    check = {
        "check_number": check_number,
        "payee": row["worker_name"],
        "amount_cents": amount,
        "date": date.today().strftime("%m/%d/%Y"),
        "memo": f"Payroll — {row['period_start'].strftime('%b %d')} to {row['period_end'].strftime('%b %d, %Y')}",
        "stub": [
            (f"GROSS — {row['job_count']} jobs", row["gross_cents"]),
            ("NET PAY", row["gross_cents"]),
        ],
        "bank": bank,
    }
    pdf = documents_service.render_check_pdf(check)

    if not existing:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO checks (check_number, paycheck_id, payee, amount_cents, memo, status, file_data) VALUES (%s, %s, %s, %s, %s, 'printed', %s);",
                (check_number, paycheck_id, check["payee"], amount, check["memo"], pdf),
            )
            db.commit()
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=\"check-{check_number}.pdf\""})


@app.get("/accounting/export")
async def accounting_export(request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM ledger_entries ORDER BY created_at DESC;")
        rows = cur.fetchall()
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(["id", "type", "description", "amount_cents", "date"])
    for r in rows:
        writer.writerow([r["id"], r["tx_type"], r["description"], r["amount_cents"], r["created_at"].isoformat()])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=\"ledger.csv\""})


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
    customer_name: str = Form(""),
    phone: str = Form(""),
    start_time: str = Form(...),
    service_type: str = Form(...),
    property_id: str = Form(""),
    db=Depends(get_db)
):
    parsed_start = datetime.fromisoformat(start_time)
    parsed_end = parsed_start + timedelta(hours=1)

    host_id = None
    prop_id = None
    host = None
    if property_id:
        property_id = property_id.strip()
        if property_id.startswith("host_"):
            try:
                host_id = int(property_id.split("host_", 1)[1])
            except (ValueError, IndexError):
                host_id = None
        else:
            try:
                prop_id = int(property_id)
            except (ValueError, TypeError):
                prop_id = None

    with db.cursor() as cur:
        if host_id is not None:
            cur.execute("SELECT id, name, email, phone FROM hosts WHERE id = %s;", (host_id,))
            host = cur.fetchone()
            if host is None:
                host_id = None
        if prop_id is not None:
            cur.execute("SELECT id, host_id FROM properties WHERE id = %s;", (prop_id,))
            prop = cur.fetchone()
            if prop:
                prop_id = prop["id"]
                if not host_id and prop["host_id"]:
                    host_id = prop["host_id"]
            else:
                prop_id = None

    if host:
        customer_name = host["name"] or customer_name
        phone = host["phone"] or phone
    elif not customer_name.strip():
        customer_name = "Unknown host"

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM calendar_events WHERE start_time < %s AND end_time > %s;", (parsed_end, parsed_start))
        if cur.fetchone()['count'] > 0:
            raise HTTPException(status_code=400, detail="Requested timeframe collides with an active event.")
        cur.execute(
            "INSERT INTO calendar_events (customer_name, phone, start_time, end_time, service_type, host_id, property_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;",
            (customer_name, phone, parsed_start, parsed_end, service_type, host_id, prop_id)
        )
        event_id = cur.fetchone()['id']
        amount_cents = stripe_svc.get_price(service_type)
        cur.execute(
            "UPDATE calendar_events SET amount_cents = %s WHERE id = %s;",
            (amount_cents, event_id)
        )
        db.commit()

    return RedirectResponse(url="/dashboard?booked=1", status_code=303)

def _first_clean_coupon(db, event_id, customer_name, phone):
    """When the FIRSTCLEAN promo is armed and this is the customer's first-ever
    booking, return a 100%-off Stripe coupon id. Returns None otherwise."""
    if not site_theme.state().get("promo_on"):
        return None
    with db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM calendar_events WHERE payment_status = 'paid' "
            "AND id <> %s AND (customer_name = %s OR phone = %s);",
            (event_id, customer_name, phone),
        )
        if cur.fetchone()["n"] > 0:
            return None
        cur.execute("SELECT value FROM app_settings WHERE key = 'stripe_coupon_first_clean';")
        row = cur.fetchone()
    if row:
        return row["value"]
    try:
        coupon = stripe.Coupon.create(name="First Clean Free", percent_off=100, duration="once")
    except Exception:
        return None
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('stripe_coupon_first_clean', %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;",
            (coupon.id,),
        )
        db.commit()
    return coupon.id

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
        customer_email = ""
        if event.get("host_id"):
            with db.cursor() as cur:
                cur.execute("SELECT email FROM hosts WHERE id = %s;", (event["host_id"],))
                host_row = cur.fetchone()
            if host_row and host_row["email"]:
                customer_email = host_row["email"]
        checkout_url = stripe_svc.create_checkout_session(
            event_id=event["id"],
            customer_name=event["customer_name"],
            customer_email=customer_email,
            service_type=event["service_type"],
            start_time=event["start_time"],
            discount_coupon=_first_clean_coupon(db, event["id"], event["customer_name"], event["phone"]),
        )
        return JSONResponse(content={"url": checkout_url})
    except ValueError as e:
        return JSONResponse(
            content={"error": f'No price is set for "{event["service_type"]}" yet — add it in Settings › Prices before sending a payment link.'},
            status_code=400,
        )
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
        payment_intent = session.get("payment_intent")
        amount_total = session.get("amount_total", 0)
        currency = session.get("currency", "usd")
        lead_id = session.get("metadata", {}).get("lead_id")

        if lead_id:
            # Construction project deposit
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE leads SET deposit_status = 'paid', stripe_session_id = %s, deposit_cents = %s, "
                    "status = CASE WHEN status IN ('completed','in_progress') THEN status ELSE 'deposit' END "
                    "WHERE id = %s;",
                    (session.get("id"), amount_total, int(lead_id)),
                )
                cur.execute(
                    "INSERT INTO payments (lead_id, stripe_session_id, stripe_payment_intent_id, amount_cents, currency, company) "
                    "SELECT %s, %s, %s, %s, %s, 'construction' "
                    "WHERE NOT EXISTS (SELECT 1 FROM payments WHERE stripe_session_id = %s);",
                    (int(lead_id), session.get("id"), payment_intent, amount_total, currency, session.get("id")),
                )
                db.commit()
            return Response(content='{"received": true}', media_type="application/json")

        event_id = session.get("metadata", {}).get("event_id")
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
                cur.execute("SELECT customer_name, service_type FROM calendar_events WHERE id = %s;", (int(event_id),))
                evt = cur.fetchone()
                if evt:
                    host_name = evt["customer_name"] or "Host"
                    cur.execute(
                        "INSERT INTO ledger_entries (tx_type, ref_type, ref_id, description, amount_cents) "
                        "VALUES ('income', 'booking', %s, %s, %s) "
                        "ON CONFLICT (ref_type, ref_id) DO NOTHING;",
                        (int(event_id), f"Host payment — {host_name} ({evt['service_type'] or 'service'})", amount_total),
                    )
                db.commit()

    return Response(content='{"received": true}', media_type="application/json")

@app.post("/api/payments/connect-webhook")
async def payments_connect_webhook(request: Request, db=Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe_svc.construct_connect_event(payload, sig)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connect webhook failed: {e}")
    if isinstance(event, dict):
        etype = event.get("type")
        eobj = (event.get("data") or {}).get("object") or {}
    else:
        etype = event.type
        eobj = event.data.object if event.data else {}
    if etype in ("account.updated",):
        account_id = eobj.get("id")
        payouts_enabled = bool(eobj.get("payouts_enabled"))
        with db.cursor() as cur:
            cur.execute("UPDATE crew SET bank_status = %s WHERE stripe_account_id = %s;",
                        ("connected" if payouts_enabled else "pending", account_id))
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
<Response><Message to="{From}">{xml_escape(ai_reply)}</Message></Response>"""
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

    greeting = "Thank you for calling Broom Service! Our automated assistant is ready to help with bookings, house rules, or checkout instructions. How can I assist you today?"
    twiml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Matthew-Neural">{greeting}</Say>
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
    <Say voice="Polly.Matthew-Neural">{ai_response}</Say>
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

# --- SWML live voice agent (SignalWire AI conversation with full site knowledge) ---

VOICE_AGENT_PROMPT = """\
You are the Broom Service voice assistant, answering calls 24/7 for Broom Service.
Be the calm laid-back cool guy on the phone: chill, friendly, casual. Short sentences, simple words, speak the way a real person talks — contractions, no corporate jargon, never robotic, never scripted. Stay helpful and professional, but relaxed. Match the caller's energy.

ABOUT THE COMPANY
- Broom Service is a short-term rental (STR) turnover-cleaning and co-hosting management company for Airbnb, Vrbo, and direct-booking properties.
- Website: https://bizstackperks.com. Assistant number, call or text 24/7: +1 (757) 846-9275. Email: hello@bizstackperks.com.
- Core promises: zero upfront cost to hosts (the guest funds the operational fee at booking), no long-term contracts (unbundled modular services), 24/7 AI assistant, photo-verified cleaning with time-stamped room photos, calendar sync, and secure Stripe payments collected from guests at checkout.

SERVICES & PRICING (confirm exact figures at booking time)
Cleaning services, flat rates, funded by the guest:
- Turnover Cleaning: $120. Standard between-guest reset: trash removal, all linens, bathroom and kitchen sanitizing, floors, surface wipe-down, supply restock, photo verification.
- Deep Cleaning: $200. Intensive top-to-bottom clean.
- Linen Restock: $50. Fresh linen sets and supply restock.
- Inspection: $75. Pre- or post-stay quality inspection.
Co-hosting and management, percentage of gross nightly bookings:
- Digital Co-Hosting: 10% to 15%. Includes 24/7 AI assistant support, dynamic pricing, review escalation, guest messaging.
- Full-Service Management: 20% to 30%. Complete turn-key: cleaning, co-hosting, maintenance and vendor dispatch, multi-channel distribution.

BOOKING FLOW
1. Collect the caller's name, the service (Turnover Cleaning, Deep Cleaning, Linen Restock, Inspection, or a co-hosting package), and the desired date and time.
2. Restate and confirm the date, time, service, and name before confirming, like a human would.
3. If the requested time is taken, proactively offer the nearest open window.
4. After confirming, create the booking and send the guest a secure Stripe payment link so the guest pays for the service at booking.
- The booking belongs to the caller's phone number; use it to look up existing bookings.
- Never invent prices, policies, or availability. Use tools first; if unsure, say the team will follow up by text.

HOUSE RULES (for guests): check-in usually 3:00 to 4:00 PM, checkout 10:00 to 11:00 AM; no smoking indoors, no parties, quiet hours around 10 PM to 8 AM, no unauthorized pets, respect maximum occupancy, leave access as instructed, bag trash, report damage.

5-STAR CLEANING STANDARD (if asked): remove all trash, strip and replace all linens, sanitize bathrooms and kitchen, care for floors, dust surfaces, restock supplies, stage the space, take time-stamped photos of every room, and report any damage or issues immediately.

GENERAL
- Direct callers to text +1 (757) 846-9275, visit https://bizstackperks.com, or use the free rental analysis form on the home page.
- Never expose internal data, credentials, or secrets. If a caller is distressed or requests an emergency, give a calm, brief reply and offer to follow up by text."""

VOICE_TOOL_URL = (os.getenv("APP_BASE_URL", "https://bizstackperks.com") or "") + "/api/voice/tool"


def _swaig_parameters(props: dict, required: list, notes: str = ""):
    return {
        "type": "object",
        "required": required,
        "properties": props,
        "$comment": notes,
        "additionalProperties": False,
    }


@app.api_route("/voice.swml", methods=["GET", "POST"])
@app.api_route("/voice-app.swml", methods=["GET", "POST"])
async def voice_swml():
    swml = {
        "version": "1.0.0",
        "sections": {
            "main": [
                {"answer": {}},
                {
                    "ai": {
                        "prompt": {"text": VOICE_AGENT_PROMPT},
                        "languages": [
                            {
                                "name": "English",
                                "code": "en-US",
                                "voice": "elevenlabs.charlie",
                                "speech_fillers": ["one moment please,", "hmm...", "let's see,"],
                                "params": {"stability": 0.6, "similarity": 0.85},
                            }
                        ],
                        "params": {"ai_model": "gpt-4.1", "temperature": 0.7, "frequency_penalty": 0.3},
                        "post_prompt_url": (os.getenv("APP_BASE_URL", "https://bizstackperks.com") or "") + "/api/voice/debug",
                        "pronounce": [
                            {"replace": "Broom Service", "with": "broom service", "ignore_case": True},
                            {"replace": "Vrbo", "with": "virbo", "ignore_case": True},
                            {"replace": "RevPAR", "with": "rev par", "ignore_case": True},
                            {"replace": "Airbnb", "with": "air bnb", "ignore_case": True},
                        ],
                        "SWAIG": {
                            "defaults": {
                                "web_hook_url": VOICE_TOOL_URL,
                            },
                            "functions": [
                                {
                                    "function": "check_booking_availability",
                                    "description": (
                                        "Check whether a requested start time is open for a cleaning "
                                        "operation. Use when a caller wants to know if a date/time is "
                                        "available. Returns open/conflict status and the nearest open "
                                        "slots around the requested time."
                                    ),
                                    "parameters": _swaig_parameters(
                                        {"start_time": {
                                            "type": "string",
                                            "description": "ISO-8601 local datetime, e.g. 2026-09-18T14:00:00.",
                                        }},
                                        ["start_time"],
                                        "Convert the caller's requested date/time to ISO-8601 US Eastern first.",
                                    ),
                                },
                                {
                                    "function": "create_booking",
                                    "description": (
                                        "Create a confirmed booking and generate the guest's secure Stripe "
                                        "payment link. ONLY use after the guest has explicitly confirmed their "
                                        "name, service type, date, and time."
                                    ),
                                    "parameters": _swaig_parameters(
                                        {
                                            "customer_name": {"type": "string", "description": "Full name of the guest."},
                                            "phone": {"type": "string", "description": "Caller's phone number in E.164, e.g. +17558469275."},
                                            "service_type": {"type": "string", "description": "One of: Turnover Cleaning, Deep Cleaning, Linen Restock, Inspection."},
                                            "start_time": {"type": "string", "description": "ISO-8601 local datetime, e.g. 2026-09-18T14:00:00."},
                                        },
                                        ["customer_name", "phone", "service_type", "start_time"],
                                    ),
                                },
                                {
                                    "function": "lookup_bookings",
                                    "description": "Look up a guest's bookings by phone number, newest first.",
                                    "parameters": _swaig_parameters(
                                        {"phone": {"type": "string", "description": "Phone used for the booking, e.g. +17558469275."}},
                                        ["phone"],
                                    ),
                                },
                                {
                                    "function": "register_customer",
                                    "description": "Save a new customer or prospect record with their contact details.",
                                    "parameters": _swaig_parameters(
                                        {
                                            "name": {"type": "string", "description": "Customer name."},
                                            "email": {"type": "string", "description": "Optional email."},
                                            "phone": {"type": "string", "description": "Optional phone number."},
                                        },
                                        ["name"],
                                    ),
                                },
                                {
                                    "function": "send_sms_message",
                                    "description": "Send a text message (e.g. a Stripe payment link) to a phone number. Use after creating a booking so the guest receives the payment link.",
                                    "parameters": _swaig_parameters(
                                        {
                                            "to": {"type": "string", "description": "Destination phone number in E.164 format."},
                                            "body": {"type": "string", "description": "Text content of the message."},
                                        },
                                        ["to", "body"],
                                    ),
                                },
                            ],
                        },
                    }
                },
            ]
        },
    }
    return JSONResponse(content=swml)


VOICE_ALLOWED_TOOLS = {
    "check_booking_availability",
    "create_booking",
    "lookup_bookings",
    "register_customer",
    "send_sms_message",
}


def _swaig_tool_response_text(name: str, result) -> str:
    if isinstance(result, dict) and result.get("ok") is False:
        return str(result.get("error") or result.get("message") or "That didn't work — the team will follow up by text.")

    if name == "check_booking_availability":
        if result.get("available") is False:
            slots = result.get("nearest_open_slots") or []
            if slots:
                return f"{result.get('message', 'That time is booked.')} Nearest open times: {', '.join(slots)}."
            return result.get("message", "That time is booked.")
        return result.get("message", "That time is open.")
    if name == "create_booking":
        if result.get("ok"):
            line = (
                f"Booking recorded for {result['customer_name']} — {result['service_type']} on "
                f"{result.get('start_time')}."
            )
            if result.get("payment_url"):
                line += f" Secure payment link: {result['payment_url']}."
            elif result.get("stripe_error"):
                line += " The Stripe payment link could not be generated right now; the team will follow up by text."
            return line
        return str(result.get("message") or result.get("error") or "The booking couldn't be completed.")
    if name == "lookup_bookings":
        bookings = result.get("bookings") or []
        if not bookings:
            return f"No bookings found for {result.get('phone', 'that number')}."
        lines = [f"{b.get('service_type')} on {b.get('start_time')} — {b.get('payment_status') or 'unpaid'}"]
        return "Bookings: " + "; ".join(lines)
    if name == "register_customer":
        return f"New customer profile saved."
    if name == "send_sms_message":
        if result.get("ok"):
            return "Text sent."
        return str(result.get("error") or "Text couldn't be sent.")
    return json.dumps(result, default=str, ensure_ascii=False)


@app.api_route("/api/voice/tool", methods=["POST"])
async def voice_tool(request: Request, db=Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    function_name = payload.get("function") or payload.get("function_name")
    argument = payload.get("argument")
    if isinstance(argument, dict):
        args = None
        parsed = argument.get("parsed")
        if isinstance(parsed, list) and parsed:
            args = parsed[0] if isinstance(parsed[0], dict) else None
        if args is None:
            raw = (argument.get("raw") or "").strip()
            if raw:
                try:
                    loaded = json.loads(raw)
                    args = loaded if isinstance(loaded, dict) else None
                except Exception:
                    args = None
    else:
        args = None

    print(
        f"VOICE-TOOL call={function_name} args={json.dumps(args) if args is not None else None} "
        f"call_id={payload.get('call_id') or payload.get('ai_session_id')}",
        flush=True,
    )

    if not function_name or function_name not in VOICE_ALLOWED_TOOLS:
        return JSONResponse({"response": "I'm not able to do that yet, but the team will follow up by text."})

    if not isinstance(args, dict):
        return JSONResponse({"response": "I didn't catch the details. Could you repeat the date and time?"})

    try:
        handlers = build_tool_handlers(db, stripe_svc)
        result = handlers[function_name](**args)
    except TypeError as e:
        print(f"⚠️ VOICE-TOOL bad args: {e}", flush=True)
        return JSONResponse({"response": "I didn't catch all the details. Could you repeat the date and time?"})
    except Exception as e:
        print(f"⚠️ VOICE-TOOL error: {e}", flush=True)
        return JSONResponse({"response": "That hit a snag — I'll have the team follow up by text."})

    text = _swaig_tool_response_text(function_name, result)
    print(f"VOICE-TOOL result={text[:400]}", flush=True)
    return JSONResponse({"response": text})

@app.api_route("/api/voice/debug", methods=["GET", "POST"])
async def voice_debug(request: Request, db=Depends(get_db)):
    raw = await request.body()
    body = None
    try:
        data = json.loads(raw or b"{}")
    except Exception:
        data = {"raw": raw.decode("utf-8", "replace")[:4000]}
    try:
        form = dict(await request.form())
        if form:
            data = {"form": form, "json": data}
            body = form
    except Exception:
        pass
    if body is None:
        body = data
    print(f"VOICE-DEBUG {json.dumps(data, default=str)[:4000]}", flush=True)

    actions = body.get("action")
    if actions and "fetch_conversation" in (actions if isinstance(actions, list) else [actions]):
        return JSONResponse({"conversation_summary": None})

    call_id = body.get("call_id") or body.get("ai_session_id") or body.get("conversation_id")
    agent = (body.get("summary") or "").strip()
    call_log = body.get("call_log")
    if agent or call_log:
        transcript = None
        if isinstance(call_log, list):
            lines = []
            for entry in call_log:
                if not isinstance(entry, dict):
                    continue
                role = entry.get("role")
                content = entry.get("content")
                if not content:
                    continue
                label = {"assistant": "AI", "user": "Caller", "tool": "Tool", "system": "System"}.get(role, str(role))
                lines.append(f"{label}: {content}")
            transcript = "\n".join(lines)
        message = agent or transcript or "Voice call (no transcript)"
        if call_id:
            message = f"[session {call_id}]\n" + message
        try:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                    "VALUES ('inbound', 'voice', %s, %s, %s);",
                    (
                        body.get("from") or body.get("From") or body.get("caller_id_num") or "unknown",
                        body.get("to") or body.get("To") or os.getenv("SIGNALWIRE_PHONE", "+17578469275"),
                        message,
                    ),
                )
            db.commit()
        except Exception as e:
            print(f"⚠️ VOICE-DEBUG log insert failed: {e}", flush=True)
    return Response(content="", status_code=204)

# --- Copilot (owner AI operator) ---

@app.get("/copilot", response_class=HTMLResponse)
async def copilot_page(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    has_key = bool(os.getenv("OPENAI_API_KEY"))
    return templates.TemplateResponse(request=request, name="copilot.html", context={
        "user": {"email": user_email},
        "has_key": has_key,
    })

@app.post("/api/copilot")
async def copilot_chat(request: Request, message: str = Form(...), db=Depends(get_db)):
    require_admin(request)
    msg = (message or "").strip()
    if not msg:
        return JSONResponse({"reply": "Send me a message."})
    if not os.getenv("OPENAI_API_KEY"):
        return JSONResponse({"reply": "OPENAI_API_KEY is not configured yet, so I can't respond."})
    try:
        agent = BusinessAIAgent(tool_handlers=build_tool_handlers(db, stripe_svc), subset="copilot")
        reply = agent.process_inbound_text(msg)
    except Exception as e:
        print(f"⚠️ Copilot error: {e}")
        reply = "Sorry — something tripped me up on that one. Try again, or use the dashboard directly."
    return JSONResponse({"reply": reply or "No reply."})

# --- Training & onboarding ---

@app.get("/training", response_class=HTMLResponse)
async def training_page(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    with db.cursor() as cur:
        cur.execute("SELECT id, title, category, file_name, file_type, created_at FROM generated_documents WHERE category = 'training' ORDER BY created_at DESC LIMIT 10;")
        decks = cur.fetchall()
        cur.execute("SELECT * FROM worker_quiz_results ORDER BY created_at DESC LIMIT 50;")
        results = cur.fetchall()
    return templates.TemplateResponse(request=request, name="training.html", context={
        "user": {"email": user_email},
        "decks": decks,
        "results": results,
        "questions": training_service.QUIZ,
    })

@app.post("/api/training/deck")
async def training_deck_build(request: Request, kind: str = Form("worker"), db=Depends(get_db)):
    require_admin(request)
    try:
        data = training_service.build_deck(kind)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Could not build deck: {e}"})
    label = "Worker Orientation" if kind == "worker" else "Host & Lead Onboarding"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO generated_documents (title, category, file_name, file_type, file_data) VALUES (%s, %s, %s, %s, %s) RETURNING id;",
            (label, "training", f"{kind}-orientation.pptx", "pptx", data),
        )
        doc_id = cur.fetchone()["id"]
        db.commit()
    return JSONResponse({"ok": True, "doc_id": doc_id, "download_url": f"/docs/download/{doc_id}"})

@app.get("/present/{kind}")
async def present_deck(kind: str, request: Request):
    if kind not in ("worker", "host"):
        raise HTTPException(status_code=404, detail="Deck not found")
    slides = training_service.deck_slides(kind)
    actor = current_actor(request)
    return templates.TemplateResponse(request, "narrated_deck.html", {
        "kind": kind,
        "label": "New Worker Orientation" if kind == "worker" else "Host & Lead Onboarding",
        "decks_json": json.dumps(slides),
        "questions": training_service.QUIZ if kind == "worker" else [],
        "actor": actor,
        "worker_name": (actor.get("name") if actor and actor.get("role") == "worker" else "") or "",
        "worker_email": (actor.get("email") if actor and actor.get("role") == "worker" else "") or "",
    })

@app.get("/present/{kind}/audio/{idx}.mp3")
async def present_deck_audio(kind: str, idx: int):
    if kind not in ("worker", "host"):
        raise HTTPException(status_code=404, detail="Deck not found")
    slides = training_service.deck_slides(kind)
    if idx < 0 or idx >= len(slides):
        raise HTTPException(status_code=404, detail="Slide not found")
    static_dir = Path(__file__).parent / "static" / "present" / kind
    static_dir.mkdir(parents=True, exist_ok=True)
    out_path = static_dir / f"{idx}.mp3"
    if not out_path.exists() or out_path.stat().st_size == 0:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise HTTPException(status_code=503, detail="TTS not configured")
        try:
            from openai import OpenAI
            text = slides[idx]["voice"]
            client = OpenAI(api_key=api_key)
            resp = client.audio.speech.create(
                model="gpt-4o-mini-tts",
                voice="onyx",
                input=text,
            )
            out_path.write_bytes(resp.content)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"TTS failed: {e}")
    return Response(content=out_path.read_bytes(), media_type="audio/mpeg",
                    headers={"Cache-Control": "public, max-age=86400"})

@app.post("/api/training/quiz")
async def training_quiz_submit(request: Request, worker_name: str = Form(...), email: str = Form(""), answers: str = Form(...), db=Depends(get_db)):
    try:
        parsed = json.loads(answers)
        if not isinstance(parsed, list) or len(parsed) != len(training_service.QUIZ):
            raise ValueError("expected a list of answers")
        parsed = [int(a) for a in parsed]
    except Exception:
        return JSONResponse({"ok": False, "error": "Answers came through malformed."})
    grade = training_service.grade_quiz(parsed)
    actor = current_actor(request)
    worker_id = actor.get("id") if actor and actor.get("role") == "worker" else None
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO worker_quiz_results (worker_id, worker_name, email, score, total, passed, answers_json) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;",
            (worker_id, worker_name, email, grade["correct"], grade["total"], grade["passed"], answers),
        )
        result_id = cur.fetchone()["id"]
        db.commit()
    return JSONResponse({"ok": True, **grade, "result_id": result_id})

# --- Narrated deck presentations (bot presents the PowerPoint) ---

@app.get("/present/{kind}", response_class=HTMLResponse)
async def present_deck(request: Request, kind: str):
    slides = training_service.deck_slides(kind)
    if not slides:
        return RedirectResponse(url="/training", status_code=status.HTTP_303_SEE_OTHER)
    label = "Worker Orientation" if kind == "worker" else "Host & Lead Onboarding"
    return templates.TemplateResponse(request=request, name="narrated_deck.html", context={
        "kind": kind,
        "label": label,
        "slides": slides,
        "decks_json": json.dumps([
            {"title": s["title"], "bullets": s["bullets"], "caption": s.get("caption", "")}
            for s in slides
        ]),
    })

@app.post("/api/present/{kind}/reset")
async def present_reset_audio(kind: str):
    import shutil
    cache_dir = Path("static/present") / kind
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    return JSONResponse({"ok": True})

@app.get("/present/{kind}/audio/{idx}.mp3")
async def present_slide_audio(kind: str, idx: int):
    slides = training_service.deck_slides(kind)
    if idx < 0 or idx >= len(slides):
        raise HTTPException(status_code=404, detail="Slide not found")
    cache_dir = Path("static/present") / kind
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{idx}.mp3"
    if not path.exists():
        from openai import OpenAI
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise HTTPException(status_code=503, detail="OPENAI_API_KEY not configured")
        slide = slides[idx]
        title = slide["title"]
        bullets = slide["bullets"]
        bullets_txt = " ".join(f"{i}. {b}" for i, b in enumerate(bullets, 1)) if bullets else ""
        script = (
            f"Alright, here is slide {idx + 1} of {len(slides)}: {title}. "
            + (f"{bullets_txt}. " if bullets_txt else " ")
            + ("That's it for this slide. " if idx < len(slides) - 1 else "That's the whole deck, great job!")
        )
        client = OpenAI(api_key=api_key)
        try:
            resp = client.audio.speech.create(
                model="gpt-4o-mini-tts",
                voice="onyx",
                input=script,
                instructions="Speak like a warm, friendly training presenter for cleaners: clear, upbeat, unhurried. Read bullet points naturally, don't read numbers robotically.",
            )
            path.write_bytes(resp.content)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"TTS failed: {e}")
    return Response(
        content=path.read_bytes(),
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )

# --- Bank / funding partners ---

@app.get("/acquisition", response_class=HTMLResponse)
async def acquisition_page(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    # Radar leads (Reddit / LinkedIn)
    radar_leads = []
    with db.cursor() as cur:
        cur.execute("SELECT * FROM leads WHERE source IN ('reddit', 'linkedin') ORDER BY created_at DESC LIMIT 20;")
        for l in cur.fetchall():
            meta = {}
            try:
                meta = json.loads(l.get("analysis_json") or "{}")
            except (TypeError, ValueError):
                pass
            source = l.get("source") or "reddit"
            author = (l.get("name") or "").split("·", 1)[-1].strip() or f"{source} user"
            draft_fn = outreach_draft if source == "reddit" else linkedin_outreach_draft
            radar_leads.append({
                "id": l["id"],
                "author": author,
                "subreddit": meta.get("subreddit", ""),
                "title": meta.get("title", ""),
                "permalink": l.get("listing_url", ""),
                "funding": bool(l.get("funding_needed")),
                "status": l.get("status", "new"),
                "source": source,
                "draft": draft_fn({
                    "author": author,
                    "subreddit": meta.get("subreddit", ""),
                    "title": meta.get("title", ""),
                    "funding": bool(l.get("funding_needed")),
                }),
            })
        cur.execute("SELECT * FROM partners ORDER BY created_at DESC;")
        partners = cur.fetchall()
        cur.execute("""
            SELECT referral_code, COUNT(*), COUNT(*) FILTER (WHERE funding_needed = TRUE)
            FROM leads WHERE referral_code IS NOT NULL GROUP BY referral_code;
        """)
        referral_activity = {r["referral_code"]: r for r in cur.fetchall()}
        cur.execute("""
            SELECT rc.code, rc.label, rc.ref_type, rc.created_at, COUNT(l.id)::int AS cnt
            FROM referral_codes rc
            LEFT JOIN leads l ON l.referral_code = rc.code
            GROUP BY rc.id ORDER BY rc.created_at DESC LIMIT 20;
        """)
        codes = cur.fetchall()
        cur.execute("""
            SELECT COALESCE(NULLIF(source,''),'website') AS channel, status, COUNT(*)::int AS cnt
            FROM leads GROUP BY channel, status ORDER BY channel, status;
        """)
        funnel_raw = cur.fetchall()
        cur.execute("""
            SELECT id, name, email, source, status, funding_needed, campaign, created_at
            FROM leads ORDER BY created_at DESC LIMIT 25;
        """)
        recent_leads = cur.fetchall()

    funnel = {}
    for r in funnel_raw:
        funnel.setdefault(r["channel"], {"total": 0, "host": 0})["total"] += r["cnt"]
        if r["status"] == "host":
            funnel[r["channel"]]["host"] += r["cnt"]

    return templates.TemplateResponse(
        request=request,
        name="acquisition.html",
        context={
            "user": {"email": user_email},
            "radar_leads": radar_leads,
            "scanned": request.query_params.get("scanned"),
            "seen": request.query_params.get("seen"),
            "errors": request.query_params.get("errors", ""),
            "scan_started": request.query_params.get("scan_started"),
            "scan_running": request.query_params.get("scan_running"),
            "radar_status": {
                "reddit": _radar_status("reddit"),
                "linkedin": _radar_status("linkedin"),
            },
            "partners": partners,
            "referral_activity": referral_activity,
            "codes": codes,
            "funnel": funnel,
            "recent_leads": recent_leads,
        },
    )


@app.post("/api/leads/{lead_id}/status")
async def update_lead_status(lead_id: int, request: Request, status_val: str = Form("new"), db=Depends(get_db)):
    require_admin(request)
    allowed = {"new", "contacted", "quote", "host", "archive"}
    if status_val not in allowed:
        return JSONResponse({"ok": False, "error": "Invalid status"}, status_code=400)
    with db.cursor() as cur:
        cur.execute("UPDATE leads SET status = %s WHERE id = %s RETURNING id;", (status_val, lead_id))
        row = cur.fetchone()
        db.commit()
    if not row:
        return JSONResponse({"ok": False, "error": "Lead not found"}, status_code=404)
    return JSONResponse({"ok": True, "lead_id": lead_id, "status": status_val})

@app.get("/partners", response_class=HTMLResponse)
async def partners_page(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM partners ORDER BY created_at DESC;")
        partners = cur.fetchall()
        cur.execute("""
            SELECT referral_code, COUNT(*)::int AS cnt,
                   COUNT(*) FILTER (WHERE funding_needed = TRUE)::int AS funding_cnt
            FROM leads WHERE referral_code IS NOT NULL GROUP BY referral_code;
        """)
        referral_activity = {r["referral_code"]: r for r in cur.fetchall()}
        cur.execute("""
            SELECT l.*, p.name AS partner_name FROM leads l
            LEFT JOIN partners p ON p.id = l.partner_id
            WHERE l.funding_needed = TRUE ORDER BY l.created_at DESC;
        """)
        funding_leads = cur.fetchall()
    return templates.TemplateResponse(request=request, name="partners.html", context={
        "user": {"email": user_email},
        "partners": partners,
        "funding_leads": funding_leads,
        "referral_activity": referral_activity,
    })

@app.post("/api/partners")
async def add_partner(request: Request, name: str = Form(...), kind: str = Form("bank"), contact_email: str = Form(""), contact_phone: str = Form(""), website: str = Form(""), notes: str = Form(""), db=Depends(get_db)):
    require_admin(request)
    if not name.strip():
        return JSONResponse({"ok": False, "error": "Partner name required."})
    with db.cursor() as cur:
        cur.execute("INSERT INTO partners (name, kind, contact_email, contact_phone, website, notes) VALUES (%s, %s, NULLIF(%s,''), NULLIF(%s,''), NULLIF(%s,''), NULLIF(%s,'')) RETURNING id;",
                    (name, kind, contact_email, contact_phone, website, notes))
        partner_id = cur.fetchone()["id"]
        code = _gen_referral_code("partner")
        cur.execute("INSERT INTO referral_codes (code, ref_type, ref_id, label) VALUES (%s, 'partner', %s, %s) ON CONFLICT (code) DO NOTHING;",
                    (code, partner_id, name.strip()))
        cur.execute("UPDATE partners SET referral_code = %s WHERE id = %s;", (code, partner_id))
        db.commit()
    return JSONResponse({"ok": True, "partner_id": partner_id})

@app.post("/api/partners/{partner_id}/referral")
async def create_partner_referral(partner_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM partners WHERE id = %s;", (partner_id,))
        partner = cur.fetchone()
        if not partner:
            raise HTTPException(status_code=404, detail="Partner not found.")
        code = _gen_referral_code("partner")
        cur.execute("INSERT INTO referral_codes (code, ref_type, ref_id, label) VALUES (%s, 'partner', %s, %s) ON CONFLICT (code) DO NOTHING;",
                    (code, partner_id, partner["name"]))
        cur.execute("UPDATE partners SET referral_code = %s WHERE id = %s;", (code, partner_id))
        db.commit()
    base = os.getenv("APP_BASE_URL", "https://bizstackperks.com")
    return JSONResponse({"ok": True, "code": code, "url": f"{base}/?ref={urllib.parse.quote(code)}&src=referral"})

@app.post("/api/leads/{lead_id}/funding")
async def set_lead_funding(lead_id: int, request: Request, funding_needed: str = Form("on"), funding_amount: str = Form(""), funding_use: str = Form(""), partner_id: str = Form(""), db=Depends(get_db)):
    require_admin(request)
    needed = funding_needed.lower() in ("on", "true", "1", "yes")
    amt_cents = int(round(float(funding_amount or 0) * 100)) if (funding_amount or "").strip() else None
    pid = int(partner_id) if (partner_id or "").strip().isdigit() else None
    with db.cursor() as cur:
        cur.execute(
            "UPDATE leads SET funding_needed = %s, funding_amount_cents = %s, funding_use = NULLIF(%s,''), partner_id = %s WHERE id = %s RETURNING id;",
            (needed, amt_cents, funding_use, pid, lead_id),
        )
        updated = cur.fetchone()
        db.commit()
    if not updated:
        return JSONResponse({"ok": False, "error": "Lead not found."})
    return JSONResponse({"ok": True, "lead_id": lead_id, "funding_needed": needed})

# --- Legal ---

@app.get("/legal", response_class=HTMLResponse)
async def legal_page(request: Request):
    site = os.getenv("APP_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    return templates.TemplateResponse(request=request, name="legal.html", context={
        "site": site,
        "site_name": "Broom Service",
        "phone": "+1 (757) 846-9275",
        "email": "hello@bizstackperks.com",
        "bot_email": "hello@bizstackperks.com",
    })

application = app
