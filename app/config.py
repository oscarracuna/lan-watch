from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
HEARTBEAT_TICKS = 48


@dataclass
class Host:
    name: str
    ip: str


@dataclass
class Settings:
    listen: str = "0.0.0.0"
    port: int = 8080
    interval_seconds: float = 5.0
    timeout_seconds: float = 1.0
    fail_threshold: int = 3
    recover_threshold: int = 2
    history_hours: float = 24.0
    alert_sound: bool = True
    degraded_ms: float = 200.0
    degraded_loss_pct: float = 40.0
    packets: int = 5
    heartbeat_ticks: int = HEARTBEAT_TICKS
    telegram_enabled: bool = False
    alert_on_recovery: bool = True
    alert_cooldown_seconds: float = 900.0
    hosts: list[Host] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        return {
            "interval_seconds": self.interval_seconds,
            "timeout_seconds": self.timeout_seconds,
            "fail_threshold": self.fail_threshold,
            "recover_threshold": self.recover_threshold,
            "history_hours": self.history_hours,
            "alert_sound": self.alert_sound,
            "degraded_ms": self.degraded_ms,
            "degraded_loss_pct": self.degraded_loss_pct,
            "packets": self.packets,
            "heartbeat_ticks": self.heartbeat_ticks,
            "telegram_enabled": self.telegram_enabled,
            "alert_on_recovery": self.alert_on_recovery,
            "alert_cooldown_seconds": self.alert_cooldown_seconds,
        }


def _as_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_config(path: Path | None = None) -> Settings:
    config_path = path or CONFIG_PATH
    raw: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("config.yaml must be a mapping of settings")
        raw = loaded

    hosts: list[Host] = []
    seen: set[str] = set()
    for item in raw.get("hosts") or []:
        if not isinstance(item, dict):
            continue
        ip = str(item.get("ip") or "").strip()
        name = str(item.get("name") or ip).strip()
        if not ip or ip in seen:
            continue
        seen.add(ip)
        hosts.append(Host(name=name or ip, ip=ip))

    return Settings(
        listen=str(raw.get("listen") or "0.0.0.0"),
        port=_as_int(raw.get("port"), 8080),
        interval_seconds=max(1.0, _as_float(raw.get("interval_seconds"), 5.0)),
        timeout_seconds=max(0.2, _as_float(raw.get("timeout_seconds"), 1.0)),
        fail_threshold=max(1, _as_int(raw.get("fail_threshold"), 3)),
        recover_threshold=max(1, _as_int(raw.get("recover_threshold"), 2)),
        history_hours=max(1.0, _as_float(raw.get("history_hours"), 24.0)),
        alert_sound=bool(raw.get("alert_sound", True)),
        degraded_ms=max(0.0, _as_float(raw.get("degraded_ms"), 200.0)),
        degraded_loss_pct=max(0.0, _as_float(raw.get("degraded_loss_pct"), 40.0)),
        packets=max(1, min(20, _as_int(raw.get("packets"), 5))),
        heartbeat_ticks=max(8, min(120, _as_int(raw.get("heartbeat_ticks"), HEARTBEAT_TICKS))),
        telegram_enabled=bool(raw.get("telegram_enabled", False)),
        alert_on_recovery=bool(raw.get("alert_on_recovery", True)),
        alert_cooldown_seconds=max(0.0, _as_float(raw.get("alert_cooldown_seconds"), 900.0)),
        hosts=hosts,
    )


def config_mtime(path: Path | None = None) -> float:
    config_path = path or CONFIG_PATH
    try:
        return config_path.stat().st_mtime
    except OSError:
        return 0.0
