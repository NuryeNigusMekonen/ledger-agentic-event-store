from __future__ import annotations

from pathlib import Path

import pytest

from src.refinery.models import DocumentProfile, ExtractedDocument, TextBlock
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


def test_layout_extractor_preserves_fast_text_page_numbers_when_docling_is_single_stream(
    tmp_path: Path,
) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_text("placeholder", encoding="utf-8")
    profile = DocumentProfile(
        document_id="doc-pages-001",
        document_name=doc.name,
        source_path=str(doc),
        origin_type="native_digital",
        layout_complexity="multi_column",
        language="en",
        language_confidence=0.9,
        domain_hint="financial",
        estimated_extraction_cost="needs_layout_model",
        page_count=2,
        char_density=500.0,
        image_area_ratio=0.1,
    )

    extractor = LayoutExtractor()
    extractor.fast_text.extract = lambda _path, _profile: ExtractedDocument(  # type: ignore[method-assign]
        document_id=profile.document_id,
        document_name=profile.document_name,
        source_path=str(doc),
        strategy_used="fast_text",
        confidence_score=0.8,
        text_blocks=[
            TextBlock(page_number=1, text="Page one heading\nbody"),
            TextBlock(page_number=2, text="Page two heading\nbody"),
        ],
        raw_text="Page one heading\nbody\n\f\nPage two heading\nbody",
    )
    extractor._extract_with_docling = lambda _path: "# Heading\n\nUnified docling text\n"  # type: ignore[method-assign]

    extracted = extractor.extract(doc, profile)

    assert [block.page_number for block in extracted.text_blocks] == [1, 2]
    assert extracted.metadata["docling_paging_strategy"] == "preserve_fast_text_pages"


def test_layout_extractor_lowers_confidence_for_mixed_image_heavy_docs_without_docling(
    tmp_path: Path,
) -> None:
    doc = tmp_path / "report.txt"
    doc.write_text("Total Revenue: 1000\nNet Income: 300\n", encoding="utf-8")

    profile = DocumentProfile(
        document_id="doc-mixed-001",
        document_name=doc.name,
        source_path=str(doc),
        origin_type="mixed",
        layout_complexity="single_column",
        language="en",
        language_confidence=0.9,
        domain_hint="general",
        estimated_extraction_cost="needs_layout_model",
        page_count=1,
        char_density=20000.0,
        image_area_ratio=0.8,
    )

    extractor = LayoutExtractor()
    extractor._extract_with_docling = lambda _path: None  # type: ignore[method-assign]

    extracted = extractor.extract(doc, profile)

    assert extracted.metadata["layout_confidence_penalty"] == pytest.approx(0.2)
    assert extracted.confidence_score < 0.88


def test_layout_extractor_lowers_confidence_for_large_multi_column_reports(
    tmp_path: Path,
) -> None:
    doc = tmp_path / "deck.txt"
    doc.write_text("Executive Summary\nRevenue up strongly\n", encoding="utf-8")

    profile = DocumentProfile(
        document_id="doc-deck-001",
        document_name=doc.name,
        source_path=str(doc),
        origin_type="native_digital",
        layout_complexity="multi_column",
        language="en",
        language_confidence=0.9,
        domain_hint="financial",
        estimated_extraction_cost="needs_layout_model",
        page_count=67,
        char_density=2660.0,
        image_area_ratio=0.12,
    )

    extractor = LayoutExtractor()
    extractor._extract_with_docling = lambda _path: "# Slide 1\n\nRevenue up strongly\n"  # type: ignore[method-assign]

    extracted = extractor.extract(doc, profile)

    assert extracted.metadata["layout_confidence_penalty"] == pytest.approx(0.35)
    assert extracted.metadata["layout_confidence_cap"] == pytest.approx(0.84)
    assert extracted.confidence_score < 0.88
