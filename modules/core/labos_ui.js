(function () {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  async function api(path, options = {}) {
    const response = await fetch(path, {credentials:"include", headers:{"Content-Type":"application/json"}, ...options});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`);
    return data;
  }
  function noteStatus(note) {
    if (note.status === "submitted") return "Submitted · awaiting review";
    if (note.status === "approved") return note.approved_by && note.approved_by !== note.user_id ? "Approved" : "Finalized";
    return "Draft";
  }
  function notice(message, href) {
    const old = document.getElementById('labos-notice'); if (old) old.remove();
    const el = document.createElement('div'); el.id = 'labos-notice'; el.setAttribute('role','status');
    el.style.cssText='position:fixed;bottom:18px;left:18px;max-width:min(460px,90vw);padding:14px 18px;border-radius:10px;background:#1E2761;color:#fff;box-shadow:0 3px 18px #0003;z-index:30000;font:14px system-ui';
    el.textContent = message;
    if (href) { const link = document.createElement('a'); link.href=href; link.target='_blank'; link.rel='noopener'; link.textContent=' Open source Sheet ↗'; link.style.color='#F2B33D'; el.append(link); }
    document.body.append(el); setTimeout(()=>el.remove(),12000);
  }
  function dialog(title) {
    const previous = document.getElementById("labos-dialog");
    if (previous) previous.remove();
    const el = document.createElement("dialog");
    el.id = "labos-dialog";
    el.style.cssText = "width:min(720px,94vw);max-height:88vh;box-sizing:border-box;overflow:auto;border:1px solid #d8ddeb;border-radius:14px;padding:20px;color:#1c2333;background:#fff;z-index:20000;font:14px system-ui";
    el.innerHTML = `<div style="display:flex;gap:16px;align-items:center"><h2 style="font-size:18px;margin:0;flex:1">${esc(title)}</h2><button type="button" aria-label="Close" style="padding:8px 12px">✕</button></div><div class="dialog-body" style="margin-top:18px">Loading…</div>`;
    document.body.append(el);
    el.querySelector("button").onclick = () => el.close();
    el.addEventListener("close", () => el.remove());
    el.showModal();
    return el.querySelector(".dialog-body");
  }
  async function syncHistory() {
    const body = dialog("Animal synchronization history");
    try {
      const [data, status] = await Promise.all([api("/api/animals/sync-history"), api("/api/animals/sync-status")]);
      const reports = data.reports || [];
      body.innerHTML = `<p>Source: ${status.sheet_id ? "Google Sheet" : "local Excel"}. Soft-removed animals remain recoverable: restore their source row, then synchronize.</p>` +
        (reports.length ? reports.map(r => {
          const c = r.counts || {};
          const issues = [...(r.rejected || []).map(x => `${x.mouse_id || "Missing ID"}: ${(x.errors || []).join("; ")}`),
            ...(r.conflicts || []).map(mid => `${mid}: conflicting duplicate ID`),
            ...(r.warnings || []).map(x => `${x.mouse_id}: ${(x.warnings || []).join("; ")}`)];
          return `<details style="border-top:1px solid #ddd;padding:12px 0"><summary style="cursor:pointer">${esc(new Date(r.ts).toLocaleString())} · +${c.added || 0} added · ${c.modified || 0} changed · ${c.removed || 0} removed · ${c.rejected || 0} rejected</summary>
            ${issues.length ? `<ul>${issues.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : "<p>No validation issues.</p>"}
            ${(r.changes || []).length ? `<div style="overflow:auto"><table style="width:100%;border-collapse:collapse"><thead><tr><th>Mouse</th><th>Change</th><th>Before → After</th></tr></thead><tbody>${r.changes.map(x => `<tr><td style="padding:5px">${esc(x.mouse_id)}</td><td>${esc(x.field || x.change_type)}</td><td>${esc(x.old_value || "—")} → ${esc(x.new_value || "—")}</td></tr>`).join("")}</tbody></table></div>` : ""}</details>`;
        }).join("") : "<p>No synchronization history recorded yet.</p>");
    } catch (e) { body.textContent = e.message; }
  }
  async function openEventNotes(event, day) {
    const body = dialog("Experiment notes");
    try {
      const [links, projects] = await Promise.all([api(`/api/eln/events/${encodeURIComponent(event.id)}/notes`), api("/api/eln/projects")]);
      const available = projects.projects || [];
      body.innerHTML = `<p>${esc(event.title)}${event.mice?.length ? " · " + esc(event.mice.join(", ")) : ""}</p>` +
        (links.notes || []).map(n => `<p><a href="${esc(n.url)}">${esc(n.note_date)} · ${esc(n.author)} · ${esc(noteStatus(n))}</a></p>`).join("") +
        `<form><p><label>ELN project <select name="project" required style="width:100%;padding:9px;margin-top:5px"><option value="">Choose a project…</option>${available.map(p => `<option value="${esc(p.slug)}" ${p.slug === event.project_slug ? "selected" : ""}>${esc(p.title || p.slug)}</option>`).join("")}</select></label></p>
        <p><label>Experiment type <select name="type" style="width:100%;padding:9px;margin-top:5px"><option value="behavior">Behavior</option><option value="slice_recording">Slice recording</option></select></label></p>
        <p><label>Experiment date <input name="date" type="date" required value="${esc(day)}" style="padding:9px;width:100%;margin-top:5px"></label></p>
        <p class="msg" role="status"></p><button type="submit" style="padding:10px 14px">Create / open my note</button></form>`;
      const form = body.querySelector("form");
      form.elements.type.value = /slice|ephys/i.test(`${event.title} ${event.subcategory}`) ? "slice_recording" : "behavior";
      form.onsubmit = async e => {
        e.preventDefault();
        const button = form.querySelector("button");
        button.disabled = true;
        try {
          const result = await api(`/api/eln/events/${encodeURIComponent(event.id)}/note`, {method:"POST", body:JSON.stringify({
            project_slug:form.elements.project.value, experiment_type:form.elements.type.value, occurrence_date:form.elements.date.value})});
          location.href = result.url;
        } catch (err) { form.querySelector(".msg").textContent = err.message; button.disabled = false; }
      };
    } catch (e) { body.textContent = e.message; }
  }
  function expandEvents(events, lower, upper) {
    const output = [];
    for (const ev of events) {
      const duration = ev.end - ev.start;
      let start = new Date(ev.start);
      for (let i = 0; i < 20000 && start < upper; i++) {
        if (ev.recur?.until && start > new Date(ev.recur.until + 'T23:59:59')) break;
        const end = new Date(start.getTime() + duration);
        if (end > lower) output.push({...ev,start:new Date(start),end,_masterId:ev.id});
        const freq = ev.recur?.freq;
        if (freq === 'monthly') start.setMonth(start.getMonth()+1);
        else if (['daily','weekly','biweekly'].includes(freq)) start.setDate(start.getDate()+({daily:1,weekly:7,biweekly:14}[freq]));
        else break;
      }
    }
    return output;
  }
  window.LabOS = {esc, api, noteStatus, syncHistory, openEventNotes, expandEvents, notice};
})();
