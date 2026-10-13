# Changelog

All notable changes to Lab OS are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versioning is
[SemVer](https://semver.org/): `MAJOR.MINOR.PATCH`.

- **MAJOR** — breaking change to the DB schema or API that needs a migration.
- **MINOR** — new feature, backward compatible.
- **PATCH** — bug fix or small tweak, no new feature.

Bump with `scripts/bump_version.sh [major|minor|patch]`, then ship with
`deploy/deploy.sh`. The running server reports its version at `GET /api/version`
and in the top nav bar, so you can always see what is live.

<!-- new-release-here -->

## [1.12.0] - 2026-10-13

### Added
- Today dashboard with lab-time schedules, overdue work, drafts, and a separate review queue; refreshes when returning to the page.
- Persistent schedule-to-ELN links, explicit project/type/date selection, mouse auto-fill, and direct links to the exact note.
- Animal synchronization history with field changes, rejected rows, duplicate conflicts, and recovery guidance.
- Regression checks for mobile metadata preservation, note access and lifecycle, daily queues, animal source failures, search/filter behavior, and daily scheduling capacity.
- Release verification gate and verified pre-deployment code/database backups.

### Changed
- Unified navigation with separate Animal DB and source Sheet destinations; duplicate identity controls and retired machine-sync menus are removed from normal flows.
- Submitted notes stay distinct from self-finalized and reviewer-approved notes; existing self-finalization remains available alongside Submit for review.
- Animal edits save to the source Sheet before updating the cache; Sheet formulas and explicit field clearing survive writes.
- Sheet-master animal deletion/archive uses the source workflow; cache-only removals that a subsequent sync would reverse are blocked. Returning from source edits refreshes the visible animal cache and reports sync failures.
- Mobile editing exposes project, cohort, assignee, status, and multi-day/all-day fields and retains other existing event metadata.
- Public publishing includes verification scripts and release notes while removing current and legacy source Sheet identifiers.

### Fixed
- Scheduler search now filters across mouse IDs, cohorts, project, assignee, rig, title, and notes; category/cohort filters are visible and status filtering is available.
- Editing on mobile no longer clears omitted tracking fields or resets completed events to planned.
- Existing/custom event titles and subtypes survive editing; Duplicate preserves mice and resource fields.
- Tall scheduler dialogs scroll while keeping actions reachable.
- Multiple notes from one author on the same day can be selected by note ID, and changes retain the selected experiment date and ancillary payload fields.
- AI scheduling never exceeds the specified per-day limit; excess animals remain in the preview's unscheduled warning.
- Dashboard no longer loads an unscoped, potentially stale browser cache from a previous account/mode.

## [1.11.0] - 2026-06-23

### Added
- Analysis hub (`/eln/analysis`): central page for offline ephys analysis — LTD tab lists all finalized Slice notes, cross-references analysis jobs, shows status/results; Excitability placeholder; VOR/OKR MATLAB script link manager.
- Ephys analysis pipeline: ABF upload → GCP server-side LTD analysis → Excel output → email report. Background worker thread (`analysis_worker.py`), job queue (`eln_analysis_jobs` table), auto test-pulse detection, cap-drift auto-exclusion.
- Experiment Log (`/eln/log`): aggregated view of all Slice + Behavior notes across projects; filter by project/type/experimenter/date; analysis results column.
- Global nav now includes Exp Log and Analysis tabs on all pages.
- Admin-mode switch dropdown in top-right user area (click name → Admin/User mode toggle, reloads page).

### Changed
- Behavior: Copy-from-previous-mouse button on VOR/OKR blocks; multi-note-per-day support; admin delete note.
- Slice: recording-params row shows immediately on cell set (not only after first protocol added).
- Scheduler: global keyboard shortcuts (t/n/arrows) now skip when focus is in an input field.

## [1.10.0] - 2026-06-20

### Added
- **Safe animal sync** (`modules/core/animal_sync.py`, Workstream B v1): the
  Sheet/Excel → DB sync is now validating, non-destructive and audited —
  normalize → validate → snapshot → diff → apply → audit → report.
- `animal_audit` (append-only, field-level change log) and `animal_snapshots`
  (full pre-sync JSON of `animals` for point-in-time rollback) tables; new
  `animals.removed` / `removed_at` columns. All additive + idempotent.
- `POST /api/animals/sync` now returns a structured report
  (`{added, modified, removed, merged, rejected, unchanged, …}`), surfaced in
  the scheduler and colony "Sync now" UIs.

### Changed
- Sync no longer does `DELETE FROM animals` → bulk re-INSERT. Rows are upserted
  by `mouse_id`; invalid rows are **rejected** (never written); a `mouse_id`
  that vanishes from the source is **soft-removed** (flagged + audited),
  never hard-deleted; app-only fields (e.g. `assigned_to`) survive a re-sync.
- Duplicate `mouse_id`s are handled by content: identical duplicates are merged
  to one (with a warning), conflicting duplicates are rejected; pre-existing
  identical duplicate rows in the DB are collapsed on sync.
- Reads exclude soft-removed animals by default (`?include_removed=` to opt in).

### Fixed
- A single bad cell or an accidental row deletion in the source can no longer
  corrupt or vaporise the canonical animal table.


## [1.9.0] - 2026-06-19

### Added
- **Event location + rig (rig number/name), system-wide.** Events now carry a `location` and a `rig`. An admin curates the dropdown lists (new `scheduler_resources` table; `GET /api/schedule/resources`, admin-only `POST`/`DELETE`), and everyone can still type a free **Custom…** value. Surfaced everywhere: desktop event modal (two dropdowns + an admin-only "Manage locations & rigs" modal), mobile event sheet (two dropdowns), mobile agenda cards (📍 location · rig), and the AI autoscheduler (passes `location`/`rig` through to generated events). Foundation for the upcoming resource-based "events on my rig/room" visibility filter.


## [1.8.0] - 2026-06-19

### Added
- **Mobile ELN home at `/eln/m`.** A simplified mobile project picker in the same style as `/m`: header avatar menu (identity + cross-app nav + sign out), project cards (title / kind / note count / member dots → tap opens `/eln/project/{slug}`), an All/Assigned/Pilot filter, search, and a `+` create sheet (pilot for everyone; admins can pick assigned). Self-gates via `/api/eln/auth/me`, reuses `/api/eln/projects`. `notebook.html` redirects ≤640px → `/eln/m`, and `/eln/m` redirects ≥900px → `/eln/` (the 640/900 gap avoids a loop). Deep ELN pages (project, slice notebook) stay desktop for now — next iteration.


## [1.7.1] - 2026-06-19

### Added
- **`/m` menu — identity + cross-app navigation.** The mobile page had no way to reach the other apps or see who's signed in (it omits the shared `labos_nav`). Added a header avatar that opens a bottom-sheet menu showing the user (avatar + name + Admin/User) with links to Schedule / Lab Notes (ELN) / Animal DB / Breeders / Dashboard, plus Sign out.


## [1.7.0] - 2026-06-19

### Added
- **Dedicated mobile scheduler at `/m`.** A purpose-built mobile page (`modules/scheduler/mobile.html`) replaces the responsive-shrink approach, which never felt native on phones. iCal-style **Agenda** (default) / **Day** (week strip + hour timeline) / **Week** (7-column grid) views; a clean bottom-sheet **AI** flow built fresh (the old responsive AI modal didn't open on phones); tap-to-edit + a `+` create sheet; a "Mine" filter. Self-gates via `/api/eln/auth/me` and reuses the same backend (`/api/events`, `/api/schedule/ai|commit`). `scheduler.html` redirects ≤640px → `/m`, and `/m` redirects ≥900px → desktop (the 640/900 gap prevents a redirect loop). The mobile page deliberately omits the shared `labos_nav` strip, whose z-index overlap is the likely reason the old mobile AI button did nothing.


## [1.6.3] - 2026-06-19

### Fixed
- **Modals were unusable on phones (AI button "did nothing").** The AI, mouse-picker, and export modals each set an inline `min-width` (520/560/480px) that overrode the mobile `.modal` rule, so on a phone they rendered wider than the screen and their action buttons (e.g. AI "Generate") fell off the right edge — the ✨ AI button worked on desktop but appeared dead on mobile. The mobile `.modal` rule now uses `!important` for min/max/width, so all modals go full-screen on phones.


## [1.6.2] - 2026-06-19

### Fixed
- **Mobile scheduler header is now one compact row.** Phones were still showing a ~4-row header (page title + full date label + wrapped controls + colony/V-O links). On ≤640px the redundant "Lab Scheduler" title, date label, search, filters-drawer toggle, exports, and colony/V-O links are hidden (the links needed `!important` to beat their inline `display`), and Mine + AI become icon-only — leaving `‹ Today ›` + `Agenda | Day` + Mine + AI in roughly one row. (Category/project/team filtering is now desktop-only on phones; the per-user "Mine" filter still works.)


## [1.6.1] - 2026-06-19

### Added
- **Auto-push data folders on Finalize.** Finalizing a slice note locks it and
  auto-copies its data folder(s) to your ≤3 push destinations (File System Access
  API, desktop Chrome/Edge) with a progress modal + per-destination results;
  recorded append-only in the audit log (`push_data`) for provenance. A note's
  **own author** can now finalize it (`POST /api/eln/notes/{id}/approve` allows
  owner or admin; admin-only unlock unchanged); new `POST
  /api/eln/notes/{id}/push-record`. Best-effort — never un-finalizes; skipped if
  no destinations are set or the browser is unsupported.

### Fixed
- **Mobile scheduler scroll + visibility.** On phones (≤640px) the page now uses native document scroll instead of a trapped `100vh` + nested-overflow layout (Safari's dynamic toolbar made the old internal scroll janky); the sidebar drawer is `position: fixed`; and the unreadable Week/Month grids are hidden — phones always get the Agenda list (or Day), enforced in `render()` so they can't be reached via a stale view state.


## [1.6.0] - 2026-06-19

### Added
- **Mobile-first scheduler.** A new **Agenda** view (day-grouped event list) that auto-becomes the default on phones (≤640px), plus a responsive touch layout: off-canvas sidebar drawer, full-screen edit modal with stacked fields and 16px inputs (no iOS zoom), a `+` floating button, and larger tap targets. One codebase — reuses the same data, filters, AI scheduling and edit modal, so future features track on mobile automatically.
- **Installable PWA.** Web app manifest + calendar icons + a minimal (no-cache) service worker, so the scheduler can be added to a phone home screen and run full-screen (standalone). Assets at `/pwa/*`, `/manifest.webmanifest`, `/sw.js`.

### Security
- **Closed an unauthenticated repo-root exposure.** Removed an `app.mount("/static", StaticFiles(directory=ROOT))` (present since v1.0.0) that served the entire app directory — `scheduler.db` (incl. ELN password hashes + session tokens), source, and `/opt/labos/secrets/*` — with NO auth, bypassing the v1.4.0 `/api` gate. Nothing referenced it. **Rotate any exposed secrets** (service token, Gemini / SMTP / service-account keys) and treat the DB as having been readable.


## [1.5.3] - 2026-06-19

### Added
- Per-user event ownership: events record an `owner_id` (the ELN user who created them), set from the session on create and preserved on update. Token / Slack-bot callers leave it null (non-breaking). Foundation for per-user views as the lab grows.
- Scheduler **"👤 Mine"** topbar toggle — filters the calendar to events you created (`owner_id`) or are assigned to (`assignee` linked to your ELN user).
- **"Created by &lt;name&gt;"** line in the event editor for events that have an owner.

### Changed
- `POST /api/events` and `/api/schedule/commit` now stamp the signed-in ELN user as the event owner (via `auth_helpers.current_user`).


## [1.5.2] - 2026-06-19

### Added
- Admin Members page: a per-user **"Daily schedule email"** toggle in the edit
  modal (sets `eln_users.email_digest` via `PATCH /api/eln/users/{id}`), so an
  admin can turn each member's digest on/off by clicking. A self-serve per-user
  preference UI is a later phase.

## [1.5.1] - 2026-06-19

### Added
- Scheduler page redirects anonymous users to `/eln/login` up front (front-end
  login gate that pairs with the v1.4.0 API auth) instead of flashing a
  "can't reach the server" error before the nav redirect.

## [1.5.0] - 2026-06-19

### Added
- **Per-user daily schedule-digest emails (Phase 1).** Each morning (07:00 lab
  time, APScheduler in the app) every ELN user is emailed the events assigned to
  them that day — mapped `events.assignee → team_members.eln_user_id → eln_users`,
  delivered via the existing SMTP mailer. Empty days are skipped; opt-out via
  `eln_users.email_digest = 0`. `modules/notify/digest.py` (`build_digests` is
  pure/testable; `send_digests` supports `dry_run` + `only_email` for safe tests).
  Next phases: Slack-DM delivery + a self-serve notification-preferences UI.

## [1.4.0] - 2026-06-19

### Security
- **Scheduler API is no longer open.** A middleware now gates every `/api/*` route
  except the ELN router (which self-gates) and `/api/version`: callers must present
  an ELN **session cookie** (browsers, same-origin) or the shared **service token**
  (`X-Labos-Token`, for machine callers). Previously anyone with the URL could read
  the animal colony and read/write/delete events. Token in
  `/opt/labos/secrets/api.env` (`LABOS_API_TOKEN`) via the `labos.service`
  EnvironmentFile; the scheduler frontend sends the cookie (`credentials:'include'`),
  and `slack_bot.py` sends the token header (when repointed to the cloud).

## [1.3.1] - 2026-06-19

### Changed
- Translated remaining Korean UI strings to English in the ELN notebook + slice
  pages (Change-password modal, data-folder/push UI, vision-extract alerts, etc.),
  completing the English-only-UI policy. Frontend strings only — no logic change.

## [1.3.0] - 2026-06-19

### Added
- **AI autoschedule (BYOK Gemini).** Natural-language scheduling: `POST
  /api/schedule/ai` turns free text into structured params via Gemini (key only,
  no new dependency — stdlib `urllib`), then a deterministic engine
  (`modules/autoschedule/`) resolves the cohort to real mouse IDs, distributes
  N/day across a date range (optional control-group mixing, weekday-only),
  flags conflicts, and previews events. `POST /api/schedule/commit` saves them.
  Gemini only parses text — every mouse ID and DB write goes through verifiable
  logic, so no hallucinated animals. Key in `/opt/labos/secrets/autoschedule.env`
  (`GEMINI_API_KEY`, optional `GEMINI_MODEL`) via the `labos.service` EnvironmentFile.

## [1.2.0] - 2026-06-19

### Added
- **Password-reset emails via SMTP.** `forgot-password` now emails the reset code
  through Gmail (`modules/core/mailer.py`, configured by `/opt/labos/secrets/smtp.env`
  via the `labos.service` EnvironmentFile). If SMTP is off or a send fails it falls
  back to showing the code in the response, so recovery never hard-fails.
- `tools/reset_eln_password.py` — break-glass CLI to reset a user's password
  directly in the DB (for total lockout / SMTP down). Run on the VM as `labos`.

## [1.1.0] - 2026-06-19

### Changed
- **Signup is now gated — no more open registration.** Self-registration creates
  a *pending* account that cannot sign in until an admin approves it (Members page
  → "Pending approval" section, Approve / Reject). The first user still bootstraps
  as the auto-approved admin; all existing accounts are grandfathered (approved).
  Adds an `approved` column to `eln_users`, `POST /api/eln/users/{id}/approve` and
  `/reject`, and a pending gate on login (shown only after a correct password, so
  account existence isn't leaked).

## [1.0.0] - 2026-06-19

Baseline. Everything already live on the cloud server is folded into v1.0.0:

### Added
- Scheduler (week/day/month, drag-resize, recurring events, iCal/CSV/PDF export).
- ELN module (login, projects, slice recording, draft→submit→approve, audit log).
- Animal DB synced from the Google Sheet; mouse-ID auto-fill.
- Cloud deployment: GCP VM `your-vm`, Caddy HTTPS, single `labos` systemd service.
- Version tracking (`VERSION`, `GET /api/version`, nav badge) and git history.
