from __future__ import annotations

import asyncio
import platform
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from app.config import Settings, config_mtime, load_config
from app.notify import TelegramNotifier
from app.store import Store

RTT_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)
PING_CONCURRENCY = 32
PERSIST_SKIP = {
    "uptime_pct",
    "heartbeat",
    "loss_pct",
    "rtt_min_ms",
    "rtt_max_ms",
    "packets_ok",
    "packets",
    "role",
    "fail_threshold",
    "recover_threshold",
    "degraded_ms",
    "degraded_loss_pct",
}


@dataclass
class HostState:
    host_key: str
    name: str
    ip: str
    role: str = "gateway"
    fail_threshold: int = 3
    recover_threshold: int = 2
    degraded_ms: float = 200.0
    degraded_loss_pct: float = 40.0
    status: str = "unknown"
    consecutive_ok: int = 0
    consecutive_fail: int = 0
    last_rtt_ms: float | None = None
    last_ok_ts: float | None = None
    last_fail_ts: float | None = None
    last_change_ts: float | None = None
    last_sample_ts: float | None = None
    uptime_pct: float | None = None
    loss_pct: float | None = None
    rtt_min_ms: float | None = None
    rtt_max_ms: float | None = None
    packets_ok: int | None = None
    packets: int | None = None
    heartbeat: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def persist_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.as_dict().items() if key not in PERSIST_SKIP}


class Hub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: str, data: Any) -> None:
        payload = {"event": event, "data": data}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except Exception:
                    self._subscribers.discard(queue)


def ping_command(ip: str, timeout: float) -> list[str]:
    system = platform.system()
    if system == "Windows":
        return ["ping", "-n", "1", "-w", str(max(1, int(timeout * 1000))), ip]
    if system == "Darwin":
        return ["ping", "-c", "1", "-W", str(max(1, int(timeout * 1000))), ip]
    return ["ping", "-c", "1", "-W", str(timeout), ip]


def ping_once_sync(ip: str, timeout: float) -> tuple[bool, float | None]:
    cmd = ping_command(ip, timeout)
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "timeout": timeout + 2,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = subprocess.run(cmd, **kwargs)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False, None
    output = completed.stdout or ""
    match = RTT_RE.search(output)
    ttl_ok = "ttl=" in output.lower()
    if match and (completed.returncode == 0 or ttl_ok):
        return True, float(match.group(1))
    return False, None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _tick_from_result(ok: bool, loss_pct: float) -> str:
    if not ok:
        return "fail"
    if loss_pct > 0:
        return "loss"
    return "ok"


class PingEngine:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        hub: Hub,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.hub = hub
        self.notifier = notifier or TelegramNotifier()
        self.states: dict[str, HostState] = {}
        self.last_sweep_ts: float | None = None
        self._stop = asyncio.Event()
        self._mtime = config_mtime()
        self._last_prune = 0.0
        self._apply_hosts(settings, restore=True)

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, Any]:
        return {
            "hosts": [state.as_dict() for state in self._sorted_states()],
            "last_sweep_ts": self.last_sweep_ts,
            "config": self.settings.public_dict(),
            "incidents": self._incidents(),
        }

    def _incidents(self) -> list[dict[str, Any]]:
        since = time.time() - self.settings.history_hours * 3600
        return self.store.incidents(since)

    def _sorted_states(self) -> list[HostState]:
        order = {host.ip: index for index, host in enumerate(self.settings.hosts)}
        return sorted(
            self.states.values(),
            key=lambda s: (
                0 if s.role == "gateway" else 1,
                order.get(s.ip, 9999),
                s.name,
            ),
        )

    def _sync_host_meta(self, state: HostState, host: Any) -> None:
        state.name = host.name
        state.role = host.role
        state.fail_threshold = host.fail_threshold
        state.recover_threshold = host.recover_threshold
        state.degraded_ms = host.degraded_ms
        state.degraded_loss_pct = host.degraded_loss_pct

    def _apply_hosts(self, settings: Settings, restore: bool = False) -> None:
        saved = self.store.load_states() if restore else {}
        wanted = {host.ip: host for host in settings.hosts}
        for key in list(self.states):
            if key not in wanted:
                del self.states[key]
        ticks = settings.heartbeat_ticks
        for ip, host in wanted.items():
            if ip in self.states:
                self._sync_host_meta(self.states[ip], host)
                self.states[ip].heartbeat = self.states[ip].heartbeat[-ticks:]
                continue
            row = saved.get(ip)
            if row:
                state = HostState(
                    host_key=ip,
                    name=host.name,
                    ip=ip,
                    status=str(row.get("status") or "unknown"),
                    consecutive_ok=int(row.get("consecutive_ok") or 0),
                    consecutive_fail=int(row.get("consecutive_fail") or 0),
                    last_rtt_ms=row.get("last_rtt_ms"),
                    last_ok_ts=row.get("last_ok_ts"),
                    last_fail_ts=row.get("last_fail_ts"),
                    last_change_ts=row.get("last_change_ts"),
                    last_sample_ts=row.get("last_sample_ts"),
                    heartbeat=self.store.recent_ticks(ip, ticks),
                )
                self._sync_host_meta(state, host)
                self.states[ip] = state
                if state.status == "down":
                    self.store.open_incident(
                        ip, host.name, ip, float(state.last_change_ts or time.time())
                    )
            else:
                state = HostState(host_key=ip, name=host.name, ip=ip)
                self._sync_host_meta(state, host)
                self.states[ip] = state

    def _maybe_reload_config(self) -> None:
        mtime = config_mtime()
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            settings = load_config()
        except Exception:
            return
        bind_changed = (
            settings.listen != self.settings.listen or settings.port != self.settings.port
        )
        self.settings = settings
        self._apply_hosts(settings, restore=False)
        self._refresh_uptimes()
        self.hub.publish("snapshot", self.snapshot())
        if bind_changed:
            self.hub.publish(
                "notice",
                {
                    "message": "Listen address or port changed; restart the service to apply.",
                    "ts": time.time(),
                },
            )

    def _refresh_uptimes(self) -> None:
        since = time.time() - self.settings.history_hours * 3600
        uptimes = self.store.uptimes(since)
        for key, state in self.states.items():
            state.uptime_pct = uptimes.get(key)

    def _apply_sample(
        self,
        state: HostState,
        ok: bool,
        rtt_ms: float | None,
        ts: float,
        *,
        loss_pct: float,
        rtt_min_ms: float | None,
        rtt_max_ms: float | None,
        packets_ok: int,
        packets: int,
    ) -> dict[str, Any] | None:
        previous = state.status
        state.last_sample_ts = ts
        state.loss_pct = loss_pct
        state.rtt_min_ms = rtt_min_ms
        state.rtt_max_ms = rtt_max_ms
        state.packets_ok = packets_ok
        state.packets = packets
        if ok:
            state.consecutive_ok += 1
            state.consecutive_fail = 0
            state.last_rtt_ms = rtt_ms
            state.last_ok_ts = ts
            if state.status != "up" and state.consecutive_ok >= state.recover_threshold:
                state.status = "up"
                state.last_change_ts = ts
        else:
            state.consecutive_fail += 1
            state.consecutive_ok = 0
            state.last_fail_ts = ts
            if state.status != "down" and state.consecutive_fail >= state.fail_threshold:
                state.status = "down"
                state.last_change_ts = ts

        state.heartbeat.append(_tick_from_result(ok, loss_pct))
        state.heartbeat = state.heartbeat[-self.settings.heartbeat_ticks :]

        self.store.add_sample(
            state.host_key,
            ts,
            ok,
            rtt_ms,
            loss_pct=loss_pct,
            rtt_min_ms=rtt_min_ms,
            rtt_max_ms=rtt_max_ms,
            packets_ok=packets_ok,
            packets=packets,
        )
        self.store.upsert_state(state.persist_dict())

        if (
            state.status != previous
            and previous in {"up", "down"}
            and state.status in {"up", "down"}
        ):
            if state.status == "down":
                self.store.open_incident(state.host_key, state.name, state.ip, ts)
            else:
                self.store.close_incident(state.host_key, ts)
            return {
                "host_key": state.host_key,
                "name": state.name,
                "ip": state.ip,
                "status": state.status,
                "previous": previous,
                "ts": ts,
                "rtt_ms": state.last_rtt_ms,
                "loss_pct": loss_pct,
            }
        return None

    async def _ping_host(self, state: HostState, semaphore: asyncio.Semaphore) -> None:
        packets = self.settings.packets
        results: list[tuple[bool, float | None]] = []
        for _ in range(packets):
            async with semaphore:
                results.append(
                    await asyncio.to_thread(
                        ping_once_sync, state.ip, self.settings.timeout_seconds
                    )
                )
        rtts = [rtt for ok, rtt in results if ok and rtt is not None]
        packets_ok = sum(1 for ok, _ in results if ok)
        loss_pct = 100.0 * (packets - packets_ok) / packets if packets else 100.0
        sweep_ok = packets_ok > 0
        ts = time.time()
        alert = self._apply_sample(
            state,
            sweep_ok,
            _median(rtts),
            ts,
            loss_pct=loss_pct,
            rtt_min_ms=min(rtts) if rtts else None,
            rtt_max_ms=max(rtts) if rtts else None,
            packets_ok=packets_ok,
            packets=packets,
        )
        self.hub.publish("update", state.as_dict())
        if alert:
            self.hub.publish("alert", alert)
            self.hub.publish("incidents", self._incidents())
            asyncio.create_task(self.notifier.notify(alert, self.settings))

    async def run(self) -> None:
        while not self._stop.is_set():
            cycle_start = time.monotonic()
            self._maybe_reload_config()
            hosts = list(self.states.values())
            semaphore = asyncio.Semaphore(PING_CONCURRENCY)
            if hosts:
                await asyncio.gather(
                    *(self._ping_host(state, semaphore) for state in hosts),
                    return_exceptions=True,
                )
            self.last_sweep_ts = time.time()
            now = time.time()
            if now - self._last_prune >= 60:
                self.store.prune(self.settings.history_hours)
                self._last_prune = now
            self._refresh_uptimes()
            self.hub.publish(
                "sweep",
                {
                    "last_sweep_ts": self.last_sweep_ts,
                    "hosts": [state.as_dict() for state in self._sorted_states()],
                    "config": self.settings.public_dict(),
                    "incidents": self._incidents(),
                },
            )
            elapsed = time.monotonic() - cycle_start
            wait = max(0.2, self.settings.interval_seconds - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
