from __future__ import annotations

from pathlib import Path

from src.refinery.pipeline import DocumentRefineryPipeline, extract_financial_facts


def test_pipeline_extracts_financial_facts_and_builds_index(tmp_path: Path) -> None:
    doc = tmp_path / "loan_report.txt"
    doc.write_text(
        """
        1 Executive Summary
        Total Revenue: 1250000
        Net Income: 210000
        EBITDA: 320000
        Total Assets: 4500000
        Total Liabilities: 1700000
        """,
        encoding="utf-8",
    )

    pipeline = DocumentRefineryPipeline(
        rules_path=tmp_path / "rules.yaml",
        sqlite_db_path=tmp_path / "facts.db",
        profiles_dir=tmp_path / "profiles",
        pageindex_dir=tmp_path / "pageindex",
        ledger_path=tmp_path / "ledger.jsonl",
    )
    result = pipeline.run(doc)

    assert result.extracted.confidence_score >= 0.0
    assert len(result.chunks) > 0
    assert result.page_index.root.title == doc.name
    assert result.facts_count >= 3

    facts = extract_financial_facts(
        doc,
        rules_path=tmp_path / "rules.yaml",
        sqlite_db_path=tmp_path / "facts2.db",
        profiles_dir=tmp_path / "profiles2",
        pageindex_dir=tmp_path / "pageindex2",
        ledger_path=tmp_path / "ledger2.jsonl",
    )
    assert facts["total_revenue"] == 1250000.0
    assert facts["net_income"] == 210000.0
