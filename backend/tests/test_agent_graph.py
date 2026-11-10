"""Tests the agent LOOP without a real LLM or API: a scripted fake model + a patched HTTP helper."""

import json

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

import app.agent as agent


class ScriptedModel(GenericFakeChatModel):
    """Replays fixed AIMessages; bind_tools is a no-op so it works in the graph."""

    def bind_tools(self, tools, **kwargs):
        return self


def test_agent_calls_tool_then_answers(monkeypatch):
    calls = []

    def fake_get(path, params=None):
        calls.append(path)
        return json.dumps({"error": "Route '46A' is not in the current timetable"})

    monkeypatch.setattr(agent, "_get", fake_get)
    model = ScriptedModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "get_route_info", "args": {"route": "46A"}, "id": "c1"}]),
        AIMessage(content="The 46A is not in the current timetable."),
    ]))

    out = agent.answer("When is the next 46A?", graph=agent.build_graph(model))

    assert calls == ["/routes/46A"]
    assert [c["name"] for c in out["tool_calls"]] == ["get_route_info"]
    assert "not in the current timetable" in out["answer"]
    assert out["tool_errors"]  # the 404 reached the model as data, not an exception
