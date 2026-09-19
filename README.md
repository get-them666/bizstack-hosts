# BizStack Hosts

Multi-brand host platform serving two businesses from a single FastAPI + PostgreSQL app on Railway:

- **Broom Service** — short-term rental turnover cleaning & co-hosting management
- **Buildstack Construction** — residential construction, remodeling & instant quoting

One Railway service, one database (company-tagged rows). A host dispatcher routes construction domains to the mounted construction sub-app; shared capabilities (unified chat, phone bot, Stripe webhooks, static assets, finance) always resolve to the Broom app. See `main.py` → `_RoutingApp`.

---

## Broom Service

- **Landing page** (`/`) — marketing site with lead capture (`POST /submit-lead`)
- **Customer portal** — login, dashboard, host management, messaging center, settings
- **Dashboard** (`/dashboard`) — live stats, turnover calendar grid, quick booking form
- **Hosts** (`/hosts`) — hosts/customers + lead pipeline
- **Messaging** (`/comms`) — SignalWire SMS/voice webhook logs and AI agent status
- **Calendar** — `/api/calendar` (list), `/api/calendar/book` (create with conflict detection)
- **SignalWire** — AI voice assistant + SMS:
  - `POST /comms/sms-webhook` — inbound SMS → AI reply (TwiML)
  - `POST /comms/voice-webhook` — inbound voice → greeting + recording
  - `POST /comms/voice-action` / `POST /comms/voice-transcribe` — call callbacks
  - Outbound call variants under `/comms/outbound-voice-*`
- **Stripe per-booking payments** — hosted Checkout, guest-funded turnover fees:
  - Booking creates a Checkout session and redirects the guest to Stripe
  - `POST /api/payments/webhook` marks a booking paid on `checkout.session.completed`
  - `/api/payments/create-link/<event_id>` re-creates a payment link for unpaid bookings
  - `/payments/success` + `/payments/cancel`
- **OpenAI AI agent** (`ai_agent.py`, `bot_knowledge.md`) — processes inbound SMS/voice, knows the company/services/pricing/navigation, and via tool calling can check availability, create bookings, generate Stripe links, look up bookings by phone, register customers, and report stats
- **Rental Revenue Analysis** (`analysis_service.py`) — property address → live report at `/analysis/<id>` (map, value, income, rents, nightly rate, self-managed vs. co-hosted Airbnb comparison) via realestateapi.com
- **Channel Manager Sync** (`channel_sync.py`) — imports Hospitable reservations into `calendar_events`
- **Documents & legal forms** (`legal_forms.py`, `training_service.py`) — prefab docs with signature blocks + onboarding decks/quizzes

## Buildstack Construction

- **Marketing site** — `/services`, `/about`, `/portfolio`, `/quote`, `/contact`, `/legal`
- **Instant quoting** (`property_service.py`, `estimating_service.py`) — address → ballpark estimate. Property lookup via RentCast; cost models seeded from supplier price books and local labor rates (wide ranges by design)
- **Job leads (permit radar)** (`permit_service.py`) — `GET /job-leads`
  - Real building permits from **Shovels.ai v2** (`X-API-Key`, `https://api.shovels.ai/v2`)
  - Default service area: **Chesapeake / Virginia Beach / Norfolk** (Hampton Roads)
  - Background importer: first pass backfills `PERMIT_BACKFILL_DAYS` (21), then runs incrementally every `PERMIT_SCAN_HOURS` (24)
  - Dedupes into `job_leads` and auto-creates `permit_finder` leads for homeowner-driven jobs, notifying the owner
  - Routes: `POST /api/job-leads/refresh`, `/api/job-leads/{id}/convert|dismiss|delete`
- **Acquisition radar** — `POST /acquisition/scan/reddit`, `POST /acquisition/scan/linkedin`
- **Lead pipeline** (`/leads`) — status, notes, delete, `POST /api/leads/{id}/deposit-link`
- **Projects & backlog** — `/projects`, `/backlog` with assign/unassign
- **Crew portal** — `/crew` login, `/crew/dashboard`, clock in/out, job photos, hours
- **Payroll** — `/payroll`, timesheet approve/reject, run/finalize, direct deposit
- **SBA microloan outreach** (`loan_outreach.py`) — Day 1/3/7/14 email cadence + AI-driven replies; inbound mail poller

## Shared services

- `documents_service.py` — email delivery + document generation. Sends via **Resend** (preferred), **AWS SES**, then SMTP fallback ladder; `SMTP_FROM` is the visible sender
- `email_bot.py` — IMAP inbox poller (auto-replies)
- `stripe_service.py` — Checkout, payment links, webhooks, Connect
- `signalwire_service.py` — SMS/voice
- `calendar_service.py`, `training_service.py`, `site_theme.py`

---

## Railway variables

Core:

- `DATABASE_URL` — Railway Postgres reference, e.g. `${{Postgres.DATABASE_URL}}`
- `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `APP_SECRET`, `COOKIE_SECURE=true`
- `APP_BASE_URL=https://bizstackperks.com`
- `OPENAI_API_KEY` (optional), `OPENAI_MODEL=gpt-4o-mini`, `APP_TIMEZONE=America/New_York`, `BOOKING_HOURS=1`

Email / notifications:

- `SMTP_FROM` — visible From address (e.g. `hello@bizstackperks.com`), `SMTP_NAME`
- `RESEND_API_KEY` — preferred sender over HTTPS
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `SES_REGION` — SES fallback
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_TLS` — SMTP fallback ladder
- `IMAP_HOST`, `IMAP_USERNAME`, `IMAP_PASSWORD` — inbound mail poller
- `NOTIFY_EMAIL` — comma-separated owner alert recipients
- `NOTIFY_SEND_DELAY` (default `0.35`) — seconds between notification sends (avoids provider rate limits)

Integrations:

- SignalWire: `SIGNALWIRE_PROJECT_ID`, `SIGNALWIRE_API_TOKEN`, `SIGNALWIRE_SPACE_URL`, `SIGNALWIRE_PHONE`
- Stripe: `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_TURNOVER`/`_DEEP`/`_LINEN`/`_INSPECTION` (defaults 120/200/50/75)
- `REALESTATE_API_KEY` — rental analysis
- `HOSPITABLE_PAT` — channel sync
- `APIFY_TOKEN` — LinkedIn radar

Permit radar (Buildstack):

- `SHOVELS_API_KEY` — required to enable live permits (otherwise demo seed)
- `PERMIT_GEO_IDS` — override ZIP/geo list (defaults to Hampton Roads)
- `PERMIT_BACKFILL_DAYS` (21), `PERMIT_INCREMENTAL_DAYS` (2), `PERMIT_SCAN_HOURS` (24)
- `PERMIT_MAX_PER_GEO` (4), `PERMIT_MAX_TOTAL` (120)
- `PERMIT_MIN_VALUE` (10000), `PERMIT_MIN_JOB` (2000)
- `PERMIT_NOTIFY_DELAY` (0.4) — seconds between lead notifications
- `DISABLE_PERMIT_IMPORT` — set to skip the auto-import scheduler (also `DISABLE_LOAN_OUTREACH`, `DISABLE_EMAIL_BOT`)

The app creates its required tables automatically at startup and exposes `/health` for Railway health checks.

## SignalWire setup

1. Create an account at https://signalwire.com and get a phone number.
2. From the "API" page grab your **Project ID**, **API Token**, and **Space URL**.
3. In **Phone Numbers → your number → HTTP & Webhooks**, point:
   - **Messaging → Message URL** to `https://<domain>/comms/sms-webhook` (POST)
   - **Voice → Voice URL** to `https://<domain>/comms/voice-webhook` (POST)
4. Set the SignalWire env vars in Railway.
5. Outbound SMS via `SignalWireService` in `signalwire_service.py`.

## Stripe setup

1. Create/activate an account at https://dashboard.stripe.com.
2. Grab keys from **Developers → API keys**.
3. Set `STRIPE_SECRET_KEY` (+ optionally `STRIPE_PUBLISHABLE_KEY`).
4. Create a webhook endpoint (**Developers → Webhooks**):
   - URL: `https://<domain>/api/payments/webhook`
   - Events: `checkout.session.completed`
   - Put the signing secret (`whsec_...`) in `STRIPE_WEBHOOK_SECRET`.
5. Set `APP_BASE_URL` to your public domain.

> Local testing: use a Stripe test key and `stripe listen --forward-to localhost:8000/api/payments/webhook`.

## Shovels.ai (permit radar) setup

1. Get an API key at https://www.shovels.ai and set `SHOVELS_API_KEY` on the service.
2. Adjust the service area with `PERMIT_GEO_IDS` (comma-separated ZIPs) if needed.
3. The scheduler runs at boot and every `PERMIT_SCAN_HOURS`; a manual refresh is available from `/job-leads`. Watch the free-tier credit budget by tuning `PERMIT_MAX_PER_GEO` / `PERMIT_MAX_TOTAL`.

## Local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql://...'
export ADMIN_EMAIL='admin@example.com'
export ADMIN_PASSWORD='...'
export APP_SECRET='...'
uvicorn main:app --reload
```
