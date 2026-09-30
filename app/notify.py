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


def _display_status(host: dict[str, Any], cfg: dict[str, Any]) -> str:
    status = str(host.get("status") or "unknown")
    if status != "up":
        return status or "unknown"
    degraded_ms = float(cfg.get("degraded_ms") or 0)
    degraded_loss = float(cfg.get("degraded_loss_pct") or 0)
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
    ip = _clean(str(host.get("ip") or ""))
    extra: list[str] = []
    rtt = host.get("last_rtt_ms")
    loss = host.get("loss_pct")
    if kind in {"up", "degraded"} and rtt is not None:
        extra.append(_fmt_rtt(float(rtt)))
    if kind == "degraded" and loss is not None and float(loss) > 0:
        extra.append(f"{float(loss):.0f}% loss")
    bits = [part for part in (name, ip, *extra) if part]
    return "  " + "  ".join(bits)


def format_status_message(snapshot: dict[str, Any]) -> str:
    hosts = list(snapshot.get("hosts") or [])
    cfg = dict(snapshot.get("config") or {})
    if not hosts:
        return "LAN Watch: no hosts configured."

    labeled = [(_display_status(host, cfg), host) for host in hosts]
    labeled.sort(
        key=lambda item: (
            STATUS_ORDER.get(item[0], 9),
            str(item[1].get("name") or "").lower(),
        )
    )
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

    def _fit(text: str) -> str:
        if len(text) <= TELEGRAM_LIMIT:
            return text
        return text[: TELEGRAM_LIMIT - 1] + "…"

    if counts["down"] == 0 and counts["degraded"] == 0 and counts["unknown"] == 0:
        up_block = "\n".join(_host_line(host, "up") for _, host in labeled)
        full = header + "\nAll hosts up.\n" + up_block
        if len(full) <= TELEGRAM_LIMIT:
            return full
        return header + "\nAll hosts up."

    sections: list[str] = []
    for kind, title in (
        ("down", "DOWN"),
        ("degraded", "DEGRADED"),
        ("unknown", "WAITING"),
    ):
        rows = [_host_line(host, kind) for item_kind, host in labeled if item_kind == kind]
        if rows:
            sections.append(title + "\n" + "\n".join(rows))
    body = header + "\n\n" + "\n\n".join(sections)
    up_rows = [_host_line(host, "up") for item_kind, host in labeled if item_kind == "up"]
    if up_rows:
        candidate = body + "\n\nUP\n" + "\n".join(up_rows)
        if len(candidate) <= TELEGRAM_LIMIT:
            return candidate
        body += f"\n\n{counts['up']} up."
    return _fit(body)


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
        log.info("Telegram /status commands enabled")

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
        cmd = _command(str(message.get("text") or ""))
        if cmd in {"/start", "/help"}:
            text = "LAN Watch. Send /status for a down-first health overview."
        elif cmd == "/status":
            text = format_status_message(snapshot())
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
