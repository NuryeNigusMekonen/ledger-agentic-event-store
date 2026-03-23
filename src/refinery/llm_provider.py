from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChatProviderConfig:
    provider: str
    api_key: str
    model: str
    endpoint: str

    def headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if self.provider == "openrouter":
            headers["X-Title"] = os.getenv("OPENROUTER_APP_NAME", "ledger-agentic-event-store")
        return headers


def resolve_chat_provider(
    *,
    openai_api_key: str | None = None,
    openai_model: str | None = None,
) -> ChatProviderConfig | None:
    if openai_api_key is not None:
        resolved_api_key = openai_api_key.strip()
        if not resolved_api_key:
            return None
        resolved_model = (
            openai_model.strip()
            if openai_model is not None and openai_model.strip()
            else os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        )
        return ChatProviderConfig(
            provider="openai",
            api_key=resolved_api_key,
            model=resolved_model,
            endpoint="https://api.openai.com/v1/chat/completions",
        )

    env_openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if env_openrouter_api_key:
        resolved_model = (
            os.getenv("OPENROUTER_MODEL", "").strip()
            or os.getenv("MODEL", "").strip()
            or "openrouter/auto"
        )
        base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip().rstrip("/")
        return ChatProviderConfig(
            provider="openrouter",
            api_key=env_openrouter_api_key,
            model=resolved_model,
            endpoint=f"{base_url}/chat/completions",
        )

    env_openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if env_openai_api_key:
        resolved_model = (
            openai_model.strip()
            if openai_model is not None and openai_model.strip()
            else os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        )
        return ChatProviderConfig(
            provider="openai",
            api_key=env_openai_api_key,
            model=resolved_model,
            endpoint="https://api.openai.com/v1/chat/completions",
        )

    return None
