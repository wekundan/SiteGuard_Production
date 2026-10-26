# SiteGuard Breakdown Tracker

FastAPI + Postgres + single-page mobile web app that follows the plant breakdown SOP.

## Workflow
1. **Report** (Sr Civil Engineer or P&M DEO) -> 2. **Inspect and decide** (Mechanical Engineer or P&M In-charge): in-house or outside workshop.
- **In-house:** site store check. Parts available -> repair. Not available -> requisition -> **central store dispatches** -> **site store confirms receipt** -> repair.
- **Outside:** quotation pending / requested (remarks) -> upload vendor quotation -> enter quote details -> **management approves** (or sends back) -> **mech/PM logs dispatch to vendor** -> repair at vendor -> **mech/PM confirms vehicle received at site** (breakdown cleared) -> DEO/PM closes register with bill and cost.
3. Mechanical dept marks the **breakdown cleared** (machine active again) -> 4. **DEO or P&M In-charge closes** the register entry with root cause and total repair cost. Downtime is calculated automatically.

A machine can be reported again once its previous breakdown is cleared, even if the register entry is still open.
Every status change is recorded in `ticket_events` (who, when, from, to, note); the Board tab shows the history.

## Quotation and bill attachments
- **Quotation:** after outside repair is chosen, mech/PM log request remarks, then upload the vendor PDF/JPG/PNG when it arrives (upload moves the ticket to the vendor-quotation stage). Additional files can be attached before submitting to management (up to 5 per kind). Management sees files on the approval card.
- **Dispatch (in-house requisition):** central store uploads the dispatch challan/PO PDF (or photo) while the ticket is **Awaiting central store**, then confirms dispatch. Set `REQUIRE_DISPATCH_DOCS=0` to make the upload optional.
- **Bill:** for outside repairs, the DEO or P&M In-charge must attach the vendor bill (and may enter an invoice number) when closing.
  The final billed amount is compared with the approved quotation; anything above it is flagged. Set `REQUIRE_BILL_FOR_OUTSIDE=0` to make the bill optional.
- **Storage:** files are stored in the database (a `ticket_files` table), max 5 MB each (`MAX_UPLOAD_BYTES`). The file type is checked from its contents,
  and only logged-in users can open files. Neon's free tier is about 0.5 GB shared with everything else, so move to S3/R2 if volume grows
  (storage is isolated in `upload_file` / `download_file` in `main.py`).

## Users and roles
All demo users share the password in the `DEMO_PASSWORD` env var (local default is in `main.py`).

| Username | Role | Can do |
|---|---|---|
| `ramesh` | Sr Civil Engineer | Report breakdowns |
| `deepak` | P&M DEO | Report; close register entry |
| `vikram` | Mechanical Engineer | Inspect, parts check, quotation, mark cleared |
| `meera` | P&M In-charge | Same as mechanical engineer, plus close register entry |
| `suresh` | Site Store | Confirm receipt of spares |
| `ananya` | Central Store | Dispatch spares |
| `rajeshwari` | Management | Approve quotations, dashboard, reset demo |

Passwords are applied only when the database is first seeded. Everyone can see the Board; roles are enforced by the server.

## SLAs
Shown as "Due in / Overdue" badges, computed on the server. Hours per stage default to: inspection 2, site store check 4,
central store dispatch 24, in transit 48, quotation 24, approval 24, repair 72, record closure 24 (placeholders).
Override with env vars like `SLA_PENDING_INSPECTION_HOURS`. In transit and repair use the ETA date (end of day, `SITE_UTC_OFFSET_MINUTES`,
default 330 = IST) when one is entered. Alerts (WhatsApp/email) are not built yet.

## Free demo deployment (Render free web + Neon free Postgres)
1. Create a free project at [neon.tech](https://neon.tech) and copy its connection string.
2. Push this folder to GitHub.
3. On [Render](https://render.com): **New + -> Blueprint** -> select the repo -> paste the Neon string into `DATABASE_URL`
   and set `DEMO_PASSWORD` -> **Apply**.
4. Open the Render URL on a phone.

**Redeploying over the previous (SOP) version** needs no reset: the new `ticket_files` table and `invoice_number` column are added automatically.

**Upgrading from the very first demo version:** that layout is incompatible. Create a fresh Neon database (or run
`DROP SCHEMA public CASCADE; CREATE SCHEMA public;` in Neon's SQL editor) and update `DATABASE_URL`. The app refuses to start
on the old layout and says so in the logs (Render then shows a failed GitHub deployment for `siteguard-breakdown-tracker`).

**After pulling workflow changes:** restart or redeploy once so PostgreSQL picks up new `ticket_status` values such as
`quotation_requested` (done automatically on startup).

Notes:
- Free Render service sleeps after ~15 min idle (first load ~30s). Optional: ping it every 5 min with UptimeRobot.
- Don't use Render's free Postgres: it is deleted after ~30 days.
- **Reset Demo** (management) wipes all data and re-seeds. Set `DEMO_RESET=0` to disable.
- No login rate-limiting yet, and tokens live in browser localStorage. Fine for a demo with fake data.

## Run locally
```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```
Without `DATABASE_URL` it uses a local SQLite file (delete `siteguard_local.db` after pulling schema changes).
`docker compose up --build` runs Postgres + app. For phone testing open `http://<laptop-ip>:8000` on the same Wi-Fi.

Smoke test (workflow, roles, SLA, metrics): `pip install httpx` then `python smoke_test.py`.
