"""Milestone 5: HTTP endpoint for the agent (the chat UI in Milestone 7 calls this).

Run from backend\\:  uv run uvicorn app.agent_api:app --port 8001
"""

from __future__ import annotations

import math
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from app.agent import answer
from app.config import Settings
from app.rate_limit import RateLimiter

app = FastAPI(title="Dublin Transit AI agent", version="0.1.0")
settings = Settings()
limiter = RateLimiter(settings.chat_per_ip_per_minute, settings.chat_per_ip_per_day, settings.chat_global_per_day)


def client_ip(request: Request) -> str:
    """Behind our own proxy (Milestone 8) the real IP is the first X-Forwarded-For entry; never trust it otherwise."""
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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
def chat(req: ChatRequest, request: Request) -> ChatResponse:
    retry_after = limiter.check(client_ip(request))
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(math.ceil(retry_after))},
        )
    history = [HumanMessage(t.content) if t.role == "user" else AIMessage(t.content) for t in req.history]
    try:
        out = answer(req.message, history)
    except RuntimeError as exc:  # e.g. missing ANTHROPIC_API_KEY
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ChatResponse(answer=out["answer"], tool_calls=out["tool_calls"])
