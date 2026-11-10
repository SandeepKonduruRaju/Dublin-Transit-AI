-- 003_realtime.sql: GTFS-Realtime snapshots.
-- No FKs to static tables: realtime can contain trips the timetable doesn't,
-- and the static reload TRUNCATEs those tables.

CREATE TABLE IF NOT EXISTS rt_snapshots (
    id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    feed_timestamp         TIMESTAMPTZ NOT NULL UNIQUE,   -- dedup key (header.timestamp)
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    trip_update_count      INTEGER     NOT NULL,
    stop_time_update_count INTEGER     NOT NULL
);

CREATE TABLE IF NOT EXISTS rt_trip_updates (
    snapshot_id           BIGINT   NOT NULL REFERENCES rt_snapshots (id) ON DELETE CASCADE,
    entity_id             TEXT     NOT NULL,
    trip_id               TEXT,
    route_id              TEXT,
    start_date            DATE,
    start_time            TEXT,                -- 'HH:MM:SS', may exceed 24:00
    direction_id          SMALLINT,
    schedule_relationship TEXT     NOT NULL    -- SCHEDULED / CANCELED / ADDED ...
);

CREATE TABLE IF NOT EXISTS rt_stop_time_updates (
    snapshot_id           BIGINT  NOT NULL REFERENCES rt_snapshots (id) ON DELETE CASCADE,
    entity_id             TEXT    NOT NULL,
    trip_id               TEXT,
    stop_sequence         INTEGER,
    stop_id               TEXT,
    arrival_delay         INTEGER,             -- seconds, + late / - early
    arrival_time          TIMESTAMPTZ,
    departure_delay       INTEGER,
    departure_time        TIMESTAMPTZ,
    schedule_relationship TEXT    NOT NULL     -- SCHEDULED / SKIPPED / NO_DATA ...
);

-- "latest snapshot, this route" and "latest snapshot, this trip"
CREATE INDEX IF NOT EXISTS idx_rt_trip_updates_snapshot_route
    ON rt_trip_updates (snapshot_id, route_id);
CREATE INDEX IF NOT EXISTS idx_rt_stop_time_updates_snapshot_trip
    ON rt_stop_time_updates (snapshot_id, trip_id, stop_sequence);
