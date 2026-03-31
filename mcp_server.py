#!/usr/bin/env python3
"""
MCP Server for Strava Activities Database

Exposes the local Strava SQLite database to MCP clients via resources and tools.
Supports both stdio (Claude Desktop) and HTTP streamable transports with bearer
token authentication.

Usage:
    python mcp_server.py                       # stdio (default)
    python mcp_server.py --transport http      # HTTP on STRAVA_MCP_HTTP_PORT
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

from mcp.server.fastmcp import FastMCP

# ── Logging to stderr only (keep stdout clean for STDIO MCP transport) ──────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# ── Server ───────────────────────────────────────────────────────────────────
mcp = FastMCP("strava-activities")

DEFAULT_DB = os.path.join(os.path.dirname(__file__), "strava_activities.db")
DB_PATH = os.getenv("STRAVA_DB_PATH", DEFAULT_DB)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _row(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return dict(row)


def _rows(rows) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Resources
# ─────────────────────────────────────────────────────────────────────────────

@mcp.resource(
    "strava://athlete",
    name="Athlete Profile",
    description="Strava athlete profile with lifetime and YTD totals",
    mime_type="application/json",
)
def resource_athlete() -> str:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM athletes ORDER BY synced_at DESC LIMIT 1").fetchone()
        return json.dumps(_row(row), indent=2, default=str)


@mcp.resource(
    "strava://activities",
    name="All Activities",
    description="All Strava activities ordered by date descending",
    mime_type="application/json",
)
def resource_activities() -> str:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_summary ORDER BY start_date_local DESC"
        ).fetchall()
        return json.dumps(_rows(rows), indent=2, default=str)


@mcp.resource(
    "strava://stats/summary",
    name="Activity Statistics",
    description="Aggregate statistics across all activities grouped by sport type",
    mime_type="application/json",
)
def resource_stats_summary() -> str:
    with get_db() as conn:
        overall = _row(conn.execute("""
            SELECT
                COUNT(*)                                    AS total_activities,
                COUNT(DISTINCT sport_type)                  AS unique_sport_types,
                MIN(start_date_local)                       AS earliest,
                MAX(start_date_local)                       AS latest,
                ROUND(SUM(distance)/1000.0, 1)             AS total_km,
                ROUND(SUM(moving_time)/3600.0, 1)          AS total_hours,
                ROUND(SUM(total_elevation_gain), 0)        AS total_elevation_m,
                ROUND(AVG(average_heartrate), 0)           AS avg_heartrate,
                ROUND(AVG(average_watts), 0)               AS avg_watts,
                COUNT(CASE WHEN average_watts > 0 THEN 1 END) AS activities_with_power,
                COUNT(CASE WHEN has_heartrate = 1 THEN 1 END) AS activities_with_hr,
                SUM(kudos_count)                            AS total_kudos,
                SUM(achievement_count)                      AS total_achievements,
                SUM(pr_count)                               AS total_prs
            FROM activities
        """).fetchone())

        by_sport = _rows(conn.execute("""
            SELECT
                sport_type,
                COUNT(*)                            AS count,
                ROUND(SUM(distance)/1000.0, 1)     AS total_km,
                ROUND(SUM(moving_time)/3600.0, 1)  AS total_hours,
                ROUND(SUM(total_elevation_gain),0) AS total_elevation_m,
                ROUND(AVG(average_heartrate), 0)   AS avg_heartrate,
                ROUND(AVG(average_watts), 0)       AS avg_watts
            FROM activities
            WHERE sport_type IS NOT NULL
            GROUP BY sport_type
            ORDER BY count DESC
        """).fetchall())

        return json.dumps({"summary": overall, "by_sport_type": by_sport},
                          indent=2, default=str)


@mcp.resource(
    "strava://stats/monthly",
    name="Monthly Statistics",
    description="Activity statistics aggregated by month and sport type",
    mime_type="application/json",
)
def resource_stats_monthly() -> str:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM monthly_stats LIMIT 200"
        ).fetchall()
        return json.dumps(_rows(rows), indent=2, default=str)


@mcp.resource(
    "strava://activities/recent",
    name="Recent Activities",
    description="Activities from the last 30 days",
    mime_type="application/json",
)
def resource_recent() -> str:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_summary WHERE start_date_local >= ? ORDER BY start_date_local DESC",
            (cutoff,),
        ).fetchall()
        return json.dumps(_rows(rows), indent=2, default=str)


@mcp.resource(
    "strava://gear",
    name="Gear",
    description="All gear (bikes and shoes) with total distance logged",
    mime_type="application/json",
)
def resource_gear() -> str:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM gear ORDER BY gear_type, primary_gear DESC, name"
        ).fetchall()
        return json.dumps(_rows(rows), indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
# Tools
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def query_activities(
    sport_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    min_distance_km: Optional[float] = None,
    max_distance_km: Optional[float] = None,
    min_moving_time_min: Optional[float] = None,
    has_power_data: Optional[bool] = None,
    has_hr_data: Optional[bool] = None,
    commute: Optional[bool] = None,
    limit: int = 50,
    order_by: str = "start_date_local",
    order_desc: bool = True,
) -> str:
    """
    Query activities with flexible filters.

    Args:
        sport_type: Filter by sport type (e.g. 'Ride', 'Run', 'VirtualRide', 'TrailRun').
        start_date: Earliest start date (YYYY-MM-DD).
        end_date: Latest start date (YYYY-MM-DD).
        min_distance_km: Minimum distance in km.
        max_distance_km: Maximum distance in km.
        min_moving_time_min: Minimum moving time in minutes.
        has_power_data: If True, only activities with power data.
        has_hr_data: If True, only activities with heart rate data.
        commute: If True/False, filter by commute flag.
        limit: Maximum number of results (default 50, max 500).
        order_by: Column to sort by (default 'start_date_local').
        order_desc: Sort descending if True (default True).
    """
    limit = min(limit, 500)
    conditions = []
    params: List[Any] = []

    allowed_order_cols = {
        "start_date_local", "distance", "moving_time", "total_elevation_gain",
        "average_heartrate", "average_watts", "weighted_average_watts",
        "suffer_score", "kudos_count", "achievement_count", "pr_count",
    }
    if order_by not in allowed_order_cols:
        order_by = "start_date_local"

    if sport_type:
        conditions.append("sport_type = ?")
        params.append(sport_type)
    if start_date:
        conditions.append("start_date_local >= ?")
        params.append(start_date)
    if end_date:
        params.append(end_date + "T23:59:59")
        conditions.append("start_date_local <= ?")
    if min_distance_km is not None:
        conditions.append("distance >= ?")
        params.append(min_distance_km * 1000)
    if max_distance_km is not None:
        conditions.append("distance <= ?")
        params.append(max_distance_km * 1000)
    if min_moving_time_min is not None:
        conditions.append("moving_time >= ?")
        params.append(int(min_moving_time_min * 60))
    if has_power_data is True:
        conditions.append("average_watts IS NOT NULL AND average_watts > 0")
    elif has_power_data is False:
        conditions.append("(average_watts IS NULL OR average_watts = 0)")
    if has_hr_data is True:
        conditions.append("has_heartrate = 1")
    elif has_hr_data is False:
        conditions.append("has_heartrate = 0")
    if commute is not None:
        conditions.append("commute = ?")
        params.append(1 if commute else 0)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    direction = "DESC" if order_desc else "ASC"

    sql = f"""
        SELECT * FROM activity_summary
        {where}
        ORDER BY {order_by} {direction}
        LIMIT {limit}
    """

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM activities {where}", params
        ).fetchone()[0]

    return json.dumps(
        {"total_matching": total, "returned": len(rows), "activities": _rows(rows)},
        indent=2, default=str,
    )


@mcp.tool()
def get_activity_details(activity_id: int) -> str:
    """
    Get full details for a single activity including laps, zone data, and segment efforts.

    Args:
        activity_id: Strava activity ID.
    """
    with get_db() as conn:
        activity = _row(conn.execute(
            "SELECT * FROM activities WHERE id = ?", (activity_id,)
        ).fetchone())
        if not activity:
            return json.dumps({"error": f"Activity {activity_id} not found"})

        # Derived metrics
        dist_km = (activity["distance"] or 0) / 1000
        move_min = (activity["moving_time"] or 0) / 60
        if dist_km > 0 and move_min > 0 and activity.get("sport_type") in (
            "Run", "TrailRun", "Walk", "Hike"
        ):
            activity["pace_min_per_km"] = round(move_min / dist_km, 2)

        laps = _rows(conn.execute(
            "SELECT * FROM activity_laps WHERE activity_id = ? ORDER BY lap_index",
            (activity_id,),
        ).fetchall())

        splits = _rows(conn.execute(
            "SELECT * FROM activity_splits_metric WHERE activity_id = ? ORDER BY split",
            (activity_id,),
        ).fetchall())

        zones = _rows(conn.execute(
            """SELECT zone_type, zone_index, zone_min, zone_max, time
               FROM activity_zones WHERE activity_id = ? ORDER BY zone_type, zone_index""",
            (activity_id,),
        ).fetchall())

        efforts = _rows(conn.execute(
            """SELECT se.*, s.name as segment_name, s.distance as segment_distance,
                      s.average_grade, s.climb_category
               FROM segment_efforts se
               LEFT JOIN segments s ON se.segment_id = s.id
               WHERE se.activity_id = ?
               ORDER BY se.start_index""",
            (activity_id,),
        ).fetchall())

    return json.dumps(
        {
            "activity": activity,
            "laps": laps,
            "splits_metric": splits,
            "zones": zones,
            "segment_efforts": efforts,
        },
        indent=2, default=str,
    )


@mcp.tool()
def get_segment_efforts(
    segment_id: Optional[int] = None,
    activity_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 100,
) -> str:
    """
    Query segment efforts, optionally filtered by segment or activity.

    Args:
        segment_id: Filter by Strava segment ID (show progression on a specific segment).
        activity_id: Filter by activity ID (show all efforts in one activity).
        start_date: Earliest effort date (YYYY-MM-DD).
        end_date: Latest effort date (YYYY-MM-DD).
        limit: Maximum results (default 100).
    """
    conditions = []
    params: List[Any] = []

    if segment_id:
        conditions.append("se.segment_id = ?")
        params.append(segment_id)
    if activity_id:
        conditions.append("se.activity_id = ?")
        params.append(activity_id)
    if start_date:
        conditions.append("se.start_date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("se.start_date <= ?")
        params.append(end_date + "T23:59:59")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT se.*,
               s.name AS segment_name,
               s.distance AS segment_distance_m,
               s.average_grade,
               s.climb_category,
               s.city, s.country
        FROM segment_efforts se
        LEFT JOIN segments s ON se.segment_id = s.id
        {where}
        ORDER BY se.start_date DESC
        LIMIT {min(limit, 500)}
    """

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    return json.dumps({"count": len(rows), "efforts": _rows(rows)}, indent=2, default=str)


@mcp.tool()
def get_power_analysis(
    sport_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 20,
) -> str:
    """
    Power statistics and recent power activities.

    Args:
        sport_type: Filter by sport type (e.g. 'Ride', 'VirtualRide').
        start_date: Earliest date (YYYY-MM-DD).
        end_date: Latest date (YYYY-MM-DD).
        limit: Number of recent power activities to return.
    """
    conditions = ["average_watts IS NOT NULL AND average_watts > 0"]
    params: List[Any] = []

    if sport_type:
        conditions.append("sport_type = ?")
        params.append(sport_type)
    if start_date:
        conditions.append("start_date_local >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("start_date_local <= ?")
        params.append(end_date + "T23:59:59")

    where = "WHERE " + " AND ".join(conditions)

    with get_db() as conn:
        stats = _row(conn.execute(f"""
            SELECT
                COUNT(*)                                        AS activities_with_power,
                ROUND(AVG(average_watts), 0)                   AS avg_watts,
                MAX(max_watts)                                  AS max_watts_ever,
                ROUND(AVG(weighted_average_watts), 0)          AS avg_weighted_watts,
                MAX(weighted_average_watts)                     AS max_weighted_watts,
                ROUND(AVG(average_watts) * 0.95, 0)           AS estimated_ftp,
                ROUND(AVG(kilojoules), 0)                      AS avg_kilojoules,
                SUM(kilojoules)                                 AS total_kilojoules
            FROM activities {where}
        """, params).fetchone())

        recent = _rows(conn.execute(f"""
            SELECT id, name, sport_type, start_date_local,
                   ROUND(distance/1000.0, 1) AS distance_km,
                   ROUND(moving_time/60.0, 0) AS moving_time_min,
                   average_watts, max_watts, weighted_average_watts, kilojoules
            FROM activities {where}
            ORDER BY start_date_local DESC
            LIMIT {min(limit, 100)}
        """, params).fetchall())

    return json.dumps({"power_stats": stats, "recent_power_activities": recent},
                      indent=2, default=str)


@mcp.tool()
def get_training_trends(
    sport_type: Optional[str] = None,
    period: str = "month",
    metric: str = "distance",
    limit: int = 24,
) -> str:
    """
    Training load trends aggregated by week or month.

    Args:
        sport_type: Filter by sport type (e.g. 'Run', 'Ride'). None = all sports.
        period: Aggregation period — 'week' or 'month'.
        metric: Metric to aggregate — 'distance_km', 'moving_time_hours',
                'elevation_m', 'average_heartrate', 'average_watts', 'count'.
        limit: Number of periods to return (most recent first).
    """
    if period == "week":
        date_expr = "strftime('%Y-W%W', start_date_local)"
    else:
        date_expr = "strftime('%Y-%m', start_date_local)"

    metric_map = {
        "distance_km": "ROUND(SUM(distance)/1000.0, 1)",
        "moving_time_hours": "ROUND(SUM(moving_time)/3600.0, 2)",
        "elevation_m": "ROUND(SUM(total_elevation_gain), 0)",
        "average_heartrate": "ROUND(AVG(average_heartrate), 0)",
        "average_watts": "ROUND(AVG(CASE WHEN average_watts > 0 THEN average_watts END), 0)",
        "count": "COUNT(*)",
    }
    agg_expr = metric_map.get(metric, metric_map["distance_km"])

    conditions = ["start_date_local IS NOT NULL"]
    params: List[Any] = []
    if sport_type:
        conditions.append("sport_type = ?")
        params.append(sport_type)

    where = "WHERE " + " AND ".join(conditions)

    sql = f"""
        SELECT
            {date_expr}                AS period,
            sport_type,
            COUNT(*)                   AS activity_count,
            ROUND(SUM(distance)/1000.0, 1)          AS total_km,
            ROUND(SUM(moving_time)/3600.0, 2)       AS total_hours,
            ROUND(SUM(total_elevation_gain), 0)     AS total_elevation_m,
            ROUND(AVG(average_heartrate), 0)        AS avg_heartrate,
            ROUND(AVG(CASE WHEN average_watts > 0 THEN average_watts END), 0) AS avg_watts,
            {agg_expr}                 AS metric_value
        FROM activities
        {where}
        GROUP BY period{', sport_type' if not sport_type else ''}
        ORDER BY period DESC
        LIMIT {min(limit, 200)}
    """

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    return json.dumps(
        {"period": period, "metric": metric, "sport_type": sport_type, "trends": _rows(rows)},
        indent=2, default=str,
    )


@mcp.tool()
def get_gear_stats() -> str:
    """
    Equipment usage statistics — total distance logged per gear item.
    """
    with get_db() as conn:
        gear_rows = _rows(conn.execute("SELECT * FROM gear ORDER BY distance DESC").fetchall())

        usage = _rows(conn.execute("""
            SELECT
                g.id, g.name, g.gear_type, g.brand_name, g.model_name,
                COUNT(a.id)                         AS activity_count,
                ROUND(SUM(a.distance)/1000.0, 1)   AS activities_km,
                MIN(a.start_date_local)             AS first_used,
                MAX(a.start_date_local)             AS last_used
            FROM gear g
            LEFT JOIN activities a ON a.gear_id = g.id
            GROUP BY g.id
            ORDER BY activities_km DESC
        """).fetchall())

    return json.dumps({"gear": gear_rows, "usage_by_gear": usage}, indent=2, default=str)


@mcp.tool()
def get_routes(
    route_type: Optional[int] = None,
    starred_only: bool = False,
    limit: int = 50,
) -> str:
    """
    List saved routes.

    Args:
        route_type: Filter by route type — 1 for ride, 2 for run.
        starred_only: If True, only return starred routes.
        limit: Maximum results (default 50).
    """
    conditions = []
    params: List[Any] = []

    if route_type is not None:
        conditions.append("route_type = ?")
        params.append(route_type)
    if starred_only:
        conditions.append("starred = 1")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT id, name, description,
                       ROUND(distance/1000.0, 1) AS distance_km,
                       ROUND(elevation_gain, 0) AS elevation_gain_m,
                       route_type, sub_type, private, starred, timestamp, synced_at
                FROM routes {where} ORDER BY timestamp DESC LIMIT {min(limit, 200)}""",
            params,
        ).fetchall()

    return json.dumps({"count": len(rows), "routes": _rows(rows)}, indent=2, default=str)


@mcp.tool()
def execute_sql(query: str, limit: int = 100) -> str:
    """
    Run a custom SELECT query against the Strava database.

    Available tables: activities, athletes, activity_laps, activity_splits_metric,
    segment_efforts, segments, starred_segments, gear, routes, activity_zones.
    Available views: activity_summary, monthly_stats.

    Only SELECT statements are permitted.

    Args:
        query: SQL SELECT query.
        limit: Maximum rows to return (default 100, max 1000).
    """
    stripped = query.strip().upper()
    if not stripped.startswith("SELECT"):
        return json.dumps({"error": "Only SELECT queries are permitted"})

    # Strip trailing semicolon and inject LIMIT if missing
    query = query.strip().rstrip(";")
    if "LIMIT" not in stripped:
        query += f" LIMIT {min(limit, 1000)}"

    try:
        with get_db() as conn:
            rows = conn.execute(query).fetchall()
        return json.dumps({"count": len(rows), "rows": _rows(rows)}, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Transport
# ─────────────────────────────────────────────────────────────────────────────

def run_stdio() -> None:
    if not os.path.exists(DB_PATH):
        logger.error(f"Database not found: {DB_PATH}")
        logger.error("Run strava_downloader.py first to populate the database.")
        sys.exit(1)
    mcp.run()


def main_http() -> None:
    """Run over Streamable HTTP with bearer token authentication."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.authentication import AuthenticationMiddleware
    from starlette.routing import Mount

    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
    from mcp.server.auth.provider import AccessToken
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    if not os.path.exists(DB_PATH):
        logger.error(f"Database not found: {DB_PATH}")
        logger.error("Run strava_downloader.py first to populate the database.")
        sys.exit(1)

    auth_token = os.getenv("STRAVA_MCP_AUTH_TOKEN")
    if not auth_token:
        logger.error("STRAVA_MCP_AUTH_TOKEN is required for HTTP transport")
        sys.exit(1)

    host = os.getenv("STRAVA_MCP_HTTP_HOST", "0.0.0.0")
    port = int(os.getenv("STRAVA_MCP_HTTP_PORT", "8080"))

    class StaticTokenVerifier:
        def __init__(self, expected: str):
            self.expected = expected

        async def verify_token(self, token: str) -> Optional[AccessToken]:
            if token == self.expected:
                return AccessToken(
                    token=token,
                    client_id="static",
                    scopes=["mcp:access"],
                    expires_at=None,
                )
            return None

    verifier = StaticTokenVerifier(auth_token)
    session_manager = StreamableHTTPSessionManager(app=mcp._mcp_server, stateless=True)

    @asynccontextmanager
    async def lifespan(app):
        async with session_manager.run():
            yield

    mcp_app = RequireAuthMiddleware(
        session_manager.handle_request,
        required_scopes=["mcp:access"],
    )

    app = Starlette(
        routes=[Mount("/mcp", app=mcp_app)],
        middleware=[
            Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(verifier)),
        ],
        lifespan=lifespan,
    )

    logger.info(f"Starting Strava MCP HTTP server on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Strava MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.getenv("STRAVA_MCP_TRANSPORT", "http"),
        help="Transport mode (default: stdio)",
    )
    args = parser.parse_args()

    if args.transport == "http":
        main_http()
    else:
        run_stdio()
