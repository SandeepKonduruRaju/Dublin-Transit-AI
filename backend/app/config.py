from datetime import datetime
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> parents[2] is the repo root, where .env lives
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    # Postgres
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # NTA GTFS-Realtime (optional here so the static loader still runs without it)
    nta_api_key: str | None = None
    nta_trip_updates_url: str = "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates"
    rt_poll_interval_seconds: int = 60
    rt_retention_days: int = 8

    # API (Milestone 4)
    timezone: str = "Europe/Dublin"
    realtime_stale_seconds: int = 300  # older realtime than this is ignored, not applied

    # Eval (Milestone 6): freeze "now" so known-answer questions stay valid. Leave unset in production.
    fixed_now: datetime | None = None

    # Agent (Milestone 5)
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-5-5"
    api_base_url: str = "http://localhost:8000"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
