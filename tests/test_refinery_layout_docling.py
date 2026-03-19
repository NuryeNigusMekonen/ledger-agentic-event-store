from __future__ import annotations

from pathlib import Path

from src.refinery.strategies.layout import LayoutExtractor
from src.refinery.triage import DocumentTriageAgent


def _profile_for(doc: Path, tmp_path: Path):
    triage = DocumentTriageAgent(profiles_dir=tmp_path / "profiles")
    return triage.profile_document(doc)


def test_layout_extractor_falls_back_when_docling_unavailable(tmp_path: Path) -> None:
    doc = tmp_path / "report.txt"
    doc.write_text("Section 1\nRevenue details\n", encoding="utf-8")
    profile = _profile_for(doc, tmp_path)

    extractor = LayoutExtractor()
    extractor._extract_with_docling = lambda _path: None  # type: ignore[method-assign]

    extracted = extractor.extract(doc, profile)

    assert extracted.metadata["docling_used"] is False
    assert extracted.metadata["docling_status"] == "fallback_local_only"


def test_layout_extractor_uses_docling_output_when_available(tmp_path: Path) -> None:
    doc = tmp_path / "report.txt"
    doc.write_text("plain source text", encoding="utf-8")
    profile = _profile_for(doc, tmp_path)

    extractor = LayoutExtractor()
    docling_text = "# Executive Summary\n\nTotal Revenue: 3000\n"
    extractor._extract_with_docling = lambda _path: docling_text  # type: ignore[method-assign]

    extracted = extractor.extract(doc, profile)

    assert extracted.raw_text == docling_text
    assert extracted.metadata["docling_used"] is True
    assert extracted.metadata["docling_status"] == "ok"
