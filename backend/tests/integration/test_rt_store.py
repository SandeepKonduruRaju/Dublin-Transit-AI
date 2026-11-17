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
