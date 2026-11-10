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
    """Real time in production; FIXED_NOW during evals and integration tests."""
    return settings.fixed_now or datetime.now(timezone.utc)


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
