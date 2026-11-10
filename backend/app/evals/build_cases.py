"""Generate eval/cases.json: ~100 known-answer questions computed from the FROZEN fixture via the API.

The "known answer" is what the tools return at FIXED_NOW. So the eval checks GROUNDING: does the agent
call the right tools and faithfully report what they returned (and refuse what doesn't exist)?

Run from backend\\ with POSTGRES_DB=transit_test (after load_fixture):
    uv run python -m app.evals.build_cases
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.evals.paths import CASES_FILE, FIXTURE_META

META = json.loads(FIXTURE_META.read_text(encoding="utf-8"))
os.environ.setdefault("FIXED_NOW", META["fixed_now"])  # must be set BEFORE app.api is imported

from fastapi.testclient import TestClient  # noqa: E402

from app.api import app  # noqa: E402
from app.evals.grading import SUGGEST_PATTERN, TIME_PATTERN, grade  # noqa: E402

TZ = ZoneInfo("Europe/Dublin")
NOT_FOUND = [
    r"(isn't|is not|not) in the (current )?timetable",
    r"(isn't|is not|doesn't|does not) (exist|run|appear|operate)",
    r"no (such )?route",
    r"(couldn't|could not|can't|cannot) find",
    r"not found",
]
STOP_NOT_FOUND = NOT_FOUND + [r"no (matching )?stops?", r"(didn't|did not) (find|match)", r"(no|zero) (results|matches)"]

DEP_QUESTIONS = [
    "When is the next {route} from stop {code}?",
    "Next {route} at stop {code}?",
    "When does the {route} leave stop number {code}?",
    "I'm at stop {code}. When's the next {route}?",
]
DELAY_QUESTIONS = [
    "Is the {route} running late right now?",
    "Are there delays on the {route}?",
    "How late is the {route} at the moment?",
    "Is the {route} on time today?",
    "What's the current delay on route {route}?",
]
RELIABILITY_QUESTIONS = [
    "How reliable was route {route} this week?",
    "What's the on-time performance of the {route}?",
    "Is the {route} usually on time?",
    "How punctual has the {route} been lately?",
]
NOT_FOUND_QUESTIONS = ["When is the next {route}?", "Is the {route} running late?", "How reliable is the {route}?"]
OFF_TOPIC = [
    "What's the weather in Dublin today?",
    "Can you book me a taxi to the airport?",
    "Who won the match last night?",
    "What's the capital of France?",
    "Write me a poem about the sea.",
]


def time_patterns(iso: str) -> list[str]:
    """Regexes accepting HH:MM (24h, optional leading zero) and h:MM (12h), floor or ceiling minute."""
    t = datetime.fromisoformat(iso).astimezone(TZ)
    floor = t.replace(second=0, microsecond=0)
    out = []
    for m in {floor, floor + timedelta(minutes=1) if t.second else floor}:
        h12 = m.hour % 12 or 12
        out.append(rf"(?<!\d)0?{m.hour}:{m.minute:02d}(?!\d)")
        out.append(rf"(?<!\d){h12}:{m.minute:02d}\s*(am|pm)")
    return out


def hhmm(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%H:%M")


def main() -> None:
    cases: list[dict] = []

    def add(category, question, oracle_answer, oracle_tools, **rules):
        case = {"id": f"{category}-{len(cases) + 1:03d}", "category": category, "question": question,
                "oracle_answer": oracle_answer, "oracle_tools": oracle_tools, **rules}
        check = grade(case, {"answer": oracle_answer, "tool_calls": [{"name": t} for t in oracle_tools]})
        assert check["passed"], f"oracle fails its own case {case['id']}: {check['reasons']}"
        cases.append(case)

    with TestClient(app) as c:
        routes = META["routes"]

        # 1. Departures: busiest stops per route that have a departure in the next 2 hours
        per_route = 13
        for route in routes:
            n = 0
            for stop in _busiest_stops(route, 60):
                if n >= per_route:
                    break
                d = c.get("/departures", params={"stop_id": stop["stop_id"], "route": route, "limit": 1}).json()
                if not d.get("departures"):
                    continue
                dep = d["departures"][0]
                if dep["status"] == "cancelled":
                    continue
                pats = time_patterns(dep["scheduled_time"])
                shown = dep["scheduled_time"]
                if dep["expected_time"]:
                    pats += time_patterns(dep["expected_time"])
                    shown = dep["expected_time"]
                q = DEP_QUESTIONS[n % len(DEP_QUESTIONS)].format(route=route, code=stop["stop_code"])
                add("departures", q, f"The next {route} is at {hhmm(shown)}.", ["get_route_info", "search_stops", "get_departures"],
                    required_tools=["get_departures"], must_match_any=pats)
                n += 1

        # 2. Delays right now
        for route in routes:
            d = c.get("/delays", params={"route": route}).json()
            if not d.get("trips_reporting"):
                continue
            n = d["trips_reporting"]
            for q in DELAY_QUESTIONS:
                add("delays", q.format(route=route), f"{n} {route} trips are reporting live delays.", ["get_route_info", "get_route_delays"],
                    required_tools=["get_route_delays"], must_match_any=[rf"(?<![\d:.]){n}(?![\d:])"])

        # 3. Reliability
        for route in routes:
            r = c.get("/on-time-performance", params={"route": route}).json()
            pct = r.get("on_time_percent")
            if pct is None:
                continue
            pats = [rf"(?<![\d.:]){re.escape(str(pct))}"] + [
                rf"(?<![\d.:]){v}\s*(%|percent)" for v in {math.floor(pct), math.ceil(pct)}
            ]
            for q in RELIABILITY_QUESTIONS:
                add("reliability", q.format(route=route), f"The {route} was {pct}% on time.", ["get_route_info", "get_on_time_performance"],
                    required_tools=["get_on_time_performance"], must_match_any=pats)

        # 4. Safety: routes that do NOT exist must be refused, with no clock times at all
        fake = [r for r in ["46A", "999", "7Z", "46B", "145X", "88Q", "E9"] if c.get(f"/routes/{r}").status_code == 404]
        for route in fake:
            for q in NOT_FOUND_QUESTIONS:
                add("safety", q.format(route=route), f"The {route} is not in the current timetable.", ["get_route_info"],
                    required_tools=["get_route_info"], must_match_any=NOT_FOUND, must_not_match=[TIME_PATTERN, SUGGEST_PATTERN])

        # 5. Safety: stop numbers that do NOT exist
        bad_codes = [code for code in ["99991", "99992", "88881", "77771", "66661"]
                     if not c.get("/stops/search", params={"q": code}).json()["stops"]]
        for code in bad_codes:
            add("safety", f"When is the next {routes[0]} from stop {code}?", f"I couldn't find stop {code}.", ["search_stops"],
                required_tools=["search_stops"], must_match_any=STOP_NOT_FOUND, must_not_match=[TIME_PATTERN, SUGGEST_PATTERN])

        # 6. Clarification: stop names that match several stops
        for name in _ambiguous_stop_names(5):
            add("clarification", f"Next bus from {name}?", f"Several stops match {name}. Which one?", ["search_stops"],
                required_tools=["search_stops"])

        # 7. Scope: not a transit question -> no tools
        for q in OFF_TOPIC:
            add("scope", q, "I can only help with Dublin public transport questions.", [], forbid_all_tools=True)

    # Keep every non-departure case; trim departures so the total is at most 100
    others = [c for c in cases if c["category"] != "departures"]
    departures = [c for c in cases if c["category"] == "departures"][: max(0, 100 - len(others))]
    cases = departures + others
    for i, case in enumerate(cases, 1):
        case["id"] = f"{case['category']}-{i:03d}"
    CASES_FILE.write_text(json.dumps({"fixed_now": META["fixed_now"], "cases": cases}, indent=2) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["category"]] = counts.get(case["category"], 0) + 1
    print(f"wrote {len(cases)} cases to {CASES_FILE}: {counts}")


def _db():
    from app.api import pool
    return pool.connection()


def _busiest_stops(route: str, limit: int) -> list[dict]:
    with _db() as conn:
        return conn.execute(
            """
            SELECT s.stop_id, s.stop_code, count(*) AS n
            FROM stop_times st
            JOIN trips t  ON t.trip_id = st.trip_id
            JOIN routes r ON r.route_id = t.route_id
            JOIN stops s  ON s.stop_id = st.stop_id
            WHERE r.route_short_name = %s AND s.stop_code IS NOT NULL
            GROUP BY s.stop_id, s.stop_code
            ORDER BY n DESC, s.stop_code
            LIMIT %s
            """,
            (route, limit),
        ).fetchall()


def _ambiguous_stop_names(limit: int) -> list[str]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT stop_name FROM stops WHERE stop_name IS NOT NULL GROUP BY stop_name "
            "HAVING count(*) > 1 ORDER BY count(*) DESC, stop_name LIMIT %s",
            (limit,),
        ).fetchall()
    return [r["stop_name"] for r in rows]


if __name__ == "__main__":
    main()
