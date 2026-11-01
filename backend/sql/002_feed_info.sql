-- 002_feed_info.sql: which static feed version is currently loaded (always one row)

CREATE TABLE IF NOT EXISTS feed_info (
    id              BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),  -- only TRUE allowed -> max one row
    feed_version    TEXT NOT NULL,
    feed_start_date DATE,
    feed_end_date   DATE,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);