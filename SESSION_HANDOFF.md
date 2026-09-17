# Buildstack + Broom Service — Session Handoff

Last updated: 2026-09-16

> ⚠️ This file contains a live admin password. It is untracked and NOT committed.
> Do not commit it. Move the password to a password manager and delete it here when done.

---

## 1. Buildstack Construction — DEPLOYED & LIVE ✅

- **URL:** https://friendly-appreciation-production-56b4.up.railway.app
- **Railway project:** `courteous-wisdom` — id `913e36b5-fe1f-4d73-80c5-aa0dd74f47be`
- **Environment:** `production` — id `883d46c2-43e5-4d36-9e16-6e801a2c67d2`
- **App service:** `friendly-appreciation` — id `07821508-53df-49e4-a144-cb4598a446cc`
- **Postgres service:** `Postgres` — id `b6ed7795-eeb9-4ffb-97e3-88ab17e1b89c`
- **Domain targetPort:** `8080` (must match the port the app binds)

### Admin login
- **Email:** soleary0006@gmail.com
- **Password:** GWEpCJpD3PneS84FXRJE
- **Login:** /login  (OTP is currently **disabled** because no SMTP is configured)

### Env vars set on the app service
`DATABASE_URL=${{Postgres.DATABASE_URL}}`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `APP_SECRET`,
`COOKIE_SECURE=true`, `OTP_ENABLED=false`, `APP_TIMEZONE=America/New_York`,
`COMPANY_NAME`, `COMPANY_PHONE`, `COMPANY_EMAIL`, `COMPANY_DOMAIN`, `SERVICE_AREA`,
`SIGNALWIRE_PHONE`, `OPENAI_MODEL=gpt-4o-mini`,
`APP_BASE_URL=https://friendly-appreciation-production-56b4.up.railway.app`

### Railway gotchas learned (important)
- Do **NOT** set a `PORT` var unless you also set the domain `targetPort` to match.
  Current state: `PORT` is unset → Railway injects `8080` → domain targets `8080`. ✅
- To change/set the domain target port:
  `railway api 'mutation U($input:ServiceDomainUpdateInput!){ serviceDomainUpdate(input:$input) }'`
  with input `{domain, environmentId, serviceDomainId, serviceId, targetPort}`.
- Deploy command used (from `bizstack-construction/`):
  `railway up --project 913e36b5-fe1f-4d73-80c5-aa0dd74f47be --environment 883d46c2-43e5-4d36-9e16-6e801a2c67d2 --service friendly-appreciation --detach`
- Domain id: `0fbc0f4c-cd0f-47c7-85b3-3c2239619d06`

### Verified working in production
`/health / /instant-quote /services /about /robots.txt /dashboard /leads /job-leads
/projects /crew/admin /payroll /copilot /crew` all 200. Admin login → `/dashboard`.
Instant-quote produced $18k–$45k (kitchen, 1800 sqft manual) and the lead saved + shows in /leads.
`/api/chat` replies gracefully (no OpenAI key). `/api/property` falls back to manual.

### Repo state
- `bizstack-construction/` is its **own git repo**, left in place at
  `/Users/shaunoleary/bizstack-hosts/bizstack-construction` (Broom's `.gitignore` ignores it).
- Committed: **`92ae1db`** "Initial Buildstack Construction site: instant quotes, job finder, crew portal, payroll with Stripe Connect"
- 18 tracked files, no secrets committed.

### Bugs found & fixed this session (all real)
1. `stripe.error` not exported → `except Exception`.
2. `project_update` form param `status` shadowed FastAPI `status` → renamed `status_val`.
3. `request: Request = None` signature ordering (several routes).
4. Garbage `ILIKE` execute line in `crew_login` removed; decorator artifact fixed.
5. `ALTER TABLE crew ...` ran **before** `CREATE TABLE crew` → killed all schema init. Moved after.
6. Worker login only stripped hyphens, so `(757)` broke matching → `REGEXP_REPLACE(phone,'[^0-9]','','g') = digits`.
7. Clock-in with no project inserted `0` (FK violation) → now writes `NULL`.
8. `crew_dashboard` clock-in passed `worker.id` as `project_id` → real job selector.
9. Railway port mismatch (domain 8080 vs app 8000) → unset PORT, domain → 8080.
10. `OTP_ENABLED=true` with no SMTP locked the owner out → set false until mail is set up.

---

## 2. Still needs USER accounts / keys (site works with fallbacks meanwhile)

Add these as Railway variables when available:
- `RENTCAST_API_KEY` — address → sqft/beds/baths/year-built for instant quotes
- `SHOVELS_API_KEY` — Job Finder building permits
- `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`,
  `STRIPE_CONNECT_WEBHOOK_SECRET` — deposits + crew direct deposit (Connect Express)
- `OPENAI_API_KEY` — real web chat / copilot / voice
- `SIGNALWIRE_PROJECT_ID`, `SIGNALWIRE_API_TOKEN`, `SIGNALWIRE_SPACE_URL` — SMS + voice
- SMTP_* — enables OTP login (then set `OTP_ENABLED=true` again)

Once keys are in, redeploy with the `railway up` command above.

---

## 3. Broom Service — NEXT (mapped, not yet changed)

The exploration is DONE. Full flow map captured below; no code changed in Broom yet.

### Biggest friction (the 2–3 click problem)
1. **Email OTP is mandatory for every role when `OTP_ENABLED=true`** — the biggest
   click driver; workers/hosts are blocked entirely if they have no email on file.
   Gates: `main.py:1997` (host), `main.py:2670` (worker web), `main.py:3603-3606` (worker app).
2. **Two worker auth worlds:** web `session_token` (`main.py:2654`) vs app `worker_session` / `worker_token` (`main.py:3588`).
3. **Hosts can't log in until office provisions a password** (`set-login`; gate `main.py:1991`).
4. **Turnover Auto-Dispatch is UI-only** (`templates/settings.html:125-129`) — channel
   bookings only become portal jobs if `properties.channel_property_uuid` is linked;
   no crew auto-assignment (`channel_sync.py:143-145`).

### Core surfaces
- **Worker:** `GET /worker`:2700 `worker_portal.html`; login `POST /api/worker/auth/login`:2654;
  app login `POST /api/worker/app/login`:3588; complete `POST /api/worker/jobs/{id}/complete`:2812;
  clock `POST /api/worker/clock`:2826 (100 m geofence `_haversine_m`:2860).
- **Host:** `GET /host`:2152 `host_portal.html`; login `POST /api/host/auth/login`:1988.
- **Cleans/bookings:** everything is `calendar_events` (`main.py:545`, cols :558-564/:710-717).
  `bookings` table is DEAD code. Public `GET|POST /book`:1483/:1502; admin `POST /api/calendar/book`:4492.
- **Pay:** `POST /api/workers/{id}/paycheck`:2589 → `worker_paychecks`; stubs/checks PDF
  `main.py:2780/4383/4413`; ledger `ledger_entries` (`main.py:781`); accounting `GET /accounting`:4347.
- **Auth:** `auth_service.py` — roles `admin|worker|host`, TTL 72h, OTP 6-digit/10-min.
  Cookies: `session_token` + `user_email/user_name/role/actor_id`; `otp_pending`;
  worker app `worker_session/worker_id`; host `host_session/host_id`.
- **Channels:** `channel_sync.py` — Hospitable API v2 (PAT). VRBO arrives transitively via
  Hospitable (`CHANNEL_LABELS` :16-23). **Turno = marketing only, no code.**

### Planned Broom work (agreed direction)
- Collapse worker/host/clean/pay to 2–3 clicks: make OTP optional-by-role or magic-link,
  unify the two worker auth schemes, allow host self-serve login, wire real turnover
  auto-dispatch (auto-assign crew on channel booking).
- **Turno sync:** mirror `HospitableService` (`channel_sync.py:33`) as `TurnoService`
  (API `https://api.turnoverbnb.com/v2`, token + `TURNO_PARTNER_ID`), reuse
  `_upsert_reservation` with `channel_source='turno'`, add trigger + webhook routes next
  to `main.py:3990`/`:4001`. Include a no-key graceful fallback (like buildstack).
- **VRBO:** either keep via Hospitable, or add Expedia Group Rapid (`supply_source=vrbo`,
  needs partner enablement).

### Turno/VRBO research notes
- Turno v2 REST API base `https://api.turnoverbnb.com/v2`; auth = secret token +
  `TURNO_PARTNER_ID` (property/booking/project/assignment/cleaner/webhook resources).
- VRBO options: Expedia Rapid (`supply_source=vrbo`, partner-gated) OR Hospitable (already wired).

---

## 4. How to resume

1. Buildstack: nothing required unless adding keys (see §2).
2. Broom: start implementing §3 — the flow map above has the file:line references needed.
3. Repos: Broom = `/Users/shaunoleary/bizstack-hosts` (git, has commits).
   Buildstack = `/Users/shaunoleary/bizstack-hosts/bizstack-construction` (own git repo, `92ae1db`).
4. This file is untracked; keep it out of commits.
