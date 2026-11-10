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
