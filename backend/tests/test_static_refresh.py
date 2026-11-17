from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.evals.paths import FIXTURE_ZIP
from app.static_refresh import seconds_until, zip_feed_version

DUBLIN = ZoneInfo("Europe/Dublin")


def test_seconds_until_later_today():
    now = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)  # 01:00 Dublin (GMT in November)
    assert seconds_until(4, DUBLIN, now) == 3 * 3600


def test_seconds_until_rolls_to_tomorrow():
    now = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
    assert seconds_until(4, DUBLIN, now) == 23 * 3600


@pytest.mark.skipif(not FIXTURE_ZIP.exists(), reason="fixture not built")
def test_zip_feed_version_reads_feed_info():
    assert zip_feed_version(FIXTURE_ZIP).startswith("FIXTURE-")
