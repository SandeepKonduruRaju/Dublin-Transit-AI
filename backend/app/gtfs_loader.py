"""Load static GTFS from the NTA zip into Postgres using COPY."""

from __future__ import annotations

import csv
import io
import sys
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime
from pathlib import Path

import psycopg
from psycopg import sql

from app.config import Settings

DEFAULT_ZIP = Path(__file__).resolve().parents[2] / "data" / "GTFS_Realtime.zip"


# ---------- value converters ----------

def parse_gtfs_time(value: str) -> int | None:
    """'HH:MM:SS' -> seconds since service-day start. Hours may exceed 23. '' -> None."""
    value = value.strip()
    if not value:
        return None
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def none_if_empty(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def int_or_none(value: str | None) -> int | None:
    value = none_if_empty(value)
    return int(value) if value is not None else None


def float_or_none(value: str | None) -> float | None:
    value = none_if_empty(value)
    return float(value) if value is not None else None


def parse_gtfs_date(value: str) -> date:
    """'20261023' -> date(2026, 10, 23)."""
    return datetime.strptime(value.strip(), "%Y%m%d").date()


def date_or_none(value: str | None) -> date | None:
    value = none_if_empty(value)
    return parse_gtfs_date(value) if value is not None else None


# ---------- row mappers: CSV dict -> tuple in table column order ----------

def map_route(r: dict[str, str]) -> tuple:
    return (
        r["route_id"],
        none_if_empty(r.get("agency_id")),
        none_if_empty(r.get("route_short_name")),
        none_if_empty(r.get("route_long_name")),
        int(r["route_type"]),
    )


DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def map_calendar(r: dict[str, str]) -> tuple:
    return (
        r["service_id"],
        *(r[day] == "1" for day in DAYS),
        parse_gtfs_date(r["start_date"]),
        parse_gtfs_date(r["end_date"]),
    )


def map_calendar_date(r: dict[str, str]) -> tuple:
    return (r["service_id"], parse_gtfs_date(r["date"]), int(r["exception_type"]))


def map_stop(r: dict[str, str]) -> tuple:
    return (
        r["stop_id"],
        none_if_empty(r.get("stop_code")),
        none_if_empty(r.get("stop_name")),
        float_or_none(r.get("stop_lat")),
        float_or_none(r.get("stop_lon")),
    )


def map_trip(r: dict[str, str]) -> tuple:
    return (
        r["trip_id"],
        r["route_id"],
        r["service_id"],
        none_if_empty(r.get("trip_headsign")),
        int_or_none(r.get("direction_id")),
    )


def map_stop_time(r: dict[str, str]) -> tuple:
    return (
        r["trip_id"],
        int(r["stop_sequence"]),
        r["stop_id"],
        parse_gtfs_time(r.get("arrival_time", "")),
        parse_gtfs_time(r.get("departure_time", "")),
    )


# (table, file in zip, columns, mapper) -- in FK order: parents first
TABLES: list[tuple[str, str, tuple[str, ...], Callable[[dict[str, str]], tuple]]] = [
    ("routes", "routes.txt",
     ("route_id", "agency_id", "route_short_name", "route_long_name", "route_type"), map_route),
    ("calendar", "calendar.txt",
     ("service_id", *DAYS, "start_date", "end_date"), map_calendar),
    ("calendar_dates", "calendar_dates.txt",
     ("service_id", "date", "exception_type"), map_calendar_date),
    ("stops", "stops.txt",
     ("stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"), map_stop),
    ("trips", "trips.txt",
     ("trip_id", "route_id", "service_id", "trip_headsign", "direction_id"), map_trip),
    ("stop_times", "stop_times.txt",
     ("trip_id", "stop_sequence", "stop_id", "arrival_secs", "departure_secs"), map_stop_time),
]


# ---------- SQL ----------

# All 6 tables in one statement, so Postgres handles the FK order itself.
TRUNCATE_SQL = "TRUNCATE stop_times, trips, stops, calendar_dates, calendar, routes"

UPSERT_FEED_INFO_SQL = """
    INSERT INTO feed_info (id, feed_version, feed_start_date, feed_end_date)
    VALUES (TRUE, %s, %s, %s)
    ON CONFLICT (id) DO UPDATE SET
        feed_version    = EXCLUDED.feed_version,
        feed_start_date = EXCLUDED.feed_start_date,
        feed_end_date   = EXCLUDED.feed_end_date,
        loaded_at       = now()
"""


# ---------- reading ----------

def read_rows(zf: zipfile.ZipFile, filename: str) -> Iterator[dict[str, str]]:
    """Stream one CSV file out of the zip, row by row. Never extracts to disk."""
    with zf.open(filename) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        yield from csv.DictReader(text)


def read_feed_info(zf: zipfile.ZipFile) -> dict[str, str]:
    rows = list(read_rows(zf, "feed_info.txt"))
    if len(rows) != 1:
        raise ValueError(f"expected 1 row in feed_info.txt, got {len(rows)}")
    return rows[0]


def get_loaded_version(cur: psycopg.Cursor) -> str | None:
    cur.execute("SELECT feed_version FROM feed_info")
    row = cur.fetchone()
    return row[0] if row else None


# ---------- loading ----------

def copy_rows(cur: psycopg.Cursor, table: str, columns: Iterable[str], rows: Iterable[tuple]) -> int:
    stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, columns)),
    )
    count = 0
    with cur.copy(stmt) as copy:
        for row in rows:
            copy.write_row(row)
            count += 1
            if count % 1_000_000 == 0:
                print(f"  {table}: {count:,} rows...", flush=True)
    return count


def load_feed(conn: psycopg.Connection, zip_path: Path) -> bool:
    """Load the feed in the caller's transaction. Returns False if this version is already loaded."""
    with zipfile.ZipFile(zip_path) as zf, conn.cursor() as cur:
        info = read_feed_info(zf)
        new_version = info["feed_version"]

        if get_loaded_version(cur) == new_version:
            print(f"Feed {new_version} already loaded, skipping.")
            return False

        cur.execute(TRUNCATE_SQL)  # rolled back too if anything below fails
        for table, filename, columns, mapper in TABLES:
            start = time.perf_counter()
            rows = (mapper(r) for r in read_rows(zf, filename))
            n = copy_rows(cur, table, columns, rows)
            print(f"{table:<15} {n:>12,} rows  {time.perf_counter() - start:7.1f}s", flush=True)

        cur.execute(
            UPSERT_FEED_INFO_SQL,
            (
                new_version,
                date_or_none(info.get("feed_start_date")),
                date_or_none(info.get("feed_end_date")),
            ),
        )
        print(f"Loaded feed {new_version}.")
        return True


def main() -> None:
    zip_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ZIP
    settings = Settings()
    # One transaction: TRUNCATE + all COPYs + feed_info commit together, or not at all.
    with psycopg.connect(settings.database_url) as conn:
        loaded = load_feed(conn, zip_path)
    print("Done." if loaded else "Nothing to do.")


if __name__ == "__main__":
    main()