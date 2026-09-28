// IntelDoc front-end: live run view (SSE) + dashboard. No build step.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};

const ICON = {
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M12 7v6M12 17.5v.01"/></svg>',
  dash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M7 12h10"/></svg>',
  spin: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M12 3a9 9 0 019 9"/></svg>',
  clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg>',
  stack: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linejoin="round"><path d="M12 4l8 4-8 4-8-4z"/><path d="M4 12l8 4 8-4M4 16l8 4 8-4"/></svg>',
};
const STATUS = {
  APPROVE: { label: "Approved", icon: ICON.check },
  REVIEW: { label: "Needs review", icon: ICON.alert },
  REJECT: { label: "Rejected", icon: ICON.x },
  FAILED: { label: "Failed", icon: ICON.dash },
  RUNNING: { label: "Running", icon: ICON.spin },
  QUEUED: { label: "Queued", icon: ICON.clock },
};
const pill = (status, extra = "") => {
  const s = STATUS[status] || STATUS.FAILED;
  return `<span class="pill ${status}">${s.icon}${s.label}${extra}</span>`;
};
const CURRENCY_SYM = { USD: "$", EUR: "€", GBP: "£", INR: "₹", JPY: "¥" };
const money = (v, cur) => {
  if (v === null || v === undefined || v === "" || v === "None") return "—";
  const n = Number(v);
  const s = n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return cur ? `${CURRENCY_SYM[cur] || cur + " "}${s}` : s;
};
const ago = (iso) => {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
};
const ms = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${n}ms`);
const toast = (msg) => {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 3500);
};
const docEmbed = (previewUrl, originalUrl) =>
  `<a href="${originalUrl}" target="_blank" rel="noopener" title="Open original"><img src="${previewUrl}" alt="Invoice document"></a>`;

// ------------------------------------------------------------------ tabs
$$(".tab").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
function showView(v) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach((s) => s.classList.toggle("active", s.id === `view-${v}`));
  if (v === "dashboard") loadDashboard();
  if (v === "master") loadMaster();
  if (v === "policy") loadPolicy();
}

// ------------------------------------------------------------------ config chip
api("/api/config").then((c) => {
  const host = new URL(c.endpoint).hostname.replace(/^api\./, "");
  $("#modelName").textContent = `${c.model} · ${host}${c.vision ? " · vision" : ""}`;
  if (!c.has_key) { $("#modelChip").classList.add("nokey"); $("#modelChip").title = "No API key configured - runs will fail at extraction"; }
});

// ------------------------------------------------------------------ sample library
let SAMPLES = [];
let lib = "scenarios";
api("/api/samples").then((s) => { SAMPLES = s; renderLib(); });
$$(".seg-btn").forEach((b) => b.addEventListener("click", () => {
  lib = b.dataset.lib;
  $$(".seg-btn").forEach((x) => x.classList.toggle("active", x === b));
  renderLib();
}));
function renderLib() {
  const items = SAMPLES.filter((s) => s.kind === lib);
  $("#libHint").textContent = lib === "scenarios"
    ? "Generated invoices. Order matters: duplicates and PO limits depend on what ran before."
    : "Real scanned invoices from the Hugging Face invoices-donut dataset (OCR + LLM).";
  $("#runAll").textContent = lib === "scenarios" ? `Run all ${items.length} in order` : "Run first 5 scans";
  $("#libList").innerHTML = items.length ? items.map((s) => `
    <div class="sample" data-id="${esc(s.id)}" title="Run ${esc(s.name)}">
      <div class="sample-name">${esc(s.name.replace(/\.(pdf|png)$/, "").replace(/_/g, " "))}</div>
      <div>${s.expected ? `<span class="pill expected">expect ${esc(s.expected.toLowerCase())}</span>` : ""}</div>
      <div class="sample-why">${esc(s.why)}</div>
    </div>`).join("") : `<div class="placeholder">No samples yet - run the fetch / generate scripts.</div>`;
  $$(".sample").forEach((el) => el.addEventListener("click", () => runSamples([el.dataset.id])));
}
$("#runAll").addEventListener("click", () => {
  const items = SAMPLES.filter((s) => s.kind === lib).map((s) => s.id);
  runSamples(lib === "scenarios" ? items : items.slice(0, 5));
});

// ------------------------------------------------------------------ inputs
const drop = $("#drop");
$("#fileInput").addEventListener("change", (e) => uploadFiles([...e.target.files]));
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));

async function uploadFiles(files) {
  if (!files.length) return;
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  try {
    const jobs = await api("/api/runs/upload", { method: "POST", body: fd });
    jobs.forEach((j, i) => addJob(j, URL.createObjectURL(files[i])));
  } catch (e) { toast(e.message); }
  $("#fileInput").value = "";
}
async function runSamples(ids) {
  try {
    const jobs = await api("/api/runs/samples", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ samples: ids }) });
    jobs.forEach((j, i) => addJob(j, `/api/samples/${ids[i]}`));
  } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------------ job queue
// One SSE connection at a time (the server runs jobs sequentially anyway,
// and browsers cap concurrent connections per host).
const JOBS = [];
let viewing = null;      // job shown in the live view
let followLatest = true; // auto-advance to the running job unless the user picked one
let streaming = null;

function addJob(j, docUrl) {
  const job = { id: j.job_id, name: j.name, docUrl, events: [], status: "QUEUED", started: null, ended: null, t0: null };
  JOBS.push(job);
  $("#queueCard").hidden = false;
  renderQueue();
  if (!viewing || followLatest && viewing.ended) show(job);
  pump();
}
function pump() {
  if (streaming) return;
  const next = JOBS.find((j) => !j.ended);
  if (!next) return;
  streaming = next;
  const es = new EventSource(`/api/jobs/${next.id}/events`);
  es.onmessage = (m) => {
    const e = JSON.parse(m.data);
    onEvent(next, e);
    if (e.type === "done" || e.type === "failed") { es.close(); streaming = null; pump(); }
  };
  es.onerror = () => { es.close(); streaming = null; setTimeout(pump, 1000); };
}
function onEvent(job, e) {
  job.events.push(e);
  if (e.type === "started") {
    job.status = "RUNNING"; job.started = performance.now(); job.t0 = e.ts;
    if (followLatest && viewing !== job && (!viewing || viewing.ended)) show(job);
  }
  if (e.type === "done" || e.type === "failed") {
    job.status = e.type === "done" ? e.outcome : "FAILED";
    job.ended = job.started + (e.ts - job.t0) * 1000; job.runId = e.run_id;
  }
  if (viewing === job) apply(e);
  renderQueue();
  if (job.ended && $("#view-dashboard").classList.contains("active")) loadDashboard();
}
function renderQueue() {
  $("#queueList").innerHTML = JOBS.slice().reverse().map((j) => `
    <li data-id="${j.id}" class="${viewing === j ? "active" : ""}">${pill(j.status)}<span class="qname">${esc(j.name)}</span></li>`).join("");
  $$("#queueList li").forEach((li) => li.addEventListener("click", () => {
    const j = JOBS.find((x) => x.id === li.dataset.id);
    followLatest = false; show(j);
  }));
}

// ------------------------------------------------------------------ live view
const STAGES = [
  { key: "extract", label: "Extract" },
  { key: "transform", label: "Transform" },
  { key: "validate", label: "Validate" },
  { key: "decide", label: "Decide" },
  { key: "load", label: "Load" },
];
const SUB_LABEL = {
  load_document: "Read file", ocr: "OCR (local)", llm_extraction: "LLM extraction",
  normalise: "Normalise values", rules: "Business rules", policy: "Apply policy", persist: "Save to Postgres",
};
const EXPECTED_SUBS = { extract: ["load_document", "llm_extraction"], transform: ["normalise"], validate: ["rules"], decide: ["policy"], load: ["persist"] };
let timerHandle = null;

function show(job) {
  viewing = job;
  $("#liveEmpty").hidden = true;
  $("#liveRun").hidden = false;
  $("#runFile").textContent = job.name;
  $("#runMeta").textContent = "Queued…";
  $("#stages").innerHTML = STAGES.map((s) => `
    <li class="stage" data-stage="${s.key}">
      <div class="stage-top"><span class="stage-icon">${ICON.dash}</span><span class="stage-name">${s.label}</span><span class="stage-time"></span></div>
      <ul class="stage-subs">${EXPECTED_SUBS[s.key].map((n) => `<li data-sub="${n}"><span>${SUB_LABEL[n]}</span><span></span></li>`).join("")}</ul>
    </li>`).join("");
  $("#decision").hidden = true;
  $("#checks").innerHTML = '<li class="placeholder">Waiting for extraction…</li>';
  $("#checkCount").textContent = "";
  $("#extracted").innerHTML = '<div class="placeholder">Nothing yet</div>';
  $("#log").innerHTML = "";
  $("#docFrame").innerHTML = docEmbed(`/api/jobs/${job.id}/preview.png`, job.docUrl);
  live.raw = null;
  job.events.forEach(apply);
  renderQueue();
  tick();
}
const live = { raw: null };

function tick() {
  clearInterval(timerHandle);
  const job = viewing;
  const draw = () => {
    if (!job.started) { $("#runTimer").textContent = "—"; return; }
    const end = job.ended ?? performance.now();
    $("#runTimer").textContent = `${((end - job.started) / 1000).toFixed(1)}s`;
  };
  draw();
  if (!job.ended) timerHandle = setInterval(() => { draw(); if (job.ended) clearInterval(timerHandle); }, 100);
}

function stageEl(key) { return $(`.stage[data-stage="${key}"]`); }
function subEl(stage, name) {
  let li = $(`li[data-sub="${name}"]`, stageEl(stage));
  if (!li) {  // e.g. OCR only appears for scans
    li = document.createElement("li");
    li.dataset.sub = name;
    li.innerHTML = `<span>${SUB_LABEL[name] || name}</span><span></span>`;
    const before = $(`li[data-sub="llm_extraction"]`, stageEl(stage));
    $(".stage-subs", stageEl(stage)).insertBefore(li, before);
  }
  return li;
}
function setStage(key, state) {
  const el = stageEl(key);
  el.classList.remove("running", "done", "error");
  el.classList.add(state);
  $(".stage-icon", el).innerHTML = state === "running" ? ICON.spin : state === "done" ? ICON.check : ICON.x;
}
function log(e, stage, text, cls = "") {
  const job = viewing;
  const t = job.t0 && e.ts ? (e.ts - job.t0).toFixed(2) + "s" : "";
  const li = document.createElement("li");
  li.innerHTML = `<span class="t">${t}</span><span>${esc(stage)}</span><span class="${cls}">${esc(text)}</span>`;
  $("#log").appendChild(li);
  $("#log").scrollTop = 1e6;
}

function apply(e) {
  switch (e.type) {
    case "queued": $("#runMeta").textContent = "Queued…"; break;
    case "started": $("#runMeta").textContent = "Running"; tick(); break;
    case "step_start": {
      const idx = STAGES.findIndex((s) => s.key === e.stage);
      STAGES.slice(0, idx).forEach((s) => { if (!stageEl(s.key).classList.contains("error")) setStage(s.key, "done"); });
      setStage(e.stage, "running");
      subEl(e.stage, e.name).className = "running";
      break;
    }
    case "step_end": {
      const li = subEl(e.stage, e.name);
      li.className = e.status;
      li.lastElementChild.textContent = ms(e.duration_ms);
      const el = stageEl(e.stage);
      el.dataset.ms = (Number(el.dataset.ms || 0) + e.duration_ms);
      $(".stage-time", el).textContent = ms(Number(el.dataset.ms));
      const label = SUB_LABEL[e.name] || e.name;
      if (e.status === "error") { setStage(e.stage, "error"); log(e, e.stage, `${label}: ${e.detail.error}`, "err"); }
      else log(e, e.stage, `${label}: ${stepSummary(e)}`);
      break;
    }
    case "extracted": live.raw = e.raw; break;
    case "normalised": renderExtracted($("#extracted"), e.invoice, live.raw, e.notes); break;
    case "finding": addCheck($("#checks"), e); break;
    case "done":
      STAGES.forEach((s) => setStage(s.key, "done"));
      $("#runMeta").textContent = `Finished · run #${e.run_id}`;
      renderDecision($("#decision"), e.outcome, e.summary, e.reasons, e.run_id);
      break;
    case "failed":
      $("#runMeta").textContent = "Failed";
      $$(".stage").forEach((el) => { if (!el.classList.contains("done") && !el.classList.contains("error")) el.classList.add("skipped"); });
      renderDecision($("#decision"), "FAILED", "Processing stopped - no decision was made. The failed run is recorded in history.", [e.error], e.run_id);
      break;
  }
}
function stepSummary(e) {
  const d = e.detail || {};
  switch (e.name) {
    case "load_document": return `${d.pages} page(s), ${d.text_layer ? "text layer found" : "no text layer → OCR"}, sha ${d.sha256}`;
    case "ocr": return `${d.chars} chars, min confidence ${d.min_confidence}`;
    case "llm_extraction": return `${d.model} · ${d.prompt_tokens}+${d.completion_tokens} tokens${d.attempts > 1 ? ` · ${d.attempts} attempts` : ""}`;
    case "normalise": return `${d.invoice_number} → ${d.normalised}, ${d.lines} lines, total ${d.total} ${d.currency ?? ""}${d.notes?.length ? ` · ${d.notes.length} note(s)` : ""}`;
    case "rules": { const r = Object.values(d.results || {}); return `${r.filter((x) => x === "pass").length} pass, ${r.filter((x) => x === "warn").length} warn, ${r.filter((x) => x === "fail").length} fail · policy v${d.policy_version ?? 0}`; }
    case "policy": return `${d.outcome}`;
    case "persist": return `invoice #${d.invoice_id}, run #${d.run_id}`;
    default: return "ok";
  }
}

// ------------------------------------------------------------------ shared renderers
const OUTCOME_ICON = { pass: ICON.check, warn: ICON.alert, fail: ICON.x, skip: ICON.dash };
function addCheck(list, f) {
  $(".placeholder", list)?.remove();
  const li = document.createElement("li");
  li.className = `check ${f.outcome}`;
  const ev = Object.entries(f.evidence || {}).filter(([, v]) => v !== null && v !== undefined);
  li.innerHTML = `
    <div class="check-row">
      <span class="check-icon">${OUTCOME_ICON[f.outcome]}</span>
      <span class="check-rule">${esc(f.rule)}</span>
      <span class="check-msg">${esc(f.message)}</span>
      <span class="check-action ${f.action}">${f.action === "none" ? "" : esc(f.action)}</span>
    </div>
    ${ev.length ? `<div class="evidence" hidden><dl class="kv">${ev.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(Array.isArray(v) ? v.map((x) => (typeof x === "object" ? JSON.stringify(x) : x)).join(" · ") : typeof v === "object" ? JSON.stringify(v) : v)}</dd>`).join("")}</dl></div>` : ""}`;
  const evEl = $(".evidence", li);
  if (evEl) {
    if (f.outcome === "fail") evEl.hidden = false;
    $(".check-row", li).addEventListener("click", () => (evEl.hidden = !evEl.hidden));
  }
  list.appendChild(li);
  const n = $$(".check", list).length, bad = $$(".check.fail", list).length;
  const counter = list.closest(".card")?.querySelector(".count");
  if (counter) counter.textContent = `${n} run${bad ? ` · ${bad} failed` : ""}`;
}

function renderDecision(el, outcome, summary, reasons, runId) {
  const s = STATUS[outcome];
  el.className = `decision ${outcome}`;
  el.hidden = false;
  el.innerHTML = `
    <div class="decision-badge">${s.icon}</div>
    <div><div class="decision-title">${s.label}</div><div class="decision-summary">${esc(summary)}</div></div>
    <div>${runId && el.id === "decision" ? `<button class="btn small" data-open-run="${runId}">Open in dashboard</button>` : ""}</div>
    ${reasons?.length ? `<ul class="decision-reasons">${reasons.map((r) => {
      const m = r.match(/^\[(\w+)\] ([\w_]+): (.*)$/);
      return m ? `<li><span class="tag">${esc(m[1].toLowerCase())} · ${esc(m[2])}</span><span>${esc(m[3])}</span></li>` : `<li><span>${esc(r)}</span></li>`;
    }).join("")}</ul>` : ""}`;
  $("[data-open-run]", el)?.addEventListener("click", () => { showView("dashboard"); openRun(runId); });
}

function renderExtracted(el, inv, raw, notes) {
  const cur = inv.currency;
  const f = (label, value, rawValue) => {
    const missing = value === null || value === undefined || value === "";
    const showRaw = rawValue && String(rawValue) !== String(value);
    return `<div><div class="field-label">${label}</div><div class="field-value ${missing ? "missing" : ""}">${missing ? "not found" : esc(value)}${showRaw ? `<span class="raw">printed: ${esc(rawValue)}</span>` : ""}</div></div>`;
  };
  const lines = inv.lines || [];
  el.innerHTML = `
    <div class="fields">
      <div><div class="field-label">Document</div><div class="field-value"><span class="chip-doc">${esc((inv.document_type || "invoice").replace("_", " "))}</span></div></div>
      ${f("Amount paid / due", [inv.amount_paid && `paid ${money(inv.amount_paid, cur)}`, inv.balance_due && `due ${money(inv.balance_due, cur)}`].filter(Boolean).join(" · ") || null)}
      ${f("Vendor", inv.vendor_name)}
      ${f("Tax id", inv.vendor_tax_id)}
      ${f("Invoice no.", inv.invoice_number, null)}
      ${f("Normalised no.", inv.invoice_number_norm)}
      ${f("Invoice date", inv.invoice_date, raw?.invoice_date)}
      ${f("Due date", inv.due_date, raw?.due_date)}
      ${f("PO number", inv.po_number)}
      ${f("Bank (IBAN)", inv.vendor_iban)}
    </div>
    <table class="lines">
      <thead><tr><th>Description</th><th class="num">Qty</th><th class="num">Unit</th><th class="num">Amount</th></tr></thead>
      <tbody>${lines.map((l) => `<tr><td>${esc(l.description)}</td><td class="num">${esc(l.quantity === null ? "—" : Number(l.quantity))}</td><td class="num">${money(l.unit_price, cur)}</td><td class="num">${money(l.amount, cur)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">No line items read</td></tr>`}</tbody>
      <tfoot>
        <tr><td colspan="3">Subtotal</td><td class="num">${money(inv.subtotal, cur)}</td></tr>
        <tr><td colspan="3">Tax</td><td class="num">${money(inv.tax, cur)}</td></tr>
        <tr><td colspan="3"><b>Total ${cur ? `(${esc(cur)})` : ""}</b></td><td class="num"><b>${money(inv.total, cur)}</b></td></tr>
      </tfoot>
    </table>
    ${notes?.length || inv.unclear_fields?.length ? `<ul class="notes">${(notes || []).map((n) => `<li>ℹ ${esc(n)}</li>`).join("")}${inv.unclear_fields?.length ? `<li>⚠ model unsure about: ${esc(inv.unclear_fields.join(", "))}</li>` : ""}</ul>` : ""}`;
}

// ------------------------------------------------------------------ dashboard
let RUNS = [];
let filter = "ALL";
$("#search").addEventListener("input", renderRuns);
$("#refresh").addEventListener("click", loadDashboard);
$("#reset").addEventListener("click", async () => {
  if (!confirm("Delete all processed invoices and run history? Vendors and POs are kept.")) return;
  await api("/api/reset", { method: "POST" });
  toast("History cleared"); loadDashboard();
});

async function loadDashboard() {
  const [stats, runs] = await Promise.all([api("/api/stats"), api("/api/runs")]);
  RUNS = runs;
  renderKpis(stats);
  renderRuleChart(stats.rules);
  renderMix(stats);
  renderRuns();
}
function renderKpis(s) {
  const b = s.by_status;
  const amt = (key) => s.amounts.map((a) => money(a[key], a.currency)).filter((x) => !/^\D*0\.00$/.test(x)).join(" · ") || "—";
  const k = (cls, label, icon, value, sub) => `<div class="kpi ${cls}"><div class="kpi-label">${icon}${label}</div><div class="kpi-value">${value}</div><div class="kpi-sub">${sub}</div></div>`;
  $("#kpis").innerHTML = [
    k("", "Runs", ICON.stack, s.total, "all time"),
    k("APPROVE", "Approved", ICON.check, b.APPROVE || 0, amt("approved")),
    k("REVIEW", "Awaiting review", ICON.alert, s.pending_review, amt("pending")),
    k("REJECT", "Rejected", ICON.x, b.REJECT || 0, `stopped ${amt("stopped")}`),
    k("FAILED", "Failed", ICON.dash, b.FAILED || 0, "no decision made"),
    k("", "Avg processing", ICON.clock, s.avg_ms ? ms(s.avg_ms) : "—", "end to end"),
  ].join("");
}
function renderRuleChart(rules) {
  const max = Math.max(1, ...rules.map((r) => r.blocked));
  const el = $("#ruleChart");
  if (!rules.length) { el.innerHTML = '<div class="empty">No runs yet.</div>'; return; }
  el.innerHTML = rules.map((r) => `
    <div class="row" data-tip="${esc(r.rule)}: stopped ${r.blocked} of ${r.total} runs (${Math.round((100 * r.blocked) / r.total)}%)">
      <span class="label">${esc(r.rule)}</span>
      <span class="track"><span class="bar" style="width:${(100 * r.blocked) / max}%;${r.blocked ? "" : "opacity:.25"}"></span></span>
      <span class="val">${r.blocked} / ${r.total}</span>
    </div>`).join("");
  bindTips(el);
}
function renderMix(s) {
  const order = ["APPROVE", "REVIEW", "REJECT", "FAILED"];
  const color = { APPROVE: "var(--good)", REVIEW: "var(--warn)", REJECT: "var(--bad)", FAILED: "var(--fail)" };
  const total = s.total || 1;
  $("#mixChart").innerHTML = `
    <div class="mix">${order.filter((o) => s.by_status[o]).map((o) => `<span style="flex:${s.by_status[o]};background:${color[o]}" data-tip="${STATUS[o].label}: ${s.by_status[o]} (${Math.round((100 * s.by_status[o]) / total)}%)"></span>`).join("") || '<span style="flex:1;background:var(--surface-2)"></span>'}</div>
    <div class="mix-legend">${order.map((o) => `<span><i style="background:${color[o]}"></i>${STATUS[o].label} <b>${s.by_status[o] || 0}</b></span>`).join("")}</div>`;
  bindTips($("#mixChart"));
  $("#money").innerHTML = (s.amounts.length ? s.amounts : [{ currency: "", approved: 0, pending: 0, stopped: 0 }]).map((a) => `
    <div><div class="m-label">Approved ${esc(a.currency)}</div><div class="m-value">${money(a.approved, a.currency)}</div></div>
    <div><div class="m-label">Held for review ${esc(a.currency)}</div><div class="m-value">${money(a.pending, a.currency)}</div></div>
    <div><div class="m-label">Stopped ${esc(a.currency)}</div><div class="m-value">${money(a.stopped, a.currency)}</div></div>`).join("");
}
function renderRuns() {
  const counts = RUNS.reduce((m, r) => ((m[r.status] = (m[r.status] || 0) + 1), m), {});
  const chips = [["ALL", "All", RUNS.length], ["APPROVE", "Approved"], ["REVIEW", "Review"], ["REJECT", "Rejected"], ["FAILED", "Failed"]];
  $("#filters").innerHTML = chips.map(([k, l, n]) => `<button class="chip ${filter === k ? "active" : ""}" data-f="${k}">${l}<b>${n ?? counts[k] ?? 0}</b></button>`).join("");
  $$("#filters .chip").forEach((c) => c.addEventListener("click", () => { filter = c.dataset.f; renderRuns(); }));
  const q = $("#search").value.trim().toLowerCase();
  const rows = RUNS.filter((r) => (filter === "ALL" || r.status === filter) &&
    (!q || [r.source_file, r.vendor_name, r.invoice_number].some((v) => (v || "").toLowerCase().includes(q))));
  $("#runsBody").innerHTML = rows.length ? rows.map((r) => `
    <tr data-run="${r.run_id}">
      <td class="muted">${r.run_id}</td>
      <td title="${esc(new Date(r.started_at).toLocaleString())}">${ago(r.started_at)}</td>
      <td class="file"><div>${esc(r.source_file)}</div><span class="summary">${esc(r.error || r.summary)}</span></td>
      <td class="vendor">${esc(r.vendor_name || "—")}</td>
      <td class="mono">${esc(r.invoice_number || "—")}</td>
      <td class="num">${money(r.total, r.currency)}</td>
      <td>${r.resolution ? pill(r.status, ' <span class="resolved">· by reviewer</span>') : pill(r.status)}</td>
      <td class="num muted">${ms(r.duration_ms)}</td>
    </tr>`).join("") : `<tr class="empty"><td colspan="8">${RUNS.length ? "No runs match." : "No runs yet - process an invoice to see it here."}</td></tr>`;
  $$("#runsBody tr[data-run]").forEach((tr) => tr.addEventListener("click", () => openRun(Number(tr.dataset.run))));
}

// ------------------------------------------------------------------ run detail drawer
$("#drawerClose").addEventListener("click", closeDrawer);
$("#scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
function closeDrawer() { $("#drawer").classList.remove("open"); $("#scrim").hidden = true; }

async function openRun(id) {
  const d = await api(`/api/runs/${id}`);
  const { run, invoice: inv, lines, steps, findings } = d;
  const status = inv?.resolution || run.decision;
  $("#drawerTitle").innerHTML = `<div class="t">${esc(run.source_file)}</div><div class="muted" style="font-size:12px">Run #${run.id} · ${new Date(run.started_at).toLocaleString()} · ${esc(run.model || "")} · policy v${run.policy_version ?? 0}</div>`;
  const body = $("#drawerBody");
  body.innerHTML = `
    <div class="decision" id="drawerDecision"></div>
    ${inv?.resolution ? `<div class="card" style="margin-bottom:16px">Reviewer decision: ${pill(inv.resolution)} <span class="muted">${esc(inv.resolution_note || "")} · ${new Date(inv.resolved_at).toLocaleString()}</span></div>` : ""}
    <div class="drawer-grid">
      <div>
        <div class="card"><div class="card-title">Document</div><div class="doc-frame">${run.document_path ? docEmbed(`/api/runs/${run.id}/preview.png`, `/api/runs/${run.id}/document`) : '<span class="muted">not stored</span>'}</div></div>
      </div>
      <div>
        <div class="card"><div class="card-title">Checks <span class="count"></span></div><ul class="checks" id="drawerChecks">${findings.length ? "" : '<li class="placeholder">No checks ran</li>'}</ul></div>
        ${inv ? `<div class="card"><div class="card-title">What was read</div><div class="extracted" id="drawerExtracted"></div></div>` : ""}
        <div class="card"><div class="card-title">Trace</div><ol class="timeline">${steps.map((s) => `
          <li class="${s.status}"><span class="dot"></span><span class="stage-l">${esc(s.stage)}</span>
            <details><summary>${esc(SUB_LABEL[s.name] || s.name)}</summary><pre>${esc(JSON.stringify(s.detail, null, 2))}</pre></details>
            <span class="mono muted">${ms(s.duration_ms)}</span></li>`).join("")}
          ${run.raw_extraction ? `<li><span class="dot"></span><span class="stage-l">raw</span><details><summary>LLM output (as returned)</summary><pre>${esc(JSON.stringify(run.raw_extraction, null, 2))}</pre></details><span></span></li>` : ""}
        </ol></div>
      </div>
    </div>`;
  const reasons = findings.filter((f) => ["review", "reject", "note"].includes(f.action)).sort((a, b) => ["reject", "review", "note"].indexOf(a.action) - ["reject", "review", "note"].indexOf(b.action))
    .map((f) => `[${f.action.toUpperCase()}] ${f.rule}: ${f.message}`);
  renderDecision($("#drawerDecision"), run.decision, run.summary, run.error ? [run.error] : reasons, run.id);
  if (inv && inv.decision === "REVIEW" && !inv.resolution) {
    const box = document.createElement("div");
    box.className = "resolve";
    box.innerHTML = `<input id="resNote" placeholder="Reviewer note (e.g. 'confirmed new IBAN by phone with known contact')"><button class="btn approve">Approve</button><button class="btn reject">Reject</button>`;
    $("#drawerDecision").appendChild(box);
    $$("button", box).forEach((b) => b.addEventListener("click", async () => {
      const resolution = b.classList.contains("approve") ? "APPROVE" : "REJECT";
      try {
        await api(`/api/invoices/${inv.id}/resolve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ resolution, note: $("#resNote").value }) });
        toast(`Invoice ${resolution === "APPROVE" ? "approved" : "rejected"} by reviewer`);
        openRun(id); loadDashboard();
      } catch (e) { toast(e.message); }
    }));
  }
  drawerActions(run, inv, findings);
  findings.forEach((f) => addCheck($("#drawerChecks"), f));
  if (inv) {
    const invView = { ...inv, vendor_tax_id: run.raw_extraction?.vendor_tax_id, vendor_iban: inv.iban,
      lines: lines.map((l) => ({ description: l.description, quantity: l.quantity, unit_price: l.unit_price, amount: l.amount })),
      unclear_fields: run.raw_extraction?.unclear_fields || [] };
    const notes = steps.find((s) => s.name === "normalise")?.detail?.notes || [];
    renderExtracted($("#drawerExtracted"), invView, run.raw_extraction, notes);
  }
  $("#scrim").hidden = false;
  $("#drawer").classList.add("open");
  void status;
}

// ------------------------------------------------------------------ master data
let loadMaster = async function () {
  const m = await api("/api/master");
  $("#poList").innerHTML = m.purchase_orders.map((p) => {
    const pct = (100 * Number(p.billed)) / Number(p.amount);
    return `<div class="po"><b class="mono">${esc(p.po_number)}</b><span>${esc(p.vendor)}</span>
      <div class="po-bar ${pct > 100 ? "over" : ""}" data-tip="${pct.toFixed(0)}% of PO billed"><span style="width:${Math.min(pct, 100)}%"></span></div>
      <span class="mono" style="text-align:right">${money(p.billed, p.currency)} / ${money(p.amount, p.currency)}</span></div>`;
  }).join("") || '<div class="placeholder">No purchase orders</div>';
  bindTips($("#poList"));
  const mask = (s) => (s && s.length > 8 ? `${s.slice(0, 4)} •••• ${s.slice(-4)}` : s || "—");
  $("#vendorTable").innerHTML = `<thead><tr><th>Vendor</th><th>Tax id</th><th>Bank account on file</th><th>Currency</th></tr></thead><tbody>${
    m.vendors.map((v) => `<tr><td class="vendor">${esc(v.name)}</td><td class="mono">${esc(v.tax_id)}</td><td class="mono">${esc(mask(v.iban))}</td><td>${esc(v.currency)}</td></tr>`).join("")}</tbody>`;
};

// ------------------------------------------------------------------ tooltips
function bindTips(root) {
  const tip = $("#tooltip");
  $$("[data-tip]", root).forEach((el) => {
    el.addEventListener("mousemove", (e) => { tip.textContent = el.dataset.tip; tip.hidden = false; tip.style.left = `${e.clientX + 12}px`; tip.style.top = `${e.clientY + 12}px`; });
    el.addEventListener("mouseleave", () => (tip.hidden = true));
  });
}

// ------------------------------------------------------------------ drawer actions: re-run, onboard vendor
function drawerActions(run, inv, findings) {
  const box = document.createElement("div");
  box.className = "drawer-actions";
  const unknown = findings.find((f) => f.rule === "vendor_match" && f.outcome === "fail" && !f.evidence?.master_tax_id);
  if (!run.superseded_by && run.document_path) {
    box.innerHTML = `<button class="btn small" data-act="rerun" title="Process the same document again with today's master data and policy">↻ Re-run with current data &amp; policy</button>`;
  }
  if (unknown) box.innerHTML += `<button class="btn small" data-act="onboard">+ Add ${esc(unknown.evidence.name || "vendor")} as approved vendor</button>`;
  if (!box.innerHTML) return;
  $("#drawerDecision").appendChild(box);

  const rerun = async () => {
    try {
      const j = await post(`/api/runs/${run.id}/rerun`, {});
      closeDrawer(); showView("process"); followLatest = true;
      addJob(j, `/api/runs/${run.id}/document`);
    } catch (e) { toast(e.message); }
  };
  $("[data-act=rerun]", box)?.addEventListener("click", rerun);
  $("[data-act=onboard]", box)?.addEventListener("click", (btn) => {
    btn.currentTarget.disabled = true;
    const ev = unknown.evidence;
    const form = document.createElement("div");
    form.className = "onboard";
    form.innerHTML = `<b>Onboard vendor</b> <span class="muted">- confirm these against your vendor file, not only the invoice</span>
      <form class="inline-form">
        <input name="name" value="${esc(ev.name || "")}" placeholder="Legal name" required>
        <input name="tax_id" value="${esc(ev.tax_id || "")}" placeholder="Tax id" required>
        <input name="iban" value="${esc(ev.iban || "")}" placeholder="IBAN (optional)">
        <input name="currency" value="${esc(ev.currency || "USD")}" maxlength="3" required>
        <button class="btn primary" type="submit">Save vendor &amp; re-run</button>
      </form>`;
    $("#drawerDecision").appendChild(form);
    $("form", form).addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await post("/api/vendors", Object.fromEntries(new FormData(e.target)));
        toast("Vendor added - re-running the document");
        rerun();
      } catch (err) { toast(err.message); }
    });
  });
}

// ------------------------------------------------------------------ master data forms
function post(url, body) {
  return api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}
$("#vendorForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await post("/api/vendors", Object.fromEntries(new FormData(e.target))); e.target.reset(); toast("Vendor added"); loadMaster(); }
  catch (err) { toast(err.message); }
});
$("#poForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = Object.fromEntries(new FormData(e.target));
  try { await post("/api/pos", { ...d, vendor_id: Number(d.vendor_id) }); e.target.reset(); toast("PO added"); loadMaster(); }
  catch (err) { toast(err.message); }
});
const renderMasterTables = loadMaster;
loadMaster = async function () {
  await renderMasterTables();
  const m = await api("/api/master");
  $("#poVendor").innerHTML = `<option value="">Vendor…</option>` +
    m.vendors.map((v) => `<option value="${v.id}">${esc(v.name.slice(0, 48))} (${esc(v.currency)})</option>`).join("");
};

// ------------------------------------------------------------------ policy
const POLICY_META = {
  amount_tolerance: { label: "Rounding tolerance", unit: "" },
  po_overrun_pct: { label: "PO overrun tolerance", unit: "%" },
  duplicate_window_days: { label: "Resubmission look-back", unit: "days" },
  stale_invoice_days: { label: "Flag invoices older than", unit: "days" },
  approval_limit: { label: "Auto-approval limit", unit: "" },
  po_required_above: { label: "PO required above", unit: "" },
  date_order: { label: "Ambiguous dates like 03/04", choices: { MDY: "Month first", DMY: "Day first" }, desc: "How to read a date that could be either" },
  unknown_vendor_action: { label: "Vendor not approved", desc: "Invoice from a vendor not in master data" },
  near_duplicate_action: { label: "Same amount, new number", desc: "Possible resubmission under a new invoice number" },
  stale_invoice_action: { label: "Old invoice", desc: "Older than the limit above" },
  receipt_action: { label: "Receipt / already paid", desc: "Document shows the amount is already paid" },
};
let POLICY = null;

async function loadPolicy() {
  POLICY = await api("/api/policy");
  $("#policyVersion").textContent = `active: v${POLICY.version}${POLICY.version ? "" : " (defaults)"}`;
  renderPolicyForm(POLICY.settings);
  const lock = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/></svg>';
  $("#lockedList").innerHTML = POLICY.locked.map((l) => `<li>${lock}
    <div><span class="mono">${esc(l.rule)}</span><div class="why">${esc(l.why)}</div></div>
    <span class="check-action ${l.action}">always ${esc(l.action)}</span></li>`).join("");
  $("#policyHistory").innerHTML = POLICY.history.length
    ? POLICY.history.map((h) => `<li><span class="v">v${h.version}</span><span>${esc(h.note || "no note")}</span><span class="muted" style="margin-left:auto">${ago(h.created_at)}</span></li>`).join("")
    : `<li class="muted">No changes yet - running on defaults</li>`;
}

function bounds(sc) {
  const pick = (key) => sc[key] ?? (sc.anyOf || []).map((x) => x[key]).find((x) => x !== undefined);
  return { min: pick("minimum"), max: pick("maximum") };
}

function renderPolicyForm(values) {
  const schema = POLICY.schema;
  $("#policyForm").innerHTML = Object.entries(POLICY_META).map(([k, m]) => {
    const sc = schema[k] || {};
    const desc = m.desc || sc.description || "";
    const changed = String(values[k]) !== String(POLICY.defaults[k]);
    const opts = sc.enum || [];
    let input, range = "";
    if (opts.length) {
      input = `<div class="choice" data-k="${k}">${opts.map((o) => `<button type="button" data-v="${o}" class="${values[k] === o ? "on" : ""}">${esc(m.choices?.[o] || o)}</button>`).join("")}</div>`;
    } else {
      const { min, max } = bounds(sc);
      input = `<input data-k="${k}" type="number" step="any" value="${esc(Number(values[k]))}" min="${min}" max="${max}"><span class="unit">${m.unit}</span>`;
      range = `allowed ${Number(min).toLocaleString()} – ${Number(max).toLocaleString()}`;
    }
    return `<div class="pfield ${changed ? "changed" : ""}"><div class="pl">${m.label}</div><div class="field-input">${input}</div>
      <div class="pd">${esc(desc)}</div>${range ? `<div class="range">${range}</div>` : ""}</div>`;
  }).join("");
  $$("#policyForm .choice button").forEach((b) => b.addEventListener("click", () => {
    $$("button", b.parentElement).forEach((x) => x.classList.toggle("on", x === b));
  }));
}

function readPolicyForm() {
  const out = { ...POLICY.settings };
  $$("#policyForm input[data-k]").forEach((i) => (out[i.dataset.k] = i.value));
  $$("#policyForm .choice").forEach((c) => (out[c.dataset.k] = $("button.on", c)?.dataset.v));
  return out;
}

$("#policySave").addEventListener("click", async () => {
  try {
    const r = await api("/api/policy", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: readPolicyForm(), note: $("#policyNote").value }) });
    $("#policyNote").value = "";
    toast(`Policy v${r.version} is active for new runs`);
    loadPolicy();
  } catch (e) { toast(e.message); }
});
$("#policyDefaults").addEventListener("click", () => renderPolicyForm(POLICY.defaults));

// ------------------------------------------------------------------ who is signed in (guest logins can't reset history)
api("/api/me").then((m) => {
  if (m.role !== "guest") return;
  $("#reset").hidden = true;
  const chip = document.createElement("span");
  chip.className = "model-chip";
  chip.title = "Guest login - everything except resetting history";
  chip.textContent = "guest";
  $("#modelChip").after(chip);
}).catch(() => {});
