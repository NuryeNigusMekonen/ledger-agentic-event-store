from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if value is None:
        return default
    parsed = [item.strip() for item in value.split(",") if item.strip()]
    return parsed or default


def _expand_localhost_aliases(origins: list[str]) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()

    def _add(origin: str) -> None:
        if origin not in seen:
            expanded.append(origin)
            seen.add(origin)

    for origin in origins:
        _add(origin)
        if "://localhost" in origin:
            _add(origin.replace("://localhost", "://127.0.0.1"))
            _add(origin.replace("://localhost", "://0.0.0.0"))
            _add(origin.replace("://localhost", "://[::1]"))
        if "://127.0.0.1" in origin:
            _add(origin.replace("://127.0.0.1", "://localhost"))
            _add(origin.replace("://127.0.0.1", "://0.0.0.0"))
            _add(origin.replace("://127.0.0.1", "://[::1]"))
        if "://0.0.0.0" in origin:
            _add(origin.replace("://0.0.0.0", "://localhost"))
            _add(origin.replace("://0.0.0.0", "://127.0.0.1"))
            _add(origin.replace("://0.0.0.0", "://[::1]"))
        if "://[::1]" in origin:
            _add(origin.replace("://[::1]", "://localhost"))
            _add(origin.replace("://[::1]", "://127.0.0.1"))
            _add(origin.replace("://[::1]", "://0.0.0.0"))
    return expanded


@dataclass(slots=True)
class AppSettings:
    database_url: str
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: list[str] = field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://0.0.0.0:5173",
            "http://[::1]:5173",
        ]
    )
    cors_origin_regex: str = (
        r"^https?://("
        r"localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|"
        r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"192\.168\.\d{1,3}\.\d{1,3}|"
        r"172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}"
        r")(:\d+)?$"
    )
    api_key: str | None = None
    apply_schema_on_start: bool = True
    command_timeout_seconds: float = 15.0
    jwt_secret: str = "change-me-in-production"
    jwt_issuer: str = "ledger-api"
    jwt_ttl_minutes: int = 120
    seed_demo_users: bool = True

    @classmethod
    def from_env(cls) -> AppSettings:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required to start the API service.")

        return cls(
            database_url=database_url,
            api_host=os.getenv("API_HOST", "127.0.0.1"),
            api_port=int(os.getenv("API_PORT", "8000")),
            cors_origins=_expand_localhost_aliases(
                _env_csv("API_CORS_ORIGINS", ["http://localhost:5173"])
            ),
            api_key=os.getenv("LEDGER_API_KEY"),
            apply_schema_on_start=_env_bool("API_APPLY_SCHEMA_ON_START", True),
            command_timeout_seconds=float(os.getenv("API_COMMAND_TIMEOUT_SECONDS", "15")),
            jwt_secret=os.getenv("JWT_SECRET", "change-me-in-production"),
            jwt_issuer=os.getenv("JWT_ISSUER", "ledger-api"),
            jwt_ttl_minutes=int(os.getenv("JWT_TTL_MINUTES", "120")),
            seed_demo_users=_env_bool("SEED_DEMO_USERS", True),
        )
