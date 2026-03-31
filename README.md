# strava-mcp

A local Strava data pipeline and MCP server. Syncs your Strava activities to a SQLite database and exposes everything to Claude via the Model Context Protocol.

## What it does

1. **`strava_downloader.py`** — fetches your athlete profile, activities (with laps, splits, segment efforts, and HR/power zones), gear, routes, and starred segments from the Strava API and stores them locally in SQLite. Run it as a cron job for incremental daily syncs.

2. **`mcp_server.py`** — an HTTP streamable MCP server (with bearer token auth) that gives Claude tools to query your Strava data conversationally.

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Configure credentials

Copy `.env.example` to `.env` and fill in your Strava credentials:

```bash
cp .env.example .env
```

You need a Strava API application and an OAuth2 refresh token:

1. Go to [strava.com/settings/api](https://www.strava.com/settings/api) and create an app
2. Authorize your app to get an authorization code:
   ```
   https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID
     &redirect_uri=http://localhost&response_type=code
     &scope=activity:read_all,profile:read_all
   ```
3. Exchange the code for tokens:
   ```bash
   curl -X POST https://www.strava.com/oauth/token \
     -d client_id=YOUR_CLIENT_ID \
     -d client_secret=YOUR_CLIENT_SECRET \
     -d code=AUTHORIZATION_CODE \
     -d grant_type=authorization_code
   ```
4. Copy `access_token`, `refresh_token`, and `expires_at` into `.env`

For the MCP server, generate a bearer token:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```
Add it as `STRAVA_MCP_AUTH_TOKEN` in `.env`.

### 3. Sync your data

```bash
# First run — last 30 days (fast, to verify everything works)
.venv/bin/python strava_downloader.py --days 30

# Full historical sync (2+ years, respects Strava rate limits automatically)
.venv/bin/python strava_downloader.py

# Re-fetch laps/zones for all activities (if you ran a summary-only import before)
.venv/bin/python strava_downloader.py --full
```

The downloader automatically refreshes expired OAuth2 tokens and saves them back to `.env`.

### 4. Start the MCP server

```bash
.venv/bin/python mcp_server.py
```

This starts an HTTP server on port `8080` (default). Connect your MCP client with:
```json
{
  "mcpServers": {
    "strava": {
      "url": "http://localhost:8080/mcp/",
      "headers": {
        "Authorization": "Bearer YOUR_STRAVA_MCP_AUTH_TOKEN"
      }
    }
  }
}
```

For stdio transport (Claude Desktop local):
```bash
.venv/bin/python mcp_server.py --transport stdio
```

## Cron job (incremental sync)

Add to your crontab for a daily sync at 6 AM:

```
0 6 * * * cd /path/to/strava-mcp && .venv/bin/python strava_downloader.py >> /var/log/strava-sync.log 2>&1
```

Each incremental run picks up from the most recent activity already in the database.

## What Claude can query

**Resources** (snapshot data):
- Athlete profile with lifetime totals
- All activities (with pace, speed, and unit conversions pre-computed)
- Aggregate stats by sport type
- Monthly training trends
- Recent activities (last 30 days)
- Gear / equipment list

**Tools** (parameterized queries):
- `query_activities` — filter by sport, date range, distance, HR, power, commute flag
- `get_activity_details` — full detail for one activity: laps, metric splits, zone distribution, segment efforts
- `get_segment_efforts` — your history on any segment (progression over time)
- `get_power_analysis` — power stats with FTP estimate
- `get_training_trends` — weekly or monthly aggregates for any metric
- `get_gear_stats` — distance logged per bike or shoe
- `get_routes` — your saved routes
- `execute_sql` — custom SELECT queries against any table

## Database schema

SQLite database at `./strava_activities.db` (configurable via `STRAVA_DB_PATH`).

| Table | Contents |
|-------|----------|
| `athletes` | Athlete profile + YTD/all-time totals |
| `activities` | All activities — summary + detail fields |
| `activity_laps` | Lap splits per activity |
| `activity_splits_metric` | 1 km metric splits per activity |
| `segment_efforts` | Segment efforts within activities |
| `segments` | Segment master data |
| `starred_segments` | Your starred segments |
| `gear` | Bikes and shoes |
| `routes` | Saved routes |
| `activity_zones` | HR/power zone distribution per activity |
| `activity_summary` *(view)* | Activities with km, pace, speed pre-computed |
| `monthly_stats` *(view)* | Aggregated by month + sport type |

## Rate limits

Strava enforces 100 requests per 15 minutes and 1000 per day. The downloader:
- Sleeps 0.6s between activity detail fetches
- Automatically waits for the next 15-minute window on a 429 response
- Retries 5xx errors up to 3 times with back-off

## Production deployment (Linux)

```bash
sudo useradd --system --shell /usr/sbin/nologin strava-mcp
sudo mkdir -p /opt/strava-mcp
# Copy files, set up venv, create .env
sudo cp deploy/strava-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now strava-mcp
```

See `deploy/strava-mcp.service` for the full systemd unit.
