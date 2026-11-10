# Milestones 3 to 5: finish, build, deploy (Claude Code playbook)

Dublin Transit AI. **Build mode:** Claude Code builds and verifies. Sandy learns afterwards from the "Learn later" section at the end.

## How to use

1. Save as `docs/milestones/M4_M5_tools_agent_deploy.md`, next to `M3_realtime_ingestion.md`.
2. In Claude Code (repo root):

   > Read `docs/milestones/M4_M5_tools_agent_deploy.md` and follow it in build mode. First finish Milestone 3 from `M3_realtime_ingestion.md` (just build), then Milestone 4, Milestone 5, then Deploy. Stop and report at each "STOP" line.

## Rules for Claude Code

- **Build mode:** skip the teaching questions. Explain each step in one line while you work.
- **Inspect before editing.** Read every file before changing it. Never guess contents. Run `git status` at the start.
- **Never** run `git commit`, `push`, `reset`, `restore`, `stash`, or delete files, unless Sandy explicitly asks.
- **PowerShell.** Run `uv run ...` from `backend\`. Run `docker compose ...` from the repo root.
- **Never print, echo or log secrets:** the NTA key, the Anthropic key, the DB password.
- **Only one poller at a time**, and wait 60 seconds between manual NTA calls.
- **Ports:** a local uvicorn on 8000/8001 conflicts with the Docker `api`/`agent` services. Stop local servers (Ctrl+C) before `docker compose up`.
- **Honest status:** a step is done only when its **Done when** checks pass. If a check fails, stop, show the error and propose a fix. Never mark it done.
- Ignore the harmless `VIRTUAL_ENV ... does not match` warning from the old root `.venv`.

---

## Step 0: Finish Milestone 3

Follow `docs/milestones/M3_realtime_ingestion.md` in **just build** mode through Part 6. Before moving on:

- [ ] The Part 6 verify output exists: snapshot count, join %, 39A query, cancelled count, storage sizes
- [ ] `match_pct` is at least 90%. If it's lower, **STOP** and report.
- [ ] Record: the average stop updates per snapshot, and the storage growth per day and for 8 days

**STOP:** show Sandy the Milestone 3 numbers and the filled-in CV bullet, then continue.

---

## Milestone 4: FastAPI tool endpoints

**Goal:** an HTTP API that turns database rows into answers: stop search, departures (timetable + realtime), route delays, on-time performance. The agent will call these.

### 4.1 Dependencies (from `backend\`)

```powershell
uv add fastapi "uvicorn[standard]" psycopg-pool tzdata
```

`tzdata` is required on Windows, where Python has no built-in time-zone database. `Europe/Dublin` needs it.

### 4.2 Config: replace `backend/app/config.py`

Read the current file first. This keeps every existing setting and adds the API and agent settings.

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

    # API (Milestone 4)
    timezone: str = "Europe/Dublin"
    realtime_stale_seconds: int = 300  # older realtime than this is ignored, not applied

    # Agent (Milestone 5)
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-5-5"
    api_base_url: str = "http://localhost:8000"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
```

### 4.3 Indexes: create `backend/sql/004_api_indexes.sql`

```sql
-- 004_api_indexes.sql: indexes for the Milestone 4 API queries

-- on-time performance joins stop updates to their trip update by (snapshot_id, entity_id)
CREATE INDEX IF NOT EXISTS idx_rt_trip_updates_snapshot_entity
    ON rt_trip_updates (snapshot_id, entity_id);

-- stop search by the number printed on the bus-stop pole
CREATE INDEX IF NOT EXISTS idx_stops_stop_code ON stops (stop_code);

-- "which routes serve this stop?" and route lookups by public name
CREATE INDEX IF NOT EXISTS idx_trips_route ON trips (route_id);
CREATE INDEX IF NOT EXISTS idx_routes_short_name ON routes (route_short_name);
```

Apply it (from `backend\`):

```powershell
(Get-Item sql\004_api_indexes.sql).Length
Get-Content sql\004_api_indexes.sql | docker compose exec -T db psql -U transit -d transit -v ON_ERROR_STOP=1
```

### 4.4 Queries: create `backend/app/transit_db.py`

```python
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
  AND st.departure_secs >= %(now_s)s + svc.offset_s
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
            "limit": limit,
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
        out.append({
            "route": r["route_short_name"],
            "headsign": r["trip_headsign"],
            "trip_id": r["trip_id"],
            "scheduled_time": scheduled.isoformat(),
            "delay_seconds": delay,
            "expected_time": expected.isoformat() if expected else None,
            "status": status,
        })

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
```

### 4.5 The API: create `backend/app/api.py`

```python
"""Milestone 4: FastAPI tool endpoints. The agent (Milestone 5) calls these over HTTP.

Run from backend\\:  uv run uvicorn app.api:app --port 8000 --reload
Docs:                http://localhost:8000/docs
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import transit_db as db
from app.config import Settings

settings = Settings()
TZ = ZoneInfo(settings.timezone)
pool = ConnectionPool(
    settings.database_url,
    min_size=1,
    max_size=5,
    open=False,
    kwargs={"autocommit": True, "row_factory": dict_row},
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=True, timeout=10)
    yield
    pool.close()


app = FastAPI(title="Dublin Transit AI tools", version="0.1.0", lifespan=lifespan)


def get_conn():
    with pool.connection() as conn:
        yield conn


Conn = Annotated[object, Depends(get_conn)]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def require_route(conn, route: str) -> None:
    if not db.route_exists(conn, route):
        raise HTTPException(status_code=404, detail=f"Route '{route}' is not in the current timetable")


@app.get("/health")
def health(conn: Conn) -> dict:
    snap = db.latest_snapshot(conn)
    feed = conn.execute("SELECT feed_version, loaded_at FROM feed_info").fetchone()
    return {
        "status": "ok",
        "static_feed_version": feed["feed_version"] if feed else None,
        "latest_realtime": snap["feed_timestamp"].isoformat() if snap else None,
        "realtime_age_seconds": int((now_utc() - snap["feed_timestamp"]).total_seconds()) if snap else None,
    }


@app.get("/routes/{short_name}")
def get_route(conn: Conn, short_name: str) -> dict:
    rows = db.get_route(conn, short_name)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Route '{short_name}' is not in the current timetable")
    return {"route": short_name, "variants": rows}


@app.get("/stops/search")
def search_stops(
    conn: Conn,
    q: Annotated[str, Query(min_length=2, description="Stop number on the pole, or part of the stop name")],
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> dict:
    return {"query": q, "stops": db.search_stops(conn, q, limit)}


@app.get("/departures")
def departures(
    conn: Conn,
    stop_id: Annotated[str, Query(description="Full stop_id, e.g. 8250DB000767 (use /stops/search first)")],
    route: Annotated[str | None, Query(description="Route short name, e.g. 39A")] = None,
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
    window_minutes: Annotated[int, Query(ge=5, le=360)] = 120,
) -> dict:
    if conn.execute("SELECT 1 FROM stops WHERE stop_id = %s", (stop_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail=f"Stop '{stop_id}' not found; use /stops/search")
    if route:
        require_route(conn, route)
    return db.departures(
        conn, stop_id, now_utc(), TZ, route=route, limit=limit,
        window_minutes=window_minutes, stale_seconds=settings.realtime_stale_seconds,
    )


@app.get("/delays")
def delays(conn: Conn, route: Annotated[str, Query(description="Route short name, e.g. 39A")]) -> dict:
    require_route(conn, route)
    return db.route_delays(conn, route, now_utc(), stale_seconds=settings.realtime_stale_seconds)


@app.get("/on-time-performance")
def on_time_performance(
    conn: Conn,
    route: Annotated[str, Query(description="Route short name, e.g. 15")],
    days: Annotated[int, Query(ge=1, le=8)] = 7,
) -> dict:
    require_route(conn, route)
    return db.on_time_performance(conn, route, now_utc(), days=days)
```

### 4.6 Tests: create `backend/tests/test_transit_helpers.py`

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.transit_db import seconds_since_midnight, service_datetime

DUBLIN = ZoneInfo("Europe/Dublin")


def test_seconds_since_midnight():
    assert seconds_since_midnight(datetime(2026, 11, 1, 8, 30, 15, tzinfo=DUBLIN)) == 30615


def test_service_datetime_normal_time():
    assert service_datetime(date(2026, 11, 1), 8 * 3600, DUBLIN) == datetime(2026, 11, 1, 8, 0, tzinfo=DUBLIN)


def test_service_datetime_after_midnight_rolls_to_next_day():
    # GTFS 24:01:00 on Saturday's service = 00:01 on Sunday
    assert service_datetime(date(2026, 10, 31), 86460, DUBLIN) == datetime(2026, 11, 1, 0, 1, tzinfo=DUBLIN)
```

### 4.7 Run and check

The poller (Docker) should be running so realtime is fresh. In terminal 1 (`backend\`):

```powershell
uv run pytest -v
uv run uvicorn app.api:app --port 8000 --reload
```

In terminal 2:

```powershell
Invoke-RestMethod "http://localhost:8000/health" | ConvertTo-Json
Invoke-RestMethod "http://localhost:8000/stops/search?q=767" | ConvertTo-Json -Depth 5
Invoke-RestMethod "http://localhost:8000/departures?stop_id=8250DB000767&route=39A&limit=3" | ConvertTo-Json -Depth 5
Invoke-RestMethod "http://localhost:8000/delays?route=39A" | ConvertTo-Json -Depth 3
Invoke-RestMethod "http://localhost:8000/on-time-performance?route=15" | ConvertTo-Json
try { Invoke-RestMethod "http://localhost:8000/routes/46A" } catch { "46A -> HTTP " + $_.Exception.Response.StatusCode.value__ }
```

Also open http://localhost:8000/docs in a browser: FastAPI's auto-generated Swagger UI.

### Done when

- [ ] All tests pass (the previous 21 plus 3 new = 24)
- [ ] `/health` shows the static feed version and a `realtime_age_seconds` under 300
- [ ] `/stops/search?q=767` returns UCD with routes including 39A
- [ ] `/departures` returns times in Dublin local time, with some `status: realtime` while the poller runs
- [ ] `/routes/46A` returns HTTP 404 (it must never invent the 46A)
- [ ] `/on-time-performance?route=15` returns a percentage with `observations`, `data_from` and `data_to`
- [ ] Stop the local uvicorn (Ctrl+C)

**STOP:** show Sandy the `/departures` and `/routes/46A` outputs, then continue.

---

## Milestone 5: LangGraph agent

**Goal:** an agent that reads a question, calls the right Milestone 4 tools over HTTP and answers only from their results.

### 5.1 The LLM key (Sandy does this)

1. Sandy creates an API key at console.anthropic.com. API usage is billed separately from a Claude.ai subscription.
2. Sandy adds two lines to `.env` himself. Claude Code must not see or print the key.

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-sonnet-5-5
```

3. Claude Code adds placeholders to `.env.example`: `ANTHROPIC_API_KEY=your-anthropic-key-here` and `LLM_MODEL=claude-sonnet-5-5`.

Check without printing the key:

```powershell
(Select-String -Path .env -Pattern '^ANTHROPIC_API_KEY=.+').Count
```

It must print `1`. If not, **STOP** and ask Sandy.

### 5.2 Dependencies (from `backend\`)

```powershell
uv add langgraph langchain-anthropic
```

### 5.3 The agent: create `backend/app/agent.py`

```python
"""Milestone 5: LangGraph agent that answers Dublin transit questions using ONLY the API tools.

Run from backend\\ (the API must be running on API_BASE_URL):
    uv run python -m app.agent "Is the 39A running late right now?"
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.config import Settings

settings = Settings()

SYSTEM_PROMPT = """You are Dublin Transit AI, an assistant for Dublin public transport questions.

Rules:
- Answer ONLY from tool results. Never invent routes, stops, times, delays or statistics.
- If a tool says a route or stop is not found, say so plainly. Do not guess a replacement unless a tool result shows it.
- To answer about "my stop" or a named place, call search_stops first, then use the exact stop_id it returns.
  If several stops match and it matters, list them briefly and ask which one.
- Give times in Dublin local time as HH:MM. Say whether a time is realtime or timetable only.
- If realtime is "stale" or "none", say the answer is based on the timetable only.
- For reliability, quote the on-time percentage, the number of observations and the date range of the data.
  If observations are few, say the estimate is weak.
- Keep answers short: 1 to 4 sentences, or a short list for several departures.

Current Dublin time: {now}"""


# ---------- HTTP helper (patched in tests) ----------

def _get(path: str, params: dict | None = None) -> str:
    """Call the tools API and return JSON text. Errors come back as text the model can read."""
    try:
        resp = httpx.get(f"{settings.api_base_url}{path}", params=params, timeout=20.0)
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"transit API unreachable: {type(exc).__name__}"})
    if resp.status_code == 404:
        return json.dumps({"error": resp.json().get("detail", "not found")})
    if resp.status_code >= 400:
        return json.dumps({"error": f"transit API returned HTTP {resp.status_code}"})
    return resp.text


# ---------- tools: thin wrappers over the API ----------

@tool
def search_stops(query: str) -> str:
    """Find bus/Luas/rail stops by the stop number on the pole (e.g. "767") or part of the name
    (e.g. "UCD"). Returns stop_id, stop_code, stop_name and the routes serving each stop."""
    return _get("/stops/search", {"q": query, "limit": 8})


@tool
def get_departures(stop_id: str, route: str | None = None, limit: int = 5) -> str:
    """Next departures from a stop. stop_id must be the full id from search_stops (e.g. 8250DB000767).
    Optionally filter by route short name (e.g. "39A"). Includes realtime delays when available."""
    params = {"stop_id": stop_id, "limit": limit}
    if route:
        params["route"] = route
    return _get("/departures", params)


@tool
def get_route_delays(route: str) -> str:
    """Current delays for every trip on a route right now (e.g. "39A"), from the latest realtime snapshot."""
    return _get("/delays", {"route": route})


@tool
def get_on_time_performance(route: str, days: int = 7) -> str:
    """How reliable a route has been over the last N days (1 to 8): on-time %, late/early counts,
    observation count and the date range of the data."""
    return _get("/on-time-performance", {"route": route, "days": days})


@tool
def get_route_info(route: str) -> str:
    """Check whether a route exists in the current timetable and get its full name (e.g. "46A")."""
    return _get(f"/routes/{route}")


TOOLS = [search_stops, get_departures, get_route_delays, get_on_time_performance, get_route_info]


# ---------- graph ----------

def build_graph(model):
    """agent -> (tool calls?) -> tools -> agent ... -> final answer."""
    llm = model.bind_tools(TOOLS)
    tz = ZoneInfo(settings.timezone)

    def call_model(state: MessagesState) -> dict:
        system = SystemMessage(SYSTEM_PROMPT.format(now=datetime.now(tz).strftime("%A %d %B %Y, %H:%M")))
        return {"messages": [llm.invoke([system, *state["messages"]])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)  # tool calls -> "tools", else -> END
    graph.add_edge("tools", "agent")
    return graph.compile()


@lru_cache(maxsize=1)
def default_graph():
    from langchain_anthropic import ChatAnthropic

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
    model = ChatAnthropic(
        model=settings.llm_model,
        api_key=settings.anthropic_api_key,
        temperature=0,
        max_tokens=1024,
    )
    return build_graph(model)


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


def answer(question: str, history: list[BaseMessage] | None = None, graph=None) -> dict:
    """Run the agent once. Returns the answer plus which tools were called (Milestone 6 eval uses this)."""
    graph = graph or default_graph()
    result = graph.invoke(
        {"messages": [*(history or []), HumanMessage(question)]},
        config={"recursion_limit": 12},  # hard stop on tool loops
    )
    messages = result["messages"]
    tool_calls = [
        {"name": call["name"], "args": call["args"]}
        for m in messages if isinstance(m, AIMessage) for call in m.tool_calls
    ]
    tool_errors = [m.content for m in messages if isinstance(m, ToolMessage) and '"error"' in str(m.content)]
    return {"answer": _text(messages[-1]), "tool_calls": tool_calls, "tool_errors": tool_errors}


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or "Is the 39A running late right now?"
    out = answer(question)
    print(out["answer"])
    print("\n[tools]", ", ".join(c["name"] for c in out["tool_calls"]) or "none")
```

### 5.4 The chat endpoint: create `backend/app/agent_api.py`

```python
"""Milestone 5: HTTP endpoint for the agent (the chat UI in Milestone 7 calls this).

Run from backend\\:  uv run uvicorn app.agent_api:app --port 8001
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from app.agent import answer

app = FastAPI(title="Dublin Transit AI agent", version="0.1.0")


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
def chat(req: ChatRequest) -> ChatResponse:
    history = [HumanMessage(t.content) if t.role == "user" else AIMessage(t.content) for t in req.history]
    try:
        out = answer(req.message, history)
    except RuntimeError as exc:  # e.g. missing ANTHROPIC_API_KEY
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ChatResponse(answer=out["answer"], tool_calls=out["tool_calls"])
```

### 5.5 Test: create `backend/tests/test_agent_graph.py`

This tests the agent loop with no LLM and no API.

```python
"""Tests the agent LOOP without a real LLM or API: a scripted fake model + a patched HTTP helper."""

import json

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

import app.agent as agent


class ScriptedModel(GenericFakeChatModel):
    """Replays fixed AIMessages; bind_tools is a no-op so it works in the graph."""

    def bind_tools(self, tools, **kwargs):
        return self


def test_agent_calls_tool_then_answers(monkeypatch):
    calls = []

    def fake_get(path, params=None):
        calls.append(path)
        return json.dumps({"error": "Route '46A' is not in the current timetable"})

    monkeypatch.setattr(agent, "_get", fake_get)
    model = ScriptedModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "get_route_info", "args": {"route": "46A"}, "id": "c1"}]),
        AIMessage(content="The 46A is not in the current timetable."),
    ]))

    out = agent.answer("When is the next 46A?", graph=agent.build_graph(model))

    assert calls == ["/routes/46A"]
    assert [c["name"] for c in out["tool_calls"]] == ["get_route_info"]
    assert "not in the current timetable" in out["answer"]
    assert out["tool_errors"]  # the 404 reached the model as data, not an exception
```

### 5.6 Run and check (real LLM)

Terminal 1 (`backend\`):

```powershell
uv run pytest -v
uv run uvicorn app.api:app --port 8000
```

Terminal 2 (`backend\`). Run these 5 smoke questions one by one:

```powershell
uv run python -m app.agent "When is the next 39A from stop 767?"
uv run python -m app.agent "Is the 39A running late right now?"
uv run python -m app.agent "How reliable was route 15 this week?"
uv run python -m app.agent "When is the next 46A?"
uv run python -m app.agent "Next bus from UCD?"
```

### Done when

- [ ] All tests pass (24 + 1 = 25)
- [ ] Q1 calls `search_stops` then `get_departures`, and gives HH:MM times labelled realtime or timetable
- [ ] Q2 calls `get_route_delays` and quotes real delays
- [ ] Q3 gives the on-time %, observation count and date range, and says the data is limited if it only covers a few days
- [ ] Q4 says the 46A isn't in the current timetable, with **NO** invented times. This is the most important check.
- [ ] Q5 either lists the matching UCD stops and asks which one, or answers for a clearly identified stop
- [ ] Record each answer and its `[tools]` line for Sandy. They become the first Milestone 6 eval cases.
- [ ] Stop the local uvicorn (Ctrl+C)

**STOP:** show Sandy the 5 answers, then continue.

---

## Deploy: run the full stack in Docker Compose

"Deploy" here means running everything locally in Docker Compose. Cloud hosting is later (Milestone 8).

### D.1 Add two services to `docker-compose.yml`

Read the file first. Keep `db`, `rt-poller` and `volumes` unchanged, and add these under `services:`:

```yaml
  api:
    build: ./backend
    command: ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
    environment:
      POSTGRES_HOST: db
      POSTGRES_PORT: 5432
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    ports:
      - "8000:8000"
    depends_on:
      db:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"]
      interval: 10s
      timeout: 5s
      retries: 6
    restart: unless-stopped

  agent:
    build: ./backend
    command: ["uvicorn", "app.agent_api:app", "--host", "0.0.0.0", "--port", "8001"]
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
      API_BASE_URL: http://api:8000
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      LLM_MODEL: ${LLM_MODEL:-claude-sonnet-5-5}
    ports:
      - "8001:8001"
    depends_on:
      api:
        condition: service_healthy
    restart: unless-stopped
```

The `rt-poller` image already copies all of `app/`, so the same Dockerfile serves all three Python services; only `command` differs.

### D.2 Start everything (repo root; no local uvicorn running)

```powershell
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

### D.3 Check the deployed stack

```powershell
Invoke-RestMethod "http://localhost:8000/health" | ConvertTo-Json
$body = @{ message = "Is the 39A running late right now?" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/chat" -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 5
$body = @{ message = "When is the next 46A?" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/chat" -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 5
docker compose logs --tail 20 rt-poller
```

### Done when

- [ ] `ps` shows 4 services: `db` (healthy), `rt-poller` (Up), `api` (healthy), `agent` (Up)
- [ ] `/health` on 8000 shows fresh realtime
- [ ] `/chat` on 8001 answers the 39A question with real data and refuses to invent the 46A
- [ ] The poller logs are still storing snapshots

**STOP:** report the final status to Sandy:

- **Done:** Milestones 3, 4, 5 and the local deploy, listing only what passed
- **Next:** Milestone 6 (100-question eval + GitHub Actions)
- **Later:** the list below
- A reminder that **nothing is committed**: Sandy decides when to commit.

To pause it all later: `docker compose stop`. Data is kept; `docker compose start` resumes.

---

## Later (not in this playbook)

- Storage: with about 11,000 stop rows per snapshot, consider a shorter retention, keeping fewer snapshots for history, or daily partitions
- On-time performance at scale: precompute a daily summary table (or a materialised view) instead of scanning raw snapshots
- DST edge: GTFS times are relative to "noon minus 12 h". The current code uses midnight, which is off by 1 hour only on the two clock-change days
- Realtime `start_date` matching for after-midnight trips that share a `trip_id` with the next day's trip
- `/departures`: sort by expected time (scheduled + delay), not scheduled time. Seen in the M5 Q5 smoke test: a 39A running 3 min early (expected 21:07) was listed after a 126U at 21:10
- Luas and rail realtime (separate APIs), as a stretch milestone
- Cloud deployment, auth and rate limiting on `/chat` (Milestone 8)

---

## Learn later (for Sandy)

Read one section at a time. Overview, then interview questions, then STAR stories. Fill in your measured numbers; never invent them.

### Milestone 3: Realtime ingestion

**Overview (the one story):** every minute, a single poller takes a "photo" of the city's buses from the NTA's rate-limited feed. It parses the protobuf into rows, skips the photo if its `header.timestamp` was already saved, and stores it atomically in Postgres. Failures back off exponentially; snapshots older than 8 days are deleted. Every user reads from the database, never from the NTA.

**Interview questions:**

- *"Why one poller instead of calling the API per user request?"* The API allows about 1 call per minute per key. Decoupling ingestion from serving means 1 user or 10,000 users still make one upstream call per minute, at the cost of data up to about 60 s old.
- *"How do you avoid duplicate snapshots?"* An in-memory timestamp check for speed, and a `UNIQUE` constraint with `ON CONFLICT DO NOTHING` for correctness across restarts and multiple pollers.
- *"Why one transaction per snapshot?"* If the parent row committed without its children, the dedupe would skip that timestamp forever, leaving a permanently incomplete snapshot.
- *"Protobuf or JSON?"* Protobuf: about 5× smaller per snapshot on real NTA data, plus typed parsing. JSON only for debugging.
- *"Why store a missing delay as NULL?"* Protobuf returns 0 for unset numbers. Treating unknown as 0 means "on time" and inflates reliability. `HasField` detects presence.

**STAR: storage growth**

- **S:** After building the realtime poller, I measured a live NTA snapshot: about 1,986 trips and 11,411 stop updates.
- **T:** Decide how long to keep data without filling the disk.
- **A:** Projected rows per day (up to 1,440 snapshots), measured actual table growth over 30 minutes, and set an 8-day retention with `ON DELETE CASCADE` so cleanup is one `DELETE`.
- **R:** [fill in: measured MB per day, chosen retention, total size].

### Milestone 4: FastAPI tools

**Overview:** the API turns raw tables into answers. `/departures` combines today's timetable (calendar logic, including yesterday's after-midnight trips) with the latest realtime delay, propagated from the nearest earlier stop. It ignores realtime older than 5 minutes and labels every time as `realtime`, `scheduled_only` or `cancelled`. `/on-time-performance` uses the last reported delay per trip and stop, with a clear definition (1 min early to 5 min late).

**Interview questions:**

- *"How do you combine timetable and realtime?"* Query scheduled departures from the static tables, then look up the latest snapshot's delay for those trips at or before this stop, and add it.
- *"What if realtime is stale?"* Older than 5 minutes means it isn't applied. The response says `stale`, and the agent tells the user it's timetable only.
- *"How do you define on-time?"* Between 1 minute early and 5 minutes late, using the final prediction per trip and stop, with the observation count and date range returned so nobody over-trusts thin data.
- *"Why a 404 for an unknown route?"* So the agent gets a clear fact ("not in the timetable") instead of an empty list it might fill with guesses.

**STAR: the 46A**

- **S:** My project's own example question asked about the 46A.
- **T:** Verify the loaded timetable was correct.
- **A:** Ran SQL verification after loading and found the 46A isn't in the current NTA feed (BusConnects replaced it). Made the API return 404 for unknown routes, and made it an eval case.
- **R:** The agent says "not in the current timetable" instead of inventing times. [fill in: eval result in Milestone 6].

### Milestone 5: LangGraph agent

**Overview:** a small state graph: the agent node asks the LLM; if it requests tools, the tools node runs them (HTTP calls to Milestone 4); the results go back to the LLM; this repeats until it answers, with a recursion limit as a safety stop. The system prompt forces grounding: only tool data, labelled realtime vs timetable, honest "not found".

**Interview questions:**

- *"Why LangGraph instead of a single prompt?"* The model must fetch live data in several steps (find the stop, then the departures). The graph makes the loop explicit, bounded and testable.
- *"How do you stop hallucination?"* Tools return explicit errors (404 → "not in the timetable"), the prompt forbids inventing data, and Milestone 6 evaluates exactly this with known-answer questions.
- *"How did you test an LLM agent without calling the LLM?"* A scripted fake chat model plus a patched HTTP helper, so the graph loop is tested deterministically and for free in CI.
- *"Why do tools call the API over HTTP instead of the DB directly?"* Separation: the API is the single source of truth and is reusable by the UI. The agent stays thin and replaceable.

**STAR: tool errors as data**

- **S:** The agent's tools call an HTTP API that can return 404 (an unknown route) or be unreachable.
- **T:** Make sure a failed tool call never crashes the conversation or pushes the model to guess.
- **A:** Wrapped every tool call so 404s and network errors come back as JSON text the LLM can read and explain. Recorded them as `tool_errors` for evaluation.
- **R:** The agent answers "route not found" or "data temporarily unavailable" instead of crashing or guessing. [fill in: the observed behaviour from the 46A smoke test]
