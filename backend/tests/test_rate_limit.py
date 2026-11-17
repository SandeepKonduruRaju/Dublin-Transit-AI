from app.rate_limit import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_per_minute_limit_then_recovers():
    clock = FakeClock()
    rl = RateLimiter(per_ip_minute=2, per_ip_day=100, global_day=100, clock=clock)
    assert rl.check("a") is None
    assert rl.check("a") is None
    retry = rl.check("a")
    assert retry is not None and 0 < retry <= 60
    clock.t += 61
    assert rl.check("a") is None


def test_ips_are_independent():
    rl = RateLimiter(per_ip_minute=1, per_ip_day=100, global_day=100, clock=FakeClock())
    assert rl.check("a") is None
    assert rl.check("b") is None
    assert rl.check("a") is not None


def test_global_daily_cap_blocks_everyone():
    clock = FakeClock()
    rl = RateLimiter(per_ip_minute=100, per_ip_day=100, global_day=3, clock=clock)
    for ip in ("a", "b", "c"):
        assert rl.check(ip) is None
    assert rl.check("new-ip") is not None  # cost bound holds even for a fresh IP
    clock.t += 86_401
    assert rl.check("new-ip") is None


def test_blocked_requests_do_not_count():
    clock = FakeClock()
    rl = RateLimiter(per_ip_minute=1, per_ip_day=2, global_day=100, clock=clock)
    assert rl.check("a") is None
    for _ in range(5):
        assert rl.check("a") is not None  # rejected, so not recorded
    clock.t += 61
    assert rl.check("a") is None  # 2nd allowed request of the day
