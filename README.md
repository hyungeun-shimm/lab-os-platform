# Lab OS

A self-hosted lab-management web app for a research (e.g. neuroscience) lab — a
single FastAPI + SQLite server that bundles a **scheduler**, an **animal/colony
database**, an **electronic lab notebook (ELN)**, an **activity dashboard**, and an
optional **Slack assistant bot**. No build step, no frontend frameworks — each page
is a self-contained HTML file of vanilla JS.

> This repository is the **public, sanitized mirror** of a private lab deployment.
> It ships generic placeholder configuration only — no real credentials, hosts, or
> lab data. Bring your own `.env` and data.

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Step-by-step setup](#step-by-step-setup)
  - [A. Run it locally (try it in 3 steps)](#a-run-it-locally)
  - [B. Deploy it as a real server (7 steps)](#b-deploy-it-as-a-real-server)
- [Configuration](#configuration)
- [The vision — where this is going](#the-vision--where-this-is-going)
- [Extending Lab OS](#extending-lab-os)
- [Documentation](#documentation)
- [License](#license)

---

## Features

- **Scheduler** — week / day / month calendar for surgeries, experiments, manuscript
  deadlines, histology timelines, and team assignments. Drag-to-move/resize, recurring
  events, categories, per-project & per-member colors, iCal / CSV / PDF export, browser
  reminders, and a natural-language **AI auto-schedule** (Gemini) button.
- **Animal DB** — a colony database synced from a spreadsheet (validating,
  non-destructive, audited sync). Mouse profiles, breeders, cohorts, conditions, and a
  searchable picker shared with the scheduler and ELN. **Cage-card photo → DB**
  auto-registration via OCR + LLM parsing.
- **ELN (Electronic Lab Notebook)** — multi-user, per-project, per-date notes with a
  Submit → Approve workflow, mouse-ID auto-fill, and experiment-type pages (slice
  recording, behavior, histology, overview).
- **Dashboard** — lab activity at a glance: notes by user and by project, status
  rollups, and an append-only audit log.
- **Slack bot** (optional) — natural-language scheduling and cage-card intake from a
  phone photo. (Bot replies are Korean by design; the rest of the UI is English.)

## How it works

One Python web server, one SQLite file. Every lab computer just opens a browser.

```
Browser ──HTTPS──▶ Caddy (auto-TLS) ──▶ uvicorn/FastAPI (one process) ──▶ scheduler.db (SQLite)
```

**Tech stack:** FastAPI · SQLite · vanilla-JS single-file frontends · slack-bolt ·
APScheduler · pandas / openpyxl. Python 3.9+. No build step.

---

## Step-by-step setup

### A. Run it locally

Good for trying it out. Takes a few minutes.

1. **Get the code & dependencies**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. **Start the server**
   ```bash
   python server.py --port 8766       # or, on macOS, double-click start_scheduler.command
   ```
3. **Open it**
   - Scheduler: <http://127.0.0.1:8766/scheduler.html>
   - ELN login: <http://127.0.0.1:8766/eln/login> — **the first account you register
     becomes the admin.** The database is created automatically.

That's the whole app, running on your machine. Stop with Ctrl-C.

### B. Deploy it as a real server

This is how the original author runs it: one small cloud VM serving the whole lab over
HTTPS, with backups. Below is the **overview**; the exact, copy-paste commands for
every step (and sanitized `systemd` / Caddy / backup configs) are in
**[SELF_HOSTING.md](SELF_HOSTING.md)**.

> Not a developer? You can hand `SELF_HOSTING.md` to an AI coding assistant and say
> *"set this up on a fresh Ubuntu VM, asking me for each placeholder."*

1. **Create a cloud VM** — a small Ubuntu 22.04 box (e.g. GCP `e2-small`, ~2 GB RAM)
   with a static IP and ports 80/443 open. Any provider works (GCP/AWS/DigitalOcean/Hetzner).
2. **Get a web address** — a free `*.duckdns.org` subdomain pointed at the VM's IP
   (swap for an institutional domain later by changing one line).
3. **Upload the code** to the VM (`scp`/`rsync` a tarball).
4. **Install & run as a service** — build a virtualenv and register a `systemd` unit so
   the app runs as exactly one always-on process bound to `127.0.0.1:8766`.
5. **Add HTTPS** — install Caddy as a reverse proxy; it fetches & renews the TLS
   certificate automatically for your domain.
6. **First login** — register the admin account, then add lab members.
7. **Turn on backups** — continuous replication (warm) + a nightly retention-locked
   snapshot (cold), so the data survives both corruption and a malicious wipe.

Then layer on the optional integrations (Slack, Gemini, cage-card intake, email) from
the [Configuration](#configuration) section.

---

## Configuration

Copy the example env file and fill in any optional integrations:

```bash
cp .env.example .env
```

- **Animal DB sync** — point the app at a spreadsheet via
  `POST /api/animals/config {"excel_path": "/path/to/colony.xlsx"}`, then sync with the
  sidebar "Sync now" button or `POST /api/animals/sync`. Column mapping is in
  [ARCHITECTURE.md](ARCHITECTURE.md#animal-db-sync).
- **Gemini AI scheduling** (optional) — set `GEMINI_API_KEY` (a free Google AI Studio
  key) to enable the scheduler's natural-language "AI" button. It only fills in
  parameters you preview; a deterministic engine creates the actual events.
- **Cage-card intake** (optional) — photograph a cage card; OCR + an LLM (or a built-in
  rule-based parser) read it and register the mouse into the animal DB — via the Slack
  bot or `animal_updater.register_from_image(...)`.
- **Slack bot** (optional) — set `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, and optionally
  `OPENAI_API_KEY`, then run `python slack_bot.py` (or `./start_bot.command`).
- **Email** (optional) — SMTP credentials enable password-reset emails; without them,
  reset codes are returned in the API response.

Full setup commands for all of the above are in [SELF_HOSTING.md](SELF_HOSTING.md), and
the env-var reference is [there too](SELF_HOSTING.md#8-environment-variables-reference).

---

## The vision — where this is going

Lab OS started as a one-researcher tool, but it's built to grow into a general-purpose
**operating system for a research lab** — the shared place a whole lab runs on, usable by
a PI, lab manager, postdocs, students, technicians, and collaborators.

The guiding principles:

- **One server, one source of truth.** Everything (schedule, colony, notes, audit) lives
  in one place every lab computer reaches via a browser — no per-machine installs, no
  file-sync drift.
- **Local-first, but multi-user ready.** It runs on a laptop today, yet the data models
  assume roles, permissions, and audit trails so the same codebase can serve a full lab.
  (The ELN already has an admin/member model and an append-only audit log.)
- **Keep lab *operations* and research *strategy* separate.** Shared operational data
  (colony, schedule, protocols) is conceptually distinct from a PI's private planning.
- **Capture, don't retype.** Photograph a cage card and it's in the database; describe a
  week of experiments in plain English and it becomes a schedule.

The longer-term modules this is a seed for: animal **colony management**, richer
**experiment logs** (behavior, ephys, histology, surgery), **inventory** (viruses,
antibodies, reagents, plasmids), **protocols & ordering**, **equipment**, and full
**roles/permissions + audit** across all of it.

## Extending Lab OS

The codebase is deliberately simple to extend — no build step, thin API handlers, and
self-contained pages. Common extension points (see [ARCHITECTURE.md](ARCHITECTURE.md)
for the data model and API map):

- **Add a field to events** — add the column in `init_db()`/`migrate_db()`, the
  serializer, the upsert, then the modal + `evToWire`/`evFromWire` in the scheduler HTML.
- **Add an ELN experiment type** — drop a self-contained HTML page in `modules/eln/`,
  add a `FileResponse` route in `server.py`, and reuse the existing notes API
  (`/api/eln/notes`, draft → submit → approve).
- **Add a new module/page** — create a folder under `modules/`, add its route, and link
  it from the shared nav (`modules/core/labos_nav.js`).
- **Add an API endpoint** — follow the pattern: thin route → helper → `db_conn()` context
  manager; for ELN routes gate with `require_user` / `require_admin` and write an audit row.
- **Plug in a different animal "master"** — anything that yields the documented columns
  (Excel, a Sheet, a CSV, Airtable) can feed the same validating, non-destructive sync.
- **Swap/extend the AI** — the auto-schedule parser is isolated in
  `modules/autoschedule/` (LLM → parameters → deterministic engine), so the model or the
  schema can be changed without touching the rest of the app.

New modules should preserve the long-term direction above: prefer data models that can
support multi-user roles, keep operations data separate from private strategy, and don't
hard-code single-user assumptions.

---

## Documentation

- **[USAGE.md](USAGE.md)** — how to *use* the app day to day: admin vs member accounts,
  signing in & recovering a lost ID/password, AI scheduling, the ELN review workflow,
  animal DB & cage-card intake.
- **[SELF_HOSTING.md](SELF_HOSTING.md)** — full step-by-step production deploy (VM,
  domain, HTTPS, service, integrations, backups), AI-assistant friendly.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — data model, API reference, frontend
  structure, animal-sync details, and the ELN auth model.

## License

[MIT](LICENSE).
