"""Build a small, frozen test dataset from the real feeds (run once, then commit eval/fixtures/).

Static: a mini GTFS zip with ALL routes (so "route not found" is realistic) but only the trips of a few
routes that run on the fixture day. Realtime: the last N saved .pb snapshots, copied as-is.

Run from backend\\:
    uv run python -m app.evals.build_fixture
    uv run python -m app.evals.build_fixture --routes 39A,15,E1 --snapshots 3
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.evals.paths import FIXTURE_DIR, FIXTURE_META, FIXTURE_RT_DIR, FIXTURE_ZIP, REPO_ROOT
from app.gtfs_loader import read_rows
from app.rt_parser import parse_feed

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TZ = ZoneInfo("Europe/Dublin")


def active_services(calendar: list[dict], calendar_dates: list[dict], d: date) -> set[str]:
    ymd = d.strftime("%Y%m%d")
    day = DAYS[d.weekday()]
    active = {c["service_id"] for c in calendar if c["start_date"] <= ymd <= c["end_date"] and c[day] == "1"}
    active |= {c["service_id"] for c in calendar_dates if c["date"] == ymd and c["exception_type"] == "1"}
    active -= {c["service_id"] for c in calendar_dates if c["date"] == ymd and c["exception_type"] == "2"}
    return active


def _write_csv(zout: zipfile.ZipFile, name: str, rows: list[dict], fieldnames: list[str]) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    zout.writestr(name, buf.getvalue())


def _fields(zf: zipfile.ZipFile, name: str) -> list[str]:
    with zf.open(name) as raw:
        return next(csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", type=Path, default=REPO_ROOT / "data" / "GTFS_Realtime.zip")
    ap.add_argument("--rt-dir", type=Path, default=REPO_ROOT / "data" / "rt_raw")
    ap.add_argument("--routes", default="39A,15,E1")
    ap.add_argument("--snapshots", type=int, default=3)
    args = ap.parse_args()
    routes_wanted = {r.strip() for r in args.routes.split(",")}

    # 1. Realtime: newest N snapshots that parse
    pb_files = sorted(args.rt_dir.glob("*.pb"))
    if len(pb_files) < args.snapshots:
        raise SystemExit(f"need {args.snapshots} .pb files in {args.rt_dir}, found {len(pb_files)}")
    chosen = pb_files[-args.snapshots:]
    stamps = [parse_feed(p.read_bytes()).feed_timestamp for p in chosen]
    fixed_now = max(stamps) + timedelta(seconds=30)
    fixture_day = fixed_now.astimezone(TZ).date()

    # 2. Static subset
    with zipfile.ZipFile(args.zip) as zf:
        routes = list(read_rows(zf, "routes.txt"))
        calendar = list(read_rows(zf, "calendar.txt"))
        calendar_dates = list(read_rows(zf, "calendar_dates.txt"))
        feed_info = list(read_rows(zf, "feed_info.txt"))
        route_ids = {r["route_id"] for r in routes if r["route_short_name"] in routes_wanted}
        missing = routes_wanted - {r["route_short_name"] for r in routes}
        if missing:
            raise SystemExit(f"routes not in the static feed: {sorted(missing)}")

        services = active_services(calendar, calendar_dates, fixture_day) | active_services(
            calendar, calendar_dates, fixture_day - timedelta(days=1)
        )
        trips = [t for t in read_rows(zf, "trips.txt") if t["route_id"] in route_ids and t["service_id"] in services]
        trip_ids = {t["trip_id"] for t in trips}
        stop_times = [st for st in read_rows(zf, "stop_times.txt") if st["trip_id"] in trip_ids]
        stop_ids = {st["stop_id"] for st in stop_times}
        stops = [s for s in read_rows(zf, "stops.txt") if s["stop_id"] in stop_ids]
        used_services = {t["service_id"] for t in trips}

        orig_version = feed_info[0]["feed_version"]
        feed_info[0]["feed_version"] = f"FIXTURE-{orig_version}"

        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(FIXTURE_ZIP, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            _write_csv(zout, "routes.txt", routes, _fields(zf, "routes.txt"))
            _write_csv(zout, "calendar.txt", [c for c in calendar if c["service_id"] in used_services], _fields(zf, "calendar.txt"))
            _write_csv(zout, "calendar_dates.txt", [c for c in calendar_dates if c["service_id"] in used_services], _fields(zf, "calendar_dates.txt"))
            _write_csv(zout, "stops.txt", stops, _fields(zf, "stops.txt"))
            _write_csv(zout, "trips.txt", trips, _fields(zf, "trips.txt"))
            _write_csv(zout, "stop_times.txt", stop_times, _fields(zf, "stop_times.txt"))
            _write_csv(zout, "feed_info.txt", feed_info, _fields(zf, "feed_info.txt"))

    # 3. Copy realtime snapshots
    if FIXTURE_RT_DIR.exists():
        shutil.rmtree(FIXTURE_RT_DIR)
    FIXTURE_RT_DIR.mkdir(parents=True)
    for p in chosen:
        shutil.copy2(p, FIXTURE_RT_DIR / p.name)

    # A busy stop on the first route, for integration tests
    first_route_ids = {r["route_id"] for r in routes if r["route_short_name"] == sorted(routes_wanted)[0]}
    first_trips = {t["trip_id"] for t in trips if t["route_id"] in first_route_ids}
    stop_counts: dict[str, int] = {}
    for st in stop_times:
        if st["trip_id"] in first_trips:
            stop_counts[st["stop_id"]] = stop_counts.get(st["stop_id"], 0) + 1
    sample_stop_id = max(sorted(stop_counts), key=stop_counts.get)

    meta = {
        "fixed_now": fixed_now.astimezone(timezone.utc).isoformat(),
        "sample_stop_id": sample_stop_id,
        "fixture_day": fixture_day.isoformat(),
        "routes": sorted(routes_wanted),
        "static_feed_version": f"FIXTURE-{orig_version}",
        "snapshots": [p.name for p in chosen],
        "counts": {"routes": len(routes), "trips": len(trips), "stop_times": len(stop_times), "stops": len(stops)},
    }
    FIXTURE_META.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    size_kb = (FIXTURE_ZIP.stat().st_size + sum(p.stat().st_size for p in FIXTURE_RT_DIR.iterdir())) // 1024
    print(json.dumps(meta, indent=2))
    print(f"fixture size on disk: about {size_kb:,} KB")


if __name__ == "__main__":
    main()
