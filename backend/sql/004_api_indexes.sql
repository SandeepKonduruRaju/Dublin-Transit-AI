-- 004_api_indexes.sql: indexes for the Milestone 4 API queries

-- on-time performance joins stop updates to their trip update by (snapshot_id, entity_id)
CREATE INDEX IF NOT EXISTS idx_rt_trip_updates_snapshot_entity
    ON rt_trip_updates (snapshot_id, entity_id);

-- stop search by the number printed on the bus-stop pole
CREATE INDEX IF NOT EXISTS idx_stops_stop_code ON stops (stop_code);

-- "which routes serve this stop?" and route lookups by public name
CREATE INDEX IF NOT EXISTS idx_trips_route ON trips (route_id);
CREATE INDEX IF NOT EXISTS idx_routes_short_name ON routes (route_short_name);
