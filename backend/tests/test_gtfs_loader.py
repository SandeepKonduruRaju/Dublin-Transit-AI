import io
import zipfile
from datetime import date

import pytest

from app.gtfs_loader import map_calendar, map_stop_time, parse_gtfs_time, read_rows

from app.gtfs_loader import read_feed_info  # add to the existing imports


def test_read_feed_info_returns_version():
    content = (
        "feed_publisher_name,feed_version,feed_start_date\r\n"
        "NTA,2407DE34-ABC,20261024\r\n"
    ).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("feed_info.txt", content)
    buf.seek(0)

    with zipfile.ZipFile(buf) as zf:
        assert read_feed_info(zf)["feed_version"] == "2407DE34-ABC"

        
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("05:45:00", 20700),
        ("24:01:00", 86460),  # after-midnight trip, same service day
        ("5:45:00", 20700),   # GTFS allows a single-digit hour
        ("", None),
        ("  ", None),
    ],
)
def test_parse_gtfs_time(value, expected):
    assert parse_gtfs_time(value) == expected


def test_parse_gtfs_time_rejects_garbage():
    with pytest.raises(ValueError):
        parse_gtfs_time("not-a-time")


def test_map_stop_time_converts_types_and_order():
    row = {
        "trip_id": "5828_1038",
        "arrival_time": "24:01:00",
        "departure_time": "24:01:00",
        "stop_id": "8240DB003663",
        "stop_sequence": "51",
    }
    assert map_stop_time(row) == ("5828_1038", 51, "8240DB003663", 86460, 86460)


def test_map_calendar_parses_flags_and_dates():
    row = {
        "service_id": "57", "monday": "0", "tuesday": "0", "wednesday": "0",
        "thursday": "0", "friday": "0", "saturday": "1", "sunday": "0",
        "start_date": "20261024", "end_date": "20261031",
    }
    assert map_calendar(row) == (
        "57", False, False, False, False, False, True, False,
        date(2026, 10, 24), date(2026, 10, 31),
    )


def test_read_rows_handles_bom_and_crlf():
    # Same quirks as the real NTA files: UTF-8 BOM and Windows line endings
    content = "\ufeffstop_id,stop_name\r\nS1,Stillorgan Road\r\n".encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("stops.txt", content)
    buf.seek(0)

    with zipfile.ZipFile(buf) as zf:
        rows = list(read_rows(zf, "stops.txt"))

    assert rows == [{"stop_id": "S1", "stop_name": "Stillorgan Road"}]