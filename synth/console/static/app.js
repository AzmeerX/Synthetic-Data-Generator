/* Synth Console — the workshop (vanilla ES2020, no build step).
 * Talks only to the frozen HTTP contract (/api/..., docs/CONSOLE_API.md v1 + Addendum v2 + Addendum v3).
 * Sections, in order: constants · utils · state · api · timers · shell (top bar, status line, toasts, polling) · router ·
 * explore views (graph, documents, review) · dyno drill-down helpers (per-stage metrics, audit) · shared helpers
 * (builds, secrets, jobs, dialog, demo mode) · engines + servers registry · engine tree renderer · views (garage, servers,
 * run, dyno, data) · inspector (slot panel, stage, entity, edge, object, sentence) · events + boot.
 * Engines, their slots, lanes and stage trees come from GET /api/engines — nothing about a pipeline is hard-coded here.
 * Every server string goes through esc() before it reaches innerHTML.
 */
'use strict';

/* ================================================================ constants */
const CY_URL = '/static/vendor/cytoscape-3.30.2.min.js';   // bundled copy (MIT, see vendor/cytoscape-LICENSE.txt)
const LS_KEY = 'synth.console.run';

// Entity-type palette (validated for CVD separation on the dark surface). Used for nodes, chips and highlights.
const TYPE_COLORS = {
  person: '#3987e5', organization: '#9085e9', legal: '#d55181', value: '#199e70',
  place: '#c98500', thing: '#008300', event: '#d95926',
};
const TYPE_ORDER = Object.keys(TYPE_COLORS);
const GREY = '#7d8b98';

const STATUS_COLORS = {
  COMMITTED: '#2fbf4f', VERIFIED: '#4ea1ff', ESCALATED: '#fab219', HELD_FOR_HUMAN: '#ec835a',
  REJECTED: '#6b7280', FAILED: '#e5484d', failed: '#e5484d',
  RUNNING: '#4ea1ff', running: '#4ea1ff', INTERRUPTED: '#fab219', DONE: '#2fbf4f', done: '#2fbf4f', DONE_WITH_FAILURES: '#fab219',
  PENDING: '#8b9bb0', ANSWERED: '#2fbf4f', IRREDUCIBLE: '#b48cff', DEAD_LETTER: '#e5484d',
  ACTIVE: '#2fbf4f', MERGED: '#8b9bb0', APART: '#ec835a', CANDIDATE: '#8b9bb0', CONTRACTED: '#4ea1ff',
  OPEN: '#fab219', RESOLVED: '#2fbf4f', DISMISSED: '#6b7280', POTENTIAL: '#fab219', FINAL: '#e5484d',
  SEGMENTED: '#4ea1ff', INGESTED: '#4ea1ff',
};
const COVERAGE_ORDER = ['FACT_COMMITTED', 'HELD_FOR_HUMAN', 'FACTS_REJECTED', 'UNEXPRESSIBLE', 'READ_NO_FACT',
  'SKIPPED_FILTER', 'SKIPPED_BUDGET', 'FAILED', 'SILENT_DROP'];
const COVERAGE_COLORS = {
  FACT_COMMITTED: '#2fbf4f', HELD_FOR_HUMAN: '#ec835a', FACTS_REJECTED: '#a0525a', UNEXPRESSIBLE: '#9085e9',
  READ_NO_FACT: '#3f6488', SKIPPED_FILTER: '#3a434d', SKIPPED_BUDGET: '#c98500', FAILED: '#e5484d',
  SILENT_DROP: '#ff3ea5',
};
const EDGE_STATUSES = ['COMMITTED', 'VERIFIED', 'ESCALATED', 'HELD_FOR_HUMAN', 'REJECTED'];
const ESC_OUTCOMES = ['PENDING', 'ANSWERED', 'HELD_FOR_HUMAN', 'IRREDUCIBLE', 'FAILED'];
const SERIES = { in: '#3987e5', out: '#9085e9' };   // metrics bars: tokens in / out

// Presentation only (no pipeline semantics): node kinds, engine status badges, lane palette, dyno run colours.
const KIND_COLORS = { rules: '#7d8b98', model: '#9085e9', gliner: '#199e70', store: '#c98500' };
const ENGINE_STATUS_COLORS = { current: '#2fbf4f', classic: '#4ea1ff', legacy: '#8b9bb0', baseline: '#b48cff', experimental: '#fab219' };
const LANE_PALETTE = ['#3987e5', '#9085e9', '#c98500', '#199e70', '#d55181'];   // middle lanes, in engine order
const LANE_FIRST = '#7d8b98';   // the first lane (ingest)
const LANE_LAST = '#2fbf4f';    // the last lane (commit · audit)
// Dyno comparison series A–D (validated: CVD ΔE ≥ 9 on the panel surface); always direct-labelled too.
const RUN_COLORS = ['#3987e5', '#d95926', '#199e70', '#9085e9'];
const RUN_LETTERS = ['A', 'B', 'C', 'D'];
// Install hints for launchers whose tool is not on PATH (the contract has no hint field).
const LAUNCH_HINTS = {
  lmstudio: 'Install LM Studio, open it once, then run “lms bootstrap” so the lms command is on PATH.',
  ollama: 'Install Ollama from ollama.com — the installer puts the ollama command on PATH.',
  llamacpp: 'Download a llama.cpp release (or build it) and put llama-server on PATH; give it a .gguf model file.',
  vllm: 'pip install vllm in a CUDA environment, then the vllm command is on PATH.',
};
const LOCAL_KINDS = new Set(['lmstudio', 'ollama', 'vllm', 'llamacpp', 'jan']);
const SERVER_KINDS = ['lmstudio', 'ollama', 'vllm', 'llamacpp', 'openai', 'openrouter', 'deepseek', 'mistral', 'groq', 'together', 'anthropic', 'opencode_go', 'custom'];   // synth.console.servers.KINDS
const LOADABLE = new Set(['lmstudio', 'ollama']);   // POST /api/servers/{id}/load

/* ================================================================ utils */
const $ = (sel, root = document) => root.querySelector(sel);
const enc = encodeURIComponent;

function esc(v) {
  if (v === null || v === undefined) return '';
  return String(v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function shortId(id) {
  if (!id) return '—';
  const s = String(id);
  const i = s.indexOf('_');
  if (i < 0 || s.length <= i + 10) return s;
  return s.slice(0, i + 1) + '…' + s.slice(-6);
}
function fmtNum(n, digits = 0) {
  if (n === null || n === undefined || n === '' || Number.isNaN(Number(n))) return '—';
  return Number(n).toLocaleString('en-GB', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function fmtTokens(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
  const v = Number(n);
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (Math.abs(v) >= 1e4) return (v / 1e3).toFixed(1) + 'k';
  return fmtNum(v);
}
function fmtConf(c) { return c === null || c === undefined ? '—' : Number(c).toFixed(2); }
function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
function fmtDate(iso) {
  if (!iso) return '—';
  return String(iso).slice(0, 10);
}
function fmtDur(sec) {
  if (sec === null || sec === undefined || Number.isNaN(Number(sec))) return '—';
  const s = Number(sec);
  if (s < 60) return s.toFixed(1) + ' s';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + String(Math.round(s % 60)).padStart(2, '0') + 's';
  return Math.floor(s / 3600) + 'h ' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + 'm';
}
function secondsBetween(a, b) {
  if (!a) return null;
  const t0 = new Date(a).getTime();
  const t1 = b ? new Date(b).getTime() : Date.now();
  return Number.isNaN(t0) || Number.isNaN(t1) ? null : (t1 - t0) / 1000;
}
function typeColor(t) { return TYPE_COLORS[t] || GREY; }
function statusColor(s) { return STATUS_COLORS[s] || GREY; }

// Chips: the colour comes from our own constant maps, never from the server; the text is escaped.
function chip(text, color = GREY, cls = '', title = '') {
  return `<span class="chip ${cls}" style="--c:${color}"${title ? ` title="${esc(title)}"` : ''}><i class="dot"></i>${esc(text)}</span>`;
}
const statusChip = (s) => chip(s ?? '—', statusColor(s));
const typeChip = (t) => chip(t || 'unknown', typeColor(t));
const plainChip = (t, title) => chip(t, '#5f6e7c', 'plain', title);
const covChip = (c) => chip(c || 'no outcome', COVERAGE_COLORS[c] || GREY);
const jsonPre = (v) => `<pre class="json">${esc(JSON.stringify(v, null, 2))}</pre>`;
// `hint` is trusted, author-written markup.
const emptyState = (title, hint = '') => `<div class="empty"><b>${esc(title)}</b>${hint}</div>`;
const skeleton = (n = 4) => Array.from({ length: n }, (_, i) => `<div class="skel ${['w80', 'w60', '', 'w40'][i % 4]}"></div>`).join('');
const errorBox = (err, what = 'Could not load') => `<div class="error-box"><b>${esc(what)}.</b> <code>${esc(err && err.message ? err.message : err)}</code></div>`;
const NO_RUN_HINT = '<div>No runs yet — start one in <a href="#/run">Run</a>.</div>';

function confBar(c) {
  const pct = c === null || c === undefined ? 0 : Math.max(0, Math.min(1, Number(c))) * 100;
  return `<span class="confbar" aria-hidden="true"><i style="width:${pct.toFixed(0)}%"></i></span> <span class="conf">${fmtConf(c)}</span>`;
}
function timeChips(times) {
  return (times || []).map((t) => plainChip(`${t.role || 'time'}: ${t.label || t.value_start || '?'}`,
    [t.value_start, t.value_end].filter(Boolean).join(' – '))).join('');
}
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
// Escaped text with [start, end) wrapped in <mark>.
function markSlice(text, start, end, attrs = '') {
  const s = String(text ?? '');
  if (start === null || start === undefined || end === null || end === undefined || start < 0 || end > s.length || start >= end) return esc(s);
  return esc(s.slice(0, start)) + `<mark ${attrs}>${esc(s.slice(start, end))}</mark>` + esc(s.slice(end));
}
// Find the mention inside its sentence: trust offsets only if they reproduce the surface text.
function locate(sentence, surface, start, end) {
  const s = String(sentence ?? '');
  const ok = (a, b) => Number.isInteger(a) && Number.isInteger(b) && a >= 0 && b <= s.length && a < b;
  if (ok(start, end) && (!surface || s.slice(start, end) === surface)) return [start, end];
  if (surface) {
    let i = s.indexOf(surface);
    if (i < 0) i = s.toLowerCase().indexOf(String(surface).toLowerCase());
    if (i >= 0) return [i, i + String(surface).length];
  }
  return ok(start, end) ? [start, end] : null;
}
function changedKeys(before, after) {
  if (!before && after) return 'created';
  if (before && !after) return 'removed';
  if (typeof before !== 'object' || typeof after !== 'object' || !before || !after) return '';
  const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
  return [...keys].filter((k) => JSON.stringify(before[k]) !== JSON.stringify(after[k])).join(', ');
}

/* ================================================================ state */
const state = {
  overview: null,
  readOnly: false,
  runs: [],
  runId: null,
  jobs: [],
  profiles: null,
  documents: null,
  graphs: {},            // runId -> {data, nodes: Map, edges: Map, survivor: Map}
  docs: new Map(),       // `${runId}|${docId}` -> document payload
  edgeReg: new Map(),    // edge id -> edge object (from graph or entity payloads)
  audits: {},            // runId -> {result, at}
  metrics: null,         // last /metrics payload (for re-sorting the table)
  view: null,
  seq: 0,                // bumps on every route; async renders check it before writing
  online: true,
  lastSync: null,
  cy: null,
  cyStatus: 'loading',
  cyObserver: null,
  positions: {},         // runId -> {nodeId: {x, y}} so the layout survives view switches
  graphUi: { hiddenTypes: new Set(), statuses: new Set(['COMMITTED', 'VERIFIED', 'ESCALATED', 'HELD_FOR_HUMAN']), hiddenRels: new Set(), minConf: 0, hideIsolated: false, search: '' },
  insp: { stack: [], current: null, tab: 'facts', seq: 0 },
  review: { outcome: 'ALL', open: new Set(), rows: null },
  metricsSort: { key: null, dir: -1 },
  control: { selectedJob: null, follow: new Set(), seenRunning: new Set() },
  presets: null,
  secrets: null,
  corpus: null,
  outputs: null,
  // v3: the workshop
  engines: null,         // GET /api/engines
  enginesError: null,
  servers: null,         // GET /api/servers
  serversError: null,
  serversAt: null,
  launchers: null,
  detected: null,        // last POST /api/servers/detect result
  bench: null,
  showArchived: false,
  onto: { list: null, name: null, draft: null, saved: null, problems: [], dirty: false },
  gear: { stage: null, problems: false, call: null, tab: 'output', data: null, calls: null },
  benchError: null,
  trees: {},             // tree box id -> {engine, nodes, profile, runId, live, selected}
  compact: false,
  garage: { engine: null, profile: null },
  inspUser: null,        // 'open' | 'closed' once the operator toggles the inspector
  sp: null,              // slot panel: {engine, slot, profile}
  speed: {},             // "serverId|model" -> {status, jobId, result, at}
  loads: {},             // serverId -> {jobId, status, model}
  slotTests: {},         // "profile|endpoint" -> {jobId, status, job, verdict, at}
  preflight: {},         // "engine|profile" -> {status, jobId, verdict, at}   (this browser session only)
  runsUi: { engine: '', profile: '', rel: '', gliner: '', allEngines: false },
  runTree: null,         // last /api/pipeline payload for the Run view
  runDetail: null,       // last /api/runs/{id} payload for the Run view
  dyno: { sel: [], sort: { key: 'started_at', dir: -1 }, evalJob: null },
  data: { tab: 'stores', table: null, limit: 50, offset: 0, order: 'desc', runOnly: true, column: '', value: '', cols: {}, tables: null, db: null, result: null, reportRuns: new Set() },
};

/* ================================================================ api */
class ApiError extends Error {
  constructor(message, status, path) { super(message); this.status = status; this.path = path; }
}

async function api(path, { method = 'GET', body, form, quiet = false } = {}) {
  let res;
  try {
    // `form` (FormData) is sent as multipart; the browser sets its boundary header
    res = await fetch(path, {
      method,
      headers: body !== undefined && !form ? { 'Content-Type': 'application/json', Accept: 'application/json' } : { Accept: 'application/json' },
      body: form || (body !== undefined ? JSON.stringify(body) : undefined),
    });
  } catch (e) {
    const err = new ApiError(`Network error — server unreachable (${e.message})`, 0, path);
    setOnline(false);
    if (!quiet) toast('Request failed', `${method} ${path}: ${err.message}`, 'error');
    throw err;
  }
  setOnline(true);
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;
    if (data && typeof data === 'object' && data.detail !== undefined) {
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    } else if (typeof data === 'string' && data) {
      detail = data.slice(0, 300);
    }
    const err = new ApiError(`${res.status}: ${detail}`, res.status, path);
    if (!quiet) toast(`${method} ${path} failed`, err.message, 'error');
    throw err;
  }
  return data;
}

const r = (id) => `/api/runs/${enc(id)}`;
const API = {
  overview: (o) => api('/api/overview', o),
  runs: (o) => api('/api/runs', o),
  run: (id, o) => api(r(id), o),
  graph: (id, o) => api(`${r(id)}/graph`, o),
  entity: (run, id, o) => api(`${r(run)}/entities/${enc(id)}`, o),
  provenance: (run, id, o) => api(`${r(run)}/provenance/${enc(id)}`, o),
  documents: (o) => api('/api/documents', o),
  document: (run, doc, o) => api(`${r(run)}/documents/${enc(doc)}`, o),
  escalations: (id, o) => api(`${r(id)}/escalations`, o),
  metrics: (id, o) => api(`${r(id)}/metrics`, o),
  audit: (id, o) => api(`${r(id)}/audit`, o),
  profiles: (o) => api('/api/profiles', o),
  jobs: (o) => api('/api/jobs', o),
  job: (id, o) => api(`/api/jobs/${enc(id)}`, o),
  startRun: (body) => api('/api/runs', { method: 'POST', body }),
  resume: (id) => api(`${r(id)}/resume`, { method: 'POST', body: {} }),
  ingest: (path) => api('/api/ingest', { method: 'POST', body: { path } }),
  presets: (o) => api('/api/providers/presets', o),
  saveProfile: (n, body) => api(`/api/profiles/${enc(n)}`, { method: 'PUT', body }),
  copyProfile: (n, name) => api(`/api/profiles/${enc(n)}/copy`, { method: 'POST', body: { name } }),
  deleteProfile: (n) => api(`/api/profiles/${enc(n)}`, { method: 'DELETE' }),
  activateProfile: (n) => api(`/api/profiles/${enc(n)}/activate`, { method: 'POST', body: {} }),
  testSlot: (n, slot) => api(`/api/profiles/${enc(n)}/test`, { method: 'POST', body: { slot } }),
  secrets: (o) => api('/api/secrets', o),
  setSecret: (name, value) => api(`/api/secrets/${enc(name)}`, { method: 'PUT', body: { value } }),
  clearSecret: (name) => api(`/api/secrets/${enc(name)}`, { method: 'DELETE' }),
  corpus: (o) => api('/api/corpus', o),
  upload: (form) => api('/api/corpus/upload', { method: 'POST', form }),
  deleteCorpus: (name) => api(`/api/corpus/${enc(name)}`, { method: 'DELETE' }),
  db: (o) => api('/api/data/db', o),
  tables: (o) => api('/api/data/tables', o),
  table: (name, params, o) => api(`/api/data/tables/${enc(name)}?${qs(params)}`, o),
  outputs: (o) => api('/api/outputs', o),
  exportRun: (id) => api(`${r(id)}/export`, { method: 'POST', body: {} }),
  buildViewer: (id) => api(`${r(id)}/view`, { method: 'POST', body: {} }),
  report: (run_ids) => api('/api/reports', { method: 'POST', body: { run_ids } }),
};

async function getGraph(runId, { quiet = false, force = false } = {}) {
  if (!runId) return null;
  if (!force && state.graphs[runId]) return state.graphs[runId];
  const data = await API.graph(runId, { quiet });
  const g = { data: { nodes: data.nodes || [], edges: data.edges || [] }, nodes: new Map(), edges: new Map(), survivor: new Map() };
  for (const n of g.data.nodes) {
    g.nodes.set(n.id, n);
    for (const m of n.merged || []) g.survivor.set(m, n.id);
  }
  for (const e of g.data.edges) { g.edges.set(e.id, e); state.edgeReg.set(e.id, e); }
  state.graphs[runId] = g;
  return g;
}
async function getDoc(runId, docId, { quiet = false } = {}) {
  const key = `${runId}|${docId}`;
  if (state.docs.has(key)) return state.docs.get(key);
  const d = await API.document(runId, docId, { quiet });
  state.docs.set(key, d);
  return d;
}
function nodeFor(g, id) {
  if (!g || !id) return null;
  return g.nodes.get(id) || g.nodes.get(g.survivor.get(id)) || null;
}
function entityLabel(id) {
  const n = nodeFor(state.graphs[state.runId], id);
  return n ? n.label : shortId(id);
}
function docName(docId) {
  const d = (state.documents || []).find((x) => x.doc_id === docId);
  return d ? d.filename : shortId(docId);
}
function currentRun() { return state.runs.find((x) => x.run_id === state.runId) || null; }

/* ================================================================ timers (one per purpose; names starting "view:" die on route change) */
const timers = new Map();
function schedule(name, fn, ms) {
  cancel(name);
  timers.set(name, setTimeout(async () => {
    timers.delete(name);
    try { await fn(); } catch { /* the api wrapper already reported it */ }
  }, ms));
}
function cancel(name) {
  if (timers.has(name)) { clearTimeout(timers.get(name)); timers.delete(name); }
}
function cancelViewTimers() {
  for (const k of [...timers.keys()]) if (k.startsWith('view:')) cancel(k);
}

/* ================================================================ shell: toasts, top bar, status line */
const toastSeen = new Map();   // message -> {el, count}
function toast(title, detail = '', kind = 'info') {
  const key = `${kind}|${title}|${detail}`;
  const prior = toastSeen.get(key);
  if (prior && document.body.contains(prior.el)) {
    prior.count += 1;
    prior.el.querySelector('.t').textContent = `${title} ×${prior.count}`;
    clearTimeout(prior.timer);
    prior.timer = setTimeout(() => prior.el.remove(), kind === 'error' ? 9000 : 4500);
    return;
  }
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  el.innerHTML = `<span class="t">${esc(title)}</span>
    <button type="button" class="icon-btn x" aria-label="Dismiss notification"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="1.5"/></svg></button>
    ${detail ? `<span class="d">${esc(detail)}</span>` : ''}`;
  el.querySelector('.x').addEventListener('click', () => el.remove());
  $('#toasts').appendChild(el);
  const entry = { el, count: 1, timer: setTimeout(() => el.remove(), kind === 'error' ? 9000 : 4500) };
  toastSeen.set(key, entry);
  while ($('#toasts').children.length > 5) $('#toasts').firstElementChild.remove();
}

function setOnline(on) {
  state.online = on;
  if (on) state.lastSync = new Date();
  renderStatusbar();
}

function renderRunSelect() {
  const sel = $('#runSelect');
  if (!state.runs.length) {
    sel.innerHTML = '<option value="">No runs yet</option>';
    sel.disabled = true;
  } else {
    sel.disabled = false;
    sel.innerHTML = state.runs.filter((x) => !x.archived || state.showArchived || x.run_id === state.runId).map((x) =>
      `<option value="${esc(x.run_id)}"${x.run_id === state.runId ? ' selected' : ''}>${esc(shortId(x.run_id))} · ${esc(engineName(x.arm))} · ${esc(x.profile || '—')} · ${esc(x.status)}</option>`).join('');
  }
  renderStatusbar();
}

function renderJobIndicator() {
  const running = state.jobs.filter((j) => j.status === 'running');
  const el = $('#jobIndicator');
  el.classList.toggle('running', running.length > 0);
  const text = running.length
    ? `${running.length} job${running.length > 1 ? 's' : ''} running · ${running.map((j) => j.kind).join(', ')}`
    : 'No jobs running';
  $('#jobIndicatorText').textContent = text;
  el.setAttribute('aria-label', `Background jobs: ${text}. Open Run`);
  $('#demoBadge').hidden = !state.readOnly;
}

function renderStatusbar() {
  const ov = state.overview;
  $('#sbDb').innerHTML = `db <span class="${state.online ? '' : 'bad'}">${esc(ov ? ov.db : '—')}</span>`;
  $('#sbCounts').textContent = ov ? `${fmtNum(ov.runs)} runs · ${fmtNum(ov.documents)} docs` : '';
  const run = currentRun();
  $('#sbRun').textContent = run ? `run ${shortId(run.run_id)} · ${engineName(run.arm)} · ${run.status}` : 'no run selected';
  const lib = { loading: 'graph lib: loading…', ready: 'graph lib: ready', failed: 'graph lib: unavailable' }[state.cyStatus];
  $('#sbGraphLib').innerHTML = `<span class="${state.cyStatus === 'ready' ? 'ok' : state.cyStatus === 'failed' ? 'bad' : ''}">${esc(lib)}</span>`;
  $('#sbSync').innerHTML = state.online
    ? (state.lastSync ? `synced ${esc(state.lastSync.toLocaleTimeString('en-GB'))}` : '')
    : '<span class="bad">server unreachable</span>';
}

async function refreshOverview(quiet = false) {
  state.overview = await API.overview({ quiet });
  state.readOnly = !!state.overview.read_only;
  renderJobIndicator();
  renderStatusbar();
}
async function refreshRuns(quiet = false) {
  state.runs = (await API.runs({ quiet })) || [];
  if (!state.runs.some((x) => x.run_id === state.runId)) state.runId = state.runs.length ? state.runs[0].run_id : null;
  renderRunSelect();
}
async function refreshJobs(quiet = false) {
  const prev = new Map(state.jobs.map((j) => [j.job_id, j]));
  state.jobs = (await API.jobs({ quiet })) || [];
  let runsChanged = false;
  let runFinished = false;
  for (const j of state.jobs) {
    const before = prev.get(j.job_id);
    if (before && before.status === 'running' && j.status !== 'running') {
      toast(`Job ${j.status}: ${j.kind}`, `${j.job_id}${j.exit_code !== null && j.exit_code !== undefined ? ` · exit ${j.exit_code}` : ''}`, j.status === 'done' ? 'ok' : 'error');
      runsChanged = true;
      if (j.kind === 'run' || j.kind === 'resume') runFinished = true;
    }
    if (j.run_id && !state.runs.some((x) => x.run_id === j.run_id)) runsChanged = true;
  }
  if (runsChanged) await refreshRuns(true).catch(() => {});
  for (const j of state.jobs) {   // follow runs started from this console
    if (j.run_id && state.control.follow.has(j.job_id) && state.runs.some((x) => x.run_id === j.run_id)) {
      state.control.follow.delete(j.job_id);
      if (state.runId !== j.run_id) {
        toast('Run started', `${j.run_id} selected`, 'ok');
        selectRun(j.run_id, { rerender: state.view === 'run' });
      }
    }
  }
  renderJobIndicator();
  const seq = state.seq;
  const alive = () => seq === state.seq;
  if (state.view === 'run' && $('#runPage')) {
    renderJobsTable();
    renderRunsButtons();
    if (runsChanged) renderRecentRuns();
    if (runFinished) loadRunLive(alive, { quiet: true });
    else if (runJobRunning() && !timers.has('view:run-live')) scheduleRunLive(alive);
  }
  if (state.view === 'data' && state.data.tab === 'outputs') { renderOutJobs(); if (runsChanged) renderReportRuns(); }
  if (state.view === 'dyno' && runFinished) loadBench(alive, { quiet: true });
}
function anyJobRunning() { return state.jobs.some((j) => j.status === 'running'); }
function isRunLive(runId) {
  if (!runId) return false;
  const run = state.runs.find((x) => x.run_id === runId);
  if (run && run.live) return true;
  return state.jobs.some((j) => j.status === 'running' && (j.kind === 'run' || j.kind === 'resume') && j.run_id === runId);
}
function runJobRunning() {
  return state.jobs.some((j) => j.status === 'running' && (j.kind === 'run' || j.kind === 'resume'));
}

async function pollGlobal() {
  try {
    await Promise.all([refreshJobs(true), refreshOverview(true)]);
  } catch { /* shown in the status line as "server unreachable" */ }
  schedule('global:poll', pollGlobal, anyJobRunning() ? 3000 : 10000);
}

function selectRun(id, { rerender = true } = {}) {
  if (!id || id === state.runId) { renderRunSelect(); return; }
  state.runId = id;
  try { localStorage.setItem(LS_KEY, id); } catch { /* storage unavailable: not needed to work */ }
  renderRunSelect();
  if (!state.insp.current || !RUNLESS_KINDS.has(state.insp.current.kind)) inspectorEmpty();
  state.review.open.clear();
  if (rerender) route();
}

/* ================================================================ router */
const VIEWS = {
  garage: viewGarage,
  servers: viewServers,
  run: viewRun,
  ontology: viewOntology,
  dyno: viewDyno,
  gearbox: viewGearbox,
  graph: viewGraph,
  documents: viewDocuments,
  doc: viewDocuments,
  review: viewReview,
  data: viewData,
};
// v1/v2 deep links keep working: they land on the view that took their job over.
const LEGACY_VIEWS = { pipeline: 'garage', models: 'garage', runs: 'run', control: 'run', overview: 'run', metrics: 'dyno' };
const EXPLORE_VIEWS = new Set(['graph', 'documents', 'review']);
const VIEW_TITLES = { garage: 'Garage', servers: 'Servers', run: 'Run', dyno: 'Dyno', gearbox: 'Gearbox', ontology: 'Ontology', graph: 'Graph', documents: 'Documents', review: 'Review queue', data: 'Data' };

function parseHash() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map((p) => {
    try { return decodeURIComponent(p); } catch { return p; }
  });
  if (LEGACY_VIEWS[parts[0]]) {
    const to = LEGACY_VIEWS[parts[0]];
    try { history.replaceState(null, '', `#/${to}`); } catch { /* not needed to work */ }
    return { name: to, params: [] };
  }
  const name = VIEWS[parts[0]] ? parts[0] : 'garage';
  return { name, params: parts.slice(1) };
}

// The Explore views render into #exploreBody, under one tab strip; the rest own the whole workspace.
function viewBox() { return $('#exploreBody') || $('#workspace'); }

function route() {
  const { name, params } = parseHash();
  cancelViewTimers();
  leaveGraph();
  leaveTrees();
  state.view = name === 'doc' ? 'documents' : name;
  const seq = ++state.seq;
  const navView = EXPLORE_VIEWS.has(state.view) ? 'explore' : state.view;
  // an empty inspector folds away on the wide workshop views; Explore brings it back (it shows what you click)
  if (window.innerWidth > 900) {
    if (EXPLORE_VIEWS.has(state.view)) { if (state.inspUser !== 'closed') openInspector(); }
    else if (!state.insp.current && state.inspUser !== 'open' && window.innerWidth < 1600) collapseInspector();
  }
  document.querySelectorAll('.sidenav a').forEach((a) => {
    if (a.dataset.view === navView) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  document.title = `${VIEW_TITLES[state.view] || 'Synth'} · Synth Console`;
  const ws = $('#workspace');
  ws.scrollTop = 0;
  if (EXPLORE_VIEWS.has(state.view)) {
    const run = currentRun();
    ws.innerHTML = `<div class="explore">
      <div class="explore-head">
        <nav class="view-tabs explore-tabs" aria-label="Explore">
          ${[['graph', 'Graph'], ['documents', 'Documents'], ['review', 'Review']].map(([v, l]) => `<a href="#/${v}"${v === state.view ? ' aria-current="page"' : ''}>${esc(l)}</a>`).join('')}
        </nav>
        <span class="muted explore-run">${run ? `run <span class="mono ink2">${esc(shortId(run.run_id))}</span> · ${esc(engineName(run.arm))} · ${esc(run.profile || '—')}` : 'no run selected'}</span>
      </div>
      <div class="explore-body" id="exploreBody"></div>
    </div>`;
  }
  Promise.resolve(VIEWS[name](params, () => seq === state.seq)).catch((e) => {
    if (seq === state.seq) viewBox().innerHTML = `<div class="view">${errorBox(e, 'This view failed to render')}</div>`;
    console.error(e);
  });
}

/* ================================================================ view: graph */
let cyLoad = null;
function loadCytoscape() {
  if (window.cytoscape) { state.cyStatus = 'ready'; return Promise.resolve(true); }
  if (cyLoad) return cyLoad;
  state.cyStatus = 'loading';
  cyLoad = new Promise((resolve) => {
    let settled = false;
    const s = document.createElement('script');
    const done = (ok) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      state.cyStatus = ok && typeof window.cytoscape === 'function' ? 'ready' : 'failed';
      if (state.cyStatus === 'failed') s.remove();
      renderStatusbar();
      resolve(state.cyStatus === 'ready');
    };
    const timer = setTimeout(() => done(false), 12000);
    s.src = CY_URL;
    s.async = true;
    s.onload = () => done(true);
    s.onerror = () => done(false);
    document.head.appendChild(s);
  });
  return cyLoad;
}

function leaveGraph() {
  if (state.cyObserver) { state.cyObserver.disconnect(); state.cyObserver = null; }
  if (state.cy) {
    savePositions();
    state.cy.destroy();
    state.cy = null;
  }
}
function savePositions() {
  if (!state.cy || !state.cy.__runId) return;
  const pos = {};
  state.cy.nodes().forEach((n) => { pos[n.id()] = { ...n.position() }; });
  state.positions[state.cy.__runId] = pos;
}

function graphVisible(g) {
  const ui = state.graphUi;
  const okNodes = new Set(g.data.nodes.filter((n) => !ui.hiddenTypes.has(n.type || 'unknown')).map((n) => n.id));
  const edges = g.data.edges.filter((e) => ui.statuses.has(e.status) && !ui.hiddenRels.has(e.rel)
    && (Number(e.confidence ?? 0) >= ui.minConf || ui.minConf === 0) && okNodes.has(e.source) && okNodes.has(e.target));
  let nodes = okNodes;
  if (ui.hideIsolated) {
    nodes = new Set();
    for (const e of edges) { nodes.add(e.source); nodes.add(e.target); }
  }
  return { nodes, edges };
}

async function viewGraph(_params, alive) {
  const ws = viewBox();
  if (!state.runId) { ws.innerHTML = `<div class="view">${emptyState('No run selected', NO_RUN_HINT)}</div>`; return; }
  ws.innerHTML = `<div class="view fill">
    <aside class="rail" id="gRail" aria-label="Graph filters">${skeleton(6)}</aside>
    <div class="graph-main">
      <div class="graph-toolbar" role="toolbar" aria-label="Graph tools">
        <input type="search" id="gSearch" placeholder="Search entities…" aria-label="Search entities by name" value="${esc(state.graphUi.search)}">
        <button type="button" class="btn sm" data-action="graph-search">Find</button>
        <span class="muted" id="gSearchInfo" aria-live="polite"></span>
        <span class="spacer"></span>
        <span class="muted num" id="gCounts"></span>
        <button type="button" class="btn sm" data-action="graph-layout" title="Re-run the force-directed (cose) layout">Layout</button>
        <button type="button" class="btn sm" data-action="graph-fit">Fit</button>
        <button type="button" class="btn sm" data-action="graph-clear" title="Clear search and neighbourhood focus">Clear focus</button>
      </div>
      <div class="graph-canvas-wrap" id="gWrap"><div class="graph-overlay">${skeleton(5)}</div></div>
    </div>
  </div>`;
  let g;
  try {
    [g] = await Promise.all([getGraph(state.runId, { quiet: true }), loadCytoscape(), ensureDocuments()]);
  } catch (e) {
    if (alive()) $('#gWrap').innerHTML = `<div class="graph-overlay">${errorBox(e, 'Could not load the graph')}</div>`;
    return;
  }
  if (!alive()) return;
  renderGraphRail(g);
  if (!g.data.nodes.length) {
    $('#gWrap').innerHTML = `<div class="graph-overlay">${emptyState('This run has no active entities', '<div>Entities appear after the GLINER and CONTRACT stages.</div>')}</div>`;
    updateGraphCounts(g);
    return;
  }
  if (state.cyStatus === 'ready') buildCy(g);
  else renderGraphFallback(g);
  updateGraphCounts(g);
  wireGraphInputs(g);
  if (state.graphUi.search) runGraphSearch(false);
}

function renderGraphRail(g) {
  const ui = state.graphUi;
  const typeCounts = {};
  g.data.nodes.forEach((n) => { const t = n.type || 'unknown'; typeCounts[t] = (typeCounts[t] || 0) + 1; });
  const types = [...TYPE_ORDER.filter((t) => typeCounts[t]), ...Object.keys(typeCounts).filter((t) => !TYPE_ORDER.includes(t)).sort()];
  const stCounts = {};
  const relCounts = {};
  g.data.edges.forEach((e) => {
    stCounts[e.status] = (stCounts[e.status] || 0) + 1;
    relCounts[e.rel] = (relCounts[e.rel] || 0) + 1;
  });
  const statuses = [...EDGE_STATUSES, ...Object.keys(stCounts).filter((s) => !EDGE_STATUSES.includes(s))];
  const rels = Object.keys(relCounts).sort();
  const check = (group, value, checked, label, count, color) => `<label class="check">
      <input type="checkbox" data-filter="${group}" value="${esc(value)}"${checked ? ' checked' : ''}>
      ${color ? `<i class="swatch" style="--c:${color}"></i>` : ''}<span class="lbl" title="${esc(label)}">${esc(label)}</span><span class="num">${esc(count)}</span>
    </label>`;
  $('#gRail').innerHTML = `
    <fieldset><legend><span class="cap">Entity types</span></legend>
      ${types.map((t) => check('type', t, !ui.hiddenTypes.has(t), t, typeCounts[t], typeColor(t))).join('')}
    </fieldset>
    <fieldset><legend><span class="cap">Edge status</span></legend>
      ${statuses.map((s) => check('status', s, ui.statuses.has(s), s, stCounts[s] || 0, statusColor(s))).join('')}
    </fieldset>
    <fieldset><legend><span class="cap">Min confidence</span></legend>
      <div class="range-row"><input type="range" id="gConf" min="0" max="1" step="0.05" value="${ui.minConf}" aria-label="Minimum edge confidence"><span class="num" id="gConfV">${ui.minConf.toFixed(2)}</span></div>
    </fieldset>
    <fieldset><legend><span class="cap">Relations</span><span class="spacer"></span>
        <button type="button" class="linkish" data-action="rels-all">all</button><button type="button" class="linkish" data-action="rels-none">none</button></legend>
      <div class="rel-list">${rels.length ? rels.map((x) => check('rel', x, !ui.hiddenRels.has(x), x, relCounts[x])).join('') : '<span class="muted">no relations</span>'}</div>
    </fieldset>
    <fieldset><legend><span class="cap">Options</span></legend>
      <label class="check"><input type="checkbox" id="gIsolated"${ui.hideIsolated ? ' checked' : ''}><span class="lbl">Hide entities without visible edges</span></label>
    </fieldset>
    <button type="button" class="btn sm" data-action="graph-reset">Reset filters</button>`;
}

function wireGraphInputs(g) {
  const rail = $('#gRail');
  rail.addEventListener('change', (e) => {
    const t = e.target;
    const ui = state.graphUi;
    if (t.dataset.filter === 'type') { if (t.checked) ui.hiddenTypes.delete(t.value); else ui.hiddenTypes.add(t.value); }
    if (t.dataset.filter === 'status') { if (t.checked) ui.statuses.add(t.value); else ui.statuses.delete(t.value); }
    if (t.dataset.filter === 'rel') { if (t.checked) ui.hiddenRels.delete(t.value); else ui.hiddenRels.add(t.value); }
    if (t.id === 'gIsolated') ui.hideIsolated = t.checked;
    applyGraphFilters(g);
  });
  const conf = $('#gConf');
  const onConf = debounce(() => applyGraphFilters(g), 80);
  conf.addEventListener('input', () => {
    state.graphUi.minConf = Number(conf.value);
    $('#gConfV').textContent = state.graphUi.minConf.toFixed(2);
    onConf();
  });
  const search = $('#gSearch');
  search.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); runGraphSearch(true); } });
  search.addEventListener('input', debounce(() => runGraphSearch(false), 250));
}

function updateGraphCounts(g) {
  const v = graphVisible(g);
  const el = $('#gCounts');
  if (el) el.textContent = `${v.nodes.size}/${g.data.nodes.length} nodes · ${v.edges.length}/${g.data.edges.length} edges`;
}

function applyGraphFilters(g) {
  const v = graphVisible(g);
  if (state.cy) {
    const visEdges = new Set(v.edges.map((e) => e.id));
    state.cy.batch(() => {
      state.cy.nodes().forEach((n) => { n.toggleClass('hidden', !v.nodes.has(n.id())); });
      state.cy.edges().forEach((e) => { e.toggleClass('hidden', !visEdges.has(e.id())); });
    });
  } else if (state.cyStatus !== 'ready') {
    renderGraphFallback(g);
  }
  updateGraphCounts(g);
}

function nodeSize(m) { return Math.min(58, 16 + 7 * Math.sqrt(Number(m) || 0)); }

function buildCy(g) {
  const wrap = $('#gWrap');
  wrap.innerHTML = `<div id="cy" role="img" aria-label="Knowledge graph. Click a node or edge to inspect it; double-click a node to focus its neighbourhood."></div>
    <div class="graph-legend" aria-hidden="true">
      <div class="row">${TYPE_ORDER.filter((t) => g.data.nodes.some((n) => n.type === t)).map((t) => `<span><i class="swatch" style="--c:${typeColor(t)}"></i>${esc(t)}</span>`).join('')}</div>
      <div class="row">
        <span><i class="line-key" style="--c:${STATUS_COLORS.COMMITTED}"></i>committed</span>
        <span><i class="line-key" style="--c:${STATUS_COLORS.VERIFIED}"></i>verified</span>
        <span><i class="line-key dashed" style="--c:${STATUS_COLORS.ESCALATED}"></i>escalated</span>
        <span><i class="line-key dashed" style="--c:${STATUS_COLORS.HELD_FOR_HUMAN}"></i>held</span>
        <span><i class="line-key faded" style="--c:${STATUS_COLORS.REJECTED}"></i>rejected</span>
        <span style="color:#ff8a8f">⊣ ¬ negated</span>
      </div>
    </div>`;
  const ids = new Set(g.data.nodes.map((n) => n.id));
  const elements = [
    ...g.data.nodes.map((n) => ({ group: 'nodes', data: { id: n.id, label: n.label || n.id, color: typeColor(n.type), size: nodeSize(n.mentions) } })),
    ...g.data.edges.filter((e) => ids.has(e.source) && ids.has(e.target)).map((e) => ({
      group: 'edges',
      data: { id: e.id, source: e.source, target: e.target, label: (e.negated ? '¬ ' : '') + (e.rel || ''), color: statusColor(e.status), width: 1 + 2.2 * Math.max(0, Math.min(1, Number(e.confidence ?? 0.5))) },
      classes: `st-${String(e.status || '').replace(/[^A-Z_]/gi, '')}${e.negated ? ' neg' : ''}`,
    })),
  ];
  const saved = state.positions[state.runId];
  const havePositions = saved && g.data.nodes.every((n) => saved[n.id]);
  const cy = window.cytoscape({
    container: $('#cy'),
    elements,
    minZoom: 0.08,
    maxZoom: 4,
    boxSelectionEnabled: false,
    style: [
      { selector: 'node', style: {
        'background-color': 'data(color)', width: 'data(size)', height: 'data(size)', label: 'data(label)',
        color: '#dbe3ea', 'font-size': 10, 'font-family': 'system-ui, Segoe UI, Roboto, sans-serif',
        'text-valign': 'bottom', 'text-margin-y': 4, 'text-outline-color': '#0f1419', 'text-outline-width': 2,
        'text-wrap': 'ellipsis', 'text-max-width': 140, 'border-width': 1.5, 'border-color': '#0f1419', 'min-zoomed-font-size': 4,
      } },
      { selector: 'edge', style: {
        width: 'data(width)', 'line-color': 'data(color)', 'target-arrow-color': 'data(color)', 'target-arrow-shape': 'triangle',
        'arrow-scale': 0.8, 'curve-style': 'bezier', label: 'data(label)', 'font-size': 8, color: '#a9b6c2',
        'text-rotation': 'autorotate', 'text-outline-color': '#0f1419', 'text-outline-width': 2, 'min-zoomed-font-size': 7,
        'font-family': 'ui-monospace, Consolas, monospace',
      } },
      { selector: 'edge.st-ESCALATED, edge.st-HELD_FOR_HUMAN', style: { 'line-style': 'dashed', 'line-dash-pattern': [6, 4] } },
      { selector: 'edge.st-REJECTED', style: { opacity: 0.32 } },
      { selector: 'edge.neg', style: { 'target-arrow-shape': 'tee', 'target-arrow-color': '#e5484d', color: '#ff8a8f', 'arrow-scale': 1.1 } },
      { selector: 'node:selected', style: { 'border-width': 3, 'border-color': '#ffffff' } },
      { selector: 'edge:selected', style: { 'overlay-color': '#ffffff', 'overlay-opacity': 0.18, 'overlay-padding': 4, width: 4 } },
      { selector: 'node.match', style: { 'border-width': 3, 'border-color': '#ffd84d', 'font-weight': 'bold', 'z-index': 10 } },
      { selector: '.dim', style: { opacity: 0.1 } },
      { selector: '.hidden', style: { display: 'none' } },
    ],
    layout: havePositions ? { name: 'preset', positions: (n) => saved[n.id()], fit: true, padding: 30 } : coseOptions(),
  });
  cy.__runId = state.runId;
  state.cy = cy;
  applyGraphFilters(g);
  if (!havePositions) cy.fit(cy.elements().not('.hidden'), 30);

  cy.on('tap', 'node', (e) => inspect({ kind: 'entity', id: e.target.id() }));
  cy.on('tap', 'edge', (e) => inspect({ kind: 'edge', id: e.target.id() }));
  cy.on('dbltap', 'node', (e) => focusNeighbourhood(e.target));
  cy.on('tap', (e) => { if (e.target === cy) clearGraphFocus(false); });
  cy.on('layoutstop', savePositions);
  state.cyObserver = new ResizeObserver(() => { if (state.cy) state.cy.resize(); });
  state.cyObserver.observe($('#gWrap'));
}

function coseOptions() {
  return { name: 'cose', animate: false, randomize: true, padding: 30, nodeRepulsion: () => 9000, idealEdgeLength: () => 110, edgeElasticity: () => 80, gravity: 0.6, numIter: 1500, nodeOverlap: 24, componentSpacing: 70, fit: true };
}

function renderGraphFallback(g) {
  const v = graphVisible(g);
  const label = (id) => (g.nodes.get(id) || {}).label || shortId(id);
  const nodes = g.data.nodes.filter((n) => v.nodes.has(n.id)).sort((a, b) => (b.mentions || 0) - (a.mentions || 0));
  $('#gWrap').innerHTML = `<div class="graph-overlay">
    <div class="error-box" style="margin-bottom:12px"><b>Graph drawing unavailable.</b> Cytoscape.js could not be loaded from <code>/static/vendor/</code>.
      The filters still apply to the tables below. <button type="button" class="btn sm" data-action="retry-cy">Retry</button></div>
    <section class="panel" style="margin-bottom:12px"><div class="panel-head"><span class="cap">Edges (${v.edges.length})</span></div>
      <div class="table-wrap"><table class="data"><thead><tr><th>Source</th><th>Relation</th><th>Target</th><th>Status</th><th class="r">Conf</th><th>Time</th></tr></thead><tbody>
      ${v.edges.map((e) => `<tr class="clickable" data-action="focus-edge" data-id="${esc(e.id)}">
        <td><button type="button" class="linkish ink" data-action="inspect" data-id="${esc(e.source)}">${esc(label(e.source))}</button></td>
        <td class="mono">${e.negated ? '<span style="color:var(--bad)">¬ </span>' : ''}${esc(e.rel)}</td>
        <td><button type="button" class="linkish ink" data-action="inspect" data-id="${esc(e.target)}">${esc(label(e.target))}</button></td>
        <td>${statusChip(e.status)}</td><td class="r num">${esc(fmtConf(e.confidence))}</td><td>${timeChips(e.time)}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">No edges match the filters.</td></tr>'}
      </tbody></table></div></section>
    <section class="panel"><div class="panel-head"><span class="cap">Entities (${nodes.length})</span></div>
      <div class="table-wrap"><table class="data"><thead><tr><th>Name</th><th>Type</th><th class="r">Mentions</th><th class="r">Docs</th></tr></thead><tbody>
      ${nodes.map((n) => `<tr class="clickable" data-action="inspect" data-id="${esc(n.id)}"><td>${esc(n.label)}</td><td>${typeChip(n.type)}</td><td class="r num">${esc(fmtNum(n.mentions))}</td><td class="r num">${esc((n.docs || []).length)}</td></tr>`).join('')}
      </tbody></table></div></section>
  </div>`;
}

function focusNeighbourhood(node) {
  const cy = state.cy;
  if (!cy) return;
  const hood = node.closedNeighborhood().not('.hidden');
  cy.batch(() => { cy.elements().addClass('dim'); hood.removeClass('dim'); });
  cy.animate({ fit: { eles: hood, padding: 70 } }, { duration: 300 });
  const info = $('#gSearchInfo');
  if (info) info.textContent = `Neighbourhood of ${node.data('label')} — click empty space to clear`;
}

function clearGraphFocus(clearSearch = true) {
  const cy = state.cy;
  if (cy) cy.batch(() => { cy.elements().removeClass('dim'); if (clearSearch) cy.nodes().removeClass('match'); });
  if (clearSearch) {
    state.graphUi.search = '';
    const s = $('#gSearch');
    if (s) s.value = '';
  }
  const info = $('#gSearchInfo');
  if (info) info.textContent = '';
}

function runGraphSearch(zoom) {
  const input = $('#gSearch');
  const q = (input ? input.value : '').trim().toLowerCase();
  state.graphUi.search = input ? input.value : '';
  const info = $('#gSearchInfo');
  const g = state.graphs[state.runId];
  if (!g) return;
  if (!q) { clearGraphFocus(true); return; }
  const hits = g.data.nodes.filter((n) => String(n.label || '').toLowerCase().includes(q) || n.id.toLowerCase() === q);
  if (info) info.textContent = hits.length ? `${hits.length} match${hits.length > 1 ? 'es' : ''}` : 'No match';
  const cy = state.cy;
  if (!cy) return;
  const ids = new Set(hits.map((n) => n.id));
  cy.batch(() => {
    cy.nodes().forEach((n) => { n.toggleClass('match', ids.has(n.id())); n.toggleClass('dim', ids.size > 0 && !ids.has(n.id())); });
    cy.edges().forEach((e) => e.toggleClass('dim', ids.size > 0 && !ids.has(e.data('source')) && !ids.has(e.data('target'))));
  });
  const eles = cy.nodes('.match').not('.hidden');
  if (eles.length && zoom) {
    cy.animate({ fit: { eles, padding: 90 } }, { duration: 300 });
    if (eles.length === 1) { cy.elements().unselect(); eles.select(); inspect({ kind: 'entity', id: eles[0].id() }); }
  } else if (eles.length < hits.length && info) {
    info.textContent += ` (${hits.length - eles.length} hidden by filters)`;
  }
}

function focusEdge(id) {
  inspect({ kind: 'edge', id });
  const cy = state.cy;
  if (!cy) return;
  const e = cy.getElementById(id);
  if (!e || !e.length) return;
  if (e.hasClass('hidden')) { toast('Edge hidden by filters', 'Enable its status / relation in the filter rail to see it on the canvas.'); return; }
  cy.elements().unselect();
  e.select();
  cy.animate({ fit: { eles: e.connectedNodes().union(e), padding: 120 } }, { duration: 300 });
}

/* ================================================================ view: documents */
async function ensureDocuments(force = false) {
  if (!state.documents || force) state.documents = (await API.documents({ quiet: true })) || [];
  return state.documents;
}

async function viewDocuments(params, alive) {
  const [docId, idxRaw] = params;
  const idx = idxRaw !== undefined && idxRaw !== '' && !Number.isNaN(Number(idxRaw)) ? Number(idxRaw) : null;
  const ws = viewBox();
  ws.innerHTML = `<div class="view fill">
    <nav class="doc-list" id="docList" aria-label="Documents">${skeleton(6)}</nav>
    <section class="reader" id="reader" aria-label="Reading pane"><div class="reader-body" style="padding:16px">${skeleton(8)}</div></section>
  </div>`;
  let docs;
  try {
    docs = await ensureDocuments(true);
  } catch (e) {
    if (alive()) $('#docList').innerHTML = `<div style="padding:12px">${errorBox(e, 'Could not list documents')}</div>`;
    docs = [];
  }
  if (!alive()) return;
  $('#docList').innerHTML = docs.length ? docs.map((d) => `
    <button type="button" class="doc-item" data-action="open-doc" data-doc="${esc(d.doc_id)}" aria-current="${d.doc_id === docId}">
      <span class="fn">${esc(d.filename || d.doc_id)}${d.sovereign ? chip('SOVEREIGN', '#ec835a', 'solid', 'Never leaves the machine') : ''}</span>
      <span class="meta"><span title="document date (${esc(d.doc_date_source || 'unknown source')})">${esc(fmtDate(d.doc_date))} <span class="muted">${esc(d.doc_date_source || '')}</span></span>
        <span>${esc(fmtNum(d.pages))} p</span><span>${esc(fmtNum(d.sentences))} sent</span><span>${esc(d.status || '')}</span></span>
    </button>`).join('') : `<div style="padding:12px">${emptyState('No documents', '<div>Upload and ingest documents in <a href="#/run">Run</a>.</div>')}</div>`;

  const reader = $('#reader');
  if (!docId) {
    reader.innerHTML = `<div class="reader-body" style="padding:16px">${emptyState('Select a document', '<div>Highlights and the coverage gutter show what the selected run extracted from each sentence.</div>')}</div>`;
    return;
  }
  if (!state.runId) {
    reader.innerHTML = `<div class="reader-body" style="padding:16px">${emptyState('No run selected', NO_RUN_HINT)}</div>`;
    return;
  }
  let doc;
  try {
    [doc] = await Promise.all([getDoc(state.runId, docId, { quiet: true }), getGraph(state.runId, { quiet: true }).catch(() => null)]);
  } catch (e) {
    if (alive()) reader.innerHTML = `<div class="reader-body" style="padding:16px">${errorBox(e, 'Could not load this document for the selected run')}</div>`;
    return;
  }
  if (!alive()) return;
  renderReader(reader, doc, docId, idx);
}

function renderReader(reader, doc, docId, idx) {
  const g = state.graphs[state.runId];
  const text = doc.text || '';
  const sents = (doc.sentences || []).slice().sort((a, b) => a.start - b.start);
  const hls = doc.highlights || [];
  const meta = doc.document || {};
  const covCounts = {};
  sents.forEach((s) => { const c = s.coverage || 'none'; covCounts[c] = (covCounts[c] || 0) + 1; });
  const covKeys = [...COVERAGE_ORDER.filter((k) => covCounts[k]), ...Object.keys(covCounts).filter((k) => !COVERAGE_ORDER.includes(k))];
  const typesPresent = new Set();
  hls.forEach((h) => { if (h.kind === 'entity') typesPresent.add((nodeFor(g, h.object_id) || {}).type || 'unknown'); });

  let html = '';
  let prev = 0;
  for (const s of sents) {
    const gap = text.slice(prev, s.start);
    if (gap.trim()) html += `<div class="sent filtered"><span class="cov" style="--c:transparent"></span><span></span><span class="idx"></span><div class="txt">${esc(gap.trim())}</div></div>`;
    prev = Math.max(prev, s.end);
    const own = hls.filter((h) => h.start < s.end && h.end > s.start);
    const facts = dedupe(own.filter((h) => h.kind === 'fact'));
    const covColor = COVERAGE_COLORS[s.coverage] || '#2a3642';
    html += `<div class="sent${s.filtered ? ' filtered' : ''}" id="s-${esc(s.idx)}" data-idx="${esc(s.idx)}">
      <span class="cov" style="--c:${covColor}" title="${esc(s.coverage || 'no terminal outcome')}"></span>
      ${facts.length
    ? `<button type="button" class="fact-mark on" data-action="sentence" data-idx="${esc(s.idx)}" aria-label="${facts.length} fact${facts.length > 1 ? 's' : ''} in sentence ${esc(s.idx)}" title="${esc(facts.map((f) => `${f.label || f.object_id}${f.status ? ` [${f.status}]` : ''}`).join('\n'))}"></button>`
    : '<span class="fact-mark"></span>'}
      <button type="button" class="idx" data-action="sentence" data-idx="${esc(s.idx)}" aria-label="Inspect sentence ${esc(s.idx)}">s${esc(s.idx)}</button>
      <div class="txt">${renderSentenceText(text, s, own, g)}</div>
    </div>`;
  }
  if (text.slice(prev).trim()) html += `<div class="sent filtered"><span class="cov" style="--c:transparent"></span><span></span><span class="idx"></span><div class="txt">${esc(text.slice(prev).trim())}</div></div>`;

  reader.innerHTML = `
    <div class="reader-head">
      <div class="title"><h2>${esc(meta.filename || docId)}</h2>${meta.sovereign ? chip('SOVEREIGN', '#ec835a', 'solid') : ''}
        <span class="muted mono" style="font-size:11px">${esc(fmtDate(meta.doc_date))} (${esc(meta.doc_date_source || '—')}) · ${esc(fmtNum(meta.pages))} pages · ${esc(fmtNum(sents.length))} sentences · run ${esc(shortId(state.runId))}</span></div>
      <div class="legend-row" aria-label="Coverage legend"><span class="cap">Coverage</span>${covKeys.map((k) => `<span><i class="key" style="--c:${COVERAGE_COLORS[k] || '#2a3642'}"></i>${esc(k)} <span class="num muted">${esc(covCounts[k])}</span></span>`).join('')}</div>
      <div class="legend-row" aria-label="Highlight legend"><span class="cap">Highlights</span>${[...typesPresent].map((t) => `<span><i class="key" style="--c:${typeColor(t)}"></i>${esc(t)}</span>`).join('')}<span><i class="ul"></i>time</span><span><i class="key" style="--c:var(--ink-2);width:3px"></i>fact in sentence</span></div>
    </div>
    <div class="reader-body" id="readerBody">${sents.length ? html : `<div style="padding:16px">${emptyState('No sentences', '<div>This document has not been segmented.</div>')}</div>`}</div>`;
  reader.__doc = { runId: state.runId, docId };
  if (idx !== null) {
    const el = document.getElementById(`s-${idx}`);
    if (el) {
      el.scrollIntoView({ block: 'center' });
      el.classList.add('flash');
      inspect({ kind: 'sentence', docId, idx });
    }
  }
}

function dedupe(list) {
  const seen = new Set();
  return list.filter((h) => (seen.has(h.object_id) ? false : seen.add(h.object_id)));
}

// Split the sentence at every entity/time boundary; each piece carries the innermost highlight of each kind.
function renderSentenceText(text, s, own, g) {
  const spans = own.filter((h) => h.kind === 'entity' || h.kind === 'time');
  const raw = text.slice(s.start, s.end);
  if (!spans.length) return esc(raw);
  const cuts = new Set([s.start, s.end]);
  spans.forEach((h) => { cuts.add(Math.max(s.start, h.start)); cuts.add(Math.min(s.end, h.end)); });
  const pts = [...cuts].sort((a, b) => a - b);
  let out = '';
  for (let i = 0; i < pts.length - 1; i += 1) {
    const a = pts[i];
    const b = pts[i + 1];
    const piece = text.slice(a, b);
    const cover = spans.filter((h) => h.start <= a && h.end >= b);
    if (!cover.length) { out += esc(piece); continue; }
    const inner = (kind) => cover.filter((h) => h.kind === kind).sort((x, y) => (x.end - x.start) - (y.end - y.start))[0];
    const ent = inner('entity');
    const tim = inner('time');
    const target = ent || tim;
    const type = ent ? ((nodeFor(g, ent.object_id) || {}).type || 'unknown') : null;
    const title = [ent ? `${ent.label || ''} (${type})` : '', tim ? `time: ${tim.label || ''}` : ''].filter(Boolean).join(' · ');
    out += `<span class="hl${ent ? ' ent' : ''}${tim ? ' time' : ''}" role="button" tabindex="0" data-action="hl" data-oid="${esc(target.object_id)}"${ent ? ` style="--c:${typeColor(type)}"` : ''} title="${esc(title)}">${esc(piece)}</span>`;
  }
  return out;
}

function openDoc(docId, idx) {
  const target = `#/doc/${enc(docId)}${idx !== undefined && idx !== null && idx !== '' ? `/${enc(idx)}` : ''}`;
  if (location.hash === target) route();
  else location.hash = target;
}

/* ================================================================ view: review queue */
async function viewReview(_params, alive) {
  const ws = viewBox();
  if (!state.runId) { ws.innerHTML = `<div class="view">${emptyState('No run selected', NO_RUN_HINT)}</div>`; return; }
  ws.innerHTML = `<div class="view">
    <div class="view-head"><h1>Review queue</h1><span class="muted">escalations sent to the frontier for run <span class="mono">${esc(shortId(state.runId))}</span></span></div>
    <div class="filter-bar" id="revFilters" role="group" aria-label="Filter by outcome"></div>
    <section class="panel"><div class="panel-body flush" id="revTable">${skeleton(6)}</div></section>
  </div>`;
  try {
    state.review.rows = (await API.escalations(state.runId, { quiet: true })) || [];
  } catch (e) {
    if (alive()) $('#revTable').innerHTML = `<div class="panel-body">${errorBox(e, 'Could not load escalations')}</div>`;
    return;
  }
  if (!alive()) return;
  renderReview();
}

function renderReview() {
  const rows = state.review.rows || [];
  const counts = { ALL: rows.length };
  rows.forEach((x) => { counts[x.outcome] = (counts[x.outcome] || 0) + 1; });
  const outcomes = ['ALL', ...ESC_OUTCOMES, ...Object.keys(counts).filter((k) => k !== 'ALL' && !ESC_OUTCOMES.includes(k))];
  $('#revFilters').innerHTML = `<span class="cap" style="margin-right:6px">Outcome</span>` + outcomes.map((o) =>
    `<button type="button" class="chip" style="--c:${o === 'ALL' ? '#a9b6c2' : statusColor(o)}" data-action="esc-filter" data-outcome="${esc(o)}" aria-pressed="${state.review.outcome === o}"><i class="dot"></i>${esc(o)} ${esc(counts[o] || 0)}</button>`).join('');
  const shown = rows.filter((x) => state.review.outcome === 'ALL' || x.outcome === state.review.outcome);
  const box = $('#revTable');
  if (!rows.length) { box.innerHTML = `<div class="panel-body">${emptyState('Nothing escalated', '<div>The gate (U9) sent no items to the frontier tier in this run.</div>')}</div>`; return; }
  if (!shown.length) { box.innerHTML = `<div class="panel-body">${emptyState('No escalations with this outcome', '')}</div>`; return; }
  box.innerHTML = `<div class="table-wrap"><table class="data">
    <thead><tr><th style="width:28px"><span class="sr-only">Expand</span></th><th>ID</th><th>Outcome</th><th>Item</th><th>Items</th><th>Reasons</th><th>Question</th><th class="r">Packet</th><th class="r">In / out</th></tr></thead>
    <tbody>${shown.map((x) => {
    const open = state.review.open.has(x.id);
    return `<tr class="clickable${open ? ' selected' : ''}" data-action="toggle-esc" data-id="${esc(x.id)}">
        <td><button type="button" class="icon-btn" style="width:20px;height:20px" data-action="toggle-esc" data-id="${esc(x.id)}" aria-expanded="${open}" aria-label="${open ? 'Collapse' : 'Expand'} escalation ${esc(x.id)}">${open ? '▾' : '▸'}</button></td>
        <td class="mono" title="${esc(x.id)}">${esc(shortId(x.id))}</td>
        <td>${statusChip(x.outcome)}</td>
        <td>${plainChip(x.item_type)}</td>
        <td style="min-width:140px"><div class="chips">${(x.items || (x.item_ids || []).map((id) => ({ id, label: id }))).map((it) => `<button type="button" class="linkish" data-action="inspect" data-id="${esc(it.id)}" title="${esc(it.id)}">${esc(it.label || shortId(it.id))}</button>`).join('<span class="muted">·</span>')}</div></td>
        <td><div class="chips">${(x.reasons || []).map((rr) => chip(rr, '#c98500')).join('') || '<span class="muted">—</span>'}</div></td>
        <td><div class="mono" style="font-size:11px">${esc(x.question_type || '—')}</div><div class="q-cell" title="${esc(x.question || '')}">${esc(x.question || '')}</div></td>
        <td class="r num">${esc(fmtNum(x.packet_tokens))}</td>
        <td class="r num">${esc(fmtNum(x.tokens_in))} / ${esc(fmtNum(x.tokens_out))}</td>
      </tr>${open ? `<tr class="expand"><td></td><td colspan="8">
        <div style="display:grid;gap:8px;padding:4px 0 8px">
          ${x.question ? `<div><span class="cap">Question</span><div style="margin-top:3px;overflow-wrap:anywhere">${esc(x.question)}</div></div>` : ''}
          <div><span class="cap">Verdict</span><div style="margin-top:3px">${x.verdict === null || x.verdict === undefined ? '<span class="muted">No verdict (yet).</span>' : jsonPre(x.verdict)}</div></div>
          <div><button type="button" class="btn sm" data-action="inspect-obj" data-id="${esc(x.id)}">Provenance &amp; history</button></div>
        </div></td></tr>` : ''}`;
  }).join('')}</tbody></table></div>`;
}

/* ================================================================ dyno drill-down helpers (per-stage metrics, audit) */
function renderTokenChart(rows) {
  const box = $('#mChart');
  const byStage = new Map();
  rows.forEach((x) => {
    const k = x.stage || '—';
    const v = byStage.get(k) || { in: 0, out: 0 };
    v.in += x.tokens_in || 0;
    v.out += x.tokens_out || 0;
    byStage.set(k, v);
  });
  if (!byStage.size) { box.innerHTML = emptyState('No model calls recorded', '<div>Stages that call a model (MICRO, T2, FRONTIER…) log every call here.</div>'); return; }
  const data = [...byStage.entries()];
  const max = Math.max(1, ...data.map(([, v]) => v.in + v.out));
  const W = Math.max(260, Math.floor(box.clientWidth - 4)); const L = 84; const R = 64; const rowH = 24; const barH = 12;
  const H = data.length * rowH + 6;
  const sx = (v) => ((W - L - R) * v) / max;
  const bars = data.map(([stage, v], i) => {
    const y = 4 + i * rowH;
    const wIn = sx(v.in); const wOut = sx(v.out);
    const gap = wIn > 0 && wOut > 0 ? 2 : 0;
    return `<g>
      <text x="${L - 8}" y="${y + barH - 1}" text-anchor="end" class="ax">${esc(stage)}</text>
      ${wIn > 0 ? `<rect x="${L}" y="${y}" width="${Math.max(1, wIn).toFixed(1)}" height="${barH}" rx="2" fill="${SERIES.in}"><title>${esc(stage)} · tokens in ${esc(fmtNum(v.in))}</title></rect>` : ''}
      ${wOut > 0 ? `<rect x="${(L + wIn + gap).toFixed(1)}" y="${y}" width="${Math.max(1, wOut - gap).toFixed(1)}" height="${barH}" rx="2" fill="${SERIES.out}"><title>${esc(stage)} · tokens out ${esc(fmtNum(v.out))}</title></rect>` : ''}
      <text x="${(L + wIn + wOut + 6).toFixed(1)}" y="${y + barH - 1}" class="val">${esc(fmtTokens(v.in + v.out))}</text>
    </g>`;
  }).join('');
  box.innerHTML = `<svg class="bar-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Tokens by stage, in and out (see table below for exact values)">
    <style>.ax{fill:#a9b6c2;font:10.5px ui-monospace,Consolas,monospace}.val{fill:#dbe3ea;font:10.5px ui-monospace,Consolas,monospace}</style>
    <line x1="${L}" x2="${L}" y1="0" y2="${H}" stroke="#2a3642"/>${bars}</svg>`;
}

function renderMetricsTable() {
  const m = state.metrics;
  const box = $('#mTable');
  if (!m || !box) return;
  const rows = (m.by_stage || []).slice();
  if (!rows.length) { box.innerHTML = `<div class="panel-body">${emptyState('No model calls recorded for this run', '')}</div>`; return; }
  const { key, dir } = state.metricsSort;
  if (key) rows.sort((a, b) => {
    const x = a[key]; const y = b[key];
    if (typeof x === 'number' || typeof y === 'number') return dir * ((Number(x) || 0) - (Number(y) || 0));
    return dir * String(x ?? '').localeCompare(String(y ?? ''));
  });
  const maxTok = Math.max(1, ...rows.map((x) => (x.tokens_in || 0) + (x.tokens_out || 0)));
  const cols = [['stage', 'Stage'], ['endpoint', 'Endpoint'], ['model', 'Model'], ['calls', 'Calls', 1], ['tokens_in', 'Tokens in', 1], ['tokens_out', 'Tokens out', 1],
    ['reasoning_tokens', 'Reasoning', 1], ['latency_ms', 'Latency', 1], ['avg', 'Avg / call', 1], ['failures', 'Failures', 1]];
  rows.forEach((x) => { x.avg = x.calls ? (x.latency_ms || 0) / x.calls : null; });
  const t = m.totals || {};
  box.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr>
      ${cols.map(([k, l, num]) => `<th class="${num ? 'r' : ''}" aria-sort="${key === k ? (dir > 0 ? 'ascending' : 'descending') : 'none'}"><button type="button" data-action="sort" data-key="${k}">${esc(l)}${key === k ? (dir > 0 ? ' ▲' : ' ▼') : ''}</button></th>`).join('')}
      <th>Share</th></tr></thead><tbody>
    ${rows.map((x) => {
    const wi = (90 * (x.tokens_in || 0)) / maxTok; const wo = (90 * (x.tokens_out || 0)) / maxTok;
    return `<tr><td>${plainChip(x.stage)}</td><td class="mono">${esc(x.endpoint)}</td><td class="mono ellip" title="${esc(x.model || '')}">${esc(x.model || '—')}</td>
        <td class="r num">${esc(fmtNum(x.calls))}</td><td class="r num">${esc(fmtNum(x.tokens_in))}</td><td class="r num">${esc(fmtNum(x.tokens_out))}</td>
        <td class="r num">${esc(fmtNum(x.reasoning_tokens))}</td><td class="r num">${esc(fmtDur((x.latency_ms || 0) / 1000))}</td>
        <td class="r num">${x.avg === null ? '—' : esc(fmtNum(x.avg))} ms</td>
        <td class="r num" style="${x.failures ? 'color:var(--bad)' : ''}">${esc(fmtNum(x.failures))}</td>
        <td><svg width="96" height="10" aria-hidden="true"><rect x="0" y="1" width="${wi.toFixed(1)}" height="8" rx="2" fill="${SERIES.in}"/><rect x="${(wi + (wi && wo ? 2 : 0)).toFixed(1)}" y="1" width="${wo.toFixed(1)}" height="8" rx="2" fill="${SERIES.out}"/></svg></td></tr>`;
  }).join('')}
    <tr><td colspan="3"><b>Total</b></td><td class="r num"><b>${esc(fmtNum(t.calls))}</b></td><td class="r num"><b>${esc(fmtNum(t.tokens_in))}</b></td><td class="r num"><b>${esc(fmtNum(t.tokens_out))}</b></td><td></td><td class="r num"><b>${esc(fmtDur((t.latency_ms || 0) / 1000))}</b></td><td colspan="3"></td></tr>
    </tbody></table></div>`;
}

function renderAudit(loading = false) {
  const box = $('#mAudit');
  if (!box) return;
  const btn = $('#auditBtn');
  if (btn) btn.disabled = loading;
  if (loading) { box.innerHTML = '<div style="display:flex;gap:8px;align-items:center"><span class="spinner" aria-hidden="true"></span><span>Running audit — coverage, provenance, change log and budget checks. This can take a few seconds…</span></div>'; return; }
  const a = state.audits[state.runId];
  if (!a) { box.innerHTML = '<div class="muted">The audit checks the run\'s invariants (total coverage, provenance on every object, change-log rows, budgets). It is computed on demand.</div>'; return; }
  if (a.error) { box.innerHTML = errorBox(a.error, 'Audit failed to run'); return; }
  const res = a.result || {};
  const failures = res.failures || [];
  const warnings = res.warnings || {};
  const wEntries = Array.isArray(warnings) ? warnings.map((w, i) => [String(i + 1), w]) : Object.entries(warnings);
  box.innerHTML = `
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">${res.passed ? chip('PASSED', STATUS_COLORS.COMMITTED, 'solid') : chip('FAILED', STATUS_COLORS.FAILED, 'solid')}
      <span class="muted">at ${esc(a.at.toLocaleTimeString('en-GB'))} · ${esc(failures.length)} failures · ${esc(wEntries.length)} warning groups</span></div>
    ${failures.length ? `<span class="cap">Failures</span><ul class="audit-list" style="color:#f2b8ba">${failures.map((f) => `<li>${esc(typeof f === 'string' ? f : JSON.stringify(f))}</li>`).join('')}</ul>` : ''}
    ${wEntries.length ? `<span class="cap" style="display:block;margin-top:8px">Warnings</span><table class="data"><tbody>${wEntries.map(([k, v]) => `<tr><td class="mono" style="width:40%">${esc(k)}</td><td class="mono">${
    typeof v === 'number' || typeof v === 'string' ? esc(v) : Array.isArray(v) && !v.length ? '<span class="muted">none</span>' : `<details><summary>${esc(Array.isArray(v) ? `${v.length} item(s)` : 'details')}</summary>${jsonPre(v)}</details>`}</td></tr>`).join('')}</tbody></table>` : ''}
    ${!failures.length && !wEntries.length ? '<div class="muted">No failures or warnings.</div>' : ''}`;
}

async function runAudit() {
  const runId = state.runId;
  if (!runId) return;
  renderAudit(true);
  try {
    const result = await API.audit(runId);
    state.audits[runId] = { result, at: new Date() };
  } catch (e) {
    state.audits[runId] = { error: e, at: new Date() };
  }
  if (state.view === 'dyno' && state.runId === runId) renderAudit(false);
}

/* ================================================================ shared v2: profiles, presets, secrets, jobs, dialog, demo mode */
function qs(params) {
  return Object.entries(params || {}).filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `${enc(k)}=${enc(v)}`).join('&');
}
function fmtBytes(n) {
  if (n === null || n === undefined || n === '' || Number.isNaN(Number(n))) return '—';
  const v = Number(n);
  if (v < 1024) return `${v} B`;
  if (v < 1024 ** 2) return `${(v / 1024).toFixed(1)} KB`;
  if (v < 1024 ** 3) return `${(v / 1024 ** 2).toFixed(1)} MB`;
  return `${(v / 1024 ** 3).toFixed(2)} GB`;
}
const outputUrl = (path) => `/api/outputs/file?path=${enc(path)}`;

// A v1 server answers /api/profiles with {models, default}; the v2 shape has {slots, gliner, active, builtin, editable}.
function normProfile(p) {
  const slots = { ...(p.slots || {}) };
  if (!p.slots && p.models) for (const [k, v] of Object.entries(p.models)) slots[k] = { model: v };
  return {
    ...p, slots, gliner: p.gliner || null,
    active: p.active !== undefined && p.active !== null ? !!p.active : !!p.default,
    editable: p.editable !== undefined && p.editable !== null ? !!p.editable : p.builtin === false,
  };
}
async function loadProfiles(force = false) {
  if (!state.profiles || force) state.profiles = ((await API.profiles({ quiet: true })) || []).map(normProfile);
  return state.profiles;
}
function profileByName(name) { return (state.profiles || []).find((p) => p.name === name) || null; }
function activeProfile() { const ps = state.profiles || []; return ps.find((p) => p.active) || ps[0] || null; }
async function loadPresets() {
  if (!state.presets) {
    try { state.presets = (await API.presets({ quiet: true })) || []; } catch { state.presets = null; return []; }
  }
  return state.presets;
}
function presetFor(id) { return (state.presets || []).find((x) => x.id === id) || null; }
function providerLabel(id) { if (!id) return 'unset'; const p = presetFor(id); return p ? p.label : id; }
async function loadSecrets(force = false) {
  if (!state.secrets || force) {
    try { state.secrets = (await API.secrets({ quiet: true })) || []; } catch { state.secrets = state.secrets || []; }
  }
  return state.secrets;
}
function keyPresent(env, fallback = null) { return env ? secretPresent(env, fallback) : fallback; }
function secretPresent(name, fallback = null) {
  const s = (state.secrets || []).find((x) => x.name === name);
  return s ? !!s.present : fallback;
}
function slotConfigured(s, slot) {
  if (!s || !s.model) return false;
  if (slot === 'gliner') return true;
  return s.configured !== false && !String(s.model).includes('<') && !String(s.base_url || '').includes('<');
}
const ENV_RE = /^[A-Z][A-Z0-9_]{2,63}$/;

const ICON = {
  key: '<svg viewBox="0 0 16 16" class="ico" aria-hidden="true"><circle cx="5" cy="8" r="2.6"/><path d="M7.6 8h6.6M12.2 8v2.6M14.2 8v1.8"/></svg>',
  lock: '<svg viewBox="0 0 16 16" class="ico" aria-hidden="true"><rect x="3.5" y="7" width="9" height="7" rx="1"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/></svg>',
  warn: '<svg viewBox="0 0 16 16" class="ico" aria-hidden="true"><path d="M8 2l6.5 11.5h-13z"/><path d="M8 6.5v3.2M8 11.4v.6"/></svg>',
  upload: '<svg viewBox="0 0 24 24" class="ico big" aria-hidden="true"><path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 15v4.5h16V15"/></svg>',
  trash: '<svg viewBox="0 0 16 16" class="ico" aria-hidden="true"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.7 9h5.6l.7-9"/></svg>',
};
function keyStatus(local, present, env = '') {
  if (local) return `<span class="keyst na">${ICON.key}no key needed</span>`;
  if (present) return `<span class="keyst ok" title="${esc(env)}">${ICON.key}key present</span>`;
  return `<span class="keyst bad" title="${esc(env)}">${ICON.key}key missing</span>`;
}
function locBadge(local) {
  if (local === true) return '<span class="loc local" title="runs on this machine">LOCAL</span>';
  if (local === false) return '<span class="loc cloud" title="leaves the machine: sovereign documents are held for a human">CLOUD</span>';
  return '';
}
const provChip = (id, text) => `<span class="prov">${esc(text || providerLabel(id))}</span>`;

const RO_TITLE = 'Demo mode: the server is read-only (it answers 403). Restart with py -m synth console to change anything.';
function demoBanner(what = 'run control, uploads and profile edits are disabled') {
  return state.readOnly ? `<div class="banner warn"><b>DEMO · read-only.</b> The server was started with <code>--demo</code>: ${esc(what)}, so these buttons are inactive. Restart with <code>py -m synth console</code> to use them.</div>` : '';
}
// Every mutating control carries data-ctl; in demo mode they are disabled with an explanation.
function applyCtl(root = document) {
  if (!state.readOnly) return;
  root.querySelectorAll('[data-ctl]').forEach((b) => { b.disabled = true; b.title = RO_TITLE; });
}

// Smoke-style job results ({slot: entry}) → per-slot pass/fail.
function smokeVerdict(job, slots = null, required = slots) {
  let res = job && job.result;
  if (res && typeof res === 'object' && res.results && typeof res.results === 'object') res = res.results;
  if (res && typeof res === 'object' && 'reachable' in res && slots && slots.length === 1) res = { [slots[0]]: res };
  const entries = res && typeof res === 'object' ? Object.entries(res).filter(([k, v]) => v && typeof v === 'object' && (!slots || slots.includes(k))) : [];
  const per = entries.map(([slot, e]) => {
    const ok = e.reachable !== false && !e.error && e.recommended !== null && e.recommended !== undefined && e.model_listed !== false;
    let msg;
    if (ok) msg = `ok · ${e.recommended}`;
    else if (e.error) msg = String(e.error);
    else if (e.reachable === false) msg = 'endpoint unreachable';
    else if (e.model_listed === false) msg = 'model id not served by this endpoint';
    else msg = 'no structured mode worked';
    return { slot, ok, msg, entry: e };
  });
  if (!per.length) {
    const last = ((job && job.log) || []).slice(-1)[0];
    return { ok: false, per, msg: job && job.status === 'failed' ? `job failed${job.exit_code !== null && job.exit_code !== undefined ? ` (exit ${job.exit_code})` : ''}${last ? `: ${last}` : ''}` : 'no result' };
  }
  return { ok: per.every((p) => p.ok) && (required || []).every((s) => per.some((p) => p.slot === s)), per };
}

// Poll one job every 2 s until it finishes; independent of views (timer names "job:…" survive route changes).
function trackJob(jobId, { onUpdate, onDone } = {}) {
  const tick = async () => {
    let job;
    try { job = await API.job(jobId, { quiet: true }); } catch { schedule(`job:${jobId}`, tick, 4000); return; }
    if (onUpdate) onUpdate(job);
    if (job.status === 'running') schedule(`job:${jobId}`, tick, 2000);
    else if (onDone) onDone(job);
  };
  schedule(`job:${jobId}`, tick, 700);
}

// Modal dialog (native <dialog>): confirm when `fields` is empty, else returns the trimmed field values.
function askDialog({ title, html = '', fields = [], ok = 'OK', danger = false }) {
  return new Promise((resolve) => {
    const dlg = $('#dlg');
    $('#dlgTitle').textContent = title;
    $('#dlgBody').innerHTML = html + fields.map((f, i) => `<label class="dlg-field"><span>${esc(f.label)}</span>
      <input id="dlgF${i}" type="${f.type === 'password' ? 'password' : 'text'}" value="${esc(f.value || '')}"${f.pattern ? ` pattern="${esc(f.pattern)}"` : ''}${f.required === false ? '' : ' required'}
        autocomplete="${f.type === 'password' ? 'new-password' : 'off'}" spellcheck="false" placeholder="${esc(f.placeholder || '')}"${f.mono ? ' class="mono"' : ''}>
      ${f.hint ? `<small>${esc(f.hint)}</small>` : ''}</label>`).join('');
    const okBtn = $('#dlgOk');
    okBtn.textContent = ok;
    okBtn.className = `btn ${danger ? 'danger' : 'primary'}`;
    const finish = (v) => { if (dlg.open) dlg.close(); resolve(v); };
    $('#dlgForm').onsubmit = (e) => { e.preventDefault(); finish(fields.length ? fields.map((_, i) => $(`#dlgF${i}`).value.trim()) : true); };
    $('#dlgCancel').onclick = () => finish(null);
    dlg.oncancel = (e) => { e.preventDefault(); finish(null); };
    dlg.showModal();
    const first = $('#dlgF0');
    if (first) { first.focus(); first.select(); } else okBtn.focus();
  });
}

/* ================================================================ v3: engines + servers registry */
Object.assign(API, {
  engines: (o) => api('/api/engines', o),
  pipeline: (params, o) => api(`/api/pipeline?${qs(params)}`, o),
  preflight: (engine, profile) => api('/api/preflight', { method: 'POST', body: { engine, profile } }),
  servers: (o) => api('/api/servers', o),
  addServer: (body) => api('/api/servers', { method: 'POST', body }),
  editServer: (id, body) => api(`/api/servers/${enc(id)}`, { method: 'PUT', body }),
  removeServer: (id) => api(`/api/servers/${enc(id)}`, { method: 'DELETE' }),
  detect: () => api('/api/servers/detect', { method: 'POST', body: {} }),
  speedtest: (id, model) => api(`/api/servers/${enc(id)}/speedtest`, { method: 'POST', body: { model } }),
  loadModel: (id, model) => api(`/api/servers/${enc(id)}/load`, { method: 'POST', body: { model } }),
  launchers: (o) => api('/api/launchers', o),
  launch: (kind, body) => api(`/api/launchers/${enc(kind)}/start`, { method: 'POST', body }),
  unlaunch: (kind) => api(`/api/launchers/${enc(kind)}/stop`, { method: 'POST', body: {} }),
  bench: (limit, o) => api(`/api/bench?${qs({ limit })}`, o),
  ontologies: (o) => api('/api/ontologies', o),
  ontology: (n, o) => api(`/api/ontologies/${enc(n)}`, o),
  saveOntology: (n, data) => api(`/api/ontologies/${enc(n)}`, { method: 'PUT', body: { data } }),
  copyOntology: (n, name) => api(`/api/ontologies/${enc(n)}/copy`, { method: 'POST', body: { name } }),
  deleteOntology: (n) => api(`/api/ontologies/${enc(n)}`, { method: 'DELETE' }),
  parseOntology: (text) => api('/api/ontologies/parse', { method: 'POST', body: { text } }),
  validateOntology: (data, o) => api('/api/ontologies/validate', { method: 'POST', body: { data } }, o),
  archiveRun: (id, archived) => api(`/api/runs/${enc(id)}/archive`, { method: 'POST', body: { archived } }),
  gearbox: (runId, o) => api(`/api/gearbox?${qs({ run_id: runId })}`, o),
  gearCalls: (runId, stage, problems, o) => api(`/api/gearbox/calls?${qs({ run_id: runId, stage: stage || '', problems: problems ? 'true' : '' })}`, o),
  gearCall: (id, o) => api(`/api/gearbox/calls/${enc(id)}`, o),
  evalRuns: (run_ids) => api('/api/bench/eval', { method: 'POST', body: { run_ids } }),
});

async function loadEngines(force = false) {
  if (state.engines && !force) return state.engines;
  try {
    state.engines = (await API.engines({ quiet: true })) || [];
    state.enginesError = null;
  } catch (e) {
    state.enginesError = e;
    state.engines = state.engines || [];
  }
  return state.engines;
}
function engineById(id) { return (state.engines || []).find((e) => e.id === id) || null; }
function defaultEngine() { const es = state.engines || []; return es.find((e) => e.default) || es[0] || null; }
function engineName(id) { const e = engineById(id); return e ? e.name : (id || '—'); }
function engineSlot(eng, slotId) { return eng ? (eng.slots || []).find((s) => s.id === slotId) || null : null; }
const isGlinerSlot = (sl) => !!sl && (sl.kind === 'gliner' || sl.endpoint === 'gliner');
function slotCfg(p, sl) { if (!p || !sl) return null; return isGlinerSlot(sl) ? p.gliner : (p.slots || {})[sl.endpoint] || null; }
function slotReady(p, sl) { return slotConfigured(slotCfg(p, sl), isGlinerSlot(sl) ? 'gliner' : sl.endpoint); }
function engineMissing(eng, p) { return eng ? (eng.slots || []).filter((sl) => sl.required !== false && !slotReady(p, sl)) : []; }
// The endpoints a preflight must pass: the engine's required LLM slots (the server adds configured optional ones).
function requiredEndpoints(eng) { return eng ? (eng.slots || []).filter((sl) => !isGlinerSlot(sl) && sl.required !== false).map((sl) => sl.endpoint) : []; }
function endpointLabel(eng, endpoint) { const sl = eng && (eng.slots || []).find((s) => s.endpoint === endpoint); return sl ? sl.label : endpoint; }
function laneColor(eng, laneId) {
  const lanes = (eng && eng.lanes) || [];
  const i = lanes.findIndex((l) => l.id === laneId);
  if (i < 0) return GREY;
  if (i === 0) return LANE_FIRST;
  if (i === lanes.length - 1 && lanes.length > 2) return LANE_LAST;
  return LANE_PALETTE[(i - 1) % LANE_PALETTE.length];
}
function slotColor(eng, sl) {
  const n = eng && sl ? (eng.nodes || []).find((x) => x.slot === sl.id) : null;
  return n ? laneColor(eng, n.lane || n.tier) : GREY;
}
const slotTag = (eng, sl) => chip(sl.label, slotColor(eng, sl), 'solid');
// An engine the registry does not know (an old or derived arm): lanes and slots are rebuilt from its nodes.
function engineFromNodes(id, nodes) {
  const lanes = [];
  for (const n of nodes || []) { const l = n.lane || n.tier; if (l && !lanes.some((x) => x.id === l)) lanes.push({ id: l, label: l, sub: '' }); }
  const slots = [];
  for (const n of nodes || []) if (n.slot && !slots.some((s) => s.id === n.slot)) slots.push({ id: n.slot, endpoint: n.slot, label: n.label || n.slot, kind: n.kind === 'gliner' ? 'gliner' : 'llm', required: true });
  return { id, name: id || 'unknown engine', status: null, tagline: '', description: '', slots, lanes, nodes: nodes || [] };
}
function engineStatusChip(eng) { return eng && eng.status ? chip(eng.status, ENGINE_STATUS_COLORS[eng.status] || GREY) : ''; }

// servers
async function loadServers(force = false) {
  if (state.servers && !force) return state.servers;
  try {
    state.servers = (await API.servers({ quiet: true })) || [];
    state.serversError = null;
    state.serversAt = new Date();
  } catch (e) {
    state.serversError = e;
    state.servers = state.servers || null;
  }
  return state.servers || [];
}
function serverById(id) { return (state.servers || []).find((s) => s.id === id) || null; }
function normUrl(u) { return String(u || '').trim().toLowerCase().replace(/\/+$/, '').replace('://127.0.0.1', '://localhost'); }
function serverForUrl(url) { const n = normUrl(url); return n ? (state.servers || []).find((s) => normUrl(s.base_url) === n) || null : null; }
function serverModels(s) { return (s && s.status && s.status.models) || []; }
function serverState(s) {
  if (!s || !s.status) return { cls: 'off', text: 'not probed' };
  if (!s.status.reachable) return { cls: 'bad', text: s.status.error ? `unreachable · ${s.status.error}` : 'unreachable' };
  if (!serverModels(s).length) return { cls: 'warn', text: 'online · no models loaded' };
  return { cls: 'ok', text: `online · ${serverModels(s).length} model${serverModels(s).length === 1 ? '' : 's'}${s.status.latency_ms !== null && s.status.latency_ms !== undefined ? ` · ${s.status.latency_ms} ms` : ''}` };
}
function light(cls, title = '', attrs = '') { return `<span class="light ${cls}" role="img" aria-label="${esc(title)}" title="${esc(title)}"${attrs}></span>`; }
const serverLight = (s, attrs = '') => { const st = serverState(s); return light(st.cls, st.text, attrs); };
function hostOf(url) { try { return new URL(url).host; } catch { return String(url || ''); } }
const cssId = (s) => String(s || '').replace(/[^A-Za-z0-9_-]/g, '_');
// Where a slot's model lives: the matching saved server, else the base URL's host.
function slotWhereHtml(eng, sl, cfg, serverId = null) {
  if (isGlinerSlot(sl)) return `${provChip(null, 'in-process')}<span class="muted">${esc((cfg && cfg.device) || '—')} · threshold ${esc(cfg && cfg.threshold !== undefined && cfg.threshold !== null ? cfg.threshold : '—')}</span>`;
  const srv = (serverId && serverById(serverId)) || serverForUrl(cfg && cfg.base_url);
  if (srv) return `${serverLight(srv)}<span class="srv-ref" title="${esc(srv.base_url)}">${esc(srv.name || hostOf(srv.base_url))}</span>`;
  if (cfg && cfg.base_url && !String(cfg.base_url).includes('<')) return `${light('off', 'not a saved server')}<span class="srv-ref mono" title="${esc(cfg.base_url)}">${esc(hostOf(cfg.base_url))}</span>`;
  return `${light('off', 'no server')}<span class="muted">no server</span>`;
}

/* ================================================================ engine tree (inverted, generalised) */
const PIPE = { cw: 216, cwMin: 180, hg: 38, vg: 46, gutter: 98, padX: 22, padY: 20, dummyW: 10 };

function leaveTrees() {
  for (const t of Object.values(state.trees)) if (t.observer) { t.observer.disconnect(); t.observer = null; }
}
// Mount a tree into #boxId: tree = {engine, nodes, profileName, runId, live, mode: 'garage' | 'run'}.
function mountTree(boxId, tree, alive) {
  const prev = state.trees[boxId];
  tree.selected = prev && prev.engine && tree.engine && prev.engine.id === tree.engine.id ? prev.selected : null;
  tree.observer = prev && prev.observer;
  state.trees[boxId] = tree;
  const box = $(`#${boxId}`);
  if (!box) return;
  if (!tree.observer) {
    tree.observer = new ResizeObserver(debounce(() => { if (alive() && state.trees[boxId]) renderTree(boxId); }, 60));
    tree.observer.observe(box);
  }
  renderTree(boxId);
}
// Garage: the engine's stages with the build's model per slot (no run progress).
function composeNodes(eng, p) {
  return (eng.nodes || []).map((n) => {
    const sl = n.slot ? engineSlot(eng, n.slot) : null;
    const cfg = sl ? slotCfg(p, sl) || {} : null;
    return {
      ...n, model: cfg ? cfg.model || null : null, provider: cfg ? cfg.provider || null : null,
      local: sl ? (isGlinerSlot(sl) ? true : cfg.local ?? null) : null, key_present: cfg ? cfg.key_present ?? null : null,
      configured: sl ? slotReady(p, sl) : null, server_id: null, progress: null, tokens: null,
    };
  });
}

function treeLegendHtml(mode) {
  return `<div class="ps-legend" aria-label="Legend">
      ${Object.entries(KIND_COLORS).map(([k, c]) => `<span><i class="kind-key" style="--c:${c}"></i>${esc(k)}</span>`).join('')}
      ${mode === 'run' ? `<span><svg width="26" height="8" aria-hidden="true"><path d="M1 4h24" class="wire-flow on" style="stroke-width:2"/></svg>running</span>
      <span><svg width="26" height="8" aria-hidden="true"><path d="M1 4h24" class="wire done"/></svg>complete</span>` : ''}
      <span><i class="unconf-key"></i>empty slot</span>
    </div>`;
}

function treeNodeState(n, ctx) {
  const p = n.progress;
  const total = p ? Number(p.total) || 0 : 0;
  const done = p ? Number(p.done) || 0 : 0;
  const failed = p ? Number(p.failed) || 0 : 0;
  const dead = p ? Number(p.dead_letter) || 0 : 0;
  const pending = p && p.pending !== undefined && p.pending !== null ? Number(p.pending) || 0 : Math.max(0, total - done - failed - dead);
  const running = !!(ctx.live && pending > 0);
  const complete = total > 0 && pending === 0;
  const sl = n.slot ? engineSlot(ctx.engine, n.slot) : null;
  const isSlot = !!sl || n.kind === 'model' || n.kind === 'gliner';
  const cfg = sl ? slotCfg(ctx.profile, sl) : null;
  const unconf = isSlot && (ctx.hasRun ? !n.model : (n.configured === false || !n.model || String(n.model).includes('<') || (cfg && sl && !isGlinerSlot(sl) && !slotConfigured(cfg, sl.endpoint))));
  const cls = [running && 'running', complete && 'complete', (failed || dead) && 'has-fail', unconf && 'unconf', isSlot && 'slotnode',
    ctx.selected === n.id && 'selected', ctx.root(n.id) && 'root'].filter(Boolean).join(' ');
  return { total, done, failed, dead, pending, running, complete, unconf, isSlot, sl, cfg, cls };
}

function treeCardHtml(n, st, ctx) {
  const head = `<span class="pn-head"><span class="pn-id">${esc(n.id)}</span><span class="pn-label" title="${esc(n.label || '')}">${esc(n.label || '')}</span><span class="pn-kind">${esc(n.kind || '')}</span></span>`;
  let body = '';
  if (ctx.root(n.id)) {
    const files = state.corpus ? state.corpus.length : null;
    const fresh = state.corpus ? state.corpus.filter((f) => !f.ingested).length : 0;
    const docs = (state.overview || {}).documents;
    body = `<span class="pn-big"><b class="num">${esc(fmtNum(docs))}</b><span>documents stored</span></span>
      ${ctx.compact ? '' : `<span class="pn-sub">${files === null ? esc(n.detail || '') : `${esc(fmtNum(files))} file${files === 1 ? '' : 's'} in data/raw${fresh ? ` · <span style="color:var(--warn)">${esc(fresh)} not ingested</span>` : ''}`}</span>`}`;
  } else if (st.isSlot) {
    const sl = st.sl || { id: n.slot, label: n.slot || n.kind, kind: n.kind === 'gliner' ? 'gliner' : 'llm', endpoint: n.slot };
    const gl = isGlinerSlot(sl);
    const local = gl ? true : n.local === true || n.local === false ? n.local : (st.cfg ? st.cfg.local : null);
    const env = st.cfg ? st.cfg.api_key_env : null;
    const kp = keyPresent(env, n.key_present !== null && n.key_present !== undefined ? n.key_present : (st.cfg ? st.cfg.key_present : null));
    const opt = sl.required === false;
    body = `<span class="pn-slot"><span class="slot-tag" style="--c:${slotColor(ctx.engine, sl)}">${esc(sl.label)}</span>${opt ? '<span class="pn-opt">optional</span>' : ''}${locBadge(local)}</span>
      <span class="pn-model mono" title="${esc(n.model || '')}">${st.unconf ? '<span class="socket">empty socket</span>' : esc(n.model)}</span>
      ${st.unconf ? `<span class="pn-warn">${ICON.warn}${opt ? 'optional — click to plug in' : 'click to plug in a model'}</span>`
    : ctx.compact ? '' : n.recorded ? '<span class="pn-sub muted" title="the model recorded on this run; the build has changed since">as recorded on this run</span>'
      : `<span class="pn-sub">${slotWhereHtml(ctx.engine, sl, st.cfg, n.server_id)}${gl ? '' : keyStatus(local, kp, env)}</span>`}`;
  } else if (n.detail && !ctx.compact) {
    body = `<span class="pn-detail" title="${esc(n.detail)}">${esc(n.detail)}</span>`;
  }
  let prog = '';
  if (ctx.hasRun && !ctx.root(n.id)) {
    if (n.progress && st.total > 0) {
      const pct = Math.min(100, (st.done / st.total) * 100);
      const fpct = Math.min(100 - pct, ((st.failed + st.dead) / st.total) * 100);
      prog = `<span class="pn-prog"><span class="bar"><i style="width:${pct.toFixed(1)}%"></i>${fpct > 0 ? `<i class="f" style="width:${fpct.toFixed(1)}%"></i>` : ''}</span><span class="frac num">${esc(fmtNum(st.done))}/${esc(fmtNum(st.total))}</span></span>`;
      const badges = [
        st.running ? '<span class="pn-run"><i></i>running</span>' : '',
        st.failed ? chip(`${st.failed} failed`, STATUS_COLORS.FAILED) : '',
        st.dead ? chip(`${st.dead} dead`, STATUS_COLORS.DEAD_LETTER, 'solid', 'dead-lettered units') : '',
      ].join('');
      const t = n.tokens;
      const tok = t && (t.in || t.out || t.calls) ? `<span class="pn-tok num" title="tokens in / out · model calls">in ${esc(fmtTokens(t.in))} · out ${esc(fmtTokens(t.out))} · ${esc(fmtNum(t.calls))} calls</span>` : '';
      if (badges || tok) prog += `<span class="pn-meta">${badges}${tok}</span>`;
    } else {
      prog = `<span class="pn-prog none"><span class="bar"></span><span class="frac muted">${ctx.live ? 'waiting' : n.progress ? '0 units' : '—'}</span></span>`;
    }
  }
  return head + body + prog;
}

function renderTree(boxId) {
  const box = $(`#${boxId}`);
  const tree = state.trees[boxId];
  if (!box || !tree) return;
  const nodes = tree.nodes || [];
  const eng = tree.engine || engineFromNodes(null, nodes);
  if (!nodes.length) { box.innerHTML = `<div class="panel-body">${emptyState('This engine has no stages', '<div>The engine registry returned no nodes.</div>')}</div>`; box.__stage = null; return; }
  box.classList.toggle('compact', !!state.compact);
  const L = pipeLayout(nodes, eng);
  const sig = JSON.stringify([eng.id, nodes.map((n) => [n.id, n.children || []])]);
  let stage = box.__stage;
  if (!stage || stage.__sig !== sig || !box.contains(stage)) {
    box.innerHTML = `<div class="pipe-stage" role="group" aria-label="${esc(eng.name || 'Engine')} stages, root at the top">
      <div class="pipe-lanes"></div>
      <svg class="pipe-wires" aria-hidden="true"><defs>
        ${[['idle', '#53667a'], ['done', '#3f8a5c'], ['live', '#4ea1ff']].map(([k, c]) => `<marker id="${boxId}-pa-${k}" viewBox="0 0 8 8" refX="7.2" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0.4,0.6 L7.6,4 L0.4,7.4 z" fill="${c}"/></marker>`).join('')}
      </defs><g class="w-base"></g><g class="w-flow"></g></svg></div>`;
    stage = box.firstElementChild;
    stage.__sig = sig;
    stage.__cards = new Map();
    stage.__wires = new Map();
    box.__stage = stage;
  }
  const roots = new Set(nodes.filter((n) => !L.parents.get(n.id).length).map((n) => n.id));
  const ctx = { engine: eng, live: !!tree.live, profile: profileByName(tree.profileName), hasRun: tree.mode === 'run' && !!tree.runId, compact: !!state.compact, root: (id) => roots.has(id), selected: tree.selected };
  // card width: shrink a little before scrolling sideways
  const avail = box.clientWidth - PIPE.gutter - 2 * PIPE.padX;
  let cw = PIPE.cw;
  let X = pipeX(L, cw);
  if (X.width > avail) {
    cw = Math.max(PIPE.cwMin, Math.floor(cw - (X.width - avail) / Math.max(1, Math.max(...L.rows.map((r) => r.filter((id) => L.items.get(id).real).length)))));
    X = pipeX(L, cw);
  }
  const states = new Map();
  for (const n of nodes) {
    const st = treeNodeState(n, ctx);
    states.set(n.id, st);
    let el = stage.__cards.get(n.id);
    if (!el) {
      el = document.createElement('button');
      el.type = 'button';
      el.dataset.action = 'tree-node';
      el.dataset.tree = boxId;
      el.dataset.id = n.id;
      stage.appendChild(el);
      stage.__cards.set(n.id, el);
    }
    const cls = `pnode kind-${String(n.kind || 'rules').replace(/[^a-z]/g, '')} ${st.cls}`;
    if (el.className !== cls) el.className = cls;
    el.style.setProperty('--kc', KIND_COLORS[n.kind] || GREY);
    el.style.setProperty('--tc', laneColor(eng, L.tier.get(n.id)));
    el.style.width = `${cw}px`;
    el.style.height = '';
    const html = treeCardHtml(n, st, ctx);
    if (el.__html !== html) { el.innerHTML = html; el.__html = html; }
    const what = st.isSlot ? `${st.sl ? st.sl.label : n.kind} slot, model ${st.unconf ? 'not set' : n.model}` : n.kind;
    el.setAttribute('aria-label', `${n.id} ${n.label || ''}, ${what}${n.progress && st.total ? `, ${st.done} of ${st.total} done${st.failed ? `, ${st.failed} failed` : ''}` : ''}${st.running ? ', running' : ''}. ${st.isSlot ? 'Open the slot panel' : 'Show stage details'}`);
  }
  for (const [id, el] of stage.__cards) if (!nodes.some((n) => n.id === id)) { el.remove(); stage.__cards.delete(id); }
  const rowH = L.rows.map((row) => Math.max(36, ...row.filter((id) => L.items.get(id).real).map((id) => stage.__cards.get(id).offsetHeight)));
  const rowTop = [];
  let y = PIPE.padY;
  rowH.forEach((h, r) => { rowTop[r] = y; y += h + PIPE.vg; });
  const stageH = y - PIPE.vg + PIPE.padY;
  const stageW = Math.max(box.clientWidth, PIPE.gutter + X.width + 2 * PIPE.padX);
  const off = PIPE.gutter + PIPE.padX + (stageW - PIPE.gutter - 2 * PIPE.padX - X.width) / 2 - X.minX;
  const cx = (id) => Math.round(off + X.x.get(id));
  stage.style.width = `${stageW}px`;
  stage.style.height = `${stageH}px`;
  for (const n of nodes) {
    const el = stage.__cards.get(n.id);
    const it = L.items.get(n.id);
    el.style.height = `${rowH[it.row]}px`;
    el.style.left = `${cx(n.id) - cw / 2}px`;
    el.style.top = `${rowTop[it.row]}px`;
  }
  // lanes: contiguous rows of the same lane, labelled from engine.lanes
  const rowTier = L.rows.map((row) => L.tier.get(row.find((id) => L.items.get(id).real)));
  const lanes = [];
  rowTier.forEach((t, r) => {
    const last = lanes[lanes.length - 1];
    if (last && last.tier === t) last.r1 = r; else lanes.push({ tier: t, r0: r, r1: r });
  });
  stage.querySelector('.pipe-lanes').innerHTML = lanes.map((ln) => {
    const t = (eng.lanes || []).find((x) => x.id === ln.tier) || { id: ln.tier, label: ln.tier, sub: '' };
    const top = ln.r0 === 0 ? 0 : rowTop[ln.r0] - PIPE.vg / 2;
    const bottom = ln.r1 === L.rows.length - 1 ? stageH : rowTop[ln.r1] + rowH[ln.r1] + PIPE.vg / 2;
    return `<div class="pipe-lane" title="${esc(t.label || t.id)}${t.sub ? ` · ${esc(t.sub)}` : ''}" style="--c:${laneColor(eng, ln.tier)};top:${top}px;height:${bottom - top}px"><span class="lane-label"><b>${esc(t.label || t.id)}</b>${t.sub ? `<span class="sub">${esc(t.sub)}</span>` : ''}</span></div>`;
  }).join('');
  // connectors
  const svg = stage.querySelector('svg.pipe-wires');
  svg.setAttribute('width', stageW);
  svg.setAttribute('height', stageH);
  svg.setAttribute('viewBox', `0 0 ${stageW} ${stageH}`);
  const gBase = svg.querySelector('.w-base');
  const gFlow = svg.querySelector('.w-flow');
  const seen = new Set();
  const order = { idle: 1, done: 2, live: 3 };
  const drawn = [];
  for (const ch of L.chains) {
    const pts = [];
    const a = L.items.get(ch.ids[0]);
    pts.push([cx(a.id), rowTop[a.row] + rowH[a.row]]);
    for (let i = 1; i < ch.ids.length; i += 1) {
      const prev = L.items.get(ch.ids[i - 1]);
      const it = L.items.get(ch.ids[i]);
      const bus = rowTop[prev.row] + rowH[prev.row] + PIPE.vg / 2;
      pts.push([cx(prev.id), bus], [cx(it.id), bus], [cx(it.id), rowTop[it.row] - (it.real ? 1 : 0)]);
      if (!it.real) pts.push([cx(it.id), rowTop[it.row] + rowH[it.row]]);
    }
    const dstr = roundedPath(pts);
    const sa = states.get(ch.from);
    const sb = states.get(ch.to);
    const kind = sb.running ? 'live' : sb.complete && (sa.complete || roots.has(ch.from) || !(L.items.get(ch.from).node || {}).progress) ? 'done' : 'idle';
    let w = stage.__wires.get(ch.key);
    if (!w) {
      w = { base: svgEl('path'), flow: svgEl('path') };
      gBase.appendChild(w.base);
      gFlow.appendChild(w.flow);
      stage.__wires.set(ch.key, w);
    }
    if (w.d !== dstr) { w.base.setAttribute('d', dstr); w.flow.setAttribute('d', dstr); w.d = dstr; }
    if (w.kind !== kind) {
      w.base.setAttribute('class', `wire ${kind}`);
      w.base.setAttribute('marker-end', `url(#${boxId}-pa-${kind})`);
      w.flow.setAttribute('class', `wire-flow${kind === 'live' ? ' on' : ''}`);
      w.kind = kind;
    }
    seen.add(ch.key);
    drawn.push([order[kind], w.base]);
  }
  for (const [k, w] of stage.__wires) if (!seen.has(k)) { w.base.remove(); w.flow.remove(); stage.__wires.delete(k); }
  drawn.sort((p, q) => p[0] - q[0]).forEach(([, el]) => { if (el !== gBase.lastChild) gBase.appendChild(el); });
}

function markTreeSelected(boxId, id) {
  const tree = state.trees[boxId];
  if (tree) tree.selected = id;
  const box = $(`#${boxId}`);
  const stage = box && box.__stage;
  if (!stage) return;
  for (const [nid, el] of stage.__cards) el.classList.toggle('selected', nid === id);
}
function clearTreeSelection() { for (const k of Object.keys(state.trees)) markTreeSelected(k, null); }

function onTreeNode(boxId, id) {
  const tree = state.trees[boxId];
  const n = tree && (tree.nodes || []).find((x) => x.id === id);
  if (!n) return;
  if (n.slot && tree.engine) {
    inspect({ kind: 'slot', engine: tree.engine.id, slot: n.slot, profile: tree.profileName, tree: boxId, node: id });
  } else inspect({ kind: 'stage', tree: boxId, id });
  markTreeSelected(boxId, id);
}

function renderStageInspector(target) {
  const body = $('#inspBody');
  const tree = state.trees[target.tree];
  const n = tree && (tree.nodes || []).find((x) => x.id === target.id);
  $('#inspKind').textContent = 'Engine stage';
  if (!n) { body.innerHTML = `<div class="insp-section">${emptyState('Stage not on screen', '<div>Open the Garage or Run view to load the engine tree.</div>')}</div>`; return; }
  const nodes = tree.nodes || [];
  const eng = tree.engine || engineFromNodes(null, nodes);
  const parents = nodes.filter((x) => (x.children || []).includes(n.id));
  const kids = (n.children || []).map((c) => nodes.find((x) => x.id === c)).filter(Boolean);
  const p = n.progress;
  const total = p ? Number(p.total) || 0 : 0;
  const done = p ? Number(p.done) || 0 : 0;
  const bad = p ? (Number(p.failed) || 0) + (Number(p.dead_letter) || 0) : 0;
  const pending = p && p.pending !== undefined && p.pending !== null ? Number(p.pending) || 0 : Math.max(0, total - done - bad);
  const lane = (eng.lanes || []).find((l) => l.id === (n.lane || n.tier));
  const link = (x) => `<button type="button" class="chip plain" style="--c:${KIND_COLORS[x.kind] || GREY}" data-action="tree-node" data-tree="${esc(target.tree)}" data-id="${esc(x.id)}"><i class="dot"></i>${esc(x.id)} ${esc(x.label || '')}</button>`;
  const isRoot = !parents.length;
  body.innerHTML = `
    <div class="insp-section">
      <div class="chips">${chip(n.kind || 'stage', KIND_COLORS[n.kind] || GREY, 'solid')}${n.stage ? plainChip(n.stage, 'orchestrator stage') : ''}${lane ? chip(lane.label, laneColor(eng, lane.id), '', lane.sub || '') : ''}</div>
      <div class="insp-title">${esc(n.id)} · ${esc(n.label || '')}</div>
      <div class="insp-id">${esc(eng.name || '')}</div>
      ${n.detail ? `<div class="ink2" style="margin-top:4px">${esc(n.detail)}</div>` : ''}
    </div>
    ${isRoot ? `<div class="insp-section"><span class="cap">Corpus</span><div class="kv">
        <span>Files</span><span class="num">${esc(fmtNum(state.corpus ? state.corpus.length : null))}</span>
        <span>Ingested</span><span class="num">${esc(fmtNum(state.corpus ? state.corpus.filter((f) => f.ingested).length : null))}</span>
        <span>Documents</span><span class="num">${esc(fmtNum((state.overview || {}).documents))} in S1</span></div>
        <div style="margin-top:8px"><a href="#/run" class="btn sm">Manage corpus ↗</a></div></div>` : tree.mode === 'run' ? `
    <div class="insp-section"><span class="cap">Progress${tree.runId ? ` <span class="mono muted" style="text-transform:none;letter-spacing:0">${esc(shortId(tree.runId))}</span>` : ''}</span>
      ${!tree.runId ? '<div class="muted">No run selected.</div>' : p ? `
        <div class="pn-prog" style="margin:4px 0 8px"><span class="bar"><i style="width:${total ? ((done / total) * 100).toFixed(1) : 0}%"></i></span><span class="frac num">${esc(fmtNum(done))}/${esc(fmtNum(total))}</span></div>
        <div class="kv"><span>Done</span><span class="num">${esc(fmtNum(done))}</span><span>Pending</span><span class="num">${esc(fmtNum(pending))}</span>
          <span>Failed</span><span class="num" style="${p.failed ? 'color:var(--bad)' : ''}">${esc(fmtNum(p.failed))}</span><span>Dead letters</span><span class="num">${esc(fmtNum(p.dead_letter))}</span>
          ${n.tokens ? `<span>Tokens in</span><span class="num">${esc(fmtNum(n.tokens.in))}</span><span>Tokens out</span><span class="num">${esc(fmtNum(n.tokens.out))}</span><span>Model calls</span><span class="num">${esc(fmtNum(n.tokens.calls))}</span>` : ''}
        </div>
        ${n.stage ? `<div style="margin-top:8px"><button type="button" class="btn sm" data-action="stage-units" data-stage="${esc(n.stage)}">Units in Data ↗</button></div>` : ''}`
    : '<div class="muted">No units recorded for this stage yet.</div>'}
    </div>` : ''}
    <div class="insp-section"><span class="cap">Flow</span>
      <div class="kv"><span>From</span><span class="chips">${parents.map(link).join('') || '<span class="muted">root</span>'}</span>
        <span>To</span><span class="chips">${kids.map(link).join('') || '<span class="muted">end of the engine</span>'}</span></div>
    </div>`;
}

// Layered layout: depth = longest path from the root; long edges get dummy points so connectors never cross a card.
function pipeLayout(nodes, eng = null) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const kids = new Map(nodes.map((n) => [n.id, [...new Set((n.children || []).filter((c) => byId.has(c) && c !== n.id))]]));
  const parents = new Map(nodes.map((n) => [n.id, []]));
  for (const [p, cs] of kids) for (const c of cs) parents.get(c).push(p);
  const indeg = new Map(nodes.map((n) => [n.id, parents.get(n.id).length]));
  const depth = new Map();
  const queue = nodes.filter((n) => !indeg.get(n.id)).map((n) => n.id);
  queue.forEach((id) => depth.set(id, 0));
  const topo = [];
  while (queue.length) {
    const id = queue.shift();
    topo.push(id);
    for (const c of kids.get(id)) {
      depth.set(c, Math.max(depth.get(c) ?? 0, depth.get(id) + 1));
      indeg.set(c, indeg.get(c) - 1);
      if (indeg.get(c) === 0) queue.push(c);
    }
  }
  let maxD = Math.max(0, ...depth.values());
  for (const n of nodes) if (!topo.includes(n.id)) { depth.set(n.id, ++maxD); topo.push(n.id); }   // cycle guard
  const tier = new Map();
  for (const id of topo) {
    const n = byId.get(id);
    tier.set(id, n.lane || n.tier || (parents.get(id).length ? tier.get(parents.get(id)[0]) : null) || ((eng && eng.lanes && eng.lanes[0]) || {}).id || 'IN');
  }

  const items = new Map();
  const rows = Array.from({ length: maxD + 1 }, () => []);
  const add = (it) => { items.set(it.id, it); rows[it.row].push(it.id); };
  const chains = [];
  for (const id of topo) {
    if (!items.has(id)) add({ id, real: true, node: byId.get(id), row: depth.get(id), ups: [], downs: [] });
    for (const c of kids.get(id)) {
      if (depth.get(c) <= depth.get(id)) continue;   // back edge on a cycle: not drawn
      const ids = [id];
      for (let r = depth.get(id) + 1; r < depth.get(c); r += 1) {
        const did = `~${id}>${c}#${r}`;
        add({ id: did, real: false, row: r, ups: [], downs: [] });
        ids.push(did);
      }
      if (!items.has(c)) add({ id: c, real: true, node: byId.get(c), row: depth.get(c), ups: [], downs: [] });
      ids.push(c);
      for (let i = 0; i < ids.length - 1; i += 1) { items.get(ids[i]).downs.push(ids[i + 1]); items.get(ids[i + 1]).ups.push(ids[i]); }
      chains.push({ key: `${id}>${c}`, from: id, to: c, ids });
    }
  }
  // two barycentre sweeps to keep the fan-out in children order and avoid crossings
  for (let iter = 0; iter < 2; iter += 1) {
    for (let r = 1; r < rows.length; r += 1) sortByBary(rows[r], rows[r - 1], (it) => it.ups, items);
    for (let r = rows.length - 2; r >= 0; r -= 1) sortByBary(rows[r], rows[r + 1], (it) => it.downs, items);
  }
  return { items, rows, chains, parents, kids, tier };
}
function sortByBary(row, ref, nbrs, items) {
  const idx = new Map(ref.map((id, i) => [id, i]));
  const key = new Map(row.map((id, i) => {
    const ns = nbrs(items.get(id)).filter((n) => idx.has(n));
    return [id, ns.length ? ns.reduce((a, n) => a + idx.get(n), 0) / ns.length : i];
  }));
  const pos = new Map(row.map((id, i) => [id, i]));
  row.sort((a, b) => key.get(a) - key.get(b) || pos.get(a) - pos.get(b));
}

function pipeX(L, cw) {
  const w = (id) => (L.items.get(id).real ? cw : PIPE.dummyW);
  const sep = (a, b) => (w(a) + w(b)) / 2 + (L.items.get(a).real && L.items.get(b).real ? PIPE.hg : PIPE.hg / 2);
  const x = new Map();
  const place = (ids, desired) => {
    const n = ids.length;
    const lo = new Array(n);
    const hi = new Array(n);
    for (let i = 0; i < n; i += 1) lo[i] = i ? Math.max(desired[i], lo[i - 1] + sep(ids[i - 1], ids[i])) : desired[i];
    for (let i = n - 1; i >= 0; i -= 1) hi[i] = i < n - 1 ? Math.min(desired[i], hi[i + 1] - sep(ids[i], ids[i + 1])) : desired[i];
    ids.forEach((id, i) => x.set(id, (lo[i] + hi[i]) / 2));
  };
  for (const row of L.rows) {   // initial: packed and centred on 0
    let acc = 0;
    const raw = row.map((id, i) => (acc += i ? sep(row[i - 1], id) : 0));
    const mid = (raw[0] + raw[raw.length - 1]) / 2;
    row.forEach((id, i) => x.set(id, raw[i] - mid));
  }
  const mean = (ids) => ids.reduce((a, id) => a + x.get(id), 0) / ids.length;
  for (let iter = 0; iter < 4; iter += 1) {
    for (let r = 1; r < L.rows.length; r += 1) place(L.rows[r], L.rows[r].map((id) => { const u = L.items.get(id).ups; return u.length ? mean(u) : x.get(id); }));
    for (let r = L.rows.length - 2; r >= 0; r -= 1) place(L.rows[r], L.rows[r].map((id) => { const dn = L.items.get(id).downs; return dn.length ? mean(dn) : x.get(id); }));
  }
  for (let r = 1; r < L.rows.length; r += 1) place(L.rows[r], L.rows[r].map((id) => { const u = L.items.get(id).ups; return u.length ? mean(u) : x.get(id); }));
  let minX = Infinity;
  let maxX = -Infinity;
  for (const [id, v] of x) { minX = Math.min(minX, v - w(id) / 2); maxX = Math.max(maxX, v + w(id) / 2); }
  return { x, minX, width: maxX - minX };
}

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}
// Polyline through axis-aligned points with rounded corners.
function roundedPath(pts, rad = 9) {
  const p = [];
  for (const q of pts) {
    const last = p[p.length - 1];
    if (!last || Math.abs(last[0] - q[0]) > 0.5 || Math.abs(last[1] - q[1]) > 0.5) p.push(q);
  }
  const s = p.filter((q, i) => i === 0 || i === p.length - 1
    || !((Math.abs(p[i - 1][0] - q[0]) < 0.5 && Math.abs(q[0] - p[i + 1][0]) < 0.5) || (Math.abs(p[i - 1][1] - q[1]) < 0.5 && Math.abs(q[1] - p[i + 1][1]) < 0.5)));
  const f = (v) => Math.round(v * 10) / 10;
  let d = `M${f(s[0][0])},${f(s[0][1])}`;
  for (let i = 1; i < s.length - 1; i += 1) {
    const [x0, y0] = s[i - 1];
    const [x1, y1] = s[i];
    const [x2, y2] = s[i + 1];
    const l1 = Math.hypot(x1 - x0, y1 - y0);
    const l2 = Math.hypot(x2 - x1, y2 - y1);
    const r = Math.min(rad, l1 / 2, l2 / 2);
    d += ` L${f(x1 - ((x1 - x0) / l1) * r)},${f(y1 - ((y1 - y0) / l1) * r)} Q${f(x1)},${f(y1)} ${f(x1 + ((x2 - x1) / l2) * r)},${f(y1 + ((y2 - y1) / l2) * r)}`;
  }
  const e = s[s.length - 1];
  return `${d} L${f(e[0])},${f(e[1])}`;
}

/* ================================================================ view: garage (engines · builds · slots) */
async function viewGarage(params, alive) {
  const ws = $('#workspace');
  ws.innerHTML = `<div class="view">
    <div class="view-head"><h1>Garage</h1><span class="muted">pick an engine, plug a model into each slot, save the build</span><span class="spacer"></span>
      <button type="button" class="btn sm" data-action="compact" aria-pressed="${state.compact}" title="Hide detail lines so more of the tree fits on screen">${state.compact ? 'Detailed' : 'Compact'}</button>
      <button type="button" class="btn sm" data-action="garage-refresh">Refresh</button></div>
    ${demoBanner('activating, copying and editing builds is disabled')}
    <div class="engine-grid" id="gEngines" role="group" aria-label="Engines">${skeleton(2)}</div>
    <section class="panel build-bar" id="gBuild" aria-label="Build">${skeleton(2)}</section>
    <div class="pipe-canvas${state.compact ? ' compact' : ''}" id="garageTree"><div class="panel-body">${skeleton(6)}</div></div>
  </div>`;
  if (params[0]) state.garage.engine = params[0];
  await Promise.allSettled([loadEngines(), loadProfiles(), loadServers(), loadSecrets(), loadCorpusQuiet(), refreshRuns(true)]);
  if (!alive()) return;
  if (!state.engines.length) {
    $('#gEngines').innerHTML = state.enginesError ? errorBox(state.enginesError, 'Could not load the engine registry (GET /api/engines)') : emptyState('No engines registered', '<div>synth/engines.py registers none.</div>');
    $('#gBuild').innerHTML = '';
    $('#garageTree').innerHTML = '';
    return;
  }
  if (!engineById(state.garage.engine)) state.garage.engine = (defaultEngine() || {}).id;
  if (!profileByName(state.garage.profile)) state.garage.profile = (activeProfile() || {}).name || null;
  renderGarage(alive);
}

function renderGarage(alive) {
  renderEngineCards();
  renderBuildBar();
  const eng = engineById(state.garage.engine);
  const p = profileByName(state.garage.profile);
  if (!eng) return;
  mountTree('garageTree', { engine: eng, nodes: composeNodes(eng, p), profileName: p ? p.name : null, runId: null, live: false, mode: 'garage' }, alive);
  applyCtl($('#workspace'));
}

function renderEngineCards() {
  const box = $('#gEngines');
  if (!box) return;
  const p = profileByName(state.garage.profile);
  box.innerHTML = (state.engines || []).map((eng) => {
    const sel = eng.id === state.garage.engine;
    const missing = engineMissing(eng, p);
    const ready = !missing.length;
    return `<button type="button" class="engine-card${sel ? ' selected' : ''}" data-action="pick-engine" data-id="${esc(eng.id)}" aria-pressed="${sel}"
        aria-label="${esc(eng.name)}${eng.default ? ', default engine' : ''}: ${ready ? 'every required slot plugged' : `${missing.length} required slot${missing.length > 1 ? 's' : ''} empty`} in build ${esc(p ? p.name : 'none')}">
      <span class="ec-top">${light(ready ? 'ok' : 'warn', ready ? 'ready with this build' : `${missing.length} required slot(s) empty`)}<b class="ec-name">${esc(eng.name)}</b>${engineStatusChip(eng)}${eng.default ? chip('default', '#4ea1ff', 'solid', 'the engine new runs use unless you pick another') : ''}</span>
      <span class="ec-tag">${esc(eng.tagline || '')}</span>
      <span class="ec-slots">${(eng.slots || []).map((sl) => {
    const ok = slotReady(p, sl);
    return `<span class="ec-slot${ok ? '' : ' vacant'}" style="--c:${slotColor(eng, sl)}" title="${esc(sl.label)} → ${esc(sl.endpoint)}${ok ? '' : sl.required === false ? ' · optional, not configured' : ' · empty'}"><i></i>${esc(sl.label)}${ok ? '' : sl.required === false ? ' <span class="muted">opt</span>' : ` ${ICON.warn}`}</span>`;
  }).join('')}</span>
      <span class="ec-id mono">${esc(eng.id)}</span>
    </button>`;
  }).join('');
}

function renderBuildBar() {
  const box = $('#gBuild');
  if (!box) return;
  const ps = state.profiles || [];
  const p = profileByName(state.garage.profile);
  const eng = engineById(state.garage.engine);
  const used = p ? state.runs.filter((x) => x.profile === p.name).length : 0;
  const missing = engineMissing(eng, p);
  box.innerHTML = `<div class="panel-head">
      <label for="gBuildSel" class="cap">Build</label>
      <select id="gBuildSel" aria-label="Build (model line-up) shown on the engine">${ps.length ? ps.map((x) => `<option value="${esc(x.name)}"${x.name === state.garage.profile ? ' selected' : ''}>${esc(x.name)}${x.active ? ' · active' : ''}${x.editable ? '' : ' · built-in'}</option>`).join('') : '<option value="">no builds</option>'}</select>
      ${p ? `${p.active ? chip('active', STATUS_COLORS.COMMITTED, 'solid', 'new runs use this build') : ''}${p.editable ? plainChip('editable') : plainChip('built-in', 'read-only: plugging a model in saves a copy')}` : ''}
      <button type="button" class="btn sm primary" data-ctl data-action="build-activate"${!p || p.active ? ' disabled' : ''} title="Make this build the default for new runs">${p && p.active ? 'Active' : 'Activate'}</button>
      <button type="button" class="btn sm" data-ctl data-action="build-copy"${p ? '' : ' disabled'}>Save as new build…</button>
      ${p && p.editable ? '<button type="button" class="btn sm danger" data-ctl data-action="build-delete">Delete</button>' : ''}
      <span class="spacer"></span>
      ${p && used ? `<a href="#/dyno" class="muted" title="runs recorded with this build">${esc(used)} run${used === 1 ? '' : 's'} used this build ↗</a>` : '<span class="muted">no runs with this build yet</span>'}
    </div>
    <div class="panel-body build-sum">
      <div class="bs-eng"><b>${esc(eng ? eng.name : '')}</b> ${eng ? `<span class="muted mono">${esc(eng.id)}</span>` : ''}<div class="ink2">${esc(eng ? eng.description || eng.tagline || '' : '')}</div></div>
      <div class="bs-state">${!p ? '<span class="muted">no build</span>' : missing.length
    ? `<div class="note warn" style="margin:0">${ICON.warn}<span><b>${esc(missing.length)} required slot${missing.length > 1 ? 's' : ''} empty:</b> ${missing.map((sl) => `<button type="button" class="linkish" data-action="edit-slot" data-engine="${esc(eng.id)}" data-slot="${esc(sl.id)}" data-profile="${esc(p.name)}">${esc(sl.label)}</button>`).join(', ')}</span></div>`
    : `<div class="note ok" style="margin:0"><span><b>Ready to run.</b> Every required slot of ${esc(eng.name)} is plugged in this build. <button type="button" class="linkish" data-action="run-this" data-engine="${esc(eng.id)}" data-profile="${esc(state.garage.profile || '')}">Run it ↗</button></span></div>`}
        ${p && p.description ? `<div class="hint">${esc(p.description)}</div>` : ''}</div>
    </div>`;
  $('#gBuildSel').addEventListener('change', (e) => {
    state.garage.profile = e.target.value;
    const seq = state.seq;
    renderGarage(() => seq === state.seq);
    const cur = state.insp.current;
    if (cur && cur.kind === 'slot') inspect({ ...cur, profile: state.garage.profile }, { push: false });
  });
  applyCtl(box);
}

function pickEngine(id) {
  if (!engineById(id)) return;
  state.garage.engine = id;
  try { history.replaceState(null, '', `#/garage/${enc(id)}`); } catch { /* optional */ }
  const seq = state.seq;
  renderGarage(() => seq === state.seq);
  const cur = state.insp.current;
  if (cur && (cur.kind === 'slot' || cur.kind === 'stage')) inspectorEmpty();
}

// An open slot panel follows server, key and build changes made elsewhere (unless it holds unsaved edits).
function refreshSlotPanel() {
  const cur = state.insp.current;
  if (cur && cur.kind === 'slot' && !advDirty() && !document.body.classList.contains('insp-collapsed')) inspect(cur, { push: false });
}
// Re-render whatever on-screen view shows builds after an edit.
function refreshWorkshop() {
  const seq = state.seq;
  const alive = () => seq === state.seq;
  if (state.view === 'garage' && $('#gEngines')) {
    if (!profileByName(state.garage.profile)) state.garage.profile = (activeProfile() || {}).name || null;
    renderGarage(alive);
  }
  if (state.view === 'run' && $('#runPage')) { renderRunProfileSelect(); renderStagePreview(); renderPreflight(); }
  if (state.view === 'servers' && $('#srvList')) renderServerList();
  else refreshSlotPanel();
}

function suggestCopyName(name) {
  const base = String(name || 'build').replace(/-custom(-\d+)?$/, '');
  let cand = `${base}-custom`;
  for (let i = 2; profileByName(cand); i += 1) cand = `${base}-custom-${i}`;
  return cand;
}
// Build names as the server takes them (synth.console.settings.NAME): a-z 0-9 . _ -, max 48, starting alphanumeric.
// Whatever is typed is turned into one: "Synth −0 Mistral" -> "synth-0-mistral".
const buildSlug = (s) => String(s || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9._-]+/g, '-')
  .replace(/-{2,}/g, '-').replace(/^[^a-z0-9]+/, '').slice(0, 48).replace(/[._-]+$/, '');
const nameField = (value, label = 'New build name') => ({ label, value, pattern: '.*[A-Za-z0-9].*', hint: 'saved lowercase with dashes, e.g. minus0-mistral', mono: true });

// Save into a build; a built-in build is copied first (name asked), then the copy is saved and activated.
async function saveToBuild(name, body, what) {
  const p = profileByName(name);
  let target = name;
  let created = false;
  if (p && !p.editable) {
    const ans = await askDialog({
      title: 'Save as a new build',
      html: `<p><span class="mono">${esc(name)}</span> is built in and read-only. Synth copies it, saves ${esc(what)} into the copy and makes the copy the active build.</p>`,
      fields: [nameField(suggestCopyName(name))],
      ok: 'Copy, save & activate',
    });
    if (!ans) return null;
    try {
      const slug = buildSlug(ans[0]);
      const copy = await API.copyProfile(name, slug);
      target = (copy && copy.name) || slug;
      created = true;
    } catch { return null; }
  }
  try {
    await API.saveProfile(target, body);
    if (created) await API.activateProfile(target);
    toast(created ? `Saved into new build ${target}` : `Saved ${what}`, created ? `${target} is now the active build` : target, 'ok');
  } catch {
    if (created) { state.profiles = null; await loadProfiles().catch(() => {}); refreshWorkshop(); }
    return null;
  }
  if (created) {
    if (state.garage.profile === name) state.garage.profile = target;
    if (state.runsUi.profile === name) state.runsUi.profile = target;
  }
  state.profiles = null;
  await Promise.allSettled([loadProfiles(), loadSecrets(true), loadServers(true)]);
  refreshWorkshop();
  return target;
}

async function buildAction(action, btn) {
  const name = state.garage.profile;
  if (!name) return;
  if (action === 'build-activate') {
    btn.disabled = true;
    try { await API.activateProfile(name); toast('Build activated', `${name} is the default for new runs`, 'ok'); } catch { btn.disabled = false; return; }
  } else if (action === 'build-copy') {
    const ans = await askDialog({ title: `Save ${name} as a new build`, html: '<p>The copy is editable: plug models into its slots without touching the original.</p>', fields: [nameField(suggestCopyName(name))], ok: 'Save copy' });
    if (!ans) return;
    try { const slug = buildSlug(ans[0]); const c = await API.copyProfile(name, slug); state.garage.profile = (c && c.name) || slug; toast('Build saved', `${state.garage.profile} (editable)`, 'ok'); } catch { return; }
  } else if (action === 'build-delete') {
    const ok = await askDialog({ title: `Delete ${name}?`, html: '<p>The build file is removed. Runs that used it keep their recorded models and manifest.</p>', ok: 'Delete build', danger: true });
    if (!ok) return;
    try { await API.deleteProfile(name); toast('Build deleted', name, 'ok'); } catch { return; }
    state.garage.profile = null;
    if (state.insp.current && state.insp.current.kind === 'slot' && state.insp.current.profile === name) inspectorEmpty();
  }
  state.profiles = null;
  await Promise.allSettled([loadProfiles(), loadServers(true)]);
  refreshWorkshop();
}

/* ================================================================ inspector: slot panel (plug and play) */
async function renderSlotPanel(target, alive) {
  await Promise.allSettled([loadEngines(), loadProfiles(), loadSecrets(), state.servers ? null : loadServers(), loadPresets()]);
  if (!alive()) return;
  const body = $('#inspBody');
  const eng = engineById(target.engine);
  const sl = engineSlot(eng, target.slot);
  const p = profileByName(target.profile) || activeProfile();
  if (!eng || !sl) { body.innerHTML = `<div class="insp-section">${errorBox(`engine ${target.engine} has no slot ${target.slot}`, 'Unknown slot')}</div>`; return; }
  if (!p) { body.innerHTML = `<div class="insp-section">${errorBox('no builds returned by /api/profiles', 'No builds')}</div>`; return; }
  const cfg = slotCfg(p, sl) || {};
  const gl = isGlinerSlot(sl);
  const ready = slotReady(p, sl);
  state.sp = { engine: eng.id, slot: sl.id, endpoint: sl.endpoint, profile: p.name, editable: p.editable, gliner: gl, src: cfg };
  $('#inspKind').textContent = `Slot · ${sl.label}`;
  const head = `<div class="insp-section">
      <div class="chips">${slotTag(eng, sl)}${sl.required === false ? plainChip('optional') : plainChip('required')}${gl ? locBadge(true) : locBadge(cfg.local)}</div>
      <div class="insp-title">${esc(sl.label)}</div>
      <div class="insp-id">${esc(eng.name)} · build <b class="ink2">${esc(p.name)}</b>${p.active ? ' (active)' : ''} · endpoint <span class="ink2">${esc(sl.endpoint)}</span></div>
      ${sl.hint ? `<div class="ink2" style="margin-top:4px">${esc(sl.hint)}</div>` : ''}
      ${p.editable ? '' : '<div class="note"><span><b>Built-in build (read-only).</b> Assigning asks for a name, copies the build, saves into the copy and activates it.</span></div>'}
    </div>
    <div class="insp-section"><span class="cap">Plugged in</span>
      <div class="plugged${ready ? '' : ' vacant'}">
        <div class="pl-model mono" title="${esc(cfg.model || '')}">${ready ? esc(cfg.model) : `<span class="socket">empty socket</span>`}</div>
        <div class="pl-where">${slotWhereHtml(eng, sl, cfg)}${gl ? '' : keyStatus(!!cfg.local, !!keyPresent(cfg.api_key_env, cfg.key_present), cfg.api_key_env)}</div>
        ${!gl && cfg.base_url ? `<div class="pl-url mono muted" title="${esc(cfg.base_url)}">${esc(cfg.base_url)}</div>` : ''}
      </div>
      ${slotTreeProgress(target)}
    </div>`;
  body.innerHTML = head + (gl ? glinerPanelHtml(p, cfg) : plugPanelHtml(eng, sl, p, cfg) + advancedHtml(sl, cfg));
  if (!gl) {
    wirePlugPanel(cfg);
    body.querySelectorAll('[data-sf]').forEach((el) => el.addEventListener(el.tagName === 'SELECT' || el.type === 'checkbox' ? 'change' : 'input', () => onAdvInput(el)));
    state.sp.orig = JSON.stringify(readAdvForm());
    advDirty();
    renderSlotTest(p.name, sl.endpoint);
  } else {
    body.querySelectorAll('[data-sf]').forEach((el) => el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', () => advDirty()));
    state.sp.orig = JSON.stringify(readGlinerForm());
    advDirty();
  }
  applyCtl(body);
}

function slotTreeProgress(target) {
  const tree = target.tree && state.trees[target.tree];
  if (!tree || tree.mode !== 'run' || !tree.runId) return '';
  const n = (tree.nodes || []).find((x) => x.slot === target.slot && x.progress);
  if (!n) return '';
  const total = Number(n.progress.total) || 0;
  const done = Number(n.progress.done) || 0;
  return `<div class="pn-prog" style="margin-top:8px" title="${esc(n.id)} progress in run ${esc(tree.runId)}"><span class="bar"><i style="width:${total ? ((done / total) * 100).toFixed(1) : 0}%"></i></span><span class="frac num">${esc(n.id)} ${esc(fmtNum(done))}/${esc(fmtNum(total))}${n.progress.failed ? ` · ${esc(n.progress.failed)} failed` : ''}</span></div>`;
}

function plugPanelHtml(eng, sl, p, cfg) {
  const ss = state.servers || [];
  if (!ss.length) {
    return `<div class="insp-section"><span class="cap">Plug in a model</span>
      ${state.serversError ? errorBox(state.serversError, 'Could not list servers') : ''}
      <div class="note warn">${ICON.warn}<span>No model servers yet. Open <a href="#/servers">Servers</a> to detect a running one, start one, or add a base URL.</span></div>
      <div class="hint">Or set the base URL and model id by hand under Advanced.</div></div>`;
  }
  const cur = serverForUrl(cfg.base_url);
  const pick = cur || ss.find((s) => s.status && s.status.reachable && serverModels(s).length) || ss[0];
  return `<div class="insp-section plug"><span class="cap">Plug in a model</span>
      <div class="sf-grid">
        <label for="spServer">Server</label>
        <div class="sf-row"><span id="spLight"></span><select id="spServer">${ss.map((s) => { const st = serverState(s); return `<option value="${esc(s.id)}"${s.id === pick.id ? ' selected' : ''}>${esc(s.name || s.id)} · ${esc(hostOf(s.base_url))} · ${esc(st.cls === 'ok' ? `${serverModels(s).length} models` : st.cls === 'warn' ? 'no models' : 'offline')}</option>`; }).join('')}</select>
          <button type="button" class="icon-btn" data-action="sp-reprobe" aria-label="Probe the servers again" title="Probe the servers again"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 2.5v3h-3" fill="none" stroke="currentColor" stroke-width="1.4"/></svg></button></div>
        <label for="spModel">Model</label>
        <div id="spModelBox"></div>
      </div>
      <div id="spKeyNote"></div>
      <div class="form-actions">
        <button type="button" class="btn primary" data-ctl data-action="sp-assign" id="spAssign">Assign to ${esc(sl.label)}</button>
        <button type="button" class="btn" data-ctl data-action="sp-speed" id="spSpeedBtn" title="Time one short generation on this server: latency and tokens per second">Speed test</button>
      </div>
      <div id="spSpeed" aria-live="polite"></div>
    </div>`;
}

function wirePlugPanel(cfg) {
  const sel = $('#spServer');
  if (!sel) return;
  const update = () => {
    const s = serverById(sel.value);
    $('#spLight').innerHTML = serverLight(s);
    const models = serverModels(s);
    const curModel = s && normUrl(s.base_url) === normUrl(cfg.base_url) ? cfg.model : '';
    $('#spModelBox').innerHTML = models.length
      ? `<select id="spModel" aria-label="Models this server serves">${models.map((m) => `<option value="${esc(m)}"${m === curModel ? ' selected' : ''}>${esc(m)}</option>`).join('')}</select>`
      : `<input id="spModel" type="text" class="mono" spellcheck="false" placeholder="model id as the server names it" value="${esc(curModel || '')}" aria-describedby="spModelHint"><div class="hint" id="spModelHint">${s && s.status && s.status.reachable ? 'The server lists no models — load one in Servers, or type its id.' : 'The server is not answering — type the model id, or start the server in Servers.'}</div>`;
    const needKey = s && s.local === false && !keyPresent(s.api_key_env, s.key_present);
    $('#spKeyNote').innerHTML = needKey ? `<div class="note warn">${ICON.key}<span>${esc(s.name)} needs an API key${s.api_key_env ? ` (<span class="mono">${esc(s.api_key_env)}</span>)` : ''} — it is missing. Set it in <a href="#/servers">Servers</a>.</span></div>` : '';
    renderSpeed();
  };
  sel.addEventListener('change', update);
  $('#spModelBox').addEventListener('change', renderSpeed);
  $('#spModelBox').addEventListener('input', renderSpeed);
  update();
}
function spChoice() {
  const s = $('#spServer') && serverById($('#spServer').value);
  const m = $('#spModel') ? $('#spModel').value.trim() : '';
  return { server: s, model: m };
}

async function spAssign(btn) {
  const sp = state.sp;
  const { server, model } = spChoice();
  if (!server) return;
  if (!model) { toast('Choose a model first', 'pick one the server serves, or type its id', 'error'); return; }
  btn.disabled = true;
  const partial = { base_url: server.base_url, model, provider: server.kind || null, local: !!server.local, api_key_env: server.api_key_env || null };
  const eng = engineById(sp.engine);
  const target = await saveToBuild(sp.profile, { slots: { [sp.endpoint]: partial } }, `${model} into ${endpointLabel(eng, sp.endpoint)}`);
  btn.disabled = false;
  applyCtl($('#inspBody'));
  if (target && state.insp.current && state.insp.current.kind === 'slot') inspect({ ...state.insp.current, profile: target }, { push: false });
}

function renderSpeed() {
  const box = $('#spSpeed');
  if (!box) return;
  const { server, model } = spChoice();
  box.innerHTML = server && model ? speedHtml(server.id, model) : '';
}
function speedHtml(serverId, model) {
  const t = state.speed[`${serverId}|${model}`];
  if (!t) return '';
  if (t.status === 'running') return `<div class="speed run"><span class="spinner" aria-hidden="true" style="width:11px;height:11px"></span><span>timing <span class="mono">${esc(model)}</span>…</span></div>`;
  const r = t.result || {};
  if (!r.ok) return `<div class="speed bad">${ICON.warn}<span>speed test failed${r.error ? `: ${esc(r.error)}` : ''}</span><span class="muted">${esc(t.at.toLocaleTimeString('en-GB'))}</span></div>`;
  return `<div class="speed ok" role="status"><span class="gauge"><b class="num">${esc(r.tokens_per_s === null || r.tokens_per_s === undefined ? '—' : fmtNum(r.tokens_per_s, 1))}</b><span>tok/s</span></span>
    <span class="gauge"><b class="num">${esc(fmtNum(r.latency_ms))}</b><span>ms latency</span></span>
    <span class="gauge"><b class="num">${esc(fmtNum(r.tokens_out))}</b><span>tokens</span></span>
    <span class="muted speed-at">${esc(t.at.toLocaleTimeString('en-GB'))}</span></div>`;
}
async function runSpeedTest(serverId, model, btn) {
  if (!serverId || !model) { toast('Choose a model first', '', 'error'); return; }
  const key = `${serverId}|${model}`;
  if (btn) btn.disabled = true;
  let job;
  try { job = await API.speedtest(serverId, model); } catch { if (btn) btn.disabled = false; return; } finally { if (btn) { btn.disabled = false; applyCtl(btn.parentElement); } }
  state.speed[key] = { status: 'running', jobId: job.job_id, at: new Date() };
  refreshSpeedViews(serverId);
  schedule('global:poll', pollGlobal, 1500);
  trackJob(job.job_id, {
    onDone: (j) => {
      const res = j.result && typeof j.result === 'object' ? j.result : { ok: false, error: j.status === 'failed' ? `job failed${(j.log || []).length ? `: ${j.log[j.log.length - 1]}` : ''}` : 'no result' };
      state.speed[key] = { status: res.ok ? 'done' : 'failed', jobId: job.job_id, result: res, at: new Date() };
      toast(res.ok ? `${fmtNum(res.tokens_per_s, 1)} tok/s` : 'Speed test failed', `${model}${res.ok ? ` · ${fmtNum(res.latency_ms)} ms latency` : res.error ? ` · ${res.error}` : ''}`, res.ok ? 'ok' : 'error');
      refreshSpeedViews(serverId);
    },
  });
}
function refreshSpeedViews(serverId) {
  renderSpeed();
  const box = $(`#spd-${cssId(serverId)}`);
  if (box) renderServerSpeed(serverId);
}

function advancedHtml(sl, s) {
  const presets = state.presets || [];
  const opts = [`<option value="">${presets.length ? '— none —' : 'presets unavailable'}</option>`,
    ...presets.map((x) => `<option value="${esc(x.id)}"${x.id === s.provider ? ' selected' : ''}>${esc(x.label)}${x.local ? ' · local' : ''}</option>`)];
  if (s.provider && !presetFor(s.provider)) opts.push(`<option value="${esc(s.provider)}" selected>${esc(s.provider)}</option>`);
  const num = (v) => (v === null || v === undefined ? '' : esc(v));
  return `<details class="insp-section adv" id="spAdv"><summary><span class="cap">Advanced</span><span class="muted">endpoint by hand, context, structured mode, prices</span></summary>
      <div class="sf-grid" style="margin-top:8px">
        <label for="sfBase">Base URL</label><input id="sfBase" type="text" class="mono" data-sf value="${esc(s.base_url || '')}" spellcheck="false" placeholder="http://localhost:1234/v1">
        <label for="sfModel">Model id</label><input id="sfModel" type="text" class="mono" data-sf value="${esc(s.model || '')}" spellcheck="false" placeholder="id as the server lists it">
        <label for="sfProvider">Provider</label><select id="sfProvider" data-sf>${opts.join('')}</select>
        <label for="sfLocal">Locality</label><label class="check"><input type="checkbox" id="sfLocal" data-sf${s.local ? ' checked' : ''}><span class="lbl">local — runs on this machine</span></label>
        <label for="sfEnv">API key env</label><div class="sf-row"><input id="sfEnv" type="text" class="mono" data-sf value="${esc(s.api_key_env || '')}" spellcheck="false" autocomplete="off" placeholder="none (local)"><span id="sfKeyState"></span></div>
        <label for="sfCtx">Context length</label><input id="sfCtx" type="number" min="512" step="1" data-sf value="${num(s.context_length)}" placeholder="tokens">
        <label for="sfMode">Structured mode</label><select id="sfMode" data-sf>${['strict_schema', 'json_then_validate'].map((m) => `<option value="${m}"${m === s.structured_mode ? ' selected' : ''}>${m}</option>`).join('')}${s.structured_mode && !['strict_schema', 'json_then_validate'].includes(s.structured_mode) ? `<option selected>${esc(s.structured_mode)}</option>` : ''}</select>
        <label for="sfTemp">Temperature</label><input id="sfTemp" type="number" min="0" max="2" step="any" data-sf value="${num(s.temperature)}">
        <label for="sfReason">Reasoning budget</label><div><input id="sfReason" type="number" min="0" step="1" data-sf value="${num(s.reasoning_budget)}" placeholder="none" aria-describedby="sfReasonHint" style="width:100%"><div class="hint" id="sfReasonHint">thinking tokens on top of the answer cap, for reasoning models</div></div>
        <label for="sfPin">Price in / out</label><div class="sf-row"><input id="sfPin" type="number" min="0" step="any" data-sf value="${num(s.price_per_mtok_in)}" placeholder="in" aria-label="Price per million input tokens"><input id="sfPout" type="number" min="0" step="any" data-sf value="${num(s.price_per_mtok_out)}" placeholder="out" aria-label="Price per million output tokens"><span class="muted nowrap">$ / Mtok</span></div>
        <span></span><div class="hint">Sovereign documents never go to a non-local endpoint: their escalations are held for a human. Keys are set per server in Servers.</div>
      </div>
      <div class="form-actions">
        <button type="button" class="btn primary" data-ctl data-action="sf-save">Save</button>
        <button type="button" class="btn" data-ctl data-action="sf-test" title="One tiny structured request through the gateway with the saved settings">Structured-output test</button>
        <button type="button" class="btn" data-action="sf-revert" id="sfRevert" disabled>Revert</button>
        <span id="sfDirty" hidden>${chip('unsaved changes', STATUS_COLORS.ESCALATED)}</span>
      </div>
      <div id="sfTest" style="margin-top:10px"></div>
    </details>`;
}

function glinerPanelHtml(p, s) {
  return `<div class="insp-section"><span class="cap">GLiNER (in-process)</span>
      <div class="sf-grid">
        <label for="sgModel">Model</label><input id="sgModel" type="text" class="mono" data-sf value="${esc(s.model || '')}" spellcheck="false" placeholder="fastino/gliner2.5-base-v1">
        <label for="sgDevice">Device</label><select id="sgDevice" data-sf>${['cuda', 'cpu'].map((x) => `<option value="${x}"${x === s.device ? ' selected' : ''}>${x}</option>`).join('')}${s.device && !['cuda', 'cpu'].includes(s.device) ? `<option selected>${esc(s.device)}</option>` : ''}</select>
        <label for="sgThr">Threshold</label><div><input id="sgThr" type="number" min="0" max="1" step="any" data-sf value="${esc(s.threshold ?? '')}" style="width:100%" aria-describedby="sgThrHint"><div class="hint" id="sgThrHint">minimum span score kept: lower finds more entities and more noise</div></div>
      </div>
      <div class="hint" style="margin-top:8px">GLiNER is not a server: it runs inside the Synth process during its stage. There is no endpoint, key or speed test.</div>
      <div class="form-actions">
        <button type="button" class="btn primary" data-ctl data-action="sf-save">${p.editable ? 'Save' : 'Save as new build…'}</button>
        <button type="button" class="btn" data-action="sf-revert" id="sfRevert" disabled>Revert</button>
        <span id="sfDirty" hidden>${chip('unsaved changes', STATUS_COLORS.ESCALATED)}</span>
      </div>
    </div>`;
}

function readGlinerForm() {
  const t = $('#sgThr').value.trim();
  return { model: $('#sgModel').value.trim(), device: $('#sgDevice').value, threshold: t === '' ? null : Number(t) };
}
function readAdvForm() {
  const n = (id) => { const v = $(id).value.trim(); return v === '' ? null : Number(v); };
  return {
    provider: $('#sfProvider').value || null,
    base_url: $('#sfBase').value.trim(),
    model: $('#sfModel').value.trim(),
    api_key_env: $('#sfEnv').value.trim() || null,
    local: $('#sfLocal').checked,
    context_length: n('#sfCtx'),
    structured_mode: $('#sfMode').value,
    temperature: n('#sfTemp'),
    reasoning_budget: n('#sfReason'),
    price_per_mtok_in: n('#sfPin'),
    price_per_mtok_out: n('#sfPout'),
  };
}
function advDirty() {
  if (!state.sp || !$('#sfRevert')) return false;
  const dirty = JSON.stringify(state.sp.gliner ? readGlinerForm() : readAdvForm()) !== state.sp.orig;
  $('#sfDirty').hidden = !dirty;
  $('#sfRevert').disabled = !dirty;
  return dirty;
}
function updateKeyState() {
  const box = $('#sfKeyState');
  if (!box) return;
  const env = $('#sfEnv').value.trim();
  const src = (state.sp && state.sp.src) || {};
  if (!env) { box.innerHTML = $('#sfLocal').checked ? '' : `<span class="keyst bad">${ICON.key}none set</span>`; return; }
  const valid = ENV_RE.test(env);
  const present = valid ? secretPresent(env, env === src.api_key_env ? src.key_present : null) : null;
  box.innerHTML = !valid ? chip('invalid name', STATUS_COLORS.FAILED, '', 'must match ^[A-Z][A-Z0-9_]{2,63}$')
    : present ? `<span class="keyst ok">${ICON.key}present</span>` : present === false ? `<span class="keyst bad">${ICON.key}missing</span>` : `<span class="keyst na">${ICON.key}unknown</span>`;
}
function onAdvInput(el) {
  if (el.id === 'sfProvider') {
    const pr = presetFor(el.value);
    if (pr) { if (pr.base_url) $('#sfBase').value = pr.base_url; $('#sfLocal').checked = !!pr.local; }
  }
  updateKeyState();
  advDirty();
}

async function sfSave(btn) {
  const sp = state.sp;
  if (!sp) return;
  const partial = sp.gliner ? readGlinerForm() : readAdvForm();
  if (!sp.gliner && partial.api_key_env && !ENV_RE.test(partial.api_key_env)) { toast('Invalid env var name', partial.api_key_env, 'error'); return; }
  btn.disabled = true;
  const eng = engineById(sp.engine);
  const target = await saveToBuild(sp.profile, sp.gliner ? { gliner: partial } : { slots: { [sp.endpoint]: partial } }, `the ${endpointLabel(eng, sp.endpoint)} settings`);
  btn.disabled = false;
  applyCtl($('#inspBody'));
  if (target && state.insp.current && state.insp.current.kind === 'slot') inspect({ ...state.insp.current, profile: target }, { push: false });
}

async function sfTest(btn) {
  const sp = state.sp;
  if (!sp) return;
  const dirty = advDirty();
  btn.disabled = true;
  let job;
  try { job = await API.testSlot(sp.profile, sp.endpoint); } catch { btn.disabled = false; applyCtl($('#inspBody')); return; }
  btn.disabled = false;
  const key = `${sp.profile}|${sp.endpoint}`;
  const entry = { jobId: job.job_id, status: 'running', job: null, dirty, at: new Date() };
  state.slotTests[key] = entry;
  jobStarted(job);
  renderSlotTest(sp.profile, sp.endpoint);
  const showing = () => state.sp && `${state.sp.profile}|${state.sp.endpoint}` === key && $('#sfTest');
  trackJob(job.job_id, {
    onUpdate: (j) => { entry.job = j; if (showing()) renderSlotTest(sp.profile, sp.endpoint); },
    onDone: (j) => {
      entry.job = j;
      entry.verdict = smokeVerdict(j, [sp.endpoint]);
      entry.status = entry.verdict.ok ? 'passed' : 'failed';
      if (showing()) renderSlotTest(sp.profile, sp.endpoint);
    },
  });
}

function renderSlotTest(profile, endpoint) {
  const box = $('#sfTest');
  if (!box) return;
  updateKeyState();
  const t = state.slotTests[`${profile}|${endpoint}`];
  if (!t) { box.innerHTML = ''; return; }
  const lines = ((t.job && t.job.log) || []).slice(-12);
  const logHtml = `<pre class="log mini" tabindex="0" aria-label="Test log tail">${lines.length ? lines.map((l) => esc(l)).join('\n') : '<span class="muted">(no output yet)</span>'}</pre>`;
  const head = `<div class="test-head"><span class="cap">Structured-output test</span><button type="button" class="linkish mono" data-action="goto-job" data-id="${esc(t.jobId)}" title="Open in Run → Jobs">${esc(shortId(t.jobId))}</button><span class="muted">${esc(t.at.toLocaleTimeString('en-GB'))}</span></div>`;
  if (t.status === 'running') {
    box.innerHTML = `${head}<div class="verdict run"><span class="spinner" aria-hidden="true"></span><b>Testing…</b><span class="muted">polling every 2 s</span></div>${logHtml}`;
    return;
  }
  const v = t.verdict || { ok: false, per: [] };
  const e = (v.per[0] || {}).entry || {};
  const tick = (x) => (x === true ? '<span class="ok-t">yes</span>' : x === false ? '<span class="bad-t">no</span>' : '<span class="muted">—</span>');
  const mode = (m) => (!m ? '<span class="muted">—</span>' : m.ok ? '<span class="ok-t">works</span>' : `<span class="bad-t" title="${esc(m.error || '')}">fails</span>`);
  const sel = $('#sfMode');
  box.innerHTML = `${head}
    <div class="verdict ${v.ok ? 'pass' : 'fail'}" role="status"><b>${v.ok ? 'PASS' : 'FAIL'}</b><span>${esc(v.ok ? (v.per[0] || {}).msg : ((v.per[0] || {}).msg || v.msg || 'failed'))}</span></div>
    ${t.dirty ? '<div class="hint">Tested the saved settings — unsaved edits were not included.</div>' : ''}
    ${v.per.length ? `<div class="kv wide" style="margin-top:8px">
      <span>Reachable</span><span>${tick(e.reachable)}</span>
      <span>Model listed</span><span>${tick(e.model_listed)}</span>
      <span>strict_schema</span><span>${mode(e.strict_schema)}</span>
      <span>json_then_validate</span><span>${mode(e.json_then_validate)}</span>
      <span>Recommended</span><span class="mono">${esc(e.recommended || '—')}${e.recommended && sel && sel.value !== e.recommended ? ` <button type="button" class="btn sm" data-action="sf-apply-mode" data-mode="${esc(e.recommended)}">Use it</button>` : ''}</span>
    </div>` : ''}
    <details style="margin-top:8px"${v.ok ? '' : ' open'}><summary class="cap" style="cursor:pointer">Log tail</summary>${logHtml}</details>`;
}

/* ================================================================ view: servers (the plug-and-play bay) */
const srvUi = { open: new Set(), kind: 'lmstudio' };

async function viewServers(_params, alive) {
  const ws = $('#workspace');
  ws.innerHTML = `<div class="view">
    <div class="view-head"><h1>Servers</h1><span class="muted">start or connect a model server, then plug its models into an engine in the Garage</span><span class="spacer"></span>
      <button type="button" class="btn sm" data-action="srv-refresh" id="srvRefresh">Probe all</button></div>
    ${demoBanner('detecting, starting, adding and editing servers is disabled')}
    <div class="grid-2 srv-intro">
      <section class="panel" aria-labelledby="detH"><div class="panel-head"><span class="cap" id="detH">Detect local servers</span><span class="spacer"></span>
          <button type="button" class="btn sm primary" data-ctl data-action="srv-detect" id="detectBtn">Detect</button></div>
        <div class="panel-body" id="detectBox"><div class="muted">Probes this machine: <span class="mono">1234</span> LM Studio · <span class="mono">11434</span> Ollama · <span class="mono">8000</span> vLLM · <span class="mono">8080</span> llama.cpp · <span class="mono">1337</span> Jan.</div></div></section>
      <section class="panel" aria-labelledby="addH"><div class="panel-head"><span class="cap" id="addH">Add a server</span><span class="muted">by host and port, or a full base URL</span></div>
        <form class="panel-body" id="addSrvForm" autocomplete="off"></form></section>
    </div>
    <section class="panel" aria-labelledby="lnH"><div class="panel-head"><span class="cap" id="lnH">Start a server</span><span class="muted">the console runs the server's own command on this machine and adds it to the list</span></div>
      <div class="panel-body" id="launchBox">${skeleton(2)}</div></section>
    <section class="panel" aria-labelledby="slH"><div class="panel-head"><span class="cap" id="slH">Servers</span><span class="muted" id="srvCount"></span><span class="spacer"></span><span class="muted" id="srvAt"></span></div>
      <div class="panel-body" id="srvList">${skeleton(4)}</div></section>
  </div>`;
  await Promise.allSettled([loadServers(true), loadLaunchers(), loadPresets(), loadSecrets(true), loadProfiles()]);
  if (!alive()) return;
  renderAddServerForm();
  renderLaunchers();
  renderServerList();
  if (state.detected) renderDetected();
  applyCtl($('#workspace'));
  scheduleServersLive(alive);
}

async function loadLaunchers() {
  try { state.launchers = (await API.launchers({ quiet: true })) || []; state.launchersError = null; } catch (e) { state.launchersError = e; }
}
function managedFor(kind) {
  return (state.servers || []).find((s) => s.managed && s.kind === kind) || null;
}
// While a console-started server runs, keep its log and every status light fresh (in place: inputs keep their values).
function scheduleServersLive(alive) {
  const managed = (state.servers || []).some((s) => s.managed && s.managed.running);
  if (!managed) return;
  schedule('view:servers', async () => {
    if (!alive()) return;
    await loadServers(true);
    if (!alive()) return;
    updateServersLive();
    scheduleServersLive(alive);
  }, 5000);
}
function updateServersLive() {
  for (const s of state.servers || []) {
    const id = cssId(s.id);
    const l = $(`#sl-${id}`);
    if (l) { const st = serverState(s); l.className = `light ${st.cls}`; l.title = st.text; l.setAttribute('aria-label', st.text); }
    const t = $(`#ss-${id}`);
    if (t) t.innerHTML = serverStatusText(s);
  }
  for (const lc of state.launchers || []) {
    const m = managedFor(lc.kind);
    const pre = $(`#ll-${cssId(lc.kind)}`);
    if (pre && m && m.managed) {
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
      pre.textContent = (m.managed.log || []).join('\n') || '(no output yet)';
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    }
  }
  const at = $('#srvAt');
  if (at && state.serversAt) at.textContent = `probed ${state.serversAt.toLocaleTimeString('en-GB')}`;
}

function serverKindOptions() {
  const ps = state.presets && state.presets.length ? state.presets : SERVER_KINDS.map((k) => ({ id: k, label: k, local: LOCAL_KINDS.has(k), needs_key: !LOCAL_KINDS.has(k) && k !== 'custom', base_url: '' }));
  return ps;
}
function renderAddServerForm() {
  const form = $('#addSrvForm');
  if (!form) return;
  const ps = serverKindOptions();
  if (!ps.some((p) => p.id === srvUi.kind)) srvUi.kind = (ps[0] || {}).id;
  form.innerHTML = `<div class="form-grid">
      <label for="asKind">Kind</label>
      <select id="asKind">${ps.map((p) => `<option value="${esc(p.id)}"${p.id === srvUi.kind ? ' selected' : ''}>${esc(p.label)}${p.local ? ' · local' : ' · cloud'}</option>`).join('')}</select>
      <label for="asHost">Host : port</label>
      <div class="sf-row"><input id="asHost" type="text" class="mono" spellcheck="false" placeholder="localhost"><span class="muted">:</span><input id="asPort" type="number" min="1" max="65535" class="mono port" placeholder="port"></div>
      <label for="asUrl">or base URL</label>
      <input id="asUrl" type="text" class="mono" spellcheck="false" placeholder="https://api.example.com/v1">
      <label for="asName">Name</label>
      <input id="asName" type="text" spellcheck="false" placeholder="shown in the Garage">
      <label for="asEnv" class="as-key">Key name</label>
      <div class="as-key"><input id="asEnv" type="text" class="mono" spellcheck="false" placeholder="SYNTH_T3_API_KEY" style="width:100%"><div class="hint">A label for your API key, <b>not the key itself</b>. Keep the suggested one. After you add the server, paste the actual key into <b>Save key</b> on its card below.</div></div>
    </div>
    <div class="form-actions"><button type="submit" class="btn primary" data-ctl id="asSubmit">Add server</button><span class="hint" id="asHint"></span></div>`;
  const fill = () => {
    const p = ps.find((x) => x.id === $('#asKind').value) || {};
    srvUi.kind = p.id;
    let url = null;
    try { url = p.base_url && !p.base_url.includes('<') ? new URL(p.base_url) : null; } catch { url = null; }
    const local = !!p.local;
    $('#asHost').value = local && url ? url.hostname : local ? 'localhost' : '';
    $('#asPort').value = local && url ? url.port : '';
    $('#asUrl').value = !local && p.base_url && !p.base_url.includes('<') ? p.base_url : '';
    $('#asUrl').placeholder = p.base_url || 'https://api.example.com/v1';
    $('#asName').placeholder = p.label || 'shown in the Garage';
    const needsKey = p.needs_key || (!local && p.id !== 'custom');
    $('#asEnv').value = needsKey ? `SYNTH_${String(p.id || 'X').toUpperCase().replace(/[^A-Z0-9]/g, '_')}_API_KEY` : '';
    form.querySelectorAll('.as-key').forEach((el) => { el.hidden = local && !p.needs_key; });
    $('#asHost').closest('.sf-row').previousElementSibling.textContent = local ? 'Host : port' : 'Host : port (or URL)';
    $('#asHint').textContent = local ? 'local: /v1 is appended for you' : needsKey ? 'cloud: needs an API key' : '';
  };
  $('#asKind').addEventListener('change', fill);
  form.addEventListener('submit', (e) => { e.preventDefault(); addServer($('#asSubmit')); });
  fill();
}

async function addServer(btn) {
  const kindId = $('#asKind').value;
  const p = serverKindOptions().find((x) => x.id === kindId) || {};
  const url = $('#asUrl').value.trim();
  const host = $('#asHost').value.trim();
  const port = $('#asPort').value.trim();
  const typed = $('#asEnv').value.trim();
  // a pasted API key is never turned into a label (it would be stored in plain text): refuse and clear it
  if (/^(sk-|sk_|gsk_|xai-|AIza|or-)/i.test(typed) || (typed.length >= 24 && /[a-z]/.test(typed) && /\d/.test(typed))) {
    $('#asEnv').value = '';
    toast('That looks like the key itself', 'Key name is only a label (e.g. SYNTH_T3_API_KEY). Add the server, then paste the key into Save key on its card.', 'error');
    $('#asEnv').focus(); return;
  }
  const env = typed ? typed.toUpperCase().replace(/[^A-Z0-9_]+/g, '_').replace(/^[^A-Z]+/, '').slice(0, 64) : '';
  if (typed && env !== typed) $('#asEnv').value = env;
  if (!url && !host) { toast('Enter a host and port, or a base URL', '', 'error'); $('#asHost').focus(); return; }
  if (env && !ENV_RE.test(env)) { toast('Invalid env var name', 'capitals, digits and _, starting with a letter', 'error'); return; }
  const body = { kind: SERVER_KINDS.includes(kindId) ? kindId : 'custom', name: $('#asName').value.trim() || p.label || kindId };
  if (url) body.base_url = url; else { body.host = host; if (port) body.port = Number(port); }
  if (env) body.api_key_env = env;
  btn.disabled = true;
  try {
    const s = await API.addServer(body);
    toast('Server added', `${(s && s.name) || body.name} · ${(s && s.base_url) || url || `${host}:${port}`}`, 'ok');
    $('#asName').value = '';
  } catch { return; } finally { btn.disabled = false; applyCtl($('#addSrvForm')); }
  await loadServers(true);
  renderServerList();
}

async function detectServers(btn) {
  btn.disabled = true;
  $('#detectBox').innerHTML = '<div class="hint"><span class="spinner" aria-hidden="true" style="width:10px;height:10px"></span> probing localhost…</div>';
  try { state.detected = (await API.detect()) || []; } catch (e) { $('#detectBox').innerHTML = errorBox(e, 'Detection failed'); return; } finally { btn.disabled = false; applyCtl(btn.parentElement); }
  renderDetected();
}
function renderDetected() {
  const box = $('#detectBox');
  if (!box) return;
  const ds = state.detected || [];
  if (!ds.length) { box.innerHTML = emptyState('Nothing answered on the usual ports', '<div>Start a server below, or add one by address.</div>'); return; }
  box.innerHTML = `<div class="det-list">${ds.map((d, i) => {
    const saved = d.saved || !!serverForUrl(d.base_url);
    const n = (d.models || []).length;
    return `<div class="det-row">${light(n ? 'ok' : 'warn', n ? 'answering' : 'answering, no models')}<b>${esc(d.name || d.kind)}</b><span class="mono muted" title="${esc(d.base_url)}">${esc(d.base_url)}</span>
      <span class="muted nowrap">${esc(n)} model${n === 1 ? '' : 's'}</span>
      ${saved ? chip('saved', STATUS_COLORS.COMMITTED) : `<button type="button" class="btn sm primary" data-ctl data-action="det-add" data-i="${i}">Add</button>`}</div>`;
  }).join('')}</div>`;
  applyCtl(box);
}
async function addDetected(i, btn) {
  const d = (state.detected || [])[Number(i)];
  if (!d) return;
  btn.disabled = true;
  try {
    await API.addServer({ kind: SERVER_KINDS.includes(d.kind) ? d.kind : 'custom', name: d.name || d.kind, base_url: d.base_url });
    toast('Server added', `${d.name} · ${d.base_url}`, 'ok');
    d.saved = true;
  } catch { btn.disabled = false; return; }
  await loadServers(true);
  renderDetected();
  renderServerList();
}

function renderLaunchers() {
  const box = $('#launchBox');
  if (!box) return;
  if (state.launchersError && !state.launchers) { box.innerHTML = errorBox(state.launchersError, 'Could not list launchers (GET /api/launchers)'); return; }
  const ls = state.launchers || [];
  if (!ls.length) { box.innerHTML = '<div class="muted">No launchers offered by the server.</div>'; return; }
  box.innerHTML = `<div class="launch-grid">${ls.map((lc) => {
    const id = cssId(lc.kind);
    const srv = managedFor(lc.kind);
    const m = srv && srv.managed;
    const running = !!(m && m.running);
    const tool = String(lc.command || '').split(/\s+/)[0];
    return `<div class="launcher${running ? ' running' : ''}${lc.available ? '' : ' unavailable'}">
      <div class="lt-top">${light(running ? 'ok' : lc.available ? 'off' : 'bad', running ? 'running' : lc.available ? `${tool} found on PATH` : `${tool} not on PATH`)}<b>${esc(lc.label || lc.kind)}</b>
        ${running ? chip('running', STATUS_COLORS.COMMITTED, 'solid') : lc.available ? chip('ready', '#4ea1ff') : chip('not installed', GREY)}</div>
      <code class="lt-cmd" title="the command the console runs">${esc(lc.command || '')}</code>
      ${lc.available ? '' : `<div class="hint">${codeTicks(lc.hint || LAUNCH_HINTS[lc.kind] || `Install ${lc.label || lc.kind} so that ${tool} is on PATH, then restart the console.`)}</div>`}
      <div class="lt-form">
        <label>Port <input type="number" id="lp-${id}" class="mono port" min="1" max="65535" value="${esc(lc.default_port ?? '')}"${running ? ' disabled' : ''}></label>
        ${lc.needs_model_path ? `<label class="grow">Model file <input type="text" id="lm-${id}" class="mono" spellcheck="false" placeholder="C:\\models\\model.gguf"${running ? ' disabled' : ''}></label>` : ''}
      </div>
      <div class="lt-actions">
        <button type="button" class="btn sm primary" data-ctl data-action="launch" data-kind="${esc(lc.kind)}"${!lc.available || running ? ' disabled' : ''}>Start</button>
        <button type="button" class="btn sm" data-ctl data-action="unlaunch" data-kind="${esc(lc.kind)}"${running ? '' : ' disabled title="only servers the console started can be stopped here"'}>Stop</button>
        ${m ? `<span class="muted">${running ? `pid ${esc(m.pid ?? '—')} · ` : 'stopped · '}<a href="#srv-${cssId(srv.id)}" data-action="srv-focus" data-id="${esc(srv.id)}">${esc(srv.name || srv.id)}</a></span>` : ''}
      </div>
      ${m ? `<pre class="log mini" id="ll-${id}" tabindex="0" aria-label="${esc(lc.label)} server log">${esc((m.log || []).join('\n') || '(no output yet)')}</pre>` : ''}
    </div>`;
  }).join('')}</div>`;
  box.querySelectorAll('pre.log').forEach((pre) => { pre.scrollTop = pre.scrollHeight; });
  applyCtl(box);
}

async function launchAction(action, kind, btn) {
  btn.disabled = true;
  if (action === 'launch') {
    const id = cssId(kind);
    const port = $(`#lp-${id}`) ? $(`#lp-${id}`).value.trim() : '';
    const mp = $(`#lm-${id}`) ? $(`#lm-${id}`).value.trim() : '';
    const lc = (state.launchers || []).find((x) => x.kind === kind) || {};
    if (lc.needs_model_path && !mp) { toast('Give the model file first', `${lc.label} serves one .gguf file`, 'error'); btn.disabled = false; $(`#lm-${id}`).focus(); return; }
    try {
      const res = await API.launch(kind, { port: port ? Number(port) : null, model_path: mp || null });
      toast(`${lc.label || kind} starting`, res && res.server ? res.server.base_url : '', 'ok');
    } catch { btn.disabled = false; return; }
  } else {
    try { await API.unlaunch(kind); toast('Server stopped', kind, 'ok'); } catch { btn.disabled = false; return; }
  }
  await loadServers(true);
  renderLaunchers();
  renderServerList();
  const seq = state.seq;
  scheduleServersLive(() => seq === state.seq);
}

// Escaped text with `code` spans (launcher hints use them).
function codeTicks(text) { return esc(text).replace(/`([^`]+)`/g, '<code>$1</code>'); }
function serverStatusText(s) {
  const st = serverState(s);
  if (st.cls === 'bad') return `<span class="bad-t">unreachable</span>${s.status && s.status.error ? ` <span class="muted">· ${esc(s.status.error)}</span>` : ''}`;
  if (st.cls === 'off') return '<span class="muted">not probed</span>';
  return `<span class="ok-t">online</span>${s.status.latency_ms !== null && s.status.latency_ms !== undefined ? ` <span class="num muted">· ${esc(s.status.latency_ms)} ms</span>` : ''}`;
}

function renderServerList() {
  const box = $('#srvList');
  if (!box) return;
  if (state.serversError && !state.servers) { box.innerHTML = errorBox(state.serversError, 'Could not list servers (GET /api/servers)'); return; }
  const ss = state.servers || [];
  $('#srvCount').textContent = ss.length ? `${ss.length} · ${ss.filter((s) => s.status && s.status.reachable).length} online` : '';
  if (state.serversAt) $('#srvAt').textContent = `probed ${state.serversAt.toLocaleTimeString('en-GB')}`;
  if (!ss.length) { box.innerHTML = emptyState('No servers yet', '<div>Detect one that is already running, start one, or add an address above.</div>'); return; }
  box.innerHTML = `<div class="srv-grid">${ss.map(serverCardHtml).join('')}</div>`;
  box.querySelectorAll('details.srv-models').forEach((d) => d.addEventListener('toggle', () => { if (d.open) srvUi.open.add(d.dataset.id); else srvUi.open.delete(d.dataset.id); }));
  ss.forEach((s) => renderServerSpeed(s.id));
  refreshSlotPanel();
  box.querySelectorAll('[data-srv-model]').forEach((sel) => sel.addEventListener('change', () => renderServerSpeed(sel.dataset.srvModel)));
  box.querySelectorAll('[data-key-input]').forEach((inp) => inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); keySave(inp.dataset.keyInput, inp.closest('.srv-key').querySelector('[data-action="key-save"]')); } }));
  applyCtl(box);
}

function serverCardHtml(s) {
  const id = cssId(s.id);
  const models = serverModels(s);
  const reach = !!(s.status && s.status.reachable);
  const used = s.used_by || [];
  const load = state.loads[s.id];
  return `<article class="srv-card${reach ? '' : ' down'}" id="srv-${id}" aria-labelledby="sn-${id}">
    <div class="srv-top">${serverLight(s, ` id="sl-${id}"`)}<b class="srv-name" id="sn-${id}">${esc(s.name || s.id)}</b>${provChip(s.kind, s.kind)}${locBadge(s.local)}${s.managed ? chip(s.managed.running ? 'started here' : 'stopped', s.managed.running ? '#4ea1ff' : GREY, '', 'managed by the console') : ''}${s.auto ? plainChip('auto', 'added automatically because a build uses this base URL') : ''}
      <span class="spacer"></span><span id="ss-${id}" class="srv-st">${serverStatusText(s)}</span></div>
    <div class="srv-url mono" title="${esc(s.base_url)}">${esc(s.base_url)}</div>
    <details class="srv-models" data-id="${esc(s.id)}"${srvUi.open.has(s.id) ? ' open' : ''}><summary><b class="num">${esc(models.length)}</b> model${models.length === 1 ? '' : 's'}${reach ? '' : ' <span class="muted">(last probe failed)</span>'}</summary>
      ${models.length ? `<ul class="model-list">${models.map((m) => `<li class="mono" title="${esc(m)}">${esc(m)}</li>`).join('')}</ul>` : `<div class="hint">${reach ? 'No models loaded.' : 'Start the server to list its models.'}${LOADABLE.has(s.kind) ? ' Use Load model to pull one.' : ''}</div>`}
    </details>
    <div class="srv-used"><span class="cap">Used by</span>${used.length ? used.map((u) => { const [pn, ep] = String(u).split('/'); return `<button type="button" class="chip plain" data-action="goto-build" data-profile="${esc(pn)}" title="Open build ${esc(pn)} in the Garage (endpoint ${esc(ep || '')})"><i class="dot"></i>${esc(pn)} · ${esc(ep || '')}</button>`; }).join('') : '<span class="muted">no build</span>'}</div>
    ${s.local === false ? serverKeyHtml(s) : ''}
    <div class="srv-speed">
      <div class="sf-row"><select data-srv-model="${esc(s.id)}" id="sm-${id}" aria-label="Model to speed-test on ${esc(s.name)}"${models.length ? '' : ' disabled'}>${models.length ? models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join('') : '<option value="">no models</option>'}</select>
        <button type="button" class="btn sm" data-ctl data-action="srv-speed" data-id="${esc(s.id)}"${models.length ? '' : ' disabled'}>Speed test</button></div>
      <div id="spd-${id}" aria-live="polite"></div>
      ${load ? `<div class="hint">${load.status === 'running' ? '<span class="spinner" aria-hidden="true" style="width:10px;height:10px"></span> loading' : load.status === 'done' ? '<span class="ok-t">loaded</span>' : '<span class="bad-t">load failed</span>'} <span class="mono">${esc(load.model)}</span> · <button type="button" class="linkish mono" data-action="goto-job" data-id="${esc(load.jobId)}">${esc(shortId(load.jobId))}</button></div>` : ''}
    </div>
    <div class="srv-actions">
      <button type="button" class="btn sm" data-action="srv-refresh" title="Probe every server again">Refresh</button>
      ${LOADABLE.has(s.kind) ? `<button type="button" class="btn sm" data-ctl data-action="srv-load" data-id="${esc(s.id)}" title="${s.kind === 'ollama' ? 'ollama pull <model>' : 'lms load <model>'}">Load model…</button>` : ''}
      <button type="button" class="btn sm" data-ctl data-action="srv-edit" data-id="${esc(s.id)}">Edit…</button>
      <span class="spacer"></span>
      <button type="button" class="btn sm danger" data-ctl data-action="srv-remove" data-id="${esc(s.id)}">Remove</button>
    </div>
  </article>`;
}

function serverKeyHtml(s) {
  const env = s.api_key_env || '';
  const present = env ? secretPresent(env, !!s.key_present) : false;
  return `<div class="srv-key"><span class="cap">API key</span>
    <div class="sf-row"><input type="text" class="mono env" data-key-env="${esc(s.id)}" value="${esc(env || `SYNTH_${String(s.kind || 'CLOUD').toUpperCase()}_API_KEY`)}" spellcheck="false" aria-label="Env var name for ${esc(s.name)}'s key">
      ${present ? `<span class="keyst ok">${ICON.key}present</span>` : `<span class="keyst bad">${ICON.key}missing</span>`}</div>
    <div class="sf-row"><input type="password" data-key-input="${esc(s.id)}" autocomplete="new-password" spellcheck="false" placeholder="paste the key — it is never shown again" aria-label="API key value for ${esc(s.name)}">
      <button type="button" class="btn sm" data-ctl data-action="key-save" data-id="${esc(s.id)}">Save key</button>
      <button type="button" class="btn sm" data-ctl data-action="key-clear" data-id="${esc(s.id)}"${present ? '' : ' disabled'}>Clear</button></div>
  </div>`;
}

function renderServerSpeed(serverId) {
  const box = $(`#spd-${cssId(serverId)}`);
  const sel = $(`#sm-${cssId(serverId)}`);
  if (!box) return;
  const model = sel ? sel.value : '';
  box.innerHTML = model ? speedHtml(serverId, model) : '';
}

async function keySave(serverId, btn) {
  const s = serverById(serverId);
  const card = btn && btn.closest('.srv-key');
  if (!s || !card) return;
  const env = card.querySelector('[data-key-env]').value.trim();
  const val = card.querySelector('[data-key-input]').value;
  if (!ENV_RE.test(env)) { toast('Invalid env var name', 'capitals, digits and _ (3–64 chars), starting with a letter', 'error'); return; }
  if (!val.trim()) { toast('Paste a key first', '', 'error'); card.querySelector('[data-key-input]').focus(); return; }
  btn.disabled = true;
  try {
    await API.setSecret(env, val.trim());
    card.querySelector('[data-key-input]').value = '';
    if (env !== s.api_key_env) await API.editServer(s.id, { api_key_env: env });
    toast('Key saved', `${env} is present`, 'ok');
  } catch { btn.disabled = false; return; }
  await Promise.allSettled([loadSecrets(true), loadServers(true)]);
  state.profiles = null;
  await loadProfiles().catch(() => {});
  renderServerList();
}
async function keyClear(serverId) {
  const s = serverById(serverId);
  const env = s && s.api_key_env;
  if (!env) return;
  const ok = await askDialog({ title: `Clear ${env}?`, html: '<p>The stored value is removed from the server. A key set in the server process\'s own environment cannot be cleared from here.</p>', ok: 'Clear key', danger: true });
  if (!ok) return;
  try { await API.clearSecret(env); toast('Key cleared', env, 'ok'); } catch { return; }
  await Promise.allSettled([loadSecrets(true), loadServers(true)]);
  renderServerList();
}

async function serverAction(action, id, btn) {
  const s = serverById(id);
  if (action === 'srv-refresh') {
    const b = $('#srvRefresh');
    if (b) { b.disabled = true; b.textContent = 'Probing…'; }
    await loadServers(true);
    if (b) { b.disabled = false; b.textContent = 'Probe all'; }
    renderServerList();
    renderLaunchers();
    return;
  }
  if (!s) return;
  if (action === 'srv-speed') {
    const sel = $(`#sm-${cssId(id)}`);
    await runSpeedTest(id, sel ? sel.value : '', btn);
  } else if (action === 'srv-load') {
    const ans = await askDialog({
      title: `Load a model on ${s.name}`,
      html: `<p>${s.kind === 'ollama' ? 'Runs <span class="mono">ollama pull &lt;model&gt;</span>: downloads it if needed.' : 'Runs <span class="mono">lms load &lt;model&gt;</span>: loads a downloaded model into memory.'}</p>`,
      fields: [{ label: 'Model', placeholder: s.kind === 'ollama' ? 'qwen2.5:14b-instruct' : 'ministral-3-14b-reasoning-2512', mono: true }],
      ok: 'Load',
    });
    if (!ans || !ans[0]) return;
    let job;
    try { job = await API.loadModel(id, ans[0]); } catch { return; }
    state.loads[id] = { jobId: job.job_id, status: 'running', model: ans[0] };
    jobStarted(job);
    renderServerList();
    trackJob(job.job_id, {
      onDone: async (j) => {
        state.loads[id] = { jobId: job.job_id, status: j.status === 'done' ? 'done' : 'failed', model: ans[0] };
        await loadServers(true);
        if (state.view === 'servers') renderServerList();
      },
    });
  } else if (action === 'srv-edit') {
    const ans = await askDialog({
      title: `Edit ${s.name}`,
      fields: [{ label: 'Name', value: s.name || '' }, { label: 'Base URL', value: s.base_url || '', mono: true, hint: 'builds that use the old URL keep it — re-assign their slots in the Garage' },
        { label: 'API key env (optional)', value: s.api_key_env || '', mono: true, required: false, pattern: '[A-Z][A-Z0-9_]{2,63}' }],
      ok: 'Save',
    });
    if (!ans) return;
    try { await API.editServer(id, { name: ans[0], base_url: ans[1], api_key_env: ans[2] || null }); toast('Server saved', ans[0], 'ok'); } catch { return; }
    await loadServers(true);
    renderServerList();
  } else if (action === 'srv-remove') {
    const used = s.used_by || [];
    const ok = await askDialog({ title: `Remove ${s.name}?`, html: `<p>The server is removed from this list. ${used.length ? `Builds that use it keep their URL: <span class="mono">${esc(used.join(', '))}</span>.` : ''}${s.managed && s.managed.running ? ' The console-started process keeps running — stop it first if you want it gone.' : ''}</p>`, ok: 'Remove', danger: true });
    if (!ok) return;
    try { await API.removeServer(id); toast('Server removed', s.name, 'ok'); } catch { return; }
    await loadServers(true);
    renderServerList();
  }
}

/* ================================================================ view: run (corpus · setup · recent runs · live tree · jobs) */
const UPLOAD_EXT = ['.pdf', '.docx', '.txt', '.md', '.html'];
const UPLOAD_MAX = 100 * 1024 * 1024;
const RELATION_MODES = [['', 'build default'], ['micro', 'micro'], ['gliner', 'gliner'], ['gliner+micro', 'gliner+micro']];
const GLINER_BACKENDS = [['', 'default'], ['gliner2', 'gliner2'], ['heuristic', 'heuristic (regex stand-in)']];
// Relation modes choose how micro graphs get their relations, so they only exist for engines with a micro slot.
const hasMicroSlot = (eng) => !!eng && (eng.slots || []).some((sl) => sl.endpoint === 't1_micro');
const hasGlinerSlot = (eng) => !!eng && (eng.slots || []).some(isGlinerSlot);

async function viewRun(_params, alive) {
  const ws = $('#workspace');
  const ui = state.runsUi;
  ws.innerHTML = `<div class="view" id="runPage">
    <div class="view-head"><h1>Run</h1><span class="muted">pick an engine and a build, check it, start it, watch it run</span></div>
    ${demoBanner()}
    <div class="runs-grid">
      <section class="panel" aria-labelledby="setupH">
        <div class="panel-head"><span class="cap" id="setupH">Run setup</span><span class="spacer"></span><a href="#/garage">Garage ↗</a></div>
        <form class="panel-body" id="runForm">
          <div class="form-grid">
            <label for="fEngine">Engine</label><select id="fEngine"><option value="">Loading…</option></select>
            <label for="fProfile">Build</label><select id="fProfile"><option value="">Loading…</option></select>
            <label for="fOnto">Ontology</label><select id="fOnto"><option value="">Loading…</option></select>
            <label for="fGliner" class="opt-gliner">GLiNER backend</label>
            <select id="fGliner" class="opt-gliner">${GLINER_BACKENDS.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('')}</select>
            <label for="fRel" class="opt-rel">Relation mode</label>
            <select id="fRel" class="opt-rel">${RELATION_MODES.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('')}</select>
          </div>
          <div class="hint" id="engineNote"></div>
          <div class="cap" style="margin:12px 0 6px">Slots this run will use</div>
          <div id="stagePreview"></div>
          <div id="preflightBox" class="preflight"></div>
          <div id="startWarn"></div>
          <div class="form-actions">
            <button type="button" class="btn" data-ctl data-action="preflight" id="preflightBtn">Preflight</button>
            <button type="submit" class="btn primary" data-ctl id="startBtn">Start run</button>
            <button type="button" class="btn" data-ctl data-action="resume-run" id="resumeBtn">Resume selected run</button>
          </div>
        </form>
      </section>
      <section class="panel corpus-panel" aria-labelledby="corpusH">
        <div class="panel-head"><span class="cap" id="corpusH">Corpus</span><span class="mono muted">data/raw</span><span class="muted" id="corpusCount"></span><span class="spacer"></span>
          <button type="button" class="btn sm" data-action="corpus-refresh">Refresh</button>
          <button type="button" class="btn sm primary" data-ctl data-action="ingest-corpus" id="ingestBtn">Ingest corpus</button></div>
        <div class="panel-body">
          <label class="dropzone" id="dropzone">
            <input type="file" id="fileInput" multiple accept="${UPLOAD_EXT.join(',')}" class="sr-only" data-ctl>
            ${ICON.upload}
            <span><b>Drop files here</b> or <span class="linkish">choose files</span></span>
            <span class="muted">PDF · DOCX · TXT · MD · HTML · up to 100 MB each</span>
          </label>
          <div id="uploadStatus" aria-live="polite"></div>
        </div>
        <div class="panel-body flush corpus-list" id="corpusList">${skeleton(4)}</div>
        <div class="panel-foot">Ingest parses new files into stored documents (S1). Deleting a file only removes it from <span class="mono">data/raw</span>: documents already ingested stay in the store, which is append-only.</div>
      </section>
    </div>
    <section class="panel" aria-labelledby="recentH"><div class="panel-head"><span class="cap" id="recentH">Recent runs</span><span class="muted" id="recentScope">newest first · click to select</span><span class="spacer"></span><button type="button" class="btn sm" data-action="runs-archived" id="runsArchBtn" hidden>Show archived</button><button type="button" class="btn sm" data-action="runs-all" id="runsAllBtn">All engines</button><a href="#/dyno">Compare on the Dyno ↗</a></div>
      <div class="panel-body flush recent-runs" id="recentRuns">${skeleton(3)}</div></section>
    <section class="panel" id="runSel" aria-label="Selected run"><div class="panel-body">${skeleton(4)}</div></section>
    <section class="panel"><div class="panel-head"><span class="cap">Jobs</span><span class="muted">newest first · click a job to tail its log</span></div>
      <div class="panel-body flush" id="jobsTable">${skeleton(3)}</div></section>
    <section class="panel" id="jobLog" hidden></section>
  </div>`;
  $('#fRel').value = ui.rel;
  $('#fGliner').value = ui.gliner;
  $('#runForm').addEventListener('submit', (e) => { e.preventDefault(); startRun(false); });
  $('#runForm').addEventListener('change', (e) => {
    if (e.target.id === 'fEngine') ui.engine = e.target.value;
    if (e.target.id === 'fProfile') ui.profile = e.target.value;
    if (e.target.id === 'fOnto') ui.ontology = e.target.value;
    if (e.target.id === 'fRel') ui.rel = e.target.value;
    if (e.target.id === 'fGliner') ui.gliner = e.target.value;
    $('#startWarn').innerHTML = '';
    if (e.target.id === 'fEngine') renderRecentRuns();
    renderRunEngineSelect();
    renderStagePreview();
    renderPreflight();
  });
  wireDropzone();
  renderRunsButtons();
  await Promise.allSettled([loadEngines(), refreshJobs(), refreshOverview(true), refreshRuns(true), loadProfiles(), loadServers(), loadSecrets(), loadCorpus(), loadOntologies()]);
  if (!alive()) return;
  renderRunEngineSelect();
  renderRunProfileSelect();
  renderRunOntologySelect();
  renderStagePreview();
  renderPreflight();
  renderCorpus();
  renderRecentRuns();
  renderJobsTable();
  renderRunsButtons();
  const sel = state.control.selectedJob || (state.jobs.find((j) => j.status === 'running') || {}).job_id;
  if (sel) selectJob(sel);
  await loadRunLive(alive);
}

function renderRunEngineSelect() {
  const sel = $('#fEngine');
  if (!sel) return;
  const es = state.engines || [];
  if (!engineById(state.runsUi.engine)) state.runsUi.engine = (defaultEngine() || {}).id || '';
  sel.innerHTML = es.length ? es.map((e) => `<option value="${esc(e.id)}"${e.id === state.runsUi.engine ? ' selected' : ''}>${esc(e.name)}${e.default ? ' (default)' : ''} — ${esc(e.tagline || e.status || '')}</option>`).join('')
    : `<option value="">${state.enginesError ? 'engine registry unavailable' : 'no engines'}</option>`;
  const eng = engineById(state.runsUi.engine);
  const showG = hasGlinerSlot(eng);
  const showR = hasMicroSlot(eng);
  document.querySelectorAll('.opt-gliner').forEach((el) => { el.hidden = !showG; });
  document.querySelectorAll('.opt-rel').forEach((el) => { el.hidden = !showR; });
  $('#engineNote').innerHTML = eng ? `${engineStatusChip(eng)} ${esc(eng.description || '')}` : '';
}

function renderRunProfileSelect() {
  const sel = $('#fProfile');
  if (!sel) return;
  const ps = state.profiles || [];
  if (!state.runsUi.profile || !profileByName(state.runsUi.profile)) state.runsUi.profile = (activeProfile() || {}).name || '';
  sel.innerHTML = ps.length ? ps.map((p) => `<option value="${esc(p.name)}"${p.name === state.runsUi.profile ? ' selected' : ''}>${esc(p.name)}${p.active ? ' (active)' : ''}</option>`).join('') : '<option value="">no builds found</option>';
}

function renderStagePreview() {
  const box = $('#stagePreview');
  if (!box) return;
  const p = profileByName(state.runsUi.profile);
  const eng = engineById(state.runsUi.engine);
  if (!p || !eng) { box.innerHTML = `<div class="muted">${!eng ? 'No engine selected.' : 'No build selected.'}</div>`; return; }
  box.innerHTML = `<table class="data preview"><colgroup><col style="width:176px"><col><col style="width:44px"></colgroup><tbody>${(eng.slots || []).map((sl) => {
    const s = slotCfg(p, sl) || {};
    const ok = slotReady(p, sl);
    const gl = isGlinerSlot(sl);
    return `<tr class="${ok ? '' : sl.required === false ? 'opt-row' : 'unconf-row'}">
      <td>${slotTag(eng, sl)}${sl.required === false ? ' <span class="muted" style="font-size:10.5px">optional</span>' : ''}</td>
      <td class="pv-model"><span class="mono" title="${esc(s.model || '')}">${ok ? esc(s.model) : `<span style="color:${sl.required === false ? 'var(--muted)' : 'var(--warn)'}">${ICON.warn} ${sl.required === false ? 'not configured — skipped' : 'empty slot'}</span>`}</span>
        <span class="pv-sub">${slotWhereHtml(eng, sl, s)}${gl ? '' : `${locBadge(s.local)}${keyStatus(!!s.local, !!keyPresent(s.api_key_env, s.key_present), s.api_key_env)}`}</span></td>
      <td class="r"><button type="button" class="linkish" data-action="edit-slot" data-engine="${esc(eng.id)}" data-slot="${esc(sl.id)}" data-profile="${esc(p.name)}" aria-label="Plug a model into ${esc(sl.label)}">${ok ? 'edit' : 'plug'}</button></td>
    </tr>`;
  }).join('')}</tbody></table>`;
}

function preflightKey() { return `${state.runsUi.engine}|${state.runsUi.profile}`; }
function renderPreflight() {
  const box = $('#preflightBox');
  if (!box) return;
  const pf = state.preflight[preflightKey()];
  const eng = engineById(state.runsUi.engine);
  const need = requiredEndpoints(eng).map((ep) => endpointLabel(eng, ep));
  let status;
  if (!pf) status = `${chip('NOT RUN', GREY)}<span class="muted">${need.length ? `tests ${esc(need.join(', '))}${(eng.slots || []).some((s) => s.required === false && !isGlinerSlot(s)) ? ' (+ optional slots when configured)' : ''}` : 'this engine has no model endpoints to test'}</span>`;
  else if (pf.status === 'running') status = `${chip('RUNNING', STATUS_COLORS.RUNNING)}<span class="spinner" aria-hidden="true" style="width:10px;height:10px"></span><button type="button" class="linkish mono" data-action="job" data-id="${esc(pf.jobId)}">${esc(shortId(pf.jobId))}</button>`;
  else status = `${chip(pf.status === 'passed' ? 'PASSED' : 'FAILED', pf.status === 'passed' ? STATUS_COLORS.COMMITTED : STATUS_COLORS.FAILED, 'solid')}<span class="muted">${esc(pf.at.toLocaleTimeString('en-GB'))}</span><button type="button" class="linkish mono" data-action="job" data-id="${esc(pf.jobId)}">${esc(shortId(pf.jobId))}</button>`;
  const per = pf && pf.verdict ? pf.verdict.per : [];
  const slotFor = (ep) => eng && (eng.slots || []).find((s) => s.endpoint === ep);
  box.innerHTML = `<div class="pf-head"><span class="cap">Preflight</span>${status}</div>
    ${per.length ? `<div class="pf-rows">${per.map((x) => { const sl = slotFor(x.slot); return `<div class="pf-row ${x.ok ? 'ok' : 'bad'}"><span class="pf-mark" aria-label="${x.ok ? 'pass' : 'fail'}">${x.ok ? '✓' : '✕'}</span><span class="pf-slot" title="endpoint ${esc(x.slot)}">${esc(sl ? sl.label : x.slot)}</span><span class="pf-msg">${esc(x.msg)}</span>${x.ok || !sl ? '' : `<button type="button" class="linkish" data-action="edit-slot" data-engine="${esc(eng.id)}" data-slot="${esc(sl.id)}" data-profile="${esc(state.runsUi.profile)}">fix</button>`}</div>`; }).join('')}</div>`
    : pf && pf.verdict && pf.verdict.msg ? `<div class="pf-rows"><div class="pf-row bad"><span class="pf-mark">✕</span><span></span><span class="pf-msg">${esc(pf.verdict.msg)}</span></div></div>` : ''}`;
}

async function runPreflight(btn) {
  const { engine, profile } = state.runsUi;
  const key = `${engine}|${profile}`;
  const eng = engineById(engine);
  await withBusy(btn, async () => {
    const job = await API.preflight(engine, profile);
    state.preflight[key] = { status: 'running', jobId: job.job_id, at: new Date() };
    jobStarted(job);
    renderPreflight();
    trackJob(job.job_id, {
      onDone: (j) => {
        const v = smokeVerdict(j, null, requiredEndpoints(eng));
        state.preflight[key] = { status: v.ok ? 'passed' : 'failed', jobId: job.job_id, verdict: v, at: new Date() };
        toast(`Preflight ${v.ok ? 'passed' : 'failed'}`, `${engineName(engine)} · ${profile}`, v.ok ? 'ok' : 'error');
        if (state.view === 'run') renderPreflight();
      },
    });
  }).catch(() => {});
}

function renderRunsButtons() {
  const resume = $('#resumeBtn');
  if (resume) {
    resume.disabled = !state.runId;
    resume.title = state.runId ? `Resume ${state.runId}` : 'Select a run first';
  }
  applyCtl($('#workspace'));
}

function renderRecentRuns() {
  const box = $('#recentRuns');
  if (!box) return;
  const ui = state.runsUi;
  const scoped = ui.allEngines || !ui.engine ? state.runs : state.runs.filter((x) => x.arm === ui.engine);
  const hidden = scoped.filter((x) => x.archived).length;
  const runs = state.showArchived ? scoped : scoped.filter((x) => !x.archived);
  const arcBtn = $('#runsArchBtn');
  if (arcBtn) { arcBtn.hidden = !hidden; arcBtn.textContent = state.showArchived ? 'Hide archived' : `Show archived (${hidden})`; }
  const scope = $('#recentScope');
  if (scope) scope.textContent = ui.allEngines || !ui.engine ? 'all engines · newest first' : `${engineName(ui.engine)} only · newest first`;
  const allBtn = $('#runsAllBtn');
  if (allBtn) { allBtn.textContent = ui.allEngines ? `Only ${engineName(ui.engine)}` : 'All engines'; allBtn.setAttribute('aria-pressed', String(!!ui.allEngines)); }
  if (!runs.length) { box.innerHTML = `<div class="panel-body">${emptyState(state.runs.length ? `No ${engineName(ui.engine)} runs yet` : 'No runs yet', '<div>Set one up above and press Start run.</div>')}</div>`; return; }
  box.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr><th>Run</th><th>Engine</th><th>Build</th><th>Status</th><th>Started</th><th class="r">Duration</th><th class="r">Docs</th><th class="r">Entities</th><th class="r">Committed</th><th class="r">Held</th></tr></thead><tbody>
    ${runs.map((x) => {
    const c = x.counts || {};
    const sel = x.run_id === state.runId;
    const eng = engineById(x.arm);
    return `<tr class="clickable${sel ? ' selected' : ''}" data-action="select-run" data-id="${esc(x.run_id)}" data-stay="1">
      <td><button type="button" class="linkish mono" data-action="select-run" data-id="${esc(x.run_id)}" data-stay="1" aria-pressed="${sel}" title="${esc(x.run_id)}">${esc(shortId(x.run_id))}</button></td>
      <td class="nowrap"><span class="ink">${esc(engineName(x.arm))}</span> ${eng ? '' : `<span class="muted mono">${esc(x.arm)}</span>`}</td>
      <td class="mono ellip" title="${esc(x.profile || '')}">${esc(x.profile || '—')}</td>
      <td>${statusChip(x.status)}${isRunLive(x.run_id) ? ' <span class="live-dot" aria-label="running"></span>' : ''}${x.archived ? ` ${plainChip('archived')}` : ''}</td>
      <td class="num">${esc(fmtTime(x.started_at))}</td>
      <td class="r num">${esc(fmtDur(secondsBetween(x.started_at, x.finished_at)))}</td>
      <td class="r num">${esc(fmtNum(x.documents))}</td><td class="r num">${esc(fmtNum(c.entities))}</td>
      <td class="r num">${esc(fmtNum(c.edges_committed))}</td><td class="r num">${esc(fmtNum(c.held))}</td></tr>`;
  }).join('')}</tbody></table></div>`;
}

// The selected run: engine tree with live progress, coverage and dead letters.
async function loadRunLive(alive, { quiet = false } = {}) {
  cancel('view:run-live');
  const box = $('#runSel');
  if (!box) return;
  const runId = state.runId;
  if (!runId) { box.innerHTML = `<div class="panel-body">${emptyState('No run selected', '<div>Start one above, or pick one from Recent runs.</div>')}</div>`; return; }
  const run = currentRun();
  const profile = run && profileByName(run.profile) ? run.profile : null;
  const [d, t] = await Promise.allSettled([API.run(runId, { quiet: true }), API.pipeline({ engine: run ? run.arm : null, run_id: runId, profile }, { quiet: true })]);
  if (!alive() || state.runId !== runId) return;
  if (d.status === 'rejected') {
    if (!quiet) box.innerHTML = `<div class="panel-body">${errorBox(d.reason, 'Could not load the run')}</div>`;
    if (runJobRunning()) scheduleRunLive(alive);
    return;
  }
  state.runDetail = d.value;
  state.runTree = t.status === 'fulfilled' ? t.value : null;
  renderRunSelected(alive, t.status === 'rejected' ? t.reason : null);
  scheduleRunLive(alive);
}
function scheduleRunLive(alive) {
  if (!runJobRunning() && !isRunLive(state.runId)) return;
  schedule('view:run-live', async () => {
    if (!alive()) return;
    await refreshRuns(true).catch(() => {});
    if (!alive()) return;
    renderRecentRuns();
    await loadRunLive(alive, { quiet: true });
  }, 3000);
}

function renderRunSelected(alive, treeError) {
  const box = $('#runSel');
  const d = state.runDetail;
  if (!box || !d) return;
  const live = isRunLive(d.run_id);
  const t = state.runTree;
  const engId = (t && (t.engine || t.arm)) || d.arm;
  const eng = engineById(engId) || (t ? engineFromNodes(engId, t.nodes) : null);
  if (!box.querySelector('#runTree')) {
    box.innerHTML = `<div class="panel-head" id="runSelHead"></div>
      <div class="panel-body run-sel-body">
        <div class="pipe-strip" id="runStrip"></div>
        <div class="pipe-canvas${state.compact ? ' compact' : ''}" id="runTree"></div>
        <div class="grid-2"><div id="runCov"></div><div id="runDead"></div></div>
      </div>`;
  }
  $('#runSelHead').innerHTML = `<span class="cap">Selected run</span>
      <span class="mono" title="${esc(d.run_id)}">${esc(shortId(d.run_id))}</span>${chip(engineName(d.arm), eng ? ENGINE_STATUS_COLORS[eng.status] || GREY : GREY)}<span class="mono muted">${esc(d.profile || '—')}</span>${statusChip(d.status)}
      ${live ? '<span class="live-dot" aria-hidden="true"></span><span class="ink2">live · refreshing every 3 s</span>' : `<span class="muted">${esc(fmtTime(d.started_at))} · ${esc(fmtDur(secondsBetween(d.started_at, d.finished_at)))} · ${esc(fmtNum(d.documents))} docs</span>`}
      <span class="spacer"></span>
      <button type="button" class="btn sm" data-action="compact" aria-pressed="${state.compact}">${state.compact ? 'Detailed' : 'Compact'}</button>
      <button type="button" class="btn sm" data-ctl data-action="run-archive" data-id="${esc(d.run_id)}" data-archived="${(currentRun() || {}).archived ? '' : '1'}" title="Hide this run from the lists (reversible; nothing is deleted)">${(currentRun() || {}).archived ? 'Unarchive' : 'Archive'}</button>
      <a href="#/gearbox">Gearbox</a><a href="#/graph">Graph</a><a href="#/review">Review</a><a href="#/dyno">Dyno</a>`;
  applyCtl($('#runSelHead'));
  $('#runStrip').innerHTML = `${d.status === 'INTERRUPTED' ? `<div class="note warn" role="status" style="margin:0 0 8px">${ICON.warn}<span><b>This run stopped without finishing</b> — its process was closed or crashed, so nothing is running it now. <b>Resume</b> continues it where it left off; finished units are not redone.</span><span class="note-actions"><button type="button" class="btn sm primary" data-ctl data-action="resume-run">Resume</button></span></div>` : ''}<div class="ps-run"><span class="muted">${esc(eng ? `${eng.name}: ${eng.tagline || ''}` : '')}</span></div>${treeLegendHtml('run')}`;
  applyCtl($('#runStrip'));
  // a run shows the models it recorded (GET /api/runs → models per endpoint), not whatever the build holds today
  const recorded = d.models || {};
  const nodes = (t ? t.nodes || [] : []).map((n) => {
    const sl = n.slot && eng ? engineSlot(eng, n.slot) : null;
    const m = sl && !isGlinerSlot(sl) ? recorded[sl.endpoint] : null;
    return m ? { ...n, model: m, configured: true, recorded: m !== n.model } : n;
  });
  if (t && eng) mountTree('runTree', { engine: eng, nodes, profileName: t.profile, runId: d.run_id, live, mode: 'run' }, alive);
  else $('#runTree').innerHTML = `<div class="panel-body">${treeError ? errorBox(treeError, 'Could not load the engine tree (GET /api/pipeline)') : emptyState('No engine tree', '')}${stageStripHtml(d.stages || [])}</div>`;
  const cur = state.insp.current;
  if (cur && cur.kind === 'stage' && cur.tree === 'runTree') renderStageInspector(cur);

  const cov = d.coverage || {};
  const keys = [...COVERAGE_ORDER.filter((k) => k in cov), ...Object.keys(cov).filter((k) => !COVERAGE_ORDER.includes(k)).sort()];
  const total = keys.reduce((a, k) => a + (Number(cov[k]) || 0), 0);
  $('#runCov').innerHTML = `<span class="cap">Coverage — terminal outcome per sentence</span><div style="margin-top:6px">${total ? `
    <div class="stackbar" role="img" aria-label="Coverage: ${esc(keys.map((k) => `${k} ${cov[k]}`).join(', '))}">
      ${keys.map((k) => `<span style="--c:${COVERAGE_COLORS[k] || GREY};flex:${Number(cov[k]) || 0} 0 0" title="${esc(k)}: ${esc(cov[k])} (${((cov[k] / total) * 100).toFixed(1)}%)"></span>`).join('')}
    </div>
    <div class="legend">${keys.map((k) => `<span><i class="swatch" style="--c:${COVERAGE_COLORS[k] || GREY}"></i>${esc(k)} <span class="num">${esc(fmtNum(cov[k]))} · ${((cov[k] / total) * 100).toFixed(1)}%</span></span>`).join('')}</div>
    <div class="muted" style="margin-top:6px;font-size:11px">${esc(fmtNum(total))} sentences reached a terminal outcome${cov.SILENT_DROP ? ` — <b style="color:var(--bad)">${esc(cov.SILENT_DROP)} silent drops fail the audit</b>` : ''}</div>`
    : emptyState('No coverage yet', '<div>Coverage is finalised when the run finishes.</div>')}</div>`;
  const dl = d.dead_letter || [];
  $('#runDead').innerHTML = `<span class="cap">Dead letters (${esc(dl.length)})</span><div style="margin-top:6px">${dl.length ? `<div class="table-wrap"><table class="data"><thead><tr><th>Stage</th><th>Unit</th><th>Error</th></tr></thead><tbody>
      ${dl.map((x) => `<tr><td>${plainChip(x.stage)}</td><td class="mono">${esc(shortId(x.unit_id))}</td><td class="wrap"><details><summary class="mono">${esc(String(x.error || '').split('\n')[0].slice(0, 140) || '—')}</summary><pre class="json">${esc(x.error)}</pre></details></td></tr>`).join('')}
    </tbody></table></div>` : '<div class="muted">No dead-lettered units.</div>'}</div>`;
}
// Fallback when the tree is unavailable: v1 per-stage progress.
function stageStripHtml(stages) {
  if (!stages.length) return '';
  return `<div class="stage-strip" role="list" style="margin-top:10px">${stages.map((s) => {
    const total = s.total || 0;
    const done = s.done || 0;
    const bad = (s.failed || 0) + (s.dead_letter || 0);
    return `<div class="stage ${bad ? 'has-fail' : done >= total && total > 0 ? 'complete' : 'partial'}" role="listitem"><span class="name">${esc(s.stage)}</span><span class="frac">${esc(fmtNum(done))}/${esc(fmtNum(total))}</span><span class="bar"><i style="width:${total ? ((done / total) * 100).toFixed(1) : 0}%"></i></span></div>`;
  }).join('')}</div>`;
}

async function loadCorpusQuiet() {
  try { state.corpus = (await API.corpus({ quiet: true })) || []; } catch { /* the DOCS card falls back to the overview count */ }
}

async function loadCorpus() {
  try { state.corpus = (await API.corpus({ quiet: true })) || []; state.corpusError = null; } catch (e) { state.corpusError = e; }
}
function renderCorpus() {
  const box = $('#corpusList');
  if (!box) return;
  if (state.corpusError && !state.corpus) { box.innerHTML = `<div class="panel-body">${errorBox(state.corpusError, 'Could not list the corpus')}</div>`; return; }
  const files = (state.corpus || []).slice().sort((a, b) => String(a.name).localeCompare(String(b.name)));
  const fresh = files.filter((f) => !f.ingested).length;
  $('#corpusCount').textContent = files.length ? `${files.length} file${files.length > 1 ? 's' : ''} · ${files.length - fresh} ingested` : '';
  const ib = $('#ingestBtn');
  if (ib) ib.textContent = fresh ? `Ingest ${fresh} new` : 'Ingest corpus';
  if (!files.length) { box.innerHTML = `<div class="panel-body">${emptyState('data/raw is empty', '<div>Drop documents above, then ingest them.</div>')}</div>`; applyCtl($('#workspace')); return; }
  box.innerHTML = `<div class="table-wrap"><table class="data corpus"><thead><tr><th>File</th><th>Status</th><th><span class="sr-only">Delete</span></th></tr></thead><tbody>
    ${files.map((f) => `<tr>
      <td class="wrap"><span class="fname" title="${esc(f.name)}">${esc(f.name)}</span><span class="fmeta num">${esc(fmtBytes(f.size))} · ${esc(fmtTime(f.modified))}</span></td>
      <td class="nowrap">${f.ingested ? `${chip('INGESTED', STATUS_COLORS.COMMITTED)}${f.doc_id ? ` <button type="button" class="linkish mono" data-action="open-doc" data-doc="${esc(f.doc_id)}" title="${esc(f.doc_id)} — open in Documents">${esc(shortId(f.doc_id))}</button>` : ''}` : chip('NEW', STATUS_COLORS.ESCALATED, '', 'not ingested yet')}</td>
      <td class="r"><button type="button" class="icon-btn" data-ctl data-action="corpus-delete" data-name="${esc(f.name)}" aria-label="Delete ${esc(f.name)} from data/raw" title="Delete from data/raw">${ICON.trash}</button></td>
    </tr>`).join('')}</tbody></table></div>`;
  applyCtl($('#workspace'));
}

function wireDropzone() {
  const dz = $('#dropzone');
  const input = $('#fileInput');
  input.addEventListener('change', () => { uploadFiles(input.files); input.value = ''; });
  ['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); if (!state.readOnly) dz.classList.add('over'); }));
  ['dragleave', 'dragend'].forEach((ev) => dz.addEventListener(ev, () => dz.classList.remove('over')));
  dz.addEventListener('drop', (e) => {
    e.preventDefault();
    dz.classList.remove('over');
    if (state.readOnly) { toast('Uploads are disabled', 'demo mode: the server is read-only', 'error'); return; }
    uploadFiles(e.dataTransfer.files);
  });
}

async function uploadFiles(list) {
  const files = [...(list || [])];
  if (!files.length) return;
  const okFiles = [];
  const rejected = [];
  for (const f of files) {
    const ext = f.name.includes('.') ? f.name.slice(f.name.lastIndexOf('.')).toLowerCase() : '';
    if (!UPLOAD_EXT.includes(ext)) rejected.push(`${f.name}: type ${ext || '(none)'} not allowed`);
    else if (f.size > UPLOAD_MAX) rejected.push(`${f.name}: larger than 100 MB`);
    else okFiles.push(f);
  }
  if (rejected.length) toast(`${rejected.length} file${rejected.length > 1 ? 's' : ''} skipped`, rejected.join('\n'), 'error');
  if (!okFiles.length) return;
  const status = $('#uploadStatus');
  if (status) status.innerHTML = `<div class="hint"><span class="spinner" aria-hidden="true" style="width:10px;height:10px"></span> uploading ${esc(okFiles.length)} file${okFiles.length > 1 ? 's' : ''} (${esc(fmtBytes(okFiles.reduce((a, f) => a + f.size, 0)))})…</div>`;
  const fd = new FormData();
  okFiles.forEach((f) => fd.append('files', f, f.name));
  try {
    const res = await API.upload(fd);
    const saved = (res && res.saved) || [];
    const skipped = (res && res.skipped) || [];
    toast(`Uploaded ${saved.length} file${saved.length === 1 ? '' : 's'}`, [saved.join(', '), ...skipped.map((s) => `skipped ${s.name}: ${s.reason}`)].filter(Boolean).join('\n'), skipped.length ? 'error' : 'ok');
  } catch { /* toast shown */ } finally {
    if ($('#uploadStatus')) $('#uploadStatus').innerHTML = '';
    await loadCorpus();
    renderCorpus();
  }
}

async function deleteCorpusFile(name) {
  const ok = await askDialog({ title: `Delete ${name}?`, html: `<p>The file is removed from <span class="mono">data/raw</span>. If it was ingested, its stored document, sentences and every run's results stay in the database (append-only) and remain viewable.</p>`, ok: 'Delete file', danger: true });
  if (!ok) return;
  try { await API.deleteCorpus(name); toast('File deleted', name, 'ok'); } catch { return; }
  await loadCorpus();
  renderCorpus();
}

async function ingestCorpus(btn) {
  await withBusy(btn, async () => {
    const job = await API.ingest('data/raw');
    jobStarted(job);
    trackJob(job.job_id, {
      onDone: async () => {
        state.documents = null;
        await loadCorpus();
        if (state.view === 'run') renderCorpus();
      },
    });
  }).catch(() => {});
}

async function withBusy(btn, fn) {
  if (btn) btn.disabled = true;
  try { return await fn(); } finally { if (btn) btn.disabled = false; if (state.view === 'run') renderRunsButtons(); }
}
function jobStarted(job) {
  if (!job || !job.job_id) return;
  toast(`Job started: ${job.kind}`, job.job_id, 'ok');
  if (job.kind === 'run' || job.kind === 'resume') state.control.follow.add(job.job_id);
  state.control.selectedJob = job.job_id;
  refreshJobs(true).catch(() => {}).then(() => { if (state.view === 'run') selectJob(job.job_id); });
  schedule('global:poll', pollGlobal, 1500);
}
async function startRun(force) {
  const ui = state.runsUi;
  const eng = engineById(ui.engine);
  const p = profileByName(ui.profile);
  const body = { engine: ui.engine || null, profile: ui.profile || null };
  if (ui.ontology) body.ontology = ui.ontology;
  if (hasMicroSlot(eng) && ui.rel) body.relation_mode = ui.rel;
  if (hasGlinerSlot(eng) && ui.gliner) body.gliner = ui.gliner;
  const pf = state.preflight[preflightKey()];
  const warn = $('#startWarn');
  const missing = engineMissing(eng, p);
  if (!force && (missing.length || !(pf && pf.status === 'passed'))) {
    const why = missing.length
      ? `<b>${esc(missing.length)} required slot${missing.length > 1 ? 's are' : ' is'} empty</b> in <span class="mono">${esc(ui.profile)}</span>: ${missing.map((sl) => esc(sl.label)).join(', ')}. The run would fail at its first model stage.`
      : `Preflight has ${pf ? (pf.status === 'running' ? 'not finished' : 'failed') : 'not been run'} for ${esc(engineName(ui.engine))} · <span class="mono">${esc(ui.profile)}</span> in this session. A run with an unreachable endpoint fails its first model stage.`;
    warn.innerHTML = `<div class="note warn" role="alert">${ICON.warn}<span>${why}</span>
      <span class="note-actions"><button type="button" class="btn sm primary" data-ctl data-action="start-anyway">Start anyway</button><button type="button" class="btn sm" data-ctl data-action="preflight">Run preflight</button></span></div>`;
    applyCtl(warn);
    return;
  }
  warn.innerHTML = '';
  await withBusy($('#startBtn'), async () => jobStarted(await API.startRun(body))).catch(() => {});
}
// Open the Run view set up for one engine + build, showing that engine's newest run (or none) — never another engine's.
function openRunFor(engineId, profile) {
  const ui = state.runsUi;
  if (engineId) ui.engine = engineId;
  if (profile) ui.profile = profile;
  ui.allEngines = false;
  const mine = state.runs.filter((x) => x.arm === ui.engine);
  const pick = mine.find((x) => x.profile === ui.profile) || mine[0];
  if (pick) selectRun(pick.run_id, { rerender: false });
  else if (state.runId && (currentRun() || {}).arm !== ui.engine) { state.runId = null; renderRunSelect(); }
  if (location.hash === '#/run') route(); else location.hash = '#/run';
}

async function resumeRun(btn) {
  if (!state.runId) return;
  await withBusy(btn, async () => jobStarted(await API.resume(state.runId))).catch(() => {});
}


function renderJobsTable() {
  const box = $('#jobsTable');
  if (!box) return;
  if (!state.jobs.length) { box.innerHTML = `<div class="panel-body">${emptyState('No jobs yet', state.readOnly ? '<div>Run control is disabled in demo mode.</div>' : '<div>Ingest the corpus, run a preflight or start a run above.</div>')}</div>`; return; }
  box.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr><th>Job</th><th>Kind</th><th>Status</th><th>Started</th><th class="r">Duration</th><th class="r">Exit</th><th>Run</th><th>Args</th></tr></thead><tbody>
    ${state.jobs.map((j) => `<tr class="clickable${j.job_id === state.control.selectedJob ? ' selected' : ''}" data-action="job" data-id="${esc(j.job_id)}">
      <td><button type="button" class="linkish mono" data-action="job" data-id="${esc(j.job_id)}" title="${esc(j.job_id)}">${esc(shortId(j.job_id))}</button></td>
      <td>${plainChip(j.kind)}</td>
      <td>${statusChip(j.status)}${j.status === 'running' ? ' <span class="spinner" aria-hidden="true" style="width:10px;height:10px"></span>' : ''}</td>
      <td class="num">${esc(fmtTime(j.started_at))}</td>
      <td class="r num">${esc(fmtDur(secondsBetween(j.started_at, j.finished_at)))}</td>
      <td class="r num">${esc(j.exit_code ?? '—')}</td>
      <td>${j.run_id ? `<button type="button" class="linkish mono" data-action="select-run" data-id="${esc(j.run_id)}" data-go="#/run" title="${esc(j.run_id)}">${esc(shortId(j.run_id))}</button>` : '<span class="muted">—</span>'}</td>
      <td class="mono muted wrap" style="min-width:140px">${esc((j.args || []).join(' '))}${j.profile ? ` <span class="ink2">· ${esc(j.profile)}</span>` : ''}</td>
    </tr>`).join('')}</tbody></table></div>`;
}

function selectJob(jobId) {
  state.control.selectedJob = jobId;
  renderJobsTable();
  cancel('view:job-tail');
  const box = $('#jobLog');
  if (!box) return;
  box.hidden = false;
  box.innerHTML = `<div class="panel-head"><span class="cap">Log</span><span class="mono">${esc(jobId)}</span></div><div class="panel-body">${skeleton(4)}</div>`;
  tailJob(jobId, true);
}

async function tailJob(jobId, first = false) {
  if (state.view !== 'run' || state.control.selectedJob !== jobId) return;
  let job;
  try {
    job = await API.job(jobId, { quiet: !first });
  } catch (e) {
    const box = $('#jobLog');
    if (box && first) box.innerHTML = `<div class="panel-body">${errorBox(e, 'Could not load the job')}</div>`;
    if (!first) schedule('view:job-tail', () => tailJob(jobId), 4000);
    return;
  }
  if (state.view !== 'run' || state.control.selectedJob !== jobId) return;
  renderJobLog(job, first);
  if (job.status === 'running') schedule('view:job-tail', () => tailJob(jobId), 2000);
}

function renderJobLog(job, first) {
  const box = $('#jobLog');
  if (!box) return;
  const prevPre = box.querySelector('pre.log');
  const atBottom = !prevPre || prevPre.scrollHeight - prevPre.scrollTop - prevPre.clientHeight < 40;
  const lines = job.log || [];
  box.innerHTML = `<div class="panel-head">
      <span class="cap">Log</span><span class="mono">${esc(job.job_id)}</span>${plainChip(job.kind)}${statusChip(job.status)}
      ${job.status === 'running' ? '<span class="spinner" aria-hidden="true"></span><span class="muted">polling every 2 s</span>' : `<span class="muted">exit ${esc(job.exit_code ?? '—')} · finished ${esc(fmtTime(job.finished_at))}</span>`}
      <span class="spacer"></span>
      ${job.run_id ? `<button type="button" class="btn sm" data-action="select-run" data-id="${esc(job.run_id)}" data-stay="1">Show run ${esc(shortId(job.run_id))}</button>` : ''}
    </div>
    <div class="panel-body">
      <div class="mono muted" style="margin-bottom:6px;overflow-wrap:anywhere">py -m synth ${esc((job.args || []).join(' '))}${job.profile ? ` · SYNTH_PROFILE=${esc(job.profile)}` : ''}</div>
      <pre class="log" tabindex="0" aria-label="Job log, last ${lines.length} lines">${lines.length ? lines.map((l) => esc(l)).join('\n') : '<span class="muted">(no output yet)</span>'}</pre>
      ${job.result !== null && job.result !== undefined ? `<details style="margin-top:8px"${job.status !== 'running' ? ' open' : ''}><summary class="cap" style="cursor:pointer">Result (stdout JSON)</summary>${jsonPre(job.result)}</details>` : ''}
    </div>`;
  const pre = box.querySelector('pre.log');
  if (pre && (atBottom || first)) pre.scrollTop = pre.scrollHeight;
  else if (pre && prevPre) pre.scrollTop = prevPre.scrollTop;
}

/* ================================================================ view: dyno (engine performance bench) */
// [key, header, better ('min' | 'max' | null), numeric]
const BENCH_COLS = [
  ['engine_name', 'Engine', null, false], ['profile', 'Build', null, false],
  ['pages', 'Docs / pages', null, true], ['wall_seconds', 'Wall', 'min', true], ['seconds_per_page', 's / page', 'min', true],
  ['facts_committed', 'Facts', 'max', true], ['facts_per_minute', 'Facts / min', 'max', true],
  ['tokens_local', 'Local tok', null, true], ['tokens_frontier', 'Frontier tok', 'min', true], ['ftvf', 'FTVF', 'min', true],
  ['cost_usd', 'Cost', 'min', true], ['fact_f1', 'Fact F1', 'max', true],
];
const benchVal = (row, key) => (key === 'fact_f1' ? (row.quality && row.quality.evaluated ? row.quality.fact_f1 : null) : row[key]);
const fmtUsd = (v) => (v === null || v === undefined ? '—' : Number(v) === 0 ? '$0' : `$${Number(v).toFixed(Number(v) < 1 ? 3 : 2)}`);
const fmtF1 = (v) => (v === null || v === undefined ? '—' : Number(v).toFixed(2));

async function viewDyno(_params, alive) {
  const ws = $('#workspace');
  ws.innerHTML = `<div class="view" id="dynoPage">
    <div class="view-head"><h1>Dyno</h1><span class="muted">performance bench · tick 2–4 runs to compare</span><span class="spacer"></span>
      <button type="button" class="btn sm primary" data-ctl data-action="bench-eval" id="evalBtn">Evaluate against gold</button>
      <button type="button" class="btn sm" data-action="bench-refresh">Refresh</button></div>
    ${demoBanner('evaluating runs is disabled')}
    <div id="evalBox"></div>
    <section class="panel" aria-labelledby="benchH"><div class="panel-head"><span class="cap" id="benchH">Bench</span><span class="muted" id="benchCount"></span><span class="spacer"></span><span id="benchSel"></span></div>
      <div class="panel-body flush" id="benchTable">${skeleton(5)}</div>
      <div class="panel-foot">Best value per column in <span class="ok-t">green</span> · FTVF = frontier tokens per verified fact (lower is better at equal quality) · quality needs promoted gold (Evaluate against gold).</div></section>
    <section class="panel" id="cmpPanel" aria-labelledby="cmpH"></section>
    <section class="panel" id="drill" aria-label="Run details"></section>
  </div>`;
  renderEvalBox();
  await Promise.allSettled([loadEngines(), refreshRuns(true)]);
  await loadBench(alive);
  if (!alive()) return;
  const obs = new ResizeObserver(debounce(() => { if (alive()) renderCompare(); }, 80));
  obs.observe($('#cmpPanel'));
  state.trees.__dyno = { observer: obs };   // disconnected by leaveTrees() on route change
  await renderDrill(alive);
}

async function loadBench(alive, { quiet = false } = {}) {
  try {
    state.bench = (await API.bench(30, { quiet: true })) || [];
    state.benchError = null;
  } catch (e) { state.benchError = e; }
  if (!alive()) return;
  state.dyno.sel = state.dyno.sel.filter((id) => (state.bench || []).some((r) => r.run_id === id));
  if (!quiet || state.bench) { renderBenchTable(); renderCompare(); }
}

function renderBenchTable() {
  const box = $('#benchTable');
  if (!box) return;
  if (state.benchError && !state.bench) { box.innerHTML = `<div class="panel-body">${errorBox(state.benchError, 'Could not load the bench (GET /api/bench)')}</div>`; return; }
  const rows = (state.bench || []).filter((r) => state.showArchived || !r.archived);
  $('#benchCount').textContent = rows.length ? `${rows.length} run${rows.length === 1 ? '' : 's'} · newest first` : '';
  const n = state.dyno.sel.length;
  $('#benchSel').innerHTML = n ? `<span class="ink2">${esc(n)} selected</span> <button type="button" class="linkish" data-action="bench-clear">clear</button>` : '<span class="muted">select runs to compare</span>';
  updateEvalBtn();
  if (!rows.length) { box.innerHTML = `<div class="panel-body">${emptyState('No runs on the bench yet', '<div>Start a run in <a href="#/run">Run</a>; every finished run is measured here.</div>')}</div>`; return; }
  const { key, dir } = state.dyno.sort;
  rows.sort((a, b) => {
    const x = benchVal(a, key); const y = benchVal(b, key);
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    if (typeof x === 'number' || typeof y === 'number') return dir * ((Number(x) || 0) - (Number(y) || 0));
    return dir * String(x).localeCompare(String(y));
  });
  const best = {};
  for (const [k, , better] of BENCH_COLS) {
    if (!better) continue;
    const vals = rows.map((r) => benchVal(r, k)).filter((v) => v !== null && v !== undefined && !Number.isNaN(Number(v))).map(Number);
    if (vals.length > 1) best[k] = better === 'min' ? Math.min(...vals) : Math.max(...vals);
  }
  const cell = (r, k, text) => {
    const v = benchVal(r, k);
    const isBest = best[k] !== undefined && v !== null && v !== undefined && Number(v) === best[k];
    return `<td class="r num${isBest ? ' best' : ''}"${isBest ? ' title="best on the bench"' : ''}>${esc(text)}</td>`;
  };
  box.innerHTML = `<div class="table-wrap"><table class="data bench"><thead><tr><th><span class="sr-only">Compare</span></th><th>Run</th>
      ${BENCH_COLS.map(([k, l, , num]) => `<th class="${num ? 'r' : ''}" aria-sort="${key === k ? (dir > 0 ? 'ascending' : 'descending') : 'none'}"><button type="button" data-action="bench-sort" data-key="${k}">${esc(l)}${key === k ? (dir > 0 ? ' ▲' : ' ▼') : ''}</button></th>`).join('')}
      <th><span class="sr-only">Details</span></th></tr></thead><tbody>
    ${rows.map((r) => {
    const si = state.dyno.sel.indexOf(r.run_id);
    const eng = engineById(r.engine);
    const q = r.quality || {};
    return `<tr class="${si >= 0 ? 'cmp-row' : ''}${r.run_id === state.runId ? ' selected' : ''}" style="${si >= 0 ? `--c:${RUN_COLORS[si]}` : ''}">
      <td><input type="checkbox" data-bench-pick="${esc(r.run_id)}" aria-label="Compare ${esc(shortId(r.run_id))}"${si >= 0 ? ' checked' : ''}${si < 0 && state.dyno.sel.length >= 4 ? ' disabled title="compare up to 4 runs"' : ''}></td>
      <td class="nowrap">${si >= 0 ? `<span class="run-letter" style="--c:${RUN_COLORS[si]}">${RUN_LETTERS[si]}</span>` : ''}<span class="mono" title="${esc(r.run_id)} · started ${esc(fmtTime(r.started_at))}">${esc(shortId(r.run_id))}</span> ${r.status && r.status !== 'DONE' ? statusChip(r.status) : ''}</td>
      <td class="nowrap">${esc(r.engine_name || engineName(r.engine))} ${eng ? `<span class="eng-dot" style="--c:${ENGINE_STATUS_COLORS[eng.status] || GREY}" title="${esc(eng.status || '')}"></span>` : ''}</td>
      <td class="mono ellip" title="${esc(r.profile || '')}">${esc(r.profile || '—')}</td>
      <td class="r num">${esc(fmtNum(r.documents))} / ${esc(fmtNum(r.pages))}</td>
      ${cell(r, 'wall_seconds', fmtDur(r.wall_seconds))}${cell(r, 'seconds_per_page', r.seconds_per_page === null || r.seconds_per_page === undefined ? '—' : fmtNum(r.seconds_per_page, 1))}
      ${cell(r, 'facts_committed', fmtNum(r.facts_committed))}${cell(r, 'facts_per_minute', r.facts_per_minute === null || r.facts_per_minute === undefined ? '—' : fmtNum(r.facts_per_minute, 1))}
      ${cell(r, 'tokens_local', fmtTokens(r.tokens_local))}${cell(r, 'tokens_frontier', fmtTokens(r.tokens_frontier))}
      ${cell(r, 'ftvf', r.ftvf === null || r.ftvf === undefined ? '—' : fmtNum(r.ftvf, 1))}${cell(r, 'cost_usd', fmtUsd(r.cost_usd))}
      ${q.evaluated ? `${cell(r, 'fact_f1', fmtF1(q.fact_f1))}` : '<td class="r muted" title="not evaluated against gold">—</td>'}
      <td class="r"><button type="button" class="btn sm" data-action="bench-drill" data-id="${esc(r.run_id)}" title="Per-stage metrics and audit for this run (selects it)">Details</button></td>
    </tr>`;
  }).join('')}</tbody></table></div>`;
  box.querySelectorAll('[data-bench-pick]').forEach((cb) => cb.addEventListener('change', () => {
    const id = cb.dataset.benchPick;
    const sel = state.dyno.sel;
    if (cb.checked && !sel.includes(id) && sel.length < 4) sel.push(id);
    if (!cb.checked) state.dyno.sel = sel.filter((x) => x !== id);
    renderBenchTable();
    renderCompare();
  }));
}

function updateEvalBtn() {
  const b = $('#evalBtn');
  if (!b) return;
  const n = state.dyno.sel.length;
  const running = state.dyno.evalJob && state.dyno.evalJob.status === 'running';
  b.textContent = running ? 'Evaluating…' : n ? `Evaluate ${n} against gold` : 'Evaluate against gold';
  b.disabled = running || !n;
  b.title = n ? 'Score the selected runs against promoted gold' : 'Tick the runs to evaluate first';
  applyCtl(b.parentElement);
}

async function benchEval(btn) {
  const ids = state.dyno.sel.slice();
  if (!ids.length) return;
  btn.disabled = true;
  let job;
  try { job = await API.evalRuns(ids); } catch { btn.disabled = false; return; }
  state.dyno.evalJob = { jobId: job.job_id, status: 'running', ids, job: null };
  jobStarted(job);
  renderEvalBox();
  updateEvalBtn();
  trackJob(job.job_id, {
    onUpdate: (j) => { state.dyno.evalJob.job = j; renderEvalBox(); },
    onDone: async (j) => {
      state.dyno.evalJob = { ...state.dyno.evalJob, status: j.status, job: j };
      if (state.view !== 'dyno') return;
      renderEvalBox();
      const seq = state.seq;
      await loadBench(() => seq === state.seq, { quiet: true });
    },
  });
}
function renderEvalBox() {
  const box = $('#evalBox');
  if (!box) return;
  const e = state.dyno.evalJob;
  if (!e) { box.innerHTML = ''; return; }
  const last = ((e.job && e.job.log) || []).slice(-1)[0];
  box.innerHTML = `<div class="note ${e.status === 'running' ? '' : e.status === 'done' ? 'ok' : 'bad'}" role="status">
    ${e.status === 'running' ? '<span class="spinner" aria-hidden="true" style="width:11px;height:11px"></span>' : ''}
    <span><b>${e.status === 'running' ? 'Evaluating' : e.status === 'done' ? 'Evaluated' : 'Evaluation failed for'} ${esc(e.ids.length)} run${e.ids.length === 1 ? '' : 's'}</b> against promoted gold${last ? ` · <span class="mono">${esc(last)}</span>` : ''}</span>
    <span class="note-actions"><button type="button" class="linkish mono" data-action="goto-job" data-id="${esc(e.jobId)}">${esc(shortId(e.jobId))}</button>${e.status !== 'running' ? '<button type="button" class="linkish" data-action="eval-dismiss">dismiss</button>' : ''}</span></div>`;
}

// One horizontal bar chart (one bar per run, coloured by run, direct-labelled with the run letter and value).
function hbarSvg(width, rows, { fmt = (v) => fmtNum(v), aria = '' } = {}) {
  const vals = rows.map((r) => (r.value === null || r.value === undefined ? null : Number(r.value)));
  const max = Math.max(0, ...vals.filter((v) => v !== null));
  const L = 128; const R = 74; const rowH = 22; const barH = 12;
  const W = Math.max(240, Math.floor(width));
  const H = rows.length * rowH + 4;
  const sx = (v) => (max > 0 ? ((W - L - R) * v) / max : 0);
  const bars = rows.map((r, i) => {
    const y = 3 + i * rowH;
    const v = vals[i];
    const w = v === null ? 0 : Math.max(v > 0 ? 3 : 0, sx(v));
    const label = String(r.label);
    return `<g>
      <rect x="2" y="${y}" width="14" height="${barH}" rx="2" fill="${r.color}"/><text x="9" y="${y + barH - 2}" text-anchor="middle" class="lt">${esc(r.letter || '')}</text>
      <text x="22" y="${y + barH - 2}" class="ax">${esc(label.length > 15 ? `${label.slice(0, 14)}…` : label)}<title>${esc(label)}</title></text>
      ${v === null ? `<text x="${L + 4}" y="${y + barH - 2}" class="na">no data</text>` : `<path d="M${L},${y} h${Math.max(0, w - 4).toFixed(1)} a4,4 0 0 1 4,4 v${barH - 8} a4,4 0 0 1 -4,4 h-${Math.max(0, w - 4).toFixed(1)} z" fill="${r.color}"><title>${esc(label)}: ${esc(fmt(v))}</title></path>
      <text x="${(L + w + 6).toFixed(1)}" y="${y + barH - 2}" class="val">${esc(fmt(v))}</text>`}
    </g>`;
  }).join('');
  return `<svg class="bar-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(aria)}">
    <style>.ax{fill:#a9b6c2;font:10.5px ui-monospace,Consolas,monospace}.val{fill:#dbe3ea;font:10.5px ui-monospace,Consolas,monospace}.na{fill:#7d8b98;font:italic 10.5px system-ui,sans-serif}.lt{fill:#0f1419;font:700 9.5px ui-monospace,Consolas,monospace}</style>
    <line x1="${L}" x2="${L}" y1="0" y2="${H}" stroke="#2a3642"/>${bars}</svg>`;
}

function renderCompare() {
  const box = $('#cmpPanel');
  if (!box) return;
  const runs = state.dyno.sel.map((id) => (state.bench || []).find((r) => r.run_id === id)).filter(Boolean);
  if (runs.length < 2) {
    box.innerHTML = `<div class="panel-head"><span class="cap" id="cmpH">Compare</span></div><div class="panel-body"><div class="muted">${runs.length ? 'Tick one more run to compare.' : 'Tick 2–4 runs in the bench to put them on the dyno side by side: speed, fuel, slot throughput and quality.'}</div></div>`;
    return;
  }
  const series = runs.map((r, i) => ({ r, color: RUN_COLORS[i], letter: RUN_LETTERS[i], label: r.engine_name || engineName(r.engine) }));
  const charts = [
    ['speed', 'Speed · facts per minute', '↑ higher is better', (r) => r.facts_per_minute, (v) => fmtNum(v, 1)],
    ['speed', 'Speed · seconds per page', '↓ lower is better', (r) => r.seconds_per_page, (v) => `${fmtNum(v, 1)} s`],
    ['fuel', 'Fuel · local tokens', 'on this machine', (r) => r.tokens_local, fmtTokens],
    ['fuel', 'Fuel · frontier tokens', '↓ lower is better', (r) => r.tokens_frontier, fmtTokens],
    ['fuel', 'Fuel · FTVF', 'frontier tokens per verified fact · ↓ lower is better', (r) => r.ftvf, (v) => fmtNum(v, 1)],
    ['fuel', 'Fuel · cost', '↓ lower is better', (r) => r.cost_usd, fmtUsd],
    ['quality', 'Output · facts committed', '↑ more verified facts', (r) => r.facts_committed, (v) => fmtNum(v)],
    ['quality', 'Quality · fact F1', '↑ higher is better · vs promoted gold', (r) => (r.quality && r.quality.evaluated ? r.quality.fact_f1 : null), fmtF1],
    ['quality', 'Quality · entity F1', '↑ higher is better · vs promoted gold', (r) => (r.quality && r.quality.evaluated ? r.quality.entity_f1 : null), fmtF1],
  ];
  const anyQ = runs.some((r) => r.quality && r.quality.evaluated);
  box.innerHTML = `<div class="panel-head"><span class="cap" id="cmpH">Compare</span><span class="muted">${esc(runs.length)} runs on the dyno</span><span class="spacer"></span><button type="button" class="linkish" data-action="bench-clear">clear</button></div>
    <div class="panel-body cmp-body">
      <div class="cmp-legend">${series.map((s) => `<span><span class="run-letter" style="--c:${s.color}">${s.letter}</span><b>${esc(s.label)}</b><span class="mono muted">${esc(s.r.profile || '')} · ${esc(shortId(s.r.run_id))}</span></span>`).join('')}</div>
      <div class="cmp-grid">${charts.map(([group, title, sub], i) => (group === 'quality' && i > 6 && !anyQ ? '' : `<figure class="cmp-card"><figcaption><span class="cmp-group">${esc(group)}</span><b>${esc(title.split(' · ')[1])}</b><span class="muted">${esc(sub)}</span></figcaption><div class="cmp-chart" data-chart="${i}"></div></figure>`)).join('')}
        ${anyQ ? '' : '<figure class="cmp-card"><figcaption><span class="cmp-group">quality</span><b>Fact / entity F1</b></figcaption><div class="muted" style="padding:6px 0">None of these runs is evaluated. <button type="button" class="linkish" data-ctl data-action="bench-eval-cmp">Evaluate them against gold</button>.</div></figure>'}
      </div>
      <figure class="cmp-card wide"><figcaption><span class="cmp-group">slots</span><b>Throughput per slot</b><span class="muted">output tokens per second of model time · ↑ higher is better</span></figcaption><div id="cmpSlots"></div></figure>
    </div>`;
  box.querySelectorAll('[data-chart]').forEach((el) => {
    const [, title, , get, fmt] = charts[Number(el.dataset.chart)];
    el.innerHTML = hbarSvg(el.clientWidth, series.map((s) => ({ label: s.label, letter: s.letter, color: s.color, value: get(s.r) })), { fmt, aria: `${title}: ${series.map((s) => `${s.letter} ${s.label} ${get(s.r) ?? 'no data'}`).join(', ')}` });
  });
  // per-slot throughput: one group per endpoint, a bar per run that drives it
  const eps = [];
  runs.forEach((r) => (r.slots || []).forEach((s) => { if (!eps.includes(s.endpoint)) eps.push(s.endpoint); }));
  const sbox = $('#cmpSlots');
  if (!eps.length) { sbox.innerHTML = '<div class="muted">No model calls recorded for these runs.</div>'; } else {
    sbox.innerHTML = `<div class="slot-groups">${eps.map((ep) => {
      const labels = [...new Set(runs.map((r) => endpointLabel(engineById(r.engine), ep)).filter((x) => x !== ep))];
      return `<div class="slot-group"><div class="sg-head"><span class="mono">${esc(ep)}</span><span class="muted">${esc(labels.join(' / '))}</span></div><div data-ep="${esc(ep)}"></div></div>`;
    }).join('')}</div>`;
    sbox.querySelectorAll('[data-ep]').forEach((el) => {
      const ep = el.dataset.ep;
      const rows = series.map((s) => { const sl = (s.r.slots || []).find((x) => x.endpoint === ep); return sl ? { label: sl.model || s.label, letter: s.letter, color: s.color, value: sl.tokens_per_s } : null; }).filter(Boolean);
      el.innerHTML = hbarSvg(el.clientWidth, rows, { fmt: (v) => `${fmtNum(v, 1)} t/s`, aria: `${ep} tokens per second: ${rows.map((x) => `${x.letter} ${x.label} ${x.value ?? 'no data'}`).join(', ')}` });
    });
  }
  applyCtl(box);
}

/* dyno drill-down: the v1 Metrics content for the run selected in the top bar */
async function renderDrill(alive) {
  const box = $('#drill');
  if (!box) return;
  if (!state.runId) { box.innerHTML = `<div class="panel-body">${emptyState('No run selected', '<div>Press Details on a bench row.</div>')}</div>`; return; }
  const run = currentRun();
  box.innerHTML = `<div class="panel-head"><span class="cap">Run details</span><span class="mono" title="${esc(state.runId)}">${esc(shortId(state.runId))}</span>${run ? `${plainChip(engineName(run.arm))}<span class="mono muted">${esc(run.profile || '')}</span>${statusChip(run.status)}` : ''}<span class="muted">model calls through the gateway (L2)</span></div>
    <div class="panel-body drill-body">
      <div class="tiles" id="mTiles">${skeleton(2)}</div>
      <div class="grid-2">
        <section class="panel inner"><div class="panel-head"><span class="cap">Tokens by stage</span><span class="spacer"></span>
          <span class="legend" style="margin:0"><span><i class="swatch" style="--c:${SERIES.in}"></i>in</span><span><i class="swatch" style="--c:${SERIES.out}"></i>out</span></span></div>
          <div class="panel-body" id="mChart">${skeleton(4)}</div></section>
        <section class="panel inner" aria-labelledby="auditH"><div class="panel-head"><span class="cap" id="auditH">Audit</span><span class="spacer"></span>
          <button type="button" class="btn sm primary" data-action="run-audit" id="auditBtn">Run audit</button></div>
          <div class="panel-body" id="mAudit"></div></section>
      </div>
      <section class="panel inner"><div class="panel-head"><span class="cap">By stage · endpoint · model</span></div><div class="panel-body flush" id="mTable">${skeleton(5)}</div></section>
    </div>`;
  renderAudit();
  let m;
  try {
    m = await API.metrics(state.runId, { quiet: true });
  } catch (e) {
    if (alive()) $('#mTable').innerHTML = `<div class="panel-body">${errorBox(e, 'Could not load metrics')}</div>`;
    return;
  }
  if (!alive()) return;
  state.metrics = m;
  const t = m.totals || {};
  const rows = m.by_stage || [];
  const failures = rows.reduce((a, x) => a + (x.failures || 0), 0);
  $('#mTiles').innerHTML = [
    ['FTVF', m.ftvf === null || m.ftvf === undefined ? '—' : fmtNum(m.ftvf, 2), 'frontier tokens / verified fact · lower is better'],
    ['Frontier tokens', fmtTokens(t.frontier_tokens), 'frontier endpoint, in + out'],
    ['Local tokens', fmtTokens(t.local_tokens), 'local endpoints, in + out'],
    ['Facts committed', fmtNum(m.facts_committed), 'macro edges COMMITTED'],
    ['Wall time', fmtDur(m.wall_seconds), 'started → finished'],
    ['Model calls', fmtNum(t.calls), `${fmtNum(failures)} failed · ${fmtDur((t.latency_ms || 0) / 1000)} model time`],
  ].map(([l, v, s]) => `<div class="tile"><div class="cap">${esc(l)}</div><div class="v">${esc(v)}</div><div class="s">${esc(s)}</div></div>`).join('');
  renderTokenChart(rows);
  renderMetricsTable();
}

/* ================================================================ view: data (stores · outputs) */
const GROUP_ORDER = ['S1 Documents', 'S2 Entities', 'S3 Time', 'S4 Micro graph', 'S5 Macro graph', 'L1 Runs', 'L2 Model calls',
  'L3 Coverage', 'L4 Change log', 'L5 Resolutions', 'L6 Flags', 'L7 Escalations', 'L8 Eval', 'J0 Units'];
const OUTPUT_COLORS = { export: '#3987e5', viewer: '#9085e9', report: '#199e70', log: '#7d8b98', other: '#5f6e7c' };
const INSPECTABLE = /^(ent|mad|med|tim|flg|esc)_[0-9A-Za-z]{8,}$/;
const ID_LIKE = /^[a-z]{3}_[0-9A-Za-z]{10,}$/;

async function viewData(params, alive) {
  const tab = params[0] === 'outputs' ? 'outputs' : 'stores';
  state.data.tab = tab;
  if (tab === 'stores' && params[1] && params[1] !== state.data.table) { state.data.table = params[1]; resetTableFilters(); }
  const ws = $('#workspace');
  ws.innerHTML = `<div class="view">
    <div class="view-head"><h1>Data</h1><span class="muted">memory stores (S1–S5), ledgers (L1–L8) and the files runs wrote</span><span class="spacer"></span>
      <button type="button" class="btn sm" data-action="data-refresh">Refresh</button></div>
    <div class="view-tabs" role="tablist" aria-label="Data">
      <a role="tab" href="#/data/stores${state.data.table ? `/${enc(state.data.table)}` : ''}" aria-selected="${tab === 'stores'}">Stores</a>
      <a role="tab" href="#/data/outputs" aria-selected="${tab === 'outputs'}">Outputs</a>
    </div>
    <div id="dataBody" role="tabpanel"></div>
  </div>`;
  if (tab === 'stores') await renderStoresTab(alive);
  else await renderOutputsTab(alive);
}

function resetTableFilters() { state.data.offset = 0; state.data.column = ''; state.data.value = ''; }

async function renderStoresTab(alive, { force = false } = {}) {
  const body = $('#dataBody');
  body.innerHTML = `<div class="tiles" id="dbTiles">${skeleton(1)}</div>
    <div class="data-split">
      <nav class="panel table-list" id="tblList" aria-label="Tables">${skeleton(10)}</nav>
      <section class="panel tbl-panel" id="tblPanel" aria-live="polite"><div class="panel-body">${skeleton(8)}</div></section>
    </div>`;
  if (force || !state.data.tables) {
    const [db, tables] = await Promise.allSettled([API.db({ quiet: true }), API.tables({ quiet: true })]);
    state.data.db = db.status === 'fulfilled' ? db.value : { error: db.reason };
    state.data.tables = tables.status === 'fulfilled' ? tables.value || [] : null;
    state.data.tablesError = tables.status === 'rejected' ? tables.reason : null;
  }
  if (!alive()) return;
  const db = state.data.db || {};
  $('#dbTiles').innerHTML = db.error ? errorBox(db.error, 'Could not read the database header') : [
    ['Dialect', db.dialect || '—', 'SQLAlchemy Core'],
    ['Database', db.url || '—', 'one database for every store'],
    ['Revision', db.revision || '—', 'Alembic head'],
    ['Size', fmtBytes(db.size_bytes), db.size_bytes === null || db.size_bytes === undefined ? 'not reported' : 'on disk'],
  ].map(([l, v, s]) => `<div class="tile"><div class="cap">${esc(l)}</div><div class="v${String(v).length > 14 ? ' small' : ''}" title="${esc(v)}">${esc(v)}</div><div class="s">${esc(s)}</div></div>`).join('');
  if (!state.data.tables) { $('#tblList').innerHTML = `<div class="panel-body">${errorBox(state.data.tablesError, 'Could not list tables')}</div>`; $('#tblPanel').innerHTML = ''; return; }
  const ts = state.data.tables;
  if (!state.data.table || !ts.some((t) => t.name === state.data.table)) {
    state.data.table = (ts.find((t) => t.name === 'entities') || ts.find((t) => t.rows) || ts[0] || {}).name || null;
    resetTableFilters();
  }
  renderTableList();
  await loadTable(alive);
}

function renderTableList() {
  const box = $('#tblList');
  if (!box) return;
  const ts = state.data.tables || [];
  const groups = new Map();
  ts.forEach((t) => { const g = t.group || 'Other'; if (!groups.has(g)) groups.set(g, []); groups.get(g).push(t); });
  const names = [...GROUP_ORDER.filter((g) => groups.has(g)), ...[...groups.keys()].filter((g) => !GROUP_ORDER.includes(g)).sort()];
  box.innerHTML = `${names.map((g) => `<div class="tl-group"><div class="tl-gh"><span class="cap">${esc(g)}</span></div>
      ${groups.get(g).map((t) => `<button type="button" class="tbl-item" data-action="data-table" data-name="${esc(t.name)}" aria-current="${t.name === state.data.table}">
        <span class="tn mono" title="${esc(t.name)}">${esc(t.name)}</span>${t.append_only ? `<span class="ao" title="append-only: rows are never updated or deleted">${ICON.lock}</span>` : ''}<span class="num tr">${esc(fmtNum(t.rows))}</span></button>`).join('')}</div>`).join('')}
    <div class="tl-foot"><span class="ao">${ICON.lock}</span> append-only · counts are whole-table</div>`;
}

async function loadTable(alive) {
  const dt = state.data;
  const name = dt.table;
  const panel = $('#tblPanel');
  if (!panel || !name) { if (panel) panel.innerHTML = `<div class="panel-body">${emptyState('No tables', '')}</div>`; return; }
  try {
    if (!dt.cols[name] && dt.runOnly && state.runId) {   // learn the columns first, so run_id is only sent where it exists
      const probe = await API.table(name, { limit: 1, offset: 0 }, { quiet: true });
      dt.cols[name] = probe.columns || [];
    }
    const useRun = dt.runOnly && state.runId && (dt.cols[name] || []).includes('run_id');
    const res = await API.table(name, { limit: dt.limit, offset: dt.offset, order: dt.order, run_id: useRun ? state.runId : null, column: dt.column || null, value: dt.column ? dt.value : null }, { quiet: true });
    if (!alive() || dt.table !== name) return;
    dt.cols[name] = res.columns || dt.cols[name] || [];
    dt.result = res;
    dt.usedRun = useRun;
    renderTablePanel();
  } catch (e) {
    if (alive() && dt.table === name) panel.innerHTML = `<div class="panel-head"><span class="mono tbl-title">${esc(name)}</span></div><div class="panel-body">${errorBox(e, 'Could not read the table')}<div style="margin-top:8px"><button type="button" class="btn sm" data-action="data-filter-clear">Clear filters</button></div></div>`;
  }
}

function cellHtml(v, row) {
  if (v === null || v === undefined) return '<span class="null">null</span>';
  if (typeof v === 'boolean') return `<span class="bool ${v}">${v}</span>`;
  if (typeof v === 'number') return `<span class="num">${esc(v)}</span>`;
  if (typeof v === 'object') {
    if (Array.isArray(v) && v.length && v.length <= 6 && v.every((x) => typeof x === 'string' && ID_LIKE.test(x))) return `<span class="id-list">${v.map((x) => idHtml(x, row)).join('')}</span>`;
    const s = JSON.stringify(v);
    const label = Array.isArray(v) ? `[${v.length}]` : `{${Object.keys(v).length}}`;
    if (s.length <= 40) return `<span class="mono jsmall">${esc(s)}</span>`;
    return `<details class="jcell"><summary><span class="jtag">${esc(label)}</span><span class="mono">${esc(s.slice(0, 44))}…</span></summary>${jsonPre(v)}</details>`;
  }
  const s = String(v);
  if (ID_LIKE.test(s)) return idHtml(s, row);
  if (s.length > 90) return `<details class="jcell"><summary><span class="txt">${esc(s.slice(0, 80))}…</span></summary><div class="long">${esc(s)}</div></details>`;
  return `<span class="txt">${esc(s)}</span>`;
}
function idHtml(s, row) {
  const runAttr = row && typeof row.run_id === 'string' ? ` data-run="${esc(row.run_id)}"` : '';
  if (INSPECTABLE.test(s)) return `<button type="button" class="linkish mono idc" data-action="data-inspect" data-id="${esc(s)}"${runAttr} title="${esc(s)} — inspect">${esc(shortId(s))}</button>`;
  if (s.startsWith('run_')) return `<button type="button" class="linkish mono idc" data-action="select-run" data-id="${esc(s)}" title="${esc(s)} — select this run">${esc(shortId(s))}</button>`;
  if (s.startsWith('doc_')) return `<button type="button" class="linkish mono idc" data-action="open-doc" data-doc="${esc(s)}" title="${esc(s)} — open in Documents">${esc(shortId(s))}</button>`;
  return `<span class="mono idc" title="${esc(s)}">${esc(shortId(s))}</span>`;
}

function renderTablePanel() {
  const dt = state.data;
  const res = dt.result;
  const panel = $('#tblPanel');
  if (!panel || !res) return;
  const info = (dt.tables || []).find((t) => t.name === dt.table) || {};
  const cols = res.columns || [];
  const rows = res.rows || [];
  const hasRun = cols.includes('run_id');
  const total = Number(res.total) || 0;
  const from = total ? (Number(res.offset) || 0) + 1 : 0;
  const to = (Number(res.offset) || 0) + rows.length;
  const pages = Math.max(1, Math.ceil(total / dt.limit));
  const page = Math.floor((Number(res.offset) || 0) / dt.limit) + 1;
  const filters = [dt.usedRun ? `run_id = ${shortId(state.runId)}` : '', dt.column ? `${dt.column} = ${dt.value}` : ''].filter(Boolean);
  panel.innerHTML = `
    <div class="panel-head"><span class="mono tbl-title">${esc(dt.table)}</span>${plainChip(info.group || 'table')}${info.append_only ? chip('append-only', '#c98500', '', 'rows are never updated or deleted') : ''}
      <span class="muted">${esc(fmtNum(total))} row${total === 1 ? '' : 's'}${filters.length ? ` matching ${esc(filters.join(' · '))}` : ''}</span><span class="spacer"></span>
      ${hasRun ? `<label class="check" title="${state.runId ? `only rows of ${esc(state.runId)} (the run selected in the top bar)` : 'select a run in the top bar'}"><input type="checkbox" id="dRun"${dt.runOnly && state.runId ? ' checked' : ''}${state.runId ? '' : ' disabled'}><span class="lbl">selected run only</span></label>` : '<span class="muted" title="this table has no run_id column">all runs</span>'}
      <button type="button" class="btn sm" data-action="data-order" title="Row order (descending / ascending by the table's key)">${dt.order === 'desc' ? '↓ desc' : '↑ asc'}</button>
    </div>
    <form class="panel-head data-filter" id="dFilter">
      <label for="dCol" class="cap">Where</label>
      <select id="dCol"><option value="">column…</option>${cols.map((c) => `<option value="${esc(c)}"${c === dt.column ? ' selected' : ''}>${esc(c)}</option>`).join('')}</select>
      <span class="muted">=</span>
      <input id="dVal" type="text" class="mono" value="${esc(dt.value)}" placeholder="exact value" spellcheck="false">
      <button type="submit" class="btn sm">Apply</button>
      ${dt.column ? '<button type="button" class="btn sm" data-action="data-filter-clear">Clear</button>' : ''}
      <span class="spacer"></span>
      <label for="dLimit" class="cap">Rows</label>
      <select id="dLimit">${[25, 50, 100, 200].map((n) => `<option${n === dt.limit ? ' selected' : ''}>${n}</option>`).join('')}</select>
    </form>
    <div class="table-wrap grid-wrap">${rows.length ? `<table class="data grid"><thead><tr><th class="r rn">#</th>${cols.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>
      ${rows.map((row, i) => `<tr><td class="r rn num">${esc(from + i)}</td>${cols.map((c) => `<td>${cellHtml(row[c], row)}</td>`).join('')}</tr>`).join('')}
    </tbody></table>` : `<div class="panel-body">${emptyState(total ? 'No rows on this page' : 'No rows', filters.length ? '<div>Nothing matches the filters. <button type="button" class="linkish" data-action="data-filter-clear">Clear filters</button></div>' : '<div>The table is empty.</div>')}</div>`}</div>
    <div class="panel-foot pager">
      <span class="num">${esc(fmtNum(from))}–${esc(fmtNum(to))} of ${esc(fmtNum(total))}</span><span class="spacer"></span>
      <button type="button" class="btn sm" data-action="data-page" data-dir="first"${page <= 1 ? ' disabled' : ''} aria-label="First page">«</button>
      <button type="button" class="btn sm" data-action="data-page" data-dir="prev"${page <= 1 ? ' disabled' : ''}>Prev</button>
      <span class="num muted">page ${esc(page)} / ${esc(pages)}</span>
      <button type="button" class="btn sm" data-action="data-page" data-dir="next"${page >= pages ? ' disabled' : ''}>Next</button>
      <button type="button" class="btn sm" data-action="data-page" data-dir="last"${page >= pages ? ' disabled' : ''} aria-label="Last page">»</button>
    </div>`;
  const seq = state.seq;
  const alive = () => seq === state.seq;
  const runBox = $('#dRun');
  if (runBox) runBox.addEventListener('change', () => { dt.runOnly = runBox.checked; dt.offset = 0; loadTable(alive); });
  $('#dLimit').addEventListener('change', (e) => { dt.limit = Number(e.target.value); dt.offset = 0; loadTable(alive); });
  $('#dFilter').addEventListener('submit', (e) => {
    e.preventDefault();
    dt.column = $('#dCol').value;
    dt.value = $('#dVal').value;
    dt.offset = 0;
    loadTable(alive);
  });
}

function dataPage(dir) {
  const dt = state.data;
  const total = Number((dt.result || {}).total) || 0;
  const last = Math.max(0, (Math.ceil(total / dt.limit) - 1) * dt.limit);
  dt.offset = { first: 0, prev: Math.max(0, dt.offset - dt.limit), next: Math.min(last, dt.offset + dt.limit), last }[dir] ?? 0;
  const seq = state.seq;
  loadTable(() => seq === state.seq);
}

function selectTable(name) {
  if (name === state.data.table) return;
  state.data.table = name;
  resetTableFilters();
  try { history.replaceState(null, '', `#/data/stores/${enc(name)}`); } catch { /* not needed to work */ }
  const tab = document.querySelector('.view-tabs a');
  if (tab) tab.setAttribute('href', `#/data/stores/${enc(name)}`);
  document.querySelectorAll('.tbl-item').forEach((b) => b.setAttribute('aria-current', String(b.dataset.name === name)));
  $('#tblPanel').innerHTML = `<div class="panel-body">${skeleton(8)}</div>`;
  const seq = state.seq;
  loadTable(() => seq === state.seq);
}

function dataInspect(id, run) {
  if (run && run !== state.runId && state.runs.some((x) => x.run_id === run)) selectRun(run, { rerender: false });
  if (!state.runId) { toast('Select a run first', 'Objects are inspected in the context of a run.', 'error'); return; }
  inspectById(id);
}

function gotoUnits(stage) {
  const dt = state.data;
  dt.table = 'unit_status';
  dt.column = 'stage';
  dt.value = stage;
  dt.offset = 0;
  dt.runOnly = true;
  location.hash = '#/data/stores/unit_status';
}

async function renderOutputsTab(alive, { force = false } = {}) {
  const body = $('#dataBody');
  const runId = state.runId;
  if (!state.data.reportRuns.size && runId) state.data.reportRuns.add(runId);
  body.innerHTML = `<div class="grid-2 out-top">
      <section class="panel"><div class="panel-head"><span class="cap">Build for the selected run</span>${runId ? `<span class="mono">${esc(shortId(runId))}</span>` : '<span class="muted">no run selected</span>'}</div>
        <div class="panel-body">
          <div class="form-actions" style="margin-top:0">
            <button type="button" class="btn" data-ctl data-action="out-export"${runId ? '' : ' disabled'}>Export graph</button>
            <button type="button" class="btn" data-ctl data-action="out-view"${runId ? '' : ' disabled'}>Build viewer</button>
          </div>
          <div class="hint" style="margin-top:8px">Export writes the committed graph as JSON under <span class="mono">data/synth_runs/&lt;run&gt;/export/</span>; the viewer is one standalone HTML page that opens without the console.</div>
          <div id="outJobs"></div>
        </div></section>
      <section class="panel"><div class="panel-head"><span class="cap">Scorecard</span><span class="muted">compare runs side by side</span><span class="spacer"></span>
          <button type="button" class="btn sm primary" data-ctl data-action="out-report" id="outReportBtn">Scorecard</button></div>
        <div class="panel-body flush out-runs" id="outRuns"></div></section>
    </div>
    <section class="panel"><div class="panel-head"><span class="cap">Files</span><span class="mono muted">data/synth_runs/</span><span class="muted" id="outCount"></span></div>
      <div class="panel-body flush" id="outList">${skeleton(5)}</div></section>`;
  renderReportRuns();
  renderOutJobs();
  applyCtl(body);
  if (force || !state.outputs) await loadOutputs();
  if (!alive()) return;
  renderOutputs();
}

async function loadOutputs() {
  try { state.outputs = (await API.outputs({ quiet: true })) || []; state.outputsError = null; } catch (e) { state.outputsError = e; }
}

function renderReportRuns() {
  const box = $('#outRuns');
  if (!box) return;
  const sel = state.data.reportRuns;
  box.innerHTML = state.runs.length ? `<div class="run-pick">${state.runs.map((x) => `<label class="check run-opt"><input type="checkbox" data-report-run="${esc(x.run_id)}"${sel.has(x.run_id) ? ' checked' : ''}>
      <span class="mono">${esc(shortId(x.run_id))}</span>${plainChip(engineName(x.arm))}${statusChip(x.status)}<span class="muted num">${esc(fmtTime(x.started_at))}</span><span class="muted ellip">${esc(x.profile || '')}</span></label>`).join('')}</div>`
    : `<div class="panel-body">${emptyState('No runs yet', NO_RUN_HINT)}</div>`;
  box.querySelectorAll('[data-report-run]').forEach((cb) => cb.addEventListener('change', () => {
    if (cb.checked) sel.add(cb.dataset.reportRun); else sel.delete(cb.dataset.reportRun);
    updateReportBtn();
  }));
  updateReportBtn();
}
function updateReportBtn() {
  const b = $('#outReportBtn');
  if (!b) return;
  const n = [...state.data.reportRuns].filter((id) => state.runs.some((x) => x.run_id === id)).length;
  b.textContent = `Scorecard for ${n} run${n === 1 ? '' : 's'}`;
  b.disabled = !n;
  applyCtl(b.parentElement);
}
function renderOutJobs() {
  const box = $('#outJobs');
  if (!box) return;
  const running = state.jobs.filter((j) => j.status === 'running' && ['export', 'view', 'report'].includes(j.kind));
  box.innerHTML = running.length ? `<div class="hint" style="margin-top:8px">${running.map((j) => `<div><span class="spinner" aria-hidden="true" style="width:10px;height:10px"></span> ${esc(j.kind)} running · <button type="button" class="linkish mono" data-action="goto-job" data-id="${esc(j.job_id)}">${esc(shortId(j.job_id))}</button></div>`).join('')}</div>` : '';
}

function renderOutputs() {
  const box = $('#outList');
  if (!box) return;
  if (state.outputsError && !state.outputs) { box.innerHTML = `<div class="panel-body">${errorBox(state.outputsError, 'Could not list outputs')}</div>`; return; }
  const files = state.outputs || [];
  $('#outCount').textContent = files.length ? `${files.length} file${files.length > 1 ? 's' : ''}` : '';
  if (!files.length) { box.innerHTML = `<div class="panel-body">${emptyState('No outputs yet', '<div>Export a graph, build a viewer or a scorecard above.</div>')}</div>`; return; }
  const groups = new Map();
  files.forEach((f) => { const k = f.run_id || ''; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(f); });
  const keys = [...groups.keys()].sort((a, b) => (a === state.runId ? -1 : b === state.runId ? 1 : 0) || (!a) - (!b));
  box.innerHTML = keys.map((k) => {
    const run = state.runs.find((x) => x.run_id === k);
    const list = groups.get(k);
    return `<div class="out-group">
      <div class="out-gh">${k ? `<span class="mono" title="${esc(k)}">${esc(shortId(k))}</span>${run ? `${plainChip(engineName(run.arm))}${statusChip(run.status)}<span class="muted">${esc(run.profile || '')}</span>` : ''}${k === state.runId ? chip('selected run', STATUS_COLORS.RUNNING) : `<button type="button" class="linkish" data-action="select-run" data-id="${esc(k)}">select</button>`}` : '<span class="ink2">Not tied to a run</span><span class="muted">reports and other files</span>'}
        <span class="spacer"></span><span class="muted">${esc(list.length)} file${list.length > 1 ? 's' : ''}</span></div>
      <table class="data"><tbody>${list.map((f) => {
    const rel = k && String(f.path).startsWith(`${k}/`) ? String(f.path).slice(k.length + 1) : String(f.path);
    const isHtml = /\.html?$/i.test(f.path);
    return `<tr>
          <td style="width:80px">${chip(f.kind || 'other', OUTPUT_COLORS[f.kind] || GREY)}</td>
          <td class="mono wrap" title="${esc(f.path)}">${esc(rel)}</td>
          <td class="r num" style="width:90px">${esc(fmtBytes(f.size))}</td>
          <td class="num muted" style="width:150px">${esc(fmtTime(f.modified))}</td>
          <td class="r" style="width:110px">${isHtml ? `<a class="btn sm" href="${esc(outputUrl(f.path))}" target="_blank" rel="noopener">Open ↗</a>` : `<a class="btn sm" href="${esc(outputUrl(f.path))}" download="${esc(String(f.path).split('/').pop())}">Download</a>`}</td>
        </tr>`;
  }).join('')}</tbody></table></div>`;
  }).join('');
}

async function outputJob(kind, btn) {
  const runId = state.runId;
  let call;
  if (kind === 'export') call = () => API.exportRun(runId);
  else if (kind === 'view') call = () => API.buildViewer(runId);
  else {
    const ids = [...state.data.reportRuns].filter((id) => state.runs.some((x) => x.run_id === id));
    if (!ids.length) return;
    call = () => API.report(ids);
  }
  btn.disabled = true;
  try {
    const job = await call();
    jobStarted(job);
    renderOutJobs();
    trackJob(job.job_id, {
      onDone: async (j) => {
        await loadOutputs();
        if (state.view === 'data' && state.data.tab === 'outputs') { renderOutputs(); renderOutJobs(); }
        if (j.status === 'done' && kind === 'view') {
          const f = (state.outputs || []).find((o) => o.run_id === runId && o.kind === 'viewer' && /\.html?$/i.test(o.path));
          if (f) toast('Viewer built', `${f.path} — open it from Data → Outputs`, 'ok');
        }
      },
    });
  } catch { /* toast shown */ } finally { btn.disabled = false; applyCtl($('#dataBody') || document); }
}

/* ================================================================ inspector */
function openInspector() {
  document.body.classList.remove('insp-collapsed');
  $('#inspectorToggle').setAttribute('aria-expanded', 'true');
}
function collapseInspector() {
  document.body.classList.add('insp-collapsed');
  $('#inspectorToggle').setAttribute('aria-expanded', 'false');
}
function toggleInspector() {
  const collapsed = document.body.classList.toggle('insp-collapsed');
  state.inspUser = collapsed ? 'closed' : 'open';
  if (!collapsed && state.insp.current && state.insp.current.kind === 'slot') inspect(state.insp.current, { push: false });
  $('#inspectorToggle').setAttribute('aria-expanded', String(!collapsed));
}
function inspectorEmpty() {
  state.sp = null;
  clearTreeSelection();
  state.insp.stack = [];
  state.insp.current = null;
  $('#inspKind').textContent = 'Inspector';
  $('#inspBack').hidden = true;
  $('#inspBody').innerHTML = '<div class="insp-empty">Select a node, edge, highlight or row to inspect it here.<br><br>Every fact links back to the sentence it came from.</div>';
}

function inspectById(id) {
  if (!id) return;
  if (id.startsWith('ent_')) inspect({ kind: 'entity', id });
  else if (id.startsWith('mad_') || id.startsWith('med_')) inspect({ kind: id.startsWith('mad_') ? 'edge' : 'object', id });
  else inspect({ kind: 'object', id });
}

const RUNLESS_KINDS = new Set(['slot', 'stage']);   // these do not need a selected run
function inspect(target, { push = true } = {}) {
  if (!state.runId && !RUNLESS_KINDS.has(target.kind)) return;
  const cur = state.insp.current;
  const same = cur && cur.kind === target.kind && cur.id === target.id && cur.idx === target.idx && cur.profile === target.profile && cur.slot === target.slot && cur.engine === target.engine && cur.tree === target.tree;
  if (push && cur && !same) state.insp.stack.push(cur);
  if (state.insp.stack.length > 30) state.insp.stack.shift();
  state.insp.current = { ...target, runId: state.runId };
  $('#inspBack').hidden = !state.insp.stack.length;
  openInspector();
  const seq = ++state.insp.seq;
  const alive = () => seq === state.insp.seq;
  const body = $('#inspBody');
  body.scrollTop = 0;
  body.innerHTML = `<div class="insp-section">${skeleton(5)}</div>`;
  const kinds = { entity: 'Entity', edge: 'Fact (macro edge)', object: 'Object', sentence: 'Sentence', slot: 'Slot', stage: 'Engine stage' };
  if (target.kind !== 'slot') state.sp = null;
  if (target.kind !== 'slot' && target.kind !== 'stage') clearTreeSelection();
  $('#inspKind').textContent = kinds[target.kind] || 'Inspector';
  const fn = { entity: renderEntityInspector, edge: renderEdgeInspector, object: renderObjectInspector, sentence: renderSentenceInspector, slot: renderSlotPanel, stage: renderStageInspector }[target.kind];
  Promise.resolve(fn(target, alive)).catch((e) => {
    if (alive()) body.innerHTML = `<div class="insp-section">${errorBox(e, 'Could not load')}</div>`;
  });
}
function inspectBack() {
  const prev = state.insp.stack.pop();
  if (prev) inspect(prev, { push: false });
}

async function renderEntityInspector(target, alive) {
  const runId = state.runId;
  const [data, g] = await Promise.all([API.entity(runId, target.id, { quiet: true }), getGraph(runId, { quiet: true }).catch(() => null)]);
  if (!alive()) return;
  const e = data.entity || {};
  (data.edges || []).forEach((ed) => state.edgeReg.set(ed.id, ed));
  const node = nodeFor(g, e.id || target.id);
  const merged = (node && node.merged && node.merged.length ? node.merged : (e.members || []).filter((m) => m !== e.id));
  state.insp.entity = data;
  const roles = data.roles || [];
  const flags = data.flags || [];
  $('#inspBody').innerHTML = `
    <div class="insp-section">
      <div class="chips">${typeChip(e.type)}${statusChip(e.status)}${e.source ? plainChip(e.source, 'source') : ''}${e.subtype ? plainChip(e.subtype, 'subtype') : ''}</div>
      <div class="insp-title">${esc(e.label || target.id)}</div>
      <div class="insp-id">${esc(e.id || target.id)}${e.requested_id && e.requested_id !== e.id ? ` · requested ${esc(e.requested_id)} (merged into this)` : ''}</div>
      ${node ? `<div class="muted" style="font-size:11.5px;margin-top:4px">${esc(fmtNum(node.mentions))} mentions · ${esc((node.docs || []).length)} documents</div>` : ''}
      ${e.merged_into ? `<div style="margin-top:4px">Merged into <button type="button" class="linkish mono" data-action="inspect" data-id="${esc(e.merged_into)}">${esc(entityLabel(e.merged_into))}</button></div>` : ''}
    </div>
    ${(data.aliases || []).length ? `<div class="insp-section"><span class="cap">Aliases</span><div class="chips">${data.aliases.map((a) => plainChip(a)).join('')}</div></div>` : ''}
    ${roles.length ? `<div class="insp-section"><span class="cap">Roles</span>${roles.map((ro) => `<div class="flag-row">${plainChip(ro.role || 'role')}${ro.org_id ? `<button type="button" class="linkish" data-action="inspect" data-id="${esc(ro.org_id)}">${esc(ro.org_label || shortId(ro.org_id))}</button>` : `<span>${esc(ro.org_label || '')}</span>`}${ro.time ? `<button type="button" class="linkish mono muted" data-action="inspect-obj" data-id="${esc(ro.time)}">${esc(String(ro.time).startsWith('tim_') ? shortId(ro.time) : ro.time)}</button>` : ''}</div>`).join('')}</div>` : ''}
    ${merged.length ? `<div class="insp-section"><span class="cap">Merged from (${merged.length})</span><div class="chips">${merged.map((m) => `<button type="button" class="chip plain" style="--c:#5f6e7c" data-action="inspect-obj" data-id="${esc(m)}" title="${esc(m)} — open its provenance and merge history"><i class="dot"></i>${esc(shortId(m))}</button>`).join('')}</div></div>` : ''}
    ${flags.length ? `<div class="insp-section"><span class="cap">Flags</span>${flags.map((f) => `<div class="flag-row">${chip(f.kind || 'flag', '#fab219')}${statusChip(f.state)}<button type="button" class="linkish ink" data-action="inspect-obj" data-id="${esc(f.id)}" style="flex:1">${esc(f.note || shortId(f.id))}</button></div>`).join('')}</div>` : ''}
    <div class="tabs" role="tablist" aria-label="Entity details">
      ${[['facts', 'Facts', (data.edges || []).length], ['timeline', 'Timeline', (data.timeline || []).length], ['mentions', 'Mentions', (data.mentions || []).length], ['resolution', 'Resolution', (data.resolutions || []).length], ['history', 'History', null]]
    .map(([k, l, n]) => `<button type="button" role="tab" id="tab-${k}" aria-controls="tabpanel" aria-selected="${state.insp.tab === k}" data-action="tab" data-tab="${k}">${esc(l)}${n !== null ? `<span class="num">${esc(n)}</span>` : ''}</button>`).join('')}
    </div>
    <div class="tabpanel" id="tabpanel" role="tabpanel" aria-labelledby="tab-${state.insp.tab}"></div>`;
  renderEntityTab(alive);
}

function setTab(tab) {
  state.insp.tab = tab;
  document.querySelectorAll('.tabs [role=tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
  const p = $('#tabpanel');
  if (p) p.setAttribute('aria-labelledby', `tab-${tab}`);
  renderEntityTab(() => true);
}

function factButton(ed) {
  return `<button type="button" class="fact" data-action="focus-edge" data-id="${esc(ed.id)}">
    <span class="who"><span class="dir" aria-label="${ed.direction === 'in' ? 'incoming from' : 'outgoing to'}">${ed.direction === 'in' ? '←' : '→'}</span>${esc(ed.other_label || entityLabel(ed.other_id))}</span>
    <span>${statusChip(ed.status)}</span>
    <span class="meta">${confBar(ed.confidence)}${ed.negated ? chip('NEGATED', STATUS_COLORS.FAILED, 'solid') : ''}${ed.stance && ed.stance !== 'stated' ? plainChip(ed.stance) : ''}${ed.tier ? plainChip(ed.tier, 'last tier') : ''}${timeChips(ed.time)}</span>
  </button>`;
}

async function renderEntityTab(alive) {
  const data = state.insp.entity;
  const p = $('#tabpanel');
  if (!data || !p) return;
  const tab = state.insp.tab;
  if (tab === 'facts') {
    const groups = new Map();
    (data.edges || []).forEach((ed) => { if (!groups.has(ed.rel)) groups.set(ed.rel, []); groups.get(ed.rel).push(ed); });
    p.innerHTML = groups.size ? [...groups.entries()].sort((a, b) => b[1].length - a[1].length).map(([rel, list]) =>
      `<div class="group-h"><span class="mono">${esc(rel)}</span><span class="muted num">${list.length}</span></div>${list.map(factButton).join('')}`).join('')
      : '<div class="muted">No facts touch this entity.</div>';
  } else if (tab === 'timeline') {
    const items = (data.timeline || []).slice().sort((a, b) => (a.when === null || a.when === undefined) - (b.when === null || b.when === undefined) || String(a.when || '').localeCompare(String(b.when || '')));
    p.innerHTML = items.length ? `<ol class="timeline">${items.map((t) => `<li>
        <div class="when">${esc(t.when || 'undated')}</div>
        <button type="button" class="linkish ink" data-action="focus-edge" data-id="${esc(t.edge_id)}">${esc(t.text || t.edge_id)}</button> ${t.status ? statusChip(t.status) : ''}
      </li>`).join('')}</ol>` : '<div class="muted">No dated facts.</div>';
  } else if (tab === 'mentions') {
    const color = typeColor((data.entity || {}).type);
    p.innerHTML = (data.mentions || []).length ? data.mentions.map((m) => {
      const loc = locate(m.sentence, m.text, m.start, m.end);
      return `<div class="mention" style="--c:${color}">
        <div class="src"><span>${esc(m.doc || shortId(m.doc_id))}</span><span>s${esc(m.sentence_idx ?? '?')}</span><span class="spacer"></span></div>
        <div class="s">${m.sentence ? (loc ? markSlice(m.sentence, loc[0], loc[1]) : esc(m.sentence)) : `<mark>${esc(m.text)}</mark>`}</div>
        <button type="button" class="linkish" style="font-size:11.5px;margin-top:2px" data-action="open-doc" data-doc="${esc(m.doc_id)}" data-idx="${esc(m.sentence_idx ?? '')}">Open in document ↗</button>
      </div>`;
    }).join('') : '<div class="muted">No mentions.</div>';
  } else if (tab === 'resolution') {
    const res = data.resolutions || [];
    p.innerHTML = res.length ? `<table class="data"><thead><tr><th>Other</th><th>Decision</th><th>Rule</th><th class="r">Score</th></tr></thead><tbody>${res.map((x) => `<tr>
        <td><button type="button" class="linkish" data-action="inspect" data-id="${esc(x.other_id)}">${esc(x.other_label || shortId(x.other_id))}</button></td>
        <td>${statusChip(x.decision)}<div class="muted" style="font-size:11px">${esc(x.decided_by || '')}</div></td>
        <td class="mono" style="overflow-wrap:anywhere">${esc(x.rule_id || '—')}</td>
        <td class="r num">${esc(x.score === null || x.score === undefined ? '—' : fmtNum(x.score, 2))}</td></tr>`).join('')}</tbody></table>`
      : '<div class="muted">No resolution decisions involve this entity.</div>';
  } else if (tab === 'history') {
    p.innerHTML = skeleton(4);
    try {
      const prov = await API.provenance(state.runId, (data.entity || {}).id, { quiet: true });
      if (!alive() || state.insp.tab !== 'history') return;
      p.innerHTML = renderHistory(prov.history);
    } catch (e) {
      if (alive()) p.innerHTML = errorBox(e, 'Could not load history');
    }
  }
}

function edgeEnds(ed, g) {
  const lab = (id) => (nodeFor(g, id) || {}).label;
  let src = lab(ed.source);
  let dst = lab(ed.target);
  if (ed.other_label) {
    if (ed.direction === 'out' && !dst) dst = ed.other_label;
    if (ed.direction === 'in' && !src) src = ed.other_label;
  }
  return { src: src || shortId(ed.source), dst: dst || shortId(ed.target) };
}

async function renderEdgeInspector(target, alive) {
  const runId = state.runId;
  const g = await getGraph(runId, { quiet: true }).catch(() => null);
  let ed = state.edgeReg.get(target.id) || (g && g.edges.get(target.id));
  if (!alive()) return;
  if (!ed) { await renderObjectInspector(target, alive); return; }
  const { src, dst } = edgeEnds(ed, g);
  await ensureDocuments().catch(() => {});
  const body = $('#inspBody');
  body.innerHTML = `
    <div class="insp-section">
      <div class="chips">${statusChip(ed.status)}${ed.tier ? plainChip(ed.tier, 'last tier') : ''}${ed.stance ? plainChip(ed.stance, 'stance') : ''}${ed.negated ? chip('NEGATED', STATUS_COLORS.FAILED, 'solid') : ''}</div>
      <div class="rel-sentence" style="margin-top:8px">
        <button type="button" class="linkish ink" data-action="inspect" data-id="${esc(ed.source)}"><b>${esc(src)}</b></button>
        <span class="arrow">—${ed.negated ? '<span class="neg">¬</span>' : ''}${esc(ed.rel)}→</span>
        <button type="button" class="linkish ink" data-action="inspect" data-id="${esc(ed.target)}"><b>${esc(dst)}</b></button>
      </div>
      ${ed.description ? `<div class="ink2" style="margin-top:4px">${esc(ed.description)}</div>` : ''}
      <div class="insp-id" style="margin-top:4px">${esc(ed.id)}</div>
    </div>
    <div class="insp-section"><div class="kv">
      <span>Confidence</span><span>${confBar(ed.confidence)}</span>
      <span>Stance</span><span>${esc(ed.stance || '—')}</span>
      <span>Negated</span><span>${ed.negated ? '<b style="color:var(--bad)">yes — the source denies this relation</b>' : 'no'}</span>
      <span>Tier</span><span class="mono">${esc(ed.tier || '—')}</span>
      <span>Documents</span><span>${(ed.doc_ids || []).map((d) => `<button type="button" class="linkish" data-action="open-doc" data-doc="${esc(d)}">${esc(docName(d))}</button>`).join(', ') || '—'}</span>
    </div></div>
    ${(ed.time || []).length ? `<div class="insp-section"><span class="cap">Time</span>${ed.time.map((t) => `<div class="flag-row">${plainChip(t.role || 'time')}<span class="mono">${esc(t.label || '—')}</span>${t.value_start || t.value_end ? `<span class="muted mono" style="font-size:11px">${esc(t.value_start || '…')} → ${esc(t.value_end || '…')}</span>` : ''}${t.time_id ? `<button type="button" class="linkish mono" style="font-size:11px" data-action="inspect-obj" data-id="${esc(t.time_id)}">${esc(shortId(t.time_id))}</button>` : ''}</div>`).join('')}</div>` : ''}
    <div id="provBox">${'<div class="insp-section">' + skeleton(3) + '</div>'}</div>`;
  let prov;
  try {
    prov = await API.provenance(runId, ed.id, { quiet: true });
  } catch (e) {
    if (alive()) $('#provBox').innerHTML = `<div class="insp-section">${errorBox(e, 'Could not load provenance')}</div>`;
    return;
  }
  if (!alive()) return;
  $('#provBox').innerHTML = await renderProvenanceSections(prov, runId);
}

async function renderObjectInspector(target, alive) {
  const runId = state.runId;
  const prov = await API.provenance(runId, target.id, { quiet: true });
  if (!alive()) return;
  const html = await renderProvenanceSections(prov, runId);
  if (!alive()) return;
  $('#inspKind').textContent = prov.object_type ? `Object · ${prov.object_type}` : 'Object';
  $('#inspBody').innerHTML = `
    <div class="insp-section">
      <div class="chips">${plainChip(prov.object_type || 'object')}${prov.object && prov.object.status ? statusChip(prov.object.status) : ''}</div>
      <div class="insp-title mono" style="font-size:13px">${esc(prov.object_id || target.id)}</div>
      ${prov.object && (prov.object.canonical_name || prov.object.surface || prov.object.note) ? `<div class="ink2">${esc(prov.object.canonical_name || prov.object.surface || prov.object.note)}</div>` : ''}
      ${prov.object ? `<details style="margin-top:6px"><summary class="cap" style="cursor:pointer">Stored row</summary>${jsonPre(prov.object)}</details>` : ''}
    </div>${html}`;
}

async function renderProvenanceSections(prov, runId) {
  const spans = prov.spans || [];
  const cards = await Promise.all(spans.map(async (s) => {
    let sentence = null;
    let loc = null;
    try {
      if (s.doc_id && s.sentence_idx !== null && s.sentence_idx !== undefined) {
        const d = await getDoc(runId, s.doc_id, { quiet: true });
        const sent = (d.sentences || []).find((x) => x.idx === s.sentence_idx);
        if (sent) {
          sentence = (d.text || '').slice(sent.start, sent.end);
          // spans may run past the first sentence; clip to it
          if (Number.isInteger(s.start) && Number.isInteger(s.end) && s.end > sent.start && s.start < sent.end) {
            loc = [Math.max(0, s.start - sent.start), Math.min(sentence.length, s.end - sent.start)];
          } else {
            loc = locate(sentence, s.quote, null, null);
          }
        }
      }
    } catch { /* fall back to the bare quote */ }
    const grounding = s.grounding ? chip(s.grounding, String(s.grounding).toLowerCase() === 'exact' ? STATUS_COLORS.COMMITTED : STATUS_COLORS.ESCALATED) : '';
    return `<div class="span-card">
      <div class="src"><span>${esc(s.doc || docName(s.doc_id))}</span><span>s${esc(s.sentence_idx ?? '?')}</span><span>[${esc(s.start ?? '?')}–${esc(s.end ?? '?')}]</span>${grounding}${s.score !== null && s.score !== undefined ? `<span>score ${esc(fmtNum(s.score, 2))}</span>` : ''}</div>
      <div class="s">${sentence !== null ? markSlice(sentence, loc && loc[0], loc && loc[1], 'class="q"') : `<mark class="q">${esc(s.quote || '')}</mark>`}</div>
      ${sentence !== null && s.quote && loc && sentence.slice(loc[0], loc[1]) !== s.quote ? `<div class="muted" style="font-size:11px;margin-top:3px">quote: “${esc(s.quote)}”</div>` : ''}
      ${s.doc_id ? `<button type="button" class="linkish" style="font-size:11.5px;margin-top:3px" data-action="open-doc" data-doc="${esc(s.doc_id)}" data-idx="${esc(s.sentence_idx ?? '')}">Open in document ↗</button>` : ''}
    </div>`;
  }));
  const calls = prov.model_calls || [];
  const callCols = ['call_id', 'stage', 'endpoint', 'model_id', 'tokens_in', 'tokens_out', 'latency_ms', 'error'].filter((k) => calls.some((c) => c && c[k] !== undefined && c[k] !== null));
  return `
    <div class="insp-section"><span class="cap">Provenance spans (${spans.length})</span>${cards.join('') || '<div class="muted">No spans recorded for this object.</div>'}</div>
    ${calls.length ? `<div class="insp-section"><span class="cap">Model calls (${calls.length})</span><div class="table-wrap"><table class="data"><thead><tr>${callCols.map((k) => `<th>${esc(k)}</th>`).join('')}</tr></thead><tbody>
      ${calls.map((c) => `<tr>${callCols.map((k) => `<td class="mono" style="overflow-wrap:anywhere">${esc(k === 'call_id' ? shortId(c[k]) : c[k])}</td>`).join('')}</tr>`).join('')}</tbody></table></div></div>` : ''}
    <div class="insp-section"><span class="cap">Change history (${(prov.history || []).length})</span>${renderHistory(prov.history)}</div>`;
}

function renderHistory(history) {
  const rows = history || [];
  if (!rows.length) return '<div class="muted">No change-log rows.</div>';
  return rows.map((h) => {
    const changed = changedKeys(h.before, h.after);
    return `<div class="hist">
      <div class="h1">${plainChip(h.op || 'op')}<span class="mono">${esc(h.module || '')}</span>${h.stage ? plainChip(h.stage, 'stage') : ''}${h.rule_id ? `<span class="mono muted">${esc(h.rule_id)}</span>` : ''}<span class="ts">${esc(fmtTime(h.ts))}</span></div>
      ${changed ? `<div class="muted" style="font-size:11.5px">${changed === 'created' || changed === 'removed' ? esc(changed) : `changed: <span class="mono ink2">${esc(changed)}</span>`}</div>` : ''}
      ${h.before !== null && h.before !== undefined || h.after !== null && h.after !== undefined ? `<details><summary>before → after</summary><div class="ba">
        <div><span class="cap">before</span>${jsonPre(h.before ?? null)}</div><div><span class="cap">after</span>${jsonPre(h.after ?? null)}</div></div></details>` : ''}
    </div>`;
  }).join('');
}

async function renderSentenceInspector(target, alive) {
  const runId = state.runId;
  const [d, g] = await Promise.all([getDoc(runId, target.docId || currentDocId(), { quiet: true }), getGraph(runId, { quiet: true }).catch(() => null)]);
  if (!alive()) return;
  const s = (d.sentences || []).find((x) => x.idx === Number(target.idx));
  if (!s) { $('#inspBody').innerHTML = `<div class="insp-section">${errorBox(`sentence ${target.idx} not found`, 'Not found')}</div>`; return; }
  const own = (d.highlights || []).filter((h) => h.start < s.end && h.end > s.start);
  const facts = dedupe(own.filter((h) => h.kind === 'fact'));
  const ents = dedupe(own.filter((h) => h.kind === 'entity'));
  const times = dedupe(own.filter((h) => h.kind === 'time'));
  $('#inspBody').innerHTML = `
    <div class="insp-section">
      <div class="chips">${covChip(s.coverage)}${s.kind ? plainChip(s.kind, 'kind') : ''}${s.filtered ? plainChip('filtered') : ''}${s.page ? plainChip(`page ${s.page}`) : ''}</div>
      <div class="insp-title" style="font-size:13px">${esc((d.document || {}).filename || '')} · s${esc(s.idx)}</div>
      <div class="insp-id">${esc(s.id)} · chars ${esc(s.start)}–${esc(s.end)}</div>
      <div class="s" style="margin-top:8px;line-height:1.7">${renderSentenceText(d.text || '', s, own, g)}</div>
    </div>
    <div class="insp-section"><span class="cap">Facts (${facts.length})</span>${facts.map((f) => {
    const ed = state.edgeReg.get(f.object_id);
    return `<button type="button" class="fact" data-action="focus-edge" data-id="${esc(f.object_id)}"><span class="who">${esc(f.label || f.object_id)}</span><span>${statusChip(f.status || (ed && ed.status))}</span>${ed ? `<span class="meta">${confBar(ed.confidence)}${ed.negated ? chip('NEGATED', STATUS_COLORS.FAILED, 'solid') : ''}${timeChips(ed.time)}</span>` : ''}</button>`;
  }).join('') || '<div class="muted">No facts grounded in this sentence.</div>'}</div>
    <div class="insp-section"><span class="cap">Entities (${ents.length})</span><div class="chips">${ents.map((h) => {
    const type = (nodeFor(g, h.object_id) || {}).type;
    return `<button type="button" class="chip" style="--c:${typeColor(type)}" data-action="inspect" data-id="${esc(h.object_id)}"><i class="dot"></i>${esc(h.label || shortId(h.object_id))}</button>`;
  }).join('') || '<span class="muted">none</span>'}</div></div>
    <div class="insp-section"><span class="cap">Time references (${times.length})</span><div class="chips">${times.map((h) => `<button type="button" class="chip plain" style="--c:#e6c34a" data-action="inspect-obj" data-id="${esc(h.object_id)}"><i class="dot"></i>${esc(h.label || shortId(h.object_id))}</button>`).join('') || '<span class="muted">none</span>'}</div></div>`;
}
function currentDocId() {
  const reader = $('#reader');
  return reader && reader.__doc ? reader.__doc.docId : null;
}

/* ================================================================ events + boot */
function onClick(e) {
  const a = e.target.closest('[data-action]');
  if (!a) return;
  if (a.matches('tr[data-action]') && e.target.closest('button, a, input, select, label, details') && e.target.closest('[data-action]') === a) return;
  const d = a.dataset;
  const ui = state.graphUi;
  switch (d.action) {
    case 'run-this': openRunFor(d.engine, d.profile); break;
    case 'onto-open': ontoOpen(d.name); break;
    case 'onto-new': ontoNew(); break;
    case 'onto-save': ontoSave(); break;
    case 'onto-saveas': ontoSaveAs(); break;
    case 'onto-delete': ontoDelete(); break;
    case 'onto-paste': ontoPaste(); break;
    case 'onto-add': ontoAdd(d.kind); break;
    case 'onto-del': ontoDel(d.kind, Number(d.i)); break;
    case 'onto-derive': ontoDerive(); break;
    case 'onto-export': ontoExport(); break;
    case 'runs-archived': state.showArchived = !state.showArchived; renderRecentRuns(); renderRunSelect(); break;
    case 'run-archive': archiveRun(d.id, !!d.archived); break;
    case 'gear-stage': state.gear.stage = d.stage || null; state.gear.call = null; loadGearCalls(); break;
    case 'gear-problems': state.gear.problems = !state.gear.problems; loadGearCalls(); break;
    case 'gear-call': openGearCall(d.id); break;
    case 'gear-tab': state.gear.tab = d.tab; renderGearCall(); break;
    case 'gear-refresh': route(); break;
    case 'runs-all': state.runsUi.allEngines = !state.runsUi.allEngines; renderRecentRuns(); break;
    case 'select-run':
      if (d.stay) {   // pick a run without leaving the page (Run view)
        selectRun(d.id, { rerender: false });
        { const seq = state.seq; renderRecentRuns(); renderRunsButtons(); const box = $('#runSel'); if (box) box.innerHTML = `<div class="panel-body">${skeleton(4)}</div>`; loadRunLive(() => seq === state.seq); }
        break;
      }
      selectRun(d.id, { rerender: !d.go }); if (d.go) { if (location.hash === d.go) route(); else location.hash = d.go; }
      break;
    case 'inspect': inspectById(d.id); break;
    case 'inspect-obj': inspect({ kind: 'object', id: d.id }); break;
    case 'focus-edge': focusEdge(d.id); break;
    case 'open-doc': openDoc(d.doc, d.idx); break;
    case 'sentence': {
      const docId = currentDocId();
      if (docId) inspect({ kind: 'sentence', docId, idx: Number(d.idx) });
      break;
    }
    case 'hl':
      document.querySelectorAll('.hl.sel').forEach((x) => x.classList.remove('sel'));
      a.classList.add('sel');
      inspectById(d.oid);
      break;
    case 'tab': setTab(d.tab); break;
    case 'toggle-esc':
      if (state.review.open.has(d.id)) state.review.open.delete(d.id); else state.review.open.add(d.id);
      renderReview();
      break;
    case 'esc-filter': state.review.outcome = d.outcome; renderReview(); break;
    case 'job':
      if (state.view === 'run') selectJob(d.id);
      else { state.control.selectedJob = d.id; location.hash = '#/run'; }
      break;
    case 'goto-job': state.control.selectedJob = d.id; if (location.hash === '#/run') route(); else location.hash = '#/run'; break;
    // engine trees (garage, run)
    case 'tree-node': onTreeNode(d.tree, d.id); break;
    case 'compact':
      state.compact = !state.compact;
      try { localStorage.setItem('synth.console.compact', state.compact ? '1' : '0'); } catch { /* optional */ }
      document.querySelectorAll('[data-action="compact"]').forEach((b) => { b.textContent = state.compact ? 'Detailed' : 'Compact'; b.setAttribute('aria-pressed', String(state.compact)); });
      for (const id of ['garageTree', 'runTree']) { const box = $(`#${id}`); if (box && state.trees[id]) { box.__stage = null; renderTree(id); } }
      break;
    case 'stage-units': gotoUnits(d.stage); break;
    // garage
    case 'pick-engine': pickEngine(d.id); break;
    case 'garage-refresh': state.profiles = null; state.engines = null; state.servers = null; route(); break;
    case 'build-activate': case 'build-copy': case 'build-delete': buildAction(d.action, a); break;
    case 'goto-build':
      state.garage.profile = d.profile;
      if (location.hash.startsWith('#/garage')) route(); else location.hash = '#/garage';
      break;
    // slot panel
    case 'edit-slot': inspect({ kind: 'slot', engine: d.engine, slot: d.slot, profile: d.profile }); break;
    case 'sp-assign': spAssign(a); break;
    case 'sp-speed': { const c = spChoice(); runSpeedTest(c.server && c.server.id, c.model, a); break; }
    case 'sp-reprobe':
      a.disabled = true;
      loadServers(true).then(() => { if (state.insp.current && state.insp.current.kind === 'slot') inspect(state.insp.current, { push: false }); refreshWorkshop(); });
      break;
    case 'sf-save': sfSave(a); break;
    case 'sf-test': sfTest(a); break;
    case 'sf-revert': if (state.insp.current) inspect(state.insp.current, { push: false }); break;
    case 'sf-apply-mode': $('#sfMode').value = d.mode; advDirty(); a.remove(); break;
    // servers
    case 'srv-detect': detectServers(a); break;
    case 'det-add': addDetected(d.i, a); break;
    case 'launch': case 'unlaunch': launchAction(d.action, d.kind, a); break;
    case 'srv-refresh': case 'srv-speed': case 'srv-load': case 'srv-edit': case 'srv-remove': serverAction(d.action, d.id, a); break;
    case 'srv-focus': {
      e.preventDefault();
      const el = $(`#srv-${cssId(d.id)}`);
      if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 1600); }
      break;
    }
    case 'key-save': keySave(d.id, a); break;
    case 'key-clear': keyClear(d.id); break;
    // run
    case 'corpus-refresh': loadCorpus().then(renderCorpus); break;
    case 'corpus-delete': deleteCorpusFile(d.name); break;
    case 'ingest-corpus': ingestCorpus(a); break;
    case 'preflight': runPreflight($('#preflightBtn')); break;
    case 'start-anyway': startRun(true); break;
    case 'resume-run': resumeRun(a); break;
    // dyno
    case 'bench-refresh': { const seq = state.seq; loadBench(() => seq === state.seq); break; }
    case 'bench-sort':
      state.dyno.sort = { key: d.key, dir: state.dyno.sort.key === d.key ? -state.dyno.sort.dir : (BENCH_COLS.find((c) => c[0] === d.key) || [])[2] === 'min' ? 1 : -1 };
      renderBenchTable();
      break;
    case 'bench-clear': state.dyno.sel = []; renderBenchTable(); renderCompare(); break;
    case 'bench-eval': case 'bench-eval-cmp': benchEval($('#evalBtn')); break;
    case 'eval-dismiss': state.dyno.evalJob = null; renderEvalBox(); updateEvalBtn(); break;
    case 'bench-drill': {
      selectRun(d.id, { rerender: false });
      const seq = state.seq;
      renderBenchTable();
      renderDrill(() => seq === state.seq).then(() => { const el = $('#drill'); if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' }); });
      break;
    }
    case 'sort':
      state.metricsSort = { key: d.key, dir: state.metricsSort.key === d.key ? -state.metricsSort.dir : -1 };
      renderMetricsTable();
      break;
    case 'run-audit': runAudit(); break;
    // data
    case 'data-refresh':
      state.data.tables = null; state.data.cols = {}; state.outputs = null;
      route();
      break;
    case 'data-table': selectTable(d.name); break;
    case 'data-order': {
      state.data.order = state.data.order === 'desc' ? 'asc' : 'desc';
      state.data.offset = 0;
      const seq = state.seq;
      loadTable(() => seq === state.seq);
      break;
    }
    case 'data-filter-clear': {
      state.data.column = ''; state.data.value = ''; state.data.offset = 0;
      const seq = state.seq;
      loadTable(() => seq === state.seq);
      break;
    }
    case 'data-page': dataPage(d.dir); break;
    case 'data-inspect': dataInspect(d.id, d.run); break;
    case 'out-export': outputJob('export', a); break;
    case 'out-view': outputJob('view', a); break;
    case 'out-report': outputJob('report', a); break;
    // graph
    case 'retry-cy': cyLoad = null; state.cyStatus = 'loading'; renderStatusbar(); route(); break;
    case 'graph-layout':
      if (state.cy) { state.cy.elements().not('.hidden').layout(coseOptions()).run(); }
      break;
    case 'graph-fit': if (state.cy) state.cy.animate({ fit: { eles: state.cy.elements().not('.hidden'), padding: 30 } }, { duration: 250 }); break;
    case 'graph-clear': clearGraphFocus(true); if (state.cy) state.cy.elements().unselect(); break;
    case 'graph-search': runGraphSearch(true); break;
    case 'graph-reset':
      ui.hiddenTypes.clear(); ui.hiddenRels.clear(); ui.minConf = 0; ui.hideIsolated = false;
      ui.statuses = new Set(['COMMITTED', 'VERIFIED', 'ESCALATED', 'HELD_FOR_HUMAN']);
      { const g = state.graphs[state.runId]; if (g) { renderGraphRail(g); applyGraphFilters(g); } }
      break;
    case 'rels-all': case 'rels-none': {
      const g = state.graphs[state.runId];
      if (!g) break;
      ui.hiddenRels = d.action === 'rels-all' ? new Set() : new Set(g.data.edges.map((x) => x.rel));
      renderGraphRail(g);
      applyGraphFilters(g);
      break;
    }
    default: break;
  }
}

function onKeydown(e) {
  // role="button" spans (document highlights) activate with Enter / Space like real buttons
  if ((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('[role="button"][data-action]')) {
    e.preventDefault();
    e.target.click();
  }
  if (e.key === 'Escape' && window.innerWidth <= 900 && !document.body.classList.contains('insp-collapsed')) toggleInspector();
}

async function boot() {
  document.addEventListener('click', onClick);
  document.addEventListener('keydown', onKeydown);
  $('#runSelect').addEventListener('change', (e) => selectRun(e.target.value));
  $('#inspectorToggle').addEventListener('click', toggleInspector);
  $('#inspClose').addEventListener('click', toggleInspector);
  $('#inspBack').addEventListener('click', inspectBack);
  $('#refreshBtn').addEventListener('click', async () => {
    delete state.graphs[state.runId];
    delete state.positions[state.runId];
    state.docs.clear();
    state.documents = null;
    state.profiles = null;
    state.secrets = null;
    state.corpus = null;
    state.outputs = null;
    state.engines = null;
    state.servers = null;
    state.bench = null;
    state.data.tables = null;
    state.data.cols = {};
    await Promise.allSettled([refreshOverview(), refreshRuns(), refreshJobs(), loadEngines()]);
    renderRunSelect();
    route();
  });
  if (window.innerWidth <= 900) document.body.classList.add('insp-collapsed');
  try { state.compact = localStorage.getItem('synth.console.compact') === '1'; } catch { /* optional */ }
  // a file dropped outside the corpus drop zone must not navigate away from the console
  ['dragover', 'drop'].forEach((ev) => window.addEventListener(ev, (e) => {
    if (e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files') && !e.target.closest('#dropzone')) e.preventDefault();
  }));
  inspectorEmpty();
  loadCytoscape();
  try { state.runId = localStorage.getItem(LS_KEY); } catch { state.runId = null; }
  await Promise.allSettled([refreshOverview(), refreshJobs(true), loadEngines()]);
  await refreshRuns().catch(() => renderRunSelect());
  ensureDocuments().catch(() => {});
  window.addEventListener('hashchange', route);
  route();
  schedule('global:poll', pollGlobal, anyJobRunning() ? 3000 : 10000);
}

/* ================================================================ Gearbox: inside a run, stage by stage, call by call */
const GEAR_FLAG_INFO = {
  error: ['#e5484d', 'the call failed (HTTP error, timeout, refusal)'],
  invalid: ['#e5484d', 'the answer did not fit the output schema'],
  truncated: ['#ec835a', 'the answer hit its token cap and was cut off (finish_reason = length)'],
  empty: ['#ec835a', 'the model answered nothing'],
  repaired: ['#fab219', 'the JSON was broken and had to be repaired'],
  retried: ['#fab219', 'the request was retried (timeouts, 5xx, rate limits)'],
  dropped: ['#b48cff', 'part of the answer was thrown away downstream (see What happened)'],
};
const gearFlag = (f, n) => chip(n === undefined ? f : `${f} ${n}`, (GEAR_FLAG_INFO[f] || [GREY])[0], '', (GEAR_FLAG_INFO[f] || [])[1] || '');
const pct = (a, b) => (b ? `${Math.round((100 * a) / b)}%` : '—');

async function viewGearbox(_params, alive) {
  const ws = $('#workspace');
  const run = currentRun();
  ws.innerHTML = `<div class="view" id="gearPage">
    <div class="view-head"><h1>Gearbox</h1><span class="muted">what every model was asked, what it thought, what it answered, and what survived</span><span class="spacer"></span>
      <button type="button" class="btn sm" data-action="gear-refresh">Refresh</button></div>
    <section class="panel" aria-labelledby="gearH"><div class="panel-head"><span class="cap" id="gearH">Stages</span><span id="gearRun" class="muted"></span><span class="spacer"></span><span id="gearTotals" class="muted"></span></div>
      <div class="panel-body flush" id="gearStages">${run ? skeleton(5) : `<div class="panel-body">${emptyState('No run selected', '<div>Pick a run in the top bar, or start one in <a href="#/run">Run</a>.</div>')}</div>`}</div>
      <div class="panel-foot">Click a stage to see its calls. Flags: ${Object.keys(GEAR_FLAG_INFO).map((f) => gearFlag(f)).join(' ')} — hover a flag for its meaning. <b>Dropped</b> counts what code threw away from the stage's output, by rule.</div></section>
    <section class="panel" id="gearCallsPanel" aria-labelledby="gearCallsH"><div class="panel-head"><span class="cap" id="gearCallsH">Calls</span><span class="muted" id="gearCallsScope"></span><span class="spacer"></span>
      <button type="button" class="btn sm" data-action="gear-problems" id="gearProbBtn" aria-pressed="${state.gear.problems}">${state.gear.problems ? 'Show all calls' : 'Problems only'}</button></div>
      <div class="panel-body flush" id="gearCalls"><div class="panel-body muted">Pick a stage above.</div></div></section>
    <section class="panel" id="gearCall" aria-label="Call detail" hidden></section>
  </div>`;
  if (!run) return;
  state.gear.runId = run.run_id;
  try { state.gear.data = await API.gearbox(run.run_id, { quiet: true }); } catch (e) { if (alive()) $('#gearStages').innerHTML = `<div class="panel-body">${errorBox(e, 'Could not load the gearbox')}</div>`; return; }
  if (!alive()) return;
  const g = state.gear.data;
  if (state.gear.stage && !g.stages.some((s) => s.stage === state.gear.stage)) state.gear.stage = null;
  if (!state.gear.stage) state.gear.stage = (g.stages.find((s) => s.calls) || {}).stage || null;
  renderGearStages();
  await loadGearCalls();
  if (state.gear.call && alive()) openGearCall(state.gear.call);
}

function renderGearStages() {
  const g = state.gear.data;
  const box = $('#gearStages');
  if (!box || !g) return;
  $('#gearRun').innerHTML = `${esc(shortId(g.run_id))} · ${esc(g.engine_name)} · ${statusChip(g.status)}`;
  $('#gearTotals').innerHTML = `${esc(fmtNum(g.calls))} model calls · <b class="${g.problem_calls ? 'warn-t' : 'ok-t'}">${esc(fmtNum(g.problem_calls))} with problems (${pct(g.problem_calls, g.calls)})</b> · ${esc(fmtNum(g.dropped))} items dropped`;
  box.innerHTML = `<div class="table-wrap"><table class="data gear-table"><thead><tr><th>Stage</th><th>Model</th><th class="r">Calls</th><th>Health</th><th>Problems</th><th>Dropped (by rule)</th><th>What it produced</th><th class="r">Tokens in / out / think</th><th class="r">Latency avg · p95</th></tr></thead><tbody>
    ${g.stages.map((s) => {
      const bad = s.problem_calls;
      const okW = s.calls ? Math.round((100 * (s.calls - bad)) / s.calls) : 0;
      const sel = s.stage === state.gear.stage;
      const units = Object.entries(s.units || {}).filter(([k]) => k !== 'DONE');
      return `<tr class="clickable${sel ? ' selected' : ''}" data-action="gear-stage" data-stage="${esc(s.stage)}">
        <td class="nowrap"><b class="ink">${esc(s.label)}</b> <span class="mono muted">${esc(s.stage)}</span>${units.length ? `<div>${units.map(([k, n]) => statusChip(`${k} ${n}`)).join(' ')}</div>` : ''}</td>
        <td class="mono ellip" title="${esc((s.models || []).join(', '))}">${s.calls ? `${esc((s.models || []).join(', ') || '—')}<div class="muted">${esc((s.endpoints || []).join(', '))}</div>` : '<span class="muted">rules (no model)</span>'}</td>
        <td class="r num">${s.calls ? esc(fmtNum(s.calls)) : '—'}</td>
        <td>${s.calls ? `<div class="gear-bar" title="${esc(`${s.calls - bad} clean · ${bad} with problems`)}"><span class="ok" style="width:${okW}%"></span><span class="bad" style="width:${100 - okW}%"></span></div><span class="muted">${pct(s.calls - bad, s.calls)} clean</span>` : ''}</td>
        <td>${Object.entries(s.problems || {}).map(([f, n]) => gearFlag(f, n)).join(' ') || (s.calls ? '<span class="ok-t">none</span>' : '')}</td>
        <td>${Object.entries(s.dropped || {}).slice(0, 4).map(([r, n]) => plainChip(`${r} ${n}`, 'rule that dropped items')).join(' ') || '<span class="muted">—</span>'}</td>
        <td>${Object.entries(s.changes || {}).filter(([k]) => !k.startsWith('DROPPED')).slice(0, 4).map(([k, n]) => `<span class="gear-op">${esc(k.toLowerCase())} <b>${esc(fmtNum(n))}</b></span>`).join('') || '<span class="muted">—</span>'}</td>
        <td class="r num">${s.calls ? `${esc(fmtTokens(s.tokens_in))} / ${esc(fmtTokens(s.tokens_out))}${s.reasoning_tokens ? ` / ${esc(fmtTokens(s.reasoning_tokens))}` : ''}` : '—'}</td>
        <td class="r num">${s.calls ? `${esc(fmtNum(s.latency_ms_avg))} · ${esc(fmtNum(s.latency_ms_p95))} ms` : '—'}</td></tr>`;
    }).join('')}</tbody></table></div>`;
}

async function loadGearCalls() {
  const g = state.gear.data;
  const box = $('#gearCalls');
  if (!box || !g) return;
  renderGearStages();
  const btn = $('#gearProbBtn');
  if (btn) { btn.textContent = state.gear.problems ? 'Show all calls' : 'Problems only'; btn.setAttribute('aria-pressed', String(state.gear.problems)); }
  const st = g.stages.find((s) => s.stage === state.gear.stage);
  $('#gearCallsScope').textContent = st ? `${st.label} · ${state.gear.problems ? 'problem calls only' : 'all calls'}` : '';
  if (!st || !st.calls) { box.innerHTML = `<div class="panel-body muted">${st ? `${esc(st.label)} runs rules, not a model: see its counts above, or its rows in Data → Stores (change_log).` : 'Pick a stage above.'}</div>`; return; }
  box.innerHTML = `<div class="panel-body">${skeleton(3)}</div>`;
  const seq = state.seq;
  let res;
  try { res = await API.gearCalls(g.run_id, st.stage, state.gear.problems, { quiet: true }); } catch (e) { box.innerHTML = `<div class="panel-body">${errorBox(e, 'Could not load the calls')}</div>`; return; }
  if (seq !== state.seq) return;
  state.gear.calls = res.calls;
  if (!res.calls.length) { box.innerHTML = `<div class="panel-body ok-t">No problem calls in ${esc(st.label)}.</div>`; return; }
  box.innerHTML = `<div class="table-wrap gear-calls"><table class="data"><thead><tr><th>Call</th><th>Unit</th><th>Flags</th><th class="r">In / out / think</th><th class="r">ms</th><th>Answer starts with</th></tr></thead><tbody>
    ${res.calls.map((c) => `<tr class="clickable${c.call_id === state.gear.call ? ' selected' : ''}" data-action="gear-call" data-id="${esc(c.call_id)}">
      <td><button type="button" class="linkish mono" data-action="gear-call" data-id="${esc(c.call_id)}" title="${esc(c.call_id)}">${esc(shortId(c.call_id))}</button></td>
      <td class="mono ellip" title="${esc(c.unit_id || '')}">${esc(shortId(c.unit_id || '—'))}</td>
      <td>${c.flags.map((f) => gearFlag(f, f === 'dropped' ? c.dropped : undefined)).join(' ') || '<span class="ok-t">clean</span>'}</td>
      <td class="r num">${esc(fmtTokens(c.tokens_in))} / ${esc(fmtTokens(c.tokens_out))}${c.reasoning_tokens ? ` / ${esc(fmtTokens(c.reasoning_tokens))}` : ''}</td>
      <td class="r num">${esc(fmtNum(c.latency_ms))}</td>
      <td class="ellip mono muted gear-prev" title="${esc(c.error || c.preview)}">${esc(c.error || c.preview || '(empty)')}</td></tr>`).join('')}</tbody></table></div>
    ${res.total > res.calls.length ? `<div class="panel-foot">Showing ${res.calls.length} of ${res.total}.</div>` : ''}`;
}

async function openGearCall(id) {
  state.gear.call = id;
  const panel = $('#gearCall');
  if (!panel) return;
  document.querySelectorAll('#gearCalls tr').forEach((tr) => tr.classList.toggle('selected', tr.dataset.id === id));
  panel.hidden = false;
  panel.innerHTML = `<div class="panel-body">${skeleton(5)}</div>`;
  try { state.gear.detail = await API.gearCall(id, { quiet: true }); } catch (e) { panel.innerHTML = `<div class="panel-body">${errorBox(e, 'Could not load the call')}</div>`; return; }
  if (state.gear.call !== id) return;
  renderGearCall();
  panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function renderGearCall() {
  const c = state.gear.detail;
  const panel = $('#gearCall');
  if (!panel || !c) return;
  const kept = c.changes.filter((x) => x.op !== 'DROPPED');
  const dropped = c.changes.filter((x) => x.op === 'DROPPED');
  const tabs = [['output', 'Output'], ['thinking', `Thinking${c.reasoning ? '' : ' (none)'}`], ['prompt', 'Prompt'], ['happened', `What happened (${kept.length} kept · ${dropped.length} dropped)`], ['meta', 'Meta']];
  const pre = (txt, cls = '') => `<pre class="json gear-pre ${cls}">${esc(txt || '')}</pre>`;
  let body = '';
  if (state.gear.tab === 'output') {
    body = `${c.meta.error ? `<div class="error-box"><b>Error.</b> <code>${esc(c.meta.error)}</code></div>` : ''}
      <div class="cap">Raw answer</div>${pre(c.output || '(empty)')}
      ${c.parsed !== null && c.parsed !== undefined ? `<div class="cap" style="margin-top:10px">Parsed JSON</div>${jsonPre(c.parsed)}` : '<div class="note warn" style="margin-top:10px"><span>The answer is not valid JSON.</span></div>'}`;
  } else if (state.gear.tab === 'thinking') {
    body = c.reasoning ? pre(c.reasoning) : '<div class="muted">This model returned no separate thinking (instruct models usually do not; reasoning models put it in &lt;think&gt; or [THINK] blocks).</div>';
  } else if (state.gear.tab === 'prompt') {
    body = `<div class="muted">prompt <span class="mono">${esc(c.prompt_name || '—')}</span> · attempt ${esc(String(c.attempt ?? 0))} · structured mode <span class="mono">${esc(c.structured_mode || '—')}</span></div>
      <div class="cap" style="margin-top:8px">System</div>${pre(c.system || '(none)')}<div class="cap" style="margin-top:10px">User (the source text and instructions it was given)</div>${pre(c.user)}`;
  } else if (state.gear.tab === 'happened') {
    const row = (x) => `<tr><td>${chip(x.op, x.op === 'DROPPED' ? '#b48cff' : x.op.startsWith('REMOVED') ? '#e5484d' : x.op === 'CREATE' ? '#2fbf4f' : '#4ea1ff')}</td><td class="mono">${esc(x.object_type)}</td><td class="mono">${esc(x.rule_id || '')}</td>
      <td><details class="jcell"><summary class="mono ellip">${esc(typeof (x.after ?? x.before) === 'string' ? (x.after ?? x.before) : JSON.stringify(x.after ?? x.before)).slice(0, 140)}</summary>${x.before !== null && x.before !== undefined ? `<div class="cap">before</div>${jsonPre(x.before)}` : ''}${x.after !== null && x.after !== undefined ? `<div class="cap">after</div>${jsonPre(x.after)}` : ''}</details></td></tr>`;
    body = c.changes.length ? `<div class="muted" style="margin-bottom:6px">Rows this call's answer created or changed in the stores, and what code dropped from it (with the rule). Dropped items never reach the graph.</div>
      <div class="table-wrap"><table class="data"><thead><tr><th>Op</th><th>Object</th><th>Rule</th><th>Content</th></tr></thead><tbody>${[...dropped, ...kept].map(row).join('')}</tbody></table></div>`
      : '<div class="muted">No store changes are tied to this call (for a review call, its effect shows on the rows it confirmed or removed; for a failed call, nothing was written).</div>';
  } else {
    body = `<div class="table-wrap"><table class="data"><tbody>${Object.entries(c.meta).map(([k, v]) => `<tr><td class="mono muted">${esc(k)}</td><td class="mono">${esc(v === null || v === undefined ? '—' : String(v))}</td></tr>`).join('')}
      <tr><td class="mono muted">unit</td><td class="mono">${esc(c.unit_id || '—')} ${c.unit ? statusChip(c.unit.status) : ''} ${c.unit && c.unit.attempts ? `attempts ${esc(String(c.unit.attempts))}` : ''}</td></tr>
      ${c.unit && c.unit.error ? `<tr><td class="mono muted">unit error</td><td class="mono">${esc(c.unit.error)}</td></tr>` : ''}
      <tr><td class="mono muted">calls for this unit</td><td>${c.unit_calls.map((id) => id === c.call_id ? `<b class="mono">${esc(shortId(id))}</b>` : `<button type="button" class="linkish mono" data-action="gear-call" data-id="${esc(id)}">${esc(shortId(id))}</button>`).join(' · ')}</td></tr></tbody></table></div>`;
  }
  panel.innerHTML = `<div class="panel-head"><span class="cap">Call</span><span class="mono" title="${esc(c.call_id)}">${esc(shortId(c.call_id))}</span><span class="mono muted">${esc(c.stage)} · ${esc(c.model || '—')} · ${esc(c.endpoint || '')}</span>${c.flags.map((f) => gearFlag(f)).join(' ') || '<span class="ok-t">clean</span>'}</div>
    <div class="view-tabs" role="tablist" aria-label="Call">${tabs.map(([id, label]) => `<a role="tab" href="javascript:void 0" data-action="gear-tab" data-tab="${id}" aria-selected="${state.gear.tab === id}">${esc(label)}</a>`).join('')}</div>
    <div class="panel-body" role="tabpanel">${body}</div>`;
}

async function archiveRun(id, archived) {
  try { await API.archiveRun(id, archived); } catch { return; }
  toast(archived ? 'Run archived' : 'Run restored', archived ? `${shortId(id)} is hidden from the lists · Show archived brings it back` : shortId(id), 'ok');
  await refreshRuns(true).catch(() => {});
  renderRunSelect();
  if (state.view === 'run') { renderRecentRuns(); const seq = state.seq; loadRunLive(() => seq === state.seq); }
}

/* ================================================================ Ontology: what Synth extracts (entities, relations, …) */
const ONTO_QUICK_HELP = `entity person: patient, doctor        a class and its types ("entity drug" for none)
entity organization: hospital, insurer
relation treats: person -> person        ends are classes, or any
relation prescribed: person -> drug *    * critical (extra scrutiny)
relation works_at: person -> organization !   ! functional (one at a time)
alias employed at: works_at              a phrase the model may use
label: money amount                      extra GLiNER labels
status: alleged, confirmed               claim statuses
link: because, unless                    logic link types`;
const listCsv = (a) => (a || []).join(', ');
const csvList = (s) => String(s || '').split(/[,;\n]/).map((x) => x.trim()).filter(Boolean);
const ontoSnake = (s) => String(s || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');

async function loadOntologies() {
  try { state.onto.list = (await API.ontologies({ quiet: true })) || []; } catch { state.onto.list = state.onto.list || []; }
  return state.onto.list;
}
function renderRunOntologySelect() {
  const sel = $('#fOnto');
  if (!sel) return;
  const list = state.onto.list || [];
  const ui = state.runsUi;
  if (!ui.ontology || !list.some((o) => o.name === ui.ontology)) ui.ontology = (list.find((o) => o.default) || list[0] || {}).name || '';
  sel.innerHTML = list.length ? list.map((o) => `<option value="${esc(o.name)}"${o.name === ui.ontology ? ' selected' : ''}>${esc(o.name)}${o.default ? ' (default)' : ''} — ${esc(fmtNum(o.entity_types))} types · ${esc(fmtNum(o.relations))} relations</option>`).join('') : '<option value="">default</option>';
}

// The editor works on a flat draft; the server stores the pack form.
function ontoToDraft(data) {
  const d = data || {};
  const rels = [];
  Object.entries(d.relations || {}).forEach(([group, rs]) => Object.entries(rs || {}).forEach(([name, ends]) => rels.push({
    name, group, src: (ends || [])[0] || 'any', dst: (ends || [])[1] || 'any',
    critical: (d.critical_relations || []).includes(name), functional: (d.functional_relations || []).includes(name) })));
  return {
    pack: d.pack || 'custom', version: d.version || 1,
    classes: Object.entries(d.entity_types || {}).map(([name, subs]) => ({ name, types: listCsv(subs) })),
    relations: rels,
    aliases: Object.entries(d.relation_aliases || {}).map(([phrase, rel]) => ({ phrase, rel })),
    labels: listCsv(d.gliner_labels), status: listCsv(d.proposition_status), links: listCsv(d.link_types),
    fy: (d.time || {}).fiscal_year_start_month || 1, roles: listCsv((d.time || {}).time_roles),
    strip: listCsv(d.honorifics_strip), keep: listCsv(d.honorifics_keep),
  };
}
function draftToOnto(dr) {
  const groups = {};
  dr.relations.filter((r) => ontoSnake(r.name)).forEach((r) => { const g = ontoSnake(r.group) || 'general'; (groups[g] = groups[g] || {})[ontoSnake(r.name)] = [ontoSnake(r.src) || 'any', ontoSnake(r.dst) || 'any']; });
  return {
    pack: ontoSnake(dr.pack) || 'custom', version: Number(dr.version) || 1,
    gliner_labels: csvList(dr.labels),
    entity_types: Object.fromEntries(dr.classes.filter((c) => ontoSnake(c.name)).map((c) => [ontoSnake(c.name), csvList(c.types).map(ontoSnake)])),
    relations: groups,
    relation_aliases: Object.fromEntries(dr.aliases.filter((a) => a.phrase.trim() && a.rel).map((a) => [a.phrase.trim().toLowerCase(), a.rel])),
    functional_relations: dr.relations.filter((r) => r.functional && ontoSnake(r.name)).map((r) => ontoSnake(r.name)),
    critical_relations: dr.relations.filter((r) => r.critical && ontoSnake(r.name)).map((r) => ontoSnake(r.name)),
    proposition_status: csvList(dr.status).map(ontoSnake), link_types: csvList(dr.links).map(ontoSnake),
    time: { fiscal_year_start_month: Number(dr.fy) || 1, time_roles: csvList(dr.roles).map(ontoSnake) },
    honorifics_strip: csvList(dr.strip), honorifics_keep: csvList(dr.keep),
  };
}

async function viewOntology(_params, alive) {
  const ws = $('#workspace');
  ws.innerHTML = `<div class="view" id="ontoPage">
    <div class="view-head"><h1>Ontology</h1><span class="muted">what Synth extracts: entity classes and types, the relations between them, and the rules around them</span></div>
    ${demoBanner('editing ontologies is disabled')}
    <div class="onto-grid">
      <section class="panel" aria-labelledby="ontoListH"><div class="panel-head"><span class="cap" id="ontoListH">Templates</span><span class="spacer"></span>
        <button type="button" class="btn sm" data-ctl data-action="onto-new">New</button></div>
        <div class="panel-body flush" id="ontoList">${skeleton(3)}</div>
        <div class="panel-foot">Built-in templates are read-only: <b>Save as</b> makes your own copy. Pick the ontology for a run in <a href="#/run">Run</a>.</div></section>
      <div id="ontoEditor" class="onto-editor"></div>
    </div></div>`;
  await loadOntologies();
  if (!alive()) return;
  const list = state.onto.list;
  const name = state.onto.name && list.some((o) => o.name === state.onto.name) ? state.onto.name : (list.find((o) => o.default) || list[0] || {}).name;
  if (state.onto.draft && state.onto.dirty) { renderOntoList(); renderOntoEditor(); } else if (name) await ontoOpen(name); else ontoNew();
}

function renderOntoList() {
  const box = $('#ontoList');
  if (!box) return;
  const list = state.onto.list || [];
  box.innerHTML = list.map((o) => `<button type="button" class="onto-item${o.name === state.onto.name ? ' selected' : ''}" data-action="onto-open" data-name="${esc(o.name)}">
      <span class="mono ink">${esc(o.name)}</span>${o.builtin ? plainChip('template', 'built in, read-only') : chip('mine', '#2fbf4f')}${o.default ? plainChip('default') : ''}
      <span class="muted">${esc(fmtNum(o.entity_classes))} classes · ${esc(fmtNum(o.entity_types))} types · ${esc(fmtNum(o.relations))} relations</span></button>`).join('')
    + (state.onto.name === null && state.onto.draft ? '<div class="onto-item selected"><span class="ink">New ontology</span><span class="muted">not saved yet</span></div>' : '');
}

async function ontoOpen(name) {
  if (state.onto.dirty && name !== state.onto.name) {
    const ok = await askDialog({ title: 'Discard unsaved changes?', html: `<p>Your edits to <span class="mono">${esc(state.onto.name || 'the new ontology')}</span> are not saved.</p>`, ok: 'Discard', danger: true });
    if (!ok) return;
  }
  let o;
  try { o = await API.ontology(name); } catch { return; }
  state.onto = { ...state.onto, name, saved: o, draft: ontoToDraft(o.data), dirty: false, problems: [] };
  renderOntoList();
  renderOntoEditor();
  ontoCheck();
}
function ontoNew() {
  state.onto = { ...state.onto, name: null, saved: null, dirty: true, problems: [],
    draft: ontoToDraft({ pack: 'custom', entity_types: { person: [], organization: [] }, relations: { general: { member_of: ['person', 'organization'] } } }) };
  renderOntoList();
  renderOntoEditor();
  ontoCheck();
}

function renderOntoEditor() {
  const box = $('#ontoEditor');
  const dr = state.onto.draft;
  if (!box || !dr) return;
  const saved = state.onto.saved;
  const ro = saved && saved.builtin;
  const classes = dr.classes.map((c) => ontoSnake(c.name)).filter(Boolean);
  const endOpts = (v) => ['any', ...classes].map((c) => `<option value="${esc(c)}"${c === ontoSnake(v) ? ' selected' : ''}>${esc(c)}</option>`).join('')
    + (v && v !== 'any' && !classes.includes(ontoSnake(v)) ? `<option value="${esc(v)}" selected>${esc(v)} (not a class)</option>` : '');
  const relNames = dr.relations.map((r) => ontoSnake(r.name)).filter(Boolean);
  const inp = (kind, i, field, value, ph = '', cls = '') => `<input type="text" class="${cls}" data-onto="${kind}" data-i="${i}" data-f="${field}" value="${esc(value ?? '')}" placeholder="${esc(ph)}" spellcheck="false">`;
  const del = (kind, i) => `<button type="button" class="btn sm" data-ctl data-action="onto-del" data-kind="${kind}" data-i="${i}" aria-label="Remove">✕</button>`;
  const add = (kind, label) => `<button type="button" class="btn sm" data-ctl data-action="onto-add" data-kind="${kind}">+ ${esc(label)}</button>`;
  const field = (f, label, hint, ph = '') => `<label class="onto-field"><span class="cap">${esc(label)}</span><input type="text" data-onto="top" data-f="${f}" value="${esc(dr[f] ?? '')}" placeholder="${esc(ph)}" spellcheck="false"><span class="hint">${hint}</span></label>`;
  box.innerHTML = `
    <section class="panel"><div class="panel-head"><span class="cap">${saved ? esc(saved.name) : 'New ontology'}</span>${ro ? plainChip('read-only template') : ''}<span id="ontoDirty">${state.onto.dirty ? chip('unsaved', '#fab219') : ''}</span><span class="spacer"></span>
        <button type="button" class="btn sm" data-ctl data-action="onto-paste">Paste in…</button>
        <button type="button" class="btn sm" data-action="onto-export">Export YAML</button>
        ${saved && !ro ? '<button type="button" class="btn sm danger" data-ctl data-action="onto-delete">Delete</button>' : ''}
        <button type="button" class="btn sm" data-ctl data-action="onto-saveas">Save as…</button>
        <button type="button" class="btn sm primary" data-ctl data-action="onto-save"${ro || !saved ? ' hidden' : ''}>Save</button></div>
      <div class="panel-body" id="ontoProblems"></div></section>
    <section class="panel"><div class="panel-head"><span class="cap">Entities</span><span class="muted">classes, and the types inside each (bank is a type of organization)</span><span class="spacer"></span>${add('class', 'Add class')}</div>
      <div class="panel-body flush"><table class="data onto-table"><thead><tr><th style="width:26%">Class</th><th>Types (comma-separated)</th><th></th></tr></thead><tbody>
      ${dr.classes.map((c, i) => `<tr><td>${inp('classes', i, 'name', c.name, 'e.g. person', 'mono')}</td><td>${inp('classes', i, 'types', c.types, 'e.g. judge, lawyer (optional)')}</td><td class="r">${del('classes', i)}</td></tr>`).join('')}
      </tbody></table></div></section>
    <section class="panel"><div class="panel-head"><span class="cap">Relations</span><span class="muted">what links two entities, and which classes may sit at each end</span><span class="spacer"></span>${add('relation', 'Add relation')}</div>
      <div class="panel-body flush"><div class="table-wrap"><table class="data onto-table"><thead><tr><th>Relation</th><th>From</th><th></th><th>To</th><th>Group</th><th title="critical: extra scrutiny">Critical</th><th title="functional: one at a time, e.g. a single chair">One at a time</th><th></th></tr></thead><tbody>
      ${dr.relations.map((r, i) => `<tr><td>${inp('relations', i, 'name', r.name, 'e.g. founded', 'mono')}</td>
        <td><select data-onto="relations" data-i="${i}" data-f="src">${endOpts(r.src)}</select></td><td class="muted">→</td>
        <td><select data-onto="relations" data-i="${i}" data-f="dst">${endOpts(r.dst)}</select></td>
        <td>${inp('relations', i, 'group', r.group, 'general', 'mono')}</td>
        <td class="c"><input type="checkbox" data-onto="relations" data-i="${i}" data-f="critical"${r.critical ? ' checked' : ''} aria-label="critical"></td>
        <td class="c"><input type="checkbox" data-onto="relations" data-i="${i}" data-f="functional"${r.functional ? ' checked' : ''} aria-label="one at a time"></td>
        <td class="r">${del('relations', i)}</td></tr>`).join('')}
      </tbody></table></div></div></section>
    <div class="grid-2">
      <section class="panel"><div class="panel-head"><span class="cap">Things to find (GLiNER labels)</span><span class="spacer"></span><button type="button" class="btn sm" data-ctl data-action="onto-derive">Fill from entities</button></div>
        <div class="panel-body"><textarea data-onto="top" data-f="labels" rows="3" spellcheck="false" placeholder="person, judge, organization, bank, …">${esc(dr.labels)}</textarea>
        <div class="hint">What GLiNER looks for in the text. Each label names a class or type (spaces for _). Also: <span class="mono">date, time period, duration</span> (times) and <span class="mono">role title</span>.</div></div></section>
      <section class="panel"><div class="panel-head"><span class="cap">Aliases</span><span class="muted">phrases the model may use for a relation</span><span class="spacer"></span>${add('alias', 'Add alias')}</div>
        <div class="panel-body flush"><table class="data onto-table"><tbody>
        ${dr.aliases.map((a, i) => `<tr><td>${inp('aliases', i, 'phrase', a.phrase, 'e.g. works for')}</td><td class="muted">→</td><td><select data-onto="aliases" data-i="${i}" data-f="rel">${['', ...relNames].map((n) => `<option value="${esc(n)}"${n === a.rel ? ' selected' : ''}>${esc(n || 'pick a relation')}</option>`).join('')}</select></td><td class="r">${del('aliases', i)}</td></tr>`).join('') || '<tr><td class="muted">none</td></tr>'}
        </tbody></table></div></section>
    </div>
    <section class="panel"><div class="panel-head"><span class="cap">Claims, time and names</span></div>
      <div class="panel-body onto-fields">
        ${field('pack', 'Pack name', 'recorded on every run that uses it', 'custom')}
        ${field('status', 'Claim statuses', 'how the text holds a claim (alleged, denied, …)')}
        ${field('links', 'Logic links', 'how claims connect (because, unless, …)')}
        ${field('roles', 'Time roles', 'what a date means on a fact')}
        <label class="onto-field"><span class="cap">Fiscal year starts</span><input type="number" min="1" max="12" data-onto="top" data-f="fy" value="${esc(dr.fy)}"><span class="hint">month 1–12 (FY2025 with 7 = Jul 2024–Jun 2025)</span></label>
        ${field('strip', 'Titles to drop from names', 'Mr, Dr, Justice …')}
        ${field('keep', 'Name parts to keep', 'parts that belong to the name')}
      </div></section>
    <section class="panel" id="ontoPastePanel" hidden><div class="panel-head"><span class="cap">Paste in</span><span class="muted">YAML or JSON of a whole pack, or the quick format below — it replaces the editor's content (nothing is saved until you save)</span></div>
      <div class="panel-body"><textarea id="ontoPasteText" rows="10" spellcheck="false" placeholder="${esc(ONTO_QUICK_HELP)}"></textarea>
      <div class="form-actions"><button type="button" class="btn primary" data-ctl id="ontoPasteGo">Read it</button><button type="button" class="btn" id="ontoPasteCancel">Cancel</button></div>
      <pre class="json gear-pre">${esc(ONTO_QUICK_HELP)}</pre></div></section>`;
  applyCtl(box);
  if (ro) box.querySelectorAll('[data-onto], [data-action="onto-add"], [data-action="onto-del"], [data-action="onto-derive"]').forEach((el) => { if (el.dataset.onto) el.disabled = true; else el.hidden = true; });
  renderOntoProblems();
  box.oninput = box.onchange = (e) => {
    const el = e.target;
    if (!el.dataset || !el.dataset.onto) return;
    const val = el.type === 'checkbox' ? el.checked : el.value;
    if (el.dataset.onto === 'top') dr[el.dataset.f] = val;
    else dr[el.dataset.onto][Number(el.dataset.i)][el.dataset.f] = val;
    if (!state.onto.dirty) { state.onto.dirty = true; const d = $('#ontoDirty'); if (d) d.innerHTML = chip('unsaved', '#fab219'); }
    // a renamed class or relation changes the From/To and alias pickers: redraw once focus has moved on, keep it there
    if (e.type === 'change' && el.dataset.f === 'name' && (el.dataset.onto === 'classes' || el.dataset.onto === 'relations')) {
      setTimeout(() => {
        const a = document.activeElement;
        const key = a && a.dataset && a.dataset.onto ? `[data-onto="${a.dataset.onto}"][data-f="${a.dataset.f}"]${a.dataset.i !== undefined ? `[data-i="${a.dataset.i}"]` : ''}` : null;
        renderOntoEditorHead();
        if (key) { const b = document.querySelector(`#ontoEditor ${key}`); if (b) b.focus(); }
      }, 0);
    }
    ontoCheckSoon();
  };
}
function renderOntoEditorHead() { const scroll = $('#workspace').scrollTop; renderOntoEditor(); $('#workspace').scrollTop = scroll; }

const ontoCheckSoon = debounce(() => ontoCheck(), 400);
async function ontoCheck() {
  if (!state.onto.draft) return;
  try { const r = await API.validateOntology(draftToOnto(state.onto.draft), { quiet: true }); state.onto.problems = r.problems || []; } catch { return; }
  renderOntoProblems();
}
function renderOntoProblems() {
  const box = $('#ontoProblems');
  if (!box) return;
  const pr = state.onto.problems || [];
  const dr = state.onto.draft;
  const n = { classes: dr.classes.filter((c) => c.name.trim()).length, types: dr.classes.reduce((a, c) => a + csvList(c.types).length, 0), rels: dr.relations.filter((r) => r.name.trim()).length };
  box.innerHTML = pr.length
    ? `<div class="note warn" role="status">${ICON.warn}<span><b>${pr.length} thing${pr.length > 1 ? 's' : ''} to fix before saving:</b><ul class="onto-probs">${pr.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></span></div>`
    : `<div class="note ok" style="margin:0"><span><b>Valid.</b> ${n.classes} classes · ${n.types} types · ${n.rels} relations. Runs that pick it extract exactly this vocabulary (Synth −0 and Datanaut Classic build their prompts from it).</span></div>`;
}

function ontoAdd(kind) {
  const dr = state.onto.draft;
  if (kind === 'class') dr.classes.push({ name: '', types: '' });
  if (kind === 'relation') dr.relations.push({ name: '', group: 'general', src: 'any', dst: 'any', critical: false, functional: false });
  if (kind === 'alias') dr.aliases.push({ phrase: '', rel: '' });
  state.onto.dirty = true;
  renderOntoEditorHead();
  const rows = document.querySelectorAll(`#ontoEditor [data-onto="${kind === 'class' ? 'classes' : kind === 'relation' ? 'relations' : 'aliases'}"][data-f="${kind === 'alias' ? 'phrase' : 'name'}"]`);
  if (rows.length) rows[rows.length - 1].focus();
}
function ontoDel(kind, i) {
  state.onto.draft[kind].splice(i, 1);
  state.onto.dirty = true;
  renderOntoEditorHead();
  ontoCheckSoon();
}
function ontoDerive() {
  const dr = state.onto.draft;
  const words = dr.classes.filter((c) => ontoSnake(c.name)).flatMap((c) => [ontoSnake(c.name), ...csvList(c.types).map(ontoSnake)]).map((w) => w.replace(/_/g, ' '));
  const extra = csvList(dr.labels).filter((l) => ['date', 'time period', 'duration', 'role title', 'money amount'].includes(l));
  dr.labels = [...new Set([...words, ...extra])].join(', ');
  state.onto.dirty = true;
  renderOntoEditorHead();
  ontoCheckSoon();
}
async function ontoSave() {
  const name = state.onto.name;
  if (!name) return ontoSaveAs();
  try { const o = await API.saveOntology(name, draftToOnto(state.onto.draft)); state.onto.saved = o; state.onto.draft = ontoToDraft(o.data); state.onto.dirty = false; } catch { return; }
  toast('Ontology saved', name, 'ok');
  await loadOntologies();
  renderOntoList();
  renderOntoEditorHead();
  ontoCheck();
}
async function ontoSaveAs() {
  const base = state.onto.name ? `${state.onto.name.replace(/\.v\d+$/, '')}-custom` : 'my-ontology';
  const ans = await askDialog({ title: 'Save as a new ontology', html: '<p>Saved to your own templates (configs/synth/ontology.local). Pick it for a run in Run.</p>', fields: [nameField(base, 'Name')], ok: 'Save' });
  if (!ans) return;
  const name = buildSlug(ans[0]);
  if ((state.onto.list || []).some((o) => o.name === name)) { toast('That name is taken', `${name} already exists — pick another`, 'error'); return; }
  try { const o = await API.saveOntology(name, draftToOnto(state.onto.draft)); state.onto = { ...state.onto, name, saved: o, draft: ontoToDraft(o.data), dirty: false }; } catch { return; }
  toast('Ontology saved', `${name} · pick it for a run in Run`, 'ok');
  await loadOntologies();
  renderOntoList();
  renderOntoEditorHead();
  ontoCheck();
}
async function ontoDelete() {
  const name = state.onto.name;
  const ok = await askDialog({ title: `Delete ${name}?`, html: '<p>The ontology file is removed. Runs that used it keep their record of it (name, version and fingerprint).</p>', ok: 'Delete', danger: true });
  if (!ok) return;
  try { await API.deleteOntology(name); } catch { return; }
  toast('Ontology deleted', name, 'ok');
  state.onto = { ...state.onto, name: null, saved: null, draft: null, dirty: false };
  route();
}
function ontoPaste() {
  const panel = $('#ontoPastePanel');
  panel.hidden = false;
  panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  $('#ontoPasteText').focus();
  $('#ontoPasteCancel').onclick = () => { panel.hidden = true; };
  $('#ontoPasteGo').onclick = async () => {
    let r;
    try { r = await API.parseOntology($('#ontoPasteText').value); } catch { return; }
    state.onto.draft = ontoToDraft(r.data);
    state.onto.problems = r.problems || [];
    state.onto.dirty = true;
    renderOntoEditor();
    toast('Pasted in', r.problems.length ? `${r.problems.length} thing(s) to fix before saving` : 'valid — save it to use it', r.problems.length ? 'info' : 'ok');
  };
}
function ontoExport() {
  const data = draftToOnto(state.onto.draft);
  const lines = [`# Synth ontology pack (${state.onto.name || 'unsaved'})`, JSON.stringify(data, null, 2)];
  const blob = new Blob([lines.join('\n')], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${state.onto.name || 'ontology'}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

document.addEventListener('DOMContentLoaded', boot);
