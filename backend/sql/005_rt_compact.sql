-- 005_rt_compact.sql: compact realtime storage (Milestone 6.5).
-- Instead of ~11,000 new rows every minute, keep ONE row per (trip, service day, stop) holding the
-- latest reported delay (upserted each snapshot), plus a tiny per-route daily summary for reliability.
-- rt_snapshots stays as a small header log (dedupe key + freshness). The old 003 child tables are no
-- longer written; dropping them is a separate, manual decision.

CREATE TABLE IF NOT EXISTS rt_trips (
    trip_id               TEXT        NOT NULL,
    start_date            DATE        NOT NULL,   -- service day (from the feed, else the feed's local date)
    route_id              TEXT,
    direction_id          SMALLINT,
    schedule_relationship TEXT        NOT NULL,   -- SCHEDULED / CANCELED / ADDED ...
    first_seen            TIMESTAMPTZ NOT NULL,
    feed_timestamp        TIMESTAMPTZ NOT NULL,   -- last snapshot that reported this trip
    PRIMARY KEY (trip_id, start_date)
);
CREATE INDEX IF NOT EXISTS idx_rt_trips_feed_ts ON rt_trips (feed_timestamp);

CREATE TABLE IF NOT EXISTS rt_stop_delays (
    trip_id               TEXT        NOT NULL,
    start_date            DATE        NOT NULL,
    stop_sequence         INTEGER     NOT NULL,
    stop_id               TEXT,
    arrival_delay         INTEGER,                -- seconds, + late / - early, NULL = unknown
    departure_delay       INTEGER,
    arrival_time          TIMESTAMPTZ,
    departure_time        TIMESTAMPTZ,
    schedule_relationship TEXT        NOT NULL,
    feed_timestamp        TIMESTAMPTZ NOT NULL,   -- last snapshot that reported this stop
    PRIMARY KEY (trip_id, start_date, stop_sequence)
);
CREATE INDEX IF NOT EXISTS idx_rt_stop_delays_feed_ts ON rt_stop_delays (feed_timestamp);

CREATE TABLE IF NOT EXISTS rt_route_daily (
    service_date     DATE        NOT NULL,
    route_short_name TEXT        NOT NULL,
    observations     INTEGER     NOT NULL,
    on_time          INTEGER     NOT NULL,
    late             INTEGER     NOT NULL,
    early            INTEGER     NOT NULL,
    total_delay_s    BIGINT      NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (service_date, route_short_name)
);
