// Minimal Nightshift Worker UI: Now + History, polling the worker status API.
// Cadence is taken from the manager-provided refresh (config-driven, never a
// hardcoded magic number per invariant 13); we fall back to 3s only if unset.

"use strict";

const FALLBACK_REFRESH_MS = 3000;
let refreshMs = FALLBACK_REFRESH_MS;
let timer = null;

async function getJSON(path) {
  const resp = await fetch(path, { headers: { Accept: "application/json" } });
  if (!resp.ok) throw new Error(`${path} -> ${resp.status}`);
  return resp.json();
}

function setView(view) {
  document.body.dataset.view = view;
  document.querySelectorAll(".nav-opt").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === view);
  });
  document.getElementById("view-now").hidden = view !== "now";
  document.getElementById("view-history").hidden = view !== "history";
  document.getElementById("view-settings").hidden = view !== "settings";
}

async function loadInfo() {
  try {
    const info = await getJSON("/api/info");
    document.getElementById("worker-id").textContent = info.worker_id || "worker";
    document.getElementById("worker-backend").textContent = info.backend || "?";
    document.getElementById("brand-tag").textContent = info.brand_tag || "Nightshift Worker";
    const models = info.models && info.models.length ? info.models.join(", ") : "—";
    const mcps = info.mcps && info.mcps.length ? info.mcps.join(", ") : "—";
    document.getElementById("cap-models").textContent = models;
    document.getElementById("cap-mcps").textContent = mcps;
    if (typeof info.refresh_ms === "number" && info.refresh_ms > 0) {
      refreshMs = info.refresh_ms;
    }
  } catch (_e) {
    /* manager/worker not ready yet */
  }
}

async function refreshNow() {
  let now = null;
  try {
    now = await getJSON("/api/now");
  } catch (_e) {
    now = null;
  }
  const idle = document.getElementById("now-idle");
  const card = document.getElementById("now-card");
  if (!now || !now.run_id) {
    idle.hidden = false;
    card.hidden = true;
    return;
  }
  idle.hidden = true;
  card.hidden = false;
  document.getElementById("now-task").textContent = now.title || now.task;
  document.getElementById("now-queue").textContent = now.queue || "main";
  document.getElementById("now-phase").textContent = now.phase || "worker";
  document.getElementById("now-model").textContent = `model: ${now.model || "auto"}`;
  document.getElementById("now-started").textContent = `started: ${now.started_at || "—"}`;
  const log = document.getElementById("now-log");
  log.textContent = (now.log_tail || []).join("");
  log.scrollTop = log.scrollHeight;
}

async function refreshHistory() {
  try {
    const [rows, stats] = await Promise.all([
      getJSON("/api/history"),
      getJSON("/api/stats"),
    ]);
    document.getElementById("st-total").textContent = stats.total_runs || 0;
    document.getElementById("st-done").textContent = stats.completed || 0;
    document.getElementById("st-err").textContent = stats.errored || 0;
    document.getElementById("st-loc").textContent = stats.total_loc || 0;
    const body = document.getElementById("history-body");
    body.innerHTML = "";
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${escapeHtml(r.title || r.task)}</td>` +
        `<td>${escapeHtml(r.queue || "main")}</td>` +
        `<td>${escapeHtml(r.model || "")}</td>` +
        `<td class="status-${escapeHtml(r.status || "")}">${escapeHtml(r.status || "")}</td>` +
        `<td>${escapeHtml(r.result_line || "")}</td>`;
      body.appendChild(tr);
    }
  } catch (_e) {
    /* ignore transient errors */
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

async function tick() {
  await refreshNow();
  if (document.body.dataset.view === "history") {
    await refreshHistory();
  }
  timer = setTimeout(tick, refreshMs);
}

// --------------------------------------------------------------------------
// Worker Settings (compact renderer over worker surface)
// --------------------------------------------------------------------------
const wSettings = { tiers: [], activeSurface: null, activeCategory: null, searchQuery: "", dirty: {}, errors: {} };

async function loadWorkerSettings() {
  try {
    const data = await getJSON("/api/settings");
    wSettings.tiers = data.tiers || [];
    wSettings.dirty = {};
    wSettings.errors = {};
    wSettings.searchQuery = "";
    if (wSettings.tiers.length && !wSettings.activeSurface) {
      const first = wSettings.tiers[0];
      wSettings.activeSurface = first.surface;
      if (first.categories.length) {
        wSettings.activeCategory = first.categories[0].name;
      }
    }
    const searchEl = document.getElementById("w-settings-search");
    if (searchEl) searchEl.value = "";
    renderWorkerSidebar();
    renderWorkerSettings();
    updateWorkerSaveBar();
  } catch (_e) { /* settings endpoint may not exist yet */ }
}

function renderWorkerSidebar() {
  const tree = document.getElementById("w-settings-tree");
  if (!tree) return;
  tree.innerHTML = "";
  for (const tier of wSettings.tiers) {
    const div = document.createElement("div");
    div.className = "w-st-tier";
    const label = document.createElement("div");
    label.className = "w-st-tier-label";
    label.textContent = tier.surface.charAt(0).toUpperCase() + tier.surface.slice(1);
    div.append(label);
    for (const cat of tier.categories) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "w-st-cat";
      if (tier.surface === wSettings.activeSurface && cat.name === wSettings.activeCategory) {
        btn.classList.add("active");
      }
      btn.textContent = cat.name;
      btn.addEventListener("click", () => {
        wSettings.activeSurface = tier.surface;
        wSettings.activeCategory = cat.name;
        wSettings.searchQuery = "";
        const searchEl = document.getElementById("w-settings-search");
        if (searchEl) searchEl.value = "";
        renderWorkerSidebar();
        renderWorkerSettings();
      });
      div.append(btn);
    }
    tree.append(div);
  }
}

function renderWorkerSettings() {
  const pane = document.getElementById("w-settings-fields");
  pane.innerHTML = "";
  const query = wSettings.searchQuery.toLowerCase().trim();
  if (query) {
    renderWorkerSearchResults(pane, query);
    return;
  }
  const tier = wSettings.tiers.find(t => t.surface === wSettings.activeSurface);
  if (!tier) return;
  const cat = tier.categories.find(c => c.name === wSettings.activeCategory);
  if (!cat) return;
  const title = document.createElement("h2");
  title.className = "w-settings-cat-title";
  title.textContent = cat.name;
  pane.append(title);
  for (const field of cat.fields) {
    pane.appendChild(buildWorkerFieldRow(field, tier.surface));
  }

  const allReadonly = cat.fields.every(f => f.type === "readonly");
  if (!allReadonly) {
    const rawDetails = document.createElement("details");
    rawDetails.className = "sf-raw-json";
    const summary = document.createElement("summary");
    summary.textContent = "Raw JSON";
    const textarea = document.createElement("textarea");
    textarea.rows = 8;
    textarea.spellcheck = false;
    const rawData = {};
    for (const f of cat.fields) {
      if (!f.secret && f.type !== "readonly") rawData[f.key] = f.stored;
    }
    textarea.value = JSON.stringify(rawData, null, 2);
    textarea.dataset.surface = tier.surface;
    textarea.dataset.category = cat.name;
    rawDetails.append(summary, textarea);
    pane.append(rawDetails);
  }
}

function renderWorkerSearchResults(pane, query) {
  const title = document.createElement("h2");
  title.className = "w-settings-cat-title";
  title.textContent = `Search: "${query}"`;
  pane.append(title);
  let count = 0;
  for (const tier of wSettings.tiers) {
    for (const cat of tier.categories) {
      for (const field of cat.fields) {
        const searchable = `${field.label} ${field.key} ${field.desc}`.toLowerCase();
        if (searchable.includes(query)) {
          pane.appendChild(buildWorkerFieldRow(field, tier.surface));
          count++;
        }
      }
    }
  }
  if (!count) {
    const empty = document.createElement("div");
    empty.className = "w-sf-empty-search";
    empty.textContent = "No settings match your search.";
    pane.append(empty);
  }
}

function buildWorkerFieldRow(field, surface) {
  const fullKey = `${surface}.${field.key}`;
  const row = document.createElement("div");
  row.className = "w-sf-row";
  if (fullKey in wSettings.dirty) row.classList.add("w-sf-dirty");
  if (fullKey in wSettings.errors) row.classList.add("w-sf-error");

  const head = document.createElement("div");
  head.className = "w-sf-head";
  const label = document.createElement("span");
  label.className = "w-sf-label";
  label.textContent = field.label;
  const key = document.createElement("code");
  key.className = "w-sf-key";
  key.textContent = field.key;
  const badges = document.createElement("span");
  badges.className = "w-sf-badges";
  if (field.apply === "restart") {
    const b = document.createElement("span");
    b.className = "w-sf-badge w-sf-badge-restart";
    b.textContent = "restart";
    badges.appendChild(b);
  } else if (field.apply === "live") {
    const b = document.createElement("span");
    b.className = "w-sf-badge w-sf-badge-live";
    b.textContent = "live";
    badges.appendChild(b);
  } else if (field.apply === "next-task") {
    const b = document.createElement("span");
    b.className = "w-sf-badge w-sf-badge-next-task";
    b.textContent = "next-task";
    badges.appendChild(b);
  }
  if (field.secret) {
    const b = document.createElement("span");
    b.className = "w-sf-badge w-sf-badge-secret";
    b.textContent = "secret";
    badges.appendChild(b);
  }
  head.append(label, key, badges);
  row.appendChild(head);

  const desc = document.createElement("div");
  desc.className = "w-sf-desc";
  desc.textContent = field.desc;
  row.appendChild(desc);

  if (field.env_shadowed) {
    const warn = document.createElement("div");
    warn.className = "w-sf-env-warn";
    warn.textContent = `Overridden by ${field.env}`;
    row.appendChild(warn);
  }

  const control = document.createElement("div");
  control.className = "w-sf-control";
  control.appendChild(buildWorkerControl(field, surface, fullKey));
  row.appendChild(control);

  if (fullKey in wSettings.errors) {
    const err = document.createElement("div");
    err.className = "w-sf-error-msg";
    err.textContent = wSettings.errors[fullKey];
    row.appendChild(err);
  }

  return row;
}

function buildWorkerControl(field, surface, fullKey) {
  const value = fullKey in wSettings.dirty ? wSettings.dirty[fullKey] : field.stored;

  if (field.secret) {
    const wrap = document.createElement("div");
    const status = document.createElement("div");
    status.className = "w-sf-secret-status";
    status.textContent = field.is_set ? "Currently set" : "Not set";
    const input = document.createElement("input");
    input.type = "password";
    input.placeholder = "Leave blank to keep current";
    input.addEventListener("input", () => {
      if (input.value) wSettings.dirty[fullKey] = input.value;
      else delete wSettings.dirty[fullKey];
      updateWorkerSaveBar();
    });
    wrap.append(status, input);
    return wrap;
  }

  switch (field.type) {
    case "bool": {
      const wrap = document.createElement("div");
      wrap.className = "w-sf-toggle";
      const track = document.createElement("div");
      track.className = "w-sf-toggle-track" + (value ? " on" : "");
      const thumb = document.createElement("div");
      thumb.className = "w-sf-toggle-thumb";
      track.appendChild(thumb);
      const lbl = document.createElement("span");
      lbl.textContent = value ? "On" : "Off";
      track.addEventListener("click", () => {
        const nv = !track.classList.contains("on");
        track.classList.toggle("on", nv);
        lbl.textContent = nv ? "On" : "Off";
        wMarkDirty(field, fullKey, nv);
      });
      wrap.append(track, lbl);
      return wrap;
    }
    case "enum": {
      const select = document.createElement("select");
      for (const opt of (field.options || [])) {
        const o = document.createElement("option");
        o.value = opt; o.textContent = opt;
        if (opt === value) o.selected = true;
        select.appendChild(o);
      }
      select.addEventListener("change", () => wMarkDirty(field, fullKey, select.value));
      return select;
    }
    case "int":
    case "float": {
      const input = document.createElement("input");
      input.type = "number";
      input.value = value != null ? value : "";
      if (field.type === "float") input.step = "any";
      input.addEventListener("input", () => {
        const v = field.type === "int" ? parseInt(input.value, 10) : parseFloat(input.value);
        if (!isNaN(v)) wMarkDirty(field, fullKey, v);
      });
      return input;
    }
    case "string_list":
    case "int_list":
    case "regex_list": {
      if (field.key === "queues") {
        return buildQueuesTagControl(field, surface, fullKey, value);
      }
      const items = Array.isArray(value) ? [...value] : [];
      const wrap = document.createElement("div");
      wrap.className = "w-sf-chips";
      const rerender = () => {
        wrap.innerHTML = "";
        items.forEach((item, i) => {
          const row = document.createElement("div");
          row.className = "w-sf-chip-row";
          const input = document.createElement("input");
          input.type = field.type === "int_list" ? "number" : "text";
          input.value = item;
          input.addEventListener("input", () => {
            items[i] = field.type === "int_list" ? parseInt(input.value, 10) : input.value;
            wMarkDirty(field, fullKey, [...items]);
          });
          const del = document.createElement("button");
          del.type = "button";
          del.className = "w-sf-chip-del";
          del.textContent = "\u00d7";
          del.addEventListener("click", () => {
            items.splice(i, 1);
            wMarkDirty(field, fullKey, [...items]);
            rerender();
          });
          row.append(input, del);
          wrap.appendChild(row);
        });
        const addBtn = document.createElement("button");
        addBtn.type = "button";
        addBtn.className = "w-sf-chip-add";
        addBtn.textContent = "+ Add";
        addBtn.addEventListener("click", () => {
          items.push(field.type === "int_list" ? 0 : "");
          wMarkDirty(field, fullKey, [...items]);
          rerender();
        });
        wrap.appendChild(addBtn);
      };
      rerender();
      return wrap;
    }
    case "str_map": {
      const entries = Object.entries(value || {}).map(([k, v]) => ({ k, v }));
      const wrap = document.createElement("div");
      wrap.className = "w-sf-map";
      const collectMap = () => {
        const obj = {};
        for (const e of entries) { if (e.k) obj[e.k] = e.v; }
        return obj;
      };
      const rerender = () => {
        wrap.innerHTML = "";
        entries.forEach((entry, i) => {
          const row = document.createElement("div");
          row.className = "w-sf-map-row";
          const ki = document.createElement("input");
          ki.placeholder = "key"; ki.value = entry.k;
          ki.addEventListener("input", () => { entries[i].k = ki.value; wMarkDirty(field, fullKey, collectMap()); });
          const vi = document.createElement("input");
          vi.placeholder = "value"; vi.value = entry.v;
          vi.addEventListener("input", () => { entries[i].v = vi.value; wMarkDirty(field, fullKey, collectMap()); });
          const del = document.createElement("button");
          del.type = "button"; del.className = "w-sf-chip-del"; del.textContent = "\u00d7";
          del.addEventListener("click", () => { entries.splice(i, 1); wMarkDirty(field, fullKey, collectMap()); rerender(); });
          row.append(ki, vi, del);
          wrap.appendChild(row);
        });
        const addBtn = document.createElement("button");
        addBtn.type = "button"; addBtn.className = "w-sf-chip-add"; addBtn.textContent = "+ Add";
        addBtn.addEventListener("click", () => { entries.push({ k: "", v: "" }); rerender(); });
        wrap.appendChild(addBtn);
      };
      rerender();
      return wrap;
    }
    default: {
      const input = document.createElement("input");
      input.type = "text";
      input.value = value != null ? value : "";
      input.addEventListener("input", () => wMarkDirty(field, fullKey, input.value || null));
      return input;
    }
  }
}

function buildQueuesTagControl(field, surface, fullKey, value) {
  const items = Array.isArray(value) ? [...value] : [];
  const wrap = document.createElement("div");
  wrap.className = "w-sf-queue-tags";

  const tagContainer = document.createElement("div");
  tagContainer.className = "w-sf-queue-tag-list";
  wrap.appendChild(tagContainer);

  const actions = document.createElement("div");
  actions.className = "w-sf-queue-actions";
  const rescanBtn = document.createElement("button");
  rescanBtn.type = "button";
  rescanBtn.className = "w-sf-queue-rescan";
  rescanBtn.textContent = "\u21BB Rescan";
  actions.appendChild(rescanBtn);
  wrap.appendChild(actions);

  const renderTags = () => {
    tagContainer.innerHTML = "";
    if (items.length === 0) {
      const empty = document.createElement("span");
      empty.className = "w-sf-queue-empty";
      empty.textContent = "No queues — click Rescan to populate from workspace";
      tagContainer.appendChild(empty);
      return;
    }
    for (let i = 0; i < items.length; i++) {
      const tag = document.createElement("span");
      tag.className = "w-sf-queue-tag";
      const name = document.createElement("span");
      name.className = "w-sf-queue-tag-name";
      name.textContent = items[i];
      const del = document.createElement("button");
      del.type = "button";
      del.className = "w-sf-queue-tag-del";
      del.textContent = "\u00d7";
      del.addEventListener("click", () => {
        items.splice(i, 1);
        wMarkDirty(field, fullKey, items.length ? [...items] : null);
        renderTags();
      });
      tag.append(name, del);
      tagContainer.appendChild(tag);
    }
  };

  rescanBtn.addEventListener("click", async () => {
    rescanBtn.disabled = true;
    rescanBtn.textContent = "\u21BB Scanning\u2026";
    try {
      const resp = await fetch("/api/scan-queues");
      if (resp.ok) {
        const data = await resp.json();
        items.length = 0;
        for (const q of (data.queues || [])) items.push(q);
        wMarkDirty(field, fullKey, items.length ? [...items] : null);
        renderTags();
      }
    } finally {
      rescanBtn.disabled = false;
      rescanBtn.textContent = "\u21BB Rescan";
    }
  });

  renderTags();
  return wrap;
}

function wMarkDirty(field, fullKey, value) {
  if (JSON.stringify(value) === JSON.stringify(field.stored)) {
    delete wSettings.dirty[fullKey];
  } else {
    wSettings.dirty[fullKey] = value;
  }
  delete wSettings.errors[fullKey];
  updateWorkerSaveBar();
}

function updateWorkerSaveBar() {
  const bar = document.getElementById("w-settings-savebar");
  const count = Object.keys(wSettings.dirty).length;
  if (count === 0) { bar.hidden = true; return; }
  bar.hidden = false;
  document.getElementById("w-settings-dirty-count").textContent =
    `${count} unsaved change${count === 1 ? "" : "s"}`;
}

async function saveWorkerSettings() {
  const delta = {};
  for (const [fullKey, value] of Object.entries(wSettings.dirty)) {
    const [surface, ...rest] = fullKey.split(".");
    const key = rest.join(".");
    if (!delta[surface]) delta[surface] = {};
    delta[surface][key] = value;
  }
  const resp = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(delta),
  });
  const data = await resp.json();
  if (!resp.ok) {
    if (data.errors) {
      wSettings.errors = data.errors;
      renderWorkerSettings();
    }
    return;
  }
  wSettings.dirty = {};
  wSettings.errors = {};
  wSettings.tiers = data.tiers || wSettings.tiers;
  updateWorkerSaveBar();
  renderWorkerSettings();
  if (data.restart_required && data.restart_required.length) {
    const banner = document.getElementById("w-settings-restart-banner");
    banner.textContent = "Restart this worker to apply: " +
      data.restart_required.map(k => k.split(".").slice(1).join(".")).join(", ");
    banner.hidden = false;
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  document.querySelectorAll(".nav-opt").forEach((b) => {
    b.addEventListener("click", () => {
      setView(b.dataset.view);
      if (b.dataset.view === "history") refreshHistory();
      if (b.dataset.view === "settings") loadWorkerSettings();
    });
  });
  document.getElementById("w-settings-discard").addEventListener("click", () => {
    wSettings.dirty = {};
    wSettings.errors = {};
    updateWorkerSaveBar();
    renderWorkerSettings();
  });
  document.getElementById("w-settings-save").addEventListener("click", saveWorkerSettings);
  const searchEl = document.getElementById("w-settings-search");
  if (searchEl) {
    searchEl.addEventListener("input", () => {
      wSettings.searchQuery = searchEl.value;
      renderWorkerSettings();
    });
  }
  await loadInfo();
  if (timer) clearTimeout(timer);
  tick();
});
