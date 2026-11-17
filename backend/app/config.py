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
    rt_retention_days: int = 2            # detailed latest-delay rows (rt_trips, rt_stop_delays)
    rt_summary_retention_days: int = 90   # per-route daily reliability summary (tiny)

    # Static timetable refresh (Milestone 6.5)
    static_gtfs_url: str = "https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip"
    static_refresh_hour: int = 4          # Dublin local time
    join_health_min_percent: float = 90.0

    # API (Milestone 4)
    timezone: str = "Europe/Dublin"
    realtime_stale_seconds: int = 300  # older realtime than this is ignored, not applied

    # Eval (Milestone 6): freeze "now" so known-answer questions stay valid. Leave unset in production.
    fixed_now: datetime | None = None

    # Agent (Milestone 5)
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-5-5"
    api_base_url: str = "http://localhost:8000"

    # /chat protection (Milestone 6.5)
    chat_per_ip_per_minute: int = 10
    chat_per_ip_per_day: int = 100
    chat_global_per_day: int = 500        # hard cap on LLM spend, whatever the number of IPs
    trust_proxy_headers: bool = False     # True only behind our own reverse proxy (Milestone 8)

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
