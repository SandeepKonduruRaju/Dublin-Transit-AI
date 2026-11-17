# Milestone 6.5: Production-readiness (Claude Code playbook)

Dublin Transit AI. **Build mode**: Claude Code builds and verifies; Sandy learns from "Learn later" at the end.
Three fixes that must exist before anything goes on a public URL (Milestone 8, Azure).

| # | Problem today | Fix |
|---|---|---|
| 1 | Realtime storage grows about **3 GB/day** (11,000 new rows per minute) | Keep only the **latest** delay per trip, day and stop (upsert), plus a tiny **daily reliability summary** |
| 2 | The static timetable went **stale in 6 days** (13.7% join) | **Daily automatic refresh** with the version gate, plus a **join-health** warning |
| 3 | A public `/chat` could **drain the Anthropic credit** | **Rate limits**: per IP per minute and per day, plus a **global daily cap** |

## How to use

1. Save as `docs/milestones/M6_5_production_readiness.md`.
2. In Claude Code (repo root):

   > Read `docs/milestones/M6_5_production_readiness.md` and follow it in build mode. Stop and report at each STOP line.

## Rules for Claude Code

- **Build mode:** one-line explanations; no teaching questions.
- **Inspect before editing.** Read each file first; run `git status` at the start. Where this playbook gives a full file, compare it with the current one and **keep any later fixes** (for example the Milestone 5 and 6 prompt rules in `agent.py`), then report what you merged.
- **Never** run `git commit`, `push`, `reset`, `restore`, `stash`, delete files, or **drop tables**, unless Sandy explicitly asks.
- **PowerShell.** Run `uv run ...` from `backend\`, and `docker compose ...` from the repo root.
- **Never print secrets.**
- **Test DB vs dev DB:** eval and integration work uses `transit_test` in a **separate terminal**. Close it afterwards.
- **Paid LLM calls** only after Sandy confirms.
- **Honest status:** done only when the Done-when checks pass.

---

## Step 1: Settings

**1a.** Replace `backend/app/config.py` (read it first; this keeps every existing setting, including `FIXED_NOW`):

```python
from datetime import datetime
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> parents[2] is the repo root, where .env lives
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    # Postgres
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # NTA GTFS-Realtime (optional here so the static loader still runs without it)
    nta_api_key: str | None = None
    nta_trip_updates_url: str = "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates"
    rt_poll_interval_seconds: int = 60
    rt_retention_days: int = 2            # detailed latest-delay rows (rt_trips, rt_stop_delays)
    rt_summary_retention_days: int = 90   # per-route daily reliability summary (tiny)

    # Static timetable refresh (Milestone 6.5)
    static_gtfs_url: str = "https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip"
    static_refresh_hour: int = 4          # Dublin local time
    join_health_min_percent: float = 90.0

    # API (Milestone 4)
    timezone: str = "Europe/Dublin"
    realtime_stale_seconds: int = 300  # older realtime than this is ignored, not applied

    # Eval (Milestone 6): freeze "now" so known-answer questions stay valid. Leave unset in production.
    fixed_now: datetime | None = None

    # Agent (Milestone 5)
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-5-5"
    api_base_url: str = "http://localhost:8000"

    # /chat protection (Milestone 6.5)
    chat_per_ip_per_minute: int = 10
    chat_per_ip_per_day: int = 100
    chat_global_per_day: int = 500        # hard cap on LLM spend, whatever the number of IPs
    trust_proxy_headers: bool = False     # True only behind our own reverse proxy (Milestone 8)

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
```

**1b.** In **both** `.env` and `.env.example` (read first; edit or append only these lines):

```dotenv
RT_RETENTION_DAYS=2
RT_SUMMARY_RETENTION_DAYS=90
```

`RT_RETENTION_DAYS` now means **detailed latest-delay rows**. Reliability history lives in the summary table for 90 days.

### Done when
- [ ] `uv run pytest -v` still passes

---

## Step 2: Compact realtime schema

Create `backend/sql/005_rt_compact.sql`:

```sql
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
```

Apply it to the **dev** database (repo root):

```powershell
(Get-Item backend\sql\005_rt_compact.sql).Length
Get-Content backend\sql\005_rt_compact.sql | docker compose exec -T db psql -U transit -d transit -v ON_ERROR_STOP=1
docker compose exec db psql -U transit -d transit -P pager=off -c "\dt rt_*"
```

### Done when
- [ ] `\dt rt_*` lists `rt_trips`, `rt_stop_delays` and `rt_route_daily` (the old tables are still there and untouched)

---

## Step 3: Storage code and the poller

**3a.** Create `backend/app/rt_store.py`:

```python
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
```

**3b.** Replace `backend/app/rt_poller.py` (read it first). Storage now lives in `rt_store.py`; the poller adds a **15-minute rollup** and the new retention. `store_snapshot` is re-exported, so existing imports keep working.

```python
"""Poll NTA GTFS-Realtime TripUpdates and store each new snapshot in Postgres.

Run from backend\\:
    uv run python -m app.rt_poller --once      # one poll, then exit (for testing)
    uv run python -m app.rt_poller             # loop forever (Ctrl+C to stop)
    uv run python -m app.rt_poller --save-raw ..\\data\\rt_raw   # also keep raw .pb files
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg

from app.config import Settings
from app.rt_parser import parse_feed
from app.rt_store import TZ, apply_retention, rollup_recent, store_snapshot  # noqa: F401 (store_snapshot re-exported)

log = logging.getLogger("rt_poller")

RETENTION_EVERY_SECONDS = 3600
ROLLUP_EVERY_SECONDS = 900  # refresh today's/yesterday's reliability summary every 15 min


class FatalConfigError(Exception):
    """Retrying cannot fix this (e.g. a rejected API key)."""


def backoff_seconds(failures: int, base: float, cap: float = 600.0) -> float:
    """Exponential backoff: base*2, base*4, ... capped. failures >= 1."""
    return min(cap, base * (2 ** failures))


def fetch_feed(client: httpx.Client, url: str, api_key: str) -> bytes:
    # No ?format=json -> the API returns protobuf. Key goes in a header, never the URL.
    resp = client.get(url, headers={"x-api-key": api_key})
    if resp.status_code in (401, 403):
        raise FatalConfigError(f"NTA API rejected the key (HTTP {resp.status_code})")
    resp.raise_for_status()  # 429 / 5xx -> httpx.HTTPStatusError -> retried with backoff
    return resp.content


def run(settings: Settings, once: bool = False, save_raw: Path | None = None) -> None:
    if not settings.nta_api_key:
        raise SystemExit("NTA_API_KEY is not set in .env")
    if save_raw:
        save_raw.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()

    def _request_stop(signum, frame):
        log.info("Shutdown requested, finishing current step...")
        stop.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)  # what `docker stop` sends

    interval = settings.rt_poll_interval_seconds
    failures = 0
    last_ts = None
    last_retention: float | None = None
    last_rollup: float | None = None
    conn: psycopg.Connection | None = None

    with httpx.Client(timeout=30.0) as client:
        while not stop.is_set():
            started = time.monotonic()
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(settings.database_url, autocommit=True)

                data = fetch_feed(client, settings.nta_trip_updates_url, settings.nta_api_key)
                parsed = parse_feed(data)

                if parsed.feed_timestamp == last_ts:
                    log.info("Feed unchanged (ts=%s), skipping", parsed.feed_timestamp.isoformat())
                else:
                    snapshot_id = store_snapshot(conn, parsed)
                    if snapshot_id is None:
                        log.info("Snapshot ts=%s already in DB, skipping", parsed.feed_timestamp.isoformat())
                    else:
                        log.info(
                            "Stored snapshot %d: ts=%s trips=%d stop_updates=%d bytes=%d",
                            snapshot_id, parsed.feed_timestamp.isoformat(),
                            len(parsed.trip_rows), len(parsed.stop_rows), len(data),
                        )
                        if save_raw:
                            name = parsed.feed_timestamp.strftime("%Y%m%dT%H%M%SZ") + ".pb"
                            (save_raw / name).write_bytes(data)
                    last_ts = parsed.feed_timestamp

                if last_rollup is None or time.monotonic() - last_rollup > ROLLUP_EVERY_SECONDS:
                    n = rollup_recent(conn, datetime.now(timezone.utc).astimezone(TZ).date())
                    last_rollup = time.monotonic()
                    log.info("Rollup: %d route-day summary rows refreshed", n)

                if last_retention is None or time.monotonic() - last_retention > RETENTION_EVERY_SECONDS:
                    deleted = apply_retention(conn, settings.rt_retention_days, settings.rt_summary_retention_days)
                    last_retention = time.monotonic()
                    if any(deleted.values()):
                        log.info("Retention: deleted %s", deleted)

                failures = 0
                wait = interval
            except FatalConfigError:
                log.error("Fatal configuration error, stopping")
                raise
            except Exception:
                if once:
                    raise
                failures += 1
                wait = backoff_seconds(failures, interval)
                log.exception("Poll failed (attempt %d), retrying in %.0fs", failures, wait)
                if conn is not None and not conn.closed:
                    conn.close()  # reconnect cleanly next time
                conn = None

            if once:
                break
            # Keep >= interval between request STARTS (rate limit), wake early on shutdown.
            stop.wait(max(0.0, wait - (time.monotonic() - started)))

    if conn is not None and not conn.closed:
        conn.close()
    log.info("Poller stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll NTA GTFS-Realtime TripUpdates into Postgres")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--save-raw", type=Path, help="directory to also save raw .pb snapshots")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(Settings(), once=args.once, save_raw=args.save_raw)


if __name__ == "__main__":
    main()
```

### Done when
- [ ] `uv run pytest -v` passes (the parser and backoff tests are unchanged)

---

## Step 4: API queries use the compact tables

**4a.** Replace `backend/app/transit_db.py` from the `# ---------- departures ----------` line to the end of the file with the code below. Keep everything above that line (time helpers, lookups, `active_service_ids`, `latest_snapshot`). This version **includes the effective-time departures logic** Claude Code built in Milestone 6. Compare it with the current code and report any difference in behaviour. Two improvements come with it:
- delays are matched on **(trip, service day)**, which fixes the after-midnight `start_date` item from Later
- on-time performance reads the **daily summary**, so it no longer scans raw rows

```python
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
```

**4b.** In `backend/app/api.py`, make two small edits:
- in `on_time_performance`, pass the time zone: `return db.on_time_performance(conn, route, now_utc(), days=days, tz=TZ)`
- in `health`, add `"join_health": db.join_health(conn),` to the returned dict

### Done when
- [ ] `uv run pytest -v` passes

---

## Step 5: Daily static refresh

**5a.** Create `backend/app/static_refresh.py`:

```python
"""Daily static timetable refresh (Milestone 6.5).

Download the NTA static GTFS zip, reload only if its feed_version changed (the existing version gate),
then check join health: the % of live realtime trips found in the timetable. Below the threshold -> WARNING.

Run from backend\\:
    uv run python -m app.static_refresh --once     # one check now
    uv run python -m app.static_refresh --daily    # loop: every day at STATIC_REFRESH_HOUR (Dublin time)
"""

from __future__ import annotations

import argparse
import logging
import signal
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg.rows import dict_row

from app.config import Settings
from app.gtfs_loader import load_feed, read_feed_info
from app.transit_db import join_health

log = logging.getLogger("static_refresh")


def zip_feed_version(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        return read_feed_info(zf)["feed_version"]


def download(url: str, dest: Path) -> int:
    """Stream the zip to disk (it's ~170 MB; never hold it in memory)."""
    size = 0
    with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(1024 * 1024):
                fh.write(chunk)
                size += len(chunk)
    return size


def refresh_once(settings: Settings) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "GTFS_Realtime.zip"
        size = download(settings.static_gtfs_url, zip_path)
        new_version = zip_feed_version(zip_path)
        with psycopg.connect(settings.database_url) as conn:  # one transaction for the reload
            loaded = load_feed(conn, zip_path)  # skips by itself when the version is unchanged
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        health = join_health(conn)
    status = {"downloaded_bytes": size, "feed_version": new_version, "reloaded": loaded, "join_health": health}
    if health and health["percent"] is not None and health["percent"] < settings.join_health_min_percent:
        log.warning("JOIN HEALTH LOW: %s%% of realtime trips match the timetable (min %s%%)",
                    health["percent"], settings.join_health_min_percent)
    log.info("Static refresh: %s", status)
    return status


def seconds_until(hour: int, tz: ZoneInfo, now: datetime | None = None) -> float:
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_daily(settings: Settings) -> None:
    tz = ZoneInfo(settings.timezone)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda s, f: stop.set())
    signal.signal(signal.SIGTERM, lambda s, f: stop.set())
    while not stop.is_set():
        wait = seconds_until(settings.static_refresh_hour, tz)
        log.info("Next static refresh in %.1f hours", wait / 3600)
        if stop.wait(wait):
            break
        try:
            refresh_once(settings)
        except Exception:
            log.exception("Static refresh failed; will try again tomorrow")
    log.info("Static refresh stopped")


def main() -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--daily", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    if args.once:
        refresh_once(settings)
    else:
        run_daily(settings)


if __name__ == "__main__":
    main()
```

**5b.** Create `backend/tests/test_static_refresh.py`:

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.evals.paths import FIXTURE_ZIP
from app.static_refresh import seconds_until, zip_feed_version

DUBLIN = ZoneInfo("Europe/Dublin")


def test_seconds_until_later_today():
    now = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)  # 01:00 Dublin (GMT in November)
    assert seconds_until(4, DUBLIN, now) == 3 * 3600


def test_seconds_until_rolls_to_tomorrow():
    now = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
    assert seconds_until(4, DUBLIN, now) == 23 * 3600


@pytest.mark.skipif(not FIXTURE_ZIP.exists(), reason="fixture not built")
def test_zip_feed_version_reads_feed_info():
    assert zip_feed_version(FIXTURE_ZIP).startswith("FIXTURE-")
```

### Done when
- [ ] The new tests pass

---

## Step 6: Protect `/chat`

**6a.** Create `backend/app/rate_limit.py`:

```python
"""In-memory sliding-window rate limiter for /chat (Milestone 6.5).

Three limits: per IP per minute, per IP per day, and a GLOBAL daily cap that bounds LLM spend no matter
how many IPs show up. In-memory = one process only; several replicas would need Redis (Later).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

MINUTE = 60.0
DAY = 86_400.0


class RateLimiter:
    def __init__(self, per_ip_minute: int, per_ip_day: int, global_day: int, clock: Callable[[], float] = time.monotonic):
        self.per_ip_minute = per_ip_minute
        self.per_ip_day = per_ip_day
        self.global_day = global_day
        self.clock = clock
        self._ip: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._lock = threading.Lock()

    @staticmethod
    def _prune(q: deque[float], now: float, window: float) -> None:
        while q and q[0] <= now - window:
            q.popleft()

    def check(self, key: str) -> float | None:
        """Record a request. Returns None if allowed, else seconds to wait (for Retry-After)."""
        with self._lock:
            now = self.clock()
            self._prune(self._global, now, DAY)
            hits = self._ip.setdefault(key, deque())
            self._prune(hits, now, DAY)

            if len(self._global) >= self.global_day:
                return self._global[0] + DAY - now
            if len(hits) >= self.per_ip_day:
                return hits[0] + DAY - now
            recent = [t for t in hits if t > now - MINUTE]
            if len(recent) >= self.per_ip_minute:
                return recent[0] + MINUTE - now

            hits.append(now)
            self._global.append(now)
            if len(self._ip) > 10_000:  # bound memory: forget idle IPs
                for k in [k for k, q in self._ip.items() if not q]:
                    del self._ip[k]
            return None
```

**6b.** Replace `backend/app/agent_api.py` (read it first):

```python
"""Milestone 5: HTTP endpoint for the agent (the chat UI in Milestone 7 calls this).

Run from backend\\:  uv run uvicorn app.agent_api:app --port 8001
"""

from __future__ import annotations

import math
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from app.agent import answer
from app.config import Settings
from app.rate_limit import RateLimiter

app = FastAPI(title="Dublin Transit AI agent", version="0.1.0")
settings = Settings()
limiter = RateLimiter(settings.chat_per_ip_per_minute, settings.chat_per_ip_per_day, settings.chat_global_per_day)


def client_ip(request: Request) -> str:
    """Behind our own proxy (Milestone 8) the real IP is the first X-Forwarded-For entry; never trust it otherwise."""
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    history: list[Turn] = Field(default_factory=list, max_length=20)


class ChatResponse(BaseModel):
    answer: str
    tool_calls: list[dict]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request) -> ChatResponse:
    retry_after = limiter.check(client_ip(request))
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(math.ceil(retry_after))},
        )
    history = [HumanMessage(t.content) if t.role == "user" else AIMessage(t.content) for t in req.history]
    try:
        out = answer(req.message, history)
    except RuntimeError as exc:  # e.g. missing ANTHROPIC_API_KEY
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ChatResponse(answer=out["answer"], tool_calls=out["tool_calls"])
```

**6c.** Create `backend/tests/test_rate_limit.py`:

```python
from app.rate_limit import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_per_minute_limit_then_recovers():
    clock = FakeClock()
    rl = RateLimiter(per_ip_minute=2, per_ip_day=100, global_day=100, clock=clock)
    assert rl.check("a") is None
    assert rl.check("a") is None
    retry = rl.check("a")
    assert retry is not None and 0 < retry <= 60
    clock.t += 61
    assert rl.check("a") is None


def test_ips_are_independent():
    rl = RateLimiter(per_ip_minute=1, per_ip_day=100, global_day=100, clock=FakeClock())
    assert rl.check("a") is None
    assert rl.check("b") is None
    assert rl.check("a") is not None


def test_global_daily_cap_blocks_everyone():
    clock = FakeClock()
    rl = RateLimiter(per_ip_minute=100, per_ip_day=100, global_day=3, clock=clock)
    for ip in ("a", "b", "c"):
        assert rl.check(ip) is None
    assert rl.check("new-ip") is not None  # cost bound holds even for a fresh IP
    clock.t += 86_401
    assert rl.check("new-ip") is None


def test_blocked_requests_do_not_count():
    clock = FakeClock()
    rl = RateLimiter(per_ip_minute=1, per_ip_day=2, global_day=100, clock=clock)
    assert rl.check("a") is None
    for _ in range(5):
        assert rl.check("a") is not None  # rejected, so not recorded
    clock.t += 61
    assert rl.check("a") is None  # 2nd allowed request of the day
```

### Done when
- [ ] The rate-limit tests pass, including "the global daily cap blocks even a fresh IP"

---

## Step 7: Eval fixture and integration tests

**7a.** In `backend/app/evals/load_fixture.py` (read it first), apply three changes:
- import `from app.rt_store import TZ, rollup_daily, store_snapshot` (instead of from `rt_poller`), plus `from datetime import timedelta`
- truncate the new tables too: `TRUNCATE rt_snapshots, rt_trips, rt_stop_delays, rt_route_daily CASCADE`
- after storing the snapshots, run the rollup for each snapshot's day and the day before. Reference version:

```python
"""Load eval/fixtures into a TEST database (refuses any database whose name doesn't end in _test).

Run from backend\\ with POSTGRES_DB=transit_test:
    uv run python -m app.evals.load_fixture
"""

from __future__ import annotations

import re
from datetime import timedelta

import psycopg

from app.config import Settings
from app.evals.paths import FIXTURE_RT_DIR, FIXTURE_ZIP, SQL_DIR
from app.gtfs_loader import load_feed
from app.rt_parser import parse_feed
from app.rt_store import TZ, rollup_daily, store_snapshot

MIGRATION = re.compile(r"^\d{3}_.+\.sql$")  # 001_schema.sql ... skips verify_realtime.sql


def main() -> None:
    settings = Settings()
    if not settings.postgres_db.endswith("_test"):
        raise SystemExit(f"refusing to load fixtures into '{settings.postgres_db}': name must end with _test")

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        for path in sorted(p for p in SQL_DIR.iterdir() if MIGRATION.match(p.name)):
            conn.execute(path.read_text(encoding="utf-8"))
            print(f"applied {path.name}")
        conn.execute("TRUNCATE rt_snapshots, rt_trips, rt_stop_delays, rt_route_daily CASCADE")

    with psycopg.connect(settings.database_url) as conn:  # one transaction for the static load
        load_feed(conn, FIXTURE_ZIP)

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        days = set()
        for pb in sorted(FIXTURE_RT_DIR.glob("*.pb")):
            parsed = parse_feed(pb.read_bytes())
            sid = store_snapshot(conn, parsed)
            day = parsed.feed_timestamp.astimezone(TZ).date()
            days |= {day, day - timedelta(days=1)}
            print(f"snapshot {pb.name} -> id {sid}")
        print(f"rollup: {rollup_daily(conn, sorted(days))} route-day rows")
        conn.execute("ANALYZE")
    print("Fixture loaded.")


if __name__ == "__main__":
    main()
```

**7b.** Create `backend/tests/integration/test_rt_store.py`:

```python
"""Compact realtime storage against the loaded fixture."""

import psycopg
from psycopg.rows import dict_row

from app.config import Settings
from app.evals.paths import FIXTURE_RT_DIR
from app.rt_parser import parse_feed
from app.rt_store import store_snapshot


def _conn():
    return psycopg.connect(Settings().database_url, autocommit=True, row_factory=dict_row)


def test_one_row_per_trip_day_stop():
    with _conn() as conn:
        dupes = conn.execute(
            "SELECT count(*) AS n FROM (SELECT 1 FROM rt_stop_delays GROUP BY trip_id, start_date, stop_sequence HAVING count(*) > 1) x"
        ).fetchone()["n"]
        assert dupes == 0
        assert conn.execute("SELECT count(*) AS n FROM rt_stop_delays").fetchone()["n"] > 0


def test_storing_the_same_snapshot_again_is_a_no_op():
    pb = sorted(FIXTURE_RT_DIR.glob("*.pb"))[-1]
    with _conn() as conn:
        before = conn.execute("SELECT count(*) AS n FROM rt_stop_delays").fetchone()["n"]
        assert store_snapshot(conn, parse_feed(pb.read_bytes())) is None
        after = conn.execute("SELECT count(*) AS n FROM rt_stop_delays").fetchone()["n"]
    assert before == after


def test_daily_summary_exists_for_fixture_routes(meta):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT route_short_name, observations FROM rt_route_daily WHERE route_short_name = ANY(%s)",
            (meta["routes"],),
        ).fetchall()
    assert rows and all(r["observations"] > 0 for r in rows)


def test_compact_rows_far_fewer_than_appended_rows():
    with _conn() as conn:
        snaps = conn.execute("SELECT sum(stop_time_update_count) AS appended FROM rt_snapshots").fetchone()["appended"]
        kept = conn.execute("SELECT count(*) AS n FROM rt_stop_delays").fetchone()["n"]
    assert kept < snaps  # upserts collapse repeated predictions
```

**7c.** Append to `backend/tests/integration/test_api_fixture.py`:

```python
def test_health_reports_join_health(client):
    jh = client.get("/health").json()["join_health"]
    assert jh is not None and jh["rt_trips"] > 0
```

**7d.** In a **new terminal** (`backend\`), rebuild and re-check the eval. The fixture files don't change; only how they're stored does:

```powershell
$env:POSTGRES_DB = "transit_test"
uv run python -m app.evals.load_fixture
uv run python -m app.evals.build_cases
uv run pytest -v
uv run python -m app.evals.run_eval --oracle
```

**STOP:** report the test count, the oracle result and how the reliability cases' expected percentages changed (they now come from the summary). Then ask Sandy before running the **paid** 15-case sample (same commands as Milestone 6, Step 6, with the API on port 8010). Close this terminal afterwards.

### Done when
- [ ] All tests pass (unit and integration, none skipped)
- [ ] The oracle eval is 100/100, Gate PASS
- [ ] After Sandy's OK: the 15-case real sample passes the gate, with safety 100%

---

## Step 8: Docker Compose

**8a.** Add this service to `docker-compose.yml` (read it first; keep the others unchanged):

```yaml
  static-refresh:
    build: ./backend
    command: ["python", "-m", "app.static_refresh", "--daily"]
    environment:
      POSTGRES_HOST: db
      POSTGRES_PORT: 5432
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped
```

**8b.** For the `rt-poller` service, add `RT_RETENTION_DAYS: ${RT_RETENTION_DAYS:-2}` and `RT_SUMMARY_RETENTION_DAYS: ${RT_SUMMARY_RETENTION_DAYS:-90}`, replacing the old retention line if there is one.

**8c.** Rebuild and restart (repo root, with no local servers on 8000 or 8001):

```powershell
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail 20 rt-poller
docker compose logs --tail 5 static-refresh
```

**8d.** Run one refresh now. It downloads about 170 MB and reloads **only if** the NTA published a new version. If it reloads, the API is slow for a few minutes because of the `TRUNCATE` lock.

```powershell
docker compose run --rm static-refresh python -m app.static_refresh --once
```

**8e.** Check the deployed endpoints:

```powershell
Invoke-RestMethod "http://localhost:8000/health" | ConvertTo-Json -Depth 4
Invoke-RestMethod "http://localhost:8000/on-time-performance?route=39A" | ConvertTo-Json
1..12 | ForEach-Object { try { (Invoke-WebRequest -Method Post -Uri "http://localhost:8001/chat" -ContentType "application/json" -Body '{"message":"hi"}' -UseBasicParsing).StatusCode } catch { $_.Exception.Response.StatusCode.value__ } }
```

The last command makes paid calls until it's blocked: **10 × 200, then 429**. Ask Sandy before running it, or temporarily set `CHAT_PER_IP_PER_MINUTE=2` to keep it to 2 paid calls.

### Done when
- [ ] `ps` shows 5 services: `db`, `rt-poller`, `api`, `agent`, `static-refresh`
- [ ] The poller logs `Stored snapshot` and `Rollup: N route-day summary rows refreshed`
- [ ] `static-refresh` logs `Next static refresh in X hours`
- [ ] `/health` shows `join_health.percent` at 90% or more
- [ ] `/on-time-performance` returns data. **Note:** reliability history restarts from the switch-over, so it shows "today only" at first.
- [ ] `/chat` returns **429 with a `Retry-After` header** once over the limit

---

## Step 9: Measure the storage win, then the old tables

Let the poller run for **at least 3 hours**, then run (repo root):

```powershell
docker compose exec db psql -U transit -d transit -P pager=off -c "SELECT relname AS tbl, pg_size_pretty(pg_total_relation_size(relid)) AS size, n_live_tup AS rows FROM pg_stat_user_tables WHERE relname LIKE 'rt_%' ORDER BY pg_total_relation_size(relid) DESC;"
```

Calculate the **new MB per day** for `rt_trips`, `rt_stop_delays` and `rt_route_daily` (size ÷ hours × 24) and compare it with the old **about 2.95 GB/day**.

Also update `backend/sql/verify_realtime.sql` so its queries use `rt_trips` and `rt_stop_delays` instead of the old tables.

**STOP:** report the before and after numbers. Then **ask Sandy** whether to drop the old tables, which frees their disk space immediately. Only with an explicit yes:

```powershell
docker compose exec db psql -U transit -d transit -c "DROP TABLE rt_stop_time_updates, rt_trip_updates;"
```

### Done when
- [ ] The storage per day is measured and reported, with no estimates
- [ ] The old tables are dropped **only if Sandy said yes**

**Final report:** Done (only what passed), Next (**Milestone 7: chat UI**), Later, and the reminder that nothing is committed.

---

## Later (not in this milestone)

- **Zero-downtime static reload:** load into staging tables, then swap atomically, so the 4 am reload doesn't block reads
- **Rate limits across replicas:** move the limiter to Redis if `/chat` ever runs on more than one instance
- **Alerting:** send the join-health warning somewhere you'll see it (email or a webhook), not just the logs
- **Backfill** the daily summary from the old tables before dropping them, if you want older history
- Have `answer()` return the number of model calls, for cost per eval run
- **Route delays can double-count a trip reported on two service days** (`route_delays` groups by trip and service day)

---

# Learn later (for Sandy)

## Overview (the one story)

The prototype worked, but it wasn't safe to leave running. Three things would have hurt in production, and each got a standard fix:
1. **Storage:** every minute it saved the *whole* feed again (11,000 rows), mostly repeating predictions that hadn't changed. Now it **upserts**: one row per trip, day and stop, always holding the *latest* delay. That latest value is exactly what reliability needs ("the last reported delay"), and a tiny **per-route daily summary** keeps 90 days of history. This is the **raw + rollup** pattern.
2. **Freshness:** the timetable changes every few days. A daily job downloads it, the existing **version gate** skips unchanged feeds, and **join health** (the % of live trips found in the timetable) warns when the two feeds drift.
3. **Cost and abuse:** an LLM endpoint costs money per request, so it gets **per-IP limits** plus a **global daily cap**. The cap bounds the worst-case bill, however many IPs show up.

## Interview questions

1. **"How did you cut storage?"** Replaced append-only snapshots with upserts keyed on (trip, service day, stop), done as COPY into a temp table plus one `INSERT … ON CONFLICT DO UPDATE`. Plus a daily rollup table for long history. *[fill in: X GB/day → Y MB/day]*.
2. **"Why the `WHERE existing.feed_timestamp < new.feed_timestamp` guard on the upsert?"** If an older snapshot ever arrives late, it can't overwrite newer data. Upserts must be **order-safe**.
3. **"Why `DISTINCT ON` before `ON CONFLICT`?"** Postgres can't update the same row twice in one statement, so duplicates inside one snapshot are removed first.
4. **"How do you know the timetable is stale?"** Join health on every snapshot: if the % of realtime trips found in the static timetable drops below 90%, log a warning. It caught 13.7% in practice.
5. **"How do you protect an LLM endpoint?"** Sliding-window limits per IP (minute and day), a global daily cap for cost, `Retry-After` on a 429, and a provider-side spend limit as the last line of defence. It's in-memory now, with Redis as the path to multiple replicas.
6. **"Why trust `X-Forwarded-For` only behind your own proxy?"** Clients can set that header themselves, so trusting it blindly lets anyone dodge per-IP limits.

## STAR: storage

- **S:** The realtime poller stored about 2.05 MB per snapshot, roughly 2.95 GB per day, which would fill a cloud disk within days.
- **T:** Keep the same answers (live delays and weekly reliability) at a fraction of the storage.
- **A:** Switched to upserting the latest delay per trip, day and stop, added a per-route daily summary table refreshed every 15 minutes, and split retention: 2 days detailed, 90 days summary. The 100-question eval confirmed the API answers were unchanged.
- **R:** *[fill in: measured MB per day after the change, and the % reduction]*.
