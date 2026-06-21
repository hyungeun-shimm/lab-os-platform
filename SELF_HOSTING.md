# Lab OS — Self-Hosting & Deployment Guide

This guide explains how to stand up Lab OS — first on your own computer, then as a
real always-on server with its own web address — the way the original author runs
it in production. Every personal value has been replaced with a `PLACEHOLDER` you
fill in.

> **Not a developer?** That's fine. You can hand this whole file to an AI coding
> assistant (Claude Code, Codex, Gemini, etc.) and say *"set this up for me on a
> fresh cloud VM, asking me for each PLACEHOLDER value as you go."* Each step below
> is written to be followed literally, top to bottom.

**Contents**
1. [What you're building (reference architecture)](#1-what-youre-building)
2. [Try it locally first (5 minutes)](#2-try-it-locally-first)
3. [Production deploy on a cloud VM](#3-production-deploy-on-a-cloud-vm)
4. [Give it a web address (free domain + HTTPS)](#4-give-it-a-web-address)
5. [Configure the integrations](#5-configure-the-integrations)
   - Animal database (Excel sync)
   - Cage-card photo → database auto-registration
   - Slack bot
   - Gemini AI experiment planning
   - Email (password resets)
6. [Backups & restore](#6-backups--restore)
7. [Updating the server later](#7-updating-the-server-later)
8. [Environment variables reference](#8-environment-variables-reference)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. What you're building

Lab OS is **one Python web server backed by one SQLite file**. Every lab computer
just opens a browser — there is no per-machine install and no file syncing.

The reference production setup the author uses:

```
   Browser (any lab computer, after login)
        │  HTTPS
        ▼
   Caddy  :443   ── automatic Let's Encrypt certificate
        │  proxies to http://127.0.0.1:8766
        ▼
   uvicorn / FastAPI  ── ONE process, managed by systemd
        │
        ▼
   scheduler.db  (SQLite file on the VM disk)
        │  continuously replicated
        ▼
   Cloud object storage (warm + cold backups)
```

| Concern | What the author chose | Why |
|---------|----------------------|-----|
| Server | 1 small cloud VM (e.g. GCP `e2-small`, 2 GB RAM, Ubuntu 22.04) | The app is tiny; one VM serves a whole lab |
| Process | a single `systemd` service, auto-reload **off** | Exactly one process → no duplicate-process / session bugs |
| Web address | a free `*.duckdns.org` subdomain (swap for a real domain later) | Instant, free, change one line later |
| HTTPS | Caddy (fetches + renews the certificate automatically) | Zero manual certificate handling |
| Backups | Litestream → object storage (warm) + nightly snapshot (cold, retention-locked) | Survives corruption *and* a malicious wipe |

You don't have to use Google Cloud — any Ubuntu VM (AWS, DigitalOcean, Hetzner, a
spare Linux box) works. The cloud-specific commands below use the `gcloud` CLI
because that's the reference; the VM steps (Caddy, systemd, backups) are identical
anywhere.

---

## 2. Try it locally first

Before any cloud work, confirm the app runs on your machine.

```bash
# Requires Python 3.9+
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py --port 8766
```

Open <http://127.0.0.1:8766/scheduler.html>. The ELN is at
<http://127.0.0.1:8766/eln/login> — **the first account you register becomes the
admin.** The database (`scheduler.db`) is created automatically. Stop with Ctrl-C.

If that works, you're ready to deploy.

---

## 3. Production deploy on a cloud VM

### Step 3.1 — Install the cloud CLI and create a project (on your computer)

```bash
# Install the Google Cloud CLI (or: https://cloud.google.com/sdk/docs/install)
brew install --cask google-cloud-sdk        # macOS

gcloud init
gcloud auth login

# Project IDs are globally unique — pick your own
gcloud projects create YOUR_PROJECT_ID --name="Lab OS"
gcloud config set project YOUR_PROJECT_ID

# Link billing to the project in the console, then enable Compute Engine:
#   https://console.cloud.google.com/billing
gcloud services enable compute.googleapis.com
```

### Step 3.2 — Create the VM, a fixed IP, and open the web ports

```bash
REGION=us-west1
ZONE=us-west1-b

# A static IP so your address never changes
gcloud compute addresses create labos-ip --region=$REGION
LABOS_IP=$(gcloud compute addresses describe labos-ip --region=$REGION --format='get(address)')
echo "Your server IP: $LABOS_IP"     # write this down

# Allow HTTPS + the certificate challenge from anywhere
gcloud compute firewall-rules create labos-allow-web \
  --allow=tcp:80,tcp:443 --source-ranges=0.0.0.0/0 \
  --description="Lab OS public HTTPS"

# The VM itself
gcloud compute instances create labos-vm \
  --zone=$ZONE --machine-type=e2-small \
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --boot-disk-size=20GB \
  --address=$LABOS_IP
```

### Step 3.3 — Upload the code

```bash
# From the folder that CONTAINS the Lab_OS folder, on your computer:
tar czf /tmp/labos.tgz --exclude='.venv' --exclude='__pycache__' \
    --exclude='*.log' --exclude='*.pid' --exclude='scheduler.db' Lab_OS

gcloud compute scp /tmp/labos.tgz labos-vm:~ --zone=$ZONE
```

### Step 3.4 — Install and start the app (on the VM)

```bash
gcloud compute ssh labos-vm --zone=$ZONE        # opens a shell ON the VM
# ---- everything below runs ON the VM ----

sudo mkdir -p /opt/labos
sudo tar xzf ~/labos.tgz -C /opt/labos --strip-components=1
cd /opt/labos

# System packages + a dedicated, locked-down service user
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip
id labos &>/dev/null || sudo useradd --system --home /opt/labos --shell /usr/sbin/nologin labos

# Build the virtualenv
sudo python3 -m venv .venv
sudo .venv/bin/pip install --upgrade pip wheel
sudo .venv/bin/pip install -r requirements.txt
sudo mkdir -p /opt/labos/secrets
sudo chown -R labos:labos /opt/labos
```

Create the service definition. The leading `-` on the `EnvironmentFile` lines makes
each one **optional**, so the app starts even before you've configured Slack/Gemini/SMTP.

```bash
sudo tee /etc/systemd/system/labos.service >/dev/null <<'UNIT'
[Unit]
Description=Lab OS (FastAPI / uvicorn)
After=network.target

[Service]
Type=simple
User=labos
Group=labos
WorkingDirectory=/opt/labos
# host 127.0.0.1 = only the local reverse proxy (Caddy) can reach it.
ExecStart=/opt/labos/.venv/bin/python server.py --host 127.0.0.1 --port 8766
Restart=always
RestartSec=3
# Mark the session cookie Secure (we are HTTPS-only behind Caddy)
Environment=LABOS_SECURE_COOKIES=1
# Optional secret files (created in Step 5). The '-' means "skip if missing".
EnvironmentFile=-/opt/labos/secrets/smtp.env
EnvironmentFile=-/opt/labos/secrets/autoschedule.env
EnvironmentFile=-/opt/labos/secrets/api.env

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now labos
sudo systemctl status labos --no-pager        # expect: active (running)
```

The app now answers on `127.0.0.1:8766` **inside the VM only**. Next we expose it
safely over HTTPS.

---

## 4. Give it a web address

### Step 4.1 — A free subdomain (DuckDNS)

1. Go to <https://www.duckdns.org> and sign in.
2. Create a subdomain, e.g. `YOUR-NAME-labos` → you get `YOUR-NAME-labos.duckdns.org`.
3. Set its IP to the `$LABOS_IP` you noted in Step 3.2.

(When you later get an institutional domain, just point its DNS A-record at the same
IP and change one line in the Caddy config below — nothing else moves.)

### Step 4.2 — HTTPS with Caddy (on the VM)

```bash
# ---- ON the VM ----
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy

# Reverse proxy + automatic certificate. Replace the hostname with YOUR domain.
sudo tee /etc/caddy/Caddyfile >/dev/null <<'CADDY'
YOUR-NAME-labos.duckdns.org {
    reverse_proxy 127.0.0.1:8766
    encode gzip
}
CADDY

sudo systemctl reload caddy
```

### Step 4.3 — Verify

From any computer, open:

```
https://YOUR-NAME-labos.duckdns.org/eln/login
```

Register the first account (it becomes admin), then add lab members from the admin
UI. Everyone is now on one server, one database, live. Handy checks on the VM:

```bash
sudo systemctl status labos     # the app (one process)
sudo systemctl status caddy     # the HTTPS proxy
journalctl -u labos -n 50       # recent app logs
```

---

## 5. Configure the integrations

All secrets live in small files under `/opt/labos/secrets/` (owner `labos`,
mode `600`). After editing any of them: `sudo systemctl restart labos`.

### Animal database (Excel sync)

The colony lives in the `animals` table, populated by reading a spreadsheet you
control. Lab OS **reads** the file at sync time; it never modifies your source.

1. Put an `.xlsx` somewhere the server can read it (for a single local install, a
   path on disk; for the cloud VM, see the "cloud master" note below).
2. Point the app at it (as a logged-in admin, from the app or via the API):
   ```bash
   curl -X POST https://YOUR-HOST/api/animals/config \
        -H "Content-Type: application/json" \
        -d '{"excel_path": "/path/to/colony.xlsx"}'
   ```
3. Trigger a sync from the sidebar "Sync now" button, or `POST /api/animals/sync`.

Expected columns (sheet `Sheet1`, header in row 1):
`Mice ID, Project, Condition, Exp, Cohort, Strain, DOB, Sex, DOS, Date in cond-1/2/3,
Days in cond-1/2/3, Sacrified (Y/N), Sacrified date, Note`. The sync is
**validating, non-destructive, and audited** — rows are diffed, never blindly
replaced, and a snapshot is taken before each run.

> **Cloud "master" option:** a cloud VM has no local spreadsheet. The author keeps
> the colony in a Google Sheet and has the server read it via the Google Sheets API
> with a read-only service account, on a schedule. That specific integration is not
> bundled in this public repo, but the pattern is simple: export the sheet rows in
> the same column layout above and feed them to the same validating sync used for
> Excel. Any "master" (Sheet, Airtable, a CSV drop) works as long as it yields those
> columns.

### Cage-card photo → database auto-registration

Instead of typing new mice in by hand, you photograph the cage card and the system
reads it. The pipeline is: **image → OCR → parse (LLM or rule-based) → upsert into
the `animals` table**.

- **From a terminal / AI assistant** (`animal_updater.py`):
  ```python
  from animal_updater import register_from_image
  r = register_from_image("/path/to/cage_card.jpg", project="my-project")
  print(r["diff"])     # preview of what was read; the upsert is already applied
  ```
- **From Slack**: send the cage-card photo to the bot (see below). It runs the same
  OCR + parse and registers the animal.
- **Parser**: if `OPENAI_API_KEY` is set, an LLM does the structured extraction;
  otherwise a built-in rule-based parser handles standard cage-card layouts. OCR
  uses `ocr_engine.py` (install an OCR backend such as Tesseract on the host).

This is the "snap a photo, it's in the DB" workflow — it writes straight to the
SQLite `animals` table, so it works on a plain local install with no cloud pieces.

### Slack bot

Natural-language scheduling + the cage-card intake above, over Slack.

1. Create an app at <https://api.slack.com/apps>.
2. Enable **Socket Mode** and create an App-Level Token (`xapp-…`, scope
   `connections:write`).
3. Add Bot Token Scopes (`chat:write`, `channels:read`, `im:read`, `im:write`,
   `im:history`), install to your workspace, and copy the Bot Token (`xoxb-…`).
4. On the VM, create the bot's secret/service files:
   ```bash
   sudo tee /opt/labos/secrets/bot.env >/dev/null <<'ENV'
   SLACK_BOT_TOKEN=xoxb-REPLACE
   SLACK_APP_TOKEN=xapp-REPLACE
   OPENAI_API_KEY=sk-REPLACE          # optional (enables LLM cage-card parsing)
   SCHEDULER_API=http://127.0.0.1:8766
   SLACK_POLL_CHANNELS=your-slack-channel
   LABOS_API_TOKEN=REPLACE            # shared token so the bot can call the app API
   ENV
   sudo chown labos:labos /opt/labos/secrets/bot.env && sudo chmod 600 /opt/labos/secrets/bot.env

   sudo tee /etc/systemd/system/labos-bot.service >/dev/null <<'UNIT'
   [Unit]
   Description=Lab OS Slack bot
   After=network-online.target labos.service
   Wants=network-online.target
   [Service]
   Type=simple
   User=labos
   Group=labos
   WorkingDirectory=/opt/labos
   Environment=PYTHONUNBUFFERED=1
   EnvironmentFile=/opt/labos/secrets/bot.env
   ExecStart=/opt/labos/.venv/bin/python slack_bot.py
   Restart=always
   RestartSec=10
   [Install]
   WantedBy=multi-user.target
   UNIT
   sudo systemctl daemon-reload && sudo systemctl enable --now labos-bot
   ```

If you set `LABOS_API_TOKEN` in the bot file, also put the **same** value in
`/opt/labos/secrets/api.env` as `LABOS_API_TOKEN=…` so the app accepts the bot's
calls. (Bot replies are in Korean by design; the rest of the UI is English.)

### Gemini AI experiment planning

The scheduler's AI button turns a plain-English request ("schedule cohort A OKR
testing next week, skip weekends") into structured calendar entries. It uses Google
Gemini as the parser; a deterministic engine then builds the actual events, so the
AI never writes to your data directly — it only fills in parameters you preview.

1. Get a **free** key from Google AI Studio: <https://aistudio.google.com/app/apikey>.
2. On the VM:
   ```bash
   sudo tee /opt/labos/secrets/autoschedule.env >/dev/null <<'ENV'
   GEMINI_API_KEY=REPLACE
   GEMINI_MODEL=gemini-2.5-flash
   ENV
   sudo chown labos:labos /opt/labos/secrets/autoschedule.env && sudo chmod 600 /opt/labos/secrets/autoschedule.env
   sudo systemctl restart labos
   ```

### Email (password resets)

Optional. To send password-reset codes by email, drop SMTP credentials (e.g. a
Gmail app password) into `/opt/labos/secrets/smtp.env`:

```bash
sudo tee /opt/labos/secrets/smtp.env >/dev/null <<'ENV'
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@example.com
SMTP_PASS=REPLACE_APP_PASSWORD
SMTP_FROM=you@example.com
ENV
sudo chown labos:labos /opt/labos/secrets/smtp.env && sudo chmod 600 /opt/labos/secrets/smtp.env
sudo systemctl restart labos
```

Without SMTP, password-reset codes are returned in the API response (fine for a
small lab; switch to real email for anything bigger).

---

## 6. Backups & restore

The entire app is one SQLite file. The author runs **two** layers so the data
survives both accidental corruption and a malicious wipe.

| Layer | What it is | Recovers |
|-------|-----------|----------|
| **Warm** — Litestream | Continuous (~10 s) replication to object storage, 30-day point-in-time history | "Undo the last hour/day" |
| **Cold** — nightly snapshot | One gzipped copy per night into a **retention-locked** bucket | Insurance — un-deletable even if the VM key is stolen |

The key idea behind the cold layer: the credential Litestream uses *can* delete its
own data, so an attacker with the VM key could erase the warm copy. The cold bucket
has a storage **retention policy**, which makes last night's snapshot impossible to
delete (by anyone) until it ages out.

### Warm layer (Litestream → object storage, on the VM)

```bash
# Install Litestream (pin to the 0.3.x line — its config matches below)
LS_VER=0.3.13
curl -fsSL -o /tmp/litestream.deb \
  "https://github.com/benbjohnson/litestream/releases/download/v${LS_VER}/litestream-v${LS_VER}-linux-amd64.deb"
sudo dpkg -i /tmp/litestream.deb

# A bucket for warm replicas (create it with your cloud's CLI first), then:
sudo tee /etc/litestream.yml >/dev/null <<'YML'
dbs:
  - path: /opt/labos/scheduler.db
    replicas:
      - type: gcs
        bucket: YOUR_WARM_BUCKET
        path: scheduler.db
        retention: 720h          # 30 days
        snapshot-interval: 24h
        sync-interval: 10s
YML

# Litestream authenticates with a dedicated storage credential (not the VM's
# default identity). Put a service-account key at /opt/labos/secrets/backup-sa.json
# (owner labos, mode 600), then:
sudo tee /etc/systemd/system/litestream.service >/dev/null <<'UNIT'
[Unit]
Description=Litestream (continuous SQLite backup)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
Environment=GOOGLE_APPLICATION_CREDENTIALS=/opt/labos/secrets/backup-sa.json
ExecStart=/usr/bin/litestream replicate -config /etc/litestream.yml
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now litestream
```

### Cold layer (nightly snapshot)

Create a second bucket with a retention policy (e.g. 1 year), give the backup
service account create+read but **not** delete on it, then add a root cron entry
that copies a consistent snapshot nightly:

```bash
# crontab -e  (as root), 03:30 UTC every night:
30 3 * * *  GOOGLE_APPLICATION_CREDENTIALS=/opt/labos/secrets/backup-sa.json \
            /opt/labos/.venv/bin/python - <<'PY' >> /var/log/labos-backup.log 2>&1
import sqlite3, gzip, datetime, subprocess
ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
src = sqlite3.connect("/opt/labos/scheduler.db")
dst = sqlite3.connect("/tmp/snap.db"); src.backup(dst); dst.close(); src.close()
with open("/tmp/snap.db","rb") as f, gzip.open("/tmp/snap.db.gz","wb") as g: g.writelines(f)
subprocess.run(["gcloud","storage","cp","/tmp/snap.db.gz",
                f"gs://YOUR_COLD_BUCKET/snapshots/scheduler-{ts}.db.gz"], check=True)
PY
```

(The Python online-backup API gives a consistent copy without stopping the app.)

### Restore

```bash
# Stop the app, move the current DB aside, restore, restart.
sudo systemctl stop labos
sudo mv /opt/labos/scheduler.db /opt/labos/scheduler.db.pre-restore

# Newest warm replica:
sudo GOOGLE_APPLICATION_CREDENTIALS=/opt/labos/secrets/backup-sa.json \
  litestream restore -o /opt/labos/scheduler.db gcs://YOUR_WARM_BUCKET/scheduler.db
# …or rewind to a moment:  add  -timestamp 2026-01-01T08:00:00Z
# …or from a cold snapshot: gunzip the chosen file into /opt/labos/scheduler.db

sudo chown labos:labos /opt/labos/scheduler.db
sudo systemctl start labos
```

---

## 7. Updating the server later

When you change the code, ship the new version and restart:

```bash
# From your computer — bundle only what you want, copy it up, re-extract, restart.
tar czf /tmp/labos.tgz --exclude='.venv' --exclude='__pycache__' \
    --exclude='*.log' --exclude='*.pid' --exclude='scheduler.db' Lab_OS
gcloud compute scp /tmp/labos.tgz labos-vm:~ --zone=$ZONE
gcloud compute ssh labos-vm --zone=$ZONE --command '
  sudo tar xzf ~/labos.tgz -C /opt/labos --strip-components=1 &&
  sudo chown -R labos:labos /opt/labos &&
  sudo systemctl restart labos'
```

The database, virtualenv, logs, and `secrets/` on the VM are never overwritten by
this (you only ship code). HTML/CSS/JS changes take effect immediately; Python
changes need the restart shown above.

---

## 8. Environment variables reference

| Variable | Used by | Purpose |
|----------|---------|---------|
| `SCHEDULER_PORT` | app | Port (default 8766) |
| `LABOS_SECURE_COOKIES` | app | `1` → mark the session cookie Secure (HTTPS only) |
| `LABOS_API_TOKEN` | app + bot | Shared token for machine callers (bot/rigs); browsers use the session cookie |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | app | AI autoschedule (free Google AI Studio key) |
| `SMTP_HOST/PORT/USER/PASS/FROM` | app | Password-reset emails (optional) |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | bot | Slack credentials (Socket Mode) |
| `OPENAI_API_KEY` | bot / animal_updater | LLM cage-card parsing (optional; rule-based fallback) |
| `SLACK_POLL_CHANNELS` | bot | Channel(s) the bot watches |
| `SCHEDULER_PMS_PATH` | app | Optional: a sibling folder of project subdirectories to list as projects |

Copy `.env.example` to `.env` for local runs; on the VM these live in the
`secrets/*.env` files referenced by the systemd units.

---

## 9. Troubleshooting

- **App won't start:** `journalctl -u labos -n 80` on the VM. Most often a missing
  dependency or a malformed `secrets/*.env`.
- **Browser can't reach it / no certificate:** confirm the DuckDNS IP matches
  `$LABOS_IP`, that firewall ports 80+443 are open, and `sudo systemctl status caddy`.
  Caddy needs port 80 reachable to obtain the certificate.
- **Animal sync does nothing:** check the configured path exists and is readable by
  the `labos` user, and that the column headers match Section 5 exactly.
- **AI button errors:** `GEMINI_API_KEY` not set, or the key has no quota.
- **Two processes / weird sessions:** there should be exactly one — `systemctl`
  manages it and reload is off. Don't also run `python server.py` by hand on the VM.

---

Questions or improvements are welcome via issues/PRs. License: [MIT](LICENSE).
