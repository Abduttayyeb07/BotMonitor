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

Use `host.docker.internal` for health endpoints running on the host. Host systemd monitoring is disabled by default because systemd is outside the container boundary.

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
