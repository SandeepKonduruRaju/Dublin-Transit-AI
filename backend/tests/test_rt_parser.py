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
