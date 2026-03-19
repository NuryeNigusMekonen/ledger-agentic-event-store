from __future__ import annotations

from pathlib import Path

import pytest

from src.refinery.strategies.layout import LayoutExtractor
from src.refinery.strategies.vision import VisionExtractor
from src.refinery.triage import DocumentTriageAgent


def _profile_for(doc: Path, tmp_path: Path):
    triage = DocumentTriageAgent(profiles_dir=tmp_path / "profiles")
    return triage.profile_document(doc)


def test_vision_extractor_uses_local_fallback_without_gemini_key(tmp_path: Path) -> None:
    doc = tmp_path / "report.txt"
    doc.write_text("Total Revenue: 1000\nNet Income: 300\n", encoding="utf-8")
    profile = _profile_for(doc, tmp_path)

    extractor = VisionExtractor(gemini_api_key="")
    extracted = extractor.extract(doc, profile)

    assert extracted.strategy_used == "vision_augmented"
    assert extracted.metadata["gemini_enabled"] is False
    assert extracted.metadata["gemini_status"] == "disabled_missing_api_key"


def test_vision_extractor_applies_gemini_insight_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = tmp_path / "report.txt"
    doc.write_text("Total Revenue: 1000\nNet Income: 300\n", encoding="utf-8")
    profile = _profile_for(doc, tmp_path)

    baseline = LayoutExtractor().extract(doc, profile)
    extractor = VisionExtractor(gemini_api_key="test-key", gemini_model="gemini-test")
    monkeypatch.setattr(
        extractor,
        "_gemini_refine",
        lambda _raw: {
            "confidence_boost": 0.22,
            "quality": "high",
            "detected_metrics_count": 2,
        },
    )

    extracted = extractor.extract(doc, profile)

    assert extracted.metadata["gemini_enabled"] is True
    assert extracted.metadata["gemini_status"] == "ok"
    assert extracted.metadata["gemini_model"] == "gemini-test"
    assert extracted.metadata["gemini_quality"] == "high"
    assert extracted.metadata["gemini_detected_metrics"] == 2
    assert extracted.confidence_score == pytest.approx(min(1.0, baseline.confidence_score + 0.22))
