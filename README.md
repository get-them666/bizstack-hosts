# BizStack Hosts

FastAPI + PostgreSQL web app for BizStack Hosts — short-term rental turnover cleaning and co-hosting management.

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
- **OpenAI AI agent** (`ai_agent.py`) — processes inbound SMS/voice text, falls back gracefully if no API key

## Railway variables

Set these variables on the service:

- `DATABASE_URL` — use Railway's Postgres reference, e.g. `${{Postgres.DATABASE_URL}}`
- `ADMIN_EMAIL`
- `ADMIN_PASSWORD`
- `APP_SECRET`
- `COOKIE_SECURE=true`
- `OPENAI_API_KEY` (optional)
- `OPENAI_MODEL=gpt-4o-mini` (optional)
- `SIGNALWIRE_PROJECT_ID` (optional)
- `SIGNALWIRE_API_TOKEN` (optional)
- `SIGNALWIRE_SPACE_URL=yourspace.signalwire.com` (optional)
- `SIGNALWIRE_PHONE` (optional, your SignalWire number, e.g. `+12025550100`)

The app creates its required tables automatically at startup and exposes `/health` for Railway health checks.

## SignalWire setup

1. Create an account at https://signalwire.com and get a phone number from your dashboard.
2. From the "API" page grab your **Project ID**, **API Token**, and **Space URL**.
3. In **Phone Numbers → your number → HTTP & Webhooks**, point:
   - **Messaging → Message URL** to `https://your-app.up.railway.app/comms/sms-webhook` (method POST)
   - **Voice → Voice URL** to `https://your-app.up.railway.app/comms/voice-webhook` (method POST)
4. Set the SignalWire env vars above in Railway.
5. Outbound SMS is available via `SignalWireService` in `signalwire_service.py` (e.g. sending secure entry links mid-call).

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