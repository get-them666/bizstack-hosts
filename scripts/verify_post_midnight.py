"""Post-quota-reset verification: confirm sweep + auto-flush delivered.

Run AFTER Resend UTC midnight reset. Checks:
  1. No outbound email comms rows during the 07:19-07:46 phantom window today.
  2. The 4 phantom recipients have no new phantom rows.
  3. Leads 342/344/345/348/350/352 transitioned (draft fired / contacted / emailed).
  4. Staged drafts drained: email drafts fired after quota reset.
  5. cross-check a sample of comms rows against Resend list API.
"""
import json
import os
import sys
import urllib.request

import psycopg2
import psycopg2.extras
from psycopg2.extras import RealDictCursor

DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:YzIBUFnhRPTKWiuHYOHXLPTJHssvkCiN@roundhouse.proxy.rlwy.net:35421/railway",
)
RESEND_KEY = os.environ.get("RESEND_API_KEY", "")

OUT = []
def p(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    OUT.append(line)

def db():
    c = psycopg2.connect(DSN)
    c.autocommit = True
    return c

def dbcur():
    return db().cursor(cursor_factory=RealDictCursor)

def main():
    cur = dbcur()
    cur.execute("SELECT current_timestamp::text AS now;")
    p("check time (UTC):", cur.fetchone()["now"])

    p("\n== 1. phantom flood check ==")
    cur.execute(
        "SELECT COUNT(*) c FROM comms_logs WHERE direction='outbound' AND channel='email' "
        "AND created_at >= '2026-09-24 07:19:00+00' AND created_at <= '2026-09-24 07:46:30+00';"
    )
    p("repeats in origin phantom window (should be 0):", cur.fetchone()["c"])

    p("\n== 2. phantom recipients ==")
    ph = ["alvanni.williams@us.af.mil", "ronald.m.stinson.civ@army.mil",
          "jonathan.long.civ@socom.mil", "ioulia.boxley@usda.gov"]
    cur.execute(
        "SELECT recipient, COUNT(*) c FROM comms_logs WHERE direction='outbound' AND channel='email' "
        "AND recipient = ANY(%s) GROUP BY recipient;", (ph,))
    rows = cur.fetchall()
    for r in rows:
        flag = "  <-- still phantom!" if r["c"] > 1 else ""
        p(f"  {r['recipient']}: {r['c']} outbound rows{flag}")
    if not rows:
        p("  (none)")

    p("\n== 3. reset leads 342/344/345/348/350/352 ==")
    cur.execute(
        "SELECT id, status, last_reply_at::text, LEFT(draft_reply, 46) d "
        "FROM leads WHERE id = ANY(%s) ORDER BY id;", ([342,344,345,348,350,352],))
    for r in cur.fetchall():
        p(f"  {r['id']}: status={r['status']} last_reply={r['last_reply_at']} draft={r['d']}")

    p("\n== 4. staged draft drain ==")
    cur.execute(
        "SELECT CASE WHEN draft_reply LIKE '[BOT DRAFT REPLY \u2013 text%' THEN 'text' "
        "ELSE 'email' END AS ch, COUNT(*) c FROM leads WHERE draft_reply IS NOT NULL GROUP BY 1;")
    for r in cur.fetchall():
        p(f"  {r['ch']} drafts remaining: {r['c']}")
    cur.execute("SELECT COUNT(*) c FROM leads WHERE draft_reply IS NOT NULL;")
    p("  total staged drafts (0 after flush):", cur.fetchone()["c"])

    p("\n== 5. Resend list-API sample ==")
    if not RESEND_KEY:
        p("  RESEND_API_KEY not set; skipping")
        return
    try:
        req = urllib.request.Request("https://api.resend.com/emails?limit=100",
                                     headers={"Authorization": f"Bearer {RESEND_KEY}"})
        data = json.load(urllib.request.urlopen(req, timeout=30))["data"]
        sent = [e for e in data if e.get("created_at","").startswith("2026-09-24")]
        p(f"  Resend shows {len(sent)} sends today; newest:")
        for e in sent[-3:]:
            p(f"    {e.get('created_at')} -> {e.get('to')}  {e.get('last_event')}")
    except Exception as exc:
        p("  resend check failed:", exc)

    path = "/tmp/resend_full.json"
    if os.path.exists(path):
        with open(path) as fh:
            blob = json.load(fh)
        if isinstance(blob, list):
            p(f"  local /tmp/resend_full.json has {len(blob)} emails")
        else:
            p(f"  /tmp/resend_full.json has {len(blob.get('data',[]))} emails")

if __name__ == "__main__":
    main()