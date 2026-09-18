# LAN Watch

Live ICMP dashboard for a private network. A single Python process pings the hosts in `config.yaml`, stores recent history in SQLite, and pushes status to a LAN web page.

ICMP reachability is the whole check. A host that drops ping but still serves traffic will look down.

## Run locally (Windows)

From the project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app
```

Open http://127.0.0.1:8080/ on this machine (or `http://<this-pc>:8080/` from another LAN device).

`127.0.0.1` is included as **This host** so you should see at least one green card immediately. Edit `config.yaml` and add the private IPs you care about. The file is reloaded on change; you do not need to restart after adding hosts.

Listen address and port changes in `config.yaml` need a restart.

## Dedicated Linux host (systemd)

Install Python 3.10+, `ping` (`iputils-ping` on Debian/Ubuntu), then copy the project to `/opt/lan-watch`:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip iputils-ping
sudo mkdir -p /opt/lan-watch
sudo cp -r app config.yaml requirements.txt /opt/lan-watch/
cd /opt/lan-watch
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp lan-watch.service /etc/systemd/system/lan-watch.service
sudo systemctl daemon-reload
sudo systemctl enable --now lan-watch
```

Dashboard: `http://<host-ip>:8080/`

Logs: `journalctl -u lan-watch -f`

The unit restarts on crash and starts at boot. Bind address and port come from `config.yaml` (`listen` / `port`).

## Config

| Key | Meaning |
| --- | --- |
| `interval_seconds` | Time between ping sweeps |
| `timeout_seconds` | Per-host ping timeout |
| `fail_threshold` | Consecutive misses before **Down** |
| `recover_threshold` | Consecutive hits before **Up** |
| `history_hours` | How long samples are kept |
| `alert_sound` | Allow the dashboard beep (users can still mute in the page) |
| `degraded_ms` | Amber state when up but median RTT is at or above this; `0` disables it |
| `degraded_loss_pct` | Amber state when packet loss is at or above this; `0` disables it |
| `packets` | ICMP echoes per host per sweep (used for loss % and min/median/max RTT) |
| `heartbeat_ticks` | How many recent sweeps to show as ticks on each card |
| `hosts` | List of `{name, ip}` |

Alerts fire only on state edges (down / recovery), not on every missed ping. Down banners stay until dismissed; recoveries clear after about 15 seconds. Optional desktop notifications use the browser Notification API after you click **Enable notifications**.

The dashboard tab title shows `2 DOWN · LAN Watch` when anything is down. Cards sort down-first and can be filtered by name or IP. Each card has a heartbeat strip of recent sweeps (green / amber / red). The incident list keeps the last 24 hours of down/up events with duration.

Each sweep sends `packets` pings (default 5). A sweep counts as up if at least one reply arrives; loss % and RTT spread are shown on the card. History lives in `data/lan-watch.db` and is pruned to `history_hours`. Click a card for a sparkline and uptime over that window.

## Notes

- No login. Bind only to a trusted LAN.
- Email alerts are not in this version; the same down/up events can drive SMTP later.
- Windows testing uses `ping -n 1`; Linux uses `ping -c 1`.
