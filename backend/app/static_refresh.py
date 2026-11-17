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
