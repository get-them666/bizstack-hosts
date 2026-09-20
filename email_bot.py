import email as _email
import imaplib
import os
import re
import threading
import time
from datetime import datetime
from email.utils import parseaddr

import psycopg
from psycopg.rows import dict_row

IMAP_HOST = os.getenv("IMAP_HOST", "mail.privateemail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993") or 993)
IMAP_USER = os.getenv("IMAP_USERNAME", os.getenv("IMAP_USER", os.getenv("SMTP_USER", "")))
IMAP_PASS = os.getenv("IMAP_PASSWORD", os.getenv("IMAP_PASS", os.getenv("SMTP_PASS", "")))

POLL_SECONDS = int(os.getenv("EMAIL_BOT_POLL_SECONDS", "45"))
EMAIL_BOT_ENABLED = os.getenv("EMAIL_BOT_ENABLED", "1").lower() in ("1", "true", "yes")

CALL_LEADS_ENABLED = os.getenv("CALL_LEADS_ENABLED", "1").lower() in ("1", "true", "yes")
CALLS_PER_PASS = int(os.getenv("CALLS_PER_PASS", "5"))
CALL_START_HOUR = int(os.getenv("CALL_START_HOUR", "9"))
CALL_END_HOUR = int(os.getenv("CALL_END_HOUR", "19"))
CALL_TZ = os.getenv("CALL_TZ", "America/New_York")

OUTREACH_DOMAINS = ("lisc.org", "vccva.org", "virginia.gov", "hrchamber.com", "sba.gov")
AUTO_SENDERS = ("noreply", "no-reply", "no_reply", "donotreply", "mailer-daemon", "postmaster", "bounce", "notifications")

CONSTRUCTION_HINTS = (
    "kitchen", "bathroom", "bath", "remodel", "renovation", "reno", "home", "house",
    "roof", "siding", "window", "deck", "porch", "floor", "flooring", "paint", "paint",
    "painting", "foundation", "structural", "addition", "garage", "adu", "concrete",
    "framing", "new build", "add-on", "build", "stair", "fence", "drywall", "electrical",
    "plumbing", "excavation", "quote", "contractor", "construction", "estimate",
)

BROOM_HINTS = (
    "clean", "cleaning", "turnover", "turn", "rental", "airbnb", "vrbo", "stay",
    "booking", "reservation", "housekeeping", "maid", "property", "guest", "checkout",
    "str", "vacation", "co-host", "cohost", "linen", "laundry", "inspection",
)

_started = False
_start_lock = threading.Lock()


def _phone_digits(phone):
    if not phone:
        return ""
    return "".join(ch for ch in str(phone) if ch.isdigit())


def _parse_inbound(raw: bytes) -> tuple:
    try:
        msg = _email.message_from_bytes(raw)
    except Exception as e:
        print(f"📧[emailbot] parse failure: {e}")
        return "", "", ""
    _, sender = parseaddr(msg.get("From", ""))
    sender = (sender or "").strip().lower()
    subject = (msg.get("Subject", "") or "").strip()
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try:
                    body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    continue
                break
            if ctype == "text/html" and not body:
                try:
                    body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            body = ""
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    msg_id = ""
    try:
        msg_id = str(msg.get("Message-ID") or "") or ""
    except Exception:
        msg_id = ""
    return sender, subject, body[:4000], msg_id


def _should_skip(sender: str, subject: str) -> bool:
    if not IMAP_USER or sender == IMAP_USER.lower():
        return True
    if "@" in sender:
        domain = sender.split("@")[1].lower()
        if domain in OUTREACH_DOMAINS:
            return True
    for tok in AUTO_SENDERS:
        if tok in sender:
            return True
    return False


def _company_for(sender: str, subject: str, body: str) -> str:
    text = f"{subject} {body}".lower()
    construction_lead = _find_lead_for_sender(sender)
    if construction_lead:
        return "construction"
    broom_lead = _find_broom_sender(sender)
    if broom_lead:
        return "broom"
    con_score = sum(1 for h in CONSTRUCTION_HINTS if h in text)
    broom_score = sum(1 for h in BROOM_HINTS if h in text)
    return "construction" if con_score >= broom_score else "broom"


def _find_lead_for_sender(sender: str) -> int | None:
    digits = "".join(ch for ch in sender.split("@")[0] if ch.isdigit())
    email_part = sender
    try:
        with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                if email_part:
                    cur.execute("SELECT id FROM leads WHERE company = 'construction' AND LOWER(email) = %s ORDER BY id DESC LIMIT 1;", (email_part,))
                    row = cur.fetchone()
                    if row:
                        return row["id"]
                if len(digits) >= 10:
                    cur.execute(
                        "SELECT id, phone FROM leads WHERE company = 'construction' AND phone IS NOT NULL;"
                    )
                    for r in cur.fetchall():
                        p = "".join(ch for ch in str(r.get("phone") or "") if ch.isdigit())
                        if p and p[-10:] == digits[-10:]:
                            return r["id"]
    except Exception as e:
        print(f"📧[emailbot] lead lookup failure: {e}")
    return None


def _find_broom_sender(sender: str) -> bool:
    email_part = sender
    try:
        with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                if email_part:
                    cur.execute("SELECT COUNT(*) AS c FROM leads WHERE company = 'broom' AND LOWER(email) = %s;", (email_part,))
                    row = cur.fetchone()
                    if row and row["c"]:
                        return True
    except Exception:
        pass
    return False


def _ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS email_bot_log (
                message_id TEXT PRIMARY KEY,
                sender TEXT,
                subject TEXT,
                body TEXT,
                company TEXT,
                lead_id INTEGER,
                reply_body TEXT,
                processed_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()


def _agent_for(company: str, db):
    ctx = {"db": db}
    if company == "construction":
        import construction_bot as cm
        from con_ai_agent import BusinessAIAgent as ConAgent

        handlers = dict(cm.build_tool_handlers(db, cm.stripe_svc))
        handlers.pop("send_sms_message", None)
        ctx["agent"] = ConAgent(tool_handlers=handlers)
    else:
        import ai_agent
        import main as broom_main

        handlers = dict(broom_main.build_tool_handlers(db, broom_main._stripe_svc if hasattr(broom_main, "_stripe_svc") else broom_main.stripe_svc))
        handlers.pop("send_sms_message", None)
        ctx["agent"] = ai_agent.BusinessAIAgent(tool_handlers=handlers, subset="guest")
    return ctx["agent"]


def _send_email(to: str, subject: str, body: str) -> bool:
    import asyncio

    import documents_service

    cfg = documents_service.smtp_config_from_env()
    if not documents_service.smtp_configured(cfg):
        print("📧[emailbot] SMTP not configured — cannot send")
        return False
    try:
        asyncio.run(documents_service.send_email(cfg, to, subject, body))
        return True
    except Exception as e:
        print(f"📧[emailbot] send failure to {to}: {e}")
        return False


def _mark_seen(M, num) -> None:
    try:
        M.store(num, "+FLAGS", "\\Seen")
    except Exception:
        pass


def _already_emailed(conn, email_addr: str) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM comms_logs WHERE channel = 'email' AND direction = 'outbound' "
                "AND LOWER(recipient) = %s LIMIT 1;",
                (email_addr,),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _email_lead(conn, company: str, row: dict) -> bool:
    import auto_reply

    try:
        msg = auto_reply.auto_reply_to_lead(
            conn,
            company,
            name=row.get("name") or "",
            phone=row.get("phone") or "",
            email=row.get("email") or "",
            service=row.get("project_type") or row.get("service") or "",
            address=row.get("address") or "",
            budget=row.get("budget") or "",
            timeline=row.get("timeline") or "",
            message=row.get("description") or "",
            source="get-started",
            lead_id=row["id"],
        )
        if not msg:
            return False
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE leads SET status = 'contacted' WHERE id = %s AND status = 'new';",
                (row["id"],),
            )
        conn.commit()
        return True
    except Exception as e:
        print(f"📧[emailbot] lead email failed for lead {row.get('id')} ({company}): {e}")
        return False


def email_outstanding_leads() -> int:
    """Email every lead on both sites that has a real address and hasn't been emailed yet."""
    sent = 0
    try:
        db = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    except Exception as e:
        print(f"📧[emailbot] lead sweep db failure: {e}")
        return 0
    try:
        _ensure_table(db)
        for company in ("construction", "broom"):
            with db.cursor() as cur:
                cur.execute(
                    "SELECT id, name, phone, email, project_type, address, budget, timeline, description, status "
                    "FROM leads WHERE company = %s "
                    "AND email IS NOT NULL AND LOWER(email) <> '' AND LOWER(email) NOT LIKE %s "
                    "ORDER BY id DESC LIMIT 500;",
                    (company, "%@lead.local"),
                )
                rows = cur.fetchall()
            for r in rows:
                if _already_emailed(db, r["email"]):
                    continue
                if _email_lead(db, company, r):
                    sent += 1
                    print(f"📧[emailbot] emailed lead {r['id']} ({company})", flush=True)
    finally:
        db.close()
    return sent


def _log_to_comms(conn, direction: str, recipient: str, body: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES (%s, %s, %s, %s, %s);",
            (direction, "email", "system", recipient, body[:2000]),
        )
    conn.commit()


def _update_lead(db, lead_id: int, reply: str) -> None:
    try:
        with db.cursor() as cur:
            cur.execute("SELECT status FROM leads WHERE id = %s;", (lead_id,))
            row = cur.fetchone()
            if not row:
                return
            if row["status"] == "new":
                cur.execute("UPDATE leads SET status = 'contacted' WHERE id = %s;", (lead_id,))
            cur.execute("UPDATE leads SET notes = COALESCE(notes, '') || %s WHERE id = %s;", (f"\n[bot email reply at {time.strftime('%Y-%m-%d %H:%M')}]: {reply[:240]}", lead_id))
        db.commit()
    except Exception as e:
        print(f"📧[emailbot] lead update failure: {e}")


def _process_message(raw: bytes) -> dict:
    sender, subject, body, msg_id = _parse_inbound(raw)
    if not sender or not subject and not body:
        return {"sender": sender, "status": "unparseable"}
    if _should_skip(sender, subject):
        return {"sender": sender, "status": "skipped"}

    company = _company_for(sender, subject, body)
    lead_id = None
    if company == "construction":
        lead_id = _find_lead_for_sender(sender)

    db = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    try:
        _ensure_table(db)
        with db.cursor() as cur:
            cur.execute("SELECT 1 FROM email_bot_log WHERE message_id = %s;", (msg_id,))
            if cur.fetchone():
                return {"sender": sender, "status": "duplicate"}
        if not msg_id:
            msg_id = f"email_{sender}_{int(time.time() * 1000)}"

        context = (
            f"Inbound EMAIL from {sender} with subject {subject!r}: {body}\n\n"
            "Write a concise, friendly professional EMAIL reply. Do not use send_sms_message. "
            "Use the available tools to register or update leads, look things up, or create "
            "deposit/estimate links as appropriate, and include any links inside your reply."
        )
        try:
            agent = _agent_for(company, db)
            reply = (agent.process_inbound_text(context) or "").strip()
        except Exception as e:
            print(f"📧[emailbot] agent failure: {e}")
            reply = (
                f"Thanks for your message — our team got it and will reply shortly.\n\n"
                f"Best,\nBizStack"
            )

        if not reply:
            return {"sender": sender, "status": "empty_reply"}

        try:
            sent = _send_email(sender, f"Re: {subject}" if subject and not subject.lower().startswith("re:") else subject, reply)
        except Exception as e:
            sent = False
            print(f"📧[emailbot] send failure to {sender}: {e}")
        if not sent:
            db.rollback()
            return {"sender": sender, "status": "send_failed"}

        _log_to_comms(db, "inbound", sender, f"{subject}\n{body[:1000]}")
        _log_to_comms(db, "outbound", sender, reply)
        if lead_id:
            _update_lead(db, lead_id, reply)

        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO email_bot_log (message_id, sender, subject, body, company, lead_id, reply_body) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s);",
                (msg_id, sender, subject, body, company, lead_id, reply),
            )
        db.commit()
        return {"sender": sender, "status": "replied", "company": company, "lead_id": lead_id}
    finally:
        db.close()


def _poll_inbox() -> list:
    results = []
    if not IMAP_USER:
        print("📧[emailbot] no SMTP/IMAP user configured — email bot disabled")
        return results
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        M.login(IMAP_USER, IMAP_PASS)
        M.select("INBOX")
        typ, data = M.search(None, "UNSEEN")
        ids = data[0].split() if data and data[0] else []
        for num in ids:
            typ, msg_data = M.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1] if isinstance(msg_data[0][1], bytes) else bytes(msg_data[0][1])
            res = _process_message(raw)
            results.append(res)
            if res["status"] in ("unparseable",):
                _mark_seen(M, num)
        M.logout()
    except Exception as e:
        print(f"📧[emailbot] inbox poll failure: {e}")
    return results


def call_outstanding_leads() -> int:
    """Bot dials phone-only construction leads that haven't been called yet."""
    if not CALL_LEADS_ENABLED:
        return 0
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(CALL_TZ))
    if not (CALL_START_HOUR <= now.hour < CALL_END_HOUR):
        return 0
    try:
        import construction_bot as cm
    except Exception as e:
        print(f"📞[emailbot] construction bot import failed: {e}")
        return 0
    own = _phone_digits(getattr(cm, "from_number", None) or cm.signalwire.from_number)
    dialed = 0
    try:
        db = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
        db.autocommit = True
    except Exception as e:
        print(f"📞[emailbot] db failure: {e}")
        return 0
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, name, phone FROM leads WHERE company = 'construction' "
                "AND phone IS NOT NULL AND (email IS NULL OR LOWER(email) = '' OR LOWER(email) LIKE %s) "
                "ORDER BY id DESC LIMIT 300;",
                ("%@lead.local",),
            )
            rows = cur.fetchall()
        for r in rows:
            if dialed >= CALLS_PER_PASS:
                break
            digits = _phone_digits(r.get("phone"))
            if len(digits) < 10 or (own and digits[-10:] == own[-10:]):
                continue
            with db.cursor() as cur:
                cur.execute("SELECT 1 FROM bot_calls WHERE lead_id = %s LIMIT 1;", (r["id"],))
                if cur.fetchone():
                    continue
            to = ("+" + digits) if digits.startswith("1") else ("+1" + digits)
            url = f"https://{cm.company()['domain']}/comms/outbound-voice-webhook"
            try:
                sid = cm.signalwire.create_outbound_call(to, url)
            except Exception as e:
                print(f"📞[emailbot] dial failed for lead {r['id']}: {e}")
                continue
            if sid:
                dialed += 1
                print(f"📞[emailbot] dialed lead {r['id']} {r.get('name') or ''} ({to}) sid={sid}", flush=True)
    finally:
        db.close()
    return dialed


def _worker_loop() -> None:
    print(f"📧[emailbot] worker started (poll every {POLL_SECONDS}s)")
    try:
        n = email_outstanding_leads()
        if n:
            print(f"📧[emailbot] first pass emailed {n} outstanding leads", flush=True)
    except Exception as e:
        print(f"📧[emailbot] initial lead sweep failure: {e}")
    while True:
        try:
            results = _poll_inbox()
            for r in results:
                if r["status"] != "skipped":
                    print(f"📧[emailbot] {r['status']} -> {r['sender']}")
            sent = email_outstanding_leads()
            if sent:
                print(f"📧[emailbot] lead sweep emailed {sent} new leads", flush=True)
            calls = call_outstanding_leads()
            if calls:
                print(f"📞[emailbot] dialed {calls} leads this pass", flush=True)
        except Exception as e:
            print(f"📧[emailbot] poll pass failure: {e}")
        time.sleep(POLL_SECONDS)


def start_email_bot() -> None:
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        _started = True
    if not EMAIL_BOT_ENABLED or not IMAP_USER:
        print("📧[emailbot] disabled (EMAIL_BOT_ENABLED unset or no IMAP user)")
        return
    t = threading.Thread(target=_worker_loop, name="email-bot", daemon=True)
    t.start()