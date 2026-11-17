-- 1. Snapshots collected, and their size
SELECT count(*) AS snapshots,
       min(feed_timestamp) AS first_ts,
       max(feed_timestamp) AS last_ts,
       round(avg(stop_time_update_count)) AS avg_stop_updates_per_snapshot
FROM rt_snapshots;

-- 2. Join health: what % of realtime trips exist in the static timetable?
WITH latest AS (SELECT feed_timestamp FROM rt_snapshots ORDER BY feed_timestamp DESC LIMIT 1)
SELECT count(*)                                                 AS rt_trips,
       count(t.trip_id)                                         AS matched_static,
       round(100.0 * count(t.trip_id) / nullif(count(*), 0), 1) AS match_pct
FROM rt_trips x
JOIN latest l ON x.feed_timestamp = l.feed_timestamp
LEFT JOIN trips t ON t.trip_id = x.trip_id;

-- 3. "Is the 39A running late right now?" (next reported stop per trip and service day)
WITH latest AS (SELECT feed_timestamp FROM rt_snapshots ORDER BY feed_timestamp DESC LIMIT 1)
SELECT DISTINCT ON (d.trip_id, d.start_date)
       r.route_short_name, t.trip_headsign, s.stop_name,
       d.stop_sequence, round(d.arrival_delay / 60.0, 1) AS delay_min
FROM rt_stop_delays d
JOIN latest l  ON d.feed_timestamp = l.feed_timestamp
JOIN trips t   ON t.trip_id = d.trip_id
JOIN routes r  ON r.route_id = t.route_id
LEFT JOIN stops s ON s.stop_id = d.stop_id
WHERE r.route_short_name = '39A'
ORDER BY d.trip_id, d.start_date, d.stop_sequence;

-- 4. Cancelled trips in the latest snapshot
WITH latest AS (SELECT feed_timestamp FROM rt_snapshots ORDER BY feed_timestamp DESC LIMIT 1)
SELECT count(*) AS cancelled_trips
FROM rt_trips x JOIN latest l ON x.feed_timestamp = l.feed_timestamp
WHERE x.schedule_relationship = 'CANCELED';

-- 5. Storage: how fast is this growing?
SELECT relname AS tbl, pg_size_pretty(pg_total_relation_size(relid)) AS size
FROM pg_catalog.pg_statio_user_tables
WHERE relname LIKE 'rt_%'
ORDER BY pg_total_relation_size(relid) DESC;
