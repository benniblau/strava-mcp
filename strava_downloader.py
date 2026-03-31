#!/usr/bin/env python3
"""
Strava Data Downloader

Fetches athlete profile, activities (with laps, splits, segment efforts, zones),
gear, routes, and starred segments from the Strava API v3 and stores everything
in a local SQLite database. Designed to run as a cron job for incremental syncs.

Usage:
    python strava_downloader.py              # incremental (since last activity in DB)
    python strava_downloader.py --days 30   # re-sync last 30 days
    python strava_downloader.py --full      # re-fetch detail for ALL activities
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"

SCHEMA_PATH = Path(__file__).parent / "schema" / "schema_strava.sql"
DEFAULT_DB_PATH = Path(__file__).parent / "strava_activities.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(dt: datetime) -> str:
    """ISO8601 string for storage."""
    return dt.isoformat()


def _now() -> str:
    return _ts(datetime.now(timezone.utc))


def _upsert(conn: sqlite3.Connection, table: str, data: Dict[str, Any], pk: str = "id") -> None:
    """Generic INSERT OR REPLACE."""
    if not data:
        return
    columns = ", ".join(data.keys())
    placeholders = ", ".join(["?" for _ in data])
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(data.values()),
    )


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class StravaDownloader:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self.client_id = os.getenv("STRAVA_CLIENT_ID", "")
        self.client_secret = os.getenv("STRAVA_CLIENT_SECRET", "")
        self.access_token = os.getenv("STRAVA_ACCESS_TOKEN", "")
        self.refresh_token = os.getenv("STRAVA_REFRESH_TOKEN", "")
        self.expires_at = int(os.getenv("STRAVA_TOKEN_EXPIRES_AT", "0"))
        self.env_path = Path(__file__).parent / ".env"

        if not self.client_id or not self.client_secret:
            print("❌  STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set in .env")
            sys.exit(1)
        if not self.refresh_token:
            print("❌  STRAVA_REFRESH_TOKEN must be set in .env")
            sys.exit(1)

        self.init_database()
        self.authenticate()

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    def init_database(self) -> None:
        print(f"Initializing database: {self.db_path}")
        schema = SCHEMA_PATH.read_text()
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(schema)
            conn.commit()
        print("✅  Database ready")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Authentication / token management
    # ------------------------------------------------------------------

    def authenticate(self, force: bool = False) -> None:
        """Refresh access token if expired or about to expire (within 5 min).

        Pass force=True to always fetch a new token regardless of expiry —
        used when the API has returned a 401 despite a seemingly valid token.
        """
        if not force and self.access_token and time.time() < self.expires_at - 300:
            print("✅  Access token still valid")
            return

        # If expires_at is 0 (unknown), probe the existing token first before
        # trying a refresh — avoids a needless round-trip when the token works.
        # Skip the probe when force=True (we already know the token is rejected).
        if not force and self.access_token and self.expires_at == 0:
            try:
                probe = requests.get(
                    f"{BASE_URL}/athlete",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    timeout=15,
                )
                if probe.status_code == 200:
                    # Token is alive; set a conservative expiry so we don't re-probe
                    self.expires_at = int(time.time()) + 3600
                    self._save_tokens()
                    print("✅  Access token verified (expiry unknown, will refresh in 1h)")
                    return
                elif probe.status_code not in (401, 403):
                    raise RuntimeError(
                        f"Strava API returned {probe.status_code} while checking token. "
                        "The API may be temporarily unavailable — please try again shortly."
                    )
                # 401/403 → fall through to refresh
            except requests.exceptions.Timeout:
                raise RuntimeError(
                    "Strava API timed out. The service may be temporarily unavailable "
                    "— please try again shortly."
                )
            except requests.exceptions.ConnectionError:
                raise RuntimeError(
                    "Cannot reach the Strava API. Check your internet connection and try again."
                )

        print("🔑  Refreshing Strava access token…")
        resp = None
        for attempt in range(3):
            try:
                resp = requests.post(TOKEN_URL, data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                }, timeout=30)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                wait = 10 * (attempt + 1)
                print(f"    Connection error on attempt {attempt + 1}: {e}. Retrying in {wait}s…")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = 10 * (attempt + 1)
                print(f"    Strava token endpoint returned {resp.status_code}, retrying in {wait}s…")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        else:
            status = resp.status_code if resp is not None else "timeout"
            raise RuntimeError(
                f"Strava token endpoint returned {status} after 3 attempts. "
                "The API may be temporarily unavailable — please try again shortly."
            )
        data = resp.json()

        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self.expires_at = data["expires_at"]

        self._save_tokens()
        print(f"✅  Token refreshed, expires {datetime.fromtimestamp(self.expires_at)}")

    def _save_tokens(self) -> None:
        """Persist updated tokens back into the .env file."""
        updates = {
            "STRAVA_ACCESS_TOKEN": self.access_token,
            "STRAVA_REFRESH_TOKEN": self.refresh_token,
            "STRAVA_TOKEN_EXPIRES_AT": str(self.expires_at),
        }

        if self.env_path.exists():
            text = self.env_path.read_text()
        else:
            text = ""

        for key, value in updates.items():
            pattern = rf"^{re.escape(key)}=.*$"
            replacement = f"{key}={value}"
            if re.search(pattern, text, re.MULTILINE):
                text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
            else:
                text = text.rstrip("\n") + f"\n{replacement}\n"

        self.env_path.write_text(text)

    # ------------------------------------------------------------------
    # HTTP client
    # ------------------------------------------------------------------

    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Any:
        """GET request with automatic rate-limit handling and 401 retry."""
        url = f"{BASE_URL}{endpoint}"
        headers = {"Authorization": f"Bearer {self.access_token}"}

        for attempt in range(3):
            resp = requests.get(url, headers=headers, params=params, timeout=30)

            if resp.status_code == 401:
                try:
                    err_body = resp.json()
                except Exception:
                    err_body = resp.text
                if attempt == 0:
                    print(f"⚠️   401 Unauthorized ({err_body}) — forcing token refresh and retrying…")
                    self.authenticate(force=True)
                    headers["Authorization"] = f"Bearer {self.access_token}"
                    continue
                raise RuntimeError(
                    f"401 Unauthorized after token refresh on {endpoint}.\n"
                    f"Strava error: {err_body}\n"
                    "This is likely a scope issue — re-authorize with "
                    "scope=activity:read_all,profile:read_all and update STRAVA_REFRESH_TOKEN."
                )

            if resp.status_code == 429:
                # Parse rate limit headers
                usage = resp.headers.get("X-RateLimit-Usage", "0,0")
                limit = resp.headers.get("X-RateLimit-Limit", "100,1000")
                print(f"⏳  Rate limited (usage: {usage} / limit: {limit})")
                # Wait until the next 15-minute boundary
                now = time.time()
                sleep_secs = 900 - (now % 900) + 5
                print(f"    Sleeping {sleep_secs:.0f}s until rate limit resets…")
                time.sleep(sleep_secs)
                continue

            if resp.status_code in (500, 502, 503, 504):
                wait = 15 * (attempt + 1)
                print(f"    Strava API {resp.status_code} on {endpoint}, retrying in {wait}s…")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()

        raise RuntimeError(f"Failed to GET {endpoint} after 3 attempts (last status: {resp.status_code})")

    # ------------------------------------------------------------------
    # Athlete
    # ------------------------------------------------------------------

    def download_athlete(self) -> int:
        """Fetch athlete profile + stats and upsert into athletes table."""
        print("\n📊  Downloading athlete profile…")
        athlete = self._get("/athlete")
        athlete_id = athlete["id"]

        print(f"    Fetching stats for athlete {athlete_id}…")
        stats = self._get(f"/athletes/{athlete_id}/stats")

        def totals(prefix: str, sport: str) -> Dict:
            key = f"{sport}_totals"
            t = stats.get(key, {})
            return {
                f"{prefix}_{sport}_totals_count": t.get("count"),
                f"{prefix}_{sport}_totals_distance": t.get("distance"),
                f"{prefix}_{sport}_totals_moving_time": t.get("moving_time"),
                f"{prefix}_{sport}_totals_elapsed_time": t.get("elapsed_time"),
                f"{prefix}_{sport}_totals_elevation_gain": t.get("elevation_gain"),
            }

        def recent_totals(sport: str) -> Dict:
            key = f"recent_{sport}_totals"
            t = stats.get(key, {})
            return {
                f"recent_{sport}_totals_count": t.get("count"),
                f"recent_{sport}_totals_distance": t.get("distance"),
                f"recent_{sport}_totals_moving_time": t.get("moving_time"),
                f"recent_{sport}_totals_elapsed_time": t.get("elapsed_time"),
                f"recent_{sport}_totals_elevation_gain": t.get("elevation_gain"),
                f"recent_{sport}_totals_achievement_count": t.get("achievement_count"),
            }

        row: Dict[str, Any] = {
            "id": athlete_id,
            "username": athlete.get("username"),
            "firstname": athlete.get("firstname"),
            "lastname": athlete.get("lastname"),
            "city": athlete.get("city"),
            "state": athlete.get("state"),
            "country": athlete.get("country"),
            "sex": athlete.get("sex"),
            "premium": int(bool(athlete.get("premium"))),
            "summit": int(bool(athlete.get("summit"))),
            "created_at": athlete.get("created_at"),
            "updated_at": athlete.get("updated_at"),
            "badge_type_id": athlete.get("badge_type_id"),
            "profile_medium": athlete.get("profile_medium"),
            "profile": athlete.get("profile"),
            "follower_count": athlete.get("follower_count"),
            "friend_count": athlete.get("friend_count"),
            "mutual_friend_count": athlete.get("mutual_friend_count"),
            "athlete_type": athlete.get("athlete_type"),
            "date_preference": athlete.get("date_preference"),
            "measurement_preference": athlete.get("measurement_preference"),
            "ftp": athlete.get("ftp"),
            "weight": athlete.get("weight"),
            "synced_at": _now(),
        }
        # Merge totals
        for prefix, sport in [("ytd", "ride"), ("ytd", "run"), ("ytd", "swim"),
                               ("all", "ride"), ("all", "run"), ("all", "swim")]:
            row.update(totals(prefix, sport))
        for sport in ["ride", "run", "swim"]:
            row.update(recent_totals(sport))

        with sqlite3.connect(self.db_path) as conn:
            _upsert(conn, "athletes", row)
            conn.commit()

        print(f"✅  Athlete: {athlete.get('firstname')} {athlete.get('lastname')}")
        return athlete_id

    # ------------------------------------------------------------------
    # Activities
    # ------------------------------------------------------------------

    def _activity_row(self, a: Dict) -> Dict[str, Any]:
        """Map a Strava activity dict to the activities table columns."""
        latlng_start = a.get("start_latlng") or []
        latlng_end = a.get("end_latlng") or []
        map_obj = a.get("map") or {}

        return {
            "id": a["id"],
            "resource_state": a.get("resource_state"),
            "athlete_id": (a.get("athlete") or {}).get("id") or a.get("athlete_id"),
            "name": a.get("name"),
            "description": a.get("description"),
            "type": a.get("type"),
            "sport_type": a.get("sport_type"),
            "workout_type": a.get("workout_type"),
            "start_date": a.get("start_date"),
            "start_date_local": a.get("start_date_local"),
            "timezone": a.get("timezone"),
            "utc_offset": a.get("utc_offset"),
            "distance": a.get("distance"),
            "moving_time": a.get("moving_time"),
            "elapsed_time": a.get("elapsed_time"),
            "total_elevation_gain": a.get("total_elevation_gain"),
            "elev_high": a.get("elev_high"),
            "elev_low": a.get("elev_low"),
            "average_speed": a.get("average_speed"),
            "max_speed": a.get("max_speed"),
            "has_heartrate": int(bool(a.get("has_heartrate"))),
            "average_heartrate": a.get("average_heartrate"),
            "max_heartrate": a.get("max_heartrate"),
            "heartrate_opt_out": int(bool(a.get("heartrate_opt_out"))),
            "device_watts": int(bool(a.get("device_watts"))),
            "average_watts": a.get("average_watts"),
            "max_watts": a.get("max_watts"),
            "weighted_average_watts": a.get("weighted_average_watts"),
            "kilojoules": a.get("kilojoules"),
            "average_cadence": a.get("average_cadence"),
            "average_temp": a.get("average_temp"),
            "start_lat": latlng_start[0] if len(latlng_start) >= 2 else None,
            "start_lng": latlng_start[1] if len(latlng_start) >= 2 else None,
            "end_lat": latlng_end[0] if len(latlng_end) >= 2 else None,
            "end_lng": latlng_end[1] if len(latlng_end) >= 2 else None,
            "map_id": map_obj.get("id"),
            "map_polyline": map_obj.get("polyline"),
            "map_summary_polyline": map_obj.get("summary_polyline"),
            "kudos_count": a.get("kudos_count", 0),
            "comment_count": a.get("comment_count", 0),
            "athlete_count": a.get("athlete_count", 1),
            "photo_count": a.get("photo_count", 0),
            "total_photo_count": a.get("total_photo_count", 0),
            "achievement_count": a.get("achievement_count", 0),
            "pr_count": a.get("pr_count", 0),
            "suffer_score": a.get("suffer_score"),
            "commute": int(bool(a.get("commute"))),
            "trainer": int(bool(a.get("trainer"))),
            "manual": int(bool(a.get("manual"))),
            "private": int(bool(a.get("private"))),
            "flagged": int(bool(a.get("flagged"))),
            "hide_from_home": int(bool(a.get("hide_from_home"))),
            "visibility": a.get("visibility"),
            "gear_id": a.get("gear_id"),
            "external_id": a.get("external_id"),
            "upload_id": a.get("upload_id"),
            "synced_at": _now(),
        }

    def download_activities(
        self,
        days_back: Optional[int] = None,
        since: Optional[str] = None,
    ) -> List[int]:
        """
        Fetch activity list and upsert into activities table.
        Returns list of newly inserted activity IDs.

        Priority order for the cutoff date:
          1. since (YYYY-MM-DD string, explicit date)
          2. days_back (relative days from today)
          3. incremental (latest activity already in DB, minus 1 day)
          4. STRAVA_START_DATE env var or 2-year default (fresh DB)
        """
        print("\n🏃  Downloading activities…")

        # Determine the 'after' cutoff timestamp
        after: Optional[int] = None
        if since is not None:
            try:
                cutoff = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
            except ValueError:
                raise ValueError(f"--since date '{since}' is not a valid YYYY-MM-DD date")
            after = int(cutoff.timestamp())
            print(f"    Fetching activities since {cutoff.date()} (--since)")
        elif days_back is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
            after = int(cutoff.timestamp())
            print(f"    Fetching activities since {cutoff.date()} ({days_back} days back)")
        else:
            # Incremental: use latest start_date in DB, minus 1 day for safety
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT MAX(start_date) FROM activities"
                ).fetchone()
                latest = row[0] if row and row[0] else None

            if latest:
                cutoff_dt = datetime.fromisoformat(
                    latest.replace("Z", "+00:00")
                ) - timedelta(days=1)
                after = int(cutoff_dt.timestamp())
                print(f"    Incremental sync from {cutoff_dt.date()}")
            else:
                # No activities yet — use STRAVA_START_DATE or 2 years
                start_str = os.getenv("STRAVA_START_DATE")
                if start_str:
                    cutoff_dt = datetime.fromisoformat(start_str).replace(
                        tzinfo=timezone.utc
                    )
                else:
                    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=730)
                after = int(cutoff_dt.timestamp())
                print(f"    Full sync from {cutoff_dt.date()}")

        new_ids: List[int] = []
        page = 1
        total_fetched = 0

        while True:
            params: Dict[str, Any] = {"per_page": 200, "page": page}
            if after:
                params["after"] = after

            batch = self._get("/athlete/activities", params=params)
            if not batch:
                break

            with sqlite3.connect(self.db_path) as conn:
                for a in batch:
                    activity_id = a["id"]
                    exists = conn.execute(
                        "SELECT 1 FROM activities WHERE id = ?", (activity_id,)
                    ).fetchone()
                    row = self._activity_row(a)
                    if not exists:
                        new_ids.append(activity_id)
                    else:
                        # Preserve detail_synced_at when updating summary
                        row.pop("detail_synced_at", None)
                        existing = conn.execute(
                            "SELECT detail_synced_at FROM activities WHERE id = ?",
                            (activity_id,),
                        ).fetchone()
                        if existing and existing[0]:
                            row["detail_synced_at"] = existing[0]
                    _upsert(conn, "activities", row)
                conn.commit()

            total_fetched += len(batch)
            print(f"    Page {page}: {len(batch)} activities ({total_fetched} total, {len(new_ids)} new)")
            page += 1
            time.sleep(0.3)

        print(f"✅  Activities: {total_fetched} fetched, {len(new_ids)} new")
        return new_ids

    # ------------------------------------------------------------------
    # Activity detail (laps, splits, segment efforts, zones)
    # ------------------------------------------------------------------

    def download_activity_details(self, activity_id: int) -> None:
        """Fetch full detail for one activity and update related tables."""
        detail = self._get(f"/activities/{activity_id}")

        with sqlite3.connect(self.db_path) as conn:
            # Update activities row with full detail fields (preserves existing)
            row = self._activity_row(detail)
            row["detail_synced_at"] = _now()
            _upsert(conn, "activities", row)

            # Laps
            conn.execute("DELETE FROM activity_laps WHERE activity_id = ?", (activity_id,))
            for lap in detail.get("laps") or []:
                conn.execute(
                    """INSERT OR REPLACE INTO activity_laps
                       (id, activity_id, resource_state, name, lap_index, split,
                        start_date, start_date_local, elapsed_time, moving_time,
                        distance, total_elevation_gain, average_speed, max_speed,
                        average_cadence, average_watts, device_watts,
                        average_heartrate, max_heartrate, pace_zone,
                        start_index, end_index)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        lap.get("id"), activity_id, lap.get("resource_state"),
                        lap.get("name"), lap.get("lap_index"), lap.get("split"),
                        lap.get("start_date"), lap.get("start_date_local"),
                        lap.get("elapsed_time"), lap.get("moving_time"),
                        lap.get("distance"), lap.get("total_elevation_gain"),
                        lap.get("average_speed"), lap.get("max_speed"),
                        lap.get("average_cadence"), lap.get("average_watts"),
                        int(bool(lap.get("device_watts"))),
                        lap.get("average_heartrate"), lap.get("max_heartrate"),
                        lap.get("pace_zone"), lap.get("start_index"), lap.get("end_index"),
                    ),
                )

            # Metric splits
            conn.execute(
                "DELETE FROM activity_splits_metric WHERE activity_id = ?", (activity_id,)
            )
            for sp in detail.get("splits_metric") or []:
                conn.execute(
                    """INSERT OR REPLACE INTO activity_splits_metric
                       (activity_id, split, distance, elapsed_time, moving_time,
                        elevation_difference, pace_zone, average_speed,
                        average_heartrate, average_cadence, average_grade_adjusted_speed)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        activity_id, sp.get("split"), sp.get("distance"),
                        sp.get("elapsed_time"), sp.get("moving_time"),
                        sp.get("elevation_difference"), sp.get("pace_zone"),
                        sp.get("average_speed"), sp.get("average_heartrate"),
                        sp.get("average_cadence"), sp.get("average_grade_adjusted_speed"),
                    ),
                )

            # Segment efforts
            conn.execute(
                "DELETE FROM segment_efforts WHERE activity_id = ?", (activity_id,)
            )
            for se in detail.get("segment_efforts") or []:
                seg = se.get("segment") or {}
                seg_id = seg.get("id")

                # Upsert the segment itself
                if seg_id:
                    latlng_s = seg.get("start_latlng") or []
                    latlng_e = seg.get("end_latlng") or []
                    seg_row = {
                        "id": seg_id,
                        "resource_state": seg.get("resource_state"),
                        "name": seg.get("name"),
                        "activity_type": seg.get("activity_type"),
                        "distance": seg.get("distance"),
                        "average_grade": seg.get("average_grade"),
                        "maximum_grade": seg.get("maximum_grade"),
                        "elevation_high": seg.get("elevation_high"),
                        "elevation_low": seg.get("elevation_low"),
                        "total_elevation_gain": seg.get("total_elevation_gain"),
                        "start_lat": latlng_s[0] if len(latlng_s) >= 2 else None,
                        "start_lng": latlng_s[1] if len(latlng_s) >= 2 else None,
                        "end_lat": latlng_e[0] if len(latlng_e) >= 2 else None,
                        "end_lng": latlng_e[1] if len(latlng_e) >= 2 else None,
                        "climb_category": seg.get("climb_category"),
                        "city": seg.get("city"),
                        "state": seg.get("state"),
                        "country": seg.get("country"),
                        "private": int(bool(seg.get("private"))),
                        "hazardous": int(bool(seg.get("hazardous"))),
                        "starred": int(bool(seg.get("starred"))),
                        "created_at": seg.get("created_at"),
                        "updated_at": seg.get("updated_at"),
                        "synced_at": _now(),
                    }
                    _upsert(conn, "segments", seg_row)

                achievements = se.get("achievements") or []
                conn.execute(
                    """INSERT OR REPLACE INTO segment_efforts
                       (id, activity_id, segment_id, name,
                        start_date, start_date_local, elapsed_time, moving_time,
                        distance, average_cadence, average_watts, device_watts,
                        average_heartrate, max_heartrate,
                        start_index, end_index, pr_rank, achievements, hidden)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        se.get("id"), activity_id, seg_id, se.get("name"),
                        se.get("start_date"), se.get("start_date_local"),
                        se.get("elapsed_time"), se.get("moving_time"),
                        se.get("distance"), se.get("average_cadence"),
                        se.get("average_watts"),
                        int(bool(se.get("device_watts"))),
                        se.get("average_heartrate"), se.get("max_heartrate"),
                        se.get("start_index"), se.get("end_index"),
                        se.get("pr_rank"),
                        json.dumps(achievements) if achievements else None,
                        int(bool(se.get("hidden"))),
                    ),
                )

            conn.commit()

        # Zones (separate API call)
        try:
            zones_data = self._get(f"/activities/{activity_id}/zones")
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "DELETE FROM activity_zones WHERE activity_id = ?", (activity_id,)
                )
                for zone_group in zones_data or []:
                    zone_type = zone_group.get("type", "unknown")
                    sensor_based = int(bool(zone_group.get("sensor_based")))
                    for idx, bucket in enumerate(zone_group.get("distribution_buckets") or []):
                        conn.execute(
                            """INSERT OR REPLACE INTO activity_zones
                               (activity_id, zone_type, sensor_based,
                                zone_index, zone_min, zone_max, time)
                               VALUES (?,?,?,?,?,?,?)""",
                            (
                                activity_id, zone_type, sensor_based,
                                idx, bucket.get("min"), bucket.get("max"),
                                bucket.get("time"),
                            ),
                        )
                conn.commit()
        except Exception as e:
            # Zones may not be available for all activity types
            print(f"    ⚠️  Zones not available for activity {activity_id}: {e}")

    def get_activities_without_detail(self) -> List[int]:
        """Return IDs of activities that haven't had detail fetched yet."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM activities WHERE detail_synced_at IS NULL ORDER BY start_date DESC"
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Gear
    # ------------------------------------------------------------------

    def get_all_gear_ids(self) -> List[str]:
        """Collect unique gear_ids referenced in activities."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT gear_id FROM activities WHERE gear_id IS NOT NULL"
            ).fetchall()
        return [r[0] for r in rows]

    def download_gear(self, gear_ids: List[str]) -> None:
        """Fetch each gear item and upsert into gear table."""
        if not gear_ids:
            return
        print(f"\n🚴  Downloading {len(gear_ids)} gear item(s)…")
        with sqlite3.connect(self.db_path) as conn:
            for gear_id in gear_ids:
                try:
                    g = self._get(f"/gear/{gear_id}")
                    row = {
                        "id": g["id"],
                        "resource_state": g.get("resource_state"),
                        "primary_gear": int(bool(g.get("primary"))),
                        "name": g.get("name"),
                        "brand_name": g.get("brand_name"),
                        "model_name": g.get("model_name"),
                        "frame_type": g.get("frame_type"),
                        "description": g.get("description"),
                        "distance": g.get("distance"),
                        "gear_type": "bike" if gear_id.startswith("b") else "shoe",
                        "retired": int(bool(g.get("retired"))),
                        "synced_at": _now(),
                    }
                    _upsert(conn, "gear", row)
                    print(f"    ✅  Gear: {g.get('name')} ({gear_id})")
                    time.sleep(0.2)
                except Exception as e:
                    print(f"    ⚠️  Could not fetch gear {gear_id}: {e}")
            conn.commit()

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    def download_routes(self, athlete_id: int) -> None:
        """Fetch all routes for the athlete."""
        print(f"\n🗺️   Downloading routes…")
        page = 1
        total = 0
        with sqlite3.connect(self.db_path) as conn:
            while True:
                batch = self._get(
                    f"/athletes/{athlete_id}/routes",
                    params={"per_page": 200, "page": page},
                )
                if not batch:
                    break
                for r in batch:
                    map_obj = r.get("map") or {}
                    row = {
                        "id": r["id"],
                        "resource_state": r.get("resource_state"),
                        "athlete_id": athlete_id,
                        "name": r.get("name"),
                        "description": r.get("description"),
                        "distance": r.get("distance"),
                        "elevation_gain": r.get("elevation_gain"),
                        "route_type": r.get("type"),
                        "sub_type": r.get("sub_type"),
                        "private": int(bool(r.get("private"))),
                        "starred": int(bool(r.get("starred"))),
                        "timestamp": r.get("timestamp"),
                        "map_polyline": map_obj.get("polyline"),
                        "map_summary_polyline": map_obj.get("summary_polyline"),
                        "synced_at": _now(),
                    }
                    _upsert(conn, "routes", row)
                total += len(batch)
                page += 1
                time.sleep(0.3)
            conn.commit()
        print(f"✅  Routes: {total}")

    # ------------------------------------------------------------------
    # Starred segments
    # ------------------------------------------------------------------

    def download_starred_segments(self, athlete_id: int) -> None:
        """Fetch starred segments and upsert segments + starred_segments tables."""
        print("\n⭐  Downloading starred segments…")
        page = 1
        total = 0
        with sqlite3.connect(self.db_path) as conn:
            # Clear old starred data and repopulate
            conn.execute(
                "DELETE FROM starred_segments WHERE athlete_id = ?", (athlete_id,)
            )
            while True:
                batch = self._get(
                    "/segments/starred",
                    params={"per_page": 200, "page": page},
                )
                if not batch:
                    break
                for seg in batch:
                    latlng_s = seg.get("start_latlng") or []
                    latlng_e = seg.get("end_latlng") or []
                    map_obj = seg.get("map") or {}
                    seg_row = {
                        "id": seg["id"],
                        "resource_state": seg.get("resource_state"),
                        "name": seg.get("name"),
                        "activity_type": seg.get("activity_type"),
                        "distance": seg.get("distance"),
                        "average_grade": seg.get("average_grade"),
                        "maximum_grade": seg.get("maximum_grade"),
                        "elevation_high": seg.get("elevation_high"),
                        "elevation_low": seg.get("elevation_low"),
                        "total_elevation_gain": seg.get("total_elevation_gain"),
                        "start_lat": latlng_s[0] if len(latlng_s) >= 2 else None,
                        "start_lng": latlng_s[1] if len(latlng_s) >= 2 else None,
                        "end_lat": latlng_e[0] if len(latlng_e) >= 2 else None,
                        "end_lng": latlng_e[1] if len(latlng_e) >= 2 else None,
                        "climb_category": seg.get("climb_category"),
                        "city": seg.get("city"),
                        "state": seg.get("state"),
                        "country": seg.get("country"),
                        "private": int(bool(seg.get("private"))),
                        "hazardous": int(bool(seg.get("hazardous"))),
                        "starred": 1,
                        "effort_count": seg.get("effort_count"),
                        "athlete_count": seg.get("athlete_count"),
                        "star_count": seg.get("star_count"),
                        "map_polyline": map_obj.get("polyline"),
                        "synced_at": _now(),
                    }
                    _upsert(conn, "segments", seg_row)
                    conn.execute(
                        """INSERT OR REPLACE INTO starred_segments
                           (athlete_id, segment_id, synced_at)
                           VALUES (?, ?, ?)""",
                        (athlete_id, seg["id"], _now()),
                    )
                total += len(batch)
                page += 1
                time.sleep(0.3)
            conn.commit()
        print(f"✅  Starred segments: {total}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def print_summary(self) -> None:
        print("\n" + "=" * 60)
        print("SYNC SUMMARY")
        print("=" * 60)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            row = conn.execute(
                """SELECT COUNT(*) as total,
                          MIN(start_date_local) as earliest,
                          MAX(start_date_local) as latest,
                          ROUND(SUM(distance)/1000.0, 1) as total_km,
                          ROUND(SUM(moving_time)/3600.0, 1) as total_hours,
                          ROUND(SUM(total_elevation_gain), 0) as total_elev_m,
                          SUM(CASE WHEN detail_synced_at IS NOT NULL THEN 1 ELSE 0 END) as with_detail
                   FROM activities"""
            ).fetchone()

            print(f"Activities : {row['total']} total  ({row['with_detail']} with full detail)")
            print(f"Date range : {row['earliest'][:10] if row['earliest'] else '?'} → {row['latest'][:10] if row['latest'] else '?'}")
            print(f"Distance   : {row['total_km']} km")
            print(f"Duration   : {row['total_hours']} hours")
            print(f"Elevation  : {row['total_elev_m']} m")

            print("\nBy sport type:")
            for r in conn.execute(
                """SELECT sport_type, COUNT(*) as cnt,
                          ROUND(SUM(distance)/1000.0,1) as km
                   FROM activities GROUP BY sport_type ORDER BY cnt DESC"""
            ).fetchall():
                print(f"  {r['sport_type'] or 'Unknown':25s} {r['cnt']:5d} activities  {r['km'] or 0:8.1f} km")

            laps = conn.execute("SELECT COUNT(*) FROM activity_laps").fetchone()[0]
            segs = conn.execute("SELECT COUNT(*) FROM segment_efforts").fetchone()[0]
            gear_count = conn.execute("SELECT COUNT(*) FROM gear").fetchone()[0]
            routes = conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
            starred = conn.execute("SELECT COUNT(*) FROM starred_segments").fetchone()[0]
            print(f"\nLaps       : {laps}")
            print(f"Seg efforts: {segs}")
            print(f"Gear       : {gear_count}")
            print(f"Routes     : {routes}")
            print(f"Starred seg: {starred}")
        print("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Download Strava data to SQLite")

    cutoff_group = parser.add_mutually_exclusive_group()
    cutoff_group.add_argument(
        "--since", metavar="DATE",
        help="Sync activities on or after this date (YYYY-MM-DD). "
             "Overrides incremental logic and --days."
    )
    cutoff_group.add_argument(
        "--days", type=int, default=None,
        help="Sync activities from this many days back (overrides incremental logic)"
    )

    parser.add_argument(
        "--full", action="store_true",
        help="Re-fetch detail (laps/zones) for ALL activities, not just new ones"
    )
    parser.add_argument(
        "--db", default=os.getenv("STRAVA_DB_PATH", str(DEFAULT_DB_PATH)),
        help="Path to SQLite database"
    )
    args = parser.parse_args()

    downloader = StravaDownloader(db_path=args.db)

    athlete_id = downloader.download_athlete()

    new_ids = downloader.download_activities(days_back=args.days, since=args.since)

    # Decide which activities need detail fetched
    if args.full:
        ids_for_detail = downloader.get_activities_without_detail()
        print(f"\n📋  --full mode: fetching detail for {len(ids_for_detail)} activities")
    else:
        ids_for_detail = new_ids
        if ids_for_detail:
            print(f"\n📋  Fetching detail for {len(ids_for_detail)} new activities")

    for i, activity_id in enumerate(ids_for_detail, 1):
        if i % 10 == 0 or i == len(ids_for_detail):
            print(f"    Detail {i}/{len(ids_for_detail)} (activity {activity_id})")
        try:
            downloader.download_activity_details(activity_id)
        except Exception as e:
            print(f"    ⚠️  Failed detail for {activity_id}: {e}")
        time.sleep(0.6)  # ~100 req/15min = ~9 req/min, 0.6s gives comfortable headroom

    gear_ids = downloader.get_all_gear_ids()
    downloader.download_gear(gear_ids)
    downloader.download_routes(athlete_id)
    downloader.download_starred_segments(athlete_id)

    downloader.print_summary()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"\n❌  {e}", file=sys.stderr)
        sys.exit(1)
