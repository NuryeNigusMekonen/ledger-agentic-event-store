from __future__ import annotations

from pathlib import Path

from src.refinery.triage import DocumentTriageAgent


def test_triage_financial_document_detects_domain_and_cost(tmp_path: Path) -> None:
    doc = tmp_path / "financial_report.txt"
    doc.write_text(
        "Total Revenue: 1200000\nNet Income: 210000\nTotal Assets: 4500000\n",
        encoding="utf-8",
    )

    triage = DocumentTriageAgent(profiles_dir=tmp_path / "profiles")
    profile = triage.profile_document(doc)

    assert profile.domain_hint == "financial"
    assert profile.origin_type == "native_digital"
    assert profile.estimated_extraction_cost == "fast_text_sufficient"
    assert (tmp_path / "profiles" / f"{profile.document_id}.json").exists()
