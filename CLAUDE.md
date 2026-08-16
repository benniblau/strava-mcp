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
| `schema/schema_strava.sql` | Full SQLite schema (10 tables, 2 views) |
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
.venv/bin/python strava_downloader.py --backfill-detail  # detail only where missing
.venv/bin/python strava_downloader.py --full             # re-fetch detail for ALL activities

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

Because `INSERT OR REPLACE` rewrites the whole row, columns that only
DetailedActivity carries would be blanked by a later summary sync. Those
columns are listed in `DETAIL_ONLY_COLUMNS` (`device_name`, `description`) and
carried forward from the existing row in `download_activities()`. **Add any new
detail-only column to that tuple**, or it will silently disappear on the next
incremental run.

New columns also never reach an existing database, since every table is
`CREATE TABLE IF NOT EXISTS`. `_add_missing_columns()` runs after the schema
and `ALTER TABLE`s anything absent; add new columns there too.

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

**Write tools** (push to Strava, require the `activity:write` scope):
- `upload_activity` — upload a FIT/GPX/TCX file, polling until Strava has
  created the activity or rejected it
- `get_upload_status` — poll an upload started with `wait=False`
- `update_activity` — name, description, sport_type, gear_id, trainer,
  commute, hide_from_home

`get_client()` builds the downloader lazily, so the read-only tools keep
working when credentials are absent or the token has lapsed.

## Strava API notes

- Rate limit: 100 requests / 15 min, 1000 / day
- Access tokens expire after 6 hours; refresh tokens are long-lived
- `GET /athlete/activities` returns SummaryActivity (no laps/zones)
- `GET /activities/{id}` returns DetailedActivity (adds laps, splits, segment
  efforts, `description` and `device_name`)
- **`device_name` is your own activities only.** There is no endpoint for
  another athlete's activities, and none for followers/following — the social
  graph is not exposed by the API at all, so device usage cannot be surveyed
  across the people you follow.
- **Uploading is asynchronous.** `POST /uploads` returns an upload id, not an
  activity; poll `GET /uploads/{id}` until `activity_id` or `error` appears.
  A file whose start time matches an existing activity fails as a duplicate
  rather than importing twice.
- **Deletion is closed to third-party apps.** `DELETE /api/v3/activities/{id}`
  exists — it answers `401 {"resource":"Application","field":"internal",
  "code":"invalid"}` rather than 404 or 405 — but it is restricted to Strava's
  internal applications. That error means the *application* is not permitted;
  a scope problem instead reads `{"resource":"AccessToken","field":"…",
  "code":"missing"}`. No scope unlocks it. `POST` with `_method=delete` hits
  the same wall.
  The website uses the same path via a Rails UJS link authenticated by session
  cookie and CSRF token — a different security context, and not something to
  automate. Deleting in the UI is the supported route; Strava keeps a deleted
  activity restorable for 30 days.
- **`PUT /activities/{id}` silently ignores unknown fields.** Sending
  `{"delete": true}` returns 200 with the activity unchanged. A misspelled
  field name reports success and does nothing, so verify the effect rather
  than the status code.
- **Changing scopes needs a fresh authorization.** A refresh token carries
  forward the scopes it was granted and cannot widen them; use
  `--authorize <code>`, which skips the usual startup refresh so the old token
  cannot interfere.
- `GET /activities/{id}/zones` is a separate call
- Downloader sleeps 0.6s between detail fetches to stay well under rate limits
- Use `--backfill-detail` to fill in detail for activities synced before detail-fetching
  was added; `--full` re-fetches detail for every activity (slow, one request each)

## Modeled after

`../garmin-mcp` — same two-component pattern, same HTTP transport stack (FastMCP + Starlette + BearerAuth + StreamableHTTPSessionManager).
