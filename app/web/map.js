const STATUS_COLOR = {
  up: "#22c55e",
  down: "#ef4444",
  degraded: "#f59e0b",
  unknown: "#64748b",
};

const hosts = new Map();
const markers = new Map();
let positions = new Map();
let map;
let config = { degraded_ms: 200, degraded_loss_pct: 40 };

const el = {
  live: document.getElementById("live"),
  unplaced: document.getElementById("unplaced"),
  placed: document.getElementById("placed"),
};

function displayStatus(host) {
  if (host.status !== "up") return host.status || "unknown";
  const degradedMs = host.degraded_ms != null ? Number(host.degraded_ms) : Number(config.degraded_ms || 0);
  const degradedLoss = host.degraded_loss_pct != null
    ? Number(host.degraded_loss_pct)
    : Number(config.degraded_loss_pct || 0);
  if (degradedMs > 0 && host.last_rtt_ms != null && host.last_rtt_ms >= degradedMs) return "degraded";
  if (degradedLoss > 0 && host.loss_pct != null && host.loss_pct >= degradedLoss) return "degraded";
  return "up";
}

function pinIcon(status) {
  const color = STATUS_COLOR[status] || STATUS_COLOR.unknown;
  return L.divIcon({
    className: "map-pin",
    html: `<span style="background:${color}"></span>`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
}

function applyHosts(list, nextConfig) {
  if (nextConfig) config = { ...config, ...nextConfig };
  hosts.clear();
  (list || []).forEach((host) => {
    if (host.role === "gateway") hosts.set(host.host_key, host);
  });
  syncMarkers();
  renderLists();
}

function syncMarkers() {
  for (const key of [...markers.keys()]) {
    if (!hosts.has(key) || !positions.has(key)) {
      markers.get(key).remove();
      markers.delete(key);
    }
  }
  for (const [key, pin] of positions) {
    const host = hosts.get(key);
    if (!host) continue;
    const status = displayStatus(host);
    let marker = markers.get(key);
    if (!marker) {
      marker = L.marker([pin.lat, pin.lng], { icon: pinIcon(status), draggable: true, title: host.name });
      marker.on("dragend", () => onDrag(key, marker));
      marker.bindPopup("");
      marker.on("popupopen", () => marker.setPopupContent(popupHtml(key)));
      marker.addTo(map);
      markers.set(key, marker);
    } else {
      marker.setLatLng([pin.lat, pin.lng]);
      marker.setIcon(pinIcon(status));
    }
  }
}

function popupHtml(key) {
  const host = hosts.get(key);
  if (!host) return "";
  const status = displayStatus(host);
  const root = document.createElement("div");
  root.className = "map-popup";
  root.innerHTML = `<b>${escapeHtml(host.name)}</b><div class="mono">${escapeHtml(host.ip)}</div><div class="badge ${status}">${status}</div>`;
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "Remove from map";
  button.addEventListener("click", () => unplace(key));
  root.appendChild(button);
  return root;
}

function renderLists() {
  const unplaced = [];
  const placed = [];
  for (const host of hosts.values()) {
    (positions.has(host.host_key) ? placed : unplaced).push(host);
  }
  const byName = (a, b) => String(a.name).localeCompare(String(b.name));
  unplaced.sort(byName);
  placed.sort(byName);
  el.unplaced.innerHTML = unplaced.length
    ? `<h3>Not on the map</h3>${unplaced.map((host) => rowHtml(host, false)).join("")}`
    : `<p class="hint">Every gateway is on the map.</p>`;
  el.placed.innerHTML = placed.length
    ? `<h3>On the map</h3>${placed.map((host) => rowHtml(host, true)).join("")}`
    : "";
  el.unplaced.querySelectorAll("[data-place]").forEach((button) => {
    button.addEventListener("click", () => placeAtCenter(button.dataset.place));
  });
  el.placed.querySelectorAll("[data-focus]").forEach((button) => {
    button.addEventListener("click", () => focusPin(button.dataset.focus));
  });
}

function rowHtml(host, onMap) {
  const status = displayStatus(host);
  const action = onMap
    ? `<button type="button" data-focus="${escapeHtml(host.host_key)}">Show</button>`
    : `<button type="button" data-place="${escapeHtml(host.host_key)}">Place</button>`;
  return `
    <div class="map-row">
      <span class="dot ${status}"></span>
      <span><b>${escapeHtml(host.name)}</b><small>${escapeHtml(host.ip)}</small></span>
      ${action}
    </div>
  `;
}

async function placeAtCenter(key) {
  const center = map.getCenter();
  const jitter = (hosts.size % 5) * 0.004;
  await savePin(key, center.lat + jitter, center.lng + jitter);
}

function focusPin(key) {
  const marker = markers.get(key);
  if (!marker) return;
  map.setView(marker.getLatLng(), Math.max(map.getZoom(), 13));
  marker.openPopup();
}

async function onDrag(key, marker) {
  const latlng = marker.getLatLng();
  const ok = await savePin(key, latlng.lat, latlng.lng);
  if (!ok) {
    const previous = positions.get(key);
    if (previous) marker.setLatLng([previous.lat, previous.lng]);
  }
}

async function savePin(key, lat, lng) {
  const res = await fetch(`/api/map/${encodeURIComponent(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lat, lng }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    window.alert(body.detail || "Could not save that pin.");
    return false;
  }
  const data = await res.json();
  positions.set(key, data.pin);
  syncMarkers();
  renderLists();
  return true;
}

async function unplace(key) {
  const res = await fetch(`/api/map/${encodeURIComponent(key)}`, { method: "DELETE" });
  if (!res.ok) return;
  positions.delete(key);
  map.closePopup();
  syncMarkers();
  renderLists();
}

async function loadPositions() {
  const res = await fetch("/api/map");
  if (!res.ok) throw new Error("map failed");
  const data = await res.json();
  positions = new Map();
  (data.gateways || []).forEach((host) => {
    hosts.set(host.host_key, host);
    if (host.pin) positions.set(host.host_key, host.pin);
  });
  if (!map) {
    map = L.map("city-map", { zoomControl: true }).setView(
      [data.center.lat, data.center.lng],
      data.zoom || 12,
    );
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19,
    }).addTo(map);
    setTimeout(() => map.invalidateSize(), 0);
  }
  syncMarkers();
  renderLists();
}

function connect() {
  const source = new EventSource("/events");
  source.addEventListener("open", () => {
    el.live.textContent = "Live";
    el.live.className = "pill ok";
  });
  source.addEventListener("snapshot", (ev) => {
    const data = JSON.parse(ev.data);
    applyHosts(data.hosts, data.config);
  });
  source.addEventListener("sweep", (ev) => {
    const data = JSON.parse(ev.data);
    applyHosts(data.hosts, data.config);
  });
  source.addEventListener("update", (ev) => {
    const host = JSON.parse(ev.data);
    if (host.role !== "gateway") return;
    hosts.set(host.host_key, host);
    syncMarkers();
    renderLists();
  });
  source.onerror = () => {
    el.live.textContent = "Reconnecting";
    el.live.className = "pill bad";
  };
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

loadPositions().then(connect).catch(() => {
  el.live.textContent = "Map unavailable";
  el.live.className = "pill bad";
});
