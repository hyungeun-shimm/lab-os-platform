# Lab OS

A self-hosted lab-management web app for a research (e.g. neuroscience) lab — a
single FastAPI + SQLite server that bundles a scheduler, an animal/colony database,
an electronic lab notebook (ELN), an activity dashboard, and an optional Slack
assistant bot. No build step, no frameworks on the frontend — each page is a
self-contained HTML file of vanilla JS.

> This repository is the **public, sanitized mirror** of a private lab deployment.
> It ships generic placeholder configuration only — no real credentials, hosts, or
> lab data. Bring your own `.env` and data.

## Features

- **Scheduler** — week / day / month calendar for surgeries, experiments, manuscript
  deadlines, histology timelines, and team assignments. Drag-to-move/resize, recurring
  events, categories/subcategories, per-project and per-member color coding, iCal / CSV /
  PDF export, browser reminders, and an auto-schedule engine.
- **Animal DB** — a colony database synced from an Excel workbook (validating,
  non-destructive, audited sync). Mouse profiles, breeders, cohorts, conditions, and a
  searchable mouse picker shared with the scheduler and ELN.
- **ELN (Electronic Lab Notebook)** — multi-user, per-project, per-date notes with a
  Submit → Approve workflow, mouse-ID auto-fill from the animal DB, and experiment-type
  pages (slice recording, behavior, histology, overview).
- **Dashboard** — lab activity at a glance: notes by user and by project, status rollups,
  and an append-only audit log.
- **Slack bot** (optional) — natural-language scheduling plus cage-card OCR/parsing to add
  animals from a phone photo. (Bot replies are in Korean by design; the rest of the UI is
  English.)

## Tech stack

FastAPI · SQLite · vanilla-JS single-file frontends · slack-bolt · APScheduler ·
pandas / openpyxl. Python 3.9+.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py --port 8766
```

Then open <http://127.0.0.1:8766/scheduler.html>. On macOS you can instead
double-click `start_scheduler.command`.

First-run notes:
- The SQLite database (`scheduler.db`) is created automatically.
- The ELN lives at `/eln/` — the **first** account you register becomes the admin.

## Configuration

Copy the example env file and fill in any optional integrations:

```bash
cp .env.example .env
```

- **Slack bot** (optional): set `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, and optionally
  `OPENAI_API_KEY` (enables LLM cage-card parsing; falls back to a rule-based parser),
  then run `python slack_bot.py` (or `./start_bot.command`).
- **Animal DB sync**: point the app at a local Excel workbook via
  `POST /api/animals/config {"excel_path": "/path/to/colony.xlsx"}`, then trigger a sync
  with the sidebar "Sync now" button or `POST /api/animals/sync`. Column mapping is
  documented in [ARCHITECTURE.md](ARCHITECTURE.md#animal-db-sync).

## Documentation

See [ARCHITECTURE.md](ARCHITECTURE.md) for the data model, API reference, frontend
structure, and the ELN auth model.

## License

[MIT](LICENSE).
