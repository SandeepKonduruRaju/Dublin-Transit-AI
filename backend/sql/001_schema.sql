-- 001_schema.sql: static GTFS tables (subset of columns we actually use)
-- Created in FK order: parents before children.

CREATE TABLE IF NOT EXISTS routes (
    route_id         TEXT PRIMARY KEY,
    agency_id        TEXT,
    route_short_name TEXT,              -- e.g. '46A'
    route_long_name  TEXT,
    route_type       SMALLINT NOT NULL  -- 3 = bus
);

CREATE TABLE IF NOT EXISTS calendar (
    service_id TEXT PRIMARY KEY,
    monday     BOOLEAN NOT NULL,
    tuesday    BOOLEAN NOT NULL,
    wednesday  BOOLEAN NOT NULL,
    thursday   BOOLEAN NOT NULL,
    friday     BOOLEAN NOT NULL,
    saturday   BOOLEAN NOT NULL,
    sunday     BOOLEAN NOT NULL,
    start_date DATE NOT NULL,
    end_date   DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_dates (
    service_id     TEXT NOT NULL,
    date           DATE NOT NULL,
    exception_type SMALLINT NOT NULL CHECK (exception_type IN (1, 2)), -- 1 = added, 2 = removed
    PRIMARY KEY (service_id, date)
);

CREATE TABLE IF NOT EXISTS stops (
    stop_id   TEXT PRIMARY KEY,
    stop_code TEXT,
    stop_name TEXT,
    stop_lat  DOUBLE PRECISION,
    stop_lon  DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id       TEXT PRIMARY KEY,
    route_id      TEXT NOT NULL REFERENCES routes (route_id),
    service_id    TEXT NOT NULL,       -- no FK: may exist only in calendar_dates
    trip_headsign TEXT,
    direction_id  SMALLINT
);

CREATE TABLE IF NOT EXISTS stop_times (
    trip_id        TEXT    NOT NULL REFERENCES trips (trip_id),
    stop_sequence  INTEGER NOT NULL,
    stop_id        TEXT    NOT NULL REFERENCES stops (stop_id),
    arrival_secs   INTEGER,            -- seconds since service-day start; can exceed 86400
    departure_secs INTEGER,
    PRIMARY KEY (trip_id, stop_sequence)
);

-- "Next departures from stop X": filter by stop, then read in time order
CREATE INDEX IF NOT EXISTS idx_stop_times_stop_departure
    ON stop_times (stop_id, departure_secs);