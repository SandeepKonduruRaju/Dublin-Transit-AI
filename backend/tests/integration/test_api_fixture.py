from datetime import datetime


def test_health_uses_fixture_and_fresh_realtime(client, meta):
    h = client.get("/health").json()
    assert h["static_feed_version"] == meta["static_feed_version"]
    assert 0 <= h["realtime_age_seconds"] <= 300


def test_unknown_route_is_404(client):
    assert client.get("/routes/46A").status_code == 404
    assert client.get("/delays", params={"route": "46A"}).status_code == 404


def test_known_route_exists(client, meta):
    for route in meta["routes"]:
        assert client.get(f"/routes/{route}").status_code == 200


def test_departures_are_in_the_future_and_sorted(client, meta):
    route = meta["routes"][0]  # sample_stop_id is a busy stop on this route
    deps = client.get("/departures", params={"stop_id": meta["sample_stop_id"], "route": route, "limit": 5}).json()["departures"]
    assert deps
    now = datetime.fromisoformat(meta["fixed_now"])
    # effective time: expected if realtime, otherwise scheduled
    times = [datetime.fromisoformat(d["expected_time"] if d["status"] == "realtime" else d["scheduled_time"]) for d in deps]
    assert times == sorted(times)
    assert all(t >= now.replace(microsecond=0) for t in times)


def test_realtime_delays_and_reliability_have_data(client, meta):
    delays = [client.get("/delays", params={"route": r}).json()["trips_reporting"] for r in meta["routes"]]
    otp = [client.get("/on-time-performance", params={"route": r}).json()["observations"] for r in meta["routes"]]
    assert any(delays) and any(otp)
