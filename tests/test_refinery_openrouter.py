from __future__ import annotations

from pathlib import Path

import pytest

from src.refinery.facts import LLMFinancialFactExtractor
from src.refinery.llm_provider import resolve_chat_provider
from src.refinery.strategies.vision import VisionExtractor
from src.refinery.triage import DocumentTriageAgent


def test_resolve_chat_provider_uses_openrouter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("MODEL", "openrouter/auto")

    provider = resolve_chat_provider()

    assert provider is not None
    assert provider.provider == "openrouter"
    assert provider.api_key == "or-key"
    assert provider.model == "openrouter/auto"
    assert provider.endpoint == "https://openrouter.ai/api/v1/chat/completions"


def test_resolve_chat_provider_prefers_openrouter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("MODEL", "openrouter/auto")

    provider = resolve_chat_provider()

    assert provider is not None
    assert provider.provider == "openrouter"
    assert provider.api_key == "or-key"


def test_vision_extractor_uses_openrouter_env_for_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = tmp_path / "report.txt"
    doc.write_text("Total Revenue: 1000\nNet Income: 300\n", encoding="utf-8")
    profile = DocumentTriageAgent(profiles_dir=tmp_path / "profiles").profile_document(doc)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("MODEL", "openrouter/auto")

    extractor = VisionExtractor(gemini_api_key="")
    monkeypatch.setattr(
        extractor,
        "_chat_refine",
        lambda _raw, provider: {
            "confidence_boost": 0.18,
            "quality": provider.provider,
            "detected_metrics_count": 2,
        },
    )

    extracted = extractor.extract(doc, profile)

    assert extracted.metadata["openrouter_enabled"] is True
    assert extracted.metadata["openrouter_status"] == "ok"
    assert extracted.metadata["openrouter_model"] == "openrouter/auto"
    assert extracted.metadata["llm_provider"] == "openrouter"


def test_vision_extractor_prefers_openrouter_before_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = tmp_path / "report.txt"
    doc.write_text("Total Revenue: 1000\nNet Income: 300\n", encoding="utf-8")
    profile = DocumentTriageAgent(profiles_dir=tmp_path / "profiles").profile_document(doc)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("MODEL", "openrouter/auto")

    extractor = VisionExtractor(gemini_api_key="gemini-key")
    monkeypatch.setattr(
        extractor.layout_extractor,
        "extract",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("layout path should not run first")
        ),
    )
    monkeypatch.setattr(
        extractor,
        "_chat_refine",
        lambda _raw, provider: {
            "confidence_boost": 0.16,
            "quality": provider.provider,
            "detected_metrics_count": 2,
        },
    )

    extracted = extractor.extract(doc, profile)

    assert extracted.metadata["vision_entry_mode"] == "provider_first"
    assert extracted.metadata["openrouter_status"] == "ok"
    assert extracted.metadata["gemini_status"] == "not_needed"
    assert extracted.metadata["llm_provider"] == "openrouter"


def test_llm_fact_extractor_returns_openrouter_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("MODEL", "openrouter/auto")

    extractor = LLMFinancialFactExtractor()
    monkeypatch.setattr(
        extractor,
        "_extract_with_chat_provider",
        lambda _snippet, _provider: {
            "total_revenue": 1000.0,
            "net_income": None,
            "ebitda": None,
            "total_assets": None,
            "total_liabilities": None,
        },
    )

    facts, provider = extractor.extract("Total Revenue appears in the statement.")

    assert provider == "openrouter"
    assert facts["total_revenue"] == 1000.0


def test_openrouter_headers_include_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_APP_NAME", "ledger-test")

    provider = resolve_chat_provider()

    assert provider is not None
    assert provider.headers()["X-Title"] == "ledger-test"
