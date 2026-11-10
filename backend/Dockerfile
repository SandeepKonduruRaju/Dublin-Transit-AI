FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependencies first: this layer stays cached until pyproject.toml / uv.lock change
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "app.rt_poller"]
