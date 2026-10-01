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

Run the dashboard as the `lanwatch` system user (`nologin`). That account owns the process, the SQLite file, and the Telegram env file. You still SSH and `sudo` as yourself; `lanwatch` is not a login.

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip iputils-ping rsync curl git
sudo useradd --system --home /home/lanwatch --shell /usr/sbin/nologin lanwatch  # skip if it already exists
sudo -u lanwatch ping -c 1 127.0.0.1   # unprivileged ping must work

git clone https://github.com/oscarracuna/lan-watch.git
cd lan-watch
sudo ./scripts/deploy.sh
```

Later updates are the same: `git pull` in that checkout, then `sudo ./scripts/deploy.sh`. The script copies code to `/opt/lan-watch`, installs the unit, restarts, and curls `/api/status`. It does **not** replace:

| Path | Role |
|---|---|
| `/etc/lan-watch/config.yaml` | Host list and tuning (`LANWATCH_CONFIG`) |
| `/etc/lan-watch/telegram.env` | Bot token and chat id, mode 600 (`EnvironmentFile`) |
| `/var/lib/lan-watch/` | SQLite history (`LANWATCH_DATA`) |

First run moves an existing `/opt/lan-watch/config.yaml` and `data/lan-watch.db` into those locations if they are not there yet. Dashboard: `http://<host-ip>:8080/`. Logs: `journalctl -u lan-watch -f`.

The unit restarts on crash and starts at boot. Bind address and port still come from config (`listen` / `port`). Port 8080 does not need root.

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
| `telegram_enabled` | Send down/up alerts to Telegram when secrets are present |
| `telegram_commands` | Long-poll `/status` (enable on **one** process only) |
| `alert_on_recovery` | Also send a message when a host comes back |
| `alert_cooldown_seconds` | Minimum time between identical Telegram messages for one host |
| `hosts` | List of `{name, ip}` |

### Telegram alerts

Do **not** put the bot token in `config.yaml` or git. On the systemd host, the file is `/etc/lan-watch/telegram.env` (mode 600, owned by `lanwatch`). Locally or on the test tree, copy `secrets/telegram.env.example` to `secrets/telegram.env`.

On the test host:

```bash
nano ~/lan-watch-test/secrets/telegram.env
chmod 600 ~/lan-watch-test/secrets/telegram.env
# Open the bot in Telegram, tap Start, send any message
~/lan-watch-test/scripts/telegram-chat-id.sh   # copy chat.id into TELEGRAM_CHAT_ID
~/lan-watch-test/scripts/telegram-test.sh
~/lan-watch-test/scripts/stop.sh && ~/lan-watch-test/scripts/start.sh
```

The Linux box must reach `api.telegram.org:443`.

With `telegram_commands: true`, the same bot answers `/status`, `/incidents`, and `/host NAME` (plus `/start`) from your `TELEGRAM_CHAT_ID` only. Other chats are ignored. `/incidents` is the history-window outage log with duration; `/host` matches a name or IP (unique prefix is enough). Telegram long-polls `getUpdates`, so nothing extra is opened on the LAN. Only one LAN Watch process should have commands on, or the two will steal updates from each other — leave it off on the 8081 test tree when prod is polling.

Alerts fire only on state edges (down / recovery), not on every missed ping. Down banners stay until dismissed; recoveries clear after about 15 seconds. Optional desktop notifications use the browser Notification API after you click **Enable notifications**.

The dashboard tab title shows `2 DOWN · LAN Watch` when anything is down. Cards sort down-first and can be filtered by name or IP. Each card has a heartbeat strip of recent sweeps (green / amber / red). The incident list keeps the last 24 hours of down/up events with duration.

Each sweep sends `packets` pings (default 5). A sweep counts as up if at least one reply arrives; loss % and RTT spread are shown on the card. History lives in `data/lan-watch.db` locally, or `/var/lib/lan-watch/` on the systemd host, and is pruned to `history_hours`. Click a card for a sparkline and uptime over that window.

## Notes

- No login. Bind only to a trusted LAN.
- Email alerts are not in this version; the same down/up events can drive SMTP later.
- Windows testing uses `ping -n 1`; Linux uses `ping -c 1`.
