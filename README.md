# Broom Service

FastAPI + PostgreSQL web app for Broom Service — short-term rental turnover cleaning and co-hosting management.

## Features

- **Landing page** (`/`) — marketing site with lead capture form (posts to `/submit-lead`)
- **Customer portal** — login, dashboard, host management, messaging center, settings
- **Dashboard** (`/dashboard`) — live stats, turnover calendar grid, quick booking form
- **Hosts** (`/hosts`) — create hosts/customers, view lead pipeline from landing form
- **Messaging** (`/comms`) — SignalWire SMS/voice webhook logs and AI agent status
- **Calendar API** — `/api/calendar` (list), `/api/calendar/book` (create with conflict detection)
- **SignalWire integration** — AI voice assistant + SMS. Webhooks:
  - `POST /comms/sms-webhook` — inbound SMS → AI reply (TwiML)
  - `POST /comms/voice-webhook` — inbound voice → greeting + recording (TwiML)
  - `POST /comms/voice-action` / `POST /comms/voice-transcribe` — call handling callbacks
- **Stripe per-booking payments** — hosted Checkout, guest-funded turnover fees:
  - Dashboard booking creates a checkout session and redirects the guest to Stripe
  - `POST /api/payments/webhook` auto-marks a booking paid on `checkout.session.completed`
  - `/api/payments/create-link/<event_id>` re-creates a payment link for unpaid bookings
  - `/payments/success` + `/payments/cancel` confirmation pages
- **OpenAI AI agent** (`ai_agent.py`) — processes inbound SMS/voice text, falls back gracefully if no API key
- **AI assistant runs the business** (`bot_knowledge.md`) — the bot knows the company, services, pricing, site navigation, and hospitality/STR industry. Via OpenAI tool calling it can check availability, **create bookings**, generate **Stripe payment links**, look up bookings by phone, register customers, and report business stats. Reply style is tuned to sound like a real human.
- **Free Rental Revenue Analysis** (`analysis_service.py`) — submitting a property address on the landing page generates a live report at `/analysis/<id>` with a map, home value, income, rents, nightly rate, and a realistic Airbnb earnings comparison (self-managed vs. Broom Service co-hosting). Powered by realestateapi.com (set `REALESTATE_API_KEY`; free key at https://www.realestateapi.com).

## Railway variables

Set these variables on the service:

- `DATABASE_URL` — use Railway's Postgres reference, e.g. `${{Postgres.DATABASE_URL}}`
- `ADMIN_EMAIL`
- `ADMIN_PASSWORD`
- `APP_SECRET`
- `COOKIE_SECURE=true`
- `OPENAI_API_KEY` (optional)
- `OPENAI_MODEL=gpt-4o-mini` (optional)
- `APP_TIMEZONE=America/New_York` (optional, timezone the AI uses for bookings)
- `BOOKING_HOURS=1` (optional, slot length for AI-assisted bookings)
- `SIGNALWIRE_PROJECT_ID` (optional)
- `SIGNALWIRE_API_TOKEN` (optional)
- `SIGNALWIRE_SPACE_URL=yourspace.signalwire.com` (optional)
- `SIGNALWIRE_PHONE` (optional, your SignalWire number, e.g. `+12025550100`)
- `STRIPE_SECRET_KEY` (required for payments)
- `STRIPE_PUBLISHABLE_KEY` (optional)
- `STRIPE_WEBHOOK_SECRET` (required for payment confirmation)
- `APP_BASE_URL=https://your-app.up.railway.app` (used for Checkout success/cancel redirects)
- `REALESTATE_API_KEY` (free key from https://www.realestateapi.com — powers the rental analysis)
- `STRIPE_PRICE_TURNOVER`, `STRIPE_PRICE_DEEP`, `STRIPE_PRICE_LINEN`, `STRIPE_PRICE_INSPECTION` (per-service USD prices; defaults 120/200/50/75)

The app creates its required tables automatically at startup and exposes `/health` for Railway health checks.

## SignalWire setup

1. Create an account at https://signalwire.com and get a phone number from your dashboard.
2. From the "API" page grab your **Project ID**, **API Token**, and **Space URL**.
3. In **Phone Numbers → your number → HTTP & Webhooks**, point:
   - **Messaging → Message URL** to `https://your-app.up.railway.app/comms/sms-webhook` (method POST)
   - **Voice → Voice URL** to `https://your-app.up.railway.app/comms/voice-webhook` (method POST)
4. Set the SignalWire env vars above in Railway.
5. Outbound SMS is available via `SignalWireService` in `signalwire_service.py` (e.g. sending secure entry links mid-call).

## Stripe setup

1. Create an account / activate at https://dashboard.stripe.com.
2. Grab a **test key** from **Developers → API keys** (`sk_test_...`).
3. Set `STRIPE_SECRET_KEY` (+ optionally `STRIPE_PUBLISHABLE_KEY`) in Railway.
4. Create a webhook endpoint in **Developers → Webhooks**:
   - URL: `https://your-app.up.railway.app/api/payments/webhook`
   - Events: `checkout.session.completed`
   - Copy the signing secret (`whsec_...`) into `STRIPE_WEBHOOK_SECRET`.
5. Set `APP_BASE_URL` to your public domain.
6. Prices default to Turnover $120 / Deep $200 / Linen $50 / Inspection $75 — override via `STRIPE_PRICE_*`.
7. In the dashboard, booking an operation creates a Stripe checkout link to send the guest. Unpaid bookings show a **Payment Link** button. A booking is marked *Paid* automatically when the Stripe webhook confirms the charge.

> Local testing: use a Stripe test key and `stripe listen --forward-to localhost:8000/api/payments/webhook`.

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