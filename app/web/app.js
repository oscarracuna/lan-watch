const MUTE_KEY = "lan-watch-mute";
const STATUS_ORDER = { down: 0, degraded: 1, unknown: 2, up: 3 };
const hosts = new Map();
let incidents = [];
let config = {
  alert_sound: true,
  history_hours: 24,
  degraded_ms: 200,
  degraded_loss_pct: 40,
  packets: 5,
  heartbeat_ticks: 48,
};
let lastSweepTs = null;
let selectedKey = null;
let searchQuery = "";
let audioCtx = null;
let notifyEnabled = false;

const el = {
  live: document.getElementById("live"),
  summary: document.getElementById("summary"),
  sweep: document.getElementById("sweep"),
  grid: document.getElementById("grid"),
  toasts: document.getElementById("toasts"),
  mute: document.getElementById("btn-mute"),
  notify: document.getElementById("btn-notify"),
  search: document.getElementById("search"),
  incidents: document.getElementById("incidents"),
  incidentCount: document.getElementById("incident-count"),
  drawer: document.getElementById("drawer"),
  backdrop: document.getElementById("backdrop"),
  drawerName: document.getElementById("drawer-name"),
  drawerIp: document.getElementById("drawer-ip"),
  drawerStats: document.getElementById("drawer-stats"),
  spark: document.getElementById("spark"),
  drawerHint: document.getElementById("drawer-hint"),
  drawerClose: document.getElementById("drawer-close"),
};

function muted() {
  return localStorage.getItem(MUTE_KEY) === "1";
}

function setMuted(value) {
  localStorage.setItem(MUTE_KEY, value ? "1" : "0");
  syncMuteButton();
}

function syncMuteButton() {
  const isMuted = muted();
  el.mute.textContent = isMuted ? "Sound off" : "Sound on";
  el.mute.classList.toggle("active", !isMuted);
}

function displayStatus(host) {
  if (host.status !== "up") return host.status || "unknown";
  if (config.degraded_ms > 0 && host.last_rtt_ms != null && host.last_rtt_ms >= config.degraded_ms) {
    return "degraded";
  }
  if (config.degraded_loss_pct > 0 && host.loss_pct != null && host.loss_pct >= config.degraded_loss_pct) {
    return "degraded";
  }
  return "up";
}

function fmtAgo(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 5) return "just now";
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return m ? `${h}h ${m}m ago` : `${h}h ago`;
}

function fmtDuration(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

function fmtSeconds(seconds) {
  if (seconds == null) return "—";
  const s = Math.max(0, Number(seconds));
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

function fmtRtt(ms) {
  if (ms == null) return "—";
  return Number(ms).toFixed(ms >= 20 ? 0 : 1);
}

function fmtLoss(pct) {
  if (pct == null) return "—";
  return `${Math.round(Number(pct))}%`;
}

function applySnapshot(data) {
  if (data.config) config = { ...config, ...data.config };
  lastSweepTs = data.last_sweep_ts;
  if (Array.isArray(data.incidents)) incidents = data.incidents;
  hosts.clear();
  (data.hosts || []).forEach((host) => hosts.set(host.host_key, host));
  render();
}

function applySweep(data) {
  if (data.config) config = { ...config, ...data.config };
  lastSweepTs = data.last_sweep_ts;
  if (Array.isArray(data.incidents)) incidents = data.incidents;
  if (Array.isArray(data.hosts)) {
    const seen = new Set();
    data.hosts.forEach((host) => {
      seen.add(host.host_key);
      hosts.set(host.host_key, host);
    });
    for (const key of [...hosts.keys()]) {
      if (!seen.has(key)) hosts.delete(key);
    }
  }
  render();
}

function updateTitle(downCount) {
  document.title = downCount > 0 ? `${downCount} DOWN · LAN Watch` : "LAN Watch";
}

let lastGridKey = "";

function gridKey(visible) {
  return visible.map((host) => [
    host.host_key,
    displayStatus(host),
    host.last_rtt_ms,
    host.loss_pct,
    host.rtt_min_ms,
    host.rtt_max_ms,
    (host.heartbeat || []).join(""),
    host.uptime_pct,
  ].join(":")).join("|") + "::" + searchQuery;
}

function render() {
  const list = [...hosts.values()];
  const counts = { up: 0, down: 0, degraded: 0, unknown: 0 };
  list.forEach((host) => {
    counts[displayStatus(host)] = (counts[displayStatus(host)] || 0) + 1;
  });
  el.summary.innerHTML = `
    <div class="stat up"><b>${counts.up}</b><span>Up</span></div>
    <div class="stat down"><b>${counts.down}</b><span>Down</span></div>
    <div class="stat degraded"><b>${counts.degraded}</b><span>Degraded</span></div>
    <div class="stat"><b>${list.length}</b><span>Hosts</span></div>
  `;
  el.sweep.textContent = lastSweepTs ? `Last sweep: ${fmtAgo(lastSweepTs)}` : "Last sweep: —";
  updateTitle(counts.down);
  renderIncidents();

  const q = searchQuery.trim().toLowerCase();
  const visible = list
    .filter((host) => {
      if (!q) return true;
      return host.name.toLowerCase().includes(q) || host.ip.toLowerCase().includes(q);
    })
    .sort((a, b) => {
      const sa = STATUS_ORDER[displayStatus(a)] ?? 9;
      const sb = STATUS_ORDER[displayStatus(b)] ?? 9;
      if (sa !== sb) return sa - sb;
      return String(a.name).localeCompare(String(b.name));
    });

  const key = gridKey(visible);
  if (key === lastGridKey && el.grid.querySelector(".card")) {
    refreshRelativeTimes();
    return;
  }
  lastGridKey = key;

  if (!list.length) {
    el.grid.innerHTML = `<p class="empty">No hosts in config.yaml yet.</p>`;
    return;
  }
  if (!visible.length) {
    el.grid.innerHTML = `<p class="empty">No hosts match “${escapeHtml(searchQuery)}”.</p>`;
    return;
  }

  el.grid.innerHTML = visible.map((host) => {
    const status = displayStatus(host);
    const rtt = status === "down" || status === "unknown"
      ? "—"
      : `${fmtRtt(host.last_rtt_ms)}<small> ms</small>`;
    const loss = host.loss_pct == null ? "—" : `${fmtLoss(host.loss_pct)} loss`;
    const spread = host.rtt_min_ms != null && host.rtt_max_ms != null
      ? `${fmtRtt(host.rtt_min_ms)}–${fmtRtt(host.rtt_max_ms)} ms`
      : "";
    const detail = status === "down"
      ? `Down for <span data-duration="${host.last_change_ts || ""}">${fmtDuration(host.last_change_ts)}</span>`
      : `Uptime ${host.uptime_pct == null ? "—" : host.uptime_pct.toFixed(1) + "%"}`;
    const last = status === "down"
      ? `Last ok <span data-ago="${host.last_ok_ts || ""}">${fmtAgo(host.last_ok_ts)}</span>`
      : `<span data-ago="${host.last_sample_ts || ""}">${fmtAgo(host.last_sample_ts)}</span>`;
    const ticks = (host.heartbeat || []).map((tick) => `<i class="${tick}"></i>`).join("");
    return `
      <article class="card ${status}" data-key="${host.host_key}" role="button" tabindex="0">
        <div class="badge">${status}</div>
        <h3>${escapeHtml(host.name)}</h3>
        <div class="ip">${escapeHtml(host.ip)}</div>
        <div class="rtt">${rtt}</div>
        <div class="stats-row"><span>${loss}</span><span>${spread}</span></div>
        <div class="ticks" aria-hidden="true">${ticks}</div>
        <div class="meta"><span>${detail}</span><span>${last}</span></div>
      </article>
    `;
  }).join("");

  el.grid.querySelectorAll(".card").forEach((card) => {
    card.addEventListener("click", () => openDrawer(card.dataset.key));
    card.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        openDrawer(card.dataset.key);
      }
    });
  });
}

function refreshRelativeTimes() {
  el.sweep.textContent = lastSweepTs ? `Last sweep: ${fmtAgo(lastSweepTs)}` : "Last sweep: —";
  el.grid.querySelectorAll("[data-ago]").forEach((node) => {
    node.textContent = fmtAgo(node.dataset.ago ? Number(node.dataset.ago) : null);
  });
  el.grid.querySelectorAll("[data-duration]").forEach((node) => {
    node.textContent = fmtDuration(node.dataset.duration ? Number(node.dataset.duration) : null);
  });
}

let lastIncidentKey = "";

function renderIncidents() {
  const open = incidents.filter((item) => !item.up_ts);
  el.incidentCount.textContent = open.length
    ? `${open.length} open`
    : incidents.length
      ? `${incidents.length} in the last ${config.history_hours}h`
      : `None in the last ${config.history_hours}h`;
  const key = incidents.map((item) => `${item.id}:${item.up_ts || ""}:${item.down_ts}`).join("|");
  if (key === lastIncidentKey && (el.incidents.querySelector(".incident") || !incidents.length)) {
    el.incidents.querySelectorAll("[data-down]").forEach((node) => {
      const down = Number(node.dataset.down);
      const up = node.dataset.up;
      node.textContent = up
        ? `${fmtSeconds(node.dataset.duration)} · ${fmtAgo(down)}`
        : `${fmtDuration(down)} · ${fmtAgo(down)}`;
    });
    return;
  }
  lastIncidentKey = key;
  if (!incidents.length) {
    el.incidents.innerHTML = `<p class="hint">No down/up events yet. Alerts after a host has been up, then fails.</p>`;
    return;
  }
  el.incidents.innerHTML = incidents.slice(0, 12).map((item) => {
    const openNow = !item.up_ts;
    const duration = openNow
      ? fmtDuration(item.down_ts)
      : fmtSeconds(item.duration_seconds);
    const label = openNow ? "Down" : "Recovered";
    return `
      <button type="button" class="incident ${openNow ? "open" : ""}" data-key="${escapeHtml(item.host_key)}">
        <span class="badge ${openNow ? "down" : "up"}">${label}</span>
        <span class="who">${escapeHtml(item.name)}<small>${escapeHtml(item.ip)}</small></span>
        <span class="dur" data-down="${item.down_ts}" data-up="${item.up_ts || ""}" data-duration="${item.duration_seconds || ""}">${duration} · ${fmtAgo(item.down_ts)}</span>
      </button>
    `;
  }).join("");
  el.incidents.querySelectorAll(".incident").forEach((row) => {
    row.addEventListener("click", () => openDrawer(row.dataset.key));
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function ensureAudio() {
  if (!audioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    audioCtx = new Ctx();
  }
  if (audioCtx.state === "suspended") audioCtx.resume();
  return audioCtx;
}

function beep(kind) {
  if (muted() || !config.alert_sound) return;
  const ctx = ensureAudio();
  if (!ctx) return;
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = "sine";
  osc.frequency.value = kind === "down" ? 420 : 740;
  gain.gain.value = kind === "down" ? 0.07 : 0.045;
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.start();
  osc.stop(ctx.currentTime + (kind === "down" ? 0.28 : 0.12));
}

function desktopNotify(alert) {
  if (!notifyEnabled || !("Notification" in window) || Notification.permission !== "granted") return;
  const title = alert.status === "down" ? `${alert.name} is down` : `${alert.name} recovered`;
  const body = alert.status === "down" ? `${alert.ip} stopped responding` : `${alert.ip} is reachable again`;
  new Notification(title, { body, tag: `lan-watch-${alert.host_key}` });
}

function showToast(alert) {
  const toast = document.createElement("div");
  toast.className = `toast ${alert.status}`;
  toast.dataset.key = alert.host_key;
  const label = alert.status === "down"
    ? `<b>${escapeHtml(alert.name)}</b> went down`
    : `<b>${escapeHtml(alert.name)}</b> recovered`;
  toast.innerHTML = `<span>${label} <span class="ip">${escapeHtml(alert.ip)}</span></span><button type="button">Dismiss</button>`;
  toast.querySelector("button").addEventListener("click", () => toast.remove());
  el.toasts.prepend(toast);
  if (alert.status !== "down") {
    setTimeout(() => toast.remove(), 15000);
  }
}

function onAlert(alert) {
  showToast(alert);
  beep(alert.status);
  desktopNotify(alert);
}

async function openDrawer(key) {
  selectedKey = key;
  const host = hosts.get(key);
  if (!host) return;
  el.drawer.hidden = false;
  el.backdrop.hidden = false;
  el.drawerName.textContent = host.name;
  el.drawerIp.textContent = host.ip;
  el.drawerHint.textContent = "Loading history…";
  el.spark.innerHTML = "";
  try {
    const res = await fetch(`/api/history/${encodeURIComponent(key)}`);
    if (!res.ok) throw new Error("history failed");
    const data = await res.json();
    if (selectedKey !== key) return;
    const status = displayStatus(data.host);
    const loss = data.host.loss_pct == null ? "—" : fmtLoss(data.host.loss_pct);
    const spread = data.host.rtt_min_ms != null && data.host.rtt_max_ms != null
      ? `${fmtRtt(data.host.rtt_min_ms)}–${fmtRtt(data.host.rtt_max_ms)}`
      : "—";
    const pkts = data.host.packets
      ? `${data.host.packets_ok ?? "—"}/${data.host.packets}`
      : "—";
    el.drawerStats.innerHTML = `
      <div><b>${status}</b><span>Status</span></div>
      <div><b>${data.uptime_pct == null ? "—" : data.uptime_pct.toFixed(1) + "%"}</b><span>Uptime (${data.hours}h)</span></div>
      <div><b>${fmtRtt(data.host.last_rtt_ms)} ms</b><span>Median RTT</span></div>
      <div><b>${spread} ms</b><span>Min–max RTT</span></div>
      <div><b>${loss}</b><span>Loss last sweep</span></div>
      <div><b>${pkts}</b><span>Packets last sweep</span></div>
    `;
    el.spark.innerHTML = sparkline(data.samples);
    el.drawerHint.textContent = data.samples.length
      ? `RTT sparkline over the last ${data.hours} hours. Each point is one sweep (${config.packets} pings).`
      : "No samples yet — wait for a couple of ping sweeps.";
  } catch (err) {
    el.drawerHint.textContent = "Could not load history.";
  }
}

function sparkline(samples) {
  const width = 360;
  const height = 110;
  const rtts = samples.filter((s) => s.ok && s.rtt_ms != null);
  if (rtts.length < 2) {
    return `<p class="hint">Not enough successful pings for a sparkline yet.</p>`;
  }
  const values = rtts.map((s) => Number(s.rtt_ms));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const points = values.map((value, i) => {
    const x = (i / (values.length - 1)) * width;
    const y = height - 10 - ((value - min) / span) * (height - 20);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
      <polyline fill="none" stroke="#22c55e" stroke-width="2.5" points="${points}"></polyline>
    </svg>
    <p class="hint">${min.toFixed(1)}–${max.toFixed(1)} ms</p>
  `;
}

function closeDrawer() {
  selectedKey = null;
  el.drawer.hidden = true;
  el.backdrop.hidden = true;
}

function connect() {
  const source = new EventSource("/events");
  source.addEventListener("open", () => {
    el.live.textContent = "Live";
    el.live.className = "pill ok";
  });
  source.addEventListener("snapshot", (ev) => applySnapshot(JSON.parse(ev.data)));
  source.addEventListener("sweep", (ev) => applySweep(JSON.parse(ev.data)));
  source.addEventListener("update", (ev) => {
    const host = JSON.parse(ev.data);
    hosts.set(host.host_key, host);
    render();
  });
  source.addEventListener("alert", (ev) => onAlert(JSON.parse(ev.data)));
  source.addEventListener("incidents", (ev) => {
    incidents = JSON.parse(ev.data);
    renderIncidents();
  });
  source.addEventListener("notice", (ev) => {
    const data = JSON.parse(ev.data);
    showToast({ status: "up", name: "LAN Watch", ip: data.message, host_key: "notice" });
  });
  source.onerror = () => {
    el.live.textContent = "Reconnecting";
    el.live.className = "pill bad";
  };
}

el.mute.addEventListener("click", () => {
  ensureAudio();
  setMuted(!muted());
});
el.notify.addEventListener("click", async () => {
  if (!("Notification" in window)) {
    el.notify.textContent = "Notifications unsupported";
    return;
  }
  const permission = await Notification.requestPermission();
  notifyEnabled = permission === "granted";
  el.notify.textContent = notifyEnabled ? "Notifications on" : "Notifications blocked";
  el.notify.classList.toggle("active", notifyEnabled);
});
el.search.addEventListener("input", () => {
  searchQuery = el.search.value;
  render();
});
el.drawerClose.addEventListener("click", closeDrawer);
el.backdrop.addEventListener("click", closeDrawer);
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closeDrawer();
});

if ("Notification" in window && Notification.permission === "granted") {
  notifyEnabled = true;
  el.notify.textContent = "Notifications on";
  el.notify.classList.add("active");
}

syncMuteButton();
connect();
setInterval(() => {
  if (hosts.size) render();
}, 1000);
