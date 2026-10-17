# SiteGuard Breakdown Tracker — Real-World Deployment Package

This folder is ready to deploy to the cloud (Render, Railway, Fly.io) or run on a local machine/server with Docker so you and your site team can use it on real phones over 4G/5G or Wi-Fi.

## Option 1: Free Cloud Deployment on Render.com (Recommended — Public HTTPS Link for Any Phone)
1. Create a new GitHub repository and upload the files in this folder (`main.py`, `requirements.txt`, `render.yaml`, `Dockerfile`, `static/`, `sql/`).
2. Sign in to [Render.com](https://render.com/) -> Click **New +** -> **Blueprint**.
3. Select your GitHub repository. Render will automatically read `render.yaml` and create:
   - A managed **PostgreSQL database** (`siteguard-postgres`)
   - The **FastAPI + Mobile PWA web service** (`siteguard-breakdown-tracker`)
4. Click **Apply**. In ~2 minutes, Render gives you a live URL like `https://siteguard-breakdown-tracker.onrender.com`.
   - On first boot, `main.py` automatically creates the PostgreSQL tables and seeds your initial machines and users.
   - Share that `https://...` link on WhatsApp with your Site Mechanics, Supervisors, and Head Office staff — they can open it on any phone and tap **Add to Home Screen**.

## Option 2: Run Locally & Test From Your Phone Over Wi-Fi / Hotspot
1. Open a terminal in this folder and run:
   ```bash
   pip install -r requirements.txt
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```
   *(Or run `docker compose up --build` to launch both PostgreSQL 16 and FastAPI in Docker).*
2. Find your laptop's local Wi-Fi IP address (`ipconfig` on Windows, `ifconfig` or `ipconfig getifaddr en0` on Mac).
3. On your phone (connected to the same Wi-Fi or phone hotspot), open Chrome or Safari and visit:
   `http://<YOUR-LAPTOP-IP>:8000`
