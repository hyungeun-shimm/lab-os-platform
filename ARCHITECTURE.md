# Lab OS — Architecture

Lab OS is a single FastAPI (Python) + SQLite application that serves several
self-contained vanilla-JS frontends. There is no build step and no frontend
framework. This document describes the data model, API, frontend structure, the
animal-DB sync, and the ELN auth model.

## Project purpose

A lab-operations platform: surgery/experiment scheduling, an animal colony database,
an electronic lab notebook, and a team activity dashboard, all backed by one SQLite
database. It is built so a single-user tool can grow toward a multi-user lab platform
(roles, permissions, audit trails) without a rewrite.

## Repository layout

```
.
├── server.py                 # FastAPI backend: all API routes, DB schema + migrations
├── requirements.txt
├── launch_bg.py              # Spawn the server detached
├── start_scheduler.command   # macOS double-click launcher
├── slack_bot.py              # Optional Slack assistant bot
├── cage_card_parser.py       # Cage-card text → structured animal record
├── ocr_engine.py             # OCR helper for cage-card photos
├── animal_updater.py         # Animal-record helpers used by the bot
├── modules/
│   ├── core/                 # auth, users, animal sync, DB schema, shared nav
│   ├── eln/                  # ELN: API router + per-page HTML
│   ├── scheduler/            # scheduler.html (+ mobile.html)
│   ├── colony/               # colony.html
│   ├── autoschedule/         # auto-scheduling engine + tests
│   └── notify/               # email digest
├── static/pwa/               # PWA manifest, service worker, icons
└── tests/                    # pytest suite
```

## How to run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py --port 8766          # default port 8766 (override with SCHEDULER_PORT)
```

Open `http://127.0.0.1:8766/scheduler.html`. The ELN is at `/eln/`.

## Database schema (`scheduler.db`)

All tables are created by `init_db()` / `init_eln_tables()` in code; new columns are
added non-destructively by `migrate_db()` using the `ALTER TABLE ADD COLUMN` pattern.

Core scheduler tables:

- **`events`** — `id`, `title`, `category`, `subcategory`, `subcategory2`, `cohort`,
  `project_slug`, `status` (`planned`/`in_progress`/`done`/`delayed`/`skipped`),
  `start_at`, `end_at`, `all_day`, `notes`, `mice` (JSON array), `recur` (JSON or null),
  `assignee`, `delay_reason`, `reschedule_to`, `created_at`, `updated_at`.
- **`team_members`** — `id`, `name`, `role`, `color`, `created_at`.
- **`custom_projects`** — `slug`, `name`, `color`, `created_at`.
- **`project_colors`** — per-slug color override.
- **`custom_categories`** / **`custom_subcategories`** — user-defined category tree.
- **`animals`** — colony cache (see Animal DB sync below).

ELN tables (all prefixed `eln_`): `eln_users`, `eln_sessions`, `eln_password_resets`,
`eln_projects`, `eln_project_members`, `eln_notes`, `eln_audit`,
`eln_naming_conventions`.

## API reference

All endpoints share one origin; the frontend uses an empty API prefix.

### Events
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/events` | List events sorted by start |
| POST | `/api/events` | Create |
| PUT | `/api/events/{id}` | Full update (upsert) |
| DELETE | `/api/events/{id}` | Delete |
| POST | `/api/events/bulk-import` | Bulk insert |

Event wire format uses ISO datetime strings for `start`/`end`; `recur` is `null` or
`{"freq": "daily"|"weekly"|"biweekly"|"monthly", "until": "YYYY-MM-DD"|null}`.

### Projects / Team / Customs
| Method | Path | Description |
|--------|------|-------------|
| GET/POST/DELETE | `/api/projects[...]` | List + custom project CRUD + color |
| GET/POST/PUT/DELETE | `/api/team[...]` | Team-member CRUD |
| GET/POST | `/api/customs[...]` | Custom categories / subcategories |

### Animals
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/animals` | List animals |
| GET | `/api/animals/sync-status` | `{excel_path, last_synced, count}` |
| POST | `/api/animals/config` | Set the Excel path |
| POST | `/api/animals/sync` | Trigger a sync |

### ELN
Routes live under `/api/eln/*` (see `modules/eln/api.py`): auth
(`/auth/register|login|logout|me|set-mode|forgot-*|reset-password`), users (admin),
projects (CRUD + members), mice read-through (`/mice/{id}`), notes (create / get /
patch / submit / approve / return), and dashboard/calendar list endpoints.

## Frontend architecture

Each page is one HTML file with inline `<style>` + `<script>` (no build, no external JS
libraries). State is held in a plain `state` object, mutated, then `render()` is called —
no reactive framework. `localStorage` holds only UI preferences (theme, compact mode,
sidebar collapse); all real data lives in SQLite via the API.

Recurring events are **not** stored as multiple rows — `expandEventsForDisplay()`
generates virtual occurrences for the visible window at render time. Wire conversion
(`evToWire` / `evFromWire`) maps between JS `Date` objects and ISO strings.

A shared top nav is injected by `modules/core/labos_nav.js` and a running-version badge
is read from `GET /api/version`.

## Animal DB sync

Animals are stored in the `animals` table as a cache of an Excel workbook. The app never
copies the file — it reads it at sync time. Configure the path with
`POST /api/animals/config {"excel_path": "..."}` (sheet `Sheet1`, header row 0).

Column mapping (Excel → DB): `Mice ID`→`mouse_id`, `Project`→`project`,
`Condition`→`condition`, `Exp`→`experiment`, `Cohort`→`cohort`, `Strain`→`strain`,
`DOB`→`dob`, `Sex`→`sex`, `DOS`→`dos`, `Date in cond-1/2/3`→`date_cond1/2/3`,
`Days in cond-1/2/3`→`days_cond1/2/3`, `Sacrified (Y/N)`→`sacrificed`,
`Sacrified date`→`sacrificed_date`, `Note`→`notes`.

The sync is **validating, non-destructive, and audited** (no destructive replace): rows
are diffed and changes recorded. Auto-sync runs on startup if a path is configured and the
file exists. Dates are stored internally as `YYYY-MM-DD`.

## ELN auth model — `effective_role`

- `user.role` is the persistent permission (`admin` / `member`).
- `session.mode` is what the user picked this browser session (`admin` / `member` / null).
- `effective_role = "admin"` only if `user.role == "admin"` **and** `session.mode == "admin"`.

This lets a lab head browse as a member by default and explicitly opt into admin actions.
Admin-gated endpoints depend on `require_admin`, which checks `effective_role`, not
`user.role`. Passwords are hashed with PBKDF2-HMAC-SHA256; sessions use a sliding 7-day
TTL cookie (`labos_session`). Every state-changing action writes an `eln_audit` row.

## Conventions

- No build step; vanilla JS only; comment the *why*, not the *what*.
- DB migrations go in `migrate_db()` (`ALTER TABLE ADD COLUMN`).
- New API routes: thin handler → helper → `db_conn()` context manager.
- ELN code sets `con.row_factory = sqlite3.Row`, lazy-imports the DB connection to avoid
  circular imports, and writes an audit row for every state change.
- Python 3.9 compatible: use `Optional[...]` in route signatures, not `X | None`.
