-- 006_drop_legacy_rt.sql: drop the append-only realtime tables from 003 (Milestone 6.5).
-- They were replaced by rt_trips / rt_stop_delays / rt_route_daily (005) and are no longer written.
-- 003 still creates them on a fresh database; this removes them again, so every database ends up the same.

DROP TABLE IF EXISTS rt_stop_time_updates, rt_trip_updates;
