// ARPG React — Rule Editor (vanilla JS, no build step)
//
// Talks to the FastAPI backend on the same origin. Auth is a session
// cookie set by /api/login; redirect to / when missing.

// Active game from the URL — every API call carries it. Default d4 so
// hand-crafted /editor URLs without a game still work.
const GAME = (() => {
  const g = new URLSearchParams(window.location.search).get("game") || "d4";
  return ["d4", "poe2"].includes(g) ? g : "d4";
})();

// Slot set per game — determines what shows up in the Skills tab and
// what dropdowns offer for SLOT_STATE_IS conditions / combo step targets.
const HOTKEYS_BY_GAME = {
  d4:   ["1", "2", "3", "4", "L", "R"],
  poe2: ["LMB", "MMB", "RMB", "Q", "E", "R", "T", "F"],
};
const HOTKEYS = HOTKEYS_BY_GAME[GAME] || HOTKEYS_BY_GAME.d4;

// Class roster per game. D4 is a flat list. POE2 is base-class -> ascendancies;
// the dropdown renders the base class as a selectable parent and each
// ascendancy indented underneath. Stored value is the leaf identifier
// ("warrior", "warrior_titan", etc.) so existing D4 builds keep working.
const CLASSES_BY_GAME = {
  d4: [
    { value: "barbarian",  label: "Barbarian" },
    { value: "druid",      label: "Druid" },
    { value: "necromancer", label: "Necromancer" },
    { value: "paladin",    label: "Paladin" },
    { value: "rogue",      label: "Rogue" },
    { value: "sorcerer",   label: "Sorcerer" },
    { value: "spiritborn", label: "Spiritborn" },
    { value: "warlock",    label: "Warlock" },
  ],
  poe2: [
    { base: "warrior",   label: "Warrior",   ascendancies: [
      { value: "warrior_titan",          label: "Titan" },
      { value: "warrior_warbringer",     label: "Warbringer" },
      { value: "warrior_smith_of_kitava", label: "Smith of Kitava" },
    ]},
    { base: "monk",      label: "Monk",      ascendancies: [
      { value: "monk_invoker",           label: "Invoker" },
      { value: "monk_acolyte_of_chayula", label: "Acolyte of Chayula" },
      { value: "monk_martial_artist",    label: "Martial Artist" },
    ]},
    { base: "mercenary", label: "Mercenary", ascendancies: [
      { value: "mercenary_witchhunter",          label: "Witchhunter" },
      { value: "mercenary_gemling_legionnaire",  label: "Gemling Legionnaire" },
      { value: "mercenary_tactician",            label: "Tactician" },
    ]},
    { base: "ranger",    label: "Ranger",    ascendancies: [
      { value: "ranger_deadeye",    label: "Deadeye" },
      { value: "ranger_pathfinder", label: "Pathfinder" },
    ]},
    { base: "sorceress", label: "Sorceress", ascendancies: [
      { value: "sorceress_stormweaver",          label: "Stormweaver" },
      { value: "sorceress_chronomancer",         label: "Chronomancer" },
      { value: "sorceress_disciple_of_varashta", label: "Disciple of Varashta" },
    ]},
    { base: "witch",     label: "Witch",     ascendancies: [
      { value: "witch_blood_mage",  label: "Blood Mage" },
      { value: "witch_infernalist", label: "Infernalist" },
      { value: "witch_lich",        label: "Lich" },
    ]},
    { base: "huntress",  label: "Huntress",  ascendancies: [
      { value: "huntress_amazon",       label: "Amazon" },
      { value: "huntress_ritualist",    label: "Ritualist" },
      { value: "huntress_spirit_walker", label: "Spirit Walker" },
    ]},
    { base: "druid",     label: "Druid",     ascendancies: [
      { value: "druid_oracle", label: "Oracle" },
      { value: "druid_shaman", label: "Shaman" },
    ]},
  ],
};

function populateClassDropdown() {
  const sel = document.getElementById("b_class");
  if (!sel) return;
  // Preserve current selection across re-renders.
  const prev = sel.value;
  // Wipe everything except the leading "— none —" option.
  while (sel.options.length > 1) sel.remove(1);
  const indent = "    ↳ ";
  const entries = CLASSES_BY_GAME[GAME] || CLASSES_BY_GAME.d4;
  entries.forEach(entry => {
    if (entry.ascendancies) {
      const baseOpt = document.createElement("option");
      baseOpt.value = entry.base;
      baseOpt.textContent = entry.label;
      sel.appendChild(baseOpt);
      entry.ascendancies.forEach(a => {
        const opt = document.createElement("option");
        opt.value = a.value;
        opt.textContent = indent + a.label;
        sel.appendChild(opt);
      });
    } else {
      const opt = document.createElement("option");
      opt.value = entry.value;
      opt.textContent = entry.label;
      sel.appendChild(opt);
    }
  });
  if (prev) sel.value = prev;
}

const CAST_TYPES = [
  { id: "SINGLE", label: "Single (one-shot when conditions met)" },
  { id: "INTERVAL", label: "Interval (periodic spam)" },
  { id: "CONDITIONAL", label: "Conditional (fire on first frame conditions are met)" },
  { id: "COMBO", label: "Combo (chain of presses)" },
  { id: "CAST_X_AND_WAIT", label: "Cast X times + wait for green clear" },
];

const COND_TYPES = [
  { id: "HEALTH_BELOW", label: "health below %" },
  { id: "HEALTH_ABOVE", label: "health above %" },
  { id: "RESOURCE_LEFT_BELOW", label: "resource left below %" },
  { id: "RESOURCE_LEFT_ABOVE", label: "resource left above %" },
  { id: "RESOURCE_RIGHT_BELOW", label: "resource right below %" },
  { id: "RESOURCE_RIGHT_ABOVE", label: "resource right above %" },
  { id: "SLOT_STATE_IS", label: "slot state is" },
  { id: "SLOT_STATE_IS_NOT", label: "slot state is not" },
  { id: "BOSS_DETECTED", label: "boss detected" },
];

const SLOT_STATES = ["READY", "ACTIVE_READY", "IN_USE", "COOLDOWN", "DISABLED"];

const WAIT_MODES = [
  { id: "WAIT_FOR_ANY_READY", label: "any step ready" },
  { id: "WAIT_FOR_ALL_READY", label: "all steps ready" },
  { id: "FIRE_NOW_REGARDLESS", label: "fire regardless" },
];

const LAST_KEY = "sanctum-signal.last_build";

// ------------------------------------------------------------- API client

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  // Always carry the active game on the query string — backend filters
  // builds + keymaps to (current_user, GAME).
  const url = path + (path.includes("?") ? "&" : "?") + "game=" + encodeURIComponent(GAME);
  const res = await fetch(url, opts);
  if (res.status === 401) {
    // Session expired/missing — back to login.
    window.location.href = "/";
    throw new Error("auth required");
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${method} ${path} → ${res.status}: ${text}`);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

const API = {
  list:   ()        => api("GET",    "/api/builds"),
  get:    (name)    => api("GET",    `/api/builds/${encodeURIComponent(name)}`),
  put:    (name, b) => api("PUT",    `/api/builds/${encodeURIComponent(name)}`, b),
  del:    (name)    => api("DELETE", `/api/builds/${encodeURIComponent(name)}`),
  rename: (old, n)  => api("POST",   `/api/builds/${encodeURIComponent(old)}/rename`, { new_name: n }),
  getProfile: ()    => api("GET",    "/api/profile"),
  putProfile: (p)   => api("PUT",    "/api/profile", p),
};

// ------------------------------------------------------------------ state

function emptyBuild(name = "new_build") {
  return {
    name,
    description: "",
    class_name: null,
    build_url: null,
    default_jitter_pct: 17.0,
    slot_monitors: HOTKEYS.reduce((acc, hk) => {
      acc[hk] = { enabled: false, pixel_x: 0, pixel_y: 0, good_color: [0, 0, 0], color_tolerance: 30 };
      return acc;
    }, {}),
    resource_monitors: [
      { name: "HEALTH",         enabled: false, sample_x: 900,  sample_y_top: 1295, sample_y_bottom: 1395, saturation_threshold: 0.30 },
      { name: "RESOURCE_LEFT",  enabled: false, sample_x: 1685, sample_y_top: 1280, sample_y_bottom: 1380, saturation_threshold: 0.30 },
      { name: "RESOURCE_RIGHT", enabled: false, sample_x: 1725, sample_y_top: 1280, sample_y_bottom: 1380, saturation_threshold: 0.30 },
    ],
    skill_timings: HOTKEYS.reduce((acc, hk) => {
      acc[hk] = { cast_ms: 0, recast_ms: 0, active_ms: 0 };
      return acc;
    }, {}),
    rules: [],
    potion: { enabled: false, hotkey: "Q", trigger_health_below: 0.5, cooldown_seconds: 30 },
  };
}

function emptyRule() {
  return {
    name: "new rule",
    target: "1",
    cast_type: "CONDITIONAL",
    enabled: true,
    jitter_pct: null,
    interval_ms: 1000,
    cast_count: 1,
    wait_for_green_clear: false,
    wait_mode: "WAIT_FOR_ALL_READY",
    inter_step_delay_ms: 80,
    conditions: [],
    combo_steps: [],
    press_delay_ms: 80,
    cooldown_seconds: 5.0,
  };
}

let buildList = [];     // [{name, updated_at}, ...]
let activeName = null;
let active = null;       // currently-loaded build object
let dirty = false;

// ----------------------------------------------------------- helpers

function $(id) { return document.getElementById(id); }
function el(tag, props = {}, children = []) {
  const e = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "html") e.innerHTML = v;
    else e.setAttribute(k, v);
  });
  children.forEach(c => e.appendChild(typeof c === "string" ? document.createTextNode(c) : c));
  return e;
}
function setStatus(cls, text) {
  const s = document.querySelector(".status");
  s.className = "status " + cls;
  $("statusText").textContent = text;
}
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 1500);
}
function escAttr(s) { return String(s ?? "").replace(/"/g, "&quot;"); }
function setDirty(v = true) {
  dirty = v;
  setStatus(v ? "dirty" : "saved", v ? "unsaved changes" : "saved");
}

// ----------------------------------------------------------- rendering

async function refreshBuildList() {
  const data = await API.list();
  buildList = data.builds || [];
  renderBuildPicker();
}

async function loadActive(name) {
  if (!name) { active = null; activeName = null; render(); return; }
  active = await API.get(name);
  activeName = name;
  localStorage.setItem(LAST_KEY, name);
  setDirty(false);
  render();
}

function render() {
  renderBuildPicker();
  // Profile tab is build-independent — never gated by active build.
  const buildTabs = '.tabpanel:not([data-tab="profile"]) input, .tabpanel:not([data-tab="profile"]) select';
  if (!active) {
    document.querySelectorAll(buildTabs).forEach(e => e.disabled = true);
    return;
  }
  document.querySelectorAll(buildTabs).forEach(e => e.disabled = false);
  renderBuildTab(active);
  renderSkillsTab(active);
  renderRulesTab(active);
  renderPotionTab(active);
}

function renderBuildPicker() {
  const sel = $("buildPicker");
  sel.innerHTML = "";
  buildList.forEach(b => sel.appendChild(el("option", { value: b.name }, [b.name])));
  if (activeName) sel.value = activeName;
}

function renderBuildTab(b) {
  $("b_name").value = b.name;
  $("b_class").value = b.class_name || "";
  $("b_description").value = b.description || "";
  $("b_url").value = b.build_url || "";
  $("b_jitter").value = b.default_jitter_pct;
}

function renderSkillsTab(b) {
  // Make sure the build has a skill_timings dict — backwards-compat for
  // older builds saved without this field.
  if (!b.skill_timings) b.skill_timings = {};

  const c = $("skillsList");
  c.innerHTML = "";
  HOTKEYS.forEach(hk => {
    const t = b.skill_timings[hk] || { cast_ms: 0, recast_ms: 0, active_ms: 0 };
    const row = el("div", { class: "skill-row" });
    row.innerHTML = `
      <div class="head">
        <div class="key-cap">${hk}</div>
        <div class="meta">${t.cast_ms === 0 && t.recast_ms === 0 && t.active_ms === 0 ? "instant (no timing)" : ""}</div>
      </div>
      <div class="grid three">
        <label>cast (ms)<input type="number" min="0" step="10" data-hk="${hk}" data-field="cast_ms" value="${t.cast_ms}"></label>
        <label>recast (ms)<input type="number" min="0" step="10" data-hk="${hk}" data-field="recast_ms" value="${t.recast_ms}"></label>
        <label>active (ms)<input type="number" min="0" step="10" data-hk="${hk}" data-field="active_ms" value="${t.active_ms}"></label>
      </div>
    `;
    row.querySelectorAll("input").forEach(inp => {
      inp.addEventListener("change", e => {
        const hk = e.target.dataset.hk;
        const field = e.target.dataset.field;
        const v = Math.max(0, parseInt(e.target.value, 10) || 0);
        if (!b.skill_timings[hk]) b.skill_timings[hk] = { cast_ms: 0, recast_ms: 0, active_ms: 0 };
        b.skill_timings[hk][field] = v;
        setDirty(); renderSkillsTab(b);
      });
    });
    c.appendChild(row);
  });
}

function renderRulesTab(b) {
  const c = $("rulesList");
  c.innerHTML = "";
  b.rules.forEach((r, i) => c.appendChild(renderRule(b, r, i)));
}

function renderRule(b, r, idx) {
  const wrap = el("div", { class: "rule", draggable: "true" });
  wrap.dataset.idx = String(idx);
  if (r._expanded) wrap.classList.add("expanded");

  // Drag-reorder events
  wrap.addEventListener("dragstart", e => {
    wrap.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(idx));
  });
  wrap.addEventListener("dragend", () => wrap.classList.remove("dragging"));
  wrap.addEventListener("dragover", e => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    wrap.classList.add("drop-target");
  });
  wrap.addEventListener("dragleave", () => wrap.classList.remove("drop-target"));
  wrap.addEventListener("drop", e => {
    e.preventDefault();
    wrap.classList.remove("drop-target");
    const fromIdx = Number(e.dataTransfer.getData("text/plain"));
    const toIdx = Number(wrap.dataset.idx);
    if (Number.isFinite(fromIdx) && fromIdx !== toIdx) {
      const moved = b.rules.splice(fromIdx, 1)[0];
      b.rules.splice(toIdx, 0, moved);
      setDirty(); render();
    }
  });

  // head
  const head = el("div", { class: "head" });
  head.innerHTML = `
    <span class="drag-handle" title="drag to reorder">☰</span>
    <span class="target-pill">${r.target}</span>
    <span class="badge">${r.cast_type}</span>
    <span class="name">${r.name || "(unnamed)"}</span>
    <button class="toggle-expand" data-act="toggle">${r._expanded ? "−" : "+"}</button>
    <button class="delete-btn" data-act="del">DEL</button>
  `;
  head.querySelector('[data-act=toggle]').onclick = () => { r._expanded = !r._expanded; render(); };
  head.querySelector('[data-act=del]').onclick = () => { b.rules.splice(idx, 1); setDirty(); render(); };
  wrap.appendChild(head);

  // body
  const body = el("div", { class: "body" });
  body.innerHTML = `
    <div class="grid two">
      <label>Name <input type="text" data-f="name" value="${escAttr(r.name)}"></label>
      <label>Target hotkey
        <select data-f="target">
          ${HOTKEYS.map(h => `<option value="${h}" ${r.target===h?'selected':''}>${h}</option>`).join("")}
        </select>
      </label>
    </div>
    <div class="grid two">
      <label>Cast type
        <select data-f="cast_type">
          ${CAST_TYPES.map(t => `<option value="${t.id}" ${r.cast_type===t.id?'selected':''}>${t.label}</option>`).join("")}
        </select>
      </label>
      <label>Enabled
        <input type="checkbox" data-f="enabled" ${r.enabled?"checked":""} style="width:auto; transform: scale(1.5); margin-top:8px;">
      </label>
    </div>
    <div class="grid four">
      <label>jitter % <small style="color:var(--text-dim)">(blank = inherit)</small>
        <input type="number" data-f="jitter_pct" min="0" max="100" step="0.5" value="${r.jitter_pct ?? ''}" placeholder="${b.default_jitter_pct}">
      </label>
      <label>press delay ms
        <input type="number" data-f="press_delay_ms" min="0" max="500" value="${r.press_delay_ms}">
      </label>
      <label>cooldown sec
        <input type="number" data-f="cooldown_seconds" min="0" step="0.5" value="${r.cooldown_seconds}">
      </label>
    </div>
    <div class="type-specific"></div>
    <div class="conditions"><h4>Top-level conditions (all must be met)</h4></div>
  `;

  // type-specific
  const ts = body.querySelector(".type-specific");
  if (r.cast_type === "INTERVAL") {
    ts.innerHTML = `
      <div class="grid two">
        <label>interval ms
          <input type="number" data-f="interval_ms" min="50" max="60000" value="${r.interval_ms}">
        </label>
      </div>`;
  } else if (r.cast_type === "CAST_X_AND_WAIT") {
    ts.innerHTML = `
      <div class="grid two">
        <label>cast count
          <input type="number" data-f="cast_count" min="1" max="20" value="${r.cast_count}">
        </label>
        <label>wait for green clear
          <input type="checkbox" data-f="wait_for_green_clear" ${r.wait_for_green_clear?"checked":""} style="transform: scale(1.4);">
        </label>
      </div>`;
  } else if (r.cast_type === "COMBO") {
    ts.innerHTML = `
      <div class="grid two">
        <label>wait mode
          <select data-f="wait_mode">
            ${WAIT_MODES.map(w => `<option value="${w.id}" ${r.wait_mode===w.id?'selected':''}>${w.label}</option>`).join("")}
          </select>
        </label>
        <label>inter-step delay ms
          <input type="number" data-f="inter_step_delay_ms" min="0" max="1000" value="${r.inter_step_delay_ms}">
        </label>
      </div>
      <div class="combo-steps"><h4>Combo steps</h4><div class="steps"></div>
        <button class="add-mini" data-act="add-step">+ STEP</button>
      </div>`;
    const stepsBox = ts.querySelector(".steps");
    r.combo_steps.forEach((step, si) => stepsBox.appendChild(renderComboStep(r, step, si)));
    ts.querySelector('[data-act=add-step]').onclick = () => {
      r.combo_steps.push({ slot: "1", delay_ms: r.inter_step_delay_ms || 80, conditions: [] });
      setDirty(); render();
    };
  }

  // top-level conditions
  const condBox = body.querySelector(".conditions");
  r.conditions.forEach((c, ci) => condBox.appendChild(renderCondition(r.conditions, ci)));
  const addCond = el("button", { class: "add-mini" }, ["+ CONDITION"]);
  addCond.onclick = () => {
    // Default new conditions to SLOT_STATE_IS targeting this rule's own
    // slot in READY state — matches the most common pattern (fire when
    // the keypress's own skill is ready) and avoids the value=0.5
    // landmine on type-switches.
    r.conditions.push({ type: "SLOT_STATE_IS", target: r.target, value: "READY" });
    setDirty(); render();
  };
  condBox.appendChild(addCond);

  // wire body inputs
  body.querySelectorAll("[data-f]").forEach(inp => {
    if (inp.tagName === "DIV") return;
    inp.addEventListener("change", e => {
      const f = e.target.dataset.f;
      let v;
      if (e.target.type === "checkbox") v = e.target.checked;
      else if (e.target.type === "number") v = e.target.value === "" ? null : Number(e.target.value);
      else v = e.target.value;
      r[f] = v;
      setDirty();
      if (f === "cast_type") render();
    });
  });

  wrap.appendChild(body);
  return wrap;
}

function renderComboStep(rule, step, idx) {
  const row = el("div", { class: "combo-step-block" });
  step.conditions = step.conditions || [];

  const head = el("div", { class: "combo-step" });
  head.innerHTML = `
    <select data-f="slot">
      ${HOTKEYS.map(h => `<option value="${h}" ${step.slot===h?'selected':''}>${h}</option>`).join("")}
    </select>
    <input type="number" data-f="delay_ms" min="0" max="2000" value="${step.delay_ms}" placeholder="delay ms">
    <span style="color:var(--text-dim); font-size:11px;">delay ms</span>
    <button class="remove-step" data-act="del">×</button>
  `;
  head.querySelectorAll("[data-f]").forEach(inp => {
    inp.addEventListener("change", e => {
      const f = e.target.dataset.f;
      step[f] = e.target.type === "number" ? Number(e.target.value) : e.target.value;
      setDirty();
    });
  });
  head.querySelector('[data-act=del]').onclick = () => {
    rule.combo_steps.splice(idx, 1); setDirty(); render();
  };
  row.appendChild(head);

  // Per-step conditions
  const condBox = el("div", { class: "step-conditions" });
  condBox.innerHTML = `<h5>Step conditions <span class="muted">(skip step if any condition fails)</span></h5>`;
  step.conditions.forEach((c, ci) => condBox.appendChild(renderCondition(step.conditions, ci)));
  const addBtn = el("button", { class: "add-mini" }, ["+ CONDITION"]);
  addBtn.onclick = () => {
    step.conditions.push({ type: "SLOT_STATE_IS", target: step.slot, value: "READY" });
    setDirty(); render();
  };
  condBox.appendChild(addBtn);
  row.appendChild(condBox);

  return row;
}

function renderCondition(arr, idx) {
  const c = arr[idx];
  const usesSlot = c.type === "SLOT_STATE_IS" || c.type === "SLOT_STATE_IS_NOT";
  const usesNumeric = !usesSlot && c.type !== "BOSS_DETECTED";

  // Sanitize stale fields — bad data sneaks in when the user changes
  // condition type without re-touching target/value (e.g. left-over
  // value: 0.5 on a SLOT_STATE_IS condition). Self-heal on render so
  // the dropdown reflects valid data and a re-save cleans the JSON.
  if (usesSlot) {
    if (!HOTKEYS.includes(c.target)) { c.target = HOTKEYS[0]; setDirty(); }
    if (!SLOT_STATES.includes(c.value)) { c.value = "READY"; setDirty(); }
  } else if (usesNumeric) {
    if (typeof c.value !== "number") { c.value = 0.5; setDirty(); }
    c.target = null;
  } else {
    c.target = null;
    c.value = null;
  }

  const row = el("div", { class: "condition" });
  const slotOpts = HOTKEYS.map(h => `<option value="${h}" ${c.target===h?'selected':''}>${h}</option>`).join("");
  const stateOpts = SLOT_STATES.map(s => `<option value="${s}" ${c.value===s?'selected':''}>${s}</option>`).join("");
  row.innerHTML = `
    <select data-f="type">
      ${COND_TYPES.map(t => `<option value="${t.id}" ${c.type===t.id?'selected':''}>${t.label}</option>`).join("")}
    </select>
    ${usesSlot
      ? `<select data-f="target">${slotOpts}</select>
         <select data-f="value">${stateOpts}</select>`
      : (usesNumeric
          ? `<span></span><input type="number" data-f="value" min="0" max="1" step="0.05" value="${c.value ?? ''}">`
          : `<span></span><span></span>`)}
    <button class="remove-cond" data-act="del">×</button>
  `;
  row.querySelectorAll("[data-f]").forEach(inp => {
    inp.addEventListener("change", e => {
      const f = e.target.dataset.f;
      let v = e.target.value;
      if (e.target.type === "number") v = e.target.value === "" ? null : Number(e.target.value);
      c[f] = v;
      // Switching condition type — reset target/value so we don't leave
      // stale numeric/slot fields lying around (e.g. SLOT_STATE_IS with
      // target=null, value=0.5 from a previous HEALTH_BELOW selection).
      if (f === "type") {
        if (v === "SLOT_STATE_IS" || v === "SLOT_STATE_IS_NOT") {
          c.target = HOTKEYS[0];
          c.value = "READY";
        } else if (v === "BOSS_DETECTED") {
          c.target = null;
          c.value = null;
        } else {
          // resource/health threshold conditions
          c.target = null;
          c.value = 0.5;
        }
        render();
      }
      setDirty();
    });
  });
  row.querySelector('[data-act=del]').onclick = () => {
    arr.splice(idx, 1); setDirty(); render();
  };
  return row;
}

function renderPotionTab(b) {
  const p = b.potion;
  $("p_hotkey").value = p.hotkey;
  $("p_threshold").value = p.trigger_health_below;
  $("p_cooldown").value = p.cooldown_seconds;
  $("p_enabled").checked = p.enabled;
}

// ----------------------------------------------------------- events

document.querySelectorAll(".tab").forEach(t => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    document.querySelectorAll(".tabpanel").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    document.querySelector(`.tabpanel[data-tab="${t.dataset.tab}"]`).classList.add("active");
  });
});

$("buildPicker").addEventListener("change", async e => {
  if (dirty && !confirm("Switch builds and discard unsaved edits?")) {
    e.target.value = activeName;
    return;
  }
  await loadActive(e.target.value);
});

$("newBuildBtn").addEventListener("click", async () => {
  const name = prompt("New build name (e.g. warlock_dread_claws)");
  if (!name) return;
  if (buildList.some(b => b.name === name)) { alert("That name already exists."); return; }
  const seed = emptyBuild(name);
  await API.put(name, seed);
  await refreshBuildList();
  await loadActive(name);
  toast(`Created '${name}'`);
});

$("deleteBuildBtn").addEventListener("click", async () => {
  if (!activeName) return;
  if (!confirm(`Delete build "${activeName}"? Cannot be undone.`)) return;
  await API.del(activeName);
  await refreshBuildList();
  const next = buildList[0]?.name || null;
  await loadActive(next);
});

["b_name", "b_class", "b_description", "b_url", "b_jitter"].forEach(id => {
  $(id).addEventListener("change", async () => {
    const b = active; if (!b) return;
    if (id === "b_name") {
      const newName = $(id).value.trim();
      if (!newName || newName === b.name) return;
      try {
        await API.rename(b.name, newName);
        b.name = newName;
        activeName = newName;
        localStorage.setItem(LAST_KEY, newName);
        await refreshBuildList();
        setDirty(false);
        toast(`Renamed to '${newName}'`);
      } catch (err) {
        alert("Rename failed: " + err.message);
        $(id).value = b.name;
      }
      return;
    }
    if (id === "b_class") b.class_name = $(id).value || null;
    else if (id === "b_description") b.description = $(id).value;
    else if (id === "b_url") b.build_url = $(id).value || null;
    else if (id === "b_jitter") b.default_jitter_pct = Number($(id).value);
    setDirty();
  });
});

["p_hotkey", "p_threshold", "p_cooldown", "p_enabled"].forEach(id => {
  $(id).addEventListener("change", () => {
    const b = active; if (!b) return;
    const p = b.potion;
    if (id === "p_hotkey") p.hotkey = $(id).value;
    else if (id === "p_threshold") p.trigger_health_below = Number($(id).value);
    else if (id === "p_cooldown") p.cooldown_seconds = Number($(id).value);
    else if (id === "p_enabled") p.enabled = $(id).checked;
    setDirty();
  });
});

$("addRuleBtn").addEventListener("click", () => {
  if (!active) return;
  active.rules.push(emptyRule());
  setDirty(); render();
});

$("saveBtn").addEventListener("click", async () => {
  if (!active) return;
  const cleaned = JSON.parse(JSON.stringify(active, (k, v) => k === "_expanded" ? undefined : v));
  try {
    await API.put(active.name, cleaned);
    toast("Saved to server");
    setDirty(false);
  } catch (err) {
    alert("Save failed: " + err.message);
  }
});

$("exportBtn").addEventListener("click", () => {
  if (!active) return;
  const cleaned = JSON.parse(JSON.stringify(active, (k, v) => k === "_expanded" ? undefined : v));
  const blob = new Blob([JSON.stringify(cleaned, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: `${active.name}.json` });
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 200);
  toast(`Exported ${active.name}.json`);
});

$("importBtn").addEventListener("click", () => $("importFile").click());
$("importFile").addEventListener("change", async e => {
  const f = e.target.files[0]; if (!f) return;
  try {
    const text = await f.text();
    const obj = JSON.parse(text);
    if (!obj.name) throw new Error("missing 'name' in JSON");
    await API.put(obj.name, obj);
    await refreshBuildList();
    await loadActive(obj.name);
    toast(`Imported '${obj.name}'`);
  } catch (err) {
    alert("Import failed: " + err.message);
  }
  e.target.value = "";
});

// -------------------------------------------------- profile (per-user/game)
// Stored separately from any individual build. Drives daemon's keymap
// translation + future detector resolution scaling.

let profile = null;

function profileDefault() {
  const km = {};
  HOTKEYS.forEach(hk => { km[hk] = hk.toLowerCase(); });
  return {
    display: { screen_w: 2560, screen_h: 1440, ui_scale: 1.0 },
    keymap: km,
  };
}

function renderProfileTab() {
  if (!profile) profile = profileDefault();
  const d = profile.display || {};
  $("prof_screen_w").value = d.screen_w ?? "";
  $("prof_screen_h").value = d.screen_h ?? "";
  $("prof_ui_scale").value = d.ui_scale ?? "";

  const list = $("keymapList");
  list.innerHTML = "";
  HOTKEYS.forEach(hk => {
    const row = el("div", { class: "keymap-row" });
    row.appendChild(el("div", { class: "keymap-slot" }, [hk]));
    row.appendChild(el("div", { class: "keymap-arrow" }, ["→"]));
    const inp = el("input", {
      type: "text",
      class: "keymap-key",
      maxlength: "12",
      value: (profile.keymap && profile.keymap[hk]) || "",
      placeholder: hk.toLowerCase(),
    });
    inp.addEventListener("input", () => {
      profile.keymap[hk] = inp.value.trim();
    });
    row.appendChild(inp);
    list.appendChild(row);
  });
}

async function loadProfile() {
  try {
    const got = await API.getProfile();
    profile = {
      display: got.display || profileDefault().display,
      keymap: { ...profileDefault().keymap, ...(got.keymap || {}) },
    };
  } catch (_) {
    profile = profileDefault();
  }
  renderProfileTab();
}

async function saveProfile() {
  const sw = parseInt($("prof_screen_w").value, 10);
  const sh = parseInt($("prof_screen_h").value, 10);
  const ui = parseFloat($("prof_ui_scale").value);
  if (!Number.isFinite(sw) || !Number.isFinite(sh) || !Number.isFinite(ui)) {
    setProfStatus("Display fields must be numeric.", "warn");
    return;
  }
  profile.display = { screen_w: sw, screen_h: sh, ui_scale: ui };
  try {
    await API.putProfile(profile);
    setProfStatus("Saved.", "ok");
    toast("Profile saved");
  } catch (err) {
    setProfStatus("Save failed: " + err.message, "warn");
  }
}

function setProfStatus(msg, kind) {
  const s = $("prof_status");
  s.textContent = msg;
  s.className = kind === "ok" ? "ok" : kind === "warn" ? "warn" : "muted";
  if (kind === "ok") setTimeout(() => { s.textContent = ""; s.className = "muted"; }, 2000);
}

document.addEventListener("DOMContentLoaded", () => {
  const save = $("prof_save");
  if (save) save.addEventListener("click", saveProfile);
  const det = $("prof_autodetect");
  if (det) det.addEventListener("click", () => {
    // Browser-side detection: best we can do is the JS window. The user
    // is filling this in for the *game* window, which is usually the
    // monitor's native res — screen.width/height is a fine first guess.
    $("prof_screen_w").value = window.screen.width;
    $("prof_screen_h").value = window.screen.height;
    if (!$("prof_ui_scale").value) $("prof_ui_scale").value = "1.0";
    setProfStatus("Filled from screen — verify against your in-game settings.", "muted");
  });
});

// ----------------------------------------------------------- bootstrap

(async () => {
  try {
    // Header chrome: game tag, user tag, sign-out wiring.
    document.body.classList.add("game-" + GAME);
    const gameTag = document.getElementById("gameTag");
    if (gameTag) {
      gameTag.textContent = GAME.toUpperCase();
      gameTag.classList.add("game-" + GAME);
    }
    populateClassDropdown();
    await loadProfile();
    try {
      const me = await fetch("/api/me").then(r => r.ok ? r.json() : null);
      const userTag = document.getElementById("userTag");
      if (me && userTag) userTag.textContent = me.user;
    } catch (_) {}
    const logoutBtn = document.getElementById("logoutBtn");
    if (logoutBtn) {
      logoutBtn.addEventListener("click", async () => {
        await fetch("/api/logout", {method: "POST"});
        window.location.href = "/";
      });
    }

    await refreshBuildList();
    if (buildList.length === 0) {
      const seed = emptyBuild("untitled_build");
      await API.put(seed.name, seed);
      await refreshBuildList();
      await loadActive(seed.name);
    } else {
      const last = localStorage.getItem(LAST_KEY);
      const target = (last && buildList.some(b => b.name === last)) ? last : buildList[0].name;
      await loadActive(target);
    }
  } catch (err) {
    alert("Failed to load: " + err.message);
  }
})();
