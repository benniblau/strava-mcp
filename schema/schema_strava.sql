-- Strava Activity Database Schema
-- Matches Strava API v3 response structure
-- https://developers.strava.com/docs/reference/

-- Athlete profile (single row, upserted on each sync)
CREATE TABLE IF NOT EXISTS athletes (
    id INTEGER PRIMARY KEY,
    username TEXT,
    firstname TEXT,
    lastname TEXT,
    city TEXT,
    state TEXT,
    country TEXT,
    sex TEXT,
    premium INTEGER DEFAULT 0,
    summit INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    badge_type_id INTEGER,
    profile_medium TEXT,
    profile TEXT,
    follower_count INTEGER,
    friend_count INTEGER,
    mutual_friend_count INTEGER,
    athlete_type INTEGER,
    date_preference TEXT,
    measurement_preference TEXT,
    ftp INTEGER,
    weight REAL,

    -- YTD totals (from /athletes/{id}/stats)
    ytd_ride_totals_count INTEGER,
    ytd_ride_totals_distance REAL,
    ytd_ride_totals_moving_time INTEGER,
    ytd_ride_totals_elapsed_time INTEGER,
    ytd_ride_totals_elevation_gain REAL,
    ytd_run_totals_count INTEGER,
    ytd_run_totals_distance REAL,
    ytd_run_totals_moving_time INTEGER,
    ytd_run_totals_elapsed_time INTEGER,
    ytd_run_totals_elevation_gain REAL,
    ytd_swim_totals_count INTEGER,
    ytd_swim_totals_distance REAL,
    ytd_swim_totals_moving_time INTEGER,
    ytd_swim_totals_elapsed_time INTEGER,
    ytd_swim_totals_elevation_gain REAL,

    -- All-time totals
    all_ride_totals_count INTEGER,
    all_ride_totals_distance REAL,
    all_ride_totals_moving_time INTEGER,
    all_ride_totals_elapsed_time INTEGER,
    all_ride_totals_elevation_gain REAL,
    all_run_totals_count INTEGER,
    all_run_totals_distance REAL,
    all_run_totals_moving_time INTEGER,
    all_run_totals_elapsed_time INTEGER,
    all_run_totals_elevation_gain REAL,
    all_swim_totals_count INTEGER,
    all_swim_totals_distance REAL,
    all_swim_totals_moving_time INTEGER,
    all_swim_totals_elapsed_time INTEGER,
    all_swim_totals_elevation_gain REAL,

    -- Recent totals (last 4 weeks)
    recent_ride_totals_count INTEGER,
    recent_ride_totals_distance REAL,
    recent_ride_totals_moving_time INTEGER,
    recent_ride_totals_elapsed_time INTEGER,
    recent_ride_totals_elevation_gain REAL,
    recent_ride_totals_achievement_count INTEGER,
    recent_run_totals_count INTEGER,
    recent_run_totals_distance REAL,
    recent_run_totals_moving_time INTEGER,
    recent_run_totals_elapsed_time INTEGER,
    recent_run_totals_elevation_gain REAL,
    recent_run_totals_achievement_count INTEGER,
    recent_swim_totals_count INTEGER,
    recent_swim_totals_distance REAL,
    recent_swim_totals_moving_time INTEGER,
    recent_swim_totals_elapsed_time INTEGER,
    recent_swim_totals_elevation_gain REAL,
    recent_swim_totals_achievement_count INTEGER,

    synced_at TEXT
);

-- Gear (bikes and shoes)
CREATE TABLE IF NOT EXISTS gear (
    id TEXT PRIMARY KEY,
    resource_state INTEGER,
    primary_gear INTEGER DEFAULT 0,
    name TEXT,
    brand_name TEXT,
    model_name TEXT,
    frame_type INTEGER,
    description TEXT,
    distance REAL,  -- meters
    gear_type TEXT, -- 'bike' or 'shoe'
    retired INTEGER DEFAULT 0,
    synced_at TEXT
);

-- Main activities table
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    resource_state INTEGER,
    athlete_id INTEGER REFERENCES athletes(id),
    name TEXT,
    description TEXT,
    type TEXT,
    sport_type TEXT,
    workout_type INTEGER,

    -- Timing
    start_date TEXT,        -- UTC ISO8601
    start_date_local TEXT,  -- Local ISO8601
    timezone TEXT,
    utc_offset REAL,

    -- Performance metrics (stored in SI units)
    distance REAL,              -- meters
    moving_time INTEGER,        -- seconds
    elapsed_time INTEGER,       -- seconds
    total_elevation_gain REAL,  -- meters
    elev_high REAL,             -- meters
    elev_low REAL,              -- meters

    -- Speed (m/s)
    average_speed REAL,
    max_speed REAL,

    -- Heart rate
    has_heartrate INTEGER DEFAULT 0,
    average_heartrate REAL,
    max_heartrate REAL,
    heartrate_opt_out INTEGER DEFAULT 0,

    -- Power
    device_watts INTEGER DEFAULT 0,
    average_watts REAL,
    max_watts INTEGER,
    weighted_average_watts INTEGER,
    kilojoules REAL,

    -- Cadence
    average_cadence REAL,

    -- Temperature
    average_temp INTEGER,

    -- Location
    start_lat REAL,
    start_lng REAL,
    end_lat REAL,
    end_lng REAL,

    -- Map
    map_id TEXT,
    map_polyline TEXT,
    map_summary_polyline TEXT,

    -- Social
    kudos_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    athlete_count INTEGER DEFAULT 1,
    photo_count INTEGER DEFAULT 0,
    total_photo_count INTEGER DEFAULT 0,

    -- Achievements
    achievement_count INTEGER DEFAULT 0,
    pr_count INTEGER DEFAULT 0,
    suffer_score INTEGER,

    -- Flags
    commute INTEGER DEFAULT 0,
    trainer INTEGER DEFAULT 0,
    manual INTEGER DEFAULT 0,
    private INTEGER DEFAULT 0,
    flagged INTEGER DEFAULT 0,
    hide_from_home INTEGER DEFAULT 0,
    visibility TEXT,

    -- Gear
    gear_id TEXT REFERENCES gear(id),

    -- External data
    external_id TEXT,
    upload_id INTEGER,

    synced_at TEXT,
    detail_synced_at TEXT  -- NULL until GET /activities/{id} has been fetched
);

CREATE INDEX IF NOT EXISTS idx_activities_athlete ON activities(athlete_id);
CREATE INDEX IF NOT EXISTS idx_activities_start_date ON activities(start_date);
CREATE INDEX IF NOT EXISTS idx_activities_start_date_local ON activities(start_date_local);
CREATE INDEX IF NOT EXISTS idx_activities_sport_type ON activities(sport_type);
CREATE INDEX IF NOT EXISTS idx_activities_gear ON activities(gear_id);
CREATE INDEX IF NOT EXISTS idx_activities_detail_synced ON activities(detail_synced_at);

-- Laps per activity
CREATE TABLE IF NOT EXISTS activity_laps (
    id INTEGER PRIMARY KEY,
    activity_id INTEGER NOT NULL REFERENCES activities(id),
    resource_state INTEGER,
    name TEXT,
    lap_index INTEGER,
    split INTEGER,

    -- Timing
    start_date TEXT,
    start_date_local TEXT,
    elapsed_time INTEGER,   -- seconds
    moving_time INTEGER,    -- seconds

    -- Metrics
    distance REAL,          -- meters
    total_elevation_gain REAL,
    average_speed REAL,     -- m/s
    max_speed REAL,         -- m/s
    average_cadence REAL,
    average_watts REAL,
    device_watts INTEGER DEFAULT 0,
    average_heartrate REAL,
    max_heartrate REAL,
    pace_zone INTEGER,

    -- Stream indices
    start_index INTEGER,
    end_index INTEGER
);

CREATE INDEX IF NOT EXISTS idx_activity_laps_activity ON activity_laps(activity_id);

-- Metric splits per activity (1km / 1mi splits)
CREATE TABLE IF NOT EXISTS activity_splits_metric (
    activity_id INTEGER NOT NULL REFERENCES activities(id),
    split INTEGER NOT NULL,
    distance REAL,
    elapsed_time INTEGER,
    moving_time INTEGER,
    elevation_difference REAL,
    pace_zone INTEGER,
    average_speed REAL,
    average_heartrate REAL,
    average_cadence REAL,
    average_grade_adjusted_speed REAL,
    PRIMARY KEY (activity_id, split)
);

CREATE INDEX IF NOT EXISTS idx_splits_activity ON activity_splits_metric(activity_id);

-- Segments master data
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    resource_state INTEGER,
    name TEXT,
    activity_type TEXT,
    distance REAL,          -- meters
    average_grade REAL,     -- percent
    maximum_grade REAL,     -- percent
    elevation_high REAL,    -- meters
    elevation_low REAL,     -- meters
    total_elevation_gain REAL,
    start_lat REAL,
    start_lng REAL,
    end_lat REAL,
    end_lng REAL,
    climb_category INTEGER, -- 0-5
    city TEXT,
    state TEXT,
    country TEXT,
    private INTEGER DEFAULT 0,
    hazardous INTEGER DEFAULT 0,
    starred INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    effort_count INTEGER,
    athlete_count INTEGER,
    star_count INTEGER,
    map_polyline TEXT,
    synced_at TEXT
);

-- Starred segments by athlete
CREATE TABLE IF NOT EXISTS starred_segments (
    athlete_id INTEGER REFERENCES athletes(id),
    segment_id INTEGER REFERENCES segments(id),
    synced_at TEXT,
    PRIMARY KEY (athlete_id, segment_id)
);

-- Segment efforts within activities
CREATE TABLE IF NOT EXISTS segment_efforts (
    id INTEGER PRIMARY KEY,
    activity_id INTEGER NOT NULL REFERENCES activities(id),
    segment_id INTEGER REFERENCES segments(id),
    name TEXT,

    -- Timing
    start_date TEXT,
    start_date_local TEXT,
    elapsed_time INTEGER,   -- seconds
    moving_time INTEGER,    -- seconds

    -- Metrics
    distance REAL,          -- meters
    average_cadence REAL,
    average_watts REAL,
    device_watts INTEGER DEFAULT 0,
    average_heartrate REAL,
    max_heartrate REAL,

    -- Stream indices
    start_index INTEGER,
    end_index INTEGER,

    pr_rank INTEGER,        -- NULL if not a PR, 1 for PR
    achievements TEXT,      -- JSON array
    hidden INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_segment_efforts_activity ON segment_efforts(activity_id);
CREATE INDEX IF NOT EXISTS idx_segment_efforts_segment ON segment_efforts(segment_id);

-- Heart rate / power zone distribution per activity
CREATE TABLE IF NOT EXISTS activity_zones (
    activity_id INTEGER NOT NULL REFERENCES activities(id),
    zone_type TEXT NOT NULL,    -- 'heartrate' or 'power'
    sensor_based INTEGER DEFAULT 0,
    zone_index INTEGER NOT NULL,
    zone_min INTEGER,
    zone_max INTEGER,
    time INTEGER,               -- seconds in this zone
    PRIMARY KEY (activity_id, zone_type, zone_index)
);

CREATE INDEX IF NOT EXISTS idx_activity_zones_activity ON activity_zones(activity_id);

-- Routes
CREATE TABLE IF NOT EXISTS routes (
    id INTEGER PRIMARY KEY,
    resource_state INTEGER,
    athlete_id INTEGER REFERENCES athletes(id),
    name TEXT,
    description TEXT,
    distance REAL,          -- meters
    elevation_gain REAL,    -- meters
    route_type INTEGER,     -- 1=ride, 2=run
    sub_type INTEGER,       -- 1=road, 2=mtb, 3=cx, 4=trail, 5=mixed
    private INTEGER DEFAULT 0,
    starred INTEGER DEFAULT 0,
    timestamp INTEGER,
    map_polyline TEXT,
    map_summary_polyline TEXT,
    synced_at TEXT
);

-- ============================================================
-- Views
-- ============================================================

CREATE VIEW IF NOT EXISTS activity_summary AS
SELECT
    a.id,
    a.name,
    a.sport_type,
    a.start_date_local,
    a.timezone,
    ROUND(a.distance / 1000.0, 2)           AS distance_km,
    ROUND(a.distance / 1609.344, 2)         AS distance_miles,
    a.moving_time,
    ROUND(a.moving_time / 60.0, 1)          AS moving_time_min,
    ROUND(a.elapsed_time / 60.0, 1)         AS elapsed_time_min,
    a.total_elevation_gain,
    -- Pace (min/km) for runs/walks
    CASE WHEN a.distance > 0 AND a.sport_type IN ('Run','TrailRun','Walk','Hike')
         THEN ROUND((a.moving_time / 60.0) / (a.distance / 1000.0), 2)
    END                                     AS pace_min_per_km,
    -- Speed
    ROUND(a.average_speed * 3.6, 1)        AS avg_speed_kmh,
    ROUND(a.max_speed * 3.6, 1)            AS max_speed_kmh,
    a.average_heartrate,
    a.max_heartrate,
    a.average_watts,
    a.weighted_average_watts,
    a.average_cadence,
    a.kilojoules,
    a.suffer_score,
    a.kudos_count,
    a.achievement_count,
    a.pr_count,
    a.commute,
    a.gear_id,
    g.name                                  AS gear_name,
    a.detail_synced_at IS NOT NULL          AS has_detail
FROM activities a
LEFT JOIN gear g ON a.gear_id = g.id;

CREATE VIEW IF NOT EXISTS monthly_stats AS
SELECT
    strftime('%Y-%m', start_date_local)     AS month,
    sport_type,
    COUNT(*)                                AS activity_count,
    ROUND(SUM(distance) / 1000.0, 1)       AS total_km,
    ROUND(SUM(moving_time) / 3600.0, 1)    AS total_hours,
    ROUND(SUM(total_elevation_gain), 0)     AS total_elevation_m,
    ROUND(AVG(average_heartrate), 0)        AS avg_heartrate,
    ROUND(AVG(average_watts), 0)            AS avg_watts,
    SUM(kudos_count)                        AS total_kudos,
    SUM(achievement_count)                  AS total_achievements
FROM activities
WHERE start_date_local IS NOT NULL
GROUP BY month, sport_type
ORDER BY month DESC, activity_count DESC;
