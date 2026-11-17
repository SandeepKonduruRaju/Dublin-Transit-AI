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

# Window starts 30 min BEFORE now so late-running buses (scheduled earlier, still to come) are included.
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
  AND st.departure_secs >= %(from_s)s + svc.offset_s
  AND st.departure_secs <  %(to_s)s + svc.offset_s
  AND (%(route)s::text IS NULL OR r.route_short_name = %(route)s)
ORDER BY st.departure_secs - svc.offset_s
LIMIT %(fetch)s
"""

# Current delay per (trip, service day) at or before the requested stop (GTFS-RT delay propagation),
# using only rows reported in the LATEST snapshot.
DELAYS_FOR_TRIPS_SQL = """
SELECT DISTINCT ON (d.trip_id, d.start_date)
       d.trip_id, d.start_date, COALESCE(d.departure_delay, d.arrival_delay) AS delay_s
FROM rt_stop_delays d
JOIN unnest(%(trip_ids)s::text[], %(dates)s::date[], %(seqs)s::int[]) AS q(trip_id, service_date, seq)
  ON q.trip_id = d.trip_id AND q.service_date = d.start_date
WHERE d.feed_timestamp = %(ts)s
  AND d.stop_sequence <= q.seq
  AND COALESCE(d.departure_delay, d.arrival_delay) IS NOT NULL
ORDER BY d.trip_id, d.start_date, d.stop_sequence DESC
"""

CANCELLED_SQL = """
SELECT trip_id, start_date FROM rt_trips
WHERE feed_timestamp = %(ts)s AND trip_id = ANY(%(trip_ids)s) AND schedule_relationship = 'CANCELED'
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
    lookback_minutes: int = 30,
) -> dict:
    """Next departures by EFFECTIVE time: expected (realtime) if known, else scheduled."""
    now_local = now.astimezone(tz)
    today = now_local.date()
    yday = today - timedelta(days=1)
    now_s = seconds_since_midnight(now_local)
    rows = conn.execute(
        SCHEDULED_SQL,
        {
            "today_svc": active_service_ids(conn, today),
            "yday_svc": active_service_ids(conn, yday),
            "today": today,
            "yday": yday,
            "stop_id": stop_id,
            "from_s": now_s - lookback_minutes * 60,
            "to_s": now_s + window_minutes * 60,
            "route": route,
            "fetch": limit + 20,
        },
    ).fetchall()

    snap = latest_snapshot(conn)
    realtime_age_s = int((now - snap["feed_timestamp"]).total_seconds()) if snap else None
    use_rt = snap is not None and realtime_age_s is not None and realtime_age_s <= stale_seconds

    delays: dict[tuple[str, date], int] = {}
    cancelled: set[tuple[str, date]] = set()
    if use_rt and rows:
        params = {
            "ts": snap["feed_timestamp"],
            "trip_ids": [r["trip_id"] for r in rows],
            "dates": [r["service_date"] for r in rows],
            "seqs": [r["stop_sequence"] for r in rows],
        }
        delays = {(r["trip_id"], r["start_date"]): r["delay_s"] for r in conn.execute(DELAYS_FOR_TRIPS_SQL, params).fetchall()}
        cancelled = {(r["trip_id"], r["start_date"]) for r in conn.execute(CANCELLED_SQL, params).fetchall()}

    candidates = []
    for r in rows:
        key = (r["trip_id"], r["service_date"])
        scheduled = service_datetime(r["service_date"], r["departure_secs"], tz)
        delay = delays.get(key)
        expected = scheduled + timedelta(seconds=delay) if delay is not None else None
        if key in cancelled:
            status, effective = "cancelled", scheduled
        elif expected is not None:
            status, effective = "realtime", expected
        else:
            status, effective = "scheduled_only", scheduled
        if effective < now_local:
            continue  # already gone (or a cancelled trip that was due before now)
        candidates.append((effective, {
            "route": r["route_short_name"],
            "headsign": r["trip_headsign"],
            "trip_id": r["trip_id"],
            "scheduled_time": scheduled.isoformat(),
            "delay_seconds": delay,
            "expected_time": expected.isoformat() if expected else None,
            "status": status,
        }))
    candidates.sort(key=lambda c: c[0])

    return {
        "stop_id": stop_id,
        "now": now_local.isoformat(),
        "realtime": "fresh" if use_rt else ("stale" if snap else "none"),
        "realtime_age_seconds": realtime_age_s,
        "departures": [d for _, d in candidates[:limit]],
    }


# ---------- current delays for a route ----------

ROUTE_DELAYS_SQL = """
SELECT DISTINCT ON (d.trip_id, d.start_date)
       d.trip_id, t.trip_headsign, s.stop_name AS next_stop,
       COALESCE(d.arrival_delay, d.departure_delay) AS delay_s
FROM rt_stop_delays d
JOIN trips t  ON t.trip_id = d.trip_id
JOIN routes r ON r.route_id = t.route_id
LEFT JOIN stops s ON s.stop_id = d.stop_id
WHERE d.feed_timestamp = %(ts)s
  AND r.route_short_name = %(route)s
  AND COALESCE(d.arrival_delay, d.departure_delay) IS NOT NULL
ORDER BY d.trip_id, d.start_date, d.stop_sequence
"""

ROUTE_CANCELLED_SQL = """
SELECT count(*) AS n
FROM rt_trips x
JOIN trips t  ON t.trip_id = x.trip_id
JOIN routes r ON r.route_id = t.route_id
WHERE x.feed_timestamp = %(ts)s AND r.route_short_name = %(route)s AND x.schedule_relationship = 'CANCELED'
"""


def route_delays(conn: psycopg.Connection, route: str, now: datetime, stale_seconds: int = 300) -> dict:
    snap = latest_snapshot(conn)
    if snap is None:
        return {"route": route, "realtime": "none", "trips": [], "cancelled_trips": 0}
    age = int((now - snap["feed_timestamp"]).total_seconds())
    params = {"ts": snap["feed_timestamp"], "route": route}
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


# ---------- on-time performance (from the daily summary) ----------

ON_TIME_SQL = """
SELECT COALESCE(sum(observations), 0) AS observations,
       COALESCE(sum(on_time), 0) AS on_time,
       COALESCE(sum(late), 0) AS late,
       COALESCE(sum(early), 0) AS early,
       sum(total_delay_s) AS total_delay_s,
       min(service_date) AS data_from,
       max(service_date) AS data_to,
       max(updated_at) AS last_updated
FROM rt_route_daily
WHERE route_short_name = %(route)s AND service_date > %(today)s::date - %(days)s::int
"""


def on_time_performance(conn: psycopg.Connection, route: str, now: datetime, days: int = 7, tz: ZoneInfo | None = None) -> dict:
    today = now.astimezone(tz).date() if tz else now.date()
    row = conn.execute(ON_TIME_SQL, {"route": route, "today": today, "days": days}).fetchone()
    n = row["observations"]
    return {
        "route": route,
        "window_days": days,
        "definition": "on time = between 1 min early and 5 min late, last reported delay per trip and stop",
        "observations": n,
        "on_time_percent": round(100 * row["on_time"] / n, 1) if n else None,
        "late": row["late"],
        "early": row["early"],
        "average_delay_seconds": round(row["total_delay_s"] / n) if n else None,
        "data_from": row["data_from"].isoformat() if row["data_from"] else None,
        "data_to": row["data_to"].isoformat() if row["data_to"] else None,
        "summary_updated": row["last_updated"].isoformat() if row["last_updated"] else None,
    }


# ---------- monitoring ----------

JOIN_HEALTH_SQL = """
SELECT count(*) AS rt_trips, count(t.trip_id) AS matched
FROM rt_trips x
LEFT JOIN trips t ON t.trip_id = x.trip_id
WHERE x.feed_timestamp = %(ts)s
"""


def join_health(conn: psycopg.Connection) -> dict | None:
    """% of realtime trips in the latest snapshot that exist in the static timetable."""
    snap = latest_snapshot(conn)
    if snap is None:
        return None
    row = conn.execute(JOIN_HEALTH_SQL, {"ts": snap["feed_timestamp"]}).fetchone()
    pct = round(100 * row["matched"] / row["rt_trips"], 1) if row["rt_trips"] else None
    return {"rt_trips": row["rt_trips"], "matched": row["matched"], "percent": pct}
