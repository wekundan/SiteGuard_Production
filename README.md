# SiteGuard Breakdown Tracker

FastAPI + Postgres + single-page mobile web app for tracking machinery breakdowns.

## Free demo deployment (Render free web + Neon free Postgres)
1. Create a free project at [neon.tech](https://neon.tech) and copy its connection string
   (looks like `postgresql://user:pass@host/db?sslmode=require`).
2. Push this folder to GitHub.
3. On [Render](https://render.com): **New + -> Blueprint** -> select the repo -> when prompted,
   paste the Neon string into `DATABASE_URL` -> **Apply**.
4. Open the Render URL on any phone. Tables and demo data are created on first boot.

Login: users `ramesh`, `deepak` (site office), `vikram` (supervisor), `ananya` (head office),
`rajeshwari` (management). Their password is the `DEMO_PASSWORD` env var (set it in Render before
first boot; default for local runs is `siteguard123`). Passwords are only applied when the DB is first
seeded. Set `SECRET_KEY` too (the blueprint generates one).

Roles: site office/supervisor report; supervisor inspects and marks working; head office raises POs;
management sees the dashboard and can reset the demo. Every status change is logged in `ticket_events`.

SLAs (shown as "Due in / Overdue" badges, computed on the server):
- Inspection: `SLA_INSPECTION_HOURS` (default 2) from report time.
- Head office action: `SLA_HO_ACTION_HOURS` (default 24, placeholder) from escalation time.
- Local part / in transit: end of the ETA date in the site timezone (`SITE_UTC_OFFSET_MINUTES`, default 330 = IST).
Alerts (WhatsApp/email) are not built yet; overdue shows in the app only.

Schema changes: tables are created with `create_all` and are not migrated. If you already ran an older
version against a database, drop its tables (or use a fresh Neon DB) before deploying this version.

Smoke test (API rules + roles): `pip install httpx` then `python smoke_test.py`.

Notes:
- Free Render service sleeps after ~15 min idle (first load takes ~30s). Optional: ping the URL
  every 5 min with UptimeRobot to keep it awake.
- Do not use Render's free Postgres: it is deleted after ~30 days.
- The **Reset Demo** button wipes all data and re-seeds. Set env var `DEMO_RESET=0` to disable.
- No login rate-limiting yet, and tokens live in browser localStorage. Fine for a demo with fake data.

## Run locally
```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```
Without `DATABASE_URL` it uses a local SQLite file. `docker compose up --build` runs Postgres + app.
To test from a phone, open `http://<laptop-ip>:8000` on the same Wi-Fi.
