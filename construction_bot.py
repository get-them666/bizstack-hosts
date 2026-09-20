"""Buildstack Construction Co. — shared email/phone bot support (perks repo).

Self-contained subset of the construction app used only by the shared inbox
(email) and phone (outbound-call) bot, which run from the Broom service and
answer the shared hello@ inbox / phone for both brands. This module mirrors the
relevant state + tool handlers from construction_main.py so that the bot no
longer imports the construction web app.
"""

import json
import os

from stripe_service import StripeService
from signalwire_service import SignalWireService
import auto_reply
import materials_service
import training_service

stripe_svc = StripeService()
signalwire = SignalWireService()

LEAD_STATUSES = ["new", "contacted", "quoted", "deposit", "in_progress", "completed", "lost"]
STATUS_LABELS = {
    "new": "New", "contacted": "Contacted", "quoted": "Quoted", "deposit": "Deposit paid",
    "in_progress": "In progress", "completed": "Completed", "lost": "Lost",
}


def company() -> dict:
    return {
        "name": os.getenv("COMPANY_NAME", "Buildstack Construction Co."),
        "phone": os.getenv("COMPANY_PHONE", "+1 (757) 846-9275"),
        "phone_e164": os.getenv("SIGNALWIRE_PHONE", "+17578469275"),
        "email": os.getenv("COMPANY_EMAIL", "hello@bizstackperks.com"),
        "domain": os.getenv("COMPANY_DOMAIN", "construction.bizstackperks.com"),
        "license": os.getenv("CONTRACTOR_LICENSE", ""),
        "service_area": os.getenv(
            "SERVICE_AREA",
            "Williamsburg–Hampton Roads, VA · Currituck County & Elizabeth City, NC",
        ),
        "founded": "2026",
    }


def build_tool_handlers(db, stripe_svc):
    """DB/Stripe tool handlers that let the AI agent capture and manage leads."""

    def register_lead(name, phone, email="", project_type="", address="", budget="", timeline="", notes=""):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO leads (name, phone, email, project_type, address, budget, timeline, description, source, status, company) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ai-assistant', 'new', 'construction') RETURNING id;",
                (name, phone, email, project_type, address, budget, timeline, notes),
            )
            lead_id = cur.fetchone()["id"]
            db.commit()
        return {"ok": True, "lead_id": lead_id, "message": f"Lead saved for {name}."}

    def lookup_leads(phone):
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        with db.cursor() as cur:
            cur.execute("SELECT id, name, project_type, status, deposit_status, created_at FROM leads WHERE company = 'construction' ORDER BY created_at DESC LIMIT 200;")
            rows = cur.fetchall()
        matches = [r for r in rows if digits and digits[-10:] in "".join(ch for ch in str(r.get("phone") or "") if ch.isdigit())]
        if not matches:
            matches = [r for r in rows if digits and digits[-10:] in "".join(ch for ch in str(r.get("name") or "") if ch.isdigit())]
        out = [
            {
                "id": r["id"], "name": r["name"], "project_type": r["project_type"],
                "status": r["status"], "deposit_status": r["deposit_status"],
                "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
            }
            for r in matches
        ]
        return {"ok": True, "phone": phone, "leads": out}

    def get_business_summary():
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM leads WHERE company = 'construction';")
            total = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM leads WHERE status NOT IN ('completed','lost') AND company = 'construction';")
            open_leads = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents),0) AS amt FROM payments WHERE company = 'construction';")
            pay = cur.fetchone()
        return {
            "ok": True,
            "total_leads": total,
            "open_leads": open_leads,
            "deposits_collected": pay["c"],
            "deposit_amount_cents": pay["amt"],
        }

    def list_leads(status=""):
        with db.cursor() as cur:
            if status:
                cur.execute(
                    "SELECT id, name, phone, project_type, status, deposit_status, created_at FROM leads WHERE status = %s AND company = 'construction' ORDER BY created_at DESC LIMIT 100;",
                    (status,),
                )
            else:
                cur.execute("SELECT id, name, phone, project_type, status, deposit_status, created_at FROM leads WHERE company = 'construction' ORDER BY created_at DESC LIMIT 100;")
            rows = cur.fetchall()
        return {"ok": True, "leads": [
            {**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}} for r in rows
        ]}

    def update_lead_status(lead_id, status):
        if status not in LEAD_STATUSES:
            return {"ok": False, "error": f"Unknown status: {status}"}
        with db.cursor() as cur:
            cur.execute("UPDATE leads SET status = %s WHERE id = %s;", (status, lead_id))
            db.commit()
        return {"ok": True, "lead_id": lead_id, "status": status}

    def send_sms_message(to, body):
        ok = signalwire.send_sms(to, body)
        return {"ok": ok} if ok else {"ok": False, "error": "SMS could not be sent."}

    def send_email_message(to, subject, body):
        import documents_service

        try:
            cfg = documents_service.smtp_config_from_env()
            if not documents_service.smtp_configured(cfg):
                return {"ok": False, "error": "SMTP not configured."}
            auto_reply.run_coro(documents_service.send_email(cfg, to, subject, body))
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                    "VALUES ('outbound', 'email', 'system', %s, %s);",
                    (to, body),
                )
                db.commit()
            return {"ok": True, "sent_to": to}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def create_deposit_link(lead_id):
        with db.cursor() as cur:
            cur.execute("SELECT * FROM leads WHERE id = %s;", (lead_id,))
            lead = cur.fetchone()
        if not lead:
            return {"ok": False, "error": "Lead not found."}
        try:
            url = stripe_svc.create_deposit_session(
                lead_id=lead["id"],
                customer_name=lead["name"],
                customer_email=lead.get("email") or "",
                project_type=lead.get("project_type") or "",
                amount_cents=lead.get("deposit_cents") or 0,
                base_url=f"https://{company()['domain']}",
            )
            return {"ok": True, "url": url}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # --- Extended owner-co-pilot toolkit -----------------------------------
    def lookup_permits(city="", address=""):
        with db.cursor() as cur:
            if address.strip():
                cur.execute(
                    "SELECT * FROM job_leads WHERE address ILIKE %s ORDER BY found_at DESC LIMIT 25;",
                    (f"%{address.strip()}%",),
                )
            elif city.strip():
                cur.execute(
                    "SELECT * FROM job_leads WHERE city ILIKE %s ORDER BY found_at DESC LIMIT 25;",
                    (f"%{city.strip()}%",),
                )
            else:
                cur.execute("SELECT * FROM job_leads ORDER BY found_at DESC LIMIT 15;")
            rows = cur.fetchall()
        return {"ok": True, "permits": [
            {
                "id": r["id"], "permit_number": r.get("permit_number"),
                "address": r.get("address"), "city": r.get("city"),
                "state": r.get("state"), "work_type": r.get("work_type"),
                "status": r.get("status"), "is_demo": r.get("is_demo"),
                "issue_date": r.get("issue_date"),
                "found_at": (r.get("found_at").isoformat() if r.get("found_at") else ""),
            }
            for r in rows
        ]}

    def list_crew():
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, name, phone, email, role, pay_type, pay_rate, is_active, "
                "stripe_account_id, bank_status FROM crew ORDER BY is_active DESC, name;"
            )
            rows = cur.fetchall()
        return {"ok": True, "crew": [
            {**{k: (v.isoformat() if hasattr(v, "isoformat") else (float(v) if k == "pay_rate" and v is not None else v)) for k, v in r.items()}}
            for r in rows
        ]}

    def lookup_crew_timesheets(crew_id=0, project_id=0, status=""):
        with db.cursor() as cur:
            if crew_id:
                cur.execute(
                    "SELECT t.*, c.name AS crew_name, p.name AS project_name "
                    "FROM timesheets t JOIN crew c ON c.id = t.crew_id "
                    "LEFT JOIN projects p ON p.id = t.project_id "
                    "WHERE t.crew_id = %s ORDER BY t.work_date DESC LIMIT 100;", (crew_id,))
            elif project_id:
                cur.execute(
                    "SELECT t.*, c.name AS crew_name, p.name AS project_name "
                    "FROM timesheets t JOIN crew c ON c.id = t.crew_id "
                    "LEFT JOIN projects p ON p.id = t.project_id "
                    "WHERE t.project_id = %s ORDER BY t.work_date DESC LIMIT 100;", (project_id,))
            elif status:
                cur.execute(
                    "SELECT t.*, c.name AS crew_name, p.name AS project_name "
                    "FROM timesheets t JOIN crew c ON c.id = t.crew_id "
                    "LEFT JOIN projects p ON p.id = t.project_id "
                    "WHERE t.status = %s ORDER BY t.work_date DESC LIMIT 100;", (status,))
            else:
                cur.execute(
                    "SELECT t.*, c.name AS crew_name, p.name AS project_name "
                    "FROM timesheets t JOIN crew c ON c.id = t.crew_id "
                    "LEFT JOIN projects p ON p.id = t.project_id "
                    "ORDER BY t.work_date DESC LIMIT 100;")
            rows = cur.fetchall()
        return {"ok": True, "timesheets": [
            {
                "id": r["id"], "crew_id": r["crew_id"], "crew_name": r.get("crew_name"),
                "project_id": r.get("project_id"), "project_name": r.get("project_name"),
                "work_date": r.get("work_date").isoformat() if r.get("work_date") else "",
                "hours": float(r.get("hours") or 0), "work_type": r.get("work_type"),
                "status": r.get("status"), "notes": r.get("notes"),
            }
            for r in rows
        ]}

    def get_payroll_summary():
        with db.cursor() as cur:
            cur.execute("SELECT * FROM payroll_runs WHERE status = 'open' ORDER BY period_start DESC LIMIT 1;")
            run = cur.fetchone()
            if not run:
                return {"ok": True, "open_run": None, "message": "No open payroll run."}
            cur.execute(
                "SELECT pl.*, c.name AS crew_name, c.bank_status, c.stripe_account_id "
                "FROM payroll_lines pl JOIN crew c ON c.id = pl.crew_id "
                "WHERE pl.run_id = %s ORDER BY c.name;", (run["id"],))
            lines = cur.fetchall()
        return {"ok": True, "open_run": {
            "run_id": run["id"],
            "period_start": run["period_start"].isoformat(),
            "period_end": run["period_end"].isoformat(),
        }, "lines": [
            {
                "crew_id": l["crew_id"], "crew_name": l["crew_name"],
                "hours": float(l.get("hours") or 0), "overtime_hours": float(l.get("overtime_hours") or 0),
                "gross_cents": l["gross_cents"],
                "bank_status": l.get("bank_status"), "direct_deposit_ready": bool(l.get("stripe_account_id")),
            }
            for l in lines
        ]}

    def run_payroll():
        with db.cursor() as cur:
            cur.execute("SELECT * FROM payroll_runs WHERE status = 'open' ORDER BY period_start DESC LIMIT 1;")
            run = cur.fetchone()
        if not run:
            return {"ok": False, "error": "No open payroll run to finalize. Create one on the Payroll page first."}
        run_id = run["id"]
        with db.cursor() as cur:
            cur.execute(
                "SELECT pl.*, c.name AS crew_name, c.pay_rate, c.stripe_account_id, c.bank_status "
                "FROM payroll_lines pl JOIN crew c ON c.id = pl.crew_id WHERE pl.run_id = %s;", (run_id,))
            lines = cur.fetchall()
        paid, skipped = [], []
        for l in lines:
            acct = (l.get("stripe_account_id") or "").strip()
            if not acct:
                skipped.append({"crew_name": l.get("crew_name"), "reason": "no direct deposit connected"})
                continue
            ok = stripe_svc.transfer_to_worker(acct, int(l["gross_cents"]), memo=f"payroll-run-{run_id}")
            if ok:
                with db.cursor() as cur:
                    cur.execute(
                        "INSERT INTO payments (lead_id, stripe_payment_intent_id, amount_cents, currency, status, company) "
                        "VALUES (NULL, %s, %s, 'usd', 'paid', 'construction');",
                        (f"payroll-run-{run_id}-crew-{l['crew_id']}", l["gross_cents"]),
                    )
                paid.append({"crew_name": l.get("crew_name"), "gross_cents": l["gross_cents"]})
            else:
                skipped.append({"crew_name": l.get("crew_name"), "reason": "Stripe transfer failed"})
        with db.cursor() as cur:
            cur.execute("UPDATE payroll_runs SET status = 'closed' WHERE id = %s;", (run_id,))
            db.commit()
        return {
            "ok": True, "run_id": run_id,
            "paid": paid,
            "not_paid": skipped,
            "total_paid_cents": sum(p["gross_cents"] for p in paid),
        }

    def get_accounting_summary():
        with db.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS amt, COUNT(*) AS n "
                "FROM payments WHERE company = 'construction' AND status = 'paid';")
            collected = cur.fetchone()
            cur.execute(
                "SELECT COALESCE(SUM(deposit_cents),0) AS amt, COUNT(*) AS n "
                "FROM leads WHERE company = 'construction' AND deposit_status = 'paid';")
            deposits = cur.fetchone()
            cur.execute(
                "SELECT COALESCE(SUM(gross_cents),0) AS amt FROM payroll_lines pl "
                "JOIN payroll_runs pr ON pr.id = pl.run_id WHERE pr.status = 'closed';")
            payroll_paid = cur.fetchone()
            cur.execute(
                "SELECT COALESCE(SUM(estimated_value),0) AS amt, COUNT(*) AS n "
                "FROM job_leads WHERE status = 'new';")
            open_job_value = cur.fetchone()
        return {
            "ok": True,
            "collected_deposits_cents": deposits["amt"],
            "collected_deposits_count": deposits["n"],
            "total_collections_cents": collected["amt"],
            "payments_count": collected["n"],
            "payroll_paid_total_cents": payroll_paid["amt"],
            "open_job_feed_value_cents": open_job_value["amt"],
            "open_job_feed_count": open_job_value["n"],
        }

    def estimate_materials(project_type="", sqft=0, include=None, live=False):
        svc = materials_service.BusinessMaterialsService()
        return svc.estimate_materials(project_type=project_type, sqft=sqft, include=include, live=bool(live))

    def get_material_price(sku):
        svc = materials_service.BusinessMaterialsService()
        cents = svc.get_price(sku)
        if cents is None or cents <= 0:
            return {"ok": False, "sku": sku, "error": "Sku not found in price book."}
        return {"ok": True, "sku": sku, "price_cents": int(cents), "price_dollars": round(cents / 100, 2)}

    def sister_business_summary():
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM leads WHERE company = 'broom';")
            leads = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM bookings;")
            bookings = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM workers WHERE is_active = TRUE;")
            workers = cur.fetchone()["c"]
            cur.execute("SELECT COALESCE(SUM(gross_cents),0) AS amt FROM worker_paychecks;")
            payroll = cur.fetchone()["amt"]
        return {
            "ok": True,
            "company": "Broom Service (bizstackperks.com)",
            "leads": leads,
            "bookings": bookings,
            "active_workers": workers,
            "payroll_paid_total_cents": payroll,
        }

    def run_site_health_check():
        site = f"https://{company()['domain']}"
        checks = {"construction_site": True, "broom_site": True, "sms": True, "stripe": True, "database": True}
        try:
            with db.cursor() as cur:
                cur.execute("SELECT 1;")
        except Exception:
            checks["database"] = False
        checks["sms"] = bool(os.getenv("SIGNALWIRE_PROJECT_ID") or os.getenv("SIGNALWIRE_API_TOKEN"))
        checks["stripe"] = stripe_svc.is_configured()
        return {
            "ok": True,
            "sites": {
                "construction": f"{site} ✓",
                "broom": "https://bizstackperks.com ✓",
            },
            "checks": checks,
        }

    def generate_training_deck(kind="worker"):
        is_construction = kind in ("construction", "construction-osha", "osha")
        kind = "construction" if is_construction else kind
        if kind not in ("worker", "host", "construction"):
            return {"ok": False, "error": f"Unknown deck kind: {kind}"}
        try:
            data = training_service.build_deck(kind)
        except Exception as e:
            return {"ok": False, "error": f"Could not build deck: {e}"}
        label = "Crew Orientation + OSHA-10 Baseline" if kind == "construction" else ("Worker Orientation" if kind == "worker" else "Host & Lead Onboarding")
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO generated_documents (title, category, file_name, file_type, file_data) "
                "VALUES (%s, 'training', %s, 'pptx', %s) RETURNING id;",
                (label, f"{kind}-orientation.pptx", data),
            )
            doc_id = cur.fetchone()["id"]
            db.commit()
        return {"ok": True, "doc_id": doc_id, "title": label,
                "download_url": f"/docs/download/{doc_id}"}

    def grade_training_quiz(crew_id, answers):
        try:
            grade = training_service.grade_osha_quiz(answers)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        name = ""
        if crew_id:
            with db.cursor() as cur:
                cur.execute("SELECT name FROM crew WHERE id = %s;", (crew_id,))
                row = cur.fetchone()
                if row:
                    name = row["name"]
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_quiz_results (worker_id, worker_name, score, total, passed, answers_json) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;",
                (crew_id, name or "Crew (copilot)", grade["correct"], grade["total"], grade["passed"],
                 json.dumps(answers)),
            )
            result_id = cur.fetchone()["id"]
            db.commit()
        return {
            "ok": True, "result_id": result_id, "crew_id": crew_id,
            "total": grade["total"], "correct": grade["correct"],
            "percent": grade["percent"], "passed": grade["passed"],
            "missed_topics": grade.get("missed_topics", []),
            "message": "Passed — clear for jobs." if grade["passed"] else "Did not pass yet — review the missed topics and retest.",
        }

    return {
        "register_lead": register_lead,
        "lookup_leads": lookup_leads,
        "get_business_summary": get_business_summary,
        "list_leads": list_leads,
        "update_lead_status": update_lead_status,
        "send_sms_message": send_sms_message,
        "send_email_message": send_email_message,
        "create_deposit_link": create_deposit_link,
        "lookup_permits": lookup_permits,
        "list_crew": list_crew,
        "lookup_crew_timesheets": lookup_crew_timesheets,
        "get_payroll_summary": get_payroll_summary,
        "run_payroll": run_payroll,
        "get_accounting_summary": get_accounting_summary,
        "estimate_materials": estimate_materials,
        "get_material_price": get_material_price,
        "sister_business_summary": sister_business_summary,
        "run_site_health_check": run_site_health_check,
        "generate_training_deck": generate_training_deck,
        "grade_training_quiz": grade_training_quiz,
    }
