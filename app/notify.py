from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from app.config import Settings, secrets_file

log = logging.getLogger("uvicorn.error")
TELEGRAM_LIMIT = 4096
STATUS_ORDER = {"down": 0, "degraded": 1, "unknown": 2, "up": 3}


def load_secrets() -> None:
    path = secrets_file()
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path, override=False)


def _clean(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def _same_chat(got: Any, expected: str) -> bool:
    return str(got).strip() == str(expected).strip()


def _command(text: str) -> str:
    first = (text or "").strip().split()[0] if text else ""
    return first.split("@", 1)[0].lower()


def _args(text: str) -> str:
    parts = (text or "").strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _fit(text: str) -> str:
    if len(text) <= TELEGRAM_LIMIT:
        return text
    return text[: TELEGRAM_LIMIT - 1] + "…"


def _ago(ts: float | None) -> str:
    if ts is None:
        return "—"
    age = max(0, int(time.time() - float(ts)))
    if age < 60:
        return f"{age}s ago"
    if age < 3600:
        return f"{age // 60}m ago"
    hours, minutes = divmod(age, 3600)
    if hours < 48:
        return f"{hours}h {minutes // 60}m ago" if minutes >= 60 else f"{hours}h ago"
    return f"{age // 86400}d ago"


def _span(seconds: float) -> str:
    age = max(0, int(seconds))
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    hours, minutes = divmod(age, 3600)
    if hours < 48:
        return f"{hours}h {minutes // 60}m" if minutes >= 60 else f"{hours}h"
    return f"{age // 86400}d"


def find_hosts(hosts: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q = query.strip().lower()
    if not q:
        return []
    exact = [
        host
        for host in hosts
        if str(host.get("ip") or "").lower() == q
        or str(host.get("name") or "").lower() == q
        or str(host.get("location") or "").lower() == q
    ]
    if exact:
        return exact
    starts = [host for host in hosts if str(host.get("name") or "").lower().startswith(q)]
    if starts:
        return starts
    return [
        host
        for host in hosts
        if q in str(host.get("name") or "").lower()
        or q in str(host.get("ip") or "").lower()
        or q in str(host.get("location") or "").lower()
    ]


def _degraded_ms(host: dict[str, Any], cfg: dict[str, Any]) -> float:
    if host.get("degraded_ms") is not None:
        return float(host["degraded_ms"])
    key = "endpoint_degraded_ms" if str(host.get("role") or "gateway") == "endpoint" else "degraded_ms"
    default = 500.0 if key == "endpoint_degraded_ms" else 200.0
    try:
        return float(cfg.get(key) if cfg.get(key) is not None else default)
    except (TypeError, ValueError):
        return default


def _degraded_loss(host: dict[str, Any], cfg: dict[str, Any]) -> float:
    if host.get("degraded_loss_pct") is not None:
        return float(host["degraded_loss_pct"])
    key = (
        "endpoint_degraded_loss_pct"
        if str(host.get("role") or "gateway") == "endpoint"
        else "degraded_loss_pct"
    )
    default = 70.0 if key == "endpoint_degraded_loss_pct" else 40.0
    try:
        return float(cfg.get(key) if cfg.get(key) is not None else default)
    except (TypeError, ValueError):
        return default


def _display_status(host: dict[str, Any], cfg: dict[str, Any]) -> str:
    status = str(host.get("status") or "unknown")
    if status != "up":
        return status or "unknown"
    degraded_ms = _degraded_ms(host, cfg)
    degraded_loss = _degraded_loss(host, cfg)
    rtt = host.get("last_rtt_ms")
    loss = host.get("loss_pct")
    if degraded_ms > 0 and rtt is not None and float(rtt) >= degraded_ms:
        return "degraded"
    if degraded_loss > 0 and loss is not None and float(loss) >= degraded_loss:
        return "degraded"
    return "up"


def _fmt_rtt(ms: float) -> str:
    if ms < 1:
        return f"{ms:.2f}ms"
    if ms < 10:
        return f"{ms:.1f}ms"
    return f"{ms:.0f}ms"


def _host_line(host: dict[str, Any], kind: str) -> str:
    name = _clean(str(host.get("name") or "host"))
    location = _clean(str(host.get("location") or ""))
    ip = _clean(str(host.get("ip") or ""))
    extra: list[str] = []
    rtt = host.get("last_rtt_ms")
    loss = host.get("loss_pct")
    if kind in {"up", "degraded"} and rtt is not None:
        extra.append(_fmt_rtt(float(rtt)))
    if kind == "degraded" and loss is not None and float(loss) > 0:
        extra.append(f"{float(loss):.0f}% loss")
    bits = [part for part in (name, location, ip, *extra) if part]
    return "  " + "  ".join(bits)


def _status_block(labeled: list[tuple[str, dict[str, Any]]]) -> str:
    labeled = sorted(
        labeled,
        key=lambda item: (
            STATUS_ORDER.get(item[0], 9),
            str(item[1].get("name") or "").lower(),
        ),
    )
    counts = {"down": 0, "degraded": 0, "unknown": 0, "up": 0}
    for kind, _ in labeled:
        counts[kind] = counts.get(kind, 0) + 1
    if counts["down"] == 0 and counts["degraded"] == 0 and counts["unknown"] == 0:
        return "\n".join(_host_line(host, "up") for _, host in labeled)
    sections: list[str] = []
    for kind, title in (
        ("down", "DOWN"),
        ("degraded", "DEGRADED"),
        ("unknown", "WAITING"),
        ("up", "UP"),
    ):
        rows = [_host_line(host, kind) for item_kind, host in labeled if item_kind == kind]
        if rows:
            sections.append(title + "\n" + "\n".join(rows))
    return "\n\n".join(sections)


def format_status_message(snapshot: dict[str, Any]) -> str:
    hosts = list(snapshot.get("hosts") or [])
    cfg = dict(snapshot.get("config") or {})
    if not hosts:
        return "LAN Watch: no hosts configured."

    labeled = [(_display_status(host, cfg), host) for host in hosts]
    counts = {"down": 0, "degraded": 0, "unknown": 0, "up": 0}
    for kind, _ in labeled:
        counts[kind] = counts.get(kind, 0) + 1

    parts = [
        f"{counts['down']} down",
        f"{counts['degraded']} degraded",
        f"{counts['up']} up",
    ]
    if counts["unknown"]:
        parts.insert(2, f"{counts['unknown']} waiting")
    header = f"LAN Watch: {len(labeled)} hosts · " + " · ".join(parts)
    sweep = snapshot.get("last_sweep_ts")
    if sweep:
        age = max(0, int(time.time() - float(sweep)))
        header += f"\nUpdated {age}s ago"

    gateways = []
    timeclocks = []
    endpoints = []
    for item in labeled:
        role = str(item[1].get("role") or "gateway")
        if role == "timeclock":
            timeclocks.append(item)
        elif role == "endpoint":
            endpoints.append(item)
        else:
            gateways.append(item)
    chunks = [header]
    if gateways:
        chunks.append("GATEWAYS\n" + _status_block(gateways))
    if timeclocks:
        chunks.append("TERMINALS\n" + _status_block(timeclocks))
    if endpoints:
        chunks.append("ENDPOINTS\n" + _status_block(endpoints))
    return _fit("\n\n".join(chunks))


def _incident_duration(item: dict[str, Any]) -> float:
    down_ts = float(item.get("down_ts") or 0)
    up_ts = item.get("up_ts")
    duration = item.get("duration_seconds")
    if duration is not None:
        return max(0.0, float(duration))
    end = float(up_ts) if up_ts is not None else time.time()
    return max(0.0, end - down_ts)


def format_incidents_message(snapshot: dict[str, Any]) -> str:
    cfg = dict(snapshot.get("config") or {})
    try:
        hours_n = float(cfg.get("history_hours") or 24)
    except (TypeError, ValueError):
        hours_n = 24.0
    incidents = list(snapshot.get("incidents") or [])
    if not incidents:
        return f"LAN Watch: no incidents in the last {hours_n:g}h."

    open_ones = [item for item in incidents if item.get("up_ts") is None]
    closed = [item for item in incidents if item.get("up_ts") is not None]
    header = (
        f"LAN Watch incidents ({hours_n:g}h): {len(incidents)}"
        f" · {len(open_ones)} still down"
    )

    def _line(item: dict[str, Any], open_now: bool) -> str:
        name = _clean(str(item.get("name") or "host"))
        ip = _clean(str(item.get("ip") or ""))
        lasted = _span(_incident_duration(item))
        if open_now:
            return f"  {name}  {ip}  {lasted} (still down)"
        return f"  {name}  {ip}  {lasted}  ended {_ago(item.get('up_ts'))}"

    sections: list[str] = [header]
    if open_ones:
        sections.append("STILL DOWN\n" + "\n".join(_line(item, True) for item in open_ones))
    if closed:
        sections.append("CLOSED\n" + "\n".join(_line(item, False) for item in closed))
    return _fit("\n\n".join(sections))


def format_host_message(snapshot: dict[str, Any], query: str) -> str:
    hosts = list(snapshot.get("hosts") or [])
    cfg = dict(snapshot.get("config") or {})
    try:
        hours_n = float(cfg.get("history_hours") or 24)
    except (TypeError, ValueError):
        hours_n = 24.0
    if not query:
        return "Usage: /host NAS or /host 192.168.1.10"
    matches = find_hosts(hosts, query)
    if not matches:
        return f"Unknown host {query}. Send /status for the list."
    if len(matches) > 1:
        lines = [_host_line(host, _display_status(host, cfg)).strip() for host in matches]
        return _fit(
            "Multiple hosts matched. Be more specific:\n"
            + "\n".join(f"  {line}" for line in lines)
        )

    host = matches[0]
    kind = _display_status(host, cfg)
    role = str(host.get("role") or "gateway").upper()
    name = _clean(str(host.get("name") or "host"))
    ip = _clean(str(host.get("ip") or ""))
    location = _clean(str(host.get("location") or ""))
    lines = [f"{name}  {ip}", f"{role} · {kind.upper()}"]
    if location:
        lines.insert(1, location)
    rtt = host.get("last_rtt_ms")
    rtt_min = host.get("rtt_min_ms")
    rtt_max = host.get("rtt_max_ms")
    loss = host.get("loss_pct")
    packets = host.get("packets")
    packets_ok = host.get("packets_ok")
    if rtt is not None:
        spread = ""
        if rtt_min is not None and rtt_max is not None:
            spread = f"  ({_fmt_rtt(float(rtt_min))}–{_fmt_rtt(float(rtt_max))})"
        lines.append(f"RTT {_fmt_rtt(float(rtt))}{spread}")
    if loss is not None:
        extra = ""
        if packets_ok is not None and packets is not None:
            extra = f"  ({packets_ok}/{packets} replies)"
        lines.append(f"Loss {float(loss):.0f}%{extra}")
    uptime = host.get("uptime_pct")
    if uptime is not None:
        lines.append(f"Uptime {float(uptime):.1f}% ({hours_n:g}h)")
    lines.append(f"Last ok  {_ago(host.get('last_ok_ts'))}")
    lines.append(f"Last fail  {_ago(host.get('last_fail_ts'))}")
    lines.append(f"Last change  {_ago(host.get('last_change_ts'))}")

    key = str(host.get("host_key") or host.get("ip") or "")
    related = [
        item
        for item in snapshot.get("incidents") or []
        if str(item.get("host_key") or "") == key or str(item.get("ip") or "") == ip
    ]
    if related:
        rows = []
        for item in related[:8]:
            lasted = _span(_incident_duration(item))
            if item.get("up_ts") is None:
                rows.append(f"  still down  {lasted}")
            else:
                rows.append(f"  {lasted}  ended {_ago(item.get('up_ts'))}")
        lines.append("Incidents")
        lines.extend(rows)
    return _fit("\n".join(lines))


def telegram_api(
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Telegram HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Telegram network error: {exc.reason}") from exc
    parsed = json.loads(body)
    if not parsed.get("ok"):
        raise RuntimeError(f"Telegram rejected {method}: {parsed.get('description')}")
    return parsed


class TelegramNotifier:
    def __init__(self) -> None:
        self._last: dict[str, tuple[str, float]] = {}
        self._warned_missing = False
        self._stop = asyncio.Event()
        self._offset = 0
        self._commands_ready = False

    def stop(self) -> None:
        self._stop.set()

    def _credentials(self) -> tuple[str, str] | None:
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        if not token or not chat_id:
            return None
        return token, chat_id

    def ready(self, settings: Settings) -> bool:
        if not settings.telegram_enabled:
            return False
        creds = self._credentials()
        if creds:
            return True
        if not self._warned_missing:
            log.warning(
                "Telegram alerts are enabled but TELEGRAM_BOT_TOKEN or "
                "TELEGRAM_CHAT_ID is empty. Edit %s (mode 600) and restart.",
                secrets_file(),
            )
            self._warned_missing = True
        return False

    def _allowed(self, alert: dict[str, Any], settings: Settings) -> bool:
        status = str(alert.get("status") or "")
        if status == "up" and not settings.alert_on_recovery:
            return False
        if status not in {"up", "down"}:
            return False
        key = str(alert.get("host_key") or "")
        now = time.time()
        last = self._last.get(key)
        if last and last[0] == status and (now - last[1]) < settings.alert_cooldown_seconds:
            log.info("Telegram cooldown skip for %s (%s)", key, status)
            return False
        return True

    def _text(self, alert: dict[str, Any]) -> str:
        name = _clean(str(alert.get("name") or "host"))
        ip = _clean(str(alert.get("ip") or ""))
        if alert.get("status") == "down":
            line = f"LAN Watch: {name} is DOWN"
        else:
            line = f"LAN Watch: {name} recovered"
        return f"{line}\n{ip}" if ip else line

    def send_sync(self, text: str, token: str, chat_id: str) -> None:
        telegram_api(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )

    async def notify(self, alert: dict[str, Any], settings: Settings) -> None:
        if not self.ready(settings) or not self._allowed(alert, settings):
            return
        creds = self._credentials()
        if not creds:
            return
        token, chat_id = creds
        text = self._text(alert)
        try:
            await asyncio.to_thread(self.send_sync, text, token, chat_id)
        except Exception:
            log.exception("Telegram send failed")
            return
        self._last[str(alert.get("host_key") or "")] = (str(alert.get("status")), time.time())
        log.info("Telegram sent %s for %s", alert.get("status"), alert.get("name"))

    def _prepare_commands(self, token: str) -> None:
        if self._commands_ready:
            return
        telegram_api(token, "deleteWebhook", {"drop_pending_updates": False})
        telegram_api(
            token,
            "setMyCommands",
            {
                "commands": [
                    {"command": "status", "description": "LAN health overview"},
                    {"command": "incidents", "description": "Outages in the history window"},
                    {"command": "host", "description": "One host by name or IP"},
                    {"command": "start", "description": "What this bot can do"},
                ]
            },
        )
        offset = 0
        for _ in range(20):
            parsed = telegram_api(
                token,
                "getUpdates",
                {"offset": offset, "timeout": 0, "allowed_updates": ["message"]},
                timeout=10,
            )
            results = list(parsed.get("result") or [])
            if not results:
                break
            offset = int(results[-1].get("update_id") or 0) + 1
        self._offset = offset
        self._commands_ready = True
        log.info("Telegram commands enabled")

    def _handle_message(
        self,
        update: dict[str, Any],
        expected_chat: str,
        snapshot: Callable[[], dict[str, Any]],
        token: str,
    ) -> None:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or not _same_chat(chat_id, expected_chat):
            if chat_id is not None:
                log.info("Ignored Telegram command from chat %s", chat_id)
            return
        raw = str(message.get("text") or "")
        cmd = _command(raw)
        data = snapshot()
        if cmd in {"/start", "/help"}:
            text = (
                "LAN Watch commands:\n"
                "/status — down-first health overview\n"
                "/incidents — outages in the history window\n"
                "/host NAME — one host (name or IP)"
            )
        elif cmd == "/status":
            text = format_status_message(data)
        elif cmd == "/incidents":
            text = format_incidents_message(data)
        elif cmd == "/host":
            text = format_host_message(data, _args(raw))
        else:
            return
        self.send_sync(text, token, str(chat_id))
        log.info("Telegram command %s", cmd)

    async def poll_commands(self, engine: Any) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            settings = engine.settings
            if not settings.telegram_commands or not self.ready(settings):
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                continue
            creds = self._credentials()
            if not creds:
                continue
            token, chat_id = creds
            try:
                if not self._commands_ready:
                    await asyncio.to_thread(self._prepare_commands, token)
                parsed = await asyncio.to_thread(
                    telegram_api,
                    token,
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": 20,
                        "allowed_updates": ["message"],
                    },
                    30,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Telegram getUpdates failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
                continue
            for update in parsed.get("result") or []:
                self._offset = int(update.get("update_id") or 0) + 1
                try:
                    await asyncio.to_thread(
                        self._handle_message,
                        update,
                        chat_id,
                        engine.snapshot,
                        token,
                    )
                except Exception:
                    log.exception("Telegram command handling failed")
