"""Parse a GTFS-Realtime TripUpdates protobuf into plain rows.

Pure functions: no network, no database. That makes this the easy part to unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from google.transit import gtfs_realtime_pb2

TripRel = gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship
StopRel = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.ScheduleRelationship

# Column order must match the tuples built below (snapshot_id is prepended when storing).
TRIP_COLUMNS = (
    "entity_id", "trip_id", "route_id", "start_date", "start_time",
    "direction_id", "schedule_relationship",
)
STOP_COLUMNS = (
    "entity_id", "trip_id", "stop_sequence", "stop_id",
    "arrival_delay", "arrival_time", "departure_delay", "departure_time",
    "schedule_relationship",
)


@dataclass(frozen=True)
class ParsedFeed:
    feed_timestamp: datetime
    trip_rows: list[tuple]
    stop_rows: list[tuple]


def _opt(msg, field: str):
    """Return the field value, or None if it was never set (proto2 presence)."""
    return getattr(msg, field) if msg.HasField(field) else None


def _utc(seconds: int | None) -> datetime | None:
    return datetime.fromtimestamp(seconds, tz=timezone.utc) if seconds is not None else None


def _gtfs_date(value: str | None) -> date | None:
    return datetime.strptime(value, "%Y%m%d").date() if value else None


def _enum_name(enum, value: int) -> str:
    try:
        return enum.Name(value)
    except ValueError:  # a value newer than our bindings know about
        return str(value)


def _event(stu, name: str) -> tuple[int | None, datetime | None]:
    """(delay, time) for stu.arrival or stu.departure."""
    if not stu.HasField(name):
        return None, None
    ev = getattr(stu, name)
    return _opt(ev, "delay"), _utc(_opt(ev, "time"))


def parse_feed(data: bytes) -> ParsedFeed:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(data)  # raises google.protobuf.message.DecodeError on garbage
    if not feed.header.HasField("timestamp"):
        raise ValueError("feed header has no timestamp, cannot dedupe")

    trip_rows: list[tuple] = []
    stop_rows: list[tuple] = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue  # vehicle positions / alerts are not stored here
        tu = entity.trip_update
        trip = tu.trip
        trip_id = _opt(trip, "trip_id")
        trip_rows.append((
            entity.id,
            trip_id,
            _opt(trip, "route_id"),
            _gtfs_date(_opt(trip, "start_date")),
            _opt(trip, "start_time"),
            _opt(trip, "direction_id"),
            _enum_name(TripRel, trip.schedule_relationship),
        ))
        for stu in tu.stop_time_update:
            arr_delay, arr_time = _event(stu, "arrival")
            dep_delay, dep_time = _event(stu, "departure")
            stop_rows.append((
                entity.id,
                trip_id,
                _opt(stu, "stop_sequence"),
                _opt(stu, "stop_id"),
                arr_delay,
                arr_time,
                dep_delay,
                dep_time,
                _enum_name(StopRel, stu.schedule_relationship),
            ))

    return ParsedFeed(_utc(feed.header.timestamp), trip_rows, stop_rows)
