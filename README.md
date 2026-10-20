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

`127.0.0.1` is included as **This host** so you should see at least one green card immediately. Edit `config.yaml`: `hosts` are **Gateways** (top of the dashboard); `timeclocks` are **Terminals** (every location, same down and degraded thresholds as gateways); `endpoints` are the section below, with looser down/degraded thresholds. The file is reloaded on change; you do not need to restart after adding hosts.

Listen address and port changes in `config.yaml` need a restart.

## Dedicated Linux host (systemd)

Run the dashboard as the `lanwatch` system user (`nologin`). That account owns the process and the SQLite file. You still SSH and `sudo` as yourself; `lanwatch` is not a login.

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip iputils-ping rsync curl git
sudo useradd --system --home /home/lanwatch --shell /usr/sbin/nologin lanwatch  # skip if it already exists
sudo -u lanwatch ping -c 1 127.0.0.1   # unprivileged ping must work

git clone -b share/dashboard https://github.com/oscarracuna/lan-watch.git
cd lan-watch
sudo ./scripts/deploy.sh
```

Later updates are the same: `git pull` in that checkout, then `sudo ./scripts/deploy.sh`. The script copies code to `/opt/lan-watch`, installs the unit, restarts, and curls `/api/status`. It does **not** replace:

| Path | Role |
|---|---|
| `/etc/lan-watch/config.yaml` | Host list and tuning (`LANWATCH_CONFIG`) |
| `/var/lib/lan-watch/` | SQLite history (`LANWATCH_DATA`) |

First run moves an existing `/opt/lan-watch/config.yaml` and `data/lan-watch.db` into those locations if they are not there yet. Dashboard: `http://<host-ip>:8080/`. Logs: `journalctl -u lan-watch -f`.

The unit restarts on crash and starts at boot. Bind address and port still come from config (`listen` / `port`). Port 8080 does not need root.

## Config

| Key | Meaning |
| --- | --- |
| `interval_seconds` | Time between ping sweeps |
| `timeout_seconds` | Per-host ping timeout |
| `fail_threshold` | Consecutive misses before **Down** (gateways in `hosts`) |
| `recover_threshold` | Consecutive hits before **Up** (gateways) |
| `endpoint_fail_threshold` | Consecutive misses before **Down** for `endpoints` (default 8) |
| `endpoint_recover_threshold` | Consecutive hits before **Up** for `endpoints` (default 3) |
| `history_hours` | How long samples are kept |
| `alert_sound` | Allow the dashboard beep (users can still mute in the page) |
| `degraded_ms` | Amber state for gateways when up but median RTT is at or above this; `0` disables it |
| `degraded_loss_pct` | Amber state for gateways when packet loss is at or above this; `0` disables it |
| `endpoint_degraded_ms` | Amber RTT threshold for endpoints (default 500) |
| `endpoint_degraded_loss_pct` | Amber loss threshold for endpoints (default 70) |
| `packets` | ICMP echoes per host per sweep (used for loss % and min/median/max RTT) |
| `heartbeat_ticks` | How many recent sweeps to show as ticks on each card |
| `hosts` | Gateways: list of `{name, ip}` (top dashboard section) |
| `timeclocks` | Timeclocks in **Terminals**: `{name, ip, location}` for every location. Down uses `fail_threshold`; amber uses `degraded_ms` and `degraded_loss_pct` (the gateway values). |
| `endpoints` | Endpoints: same shape as gateways, shown below terminals, with the looser endpoint thresholds |

Alerts fire only on state edges (down / recovery), not on every missed ping. Down banners stay until dismissed; recoveries clear after about 15 seconds. Optional desktop notifications use the browser Notification API after you click **Enable notifications**.

The dashboard tab title shows `2 DOWN · LAN Watch` when anything is down. Gateways sit above terminals, then endpoints. Cards sort down-first within each section and can be filtered by name, location, or IP. Each card has a heartbeat strip of recent sweeps (green / amber / red). The incident list keeps the last 24 hours of down/up events with duration.

Each sweep sends `packets` pings (default 5). A sweep counts as up if at least one reply arrives; loss % and RTT spread are shown on the card. History lives in `data/lan-watch.db` locally, or `/var/lib/lan-watch/` on the systemd host, and is pruned to `history_hours`. Click a card for a sparkline and uptime over that window.

## Notes

- No login. Bind only to a trusted LAN.
- Email alerts are not in this version; the same down/up events can drive SMTP later.
- Windows testing uses `ping -n 1`; Linux uses `ping -c 1`.
