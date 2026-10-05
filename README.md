# Central Bot Monitoring

Centralized monitoring for Docker containers, systemd services, host resources, health checks, heartbeats, and Telegram incident notifications.

## Design

- Runs as one isolated process on the bot server.
- Reads Docker state/events through the Docker API.
- Checks systemd units and host resources.
- Uses SQLite so incidents survive monitor restarts.
- Fingerprints incidents and applies cooldowns to prevent Telegram spam.
- Sends recovery notifications when an incident clears.
- Does not modify or restart monitored applications.

## Docker deployment

The recommended deployment is Docker Compose. It persists incident state in `./data` and reads Docker state through `/var/run/docker.sock`. Docker socket access is highly privileged, so keep this service private and do not expose it through a public port.

```bash
cp config.example.yaml config.yaml
cp .env.example .env
# edit config.yaml and .env
mkdir -p data
docker compose build
docker compose up -d
docker compose logs -f central-monitor
```

Stop it with:

```bash
docker compose down
```

The Compose service uses host networking so health endpoints bound to host
localhost can be checked with `http://127.0.0.1:PORT/...`. Host systemd
monitoring uses the separate collector described below.

## Safe host systemd collector

The host systemd collector is separate from the Docker monitor. It checks only the configured units, writes `data/systemd-status.json`, and does not mount the host D-Bus or systemd directories into Docker.

On the server:

```bash
sudo cp systemd/central-monitor-systemd-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now central-monitor-systemd-collector
sudo systemctl status central-monitor-systemd-collector
```

Then rebuild the Docker monitor:

```bash
sudo systemctl restart central-monitor-systemd-collector
docker compose up -d --build
```

Restart the host collector after updating its script; rebuilding the Docker
monitor alone leaves the already-running host collector on its old code.

## Local Python quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# edit config.yaml and set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
python -m monitor
```

The monitor is intentionally configured explicitly. Start with the critical containers and systemd units, then add application heartbeats and health endpoints after validating names on the server.

## Environment

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_IDS=123456789,-1001234567890
```

Never commit `config.yaml`, `.env`, tokens, or credentials.

## Outage detection

Configured containers that are removed (including `docker compose down`) count
as unavailable. Project membership comes from `projects.*.items[].containers`,
so the dashboard and alert grouping share the same inventory. A complete outage
produces one project incident, including stacks with stopped applications and an
unhealthy database. A partial shutdown waits `project_correlation_seconds` before
sending container alerts. An active project outage stays open during partial recovery.

The example polls every 5 seconds and uses a 10-second grouping window. Copy
these settings into your active `config.yaml` to use them. Very short outages
between polls can still be missed; this version uses polling, not Docker events.
Telegram lookups share a short-lived Docker inventory cache. Incident opens are
acknowledged after successful delivery; failed deliveries are retried. Recovery
notifications are queued in SQLite and retried after monitor restarts.

Regression checks: `python -m unittest discover -s tests -v`.

## Bots report checks

`/botshealth` sends the combined Bots report; the scheduled copy runs at 09:05
Pakistan time. Each check can fail independently without hiding the other bots.
The report uses Docker log timestamps and container health, plus these live
checks:

- Nawa Valdora checks that `zigchain-wallet-monitor` is running and compares
  `/status` heights from its internal RPC, ZigScan, CryptoComics, and Numia.
  The internal height must be within 20 blocks of every public RPC.
- TokenX Vault reads the latest `HTTP backfill ... backlog=N` line for BSC and
  Ethereum USDT/USDC, requires each backlog at most 500 blocks, and checks that
  `tokenx-vault-postgres` is running and Docker reports it healthy. PostgreSQL
  checkpoint messages alone do not establish current database health.
- BEP20 and ETH USDT read their continuous `Live scan` and `Live scan result`
  lines. Both chain height and WebSocket height must advance within five
  minutes. ETH backlog must remain at most 500 blocks. BEP20's known Tatum 402
  credit failure can leave HTTP backfill far behind even while WebSocket data
  is live; the backlog is shown in the report without creating another alert
  for that known failure. Missing scan lines are reported as an issue because
  these bots normally emit them continuously.
- Sheets Sync requires a completed run updating all three configured vaults.
  The last complete run must be within 75 minutes, consecutive runs must be no
  more than 75 minutes apart, and an incomplete run gets a five-minute grace
  period before it is reported as an issue.
- Wallet Monitor, MDF Tracker, and HighBuy use Docker running/health state and
  do not expect startup WebSocket lines or occasional buy alerts to repeat.
  Three recent Telegram polling
  failures within ten minutes are reported; a recent fatal Wallet Monitor
  error is reported immediately.
- Wallet Watchman uses the host collector's journal summary. Wallet reload
  lines are displayed if present but are not required as a heartbeat. Three
  RPC failures within ten minutes create one deduplicated incident; an isolated
  timeout does not. Zigchain Exporter checks that systemd
  reports it running. If `bot_reports.system_services.exporter_metrics_url` is
  configured, its HTTP response is also checked; until then metrics delivery
  is shown as unverified. A collector snapshot older than 60 seconds is treated
  as unavailable.
