// Main process: window, file watchers, tray, IPC.
// Watches:
//   - memory/*.md            -> rebuild graph (calls python build_graph.py)
//   - memory/graph/activity/sessions.json -> push to renderer
//   - ~/.claude/projects/<encoded-cwd>/<sid>.jsonl -> detect ALL CC sessions in real-time
const { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, shell, Notification } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const os = require('os');
const net = require('net');
const tls = require('tls');

const HERE = __dirname;
const GRAPH_DIR = path.resolve(HERE, '..');               // memory/graph/
const MEM_DIR = path.resolve(GRAPH_DIR, '..');             // memory/
const ACTIVITY_DIR = path.join(GRAPH_DIR, 'activity');
const HOME = os.homedir();
const PROJECTS_ROOT = path.join(HOME, '.claude', 'projects');

let win = null;
let tray = null;
let buildQueued = false;
let buildRunning = false;

// In-memory state of CC transcripts: { [sessionId]: { path, cwd, mtime, size, projectId, lastEventTime, lastTool, lastToolTarget, msgCount } }
const TRANSCRIPTS = new Map();

// ---------------- Project inference ----------------
// Add your projects here as [substring_in_cwd_path, project_id_to_assign].
// Path matching is case-insensitive. First match wins.
// Example:
//   ['myapp', 'proj_myapp'],
//   ['client_acme', 'proj_acme'],
const PROJECT_TAGS = [
  // ['example', 'proj_example'],
];
function inferProject(cwd) {
  if (!cwd) return null;
  const low = cwd.toLowerCase().replace(/\\/g, '/');
  for (const [t, p] of PROJECT_TAGS) if (low.includes(t)) return p;
  return null;
}
// Decode encoded cwd dir name (CC encodes: 'C:\Users\PC' -> 'C--Users-PC')
function decodeCwd(dirName) {
  // CC uses pattern: drive prefix 'C-' then path with '-' replacing '\'
  let s = dirName.replace(/^([A-Za-z])--/, '$1:\\');
  s = s.replace(/-/g, '\\');
  return s;
}

// ---------------- Window ----------------
function createWindow() {
  win = new BrowserWindow({
    width: 1400,
    height: 900,
    backgroundColor: '#02030a',
    title: 'Claude Neural Graph',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  win.loadFile(path.join(GRAPH_DIR, 'index.html'));
  win.on('close', (e) => {
    // hide to tray instead of quit
    if (!app.isQuiting) {
      e.preventDefault();
      win.hide();
    }
  });
}

// ---------------- Tray ----------------
function createTray() {
  // simple 16x16 red circle nativeImage built from data URL
  const pngBase64 = 'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAUklEQVR4AWNgGAWjYBSMggwGI7g0GDAOMDgwhDAYMfgxhDDcZTAjQAFsZApB+RBxAUygBQXwBQwgBgmgIGxgYGBhgYBxgcGEIYHBgYGEwYAB0vIyBjMBPwAAAAASUVORK5CYII=';
  const ico = nativeImage.createFromDataURL('data:image/png;base64,' + pngBase64);
  tray = new Tray(ico);
  tray.setToolTip('Claude Neural Graph');
  const menu = Menu.buildFromTemplate([
    { label: 'Show / Hide', click: () => { if (win) (win.isVisible() ? win.hide() : win.show()); } },
    { label: 'Rebuild graph now', click: () => triggerBuild() },
    { label: 'Run health check now', click: () => triggerHealth() },
    { label: 'Recompute growth now', click: () => triggerGrowth() },
    { label: 'Fetch external signals now', click: () => triggerSignals() },
    { label: 'Open memory folder', click: () => shell.openPath(MEM_DIR) },
    { type: 'separator' },
    { label: 'Quit', click: () => { app.isQuiting = true; app.quit(); } },
  ]);
  tray.setContextMenu(menu);
  tray.on('click', () => { if (win) (win.isVisible() ? win.hide() : win.show()); });
}

// ---------------- Graph build ----------------
function triggerBuild() {
  if (buildRunning) { buildQueued = true; return; }
  buildRunning = true;
  const p = spawn('py', [path.join(GRAPH_DIR, 'build_graph.py')], { cwd: GRAPH_DIR });
  p.on('exit', () => {
    buildRunning = false;
    if (buildQueued) { buildQueued = false; triggerBuild(); }
    pushGraphToRenderer();
  });
  p.on('error', () => { buildRunning = false; });
}

function pushGraphToRenderer() {
  try {
    const json = fs.readFileSync(path.join(MEM_DIR, 'graph.json'), 'utf8');
    if (win && !win.isDestroyed()) win.webContents.send('graph:data', JSON.parse(json));
  } catch (e) { /* */ }
}

// ---------------- File watchers ----------------
function watchMemory() {
  // fs.watch can fire many events; debounce
  let timer = null;
  fs.watch(MEM_DIR, { persistent: true }, (_evt, filename) => {
    if (!filename || !filename.endsWith('.md')) return;
    clearTimeout(timer);
    timer = setTimeout(triggerBuild, 600);
  });
}

function watchActivity() {
  if (!fs.existsSync(ACTIVITY_DIR)) fs.mkdirSync(ACTIVITY_DIR, { recursive: true });
  const sessionsPath = path.join(ACTIVITY_DIR, 'sessions.json');
  const healthPath = path.join(ACTIVITY_DIR, 'health.json');
  const growthPath = path.join(ACTIVITY_DIR, 'growth.json');
  const signalsPath = path.join(ACTIVITY_DIR, 'signals.json');
  const anomaliesPath = path.join(ACTIVITY_DIR, 'anomalies.jsonl');
  const processesPath = path.join(ACTIVITY_DIR, 'processes.json');
  const trafficPath = path.join(ACTIVITY_DIR, 'traffic.jsonl');
  let trafficOffset = 0;
  if (fs.existsSync(trafficPath)) { try { trafficOffset = fs.statSync(trafficPath).size; } catch (e) {} }
  function pushSessions() {
    try {
      const txt = fs.readFileSync(sessionsPath, 'utf8');
      const data = JSON.parse(txt);
      if (win && !win.isDestroyed()) win.webContents.send('sessions:data', data);
      maybeNotify(data);
    } catch (e) { /* */ }
  }
  function pushHealth() {
    try {
      const txt = fs.readFileSync(healthPath, 'utf8');
      const data = JSON.parse(txt);
      if (win && !win.isDestroyed()) win.webContents.send('health:data', data);
    } catch (e) { /* */ }
  }
  function pushGrowth() {
    try {
      const txt = fs.readFileSync(growthPath, 'utf8');
      const data = JSON.parse(txt);
      if (win && !win.isDestroyed()) win.webContents.send('growth:data', data);
    } catch (e) { /* */ }
  }
  function pushSignals() {
    try {
      const txt = fs.readFileSync(signalsPath, 'utf8');
      const data = JSON.parse(txt);
      if (win && !win.isDestroyed()) win.webContents.send('signals:data', data);
    } catch (e) { /* */ }
  }
  function pushAnomalies() {
    try {
      const txt = fs.readFileSync(anomaliesPath, 'utf8');
      const lines = txt.split('\n').filter(l => l.trim());
      // last 50 only
      const items = lines.slice(-50).map(l => { try { return JSON.parse(l); } catch (e) { return null; } }).filter(Boolean);
      if (win && !win.isDestroyed()) win.webContents.send('anomalies:data', { items });
    } catch (e) { /* */ }
  }
  function pushProcesses() {
    try {
      const txt = fs.readFileSync(processesPath, 'utf8');
      const data = JSON.parse(txt);
      if (win && !win.isDestroyed()) win.webContents.send('processes:data', data);
    } catch (e) { /* */ }
  }
  function pushTrafficTail() {
    try {
      if (!fs.existsSync(trafficPath)) return;
      const st = fs.statSync(trafficPath);
      if (st.size <= trafficOffset) {
        if (st.size < trafficOffset) trafficOffset = 0;  // file truncated
        return;
      }
      const fd = fs.openSync(trafficPath, 'r');
      const buf = Buffer.alloc(Math.min(st.size - trafficOffset, 256 * 1024));
      fs.readSync(fd, buf, 0, buf.length, trafficOffset);
      fs.closeSync(fd);
      trafficOffset = st.size;
      const events = buf.toString('utf8').split('\n').filter(l => l.trim()).map(l => { try { return JSON.parse(l); } catch (e) { return null; } }).filter(Boolean);
      if (events.length && win && !win.isDestroyed()) {
        win.webContents.send('traffic:events', { events });
      }
    } catch (e) { /* */ }
  }
  fs.watch(ACTIVITY_DIR, { persistent: true }, (_evt, filename) => {
    if (filename === 'sessions.json') pushSessions();
    else if (filename === 'health.json') pushHealth();
    else if (filename === 'growth.json') pushGrowth();
    else if (filename === 'signals.json') pushSignals();
    else if (filename === 'anomalies.jsonl') pushAnomalies();
    else if (filename === 'processes.json') pushProcesses();
    else if (filename === 'traffic.jsonl') pushTrafficTail();
  });
  if (fs.existsSync(sessionsPath)) pushSessions();
  if (fs.existsSync(healthPath)) pushHealth();
  if (fs.existsSync(growthPath)) pushGrowth();
  if (fs.existsSync(signalsPath)) pushSignals();
  if (fs.existsSync(anomaliesPath)) pushAnomalies();
  if (fs.existsSync(processesPath)) pushProcesses();
}

// Run health.py periodically (every 60 min)
let healthRunning = false;
function triggerHealth() {
  if (healthRunning) return;
  healthRunning = true;
  const p = spawn('py', [path.join(GRAPH_DIR, 'health.py')], { cwd: GRAPH_DIR });
  p.on('exit', () => { healthRunning = false; });
  p.on('error', () => { healthRunning = false; });
}

// Run growth.py periodically (every 4 hours)
let growthRunning = false;
function triggerGrowth() {
  if (growthRunning) return;
  growthRunning = true;
  const p = spawn('py', [path.join(GRAPH_DIR, 'growth.py')], { cwd: GRAPH_DIR });
  p.on('exit', () => { growthRunning = false; });
  p.on('error', () => { growthRunning = false; });
}

// Run signals.py periodically (every 6 hours) — external news/releases watcher
let signalsRunning = false;
function triggerSignals() {
  if (signalsRunning) return;
  signalsRunning = true;
  const p = spawn('py', [path.join(GRAPH_DIR, 'signals.py')], { cwd: GRAPH_DIR });
  p.on('exit', () => { signalsRunning = false; });
  p.on('error', () => { signalsRunning = false; });
}

// Process watcher daemon (Phase 6c) — long-running poll
let watcherProc = null;
function startProcessWatcher() {
  if (watcherProc) return;
  try {
    watcherProc = spawn('py', [path.join(GRAPH_DIR, 'process_watcher.py'), '--loop', '5'], { cwd: GRAPH_DIR });
    watcherProc.on('exit', () => { watcherProc = null; });
    watcherProc.on('error', () => { watcherProc = null; });
  } catch (e) { watcherProc = null; }
}
function stopProcessWatcher() {
  if (watcherProc) { try { watcherProc.kill(); } catch (e) {} watcherProc = null; }
}

// Tray notifications disabled (visual ghost in graph is enough).
function maybeNotify(_data) { /* no-op */ }

// ---------------- CC transcripts scan ----------------
// Walk PROJECTS_ROOT, find all <encoded-cwd>/<sid>.jsonl, tail size+mtime
function scanTranscripts() {
  if (!fs.existsSync(PROJECTS_ROOT)) return;
  let entries;
  try { entries = fs.readdirSync(PROJECTS_ROOT, { withFileTypes: true }); } catch (e) { return; }
  for (const ent of entries) {
    if (!ent.isDirectory()) continue;
    const projDir = path.join(PROJECTS_ROOT, ent.name);
    const cwd = decodeCwd(ent.name);
    const projectId = inferProject(cwd);
    let files;
    try { files = fs.readdirSync(projDir, { withFileTypes: true }); } catch (e) { continue; }
    // gather session jsonl files AND subagent jsonl files
    const targets = [];
    for (const f of files) {
      if (f.isFile() && f.name.endsWith('.jsonl')) {
        targets.push({ sid: f.name.replace(/\.jsonl$/, ''), full: path.join(projDir, f.name), parentSid: null, agentName: null });
      } else if (f.isDirectory()) {
        const subDir = path.join(projDir, f.name, 'subagents');
        if (fs.existsSync(subDir)) {
          let subs;
          try { subs = fs.readdirSync(subDir); } catch (e) { subs = []; }
          for (const sf of subs) {
            if (!sf.endsWith('.jsonl')) continue;
            // agent-<name>-<hash>.jsonl
            const m = sf.match(/^agent-(.+?)-[a-f0-9]+\.jsonl$/);
            const agentName = m ? m[1] : sf.replace(/\.jsonl$/, '');
            targets.push({ sid: f.name + ':' + sf.replace(/\.jsonl$/, ''), full: path.join(subDir, sf), parentSid: f.name, agentName });
          }
        }
      }
    }
    for (const tgt of targets) {
      const sid = tgt.sid;
      const full = tgt.full;
      let st;
      try { st = fs.statSync(full); } catch (e) { continue; }
      const prev = TRANSCRIPTS.get(sid);
      if (!prev || prev.size !== st.size || prev.mtime !== st.mtimeMs) {
        const cur = prev || {
          id: sid, cwd, projectId, path: full,
          mtime: 0, size: 0, msgCount: 0,
          lastTool: null, lastToolTarget: null, lastEventTime: 0, lastUserPrompt: '', lastAssistantSnip: '',
          tokIn: 0, tokOut: 0, cacheCreate: 0, cacheRead: 0,
          toolCounts: {}, toolLatencies: {}, toolErrors: {},
          pendingTools: {}, // tool_use_id -> {name, t0}
          parentSid: tgt.parentSid, agentName: tgt.agentName,
        };
        // Read new bytes (tail)
        if (st.size > cur.size) {
          try {
            const fd = fs.openSync(full, 'r');
            const buf = Buffer.alloc(Math.min(st.size - cur.size, 1024 * 1024));
            fs.readSync(fd, buf, 0, buf.length, cur.size);
            fs.closeSync(fd);
            const text = buf.toString('utf8');
            const lines = text.split(/\r?\n/).filter(Boolean);
            for (const ln of lines) {
              try {
                const m = JSON.parse(ln);
                cur.msgCount++;
                // CC transcript message shape: {type: 'user'|'assistant'|'system', message: {...}, ...}
                if (m.type === 'user') {
                  const content = m.message?.content;
                  if (typeof content === 'string') cur.lastUserPrompt = content.slice(0, 200);
                  else if (Array.isArray(content)) {
                    const tx = content.find(c => c.type === 'text');
                    if (tx) cur.lastUserPrompt = (tx.text || '').slice(0, 200);
                  }
                }
                if (m.type === 'assistant') {
                  const msg = m.message || {};
                  const usage = msg.usage;
                  if (usage) {
                    cur.tokIn += usage.input_tokens || 0;
                    cur.tokOut += usage.output_tokens || 0;
                    cur.cacheCreate += usage.cache_creation_input_tokens || 0;
                    cur.cacheRead += usage.cache_read_input_tokens || 0;
                  }
                  const content = msg.content;
                  if (Array.isArray(content)) {
                    for (const c of content) {
                      if (c.type === 'tool_use') {
                        cur.lastTool = c.name;
                        const inp = c.input || {};
                        cur.lastToolTarget = inp.file_path || inp.path || inp.command || inp.pattern || '';
                        cur.toolCounts[c.name] = (cur.toolCounts[c.name] || 0) + 1;
                        // track pending for latency
                        if (c.id) cur.pendingTools[c.id] = { name: c.name, t0: Date.now() };
                      } else if (c.type === 'text') {
                        cur.lastAssistantSnip = (c.text || '').slice(0, 160);
                      }
                    }
                  }
                }
                // tool_result lives under user-type messages in CC transcripts
                if (m.type === 'user') {
                  const content = m.message?.content;
                  if (Array.isArray(content)) {
                    for (const c of content) {
                      if (c.type === 'tool_result') {
                        const id = c.tool_use_id;
                        const pend = id && cur.pendingTools[id];
                        if (pend) {
                          const lat = Date.now() - pend.t0;
                          (cur.toolLatencies[pend.name] = cur.toolLatencies[pend.name] || []).push(lat);
                          // cap array at 200 to avoid bloat
                          if (cur.toolLatencies[pend.name].length > 200) cur.toolLatencies[pend.name].shift();
                          delete cur.pendingTools[id];
                          if (c.is_error) cur.toolErrors[pend.name] = (cur.toolErrors[pend.name] || 0) + 1;
                        } else if (c.is_error) {
                          // unknown tool, still count error generically
                          cur.toolErrors['_unknown'] = (cur.toolErrors['_unknown'] || 0) + 1;
                        }
                      }
                    }
                  }
                }
                cur.lastEventTime = Date.now() / 1000;
              } catch (e) { /* skip malformed line */ }
            }
          } catch (e) { /* */ }
        }
        cur.size = st.size;
        cur.mtime = st.mtimeMs;
        TRANSCRIPTS.set(sid, cur);
      }
    }
  }
  pushTranscripts();
}

// Claude Opus 4.6 pricing per 1M tokens
const PRICE = { in: 15, out: 75, cacheCreate: 18.75, cacheRead: 1.50 };
function calcCost(s) {
  return (s.tokIn * PRICE.in + s.tokOut * PRICE.out + s.cacheCreate * PRICE.cacheCreate + s.cacheRead * PRICE.cacheRead) / 1e6;
}
function percentile(arr, p) {
  if (!arr || !arr.length) return 0;
  const sorted = [...arr].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))];
}

function pushTranscripts() {
  if (!win || win.isDestroyed()) return;
  const now = Date.now() / 1000;
  const out = [];
  // aggregate maps
  const projectTotals = {}; // pid -> {tokIn,tokOut,cacheCreate,cacheRead,cost,sessions}
  const toolAgg = {};       // toolName -> {count, errors, latencies[]}
  let dailyTokIn = 0, dailyTokOut = 0, dailyCacheC = 0, dailyCacheR = 0, dailyCost = 0;
  const dayStart = new Date(); dayStart.setHours(0, 0, 0, 0);
  const dayStartSec = dayStart.getTime() / 1000;

  for (const s of TRANSCRIPTS.values()) {
    const age = now - s.lastEventTime;
    if (age > 60 * 60 * 6) continue; // 6h cutoff
    const cost = calcCost(s);
    out.push({
      id: s.id,
      cwd: s.cwd,
      project_id: s.projectId,
      msg_count: s.msgCount,
      last_tool: s.lastTool,
      last_tool_target: s.lastToolTarget,
      last_event_at: s.lastEventTime,
      last_user_prompt: s.lastUserPrompt,
      last_assistant_snip: s.lastAssistantSnip,
      active: age < 90,
      tok_in: s.tokIn, tok_out: s.tokOut,
      cache_create: s.cacheCreate, cache_read: s.cacheRead,
      cost_usd: cost,
      parent_sid: s.parentSid || null,
      agent_name: s.agentName || null,
    });
    // project aggregation
    const pid = s.projectId || '_unassigned';
    const pa = projectTotals[pid] || { tokIn: 0, tokOut: 0, cacheCreate: 0, cacheRead: 0, cost: 0, sessions: 0 };
    pa.tokIn += s.tokIn; pa.tokOut += s.tokOut; pa.cacheCreate += s.cacheCreate; pa.cacheRead += s.cacheRead;
    pa.cost += cost; pa.sessions++;
    projectTotals[pid] = pa;
    // daily (sessions touched today)
    if (s.lastEventTime >= dayStartSec) {
      dailyTokIn += s.tokIn; dailyTokOut += s.tokOut;
      dailyCacheC += s.cacheCreate; dailyCacheR += s.cacheRead;
      dailyCost += cost;
    }
    // tool aggregation
    for (const [name, cnt] of Object.entries(s.toolCounts || {})) {
      const ta = toolAgg[name] || { count: 0, errors: 0, latencies: [] };
      ta.count += cnt;
      ta.errors += s.toolErrors?.[name] || 0;
      const lats = s.toolLatencies?.[name];
      if (lats) ta.latencies = ta.latencies.concat(lats);
      toolAgg[name] = ta;
    }
  }
  out.sort((a, b) => b.last_event_at - a.last_event_at);

  // conflict detection: active sessions targeting same file within last 5min
  const fileToSessions = {};
  const ACTIVE_WINDOW = 300; // 5 min
  out.forEach(s => {
    if ((now - s.last_event_at) > ACTIVE_WINDOW) return;
    const t = s.last_tool_target;
    if (!t) return;
    // only treat as file if looks like a path
    if (!/[\\/]/.test(t)) return;
    const norm = t.toLowerCase().replace(/\\/g, '/');
    (fileToSessions[norm] = fileToSessions[norm] || []).push(s.id);
  });
  const conflicts = Object.entries(fileToSessions)
    .filter(([_, sids]) => sids.length > 1)
    .map(([file, sids]) => ({ file, sessions: sids }));

  // top tools
  const toolStats = Object.entries(toolAgg).map(([name, t]) => ({
    name, count: t.count, errors: t.errors,
    p50_ms: percentile(t.latencies, 0.5),
    p95_ms: percentile(t.latencies, 0.95),
    error_rate: t.count ? t.errors / t.count : 0,
  })).sort((a, b) => b.count - a.count);

  win.webContents.send('transcripts:data', {
    sessions: out,
    project_totals: projectTotals,
    tool_stats: toolStats,
    daily: { tok_in: dailyTokIn, tok_out: dailyTokOut, cache_create: dailyCacheC, cache_read: dailyCacheR, cost_usd: dailyCost },
    conflicts,
  });
}

// ---------------- Infra probes ----------------
// status: 'up' | 'down' | 'partial' | 'unknown'
// up: at least one common port reachable; down: all timed out/refused
const INFRA_STATUS = new Map(); // id -> { status, latency_ms, last_check, ports: {port: 'up'|'down'} }
const PROBE_PORTS_HOST = [443, 80, 22];
const PROBE_PORTS_IP = [22, 443, 80];
const PROBE_TIMEOUT = 3000;

function certProbe(host, timeout) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (res) => { if (done) return; done = true; try { sock.destroy(); } catch (e) {} resolve(res); };
    const sock = tls.connect({ host, port: 443, servername: host, rejectUnauthorized: false, timeout }, () => {
      try {
        const cert = sock.getPeerCertificate();
        if (!cert || !cert.valid_to) return finish(null);
        const exp = new Date(cert.valid_to).getTime();
        const days = Math.round((exp - Date.now()) / 86400000);
        finish({ valid_to: cert.valid_to, days_left: days, issuer: (cert.issuer && (cert.issuer.O || cert.issuer.CN)) || null });
      } catch (e) { finish(null); }
    });
    sock.once('error', () => finish(null));
    sock.once('timeout', () => finish(null));
  });
}

function tcpProbe(host, port, timeout) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const sock = new net.Socket();
    let done = false;
    const finish = (ok) => {
      if (done) return;
      done = true;
      try { sock.destroy(); } catch (e) {}
      resolve(ok ? Date.now() - t0 : null);
    };
    sock.setTimeout(timeout);
    sock.once('connect', () => finish(true));
    sock.once('timeout', () => finish(false));
    sock.once('error', () => finish(false));
    try { sock.connect(port, host); } catch (e) { finish(false); }
  });
}

async function probeNode(node) {
  const target = node.label;
  if (!target) return null;
  const ports = node.infra_kind === 'host' ? PROBE_PORTS_HOST : PROBE_PORTS_IP;
  const results = {};
  let bestLatency = null;
  let upCount = 0;
  for (const p of ports) {
    const lat = await tcpProbe(target, p, PROBE_TIMEOUT);
    if (lat !== null) {
      results[p] = lat;
      upCount++;
      if (bestLatency === null || lat < bestLatency) bestLatency = lat;
    } else {
      results[p] = null;
    }
  }
  let status = 'down';
  if (upCount === ports.length) status = 'up';
  else if (upCount > 0) status = 'partial';
  const out = { status, latency_ms: bestLatency, last_check: Date.now(), ports: results };
  // cert probe only on hosts where 443 reachable
  if (node.infra_kind === 'host' && results[443] !== null) {
    const cert = await certProbe(target, PROBE_TIMEOUT);
    if (cert) out.cert = cert;
  }
  return out;
}

let probeRunning = false;
async function probeAll() {
  if (probeRunning) return;
  probeRunning = true;
  try {
    let graph;
    try {
      const json = fs.readFileSync(path.join(MEM_DIR, 'graph.json'), 'utf8');
      graph = JSON.parse(json);
    } catch (e) { return; }
    const infraNodes = (graph.nodes || []).filter(n => n.type === 'infra');
    // run in parallel batches of 8 to avoid overwhelm
    const BATCH = 8;
    for (let i = 0; i < infraNodes.length; i += BATCH) {
      const batch = infraNodes.slice(i, i + BATCH);
      const results = await Promise.all(batch.map(n => probeNode(n).then(r => [n.id, r])));
      for (const [id, r] of results) if (r) INFRA_STATUS.set(id, r);
    }
    pushInfraToRenderer();
  } finally {
    probeRunning = false;
  }
}

function pushInfraToRenderer() {
  if (!win || win.isDestroyed()) return;
  const out = {};
  for (const [id, v] of INFRA_STATUS.entries()) out[id] = v;
  win.webContents.send('infra:data', out);
}

// ---------------- IPC ----------------
ipcMain.handle('request:initial', () => {
  pushGraphToRenderer();
  scanTranscripts();
  pushInfraToRenderer();
});

// ---------------- App lifecycle ----------------
app.whenReady().then(() => {
  createWindow();
  createTray();
  triggerBuild();
  watchMemory();
  watchActivity();
  setInterval(scanTranscripts, 1500);
  scanTranscripts();
  // run first infra probe after 5s, then every 30s
  setTimeout(probeAll, 5000);
  setInterval(probeAll, 30000);
  // health: first run after 15s, then every 60 min
  setTimeout(triggerHealth, 15000);
  setInterval(triggerHealth, 60 * 60 * 1000);
  // growth: first run after 30s, then every 4h
  setTimeout(triggerGrowth, 30000);
  setInterval(triggerGrowth, 4 * 60 * 60 * 1000);
  // signals: first run after 60s, then every 6h
  setTimeout(triggerSignals, 60000);
  setInterval(triggerSignals, 6 * 60 * 60 * 1000);
  // process watcher daemon — start after 3s
  setTimeout(startProcessWatcher, 3000);
});

app.on('window-all-closed', () => { /* keep running in tray */ });
app.on('before-quit', () => { app.isQuiting = true; stopProcessWatcher(); });
