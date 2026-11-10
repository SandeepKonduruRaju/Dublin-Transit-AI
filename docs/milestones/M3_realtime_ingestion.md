# Milestone 3: Realtime ingestion (Claude Code playbook)

Dublin Transit AI. Learn it, then build it, one part at a time.

## How to use this file

1. Save this file in the repo as `docs/milestones/M3_realtime_ingestion.md`.
2. Open Claude Code in the repo root (`C:\dev\dublin-transit-ai`) and say:

   > Read `docs/milestones/M3_realtime_ingestion.md` and follow it exactly. Start with the Kickoff check.

3. Say **"next"** to move on to the next part. Say **"just build"** to skip the teaching for a part.

---

## Rules for Claude Code (follow these every time)

**Teaching loop, for each part:**

1. Explain the **Learn** section in short, simple words.
2. Ask the **Check question**, then **stop and wait** for Sandy's answer.
3. If Sandy is unsure or wrong, give the **hint** first. If he's still stuck, give the answer.
4. Then do the **Build** steps and the **Run** steps.
5. End with a status block: **Done**, **Next**, **Later**.

**Working rules:**

- **Inspect before editing.** Read a file before changing it. Run `git status` before the first change of each session. Never guess file contents.
- **Never** run or suggest `git commit`, `git push`, `git reset`, `git restore`, `git stash`, or any delete, unless Sandy explicitly asks.
- **PowerShell on Windows.** Run every `uv run ...` command from `backend\`. Docker and `psql` commands work from anywhere in the repo.
- **Never print, echo or log the NTA API key.** Read it from `.env` only.
- **Only one poller at a time.** The NTA allows about 1 request per minute per key. Never run the local poller and the Docker poller together. Wait 60 seconds between any two manual API calls.
- Be honest about status: built vs next vs later. Never call something done until its **Done when** checks pass.
- Keep answers short, with proper technical terms. Teach only the essentials needed for building and for interviews.
- Ignore the warning `VIRTUAL_ENV=...\.venv does not match`. It comes from an old root `.venv` that is still activated in the terminal and is harmless.

---

## Current state (before Milestone 3)

**Built (Milestones 1 and 2):**

- Repo `dublin-transit-ai` on `main`, with the monorepo layout `backend/`, `web/`, `eval/`, `data/`
- Postgres 17 in Docker Compose. The host port is **5433** (5432 is taken by a native Windows Postgres).
- `backend/` is a uv project with `psycopg[binary]`, `pydantic-settings` and `pytest` (dev).
- `backend/app/config.py` (Settings read from the repo-root `.env`)
- `backend/app/gtfs_loader.py`: the static GTFS loader (`COPY`, one transaction, version-gated)
- `backend/sql/001_schema.sql` (6 static tables) and `backend/sql/002_feed_info.sql`
- Static feed `2407DE34-...` loaded: 9,540,233 `stop_times` rows
- 10 unit tests passing
- `data/GTFS_Realtime.zip` and `data/tripupdates_test.json` (a 2.6 MB JSON sample)

**Facts already verified:**

- Endpoint: `https://api.nationaltransport.ie/gtfsr/v2/TripUpdates`. The key goes in the `x-api-key` header.
- Without `?format=json`, the API returns **protobuf**.
- The 46A is **not** in the current feed (BusConnects E1 and E2 cover that corridor). The 39A and 15 exist.

### Kickoff check

Run these and summarise the results before Part 1. Change nothing yet.

```powershell
cd C:\dev\dublin-transit-ai
git status
git log --oneline -5
docker compose ps
Get-ChildItem backend\app, backend\sql, backend\tests -Name
```

**Expected:** `db` is healthy on `5433`, and the files listed above exist. If anything differs, report it and stop.

---

## Part 1 of 6: GTFS-RT format and the parser

### Learn

- **Structure:** `FeedMessage` → `header.timestamp` + `entity[]` → `trip_update` → `trip` (trip_id, route_id, schedule_relationship) + `stop_time_update[]` (stop_sequence, stop_id, arrival/departure delay in seconds).
- **Protobuf vs JSON:** protobuf is binary, schema-defined, smaller and typed. JSON is readable but large. The poller uses **protobuf**; JSON is for debugging.
- **Deduplication key:** `header.timestamp` is when the NTA generated the feed. The same timestamp means the same feed, so skip it.
- **Missing vs zero:** a delay that isn't in the feed is stored as `NULL`, never `0`. `0` means "on time"; `NULL` means "unknown".
- **Interview line:** *"I poll GTFS-RT as protobuf, which is several times smaller than JSON per snapshot, and parse it with the official bindings into typed rows, deduping on the header timestamp."*

### Check question

> Why does the parser store a missing delay as `None` (NULL) instead of `0`?

- **Hint:** what does a delay of `0` claim about the bus?
- **Answer:** `0` claims the bus is exactly on time. If unknown delays became `0`, the "how reliable was route 15 this week" numbers would look better than reality. `NULL` keeps "unknown" separate from "on time", and SQL's `avg()` skips NULLs automatically.

### Build

1. Add dependencies (from `backend\`):

```powershell
uv add httpx gtfs-realtime-bindings
```

2. Create `backend/app/rt_parser.py`:

```python
"""Parse a GTFS-Realtime TripUpdates protobuf into plain rows.

Pure functions: no network, no database. That makes this the easy part to unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from google.transit import gtfs_realtime_pb2

TripRel = gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship
StopRel = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.ScheduleRelationship

# Column order must match the tuples built below (snapshot_id is prepended when storing).
TRIP_COLUMNS = (
    "entity_id", "trip_id", "route_id", "start_date", "start_time",
    "direction_id", "schedule_relationship",
)
STOP_COLUMNS = (
    "entity_id", "trip_id", "stop_sequence", "stop_id",
    "arrival_delay", "arrival_time", "departure_delay", "departure_time",
    "schedule_relationship",
)


@dataclass(frozen=True)
class ParsedFeed:
    feed_timestamp: datetime
    trip_rows: list[tuple]
    stop_rows: list[tuple]


def _opt(msg, field: str):
    """Return the field value, or None if it was never set (proto2 presence)."""
    return getattr(msg, field) if msg.HasField(field) else None


def _utc(seconds: int | None) -> datetime | None:
    return datetime.fromtimestamp(seconds, tz=timezone.utc) if seconds is not None else None


def _gtfs_date(value: str | None) -> date | None:
    return datetime.strptime(value, "%Y%m%d").date() if value else None


def _enum_name(enum, value: int) -> str:
    try:
        return enum.Name(value)
    except ValueError:  # a value newer than our bindings know about
        return str(value)


def _event(stu, name: str) -> tuple[int | None, datetime | None]:
    """(delay, time) for stu.arrival or stu.departure."""
    if not stu.HasField(name):
        return None, None
    ev = getattr(stu, name)
    return _opt(ev, "delay"), _utc(_opt(ev, "time"))


def parse_feed(data: bytes) -> ParsedFeed:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(data)  # raises google.protobuf.message.DecodeError on garbage
    if not feed.header.HasField("timestamp"):
        raise ValueError("feed header has no timestamp, cannot dedupe")

    trip_rows: list[tuple] = []
    stop_rows: list[tuple] = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue  # vehicle positions / alerts are not stored here
        tu = entity.trip_update
        trip = tu.trip
        trip_id = _opt(trip, "trip_id")
        trip_rows.append((
            entity.id,
            trip_id,
            _opt(trip, "route_id"),
            _gtfs_date(_opt(trip, "start_date")),
            _opt(trip, "start_time"),
            _opt(trip, "direction_id"),
            _enum_name(TripRel, trip.schedule_relationship),
        ))
        for stu in tu.stop_time_update:
            arr_delay, arr_time = _event(stu, "arrival")
            dep_delay, dep_time = _event(stu, "departure")
            stop_rows.append((
                entity.id,
                trip_id,
                _opt(stu, "stop_sequence"),
                _opt(stu, "stop_id"),
                arr_delay,
                arr_time,
                dep_delay,
                dep_time,
                _enum_name(StopRel, stu.schedule_relationship),
            ))

    return ParsedFeed(_utc(feed.header.timestamp), trip_rows, stop_rows)
```

### Run

Fetch **one** protobuf sample (wait 60 seconds if you called the API recently) and compare its size with the JSON sample. From `backend\`:

```powershell
$key = ((Get-Content ..\.env | Select-String '^NTA_API_KEY=').Line -split '=', 2)[1]
Invoke-WebRequest -Uri "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates" -Headers @{ "x-api-key" = $key } -OutFile ..\data\tripupdates_sample.pb
"{0:N0} bytes protobuf vs {1:N0} bytes JSON" -f (Get-Item ..\data\tripupdates_sample.pb).Length, (Get-Item ..\data\tripupdates_test.json).Length
uv run python -c "from pathlib import Path; from app.rt_parser import parse_feed; p = parse_feed(Path('../data/tripupdates_sample.pb').read_bytes()); print(p.feed_timestamp, len(p.trip_rows), 'trips', len(p.stop_rows), 'stop updates')"
```

### Done when

- [ ] `httpx` and `gtfs-realtime-bindings` appear in `backend/pyproject.toml`
- [ ] The protobuf sample size is recorded (Sandy notes the ratio against the JSON sample)
- [ ] The parser prints a recent UTC timestamp and non-zero trip and stop-update counts

---

## Part 2 of 6: Realtime schema

### Learn

- **Three tables:** `rt_snapshots` (one row per saved feed), `rt_trip_updates` (one per trip per snapshot), `rt_stop_time_updates` (one per stop update per trip per snapshot).
- **Trip level and stop level are separate:** a CANCELED trip has no stop updates, so without `rt_trip_updates` you couldn't answer "is the 39A running?".
- **No foreign keys to the static tables:** realtime can contain ADDED trips the timetable doesn't, and the static reload TRUNCATEs those tables. You still join on `trip_id`; the database just doesn't enforce it.
- **`ON DELETE CASCADE`** from the child tables to `rt_snapshots`: deleting an old snapshot removes its rows too. That's what makes retention (Part 4) a one-line `DELETE`.
- **`UNIQUE (feed_timestamp)`:** the database-level duplicate guard.
- **Interview line:** *"Realtime is stored as parsed snapshot rows with no FKs to the static tables, because realtime can contain trips the timetable doesn't and static reloads truncate those tables."*

### Check question

> The poller already checks `header.timestamp` in Python before saving. Why also put a `UNIQUE` constraint on `feed_timestamp` in the database?

- **Hint:** what does the Python check remember after a restart? And what if two pollers run at once?
- **Answer:** the Python check only knows the last timestamp **in memory**. After a restart, it's empty, so the first poll would re-save a feed that's already stored. If two pollers run at once (say, local plus Docker by mistake), both pass their own check and both insert. The `UNIQUE` constraint is the **single source of truth**: `ON CONFLICT DO NOTHING` makes any duplicate harmless, however it happens. Check in the application for speed; enforce in the database for correctness.

### Build

Create `backend/sql/003_realtime.sql`:

```sql
-- 003_realtime.sql: GTFS-Realtime snapshots.
-- No FKs to static tables: realtime can contain trips the timetable doesn't,
-- and the static reload TRUNCATEs those tables.

CREATE TABLE IF NOT EXISTS rt_snapshots (
    id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    feed_timestamp         TIMESTAMPTZ NOT NULL UNIQUE,   -- dedup key (header.timestamp)
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    trip_update_count      INTEGER     NOT NULL,
    stop_time_update_count INTEGER     NOT NULL
);

CREATE TABLE IF NOT EXISTS rt_trip_updates (
    snapshot_id           BIGINT   NOT NULL REFERENCES rt_snapshots (id) ON DELETE CASCADE,
    entity_id             TEXT     NOT NULL,
    trip_id               TEXT,
    route_id              TEXT,
    start_date            DATE,
    start_time            TEXT,                -- 'HH:MM:SS', may exceed 24:00
    direction_id          SMALLINT,
    schedule_relationship TEXT     NOT NULL    -- SCHEDULED / CANCELED / ADDED ...
);

CREATE TABLE IF NOT EXISTS rt_stop_time_updates (
    snapshot_id           BIGINT  NOT NULL REFERENCES rt_snapshots (id) ON DELETE CASCADE,
    entity_id             TEXT    NOT NULL,
    trip_id               TEXT,
    stop_sequence         INTEGER,
    stop_id               TEXT,
    arrival_delay         INTEGER,             -- seconds, + late / - early
    arrival_time          TIMESTAMPTZ,
    departure_delay       INTEGER,
    departure_time        TIMESTAMPTZ,
    schedule_relationship TEXT    NOT NULL     -- SCHEDULED / SKIPPED / NO_DATA ...
);

-- "latest snapshot, this route" and "latest snapshot, this trip"
CREATE INDEX IF NOT EXISTS idx_rt_trip_updates_snapshot_route
    ON rt_trip_updates (snapshot_id, route_id);
CREATE INDEX IF NOT EXISTS idx_rt_stop_time_updates_snapshot_trip
    ON rt_stop_time_updates (snapshot_id, trip_id, stop_sequence);
```

### Run

From `backend\`:

```powershell
(Get-Item sql\003_realtime.sql).Length
Get-Content sql\003_realtime.sql | docker compose exec -T db psql -U transit -d transit -v ON_ERROR_STOP=1
docker compose exec db psql -U transit -d transit -P pager=off -c "\dt rt_*"
```

### Done when

- [ ] The file length is above 0 (an empty file has caught us before)
- [ ] `\dt rt_*` lists 3 tables
- [ ] Re-running the file prints only `already exists, skipping` notices

---

## Part 3 of 6: The poller loop

### Learn

- **One loop:** fetch → parse → (skip if the timestamp is unchanged) → store → sleep → repeat.
- **One transaction per snapshot:** the snapshot row plus all child rows commit together, or not at all.
- **`ON CONFLICT DO NOTHING RETURNING id`:** if no id comes back, the snapshot already exists, so skip it.
- **Rate limit:** keep at least 60 seconds between request **starts** (measured with `time.monotonic()`), so slow responses don't cause two calls to fire close together.
- **`--once` flag:** poll once and exit. It's the safe way to test.
- **`--save-raw DIR`:** also keep the raw `.pb` files. Milestone 6 freezes these as eval fixtures.
- **Interview line:** *"Each snapshot is written atomically: the parent row and its child rows are COPYed in one transaction, with the DB's UNIQUE constraint as the final dedupe guard."*

### Check question

> `store_snapshot` inserts the snapshot row and COPYs the child rows in **one** transaction. What would go wrong if the snapshot row committed first and the process crashed during the COPY?

- **Hint:** after the crash, what would the dedupe check say about that `feed_timestamp`?
- **Answer:** the snapshot row would exist with missing or partial child rows. On the next poll, the same feed timestamp hits the `UNIQUE` constraint, so it's skipped **forever**. That snapshot stays permanently incomplete, and nobody notices. With one transaction, a crash rolls back everything, so the next poll stores it cleanly.

### Build

1. **Replace** `backend/app/config.py` (read it first; this only adds the NTA and poller settings):

```python
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
    rt_retention_days: int = 8

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
```

2. Add these two lines to **both** `.env.example` and `.env` (repo root). Read each file first and append only:

```dotenv
RT_POLL_INTERVAL_SECONDS=60
RT_RETENTION_DAYS=8
```

3. Create `backend/app/rt_poller.py`:

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
from collections.abc import Iterable
from pathlib import Path

import httpx
import psycopg
from psycopg import sql

from app.config import Settings
from app.rt_parser import STOP_COLUMNS, TRIP_COLUMNS, ParsedFeed, parse_feed

log = logging.getLogger("rt_poller")

INSERT_SNAPSHOT_SQL = """
    INSERT INTO rt_snapshots (feed_timestamp, trip_update_count, stop_time_update_count)
    VALUES (%s, %s, %s)
    ON CONFLICT (feed_timestamp) DO NOTHING
    RETURNING id
"""

# ON DELETE CASCADE removes the child rows too
RETENTION_SQL = "DELETE FROM rt_snapshots WHERE feed_timestamp < now() - make_interval(days => %s)"

RETENTION_EVERY_SECONDS = 3600


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


def _copy(cur: psycopg.Cursor, table: str, columns: Iterable[str], snapshot_id: int, rows: list[tuple]) -> None:
    stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, ("snapshot_id", *columns))),
    )
    with cur.copy(stmt) as copy:
        for row in rows:
            copy.write_row((snapshot_id, *row))


def store_snapshot(conn: psycopg.Connection, parsed: ParsedFeed) -> int | None:
    """Store one snapshot atomically. Returns its id, or None if this feed_timestamp already exists."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(INSERT_SNAPSHOT_SQL, (parsed.feed_timestamp, len(parsed.trip_rows), len(parsed.stop_rows)))
        row = cur.fetchone()
        if row is None:
            return None  # the UNIQUE constraint caught a duplicate
        snapshot_id = row[0]
        _copy(cur, "rt_trip_updates", TRIP_COLUMNS, snapshot_id, parsed.trip_rows)
        _copy(cur, "rt_stop_time_updates", STOP_COLUMNS, snapshot_id, parsed.stop_rows)
        return snapshot_id


def apply_retention(conn: psycopg.Connection, days: int) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(RETENTION_SQL, (days,))
        return cur.rowcount


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

                if last_retention is None or time.monotonic() - last_retention > RETENTION_EVERY_SECONDS:
                    deleted = apply_retention(conn, settings.rt_retention_days)
                    last_retention = time.monotonic()
                    if deleted:
                        log.info("Retention: deleted %d snapshots older than %d days", deleted, settings.rt_retention_days)

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

### Run

From `backend\` (wait 60 seconds after any earlier API call):

```powershell
uv run python -m app.rt_poller --once
```

Wait 60 seconds, then run the **loop** for about 5 minutes and stop it with **Ctrl+C**:

```powershell
uv run python -m app.rt_poller --save-raw ..\data\rt_raw
```

### Done when

- [ ] `--once` logs `Stored snapshot 1: ts=... trips=... stop_updates=...`
- [ ] The loop logs a mix of `Stored snapshot` and `Feed unchanged` lines, about 60 seconds apart
- [ ] Ctrl+C logs `Shutdown requested` and then `Poller stopped`, with no traceback
- [ ] `data\rt_raw\` contains `.pb` files (gitignored, because `data/` is ignored)

---

## Part 4 of 6: Reliability

### Learn

- **Exponential backoff:** after each consecutive failure, wait twice as long (120 s, 240 s, 480 s, capped at 600 s). This avoids hammering a struggling API and respects the rate limit. It resets to 60 s on the first success.
- **Fatal vs retryable errors:**
  - **401/403** (bad key) is fatal, so stop. Retrying can't fix it.
  - **429/5xx, timeouts, DB down, a bad response** are retryable: log, back off, carry on.
- **Never crash on one bad response:** the loop catches the exception, logs the full traceback (`log.exception`) and continues. Trade-off: a code bug would also be retried, so the logs must be watched.
- **Reconnect:** after a failure, the DB connection is closed and reopened on the next attempt.
- **Graceful shutdown:** SIGINT/SIGTERM set a flag, and the loop finishes its current step and exits. `docker stop` sends SIGTERM.
- **Retention:** once an hour, `DELETE` snapshots older than `RT_RETENTION_DAYS` (8 days covers "this week"). `ON DELETE CASCADE` removes the child rows.
- **Interview line:** *"The poller separates fatal errors from retryable ones, backs off exponentially on failures, reconnects to the database automatically, shuts down cleanly on SIGTERM and enforces an 8-day retention window."*

### Check question

> The NTA returns HTTP 503 three times in a row. How long does the poller wait before the 4th try, and why not just retry immediately?

- **Hint:** look at `backoff_seconds(failures, base=60)`.
- **Answer:** `60 × 2³ = 480` seconds. Retrying immediately would hammer an API that's already struggling, burn the rate limit, and could get the key throttled. Backoff gives the upstream time to recover.

### Build

Create `backend/tests/test_rt_parser.py` (it tests the parser and the backoff, with no network or DB):

```python
from datetime import date, datetime, timezone

import pytest
from google.transit import gtfs_realtime_pb2

from app.rt_parser import parse_feed
from app.rt_poller import backoff_seconds

FEED_TS = 1793100000


def make_feed(with_timestamp: bool = True) -> bytes:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    if with_timestamp:
        feed.header.timestamp = FEED_TS

    # 1) a normal trip, 4 minutes late at stop_sequence 51
    e1 = feed.entity.add()
    e1.id = "e1"
    tu = e1.trip_update
    tu.trip.trip_id = "5828_1038"
    tu.trip.route_id = "1 39A c a"
    tu.trip.start_date = "20261027"
    tu.trip.start_time = "23:30:00"
    tu.trip.direction_id = 0
    stu = tu.stop_time_update.add()
    stu.stop_sequence = 51
    stu.stop_id = "8240DB003663"
    stu.arrival.delay = 240
    stu.departure.delay = 240

    # 2) a cancelled trip: no stop updates at all
    e2 = feed.entity.add()
    e2.id = "e2"
    e2.trip_update.trip.trip_id = "T2"
    e2.trip_update.trip.schedule_relationship = gtfs_realtime_pb2.TripDescriptor.CANCELED

    # 3) a vehicle position: must be ignored
    e3 = feed.entity.add()
    e3.id = "e3"
    e3.vehicle.trip.trip_id = "T3"

    return feed.SerializeToString()


def test_header_timestamp_becomes_utc_datetime():
    assert parse_feed(make_feed()).feed_timestamp == datetime.fromtimestamp(FEED_TS, tz=timezone.utc)


def test_only_trip_updates_are_kept():
    assert [r[0] for r in parse_feed(make_feed()).trip_rows] == ["e1", "e2"]


def test_trip_row_fields():
    row = parse_feed(make_feed()).trip_rows[0]
    assert row == ("e1", "5828_1038", "1 39A c a", date(2026, 10, 27), "23:30:00", 0, "SCHEDULED")


def test_stop_row_delay_and_missing_fields_are_none():
    rows = parse_feed(make_feed()).stop_rows
    assert rows == [("e1", "5828_1038", 51, "8240DB003663", 240, None, 240, None, "SCHEDULED")]


def test_cancelled_trip_has_no_stop_rows():
    parsed = parse_feed(make_feed())
    assert parsed.trip_rows[1][-1] == "CANCELED"
    assert all(r[0] != "e2" for r in parsed.stop_rows)


def test_missing_header_timestamp_is_rejected():
    with pytest.raises(ValueError):
        parse_feed(make_feed(with_timestamp=False))


@pytest.mark.parametrize(("failures", "expected"), [(1, 120), (2, 240), (3, 480), (4, 600), (10, 600)])
def test_backoff_doubles_and_caps(failures, expected):
    assert backoff_seconds(failures, base=60) == expected
```

### Run

1. **Tests** (from `backend\`):

```powershell
uv run pytest -v
```

2. **Drill: database down.** Start the loop in terminal 1:

```powershell
uv run python -m app.rt_poller
```

In terminal 2, stop the database, wait about 3 minutes, then start it again:

```powershell
docker compose stop db
docker compose start db
```

Watch terminal 1: `Poll failed ... retrying in 120s`, then a recovery to `Stored snapshot`. Then stop the loop with Ctrl+C.

3. **Drill: bad key.** This uses an environment-variable override, so `.env` is untouched. Wait 60 seconds first.

```powershell
$env:NTA_API_KEY = "wrong-key"
uv run python -m app.rt_poller --once
Remove-Item Env:NTA_API_KEY
```

**Expected:** `FatalConfigError: NTA API rejected the key (HTTP 401)`, or 403, with no retry loop. The last line only clears the temporary override in this terminal.

### Done when

- [ ] 17 tests pass (10 existing + 7 new)
- [ ] The DB-down drill shows backoff and then recovery, with no crash
- [ ] The bad-key drill stops immediately with a fatal error

---

## Part 5 of 6: Running it in Docker Compose

### Learn

- **Why:** the poller must run all the time, restart after crashes and start after the DB is healthy. Compose does all three: `restart: unless-stopped` and `depends_on: condition: service_healthy`.
- **The Dockerfile layer order:** copy `pyproject.toml` and `uv.lock` first, run `uv sync`, *then* copy the code. Code changes then reuse the cached dependency layer, so rebuilds are fast.
- **`--frozen`:** install exactly what's in `uv.lock`, and fail if it's out of date. That's the reproducibility lesson from Milestone 2.
- **Container networking:** inside Compose, the poller reaches Postgres at host **`db`**, port **5432** (the service name and the container port). `localhost:5433` only exists from Windows.
- **Interview line:** *"The poller runs as a Compose service built from the locked dependencies, waits for the DB healthcheck, restarts automatically and reaches Postgres by service name on the internal network."*

### Check question

> On Windows, the loader connects to `localhost:5433`. Inside Compose, the poller uses `db:5432`. Why the difference?

- **Hint:** which side of the `"5433:5432"` port mapping is the container's own port?
- **Answer:** inside a container, `localhost` means *the container itself*, not Windows. Compose gives every service a DNS name on a private network, so `db` resolves to the Postgres container. On that network, you connect to Postgres's real port, **5432**. The `5433` is only the **host-side** mapping that Windows uses to reach in.

### Build

1. Create `backend/Dockerfile`. Match the uv tag to `uv --version`; check that output first.

```dockerfile
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependencies first: this layer stays cached until pyproject.toml / uv.lock change
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "app.rt_poller"]
```

2. Create `backend/.dockerignore`:

```
.venv
__pycache__
.pytest_cache
tests
sql
```

3. Add this service to `docker-compose.yml` under `services:`, after `db`. Read the file first and keep `db` and `volumes` unchanged.

```yaml
  rt-poller:
    build: ./backend
    environment:
      POSTGRES_HOST: db
      POSTGRES_PORT: 5432
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
      NTA_API_KEY: ${NTA_API_KEY}
      RT_POLL_INTERVAL_SECONDS: ${RT_POLL_INTERVAL_SECONDS:-60}
      RT_RETENTION_DAYS: ${RT_RETENTION_DAYS:-8}
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped
```

### Run

**Make sure no local poller is running** (only one poller at a time). From the repo root:

```powershell
docker compose config --quiet
docker compose up -d --build rt-poller
docker compose ps
docker compose logs -f rt-poller
```

Watch for 2 to 3 minutes, then press **Ctrl+C**. That only stops following the logs; the poller keeps running.

### Done when

- [ ] `config --quiet` prints nothing (the YAML is valid)
- [ ] `ps` shows `rt-poller` as `Up` and `db` as `healthy`
- [ ] The logs show `Stored snapshot` lines about 60 seconds apart
- [ ] `docker compose stop rt-poller` logs `Shutdown requested` / `Poller stopped` (graceful SIGTERM). Start it again with `docker compose start rt-poller`.

---

## Part 6 of 6: Verify

Let the Docker poller run for **at least 30 minutes** first.

### Learn

- Unit tests prove the **code**. This part proves the **data**: snapshots are arriving, realtime **joins** to the static timetable, and storage growth is known.
- **Join health is the key metric:** if a low % of realtime `trip_id`s exist in `trips`, the static feed is outdated or the wrong version (the v1/v2 mismatch warning from Milestone 1).
- **Interview line:** *"I monitor join health between realtime and static trip IDs. A drop signals a timetable version mismatch before users see wrong answers."*

### Check question

> Query 2 shows `match_pct = 60`. What's the most likely cause, and what do you do?

- **Hint:** realtime always describes *today's* service. What might be stale?
- **Answer:** the static timetable is out of date: the NTA published a new feed with new trip IDs, and the loaded one is old (or the wrong version). Download the latest `GTFS_Realtime.zip` and re-run the loader. The `feed_version` check reloads it because the version changed.

### Run

Create `backend/sql/verify_realtime.sql`:

```sql
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
```

Run it from `backend\`:

```powershell
Get-Content sql\verify_realtime.sql | docker compose exec -T db psql -U transit -d transit -P pager=off
```

**Storage estimate:** (total `rt_%` size) ÷ (snapshots) × 1,440 = growth per day. Multiply by 8 for the retention window. Record the result.

### Done when

- [ ] Query 1: snapshot count roughly equals the minutes the poller ran (minus "unchanged" skips)
- [ ] Query 2: `match_pct` is high (expect 90% or more). If it's low, stop and investigate before calling Milestone 3 done.
- [ ] Query 3: real 39A trips with stop names and delays, *or* zero rows if no 39A is running at that moment (then try `'15'` or `'E1'`)
- [ ] Query 5: the daily growth and 8-day total are calculated and noted

---

## Milestone 3 wrap-up

Fill in the numbers **measured** in Parts 1 and 6. Never invent them.

**CV bullet (template):**

> Built a fault-tolerant GTFS-Realtime ingestion service (Python, protobuf, PostgreSQL, Docker Compose) that polls Ireland's NTA live bus feed every 60 s, with timestamp-based deduplication, atomic snapshot writes, exponential backoff and 8-day retention; about **[X]** stop-time updates per snapshot, with **[Y]%** of realtime trips joining to the static timetable.

**Interview line:**

> *"The NTA feed is rate-limited, so a single poller writes protobuf snapshots to Postgres every minute and every reader queries the database. Snapshots are atomic and deduped by the feed timestamp at the DB level, failures back off exponentially, and I track the realtime-to-static join rate to catch timetable version drift."*

**Status at the end of Milestone 3:**

- **Done:** Parts 1 to 6 (only after every Done-when box is ticked)
- **Next:** Milestone 4, FastAPI tools (`/departures`, `/delays`, `/on-time-performance`)
- **Later:** see below

## Later (not part of Milestone 3)

- Respect the `Retry-After` header on HTTP 429
- Partition the `rt_` tables by day (drop old partitions instead of `DELETE`) if growth gets large
- Ingest Vehicles and Alerts as well as TripUpdates
- Choose a frozen set of `data\rt_raw\*.pb` files for the Milestone 6 eval
- Health metrics for the poller (last successful poll age), surfaced in Milestone 8
