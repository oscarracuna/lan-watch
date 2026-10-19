from __future__ import annotations

import json
import threading
from typing import Any

from app.config import data_dir

# Ciudad Juárez, with room to place a site slightly outside the urban core.
JUAREZ_CENTER = (31.6904, -106.4245)
LAT_RANGE = (31.45, 31.90)
LNG_RANGE = (-106.70, -106.15)

_lock = threading.Lock()


def positions_path():
    return data_dir() / "map-positions.json"


def _valid(lat: float, lng: float) -> bool:
    return LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LNG_RANGE[0] <= lng <= LNG_RANGE[1]


def load_positions() -> dict[str, dict[str, float]]:
    path = positions_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        try:
            lat = float(value["lat"])
            lng = float(value["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        if _valid(lat, lng):
            out[str(key)] = {"lat": lat, "lng": lng}
    return out


def save_position(host_key: str, lat: float, lng: float) -> dict[str, float]:
    lat_n = float(lat)
    lng_n = float(lng)
    if not _valid(lat_n, lng_n):
        raise ValueError("Pin must stay on the Ciudad Juárez map")
    point = {"lat": round(lat_n, 6), "lng": round(lng_n, 6)}
    with _lock:
        current = load_positions()
        current[host_key] = point
        _write(current)
    return point


def clear_position(host_key: str) -> None:
    with _lock:
        current = load_positions()
        current.pop(host_key, None)
        _write(current)


def _write(positions: dict[str, Any]) -> None:
    path = positions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(positions, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
