"""Transit queries used by the API. Every function takes a connection (dict rows) and plain values,
so it can be tested without HTTP. `now` is always passed in, never read inside, so tests can freeze time."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ON_TIME_EARLY_S = -60   # up to 1 min early counts as on time
ON_TIME_LATE_S = 300    # up to 5 min late counts as on time


# ---------- time helpers (pure) ----------

def seconds_since_midnight(now_local: datetime) -> int:
    return now_local.hour * 3600 + now_local.minute * 60 + now_local.second


def service_datetime(service_date: date, secs: int, tz: ZoneInfo) -> datetime:
    """GTFS time (seconds, may exceed 86400) on a service date -> aware local datetime."""
    return datetime.combine(service_date, time(0), tzinfo=tz) + timedelta(seconds=secs)


# ---------- lookups ----------

def route_exists(conn: psycopg.Connection, short_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM routes WHERE route_short_name = %s LIMIT 1", (short_name,)).fetchone()
    return row is not None


def get_route(conn: psycopg.Connection, short_name: str) -> list[dict]:
    return conn.execute(
        """SELECT route_id, route_short_name, route_long_name, agency_id, route_type
           FROM routes WHERE route_short_name = %s ORDER BY route_id""",
        (short_name,),
    ).fetchall()


def search_stops(conn: psycopg.Connection, q: str, limit: int = 10) -> list[dict]:
    """Match the stop number on the pole exactly, or the name partially."""
    return conn.execute(
        """
        SELECT s.stop_id, s.stop_code, s.stop_name, s.stop_lat, s.stop_lon,
               ARRAY(
                   SELECT DISTINCT r.route_short_name
                   FROM stop_times st
                   JOIN trips t  ON t.trip_id = st.trip_id
                   JOIN routes r ON r.route_id = t.route_id
                   WHERE st.stop_id = s.stop_id
                   ORDER BY 1
               ) AS routes
        FROM stops s
        WHERE s.stop_code = %(q)s OR s.stop_name ILIKE %(pattern)s
        ORDER BY (s.stop_code = %(q)s) DESC, s.stop_name
        LIMIT %(limit)s
        """,
        {"q": q.strip(), "pattern": f"%{q.strip()}%", "limit": limit},
    ).fetchall()


def active_service_ids(conn: psycopg.Connection, d: date) -> list[str]:
    """Services running on date d: weekly calendar, plus added dates, minus removed dates."""
    query = sql.SQL(
        """
        SELECT service_id FROM calendar
        WHERE start_date <= %(d)s AND end_date >= %(d)s AND {day}
        UNION
        SELECT service_id FROM calendar_dates WHERE date = %(d)s AND exception_type = 1
        EXCEPT
        SELECT service_id FROM calendar_dates WHERE date = %(d)s AND exception_type = 2
        """
    ).format(day=sql.Identifier(DAYS[d.weekday()]))
    return [r["service_id"] for r in conn.execute(query, {"d": d}).fetchall()]


def latest_snapshot(conn: psycopg.Connection) -> dict | None:
    return conn.execute(
        "SELECT id, feed_timestamp FROM rt_snapshots ORDER BY feed_timestamp DESC LIMIT 1"
    ).fetchone()


# ---------- departures ----------

SCHEDULED_SQL = """
WITH svc AS (
    SELECT unnest(%(today_svc)s::text[]) AS service_id, %(today)s::date AS service_date, 0 AS offset_s
    UNION ALL
    SELECT unnest(%(yday_svc)s::text[]), %(yday)s::date, 86400       -- yesterday's after-midnight trips
)
SELECT st.trip_id, st.stop_sequence, st.departure_secs, svc.service_date,
       r.route_short_name, t.trip_headsign
FROM svc
JOIN trips t       ON t.service_id = svc.service_id
JOIN stop_times st ON st.trip_id = t.trip_id
JOIN routes r      ON r.route_id = t.route_id
WHERE st.stop_id = %(stop_id)s
  AND st.departure_secs >= %(now_s)s + svc.offset_s - 1800   -- 30 min back: late buses may still be coming
  AND st.departure_secs <  %(now_s)s + svc.offset_s + %(window_s)s
  AND (%(route)s::text IS NULL OR r.route_short_name = %(route)s)
ORDER BY st.departure_secs - svc.offset_s
LIMIT %(limit)s
"""

# Latest known delay for each trip at or before the requested stop (GTFS-RT delay propagation)
DELAYS_FOR_TRIPS_SQL = """
SELECT DISTINCT ON (u.trip_id) u.trip_id, COALESCE(u.departure_delay, u.arrival_delay) AS delay_s
FROM rt_stop_time_updates u
JOIN unnest(%(trip_ids)s::text[], %(seqs)s::int[]) AS q(trip_id, seq) ON q.trip_id = u.trip_id
WHERE u.snapshot_id = %(sid)s
  AND u.stop_sequence <= q.seq
  AND COALESCE(u.departure_delay, u.arrival_delay) IS NOT NULL
ORDER BY u.trip_id, u.stop_sequence DESC
"""

CANCELLED_SQL = """
SELECT trip_id FROM rt_trip_updates
WHERE snapshot_id = %(sid)s AND trip_id = ANY(%(trip_ids)s) AND schedule_relationship = 'CANCELED'
"""


def departures(
    conn: psycopg.Connection,
    stop_id: str,
    now: datetime,
    tz: ZoneInfo,
    route: str | None = None,
    limit: int = 5,
    window_minutes: int = 120,
    stale_seconds: int = 300,
) -> dict:
    now_local = now.astimezone(tz)
    today = now_local.date()
    yday = today - timedelta(days=1)
    rows = conn.execute(
        SCHEDULED_SQL,
        {
            "today_svc": active_service_ids(conn, today),
            "yday_svc": active_service_ids(conn, yday),
            "today": today,
            "yday": yday,
            "stop_id": stop_id,
            "now_s": seconds_since_midnight(now_local),
            "window_s": window_minutes * 60,
            "route": route,
            "limit": limit + 20,  # headroom: some rows are dropped below once delays are applied
        },
    ).fetchall()

    snap = latest_snapshot(conn)
    realtime_age_s = int((now - snap["feed_timestamp"]).total_seconds()) if snap else None
    use_rt = snap is not None and realtime_age_s is not None and realtime_age_s <= stale_seconds

    delays: dict[str, int] = {}
    cancelled: set[str] = set()
    if use_rt and rows:
        trip_ids = [r["trip_id"] for r in rows]
        params = {"sid": snap["id"], "trip_ids": trip_ids, "seqs": [r["stop_sequence"] for r in rows]}
        delays = {r["trip_id"]: r["delay_s"] for r in conn.execute(DELAYS_FOR_TRIPS_SQL, params).fetchall()}
        cancelled = {r["trip_id"] for r in conn.execute(CANCELLED_SQL, params).fetchall()}

    # Effective time = expected if realtime, otherwise scheduled (cancelled trips use scheduled).
    cutoff = now.replace(microsecond=0)
    out = []
    for r in rows:
        scheduled = service_datetime(r["service_date"], r["departure_secs"], tz)
        delay = delays.get(r["trip_id"])
        if r["trip_id"] in cancelled:
            status = "cancelled"
        elif delay is not None:
            status = "realtime"
        else:
            status = "scheduled_only"
        expected = scheduled + timedelta(seconds=delay) if delay is not None else None
        effective = expected if status == "realtime" else scheduled
        if effective < cutoff:
            continue  # already gone
        out.append((effective, {
            "route": r["route_short_name"],
            "headsign": r["trip_headsign"],
            "trip_id": r["trip_id"],
            "scheduled_time": scheduled.isoformat(),
            "delay_seconds": delay,
            "expected_time": expected.isoformat() if expected else None,
            "status": status,
        }))
    out.sort(key=lambda pair: pair[0])
    out = [dep for _, dep in out[:limit]]

    return {
        "stop_id": stop_id,
        "now": now_local.isoformat(),
        "realtime": "fresh" if use_rt else ("stale" if snap else "none"),
        "realtime_age_seconds": realtime_age_s,
        "departures": out,
    }


# ---------- current delays for a route ----------

ROUTE_DELAYS_SQL = """
SELECT DISTINCT ON (u.trip_id)
       u.trip_id, t.trip_headsign, s.stop_name AS next_stop,
       COALESCE(u.arrival_delay, u.departure_delay) AS delay_s
FROM rt_stop_time_updates u
JOIN trips t  ON t.trip_id = u.trip_id
JOIN routes r ON r.route_id = t.route_id
LEFT JOIN stops s ON s.stop_id = u.stop_id
WHERE u.snapshot_id = %(sid)s
  AND r.route_short_name = %(route)s
  AND COALESCE(u.arrival_delay, u.departure_delay) IS NOT NULL
ORDER BY u.trip_id, u.stop_sequence
"""

ROUTE_CANCELLED_SQL = """
SELECT count(*) AS n
FROM rt_trip_updates tu
JOIN trips t  ON t.trip_id = tu.trip_id
JOIN routes r ON r.route_id = t.route_id
WHERE tu.snapshot_id = %(sid)s AND r.route_short_name = %(route)s AND tu.schedule_relationship = 'CANCELED'
"""


def route_delays(conn: psycopg.Connection, route: str, now: datetime, stale_seconds: int = 300) -> dict:
    snap = latest_snapshot(conn)
    if snap is None:
        return {"route": route, "realtime": "none", "trips": [], "cancelled_trips": 0}
    age = int((now - snap["feed_timestamp"]).total_seconds())
    params = {"sid": snap["id"], "route": route}
    trips = conn.execute(ROUTE_DELAYS_SQL, params).fetchall()
    cancelled = conn.execute(ROUTE_CANCELLED_SQL, params).fetchone()["n"]
    delays = [t["delay_s"] for t in trips]
    return {
        "route": route,
        "realtime": "fresh" if age <= stale_seconds else "stale",
        "feed_timestamp": snap["feed_timestamp"].isoformat(),
        "realtime_age_seconds": age,
        "trips_reporting": len(trips),
        "late_over_5_min": sum(1 for d in delays if d > ON_TIME_LATE_S),
        "average_delay_seconds": round(sum(delays) / len(delays)) if delays else None,
        "cancelled_trips": cancelled,
        "trips": [
            {"trip_id": t["trip_id"], "headsign": t["trip_headsign"], "next_stop": t["next_stop"], "delay_seconds": t["delay_s"]}
            for t in trips
        ],
    }


# ---------- on-time performance ----------

# Last reported delay per (trip, service day, stop): the final prediction before the bus got there.
ON_TIME_SQL = """
WITH obs AS (
    SELECT DISTINCT ON (tu.trip_id, tu.start_date, u.stop_sequence)
           COALESCE(u.arrival_delay, u.departure_delay) AS delay_s,
           s.feed_timestamp
    FROM rt_snapshots s
    JOIN rt_stop_time_updates u ON u.snapshot_id = s.id
    JOIN rt_trip_updates tu     ON tu.snapshot_id = u.snapshot_id AND tu.entity_id = u.entity_id
    JOIN trips t                ON t.trip_id = u.trip_id
    JOIN routes r               ON r.route_id = t.route_id
    WHERE s.feed_timestamp >= %(since)s
      AND r.route_short_name = %(route)s
      AND COALESCE(u.arrival_delay, u.departure_delay) IS NOT NULL
    ORDER BY tu.trip_id, tu.start_date, u.stop_sequence, s.feed_timestamp DESC
)
SELECT count(*) AS observations,
       count(*) FILTER (WHERE delay_s BETWEEN %(early)s AND %(late)s) AS on_time,
       count(*) FILTER (WHERE delay_s > %(late)s)  AS late,
       count(*) FILTER (WHERE delay_s < %(early)s) AS early,
       round(avg(delay_s)) AS average_delay_seconds,
       min(feed_timestamp) AS data_from,
       max(feed_timestamp) AS data_to
FROM obs
"""


def on_time_performance(conn: psycopg.Connection, route: str, now: datetime, days: int = 7) -> dict:
    row = conn.execute(
        ON_TIME_SQL,
        {"since": now - timedelta(days=days), "route": route, "early": ON_TIME_EARLY_S, "late": ON_TIME_LATE_S},
    ).fetchone()
    n = row["observations"]
    return {
        "route": route,
        "window_days": days,
        "definition": "on time = between 1 min early and 5 min late, last reported delay per trip and stop",
        "observations": n,
        "on_time_percent": round(100 * row["on_time"] / n, 1) if n else None,
        "late": row["late"],
        "early": row["early"],
        "average_delay_seconds": int(row["average_delay_seconds"]) if row["average_delay_seconds"] is not None else None,
        "data_from": row["data_from"].isoformat() if row["data_from"] else None,
        "data_to": row["data_to"].isoformat() if row["data_to"] else None,
    }
