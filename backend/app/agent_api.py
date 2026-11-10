"""Milestone 5: HTTP endpoint for the agent (the chat UI in Milestone 7 calls this).

Run from backend\\:  uv run uvicorn app.agent_api:app --port 8001
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from app.agent import answer

app = FastAPI(title="Dublin Transit AI agent", version="0.1.0")


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    history: list[Turn] = Field(default_factory=list, max_length=20)


class ChatResponse(BaseModel):
    answer: str
    tool_calls: list[dict]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    history = [HumanMessage(t.content) if t.role == "user" else AIMessage(t.content) for t in req.history]
    try:
        out = answer(req.message, history)
    except RuntimeError as exc:  # e.g. missing ANTHROPIC_API_KEY
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ChatResponse(answer=out["answer"], tool_calls=out["tool_calls"])
