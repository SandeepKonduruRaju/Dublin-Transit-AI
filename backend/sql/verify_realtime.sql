-- 1. Snapshots collected, and their size
SELECT count(*) AS snapshots,
       min(feed_timestamp) AS first_ts,
       max(feed_timestamp) AS last_ts,
       round(avg(stop_time_update_count)) AS avg_stop_updates_per_snapshot
FROM rt_snapshots;

-- 2. Join health: what % of realtime trips exist in the static timetable?
WITH latest AS (SELECT id FROM rt_snapshots ORDER BY feed_timestamp DESC LIMIT 1)
SELECT count(*)                                                 AS rt_trips,
       count(t.trip_id)                                         AS matched_static,
       round(100.0 * count(t.trip_id) / nullif(count(*), 0), 1) AS match_pct
FROM rt_trip_updates u
JOIN latest l ON u.snapshot_id = l.id
LEFT JOIN trips t ON t.trip_id = u.trip_id;

-- 3. "Is the 39A running late right now?" (next reported stop per trip)
WITH latest AS (SELECT id FROM rt_snapshots ORDER BY feed_timestamp DESC LIMIT 1)
SELECT DISTINCT ON (u.trip_id)
       r.route_short_name, t.trip_headsign, s.stop_name,
       u.stop_sequence, round(u.arrival_delay / 60.0, 1) AS delay_min
FROM rt_stop_time_updates u
JOIN latest l  ON u.snapshot_id = l.id
JOIN trips t   ON t.trip_id = u.trip_id
JOIN routes r  ON r.route_id = t.route_id
LEFT JOIN stops s ON s.stop_id = u.stop_id
WHERE r.route_short_name = '39A'
ORDER BY u.trip_id, u.stop_sequence;

-- 4. Cancelled trips in the latest snapshot
WITH latest AS (SELECT id FROM rt_snapshots ORDER BY feed_timestamp DESC LIMIT 1)
SELECT count(*) AS cancelled_trips
FROM rt_trip_updates u JOIN latest l ON u.snapshot_id = l.id
WHERE u.schedule_relationship = 'CANCELED';

-- 5. Storage: how fast is this growing?
SELECT relname AS tbl, pg_size_pretty(pg_total_relation_size(relid)) AS size
FROM pg_catalog.pg_statio_user_tables
WHERE relname LIKE 'rt_%'
ORDER BY pg_total_relation_size(relid) DESC;
