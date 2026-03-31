# strava-mcp — Claude Code Notes

## Project overview

Two-component system:
- **`strava_downloader.py`** — cron job that fetches data from the Strava API v3 and stores it in SQLite
- **`mcp_server.py`** — HTTP streamable MCP server that exposes the SQLite data to Claude

## Key files

| File | Purpose |
|------|---------|
| `strava_downloader.py` | Data ingestion (auth, API calls, DB upserts) |
| `mcp_server.py` | FastMCP server — resources + tools + HTTP transport |
| `schema/schema_strava.sql` | Full SQLite schema (11 tables, 2 views) |
| `.env` | Credentials and config (auto-updated by downloader) |
| `requirements.txt` | Python dependencies |
| `deploy/strava-mcp.service` | systemd unit for production |

## Running locally

```bash
# Install dependencies (once)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Sync Strava data
.venv/bin/python strava_downloader.py --days 30   # last 30 days
.venv/bin/python strava_downloader.py             # incremental (since last DB entry)
.venv/bin/python strava_downloader.py --full      # re-fetch detail for all activities

# Start MCP server (HTTP, default)
.venv/bin/python mcp_server.py

# Start MCP server (stdio override)
.venv/bin/python mcp_server.py --transport stdio
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `STRAVA_CLIENT_ID` | Strava app client ID |
| `STRAVA_CLIENT_SECRET` | Strava app client secret |
| `STRAVA_REFRESH_TOKEN` | OAuth2 refresh token (auto-updated) |
| `STRAVA_ACCESS_TOKEN` | OAuth2 access token (auto-updated) |
| `STRAVA_TOKEN_EXPIRES_AT` | Unix timestamp of token expiry (auto-updated) |
| `STRAVA_DB_PATH` | Path to SQLite database (default: `./strava_activities.db`) |
| `STRAVA_START_DATE` | Earliest date for full sync (default: 2 years back) |
| `STRAVA_MCP_TRANSPORT` | `http` (default) or `stdio` |
| `STRAVA_MCP_AUTH_TOKEN` | Bearer token for HTTP transport |
| `STRAVA_MCP_HTTP_HOST` | HTTP bind address (default: `0.0.0.0`) |
| `STRAVA_MCP_HTTP_PORT` | HTTP port (default: `8080`) |

## Token management

`strava_downloader.py` automatically refreshes expired OAuth2 tokens and writes the updated values back into `.env`. The downloader probes the existing access token first to avoid an unnecessary refresh call.

## Database schema

```
athletes              — athlete profile + lifetime/YTD totals
activities            — main activity records (SummaryActivity fields + detail)
activity_laps         — lap splits per activity
activity_splits_metric — 1km metric splits per activity
segment_efforts       — segment efforts within activities
segments              — segment master data
starred_segments      — athlete's starred segments
gear                  — bikes and shoes
routes                — saved routes
activity_zones        — HR/power zone distribution per activity

Views:
  activity_summary    — activities with pre-computed km, pace, speed conversions
  monthly_stats       — aggregated by month + sport_type
```

All writes use `INSERT OR REPLACE` (upsert). Re-running the downloader is always safe.

## MCP resources and tools

**Resources** (read-only data):
- `strava://athlete` — athlete profile + stats
- `strava://activities` — all activities via `activity_summary` view
- `strava://stats/summary` — aggregate stats by sport type
- `strava://stats/monthly` — monthly trends
- `strava://activities/recent` — last 30 days
- `strava://gear` — equipment list

**Tools** (callable functions):
- `query_activities` — flexible filter by sport, date, distance, HR, power, commute
- `get_activity_details` — single activity + laps + zones + segment efforts
- `get_segment_efforts` — progression on a specific segment
- `get_power_analysis` — power stats + FTP estimate
- `get_training_trends` — weekly/monthly aggregates
- `get_gear_stats` — equipment usage breakdown
- `get_routes` — saved routes
- `execute_sql` — custom SELECT queries (read-only)

## Strava API notes

- Rate limit: 100 requests / 15 min, 1000 / day
- Access tokens expire after 6 hours; refresh tokens are long-lived
- `GET /athlete/activities` returns SummaryActivity (no laps/zones)
- `GET /activities/{id}` returns DetailedActivity (adds laps, splits, segment efforts)
- `GET /activities/{id}/zones` is a separate call
- Downloader sleeps 0.6s between detail fetches to stay well under rate limits
- Use `--full` flag to backfill detail for activities synced before detail-fetching was added

## Modeled after

`../garmin-mcp` — same two-component pattern, same HTTP transport stack (FastMCP + Starlette + BearerAuth + StreamableHTTPSessionManager).
