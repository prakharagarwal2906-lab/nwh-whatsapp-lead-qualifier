"""Runtime settings, read from environment variables.

Secrets (webhook tokens, API keys) must come from a secrets manager or the
environment, never from code.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _csv(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [v.strip() for v in raw.split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "qualifier.db"))
    # postgresql://user:pass@host:5432/db  — when set, used instead of the SQLite file.
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    # Two tokens may be valid at once so rotation never drops events.
    webhook_tokens: list[str] = field(default_factory=lambda: _csv("GUPSHUP_WEBHOOK_TOKENS"))
    webhook_token_header: str = field(
        default_factory=lambda: os.getenv("GUPSHUP_WEBHOOK_TOKEN_HEADER", "X-Webhook-Token")
    )
    # Empty list disables the IP check (useful locally); set Gupshup ranges in prod.
    allowed_ips: list[str] = field(default_factory=lambda: _csv("GUPSHUP_ALLOWED_IPS"))
    max_event_age_seconds: int = field(
        default_factory=lambda: int(os.getenv("MAX_EVENT_AGE_SECONDS", "86400"))
    )
    gupshup_base_url: str = field(
        default_factory=lambda: os.getenv("GUPSHUP_BASE_URL", "https://partner.gupshup.io")
    )
    gupshup_app_token: str = field(default_factory=lambda: os.getenv("GUPSHUP_APP_TOKEN", ""))
    l2r_base_url: str = field(default_factory=lambda: os.getenv("L2R_BASE_URL", "http://localhost:9000"))
    l2r_api_key: str = field(default_factory=lambda: os.getenv("L2R_API_KEY", ""))
    internal_api_key: str = field(default_factory=lambda: os.getenv("INTERNAL_API_KEY", ""))
    brand_name: str = field(default_factory=lambda: os.getenv("BRAND_NAME", "BikeWale"))
    # BikeWale's calling team qualifies leads first, so its hours decide "after hours" and call times.
    team_working_hours: dict = field(default_factory=lambda: json.loads(os.getenv(
        "TEAM_WORKING_HOURS", json.dumps({d: [["09:30", "18:30"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat")}))))
    team_timezone: str = field(default_factory=lambda: os.getenv("TEAM_TIMEZONE", "Asia/Kolkata"))
    # Seed values for the MASTER dealer row (BikeWale); after the first start the row is the source of truth.
    support_number: str = field(default_factory=lambda: os.getenv("SUPPORT_NUMBER", "918047105555"))
    waba_number: str = field(default_factory=lambda: os.getenv("WABA_NUMBER", "918047104444"))
    gupshup_app_id: str = field(default_factory=lambda: os.getenv("GUPSHUP_APP_ID", ""))
    log_file: str = field(default_factory=lambda: os.getenv("LOG_FILE", "logs/qualifier.log"))
    session_window_hours: int = 24


def get_settings() -> Settings:
    return Settings()
