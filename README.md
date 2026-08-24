# watcher

Polls a GraphQL endpoint on an interval and posts an alert to a Discord webhook
for each new match in a configured area.

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in real values.
3. `python watcher.py`

Runs until stopped.

## Running on GitHub Actions

`.github/workflows/watcher.yml` runs this on a schedule instead of requiring an
always-on machine. In this repo's Settings -> Secrets and variables -> Actions,
add these repository secrets:

- `DISCORD_WEBHOOK`
- `PVP_DISCORD_WEBHOOK` (optional -- separate channel for PVP top-rank alerts)
- `API_BASE`
- `COOKIE_NAME`
- `SESSION_COOKIE`
- `MIN_LAT`, `MAX_LAT`, `MIN_LON`, `MAX_LON`

The session cookie will expire eventually and need to be refreshed in the
`SESSION_COOKIE` secret.
