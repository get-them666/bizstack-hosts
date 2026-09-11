# BizStack Hosts

FastAPI + PostgreSQL web app for BizStack Hosts.

## Railway variables
Set these variables on the service:

- `DATABASE_URL` — use Railway's Postgres reference, e.g. `${{Postgres.DATABASE_URL}}`
- `ADMIN_EMAIL`
- `ADMIN_PASSWORD`
- `APP_SECRET`
- `COOKIE_SECURE=true`
- `OPENAI_API_KEY` (optional)
- `OPENAI_MODEL=gpt-4o-mini` (optional)

The app creates its required tables automatically at startup and exposes `/health` for Railway health checks.

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
# bizstack-hosts
