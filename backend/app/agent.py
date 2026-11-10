"""Milestone 5: LangGraph agent that answers Dublin transit questions using ONLY the API tools.

Run from backend\\ (the API must be running on API_BASE_URL):
    uv run python -m app.agent "Is the 39A running late right now?"
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.config import Settings

settings = Settings()

SYSTEM_PROMPT = """You are Dublin Transit AI, an assistant for Dublin public transport questions.

Rules:
- Answer ONLY from tool results. Never invent routes, stops, times, delays or statistics.
- If a tool says a route or stop is not found, say so plainly. Do not guess a replacement unless a tool result shows it.
- If the question names a route, call get_route_info for it FIRST, before asking any follow-up question.
  If the route is not found, say it is not in the current timetable and stop there.
- Never suggest another route number unless a tool result listed it.
- To answer about "my stop" or a named place, call search_stops first, then use the exact stop_id it returns.
  If several stops match and it matters, list them briefly and ask which one.
- Give times in Dublin local time as HH:MM. Say whether a time is realtime or timetable only.
- If realtime is "stale" or "none", say the answer is based on the timetable only.
- For reliability, quote the on-time percentage, the number of observations and the date range of the data.
  If observations are few, say the estimate is weak.
- Keep answers short: 1 to 4 sentences, or a short list for several departures.

Current Dublin time: {now}"""


# ---------- HTTP helper (patched in tests) ----------

def _get(path: str, params: dict | None = None) -> str:
    """Call the tools API and return JSON text. Errors come back as text the model can read."""
    try:
        resp = httpx.get(f"{settings.api_base_url}{path}", params=params, timeout=20.0)
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"transit API unreachable: {type(exc).__name__}"})
    if resp.status_code == 404:
        return json.dumps({"error": resp.json().get("detail", "not found")})
    if resp.status_code >= 400:
        return json.dumps({"error": f"transit API returned HTTP {resp.status_code}"})
    return resp.text


# ---------- tools: thin wrappers over the API ----------

@tool
def search_stops(query: str) -> str:
    """Find bus/Luas/rail stops by the stop number on the pole (e.g. "767") or part of the name
    (e.g. "UCD"). Returns stop_id, stop_code, stop_name and the routes serving each stop."""
    return _get("/stops/search", {"q": query, "limit": 8})


@tool
def get_departures(stop_id: str, route: str | None = None, limit: int = 5) -> str:
    """Next departures from a stop. stop_id must be the full id from search_stops (e.g. 8250DB000767).
    Optionally filter by route short name (e.g. "39A"). Includes realtime delays when available."""
    params = {"stop_id": stop_id, "limit": limit}
    if route:
        params["route"] = route
    return _get("/departures", params)


@tool
def get_route_delays(route: str) -> str:
    """Current delays for every trip on a route right now (e.g. "39A"), from the latest realtime snapshot."""
    return _get("/delays", {"route": route})


@tool
def get_on_time_performance(route: str, days: int = 7) -> str:
    """How reliable a route has been over the last N days (1 to 8): on-time %, late/early counts,
    observation count and the date range of the data."""
    return _get("/on-time-performance", {"route": route, "days": days})


@tool
def get_route_info(route: str) -> str:
    """Check whether a route exists in the current timetable and get its full name (e.g. "46A")."""
    return _get(f"/routes/{route}")


TOOLS = [search_stops, get_departures, get_route_delays, get_on_time_performance, get_route_info]


# ---------- graph ----------

def build_graph(model):
    """agent -> (tool calls?) -> tools -> agent ... -> final answer."""
    llm = model.bind_tools(TOOLS)
    tz = ZoneInfo(settings.timezone)

    def call_model(state: MessagesState) -> dict:
        now = (settings.fixed_now or datetime.now(timezone.utc)).astimezone(tz)
        system = SystemMessage(SYSTEM_PROMPT.format(now=now.strftime("%A %d %B %Y, %H:%M")))
        return {"messages": [llm.invoke([system, *state["messages"]])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)  # tool calls -> "tools", else -> END
    graph.add_edge("tools", "agent")
    return graph.compile()


@lru_cache(maxsize=1)
def default_graph():
    from langchain_anthropic import ChatAnthropic

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
    model = ChatAnthropic(
        model=settings.llm_model,
        api_key=settings.anthropic_api_key,
        max_tokens=1024,
    )
    return build_graph(model)


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


def answer(question: str, history: list[BaseMessage] | None = None, graph=None) -> dict:
    """Run the agent once. Returns the answer plus which tools were called (Milestone 6 eval uses this)."""
    graph = graph or default_graph()
    result = graph.invoke(
        {"messages": [*(history or []), HumanMessage(question)]},
        config={"recursion_limit": 12},  # hard stop on tool loops
    )
    messages = result["messages"]
    tool_calls = [
        {"name": call["name"], "args": call["args"]}
        for m in messages if isinstance(m, AIMessage) for call in m.tool_calls
    ]
    tool_errors = [m.content for m in messages if isinstance(m, ToolMessage) and '"error"' in str(m.content)]
    return {"answer": _text(messages[-1]), "tool_calls": tool_calls, "tool_errors": tool_errors}


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or "Is the 39A running late right now?"
    out = answer(question)
    print(out["answer"])
    print("\n[tools]", ", ".join(c["name"] for c in out["tool_calls"]) or "none")
