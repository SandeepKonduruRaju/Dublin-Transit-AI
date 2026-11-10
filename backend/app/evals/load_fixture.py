"""Load eval/fixtures into a TEST database (refuses any database whose name doesn't end in _test).

Run from backend\\ with POSTGRES_DB=transit_test:
    uv run python -m app.evals.load_fixture
"""

from __future__ import annotations

import re

import psycopg

from app.config import Settings
from app.evals.paths import FIXTURE_RT_DIR, FIXTURE_ZIP, SQL_DIR
from app.gtfs_loader import load_feed
from app.rt_parser import parse_feed
from app.rt_poller import store_snapshot

MIGRATION = re.compile(r"^\d{3}_.+\.sql$")  # 001_schema.sql ... skips verify_realtime.sql


def main() -> None:
    settings = Settings()
    if not settings.postgres_db.endswith("_test"):
        raise SystemExit(f"refusing to load fixtures into '{settings.postgres_db}': name must end with _test")

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        for path in sorted(p for p in SQL_DIR.iterdir() if MIGRATION.match(p.name)):
            conn.execute(path.read_text(encoding="utf-8"))
            print(f"applied {path.name}")
        conn.execute("TRUNCATE rt_snapshots CASCADE")

    with psycopg.connect(settings.database_url) as conn:  # one transaction for the static load
        load_feed(conn, FIXTURE_ZIP)

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        for pb in sorted(FIXTURE_RT_DIR.glob("*.pb")):
            sid = store_snapshot(conn, parse_feed(pb.read_bytes()))
            print(f"snapshot {pb.name} -> id {sid}")
        conn.execute("ANALYZE")
    print("Fixture loaded.")


if __name__ == "__main__":
    main()
