"use strict";

// Workflow editor (docs/spec/2026-07-17-workflow-editor.md §5) — a visual
// authoring surface over the same `.nightshift/workflows/*.json` files an
// operator could write by hand. Three screens:
//
//   * #screen-workflows       — the definition + prompt library (provenance,
//                               open / duplicate / delete).
//   * #screen-workflow-editor — ordered step cards (left) + a render-only SVG
//                               graph (right), a live validation strip driven
//                               by the manager's own parse_workflow, and a raw
//                               JSON toggle. Every edge destination is picked
//                               from a closed set — never drawn by hand.
//   * #screen-prompt-editor   — markdown source + rendered preview for the
//                               doc-step prompt charters.
//
// The browser duplicates NO validation rules: every edit round-trips through
// POST /api/workflows/validate (debounced ~300 ms). The two constructive
// exceptions — inputs checkboxes limited to earlier outputs, and the
// cycle-surfaced max_visits highlight — prevent errors rather than judge them.
//
// Loaded after app.js; reuses its globals ($, state, getJSON, sendJSON,
// escapeHtml, renderMarkdown, setView, loadWorkflows, workflowSteps).

const wfEd = {
  defs: {},          // /api/workflows payload {name: {steps, source, shadows_shipped}}
  prompts: {},       // /api/workflow-prompts payload {name: {source, shadows_shipped}}
  returnView: "now", // where the workflows list's back-affordances return to
  editor: null,      // the open definition-editor model (below)
  prompt: null,      // the open prompt-editor model
};

const WF_END = "$end";
const WF_VALIDATE_DEBOUNCE_MS = 300;

// --------------------------------------------------------------------------
// Data loading
// --------------------------------------------------------------------------

async function wfLoadLists() {
  try {
    const [defs, prompts] = await Promise.all([
      getJSON("/api/workflows"),
      getJSON("/api/workflow-prompts"),
    ]);
    wfEd.defs = defs;
    wfEd.prompts = prompts;
    state.workflows = defs; // keep app.js's picker/badge in sync
  } catch { /* transient; keep the last known lists */ }
}

// SSE convergence: another browser (or this one) changed a definition/prompt.
window.onWorkflowsChanged = async () => {
  await wfLoadLists();
  if (state.view === "workflows") renderWorkflowsScreen();
};

// Queued/blocked tasks whose frontmatter references a definition — the
// client-side warning count for definition/step deletion (spec §5).
function wfTasksReferencing(name) {
  return (state.queue || []).filter((t) => t.workflow === name).map((t) => t.task);
}

function wfProvenanceBadge(info) {
  const badge = document.createElement("span");
  if (info && info.shadows_shipped) {
    badge.className = "wf-prov shadow";
    badge.textContent = "shadows shipped";
    badge.title = "An operator file overrides the shipped definition of the same name";
  } else if (info && info.source === "operator") {
    badge.className = "wf-prov operator";
    badge.textContent = "operator";
  } else {
    badge.className = "wf-prov shipped";
    badge.textContent = "shipped";
    badge.title = "Shipped package asset — read-only; duplicate to edit";
  }
  return badge;
}

// --------------------------------------------------------------------------
// Screen: Workflows (the library)
// --------------------------------------------------------------------------

function renderWorkflowsScreen() {
  const body = $("workflows-list-body");
  if (!body) return;
  body.innerHTML = "";

  const defNames = Object.keys(wfEd.defs || {}).sort();
  const defList = document.createElement("ul");
  defList.className = "wf-lib-list";
  for (const name of defNames) {
    const info = wfEd.defs[name];
    const li = document.createElement("li");
    li.className = "wf-lib-row";

    const main = document.createElement("button");
    main.type = "button";
    main.className = "wf-lib-main";
    const title = document.createElement("span");
    title.className = "wf-lib-name";
    title.textContent = name;
    const path = document.createElement("span");
    path.className = "wf-lib-path";
    path.textContent = (info.steps || []).join(" → ");
    main.append(title, wfProvenanceBadge(info), path);
    main.addEventListener("click", () => openWorkflowEditor(name));

    const actions = document.createElement("div");
    actions.className = "wf-lib-actions";
    const dup = document.createElement("button");
    dup.type = "button";
    dup.className = "ghost-btn";
    dup.textContent = "Duplicate";
    dup.addEventListener("click", () => openWorkflowEditor(name, { duplicate: true }));
    actions.append(dup);
    if (info.source === "operator") {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "ghost-btn danger";
      del.textContent = info.shadows_shipped ? "Delete (restores shipped)" : "Delete";
      del.addEventListener("click", () => wfDeleteDefinition(name));
      actions.append(del);
    }
    li.append(main, actions);
    defList.append(li);
  }
  body.append(defList);
  if (!defNames.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No workflow definitions loaded.";
    body.append(empty);
  }

  const promptHead = document.createElement("div");
  promptHead.className = "pane-head wf-prompts-head";
  const h3 = document.createElement("h2");
  h3.textContent = "Prompts";
  const newPrompt = document.createElement("button");
  newPrompt.type = "button";
  newPrompt.className = "ghost-btn";
  newPrompt.textContent = "+ New prompt";
  newPrompt.addEventListener("click", () => openPromptEditor(null));
  promptHead.append(h3, newPrompt);
  body.append(promptHead);

  const hint = document.createElement("p");
  hint.className = "field-desc";
  hint.textContent =
    "Prompt charters for doc steps. Operator prompts live in " +
    ".nightshift/prompts/ and shadow shipped ones by filename.";
  body.append(hint);

  const pList = document.createElement("ul");
  pList.className = "wf-lib-list";
  for (const name of Object.keys(wfEd.prompts || {}).sort()) {
    const info = wfEd.prompts[name];
    const li = document.createElement("li");
    li.className = "wf-lib-row";
    const main = document.createElement("button");
    main.type = "button";
    main.className = "wf-lib-main";
    const title = document.createElement("span");
    title.className = "wf-lib-name";
    title.textContent = name;
    main.append(title, wfProvenanceBadge(info));
    main.addEventListener("click", () => openPromptEditor(name));
    const actions = document.createElement("div");
    actions.className = "wf-lib-actions";
    const dup = document.createElement("button");
    dup.type = "button";
    dup.className = "ghost-btn";
    dup.textContent = "Duplicate";
    dup.addEventListener("click", () => openPromptEditor(name, { duplicate: true }));
    actions.append(dup);
    if (info.source === "operator") {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "ghost-btn danger";
      del.textContent = info.shadows_shipped ? "Delete (restores shipped)" : "Delete";
      del.addEventListener("click", () => wfDeletePrompt(name));
      actions.append(del);
    }
    li.append(main, actions);
    pList.append(li);
  }
  body.append(pList);

  // First visit before the primed load finished: fetch then repaint once.
  if (!wfEd._loadedOnce) {
    wfEd._loadedOnce = true;
    wfLoadLists().then(() => {
      if (state.view === "workflows") renderWorkflowsScreen();
    });
  }
}

async function wfDeleteDefinition(name) {
  const info = wfEd.defs[name] || {};
  const refs = wfTasksReferencing(name);
  let msg = `Delete workflow definition '${name}'?`;
  if (info.shadows_shipped) msg += "\n\nThis restores the shipped definition of the same name.";
  if (refs.length) {
    msg += `\n\n${refs.length} queued/blocked task(s) reference it (` +
      refs.slice(0, 5).join(", ") + (refs.length > 5 ? ", …" : "") +
      "); they will block with an unknown-workflow reason until re-pointed.";
  }
  if (!window.confirm(msg)) return;
  const { ok, data } = await sendJSON(
    `/api/workflows/${encodeURIComponent(name)}`, "DELETE",
  );
  if (!ok) {
    window.alert(`Delete failed: ${(data && data.error) || "unknown error"}`);
    return;
  }
  await wfLoadLists();
  if (state.view === "workflows") renderWorkflowsScreen();
}

async function wfDeletePrompt(name) {
  const info = wfEd.prompts[name] || {};
  let msg = `Delete prompt '${name}'?`;
  if (info.shadows_shipped) msg += "\n\nThis restores the shipped prompt of the same name.";
  if (!window.confirm(msg)) return;
  const { ok, data } = await sendJSON(
    `/api/workflow-prompts/${encodeURIComponent(name)}`, "DELETE",
  );
  if (!ok) {
    window.alert(`Delete failed: ${(data && data.error) || "unknown error"}`);
    return;
  }
  await wfLoadLists();
  if (state.view === "workflows") renderWorkflowsScreen();
}

// --------------------------------------------------------------------------
// Definition editor — model <-> JSON
// --------------------------------------------------------------------------
// The editor model mirrors the JSON vocabulary exactly (spec §3.1 of the
// workflows spec): three step kinds, next/signals/max_visits, and the
// absent/null/int max_turns tri-state made explicit as `turnsMode`.

function wfNewStepModel(kind = "doc") {
  return {
    id: "", kind, role: kind === "doc" ? "planner" : "implementor",
    prompt: "", output: "", inputs: ["brief"],
    turnsMode: "inherit", turns: 30,
    signals: [],           // [{token, dest}]
    next: "",              // "" = auto (absent key: next in list / $end)
    maxVisits: "",         // "" = absent
  };
}

function wfStepFromJson(raw) {
  const s = wfNewStepModel(raw.kind || "doc");
  s.id = raw.id || "";
  s.role = raw.role || "";
  s.prompt = raw.prompt || "";
  s.output = raw.output || "";
  s.inputs = Array.isArray(raw.inputs) ? [...raw.inputs] : [];
  if (!("max_turns" in raw)) s.turnsMode = "inherit";
  else if (raw.max_turns === null) s.turnsMode = "unbounded";
  else { s.turnsMode = "n"; s.turns = raw.max_turns; }
  s.signals = Object.entries(raw.signals || {}).map(([token, dest]) => ({ token, dest }));
  s.next = "next" in raw ? (raw.next || "") : "";
  s.maxVisits = "max_visits" in raw ? String(raw.max_visits) : "";
  return s;
}

function wfStepToJson(s) {
  const o = { id: s.id.trim(), kind: s.kind, role: s.role.trim() };
  if (s.kind === "doc") {
    o.prompt = s.prompt;
    o.output = s.output.trim();
  }
  if (s.inputs.length) o.inputs = [...s.inputs];
  if (s.turnsMode === "unbounded") o.max_turns = null;
  else if (s.turnsMode === "n") o.max_turns = Number(s.turns) || 0;
  const sigs = s.signals.filter((r) => r.token.trim());
  if (sigs.length) {
    o.signals = {};
    for (const r of sigs) o.signals[r.token.trim()] = r.dest;
  }
  if (s.next) o.next = s.next;
  if (String(s.maxVisits).trim() !== "") o.max_visits = Number(s.maxVisits);
  return o;
}

function wfBuildJson(ed) {
  return { name: ed.name.trim(), steps: ed.steps.map(wfStepToJson) };
}

// Every place the cursor may go from step i: signal targets + the effective
// `next` (explicit, else next-in-list, else $end).
function wfDestinations(steps, i) {
  const dests = [];
  const s = steps[i];
  for (const r of s.signals) if (r.token.trim() && r.dest) dests.push(r.dest);
  const nxt = s.next || (i + 1 < steps.length ? steps[i + 1].id : WF_END);
  dests.push(nxt);
  return dests;
}

// Steps on a cycle (reachable from themselves) — the constructive UI cue that
// max_visits is required, shown before the validator has to say it.
function wfCycleSteps(steps) {
  const ids = steps.map((s) => s.id);
  const edges = {};
  for (let i = 0; i < steps.length; i++) {
    edges[ids[i]] = wfDestinations(steps, i).filter((d) => d !== WF_END && ids.includes(d));
  }
  const onCycle = new Set();
  for (const start of ids) {
    const seen = new Set();
    const stack = [...(edges[start] || [])];
    while (stack.length) {
      const node = stack.pop();
      if (seen.has(node)) continue;
      seen.add(node);
      stack.push(...(edges[node] || []));
    }
    if (seen.has(start)) onCycle.add(start);
  }
  return onCycle;
}

// --------------------------------------------------------------------------
// Definition editor — open / screen render
// --------------------------------------------------------------------------

async function openWorkflowEditor(name, { duplicate = false } = {}) {
  if (!["workflow-editor", "prompt-editor"].includes(state.view)) {
    wfEd.returnView = state.view === "workflows" ? "workflows" : state.view;
  }
  await wfLoadLists();
  let ed;
  if (name) {
    let data;
    try {
      data = await getJSON(`/api/workflows/${encodeURIComponent(name)}`);
    } catch {
      window.alert(`Could not load workflow '${name}'.`);
      return;
    }
    const def = data.definition || {};
    ed = {
      name: duplicate ? `${name}-copy` : name,
      nameEditable: duplicate,
      isNew: duplicate,
      source: duplicate ? "operator" : data.source,
      shadowsShipped: !duplicate && data.shadows_shipped,
      shippedDefinition: duplicate ? null : data.shipped_definition,
      readOnly: !duplicate && data.source === "shipped",
      steps: (def.steps || []).map(wfStepFromJson),
    };
  } else {
    const step = wfNewStepModel("code");
    step.id = "implement";
    ed = {
      name: "", nameEditable: true, isNew: true,
      source: "operator", shadowsShipped: false, shippedDefinition: null,
      readOnly: false,
      steps: [step],
    };
  }
  ed.rawMode = false;
  ed.rawText = "";
  ed.rawError = null;
  ed.valid = null;       // null = validation pending/unknown
  ed.error = null;
  ed.errorStep = null;   // step id named by the current error (card outline)
  ed.validating = false;
  ed.dirty = false;
  ed.showShipped = false;
  wfEd.editor = ed;
  setView("workflow-editor");
  wfScheduleValidate(0);
}

function renderWorkflowEditor() {
  const ed = wfEd.editor;
  const body = $("wf-editor-body");
  if (!body) return;
  if (!ed) { setView("workflows"); return; }
  $("wf-editor-title").textContent = ed.isNew && !ed.name
    ? "New workflow" : (ed.name || "Workflow");
  body.innerHTML = "";

  // ----- header row: name, provenance, mode toggle, actions --------------
  const head = document.createElement("div");
  head.className = "wf-ed-head";

  if (ed.nameEditable) {
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "wf-ed-name";
    nameInput.placeholder = "workflow-name";
    nameInput.value = ed.name;
    nameInput.addEventListener("input", () => {
      ed.name = nameInput.value;
      ed.dirty = true;
      wfUpdateShadowNote();
      wfScheduleValidate();
    });
    head.append(nameInput);
  } else {
    const nameEl = document.createElement("span");
    nameEl.className = "wf-ed-name-static";
    nameEl.textContent = ed.name;
    head.append(nameEl);
  }
  head.append(wfProvenanceBadge({
    source: ed.source, shadows_shipped: ed.shadowsShipped,
  }));

  // Form | JSON segmented toggle (two-way raw view, spec §5).
  const seg = document.createElement("div");
  seg.className = "segmented wf-ed-modeseg";
  for (const [label, raw] of [["Form", false], ["JSON", true]]) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "seg-opt" + (ed.rawMode === raw ? " on" : "");
    b.textContent = label;
    b.addEventListener("click", () => wfSetRawMode(raw));
    seg.append(b);
  }
  head.append(seg);

  const spacer = document.createElement("span");
  spacer.className = "wf-ed-spacer";
  head.append(spacer);

  if (ed.readOnly) {
    const dup = document.createElement("button");
    dup.type = "button";
    dup.className = "ghost-btn";
    dup.textContent = "Duplicate to edit";
    dup.addEventListener("click", () => openWorkflowEditor(ed.name, { duplicate: true }));
    head.append(dup);
  } else if (!ed.isNew) {
    const del = document.createElement("button");
    del.type = "button";
    del.className = "ghost-btn danger";
    del.textContent = ed.shadowsShipped ? "Delete (restores shipped)" : "Delete";
    del.addEventListener("click", async () => {
      await wfDeleteDefinition(ed.name);
      wfEd.editor = null;
      setView("workflows");
    });
    head.append(del);
  }
  body.append(head);

  // Shadow warning (spec §5: surfaced deliberately, not left as a foot-gun).
  const shadowNote = document.createElement("div");
  shadowNote.id = "wf-ed-shadow-note";
  shadowNote.className = "wf-ed-shadow-note";
  body.append(shadowNote);

  // Read-only note for shipped definitions.
  if (ed.readOnly) {
    const note = document.createElement("div");
    note.className = "wf-ed-readonly-note";
    note.textContent =
      "Shipped definitions are read-only package assets. Duplicate to make an editable copy.";
    body.append(note);
  }

  // ----- main split: cards (or raw JSON) | graph --------------------------
  const split = document.createElement("div");
  split.className = "wf-ed-split";

  const left = document.createElement("div");
  left.className = "wf-ed-left";
  if (ed.rawMode) {
    const ta = document.createElement("textarea");
    ta.className = "wf-ed-raw";
    ta.spellcheck = false;
    ta.readOnly = ed.readOnly;
    ta.value = ed.rawText;
    ta.addEventListener("input", () => {
      ed.rawText = ta.value;
      ed.dirty = true;
      wfScheduleValidate();
    });
    left.append(ta);
  } else {
    const cards = document.createElement("div");
    cards.id = "wf-ed-cards";
    cards.className = "wf-ed-cards";
    ed.steps.forEach((step, i) => cards.append(wfRenderStepCard(step, i)));
    left.append(cards);
    if (!ed.readOnly) {
      const add = document.createElement("button");
      add.type = "button";
      add.className = "ghost-btn wf-ed-addstep";
      add.textContent = "+ Add step";
      add.addEventListener("click", () => {
        ed.steps.push(wfNewStepModel("doc"));
        wfEditorChanged({ structural: true });
      });
      left.append(add);
    }
  }
  split.append(left);

  const right = document.createElement("div");
  right.className = "wf-ed-right";
  const graphCap = document.createElement("div");
  graphCap.className = "wf-ed-graph-cap";
  graphCap.textContent = "GRAPH PREVIEW";
  const graphHost = document.createElement("div");
  graphHost.id = "wf-ed-graph";
  graphHost.className = "wf-ed-graph";
  right.append(graphCap, graphHost);

  // Diff-against-shipped affordance for shadows (read-only JSON of the
  // shipped original beneath the graph).
  if (ed.shadowsShipped && ed.shippedDefinition) {
    const tgl = document.createElement("button");
    tgl.type = "button";
    tgl.className = "ghost-btn wf-ed-shipped-toggle";
    tgl.textContent = ed.showShipped ? "Hide shipped original" : "View shipped original";
    tgl.addEventListener("click", () => {
      ed.showShipped = !ed.showShipped;
      renderWorkflowEditor();
    });
    right.append(tgl);
    if (ed.showShipped) {
      const pre = document.createElement("pre");
      pre.className = "wf-ed-shipped-json";
      pre.textContent = JSON.stringify(ed.shippedDefinition, null, 2);
      right.append(pre);
    }
  }
  split.append(right);
  body.append(split);

  // ----- validation strip + save bar --------------------------------------
  const strip = document.createElement("div");
  strip.id = "wf-ed-validation";
  strip.className = "wf-ed-validation";
  body.append(strip);

  const bar = document.createElement("div");
  bar.className = "wf-ed-savebar";
  const save = document.createElement("button");
  save.id = "wf-ed-save";
  save.type = "button";
  save.className = "btn primary";
  save.textContent = "Save";
  save.addEventListener("click", wfSaveDefinition);
  bar.append(save);
  body.append(bar);

  wfUpdateShadowNote();
  wfUpdateValidationStrip();
  wfRenderGraph();
}

// A step card: every field of the vocabulary as a form control (spec §5).
function wfRenderStepCard(step, index) {
  const ed = wfEd.editor;
  const ro = ed.readOnly;
  const card = document.createElement("section");
  card.className = "wf-card";
  card.dataset.stepIndex = String(index);
  card.dataset.stepId = step.id;
  if (ed.errorStep && ed.errorStep === step.id) card.classList.add("wf-card-error");

  // Drag-to-reorder: list order defines default `next` chaining and the
  // inputs-from-earlier-steps rule.
  card.draggable = !ro;
  card.addEventListener("dragstart", (e) => {
    if (ro) return;
    e.dataTransfer.setData("text/wf-step", String(index));
    e.dataTransfer.effectAllowed = "move";
    card.classList.add("dragging");
  });
  card.addEventListener("dragend", () => card.classList.remove("dragging"));
  card.addEventListener("dragover", (e) => {
    if (ro) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    card.classList.add("dragover");
  });
  card.addEventListener("dragleave", () => card.classList.remove("dragover"));
  card.addEventListener("drop", (e) => {
    if (ro) return;
    e.preventDefault();
    card.classList.remove("dragover");
    const from = parseInt(e.dataTransfer.getData("text/wf-step"), 10);
    if (Number.isNaN(from) || from === index) return;
    const [moved] = ed.steps.splice(from, 1);
    ed.steps.splice(index, 0, moved);
    wfEditorChanged({ structural: true });
  });

  // ----- head: handle, index, id, kind, remove ---------------------------
  const head = document.createElement("div");
  head.className = "wf-card-head";

  const handle = document.createElement("span");
  handle.className = "wf-card-handle";
  handle.title = "Drag to reorder";
  handle.textContent = "⠿";
  head.append(handle);

  const idInput = document.createElement("input");
  idInput.type = "text";
  idInput.className = "wf-card-id";
  idInput.placeholder = "step-id";
  idInput.value = step.id;
  idInput.disabled = ro;
  idInput.addEventListener("input", () => { step.id = idInput.value; wfEditorChanged({ light: true }); });
  // Renames ripple into every destination picker: rebuild on commit (blur).
  idInput.addEventListener("change", () => wfEditorChanged({ structural: true }));
  head.append(idInput);

  const kindSeg = document.createElement("div");
  kindSeg.className = "segmented wf-card-kindseg";
  for (const kind of ["doc", "code", "split"]) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "seg-opt" + (step.kind === kind ? " on" : "");
    b.textContent = kind;
    b.disabled = ro;
    b.addEventListener("click", () => {
      step.kind = kind;
      // The vocabulary forbids prompt/output off doc steps; a split step
      // routes only to $end. Clear rather than carry illegal fields.
      if (kind !== "doc") { step.prompt = ""; step.output = ""; }
      if (kind === "split") { step.next = WF_END; step.signals = []; }
      wfEditorChanged({ structural: true });
    });
    kindSeg.append(b);
  }
  head.append(kindSeg);

  if (!ro) {
    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "ghost-btn danger wf-card-remove";
    rm.textContent = "✕";
    rm.title = "Remove step";
    rm.addEventListener("click", () => {
      const refs = wfTasksReferencing(ed.name).length;
      const cursorHere = (state.queue || []).some(
        (t) => t.workflow === ed.name && t.workflow_step === step.id,
      );
      if (cursorHere && !window.confirm(
        `A queued task's cursor is on step '${step.id}'; removing it will block ` +
        "that task until the step is restored. Remove anyway?",
      )) return;
      if (!cursorHere && refs && !window.confirm(
        `${refs} queued task(s) run '${ed.name}'. Remove step '${step.id}'?`,
      )) return;
      ed.steps.splice(index, 1);
      wfEditorChanged({ structural: true });
    });
    head.append(rm);
  }
  card.append(head);

  const grid = document.createElement("div");
  grid.className = "wf-card-grid";

  const fieldRow = (label, control, opts = {}) => {
    const row = document.createElement("label");
    row.className = "wf-field" + (opts.cls ? ` ${opts.cls}` : "");
    const cap = document.createElement("span");
    cap.className = "wf-field-cap";
    cap.textContent = label;
    row.append(cap, control);
    grid.append(row);
    return row;
  };

  // role: suggested planner/implementor, free text allowed (§3.2 "any other
  // key" is legal vocabulary).
  const role = document.createElement("input");
  role.type = "text";
  role.className = "wf-card-role";
  role.setAttribute("list", "wf-role-suggestions");
  role.value = step.role;
  role.disabled = ro;
  role.placeholder = "planner | implementor | …";
  role.addEventListener("input", () => { step.role = role.value; wfEditorChanged({ light: true }); });
  fieldRow("Role", role);

  // Kind-dependent fields, shown/hidden live: doc → prompt picker + output.
  if (step.kind === "doc") {
    const promptWrap = document.createElement("div");
    promptWrap.className = "wf-prompt-wrap";
    const promptSel = document.createElement("select");
    promptSel.disabled = ro;
    const names = Object.keys(wfEd.prompts || {}).sort();
    if (step.prompt && !names.includes(step.prompt)) names.unshift(step.prompt);
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "— pick a prompt —";
    promptSel.append(blank);
    for (const n of names) {
      const opt = document.createElement("option");
      opt.value = n;
      opt.textContent = n;
      opt.selected = n === step.prompt;
      promptSel.append(opt);
    }
    promptSel.addEventListener("change", () => {
      step.prompt = promptSel.value;
      wfEditorChanged({});
    });
    const editPrompt = document.createElement("button");
    editPrompt.type = "button";
    editPrompt.className = "ghost-btn wf-prompt-edit";
    editPrompt.textContent = "Edit prompt…";
    editPrompt.disabled = !step.prompt;
    editPrompt.addEventListener("click", () => {
      if (step.prompt) openPromptEditor(step.prompt, { returnTo: "workflow-editor" });
    });
    promptWrap.append(promptSel, editPrompt);
    fieldRow("Prompt", promptWrap, { cls: "wf-field-wide" });

    const output = document.createElement("input");
    output.type = "text";
    output.placeholder = "artifact name (e.g. plan)";
    output.value = step.output;
    output.disabled = ro;
    output.addEventListener("input", () => { step.output = output.value; wfEditorChanged({ light: true }); });
    output.addEventListener("change", () => wfEditorChanged({ structural: true }));
    fieldRow("Output", output);
  }

  // inputs: checkboxes over the closed set {brief} ∪ {earlier outputs} —
  // recomputed on reorder, so the earlier-step rule is unviolatable by
  // construction.
  const avail = ["brief"];
  for (let j = 0; j < index; j++) {
    const out = (wfEd.editor.steps[j].output || "").trim();
    if (out && !avail.includes(out)) avail.push(out);
  }
  const inputsWrap = document.createElement("div");
  inputsWrap.className = "wf-inputs";
  for (const name of avail) {
    const lbl = document.createElement("label");
    lbl.className = "wf-input-choice";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = step.inputs.includes(name);
    cb.disabled = ro;
    cb.addEventListener("change", () => {
      // Keep declaration order stable: brief first, then earlier outputs.
      const next = new Set(step.inputs);
      if (cb.checked) next.add(name); else next.delete(name);
      step.inputs = avail.filter((n) => next.has(n));
      wfEditorChanged({});
    });
    lbl.append(cb, document.createTextNode(name));
    inputsWrap.append(lbl);
  }
  // An input naming a no-longer-available artifact (after a reorder) is
  // dropped from the model too — constructive, not validated.
  step.inputs = step.inputs.filter((n) => avail.includes(n));
  fieldRow("Inputs", inputsWrap, { cls: "wf-field-wide" });

  // max_turns tri-state: inherit | unbounded | n — the JSON's absent-vs-null
  // distinction made explicit instead of invisible.
  const turnsWrap = document.createElement("div");
  turnsWrap.className = "wf-turns";
  const turnsSeg = document.createElement("div");
  turnsSeg.className = "segmented";
  for (const [label, mode] of [["Inherit", "inherit"], ["Unbounded", "unbounded"], ["Limit", "n"]]) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "seg-opt" + (step.turnsMode === mode ? " on" : "");
    b.textContent = label;
    b.disabled = ro;
    b.addEventListener("click", () => { step.turnsMode = mode; wfEditorChanged({ structural: true }); });
    turnsSeg.append(b);
  }
  turnsWrap.append(turnsSeg);
  if (step.turnsMode === "n") {
    const n = document.createElement("input");
    n.type = "number";
    n.min = "1";
    n.className = "wf-turns-n";
    n.value = String(step.turns);
    n.disabled = ro;
    n.addEventListener("input", () => { step.turns = n.value; wfEditorChanged({ light: true }); });
    turnsWrap.append(n);
  }
  fieldRow("Max turns", turnsWrap);

  // Destination pickers: signals rows + next. Every destination is chosen
  // from step ids + $end — edges are never drawn by hand (the anti-canvas
  // decision, spec §5). Split steps route only to $end.
  const ids = wfEd.editor.steps.map((s) => s.id.trim()).filter(Boolean);
  const destSelect = (value, onchange, { autoLabel = null, endOnly = false } = {}) => {
    const sel = document.createElement("select");
    sel.disabled = ro || endOnly;
    const options = [];
    if (autoLabel !== null) options.push(["", autoLabel]);
    if (!endOnly) for (const id of ids) options.push([id, id]);
    options.push([WF_END, WF_END]);
    if (value && !options.some(([v]) => v === value)) options.push([value, `${value} (missing)`]);
    for (const [v, label] of options) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = label;
      opt.selected = v === value;
      sel.append(opt);
    }
    sel.addEventListener("change", () => onchange(sel.value));
    return sel;
  };

  if (step.kind !== "split") {
    const sigWrap = document.createElement("div");
    sigWrap.className = "wf-signals";
    step.signals.forEach((row, sigIdx) => {
      const line = document.createElement("div");
      line.className = "wf-signal-row";
      const token = document.createElement("input");
      token.type = "text";
      token.placeholder = "signal token";
      token.value = row.token;
      token.disabled = ro;
      token.addEventListener("input", () => { row.token = token.value; wfEditorChanged({ light: true }); });
      token.addEventListener("change", () => wfEditorChanged({ structural: true }));
      const arrow = document.createElement("span");
      arrow.className = "wf-signal-arrow";
      arrow.textContent = "→";
      const dest = destSelect(row.dest, (v) => { row.dest = v; wfEditorChanged({}); });
      line.append(token, arrow, dest);
      if (!ro) {
        const rm = document.createElement("button");
        rm.type = "button";
        rm.className = "ghost-btn wf-signal-rm";
        rm.textContent = "✕";
        rm.addEventListener("click", () => {
          step.signals.splice(sigIdx, 1);
          wfEditorChanged({ structural: true });
        });
        line.append(rm);
      }
      sigWrap.append(line);
    });
    if (!ro) {
      const add = document.createElement("button");
      add.type = "button";
      add.className = "ghost-btn wf-signal-add";
      add.textContent = "+ signal";
      add.addEventListener("click", () => {
        step.signals.push({ token: "", dest: WF_END });
        wfEditorChanged({ structural: true });
      });
      sigWrap.append(add);
    }
    fieldRow("Signals", sigWrap, { cls: "wf-field-wide" });
  }

  const autoDest = index + 1 < wfEd.editor.steps.length
    ? (wfEd.editor.steps[index + 1].id.trim() || "next step") : WF_END;
  const nextSel = destSelect(
    step.kind === "split" ? WF_END : step.next,
    (v) => { step.next = v; wfEditorChanged({}); },
    {
      autoLabel: step.kind === "split" ? null : `auto (${autoDest})`,
      endOnly: step.kind === "split",
    },
  );
  fieldRow("Next", nextSel);

  // max_visits: auto-surfaced (highlighted, required) when the live graph
  // detects the step is on a cycle — the validator's rule, shown first.
  const onCycle = wfCycleSteps(wfEd.editor.steps).has(step.id.trim());
  const mv = document.createElement("input");
  mv.type = "number";
  mv.min = "1";
  mv.placeholder = onCycle ? "required (on a cycle)" : "default 1";
  mv.value = step.maxVisits;
  mv.disabled = ro;
  mv.addEventListener("input", () => { step.maxVisits = mv.value; wfEditorChanged({ light: true }); });
  mv.addEventListener("change", () => wfEditorChanged({ structural: true }));
  const mvRow = fieldRow("Max visits", mv);
  if (onCycle && String(step.maxVisits).trim() === "") mvRow.classList.add("wf-field-required");

  card.append(grid);
  return card;
}

// One shared datalist for role suggestions.
function wfEnsureRoleDatalist() {
  if (document.getElementById("wf-role-suggestions")) return;
  const dl = document.createElement("datalist");
  dl.id = "wf-role-suggestions";
  for (const role of ["planner", "implementor"]) {
    const opt = document.createElement("option");
    opt.value = role;
    dl.append(opt);
  }
  document.body.append(dl);
}

// --------------------------------------------------------------------------
// Definition editor — change plumbing, validation, save
// --------------------------------------------------------------------------

// `light` edits (typing in a text/number field) revalidate + redraw the graph
// without rebuilding the cards (a rebuild would eat the caret); every other
// edit (reorder, add/remove, kind, pickers, checkboxes, committed renames)
// rebuilds the whole editor so dependent controls — destination pickers,
// inputs checkboxes, the cycle-surfaced max_visits highlight — stay true.
function wfEditorChanged({ structural = false, light = false } = {}) {
  const ed = wfEd.editor;
  if (!ed) return;
  void structural;
  ed.dirty = true;
  if (light) {
    wfRenderGraph();
    wfUpdateShadowNote();
  } else {
    renderWorkflowEditor();
  }
  wfScheduleValidate();
}

let _wfValidateTimer = null;
let _wfValidateSeq = 0;

function wfScheduleValidate(delay = WF_VALIDATE_DEBOUNCE_MS) {
  const ed = wfEd.editor;
  if (!ed) return;
  ed.validating = true;
  ed.valid = null;
  wfUpdateValidationStrip();
  if (_wfValidateTimer) clearTimeout(_wfValidateTimer);
  _wfValidateTimer = setTimeout(wfValidateNow, delay);
}

async function wfValidateNow() {
  const ed = wfEd.editor;
  if (!ed) return;
  const seq = ++_wfValidateSeq;
  let candidate;
  if (ed.rawMode) {
    try {
      candidate = JSON.parse(ed.rawText);
      ed.rawError = null;
    } catch (e) {
      ed.rawError = `JSON parse error: ${e.message}`;
      ed.valid = false;
      ed.error = ed.rawError;
      ed.errorStep = null;
      ed.validating = false;
      wfUpdateValidationStrip();
      return;
    }
  } else {
    candidate = wfBuildJson(ed);
  }
  if (!String(candidate.name || "").trim()) {
    ed.valid = false;
    ed.error = "the definition needs a name";
    ed.errorStep = null;
    ed.validating = false;
    wfUpdateValidationStrip();
    return;
  }
  let verdict;
  try {
    const r = await fetch("/api/workflows/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(candidate),
    });
    verdict = await r.json();
  } catch {
    verdict = { ok: false, error: "validation request failed (manager unreachable?)" };
  }
  if (seq !== _wfValidateSeq || wfEd.editor !== ed) return; // stale response
  ed.validating = false;
  ed.valid = !!verdict.ok;
  ed.error = verdict.ok ? null : (verdict.error || "invalid definition");
  // Outline the offending card when the error names a step id.
  const m = ed.error && ed.error.match(/step '([^']+)'/);
  const newErrorStep = m ? m[1] : null;
  if (newErrorStep !== ed.errorStep) {
    ed.errorStep = newErrorStep;
    const cards = document.querySelectorAll("#wf-ed-cards .wf-card");
    for (const c of cards) {
      c.classList.toggle("wf-card-error", !!newErrorStep && c.dataset.stepId === newErrorStep);
    }
  }
  wfUpdateValidationStrip();
}

function wfUpdateValidationStrip() {
  const ed = wfEd.editor;
  const strip = $("wf-ed-validation");
  const save = $("wf-ed-save");
  if (!ed || !strip) return;
  strip.classList.remove("ok", "err", "pending");
  if (ed.readOnly) {
    strip.textContent = "Read-only (shipped definition).";
    strip.classList.add("pending");
  } else if (ed.validating || ed.valid === null) {
    strip.textContent = "Validating…";
    strip.classList.add("pending");
  } else if (ed.valid) {
    strip.textContent = "✓ Valid — the manager's parse_workflow accepts this definition.";
    strip.classList.add("ok");
  } else {
    strip.textContent = ed.error || "Invalid definition.";
    strip.classList.add("err");
  }
  if (save) {
    save.disabled = ed.readOnly || ed.validating || ed.valid !== true
      || !!ed.rawError || !String(ed.name || "").trim();
  }
}

function wfUpdateShadowNote() {
  const ed = wfEd.editor;
  const note = document.getElementById("wf-ed-shadow-note");
  if (!ed || !note) return;
  const name = String(ed.name || "").trim();
  const info = wfEd.defs[name];
  const shadows = ed.shadowsShipped
    || (!!info && info.source === "shipped" && !ed.readOnly)
    || (!!info && info.shadows_shipped && !ed.readOnly);
  note.hidden = !shadows;
  note.textContent = shadows
    ? `Saving as '${name}' shadows the shipped definition of the same name ` +
      "(deleting the operator file later restores it)."
    : "";
}

function wfSetRawMode(raw) {
  const ed = wfEd.editor;
  if (!ed || ed.rawMode === raw) return;
  if (raw) {
    ed.rawText = JSON.stringify(wfBuildJson(ed), null, 2);
    ed.rawError = null;
    ed.rawMode = true;
  } else {
    // Parse errors in the raw view block switching back (spec §5).
    let parsed;
    try {
      parsed = JSON.parse(ed.rawText);
    } catch (e) {
      ed.rawError = `JSON parse error: ${e.message}`;
      ed.valid = false;
      ed.error = ed.rawError;
      wfUpdateValidationStrip();
      return;
    }
    ed.rawError = null;
    if (typeof parsed.name === "string" && ed.nameEditable) ed.name = parsed.name;
    ed.steps = (Array.isArray(parsed.steps) ? parsed.steps : []).map(wfStepFromJson);
    ed.rawMode = false;
  }
  renderWorkflowEditor();
  wfScheduleValidate(0);
}

async function wfSaveDefinition() {
  const ed = wfEd.editor;
  if (!ed || ed.readOnly) return;
  let candidate;
  if (ed.rawMode) {
    try {
      candidate = JSON.parse(ed.rawText);
    } catch (e) {
      ed.rawError = `JSON parse error: ${e.message}`;
      wfUpdateValidationStrip();
      return;
    }
  } else {
    candidate = wfBuildJson(ed);
  }
  const name = String(candidate.name || "").trim();
  candidate.name = name;
  const { ok, data } = await sendJSON(
    `/api/workflows/${encodeURIComponent(name)}`, "PUT", candidate,
  );
  if (!ok) {
    ed.valid = false;
    ed.error = (data && data.error) || "save failed";
    ed.errorStep = null;
    wfUpdateValidationStrip();
    return;
  }
  ed.dirty = false;
  ed.isNew = false;
  ed.nameEditable = false;
  ed.name = name;
  ed.source = "operator";
  ed.shadowsShipped = !!(data && data.shadows_shipped);
  await wfLoadLists();
  if (ed.shadowsShipped && !ed.shippedDefinition) {
    try {
      const single = await getJSON(`/api/workflows/${encodeURIComponent(name)}`);
      ed.shippedDefinition = single.shipped_definition;
    } catch { /* provenance detail only */ }
  }
  renderWorkflowEditor();
  const strip = $("wf-ed-validation");
  if (strip) {
    strip.textContent = "✓ Saved — the definition hot-reloaded; the next dispatch sees it.";
    strip.classList.add("ok");
  }
}

// --------------------------------------------------------------------------
// Graph preview — render-only SVG (spec §5): layered left-to-right in list
// order, solid arrows for next, dashed labeled arrows for signals, a distinct
// $end terminal, back-edges curved + badged with the target's max_visits.
// --------------------------------------------------------------------------

const WF_SVG_NS = "http://www.w3.org/2000/svg";

function _svgEl(tag, attrs = {}) {
  const el = document.createElementNS(WF_SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

function wfRenderGraph() {
  const ed = wfEd.editor;
  const host = document.getElementById("wf-ed-graph");
  if (!ed || !host) return;
  host.innerHTML = "";

  const steps = ed.rawMode
    ? (() => {
        try {
          const parsed = JSON.parse(ed.rawText);
          return (Array.isArray(parsed.steps) ? parsed.steps : []).map(wfStepFromJson);
        } catch { return null; }
      })()
    : ed.steps;
  if (steps === null) {
    const note = document.createElement("div");
    note.className = "wf-graph-note";
    note.textContent = "Graph unavailable while the JSON does not parse.";
    host.append(note);
    return;
  }

  const nodeW = 96, nodeH = 30, gapX = 44;
  const laneY = 120;
  const n = steps.length;
  const width = (n + 1) * (nodeW + gapX) + 20;
  const height = 240;
  const svg = _svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    class: "wf-graph-svg",
    preserveAspectRatio: "xMinYMid meet",
  });

  // Arrowhead markers (normal + dashed-signal tint via CSS classes).
  const defs = _svgEl("defs");
  for (const id of ["wf-arrow", "wf-arrow-signal", "wf-arrow-back"]) {
    const marker = _svgEl("marker", {
      id, viewBox: "0 0 10 10", refX: 9, refY: 5,
      markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse",
    });
    marker.append(_svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", class: `${id}-head` }));
    defs.append(marker);
  }
  svg.append(defs);

  const xOf = (i) => 10 + i * (nodeW + gapX);
  const centerOf = (i) => ({ x: xOf(i) + nodeW / 2, y: laneY });
  const ids = steps.map((s) => s.id.trim());
  const indexOf = (id) => (id === WF_END ? n : ids.indexOf(id));
  const visitsOf = (id) => {
    const s = steps[ids.indexOf(id)];
    return s && String(s.maxVisits).trim() !== "" ? String(s.maxVisits) : "?";
  };

  // Edges first (under the nodes).
  const edgeLayer = _svgEl("g");
  svg.append(edgeLayer);

  const drawEdge = (fromI, toI, { signal = null } = {}) => {
    if (toI < 0) return; // dangling id: the validator reports it; skip drawing
    const a = centerOf(fromI), b = centerOf(toI);
    const back = toI <= fromI;
    const cls = back ? "wf-edge back" : signal ? "wf-edge signal" : "wf-edge next";
    const marker = back ? "wf-arrow-back" : signal ? "wf-arrow-signal" : "wf-arrow";
    let d;
    let labelPos;
    if (!back && toI === fromI + 1 && !signal) {
      // Adjacent default hop: a straight line on the lane.
      d = `M ${a.x + nodeW / 2} ${a.y} L ${b.x - nodeW / 2} ${a.y}`;
      labelPos = { x: (a.x + b.x) / 2, y: a.y - 8 };
    } else {
      // Curved arc: forward signal hops dip below the lane; back-edges rise
      // above it, higher the longer the jump.
      const span = Math.abs(toI - fromI);
      const lift = (back ? -1 : 1) * (28 + span * 16);
      const y0 = back ? a.y - nodeH / 2 : a.y + nodeH / 2;
      const y1 = back ? b.y - nodeH / 2 : b.y + nodeH / 2;
      d = `M ${a.x} ${y0} C ${a.x} ${y0 + lift}, ${b.x} ${y1 + lift}, ${b.x} ${y1}`;
      labelPos = { x: (a.x + b.x) / 2, y: (back ? a.y - nodeH / 2 : a.y + nodeH / 2) + lift * 0.78 };
    }
    edgeLayer.append(_svgEl("path", {
      d, class: cls, fill: "none", "marker-end": `url(#${marker})`,
    }));
    if (signal && !back) {
      const label = _svgEl("text", { x: labelPos.x, y: labelPos.y, class: "wf-edge-label" });
      label.textContent = signal;
      edgeLayer.append(label);
    }
    if (back) {
      // Badge the back-edge with the *target's* max_visits budget.
      const badge = _svgEl("text", {
        x: labelPos.x, y: labelPos.y - 4, class: "wf-edge-label back-badge",
      });
      badge.textContent = `${signal ? signal + " · " : ""}≤${visitsOf(ids[toI])} visits`;
      edgeLayer.append(badge);
    }
  };

  steps.forEach((s, i) => {
    for (const row of s.signals) {
      if (!row.token.trim() || !row.dest) continue;
      drawEdge(i, indexOf(row.dest), { signal: row.token.trim() });
    }
    const nxt = s.kind === "split"
      ? WF_END
      : (s.next || (i + 1 < n ? ids[i + 1] || WF_END : WF_END));
    drawEdge(i, indexOf(nxt));
  });

  // Nodes: the step chips the queue badge already uses (kind styling), plus a
  // distinct $end terminal.
  steps.forEach((s, i) => {
    const g = _svgEl("g", { class: `wf-node kind-${s.kind}`, "data-step-id": s.id });
    g.append(_svgEl("rect", {
      x: xOf(i), y: laneY - nodeH / 2, width: nodeW, height: nodeH,
      rx: 8, class: "wf-node-rect",
    }));
    const label = _svgEl("text", {
      x: xOf(i) + nodeW / 2, y: laneY + 4, class: "wf-node-label",
    });
    label.textContent = s.id.trim() || `step ${i + 1}`;
    g.append(label);
    const kindTag = _svgEl("text", {
      x: xOf(i) + nodeW / 2, y: laneY + nodeH / 2 + 14, class: "wf-node-kind",
    });
    kindTag.textContent = s.kind + (String(s.maxVisits).trim() !== "" ? ` · ≤${s.maxVisits}` : "");
    g.append(kindTag);
    // The graph is a view, never an input surface — a click only focuses the
    // step's card.
    g.addEventListener("click", () => {
      const card = document.querySelector(
        `#wf-ed-cards .wf-card[data-step-index="${i}"]`,
      );
      if (card) {
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.classList.add("wf-card-flash");
        setTimeout(() => card.classList.remove("wf-card-flash"), 900);
      }
    });
    svg.append(g);
  });

  const endG = _svgEl("g", { class: "wf-node kind-end" });
  endG.append(_svgEl("circle", {
    cx: xOf(n) + nodeW / 2, cy: laneY, r: 18, class: "wf-node-end",
  }));
  const endLabel = _svgEl("text", {
    x: xOf(n) + nodeW / 2, y: laneY + 4, class: "wf-node-label end",
  });
  endLabel.textContent = "$end";
  endG.append(endLabel);
  svg.append(endG);

  host.append(svg);
}

// --------------------------------------------------------------------------
// Prompt editor (spec §5.1)
// --------------------------------------------------------------------------

async function openPromptEditor(name, { duplicate = false, returnTo = null } = {}) {
  const from = returnTo
    || (state.view === "prompt-editor" ? wfEd.prompt && wfEd.prompt.returnView : state.view)
    || "workflows";
  let model;
  if (name) {
    let data;
    try {
      data = await getJSON(`/api/workflow-prompts/${encodeURIComponent(name)}`);
    } catch {
      window.alert(`Could not load prompt '${name}'.`);
      return;
    }
    model = {
      name: duplicate ? name.replace(/\.md$/, "") + "-copy.md" : data.name,
      nameEditable: duplicate,
      isNew: duplicate,
      text: data.text,
      source: duplicate ? "operator" : data.source,
      shadowsShipped: !duplicate && data.shadows_shipped,
      shippedBody: duplicate ? null : data.shipped_body,
      readOnly: !duplicate && data.source === "shipped",
    };
  } else {
    model = {
      name: "", nameEditable: true, isNew: true,
      text: "", source: "operator", shadowsShipped: false,
      shippedBody: null, readOnly: false,
    };
  }
  model.returnView = from;
  model.dirty = false;
  wfEd.prompt = model;
  setView("prompt-editor");
}

function renderPromptEditor() {
  const pm = wfEd.prompt;
  const body = $("prompt-editor-body");
  if (!body) return;
  if (!pm) { setView("workflows"); return; }
  $("prompt-editor-title").textContent = pm.isNew && !pm.name ? "New prompt" : pm.name;
  body.innerHTML = "";

  const head = document.createElement("div");
  head.className = "wf-ed-head";
  if (pm.nameEditable) {
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "wf-ed-name";
    nameInput.placeholder = "my-charter.md";
    nameInput.value = pm.name;
    nameInput.addEventListener("input", () => {
      pm.name = nameInput.value;
      pm.dirty = true;
      wfPromptRefreshBar();
    });
    head.append(nameInput);
  } else {
    const nameEl = document.createElement("span");
    nameEl.className = "wf-ed-name-static";
    nameEl.textContent = pm.name;
    head.append(nameEl);
  }
  head.append(wfProvenanceBadge({ source: pm.source, shadows_shipped: pm.shadowsShipped }));
  const spacer = document.createElement("span");
  spacer.className = "wf-ed-spacer";
  head.append(spacer);
  if (pm.readOnly) {
    const dup = document.createElement("button");
    dup.type = "button";
    dup.className = "ghost-btn";
    dup.textContent = "Duplicate to edit";
    dup.addEventListener("click", () => openPromptEditor(pm.name, {
      duplicate: true, returnTo: pm.returnView,
    }));
    head.append(dup);
  } else if (!pm.isNew) {
    const del = document.createElement("button");
    del.type = "button";
    del.className = "ghost-btn danger";
    del.textContent = pm.shadowsShipped ? "Delete (restores shipped)" : "Delete";
    del.addEventListener("click", async () => {
      const before = pm.name;
      await wfDeletePrompt(before);
      if (!(wfEd.prompts || {})[before] || wfEd.prompts[before].source === "shipped") {
        wfEd.prompt = null;
        setView(pm.returnView);
      }
    });
    head.append(del);
  }
  body.append(head);

  // The charter discipline callout (workflows spec §8.2): byte-stable body,
  // no task-varying content — the header injects paths and variables.
  const callout = document.createElement("div");
  callout.className = "wf-prompt-callout";
  callout.textContent =
    "Charter discipline: keep the body byte-stable across runs — no task-varying " +
    "content. The engine injects the task file, artifact paths, and $OUTPUT_FILE " +
    "in a header above this body, so implicit prompt-caching keeps hitting.";
  body.append(callout);

  if (pm.readOnly) {
    const note = document.createElement("div");
    note.className = "wf-ed-readonly-note";
    note.textContent = "Shipped prompts are read-only package assets. Duplicate to edit.";
    body.append(note);
  }

  const split = document.createElement("div");
  split.className = "wf-prompt-split";
  const ta = document.createElement("textarea");
  ta.className = "wf-prompt-source";
  ta.spellcheck = false;
  ta.readOnly = pm.readOnly;
  ta.placeholder = "# Charter\n\nWrite the doc-step charter in markdown…";
  ta.value = pm.text;
  const preview = document.createElement("div");
  preview.className = "wf-prompt-preview markdown-body";
  preview.innerHTML = renderMarkdown(pm.text || "");
  let previewTimer = null;
  ta.addEventListener("input", () => {
    pm.text = ta.value;
    pm.dirty = true;
    wfPromptRefreshBar();
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(() => {
      preview.innerHTML = renderMarkdown(pm.text || "");
    }, 200);
  });
  split.append(ta, preview);
  body.append(split);

  const bar = document.createElement("div");
  bar.className = "wf-ed-savebar";
  const status = document.createElement("span");
  status.id = "wf-prompt-status";
  status.className = "wf-prompt-status";
  const save = document.createElement("button");
  save.id = "wf-prompt-save";
  save.type = "button";
  save.className = "btn primary";
  save.textContent = "Save";
  save.addEventListener("click", wfSavePrompt);
  bar.append(status, save);
  body.append(bar);
  wfPromptRefreshBar();
}

function wfPromptRefreshBar() {
  const pm = wfEd.prompt;
  const save = document.getElementById("wf-prompt-save");
  const status = document.getElementById("wf-prompt-status");
  if (!pm || !save) return;
  const name = String(pm.name || "").trim();
  save.disabled = pm.readOnly || !name || !String(pm.text || "").trim();
  if (status) {
    const target = name.endsWith(".md") ? name : (name ? `${name}.md` : "");
    const shadows = !pm.readOnly && !!target && !!(wfEd.prompts || {})[target]
      && wfEd.prompts[target].source === "shipped";
    status.textContent = shadows
      ? `Saving shadows the shipped prompt '${target}'.` : "";
  }
}

async function wfSavePrompt() {
  const pm = wfEd.prompt;
  if (!pm || pm.readOnly) return;
  const name = String(pm.name || "").trim();
  const { ok, data } = await sendJSON(
    `/api/workflow-prompts/${encodeURIComponent(name)}`, "PUT", { text: pm.text },
  );
  if (!ok) {
    window.alert(`Save failed: ${(data && data.error) || "unknown error"}`);
    return;
  }
  pm.dirty = false;
  pm.isNew = false;
  pm.nameEditable = false;
  pm.name = (data && data.name) || name;
  pm.source = "operator";
  pm.shadowsShipped = !!(data && data.shadows_shipped);
  await wfLoadLists();
  renderPromptEditor();
  const status = document.getElementById("wf-prompt-status");
  if (status) status.textContent = "✓ Saved — the next doc-step dispatch reads it.";
}

// --------------------------------------------------------------------------
// Wiring
// --------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  wfEnsureRoleDatalist();
  const newBtn = $("btn-new-workflow");
  if (newBtn) newBtn.addEventListener("click", () => openWorkflowEditor(null));
  const edBack = $("wf-editor-back");
  if (edBack) edBack.addEventListener("click", () => {
    const ed = wfEd.editor;
    if (ed && ed.dirty && !window.confirm("Discard unsaved workflow edits?")) return;
    wfEd.editor = null;
    setView(wfEd.returnView === "workflow-editor" ? "workflows" : wfEd.returnView || "workflows");
  });
  const prBack = $("prompt-editor-back");
  if (prBack) prBack.addEventListener("click", () => {
    const pm = wfEd.prompt;
    if (pm && pm.dirty && !window.confirm("Discard unsaved prompt edits?")) return;
    const dest = (pm && pm.returnView) || "workflows";
    wfEd.prompt = null;
    setView(dest);
  });
  // Prime the library lists so the first open is instant.
  wfLoadLists();
});
