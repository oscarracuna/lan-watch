from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT_TICKS = 48


def _env_path(name: str) -> Path | None:
    value = (os.environ.get(name) or "").strip()
    return Path(value) if value else None


def config_file() -> Path:
    return _env_path("LANWATCH_CONFIG") or (ROOT / "config.yaml")


def data_dir() -> Path:
    return _env_path("LANWATCH_DATA") or (ROOT / "data")


def secrets_file() -> Path:
    return _env_path("LANWATCH_SECRETS") or (ROOT / "secrets" / "telegram.env")


CONFIG_PATH = ROOT / "config.yaml"


@dataclass
class Host:
    name: str
    ip: str
    role: str = "gateway"
    location: str = ""
    fail_threshold: int = 3
    recover_threshold: int = 2
    degraded_ms: float = 200.0
    degraded_loss_pct: float = 40.0


@dataclass
class Settings:
    listen: str = "0.0.0.0"
    port: int = 8080
    interval_seconds: float = 5.0
    timeout_seconds: float = 1.0
    fail_threshold: int = 3
    recover_threshold: int = 2
    endpoint_fail_threshold: int = 8
    endpoint_recover_threshold: int = 3
    history_hours: float = 24.0
    alert_sound: bool = True
    degraded_ms: float = 200.0
    degraded_loss_pct: float = 40.0
    endpoint_degraded_ms: float = 500.0
    endpoint_degraded_loss_pct: float = 70.0
    packets: int = 5
    heartbeat_ticks: int = HEARTBEAT_TICKS
    telegram_enabled: bool = False
    telegram_commands: bool = False
    alert_on_recovery: bool = True
    alert_cooldown_seconds: float = 900.0
    hosts: list[Host] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        return {
            "interval_seconds": self.interval_seconds,
            "timeout_seconds": self.timeout_seconds,
            "fail_threshold": self.fail_threshold,
            "recover_threshold": self.recover_threshold,
            "endpoint_fail_threshold": self.endpoint_fail_threshold,
            "endpoint_recover_threshold": self.endpoint_recover_threshold,
            "history_hours": self.history_hours,
            "alert_sound": self.alert_sound,
            "degraded_ms": self.degraded_ms,
            "degraded_loss_pct": self.degraded_loss_pct,
            "endpoint_degraded_ms": self.endpoint_degraded_ms,
            "endpoint_degraded_loss_pct": self.endpoint_degraded_loss_pct,
            "packets": self.packets,
            "heartbeat_ticks": self.heartbeat_ticks,
            "telegram_enabled": self.telegram_enabled,
            "telegram_commands": self.telegram_commands,
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


def _parse_host_list(
    items: Any,
    role: str,
    seen: set[str],
    fail_threshold: int,
    recover_threshold: int,
    degraded_ms: float,
    degraded_loss_pct: float,
) -> list[Host]:
    hosts: list[Host] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        ip = str(item.get("ip") or "").strip()
        name = str(item.get("name") or ip).strip()
        location = str(item.get("location") or "").strip()
        if not ip or ip in seen:
            continue
        seen.add(ip)
        hosts.append(
            Host(
                name=name or ip,
                ip=ip,
                role=role,
                location=location,
                fail_threshold=fail_threshold,
                recover_threshold=recover_threshold,
                degraded_ms=degraded_ms,
                degraded_loss_pct=degraded_loss_pct,
            )
        )
    return hosts


def load_config(path: Path | None = None) -> Settings:
    config_path = path or config_file()
    raw: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("config.yaml must be a mapping of settings")
        raw = loaded

    fail_threshold = max(1, _as_int(raw.get("fail_threshold"), 3))
    recover_threshold = max(1, _as_int(raw.get("recover_threshold"), 2))
    endpoint_fail_threshold = max(1, _as_int(raw.get("endpoint_fail_threshold"), 8))
    endpoint_recover_threshold = max(1, _as_int(raw.get("endpoint_recover_threshold"), 3))
    degraded_ms = max(0.0, _as_float(raw.get("degraded_ms"), 200.0))
    degraded_loss_pct = max(0.0, _as_float(raw.get("degraded_loss_pct"), 40.0))
    endpoint_degraded_ms = max(0.0, _as_float(raw.get("endpoint_degraded_ms"), 500.0))
    endpoint_degraded_loss_pct = max(0.0, _as_float(raw.get("endpoint_degraded_loss_pct"), 70.0))

    seen: set[str] = set()
    hosts = _parse_host_list(
        raw.get("hosts"),
        "gateway",
        seen,
        fail_threshold,
        recover_threshold,
        degraded_ms,
        degraded_loss_pct,
    )
    hosts.extend(
        _parse_host_list(
            raw.get("endpoints"),
            "endpoint",
            seen,
            endpoint_fail_threshold,
            endpoint_recover_threshold,
            endpoint_degraded_ms,
            endpoint_degraded_loss_pct,
        )
    )
    # Same fail, recover, and degraded thresholds as gateways.
    hosts.extend(
        _parse_host_list(
            raw.get("timeclocks"),
            "timeclock",
            seen,
            fail_threshold,
            recover_threshold,
            degraded_ms,
            degraded_loss_pct,
        )
    )

    return Settings(
        listen=str(raw.get("listen") or "0.0.0.0"),
        port=_as_int(raw.get("port"), 8080),
        interval_seconds=max(1.0, _as_float(raw.get("interval_seconds"), 5.0)),
        timeout_seconds=max(0.2, _as_float(raw.get("timeout_seconds"), 1.0)),
        fail_threshold=fail_threshold,
        recover_threshold=recover_threshold,
        endpoint_fail_threshold=endpoint_fail_threshold,
        endpoint_recover_threshold=endpoint_recover_threshold,
        history_hours=max(1.0, _as_float(raw.get("history_hours"), 24.0)),
        alert_sound=bool(raw.get("alert_sound", True)),
        degraded_ms=degraded_ms,
        degraded_loss_pct=degraded_loss_pct,
        endpoint_degraded_ms=endpoint_degraded_ms,
        endpoint_degraded_loss_pct=endpoint_degraded_loss_pct,
        packets=max(1, min(20, _as_int(raw.get("packets"), 5))),
        heartbeat_ticks=max(8, min(120, _as_int(raw.get("heartbeat_ticks"), HEARTBEAT_TICKS))),
        telegram_enabled=bool(raw.get("telegram_enabled", False)),
        telegram_commands=bool(raw.get("telegram_commands", False)),
        alert_on_recovery=bool(raw.get("alert_on_recovery", True)),
        alert_cooldown_seconds=max(0.0, _as_float(raw.get("alert_cooldown_seconds"), 900.0)),
        hosts=hosts,
    )


def config_mtime(path: Path | None = None) -> float:
    config_path = path or config_file()
    try:
        return config_path.stat().st_mtime
    except OSError:
        return 0.0
