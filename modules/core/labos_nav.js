/* Lab OS — unified top-strip nav.
 * Included on every page via:  <script src="/labos-nav.js" defer></script>
 * Pulls the current user from /api/eln/auth/me. If not authenticated and we are
 * not already on /eln/login, redirects to /eln/login?next=<current>.
 * Injects a slim app-switcher bar at the very top of <body>.
 */
(function () {
  // Skip on login / public pages to avoid redirect loops.
  const path = window.location.pathname;
  if (path === "/eln/login" || path.startsWith("/eln/login/")) return;

  const DEFAULT_ANIMAL_SHEET_URL = "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit";
  if (!window.LabOS) {
    const script = document.createElement("script");
    script.src = "/labos-ui.js";
    document.head.appendChild(script);
  }

  const APPS = [
    { id: "schedule", label: "Schedule",  icon: "📅", href: "/scheduler.html",
      match: (p) => p === "/scheduler.html" || p === "/" },
    { id: "eln",      label: "ELN",       icon: "📓", href: "/eln/",
      match: (p) => p === "/eln/" || p === "/eln" || p.startsWith("/eln/project/") },
    { id: "colony",   label: "Animal DB", icon: "🐭", href: "/eln/colony",
      match: (p) => p === "/eln/colony" || p.startsWith("/eln/mouse/") || p.startsWith("/eln/legacy/") },
    { id: "breeders", label: "Breeders",  icon: "🧬", href: "/eln/breeders",
      match: (p) => p === "/eln/breeders" },
    { id: "log",       label: "Exp Log",   icon: "📋", href: "/eln/log",
      match: (p) => p === "/eln/log" },
    { id: "analysis",  label: "Analysis",  icon: "⚗️", href: "/eln/analysis",
      match: (p) => p === "/eln/analysis" },
    { id: "dashboard", label: "Today", icon: "📊", href: "/eln/dashboard",
      match: (p) => p === "/eln/dashboard" },
    { id: "animal_sheet", label: "Animal Sheet ↗", icon: "", href: DEFAULT_ANIMAL_SHEET_URL, external: true,
      match: () => false },
  ];

  const css = `
    body.labos-unified header.app-top .brand,
    body.labos-unified header.app-top .brand-sub,
    body.labos-unified header.app-top .me,
    body.labos-unified header.app-top .signout,
    body.labos-unified header.app-top .switch-mode { display:none !important; }
    body.labos-unified header.app-top { padding:8px 20px; }
    body.labos-unified header.app-top.labos-redundant { display:none; }
    .labos-nav {
      position: sticky; top: 0; z-index: 9999;
      background: #131A40;
      color: #fff;
      font-family: -apple-system, "Helvetica Neue", Calibri, sans-serif;
      font-size: 13px;
      box-shadow: 0 1px 0 rgba(0,0,0,0.18);
    }
    .labos-nav-inner {
      display: flex; align-items: center; gap: 14px;
      padding: 6px 16px;
    }
    .labos-nav-brand {
      font-family: Georgia, serif; font-weight: 700; font-size: 15px;
      color: #F2B33D; letter-spacing: 0.02em;
    }
    .labos-nav-brand a { color: inherit; text-decoration: none; }
    .labos-nav-ver {
      color: #6E7BA8; font-size: 10px; font-weight: 600;
      letter-spacing: 0.02em; margin-left: -8px;
    }
    .labos-nav-ver:hover { color: #CADCFC; }
    .labos-nav-tabs { display: flex; gap: 2px; flex: 1; flex-wrap: wrap; }
    .labos-nav-tab {
      color: #CADCFC; text-decoration: none;
      padding: 5px 10px; border-radius: 6px;
      font-size: 12.5px; font-weight: 600;
      transition: background 0.15s, color 0.15s;
      display: inline-flex; align-items: center; gap: 5px;
    }
    .labos-nav-tab:hover { background: rgba(255,255,255,0.08); color: #fff; }
    .labos-nav-tab.active { background: #F2B33D; color: #131A40; }
    .labos-nav-tab.active:hover { background: #F2B33D; color: #131A40; }
    .labos-nav-user {
      display: flex; align-items: center; gap: 8px;
      color: #CADCFC; font-size: 12px;
    }
    .labos-nav-user .av {
      width: 24px; height: 24px; border-radius: 50%;
      background: #F2B33D; color: #131A40;
      display: inline-flex; align-items: center; justify-content: center;
      font-weight: 700; font-size: 10.5px;
    }
    .labos-nav-user .nm { color: #fff; font-weight: 600; }
    .labos-nav-user .mode {
      background: rgba(242,179,61,0.25); color: #F2B33D;
      padding: 1px 7px; border-radius: 999px;
      font-size: 10px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
    }
    .labos-nav-user .mode.member { background: rgba(143,179,230,0.30); color: #CADCFC; }
    .labos-nav-user a.logout {
      color: #8FB3E6; text-decoration: none; font-size: 11.5px;
      margin-left: 4px;
    }
    .labos-nav-user a.logout:hover { color: #fff; }
    .labos-nav-userinfo {
      display: flex; align-items: center; gap: 6px; cursor: pointer;
      padding: 3px 6px; border-radius: 6px;
      transition: background 0.15s;
    }
    .labos-nav-userinfo:hover { background: rgba(255,255,255,0.10); }
    .labos-nav-dropdown {
      position: absolute; top: calc(100% + 4px); right: 0;
      background: #1E2761; border: 1px solid rgba(255,255,255,0.15);
      border-radius: 10px; min-width: 160px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.35); padding: 6px;
      z-index: 10000;
    }
    .labos-nav-dropdown button {
      display: block; width: 100%; text-align: left;
      background: none; border: none; color: #CADCFC;
      padding: 8px 12px; border-radius: 7px;
      font: inherit; font-size: 13px; cursor: pointer;
    }
    .labos-nav-dropdown button:hover { background: rgba(255,255,255,0.10); color: #fff; }
    .labos-nav-dropdown button.active-mode { color: #F2B33D; font-weight: 700; }
    .labos-nav-dropdown hr { border: none; border-top: 1px solid rgba(255,255,255,0.12); margin: 4px 0; }
    .labos-nav-userrail { position: relative; }
    .labos-nav-burger {
      display: none; background: none; border: none; color: #fff;
      font-size: 20px; line-height: 1; padding: 2px 6px; cursor: pointer;
    }
    /* Mobile: collapse the app tabs behind a ☰ so the strip is one slim row. */
    @media (max-width: 640px) {
      .labos-nav-inner { gap: 10px; padding: 8px 12px; }
      .labos-nav-burger { display: inline-block; }
      .labos-nav-ver { display: none; }
      .labos-nav-tabs {
        display: none; position: absolute; top: 100%; left: 0; right: 0;
        background: #131A40; flex-direction: column; gap: 2px;
        padding: 6px 10px 10px; box-shadow: 0 8px 20px rgba(0,0,0,0.35);
      }
      .labos-nav.open .labos-nav-tabs { display: flex; }
      .labos-nav-tab { padding: 9px 12px; font-size: 14px; }
      .labos-nav-user { margin-left: auto; }
      .labos-nav-user .nm, .labos-nav-user .mode { display: none; }
    }
  `;

  function fmtInitials(u) {
    return (u.initials || (u.display_name || u.username || "?")[0]).toUpperCase();
  }

  function render(user) {
    const styleEl = document.createElement("style");
    styleEl.textContent = css;
    document.head.appendChild(styleEl);

    const tabs = APPS.map((a) => {
      const active = a.match(path) ? "active" : "";
      const externalAttrs = a.external ? ` target="_blank" rel="noopener" data-labos-external="${a.id}"` : "";
      return `<a class="labos-nav-tab ${active}" data-app-id="${a.id}" href="${a.href}"${externalAttrs}>
                <span>${a.icon}</span><span>${a.label}</span>
              </a>`;
    }).join("");

    const isAdmin = user && user.effective_role === "admin";
    const isAdminRole = user && user.role === "admin";
    const userBlock = user ? `
      <div class="labos-nav-user">
        <div class="labos-nav-userrail">
          <div class="labos-nav-userinfo" data-labos-userinfo>
            <div class="av" ${user.color ? `style="background:${user.color};color:#fff;"` : ""}>${fmtInitials(user)}</div>
            <span class="nm">${user.display_name || user.username || "—"}</span>
            <span class="mode ${isAdmin ? "" : "member"}">${isAdmin ? "Admin" : "User"}</span>
          </div>
        </div>
        <a class="logout" href="#" data-labos-logout>Sign out</a>
      </div>` : "";

    const navEl = document.createElement("div");
    navEl.className = "labos-nav";
    navEl.innerHTML = `
      <div class="labos-nav-inner">
        <button class="labos-nav-burger" data-labos-burger aria-label="Menu" type="button">☰</button>
        <div class="labos-nav-brand"><a href="/eln/dashboard">Lab OS</a></div>
        <span class="labos-nav-ver" data-labos-ver title="running version"></span>
        <div class="labos-nav-tabs">${tabs}</div>
        ${userBlock}
      </div>`;
    document.body.insertBefore(navEl, document.body.firstChild);
    document.body.classList.add("labos-unified");
    const localHeader = document.querySelector("header.app-top");
    if (localHeader && !localHeader.querySelector(".back,.crumbs,.breadcrumb")) localHeader.classList.add("labos-redundant");
    const passwordButton = document.getElementById('change-pw');
    if (passwordButton) {
      const passwordLink = document.createElement('button');
      passwordLink.type = 'button'; passwordLink.textContent = 'Password';
      passwordLink.style.cssText = 'background:none;border:0;color:#CADCFC;cursor:pointer;font:inherit';
      passwordLink.onclick = () => passwordButton.click();
      navEl.querySelector('.labos-nav-user')?.append(passwordLink);
    }

    const burger = navEl.querySelector("[data-labos-burger]");
    if (burger) burger.addEventListener("click", () => navEl.classList.toggle("open"));
    navEl.querySelectorAll(".labos-nav-tab").forEach((t) =>
      t.addEventListener("click", () => navEl.classList.remove("open")));
    navEl.querySelectorAll('[data-labos-external="animal_sheet"]').forEach((t) =>
      t.addEventListener("click", () => {
        try { localStorage.setItem("labos_animal_sheet_opened_at", String(Date.now())); } catch (_) {}
      }));

    fetch("/api/animals/sync-status", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((s) => {
        const url = s && s.sheet_url ? s.sheet_url : DEFAULT_ANIMAL_SHEET_URL;
        navEl.querySelectorAll('[data-app-id="animal_sheet"]').forEach((a) => {
          a.href = url; a.style.display = s?.sheet_url ? '' : 'none';
        });
      })
      .catch(() => {});

    fetch("/api/version", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((v) => {
        const el = navEl.querySelector("[data-labos-ver]");
        if (el && v) {
          el.textContent = "v" + v.version + (v.git_sha ? " · " + v.git_sha : "");
          if (v.deployed_at) el.title = "deployed " + v.deployed_at;
        }
      })
      .catch(() => {});

    const out = navEl.querySelector("[data-labos-logout]");
    if (out) {
      out.addEventListener("click", async (e) => {
        e.preventDefault();
        try {
          await fetch("/api/eln/auth/logout", { method: "POST", credentials: "include" });
        } catch (_) {}
        window.location.href = "/eln/login";
      });
    }

    // Mode-switch dropdown (admin-role users only)
    const userInfo = navEl.querySelector("[data-labos-userinfo]");
    if (userInfo && isAdminRole) {
      userInfo.addEventListener("click", (e) => {
        e.stopPropagation();
        let existing = navEl.querySelector(".labos-nav-dropdown");
        if (existing) { existing.remove(); return; }

        const dropdown = document.createElement("div");
        dropdown.className = "labos-nav-dropdown";
        dropdown.innerHTML = `
          <button class="${isAdmin ? "active-mode" : ""}" data-mode="admin">📊 Admin mode${isAdmin ? " ✓" : ""}</button>
          <button class="${!isAdmin ? "active-mode" : ""}" data-mode="member">🧪 User mode${!isAdmin ? " ✓" : ""}</button>
          <hr>
          <button data-mode="logout" style="color:#8FB3E6;">Sign out</button>`;
        navEl.querySelector(".labos-nav-userrail").appendChild(dropdown);

        dropdown.querySelectorAll("button[data-mode]").forEach((btn) => {
          btn.addEventListener("click", async (ev) => {
            ev.stopPropagation();
            const mode = btn.dataset.mode;
            if (mode === "logout") {
              try { await fetch("/api/eln/auth/logout", { method: "POST", credentials: "include" }); } catch (_) {}
              window.location.href = "/eln/login";
              return;
            }
            if ((mode === "admin" && isAdmin) || (mode === "member" && !isAdmin)) {
              dropdown.remove(); return;
            }
            try {
              await fetch("/api/eln/auth/set-mode", {
                method: "POST", credentials: "include",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ mode }),
              });
            } catch (_) {}
            location.reload();
          });
        });

        const closeDropdown = () => { dropdown.remove(); document.removeEventListener("click", closeDropdown); };
        setTimeout(() => document.addEventListener("click", closeDropdown), 0);
      });
    }
  }

  function redirectToLogin() {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.href = `/eln/login?next=${next}`;
  }

  let animalSyncInFlight = false;
  async function syncAnimalsAfterSheetEdit() {
    if (animalSyncInFlight) return;
    let openedAt = 0;
    try { openedAt = parseInt(localStorage.getItem("labos_animal_sheet_opened_at") || "0", 10); } catch (_) {}
    if (!openedAt || Date.now() - openedAt > 4 * 60 * 60 * 1000) return;
    animalSyncInFlight = true;
    try {
      const r = await fetch("/api/animals/sync", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (r.ok) {
        try { localStorage.removeItem("labos_animal_sheet_opened_at"); } catch (_) {}
        const data = await r.json().catch(() => ({}));
        window.dispatchEvent(new CustomEvent("labos:animals-synced", { detail: data }));
        if (data.report?.counts?.rejected || data.report?.conflicts?.length) {
          window.LabOS?.syncHistory();
        }
      } else {
        window.LabOS?.notice('Animal DB could not refresh from the source. Use Sync animals or contact the lab administrator.');
      }
    } catch (_) {
      window.LabOS?.notice('Animal DB refresh failed. Check the connection and try Sync animals.');
    } finally {
      animalSyncInFlight = false;
    }
  }
  window.addEventListener("focus", syncAnimalsAfterSheetEdit);

  fetch("/api/eln/auth/me", { credentials: "include" })
    .then((r) => {
      if (r.status === 401) { redirectToLogin(); return null; }
      if (!r.ok) return null;
      return r.json();
    })
    .then((d) => { if (d && d.user) render(d.user); })
    .catch(() => { /* network error — don't redirect, just skip nav */ });
})();
