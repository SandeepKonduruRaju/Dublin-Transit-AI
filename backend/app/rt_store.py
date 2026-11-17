"""Compact realtime storage (Milestone 6.5): upsert the LATEST delay per (trip, day, stop) and roll up
per-route daily reliability. Replaces "append ~11,000 rows per minute"."""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from app.rt_parser import ParsedFeed

TZ = ZoneInfo("Europe/Dublin")
ON_TIME_EARLY_S = -60
ON_TIME_LATE_S = 300

INSERT_SNAPSHOT_SQL = """
    INSERT INTO rt_snapshots (feed_timestamp, trip_update_count, stop_time_update_count)
    VALUES (%s, %s, %s)
    ON CONFLICT (feed_timestamp) DO NOTHING
    RETURNING id
"""

TRIP_COLS = ("trip_id", "start_date", "route_id", "direction_id", "schedule_relationship", "feed_timestamp")
STOP_COLS = ("trip_id", "start_date", "stop_sequence", "stop_id", "arrival_delay", "departure_delay",
             "arrival_time", "departure_time", "schedule_relationship", "feed_timestamp")

# DISTINCT ON removes duplicates inside one snapshot (ON CONFLICT can't touch a row twice).
# The WHERE guard ignores out-of-order (older) snapshots.
UPSERT_TRIPS_SQL = """
    INSERT INTO rt_trips (trip_id, start_date, route_id, direction_id, schedule_relationship, first_seen, feed_timestamp)
    SELECT DISTINCT ON (trip_id, start_date)
           trip_id, start_date, route_id, direction_id, schedule_relationship, feed_timestamp, feed_timestamp
    FROM tmp_rt_trips
    ORDER BY trip_id, start_date
    ON CONFLICT (trip_id, start_date) DO UPDATE SET
        route_id = EXCLUDED.route_id,
        direction_id = EXCLUDED.direction_id,
        schedule_relationship = EXCLUDED.schedule_relationship,
        feed_timestamp = EXCLUDED.feed_timestamp
    WHERE rt_trips.feed_timestamp < EXCLUDED.feed_timestamp
"""

UPSERT_STOPS_SQL = """
    INSERT INTO rt_stop_delays (trip_id, start_date, stop_sequence, stop_id, arrival_delay, departure_delay,
                                arrival_time, departure_time, schedule_relationship, feed_timestamp)
    SELECT DISTINCT ON (trip_id, start_date, stop_sequence)
           trip_id, start_date, stop_sequence, stop_id, arrival_delay, departure_delay,
           arrival_time, departure_time, schedule_relationship, feed_timestamp
    FROM tmp_rt_stops
    ORDER BY trip_id, start_date, stop_sequence
    ON CONFLICT (trip_id, start_date, stop_sequence) DO UPDATE SET
        stop_id = EXCLUDED.stop_id,
        arrival_delay = EXCLUDED.arrival_delay,
        departure_delay = EXCLUDED.departure_delay,
        arrival_time = EXCLUDED.arrival_time,
        departure_time = EXCLUDED.departure_time,
        schedule_relationship = EXCLUDED.schedule_relationship,
        feed_timestamp = EXCLUDED.feed_timestamp
    WHERE rt_stop_delays.feed_timestamp < EXCLUDED.feed_timestamp
"""

ROLLUP_SQL = """
    INSERT INTO rt_route_daily (service_date, route_short_name, observations, on_time, late, early, total_delay_s, updated_at)
    SELECT d.start_date, r.route_short_name,
           count(*),
           count(*) FILTER (WHERE d.delay_s BETWEEN %(early)s AND %(late)s),
           count(*) FILTER (WHERE d.delay_s > %(late)s),
           count(*) FILTER (WHERE d.delay_s < %(early)s),
           sum(d.delay_s),
           now()
    FROM (
        SELECT trip_id, start_date, COALESCE(arrival_delay, departure_delay) AS delay_s
        FROM rt_stop_delays
        WHERE start_date = ANY(%(dates)s)
    ) d
    JOIN trips t  ON t.trip_id = d.trip_id
    JOIN routes r ON r.route_id = t.route_id
    WHERE d.delay_s IS NOT NULL AND r.route_short_name IS NOT NULL
    GROUP BY d.start_date, r.route_short_name
    ON CONFLICT (service_date, route_short_name) DO UPDATE SET
        observations = EXCLUDED.observations,
        on_time = EXCLUDED.on_time,
        late = EXCLUDED.late,
        early = EXCLUDED.early,
        total_delay_s = EXCLUDED.total_delay_s,
        updated_at = EXCLUDED.updated_at
"""


def _copy(cur: psycopg.Cursor, table: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
    stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns))
    )
    with cur.copy(stmt) as copy:
        for row in rows:
            copy.write_row(row)


def build_rows(parsed: ParsedFeed) -> tuple[list[tuple], list[tuple]]:
    """Parser rows -> compact rows. Trips without trip_id and stops without stop_sequence are skipped."""
    ts = parsed.feed_timestamp
    fallback_day = ts.astimezone(TZ).date()
    entity_day: dict[str, tuple[str, date]] = {}
    trips: list[tuple] = []
    for entity_id, trip_id, route_id, start_date, _start_time, direction_id, rel in parsed.trip_rows:
        if trip_id is None:
            continue
        day = start_date or fallback_day
        entity_day[entity_id] = (trip_id, day)
        trips.append((trip_id, day, route_id, direction_id, rel, ts))
    stops: list[tuple] = []
    for entity_id, _trip_id, seq, stop_id, arr_d, arr_t, dep_d, dep_t, rel in parsed.stop_rows:
        if seq is None or entity_id not in entity_day:
            continue
        trip_id, day = entity_day[entity_id]
        stops.append((trip_id, day, seq, stop_id, arr_d, dep_d, arr_t, dep_t, rel, ts))
    return trips, stops


def store_snapshot(conn: psycopg.Connection, parsed: ParsedFeed) -> int | None:
    """Atomically log the snapshot and upsert its latest values. None if this feed_timestamp was already stored."""
    trips, stops = build_rows(parsed)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(INSERT_SNAPSHOT_SQL, (parsed.feed_timestamp, len(parsed.trip_rows), len(parsed.stop_rows)))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("CREATE TEMP TABLE tmp_rt_trips (LIKE rt_trips INCLUDING DEFAULTS) ON COMMIT DROP")
        cur.execute("CREATE TEMP TABLE tmp_rt_stops (LIKE rt_stop_delays INCLUDING DEFAULTS) ON COMMIT DROP")
        cur.execute("ALTER TABLE tmp_rt_trips ALTER COLUMN first_seen DROP NOT NULL")
        _copy(cur, "tmp_rt_trips", TRIP_COLS, trips)
        _copy(cur, "tmp_rt_stops", STOP_COLS, stops)
        cur.execute(UPSERT_TRIPS_SQL)
        cur.execute(UPSERT_STOPS_SQL)
        return row[0]


def rollup_daily(conn: psycopg.Connection, dates: list[date]) -> int:
    """Recompute rt_route_daily for the given service dates from the latest delays."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(ROLLUP_SQL, {"dates": dates, "early": ON_TIME_EARLY_S, "late": ON_TIME_LATE_S})
        return cur.rowcount


def rollup_recent(conn: psycopg.Connection, today: date) -> int:
    """Today and yesterday: after-midnight trips still belong to yesterday's service day."""
    return rollup_daily(conn, [today - timedelta(days=1), today])


def apply_retention(conn: psycopg.Connection, detail_days: int, summary_days: int) -> dict[str, int]:
    deleted = {}
    with conn.transaction(), conn.cursor() as cur:
        for table, column, days in (
            ("rt_stop_delays", "feed_timestamp", detail_days),
            ("rt_trips", "feed_timestamp", detail_days),
            ("rt_snapshots", "feed_timestamp", max(detail_days, 8)),
        ):
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE {} < now() - make_interval(days => %s)").format(
                    sql.Identifier(table), sql.Identifier(column)
                ),
                (days,),
            )
            deleted[table] = cur.rowcount
        cur.execute("DELETE FROM rt_route_daily WHERE service_date < current_date - %s::int", (summary_days,))
        deleted["rt_route_daily"] = cur.rowcount
    return deleted
