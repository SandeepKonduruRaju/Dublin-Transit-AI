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
