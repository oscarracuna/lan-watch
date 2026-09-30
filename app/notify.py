from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

from app.config import Settings, secrets_file

log = logging.getLogger("uvicorn.error")


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


class TelegramNotifier:
    def __init__(self) -> None:
        self._last: dict[str, tuple[str, float]] = {}
        self._warned_missing = False

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
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"Telegram HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Telegram network error: {exc.reason}") from exc
        parsed = json.loads(body)
        if not parsed.get("ok"):
            raise RuntimeError(f"Telegram rejected send: {parsed.get('description')}")

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
