import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from app.config import load_config
from app.pinger import Hub, PingEngine
from app.store import Store

CONFIG = textwrap.dedent(
    """
    fail_threshold: 3
    recover_threshold: 2
    endpoint_fail_threshold: 8
    endpoint_recover_threshold: 3
    degraded_ms: 200
    degraded_loss_pct: 40
    endpoint_degraded_ms: 500
    endpoint_degraded_loss_pct: 70
    hosts:
      - name: Gateway
        ip: 10.0.0.1
    endpoints:
      - name: Camera
        ip: 10.0.0.2
    timeclocks:
      - name: Front desk
        ip: 10.1.0.8
        location: Office
      - name: Shop floor
        ip: 10.2.0.8
        location: Warehouse
      - name: Duplicate
        ip: 10.0.0.1
        location: Office
    """
)


def _write_config(directory: Path) -> Path:
    path = directory / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def _sample(engine: PingEngine, state, ok: bool, ts: float):
    return engine._apply_sample(
        state,
        ok,
        1.0 if ok else None,
        ts,
        loss_pct=0.0 if ok else 100.0,
        rtt_min_ms=1.0 if ok else None,
        rtt_max_ms=1.0 if ok else None,
        packets_ok=1 if ok else 0,
        packets=1,
    )


class TimeclockConfigTests(unittest.TestCase):
    def test_timeclocks_reuse_gateway_alarm_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_config(_write_config(Path(tmp)))

        roles = {host.role: host for host in settings.hosts}
        gateway = roles["gateway"]
        timeclock = next(host for host in settings.hosts if host.ip == "10.1.0.8")
        endpoint = roles["endpoint"]

        self.assertEqual(timeclock.role, "timeclock")
        self.assertEqual(timeclock.fail_threshold, settings.fail_threshold)
        self.assertEqual(timeclock.fail_threshold, gateway.fail_threshold)
        self.assertEqual(timeclock.fail_threshold, 3)
        self.assertEqual(timeclock.recover_threshold, gateway.recover_threshold)
        self.assertEqual(timeclock.degraded_ms, gateway.degraded_ms)
        self.assertEqual(timeclock.degraded_loss_pct, gateway.degraded_loss_pct)
        self.assertEqual(endpoint.fail_threshold, 8)
        self.assertNotEqual(timeclock.fail_threshold, endpoint.fail_threshold)
        self.assertNotEqual(timeclock.degraded_ms, endpoint.degraded_ms)

    def test_lists_timeclocks_for_every_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_config(_write_config(Path(tmp)))

        clocks = [host for host in settings.hosts if host.role == "timeclock"]
        self.assertEqual(
            [(host.name, host.location) for host in clocks],
            [("Front desk", "Office"), ("Shop floor", "Warehouse")],
        )

    def test_duplicate_ip_keeps_the_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_config(_write_config(Path(tmp)))

        matches = [host for host in settings.hosts if host.ip == "10.0.0.1"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].role, "gateway")

    def test_shipped_config_loads(self) -> None:
        settings = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
        self.assertTrue(any(host.role == "gateway" for host in settings.hosts))
        self.assertFalse(any(host.role == "timeclock" for host in settings.hosts))
        self.assertEqual(settings.fail_threshold, 3)


class TimeclockAlarmTests(unittest.TestCase):
    def test_down_alarm_uses_gateway_fail_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_config(_write_config(root))
            store = Store(root / "lan-watch.db")
            try:
                engine = PingEngine(settings, store, Hub())
                clock = engine.states["10.1.0.8"]
                camera = engine.states["10.0.0.2"]
                self.assertEqual(clock.fail_threshold, 3)
                self.assertEqual(clock.location, "Office")
                self.assertEqual(camera.fail_threshold, 8)

                ts = time.time()
                for _ in range(clock.recover_threshold):
                    ts += 1
                    self.assertIsNone(_sample(engine, clock, True, ts))
                self.assertEqual(clock.status, "up")

                for _ in range(clock.fail_threshold - 1):
                    ts += 1
                    self.assertIsNone(_sample(engine, clock, False, ts))
                    self.assertEqual(clock.status, "up")

                ts += 1
                alert = _sample(engine, clock, False, ts)
                self.assertEqual(clock.status, "down")
                self.assertIsNotNone(alert)
                self.assertEqual(alert["status"], "down")
                self.assertEqual(alert["name"], "Front desk")

                for _ in range(camera.recover_threshold):
                    ts += 1
                    _sample(engine, camera, True, ts)
                self.assertEqual(camera.status, "up")
                for _ in range(clock.fail_threshold):
                    ts += 1
                    self.assertIsNone(_sample(engine, camera, False, ts))
                self.assertEqual(camera.status, "up")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
