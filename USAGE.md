# Lab OS — User Guide

How to *use* Lab OS day to day, once it's running. (Setting it up is covered in
[README.md](README.md) and [SELF_HOSTING.md](SELF_HOSTING.md).)

**Contents**
- [Accounts & roles (admin vs member)](#accounts--roles)
- [Signing in, and recovering a lost ID or password](#signing-in--account-recovery)
- [Scheduler — including AI scheduling](#scheduler)
- [Electronic Lab Notebook (ELN)](#electronic-lab-notebook-eln)
- [Animal database & cage-card intake](#animal-database)
- [Dashboard](#dashboard)
- [On your phone (mobile & installing as an app)](#on-your-phone)
- [Slack bot](#slack-bot)

---

## Accounts & roles

Lab OS accounts live in the ELN (`/eln/`). There are two persistent roles:

| Role | Who | Can do |
|------|-----|--------|
| **admin** | PI / lab manager | Everything: see all projects, manage members & their roles, rename/archive projects, approve/return notes, view the audit log, configure the animal DB. |
| **member** | postdoc / student / tech | See only the projects they're assigned to (plus their own "pilot" projects); write and submit their own notes. |

**The first account ever registered automatically becomes the admin.** That person
then adds everyone else (or others self-register and the admin sets their role).

### Admin "mode" — a safety switch

An admin account doesn't act as admin all the time. On each browser session an admin
picks a **mode**:

- **Admin mode** → full privileges.
- **User (member) mode** → the admin sees exactly what a regular member sees.

This is the "be an admin, but browse as an intern" feature: it prevents accidental
privileged clicks when you just want to write a note. The effective permission is
*admin only if your account is admin **and** you chose admin mode this session.* You
can switch anytime with the **"Switch mode"** button in the header. (Members only ever
have member mode.)

### Managing members (admin)

From the ELN, open **Members** (`/eln/members`) to add people, set each person's role
(admin/member), edit their display name/email/color, or deactivate an account. To
control who sees a given project, open that project's **👥 Manage members**.

### Usernames

A username is auto-generated when you register: **first-name initial + first 6 letters
of the last name** (e.g. *Dana Johnston* → `djohnst`). Collisions get a number
(`djohnst2`). You'll see the exact username previewed live on the New User form.

---

## Signing in & account recovery

Go to **`/eln/login`**. It has four tabs: **Sign in · New User · Forgot ID · Forgot
password.**

### Forgot your ID (username)

1. Open the **Forgot ID** tab and enter the **email** you registered with.
2. The app shows the username(s) linked to that email.

(For a small lab this is shown on screen directly — no email needed.)

### Forgot your password

1. Open the **Forgot password** tab, enter your **username + email**.
2. You get a **6-digit reset code**, valid for **30 minutes**.
3. Enter the code + a new password to finish. (All your existing sessions are signed
   out for safety.)

**Where does the code come from?** Two modes, automatic:

- **Email mode** — if the server has SMTP configured, the code is **emailed** to your
  registered address.
- **Lab-local mode** — if SMTP is *not* configured, the code is **shown on screen**
  right there. This keeps a small lab self-sufficient with no email setup.

> For privacy, the page never reveals whether a username/email actually exists — you
> always see a success message; a code only appears/sends if they truly match.

### Wiring up email delivery (admin, optional)

To switch from "code on screen" to "code emailed", give the server SMTP credentials.
On the server, add them to `/opt/labos/secrets/smtp.env` and restart
(`sudo systemctl restart labos`):

```
LABOS_SMTP_HOST=smtp.gmail.com
LABOS_SMTP_PORT=587
LABOS_SMTP_USER=you@example.com
LABOS_SMTP_PASS=your-16-char-app-password   # Gmail App Password, not your login password
LABOS_SMTP_FROM=you@example.com
LABOS_SMTP_FROM_NAME=Lab OS
```

Once those are present, password-reset codes are emailed automatically (and the app
quietly falls back to on-screen if a send ever fails, so no one gets locked out).

### Admin password reset from the command line

If someone is fully locked out, an admin on the server can reset a password directly:

```bash
.venv/bin/python tools/reset_eln_password.py --username djohnst --password 'NewPass123!'
# or generate a random one:
.venv/bin/python tools/reset_eln_password.py --username djohnst --random
```

---

## Scheduler

Open **`/scheduler.html`**. Week / Day / Month views with an hour grid.

**Basics**
- **Create:** click an empty time slot, or the **+ Add** button. Pick a category
  (Surgery, Behavior, Experiments, Histology, Manuscript, Deadlines, or your own),
  optional sub-categories, project, assignee, mice, status, and notes.
- **Move / resize:** drag an event to move it; drag its edge to resize (15-min snap).
- **Recurring:** set a repeat (daily/weekly/biweekly/monthly) with an optional end date.
  Repeats show a ⟳ marker; editing one opens the master event.
- **Filter & search:** sidebar checkboxes filter by category, project, and team member;
  the search box filters by title/project/category.
- **Color coding:** event fill = category color; left border = assignee color.
- **Export:** iCalendar (`.ics`), CSV, or PDF (print) of whatever's currently visible.

### AI scheduling (the ✨ AI button)

Turn a plain-English description into calendar entries:

1. Click **✨ AI** in the toolbar.
2. Type what you want, e.g.
   *"Schedule cohort A OKR testing every weekday next week, 2pm, assign to Dana."*
3. Click **Generate preview** — the AI proposes events and shows them as a **preview**.
4. Review, then **confirm** to add them. **Nothing is saved until you approve.**

Under the hood the AI (Gemini) only fills in *parameters*; a deterministic engine
builds the actual events, so it can't silently corrupt your calendar. This requires the
server to have a `GEMINI_API_KEY` set (a free Google AI Studio key — see SELF_HOSTING.md).
If no key is configured, the button will tell you it's not enabled.

---

## Electronic Lab Notebook (ELN)

Open **`/eln/`**. You'll see a project picker (admins see all projects; members see
their assigned + pilot projects).

- **Projects:** create an **Assigned** project (admin) or a personal **Pilot** project
  (anyone). Cards show member dots, note count, and last activity.
- **Notes:** inside a project, pick an experiment type (e.g. **Slice recording**) and a
  date. You write one note per date; mouse info auto-fills from the animal DB when you
  type a Mouse ID.
- **Review workflow:** a note moves **draft → submitted → approved**. You can edit only
  while it's a draft; submitting locks it; an admin can **approve** it or **return** it
  to draft. This gives the PI a clean "review the intern's notes" loop.
- **Slice recording** specifically auto-builds the `.abf` filename from your inputs
  (condition, type, cohort, cell, protocol) and pre-fills strain/sex/DOB/age/condition
  from the animal DB.

---

## Animal database

Open **Animal DB** (`/eln/colony`). Search mice by ID, project, condition, strain,
cohort, or experiment; open a mouse for its full profile; manage breeders and cohorts.

- **Syncing:** the colony is loaded from your spreadsheet. An admin sets the source and
  clicks **Sync now**; the sync is validating and non-destructive (it diffs, never blindly
  overwrites, and snapshots first). Column mapping is in
  [ARCHITECTURE.md](ARCHITECTURE.md#animal-db-sync).
### Scan a cage card with your phone camera

Instead of typing a new mouse in by hand, photograph the cage card and let the app read
it. This is the fastest way to add animals, and it's built for phones:

1. On a phone, open **Animal DB → 📷 Scan card**, or tap **📷 Scan cage card** in the
   mobile menu (the page is `/eln/cage-card`).
2. Tap **Take / choose photo** — your phone's **camera opens directly** (you can also pick
   an existing image).
3. The photo is read automatically by **GPT-4o-mini vision**, and the extracted fields
   (mouse ID, strain, sex, DOB, cohort, condition, …) appear in an **editable form**.
   Anything the AI couldn't read is flagged so you can fill it in.
4. Check/fix the fields and tap **Save to colony** — the mouse is written into the animal
   database. Then **Scan another** to continue.

> Requires `OPENAI_API_KEY` on the server (the vision step). Without it, the scan reports
> that the AI isn't available — see [SELF_HOSTING.md](SELF_HOSTING.md).

You can also register a cage card **from the Slack bot** (send it the photo), or from a
terminal:
```python
from animal_updater import register_from_image
register_from_image("/path/to/cage_card.jpg", project="my-project")
```

---

## Dashboard

Open **Dashboard** (`/eln/dashboard`) for lab activity at a glance: notes by user and by
project, status rollups, and (admins) a link to the append-only **audit log**, which
records every meaningful action (logins, note edits, approvals, member changes, etc.).

---

## On your phone

Lab OS adapts to phones automatically — **just open the same URL on your phone**, no
separate app or address needed.

- Opening the **scheduler** on a phone (screen ≤ 640px wide) auto-redirects to a
  touch-optimized mobile scheduler at **`/m`** — an iCal-style **Agenda / Day / Week**
  view built for one-handed use.
- Opening the **ELN** (`/eln/`) on a phone auto-redirects to a mobile home at
  **`/eln/m`** — a card list of your projects with a menu to Schedule, Lab Notes,
  Animal DB, Breeders, and Dashboard.
- On a wide screen those mobile pages send you back to the full desktop UI, so you
  always get the right layout for the device (no redirect loops).
- **Scan cage cards with the camera:** from **Animal DB → 📷 Scan card** (or the mobile
  menu's 📷 Scan cage card), take a photo and the mouse is read and saved — see
  [Animal database](#animal-database) above.

### Install it like an app (PWA)

Lab OS is a **Progressive Web App**, so you can add it to your home screen and launch it
full-screen like a native app (it keeps you logged in):

- **iPhone / iPad (Safari):** Share → **Add to Home Screen**.
- **Android (Chrome):** menu **⋮** → **Install app** (or *Add to Home screen*).

It installs as **"Lab OS"**, opens standalone (no browser bars), in portrait. Because
it's served over HTTPS with a service worker, the shell loads fast and the icon behaves
like any other app on the phone.

## Slack bot

If the lab runs the Slack bot, you can do common things from Slack (DM the bot or use a
channel it's in): ask about today's schedule, add/update animals, and **register a mouse
by sending a cage-card photo**. The bot understands natural language and replies in
Korean. Setup is in [SELF_HOSTING.md](SELF_HOSTING.md#slack-bot).
