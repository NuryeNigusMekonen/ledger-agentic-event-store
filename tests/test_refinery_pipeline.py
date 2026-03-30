from __future__ import annotations

from pathlib import Path

import src.refinery.pipeline as pipeline_module
from src.refinery.pipeline import (
    DocumentRefineryPipeline,
    extract_financial_evidence,
    extract_financial_facts,
)


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

    evidence = extract_financial_evidence(
        doc,
        rules_path=tmp_path / "rules.yaml",
        sqlite_db_path=tmp_path / "facts3.db",
        profiles_dir=tmp_path / "profiles3",
        pageindex_dir=tmp_path / "pageindex3",
        ledger_path=tmp_path / "ledger3.jsonl",
    )
    revenue_provenance = evidence.fact_provenance["total_revenue"]
    assert revenue_provenance["page_number"] == 1
    assert str(revenue_provenance["source_chunk_id"])
    assert "Total Revenue" in str(revenue_provenance["source_excerpt"])
    assert evidence.extraction_context["page_count"] == 1
    assert evidence.extraction_context["domain_hint"] == "financial"


def test_pipeline_llm_fallback_fills_missing_metrics(tmp_path: Path, monkeypatch) -> None:
    doc = tmp_path / "narrative_report.txt"
    doc.write_text(
        """
        Management discussion:
        The company delivered strong performance year over year.
        """,
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline_module.LLMFinancialFactExtractor,
        "extract",
        lambda self, _raw_text: (
            {
                "total_revenue": 910000.0,
                "net_income": 125000.0,
                "ebitda": 180000.0,
                "total_assets": 2500000.0,
                "total_liabilities": 900000.0,
            },
            "openai",
        ),
    )

    pipeline = DocumentRefineryPipeline(
        rules_path=tmp_path / "rules.yaml",
        sqlite_db_path=tmp_path / "facts.db",
        profiles_dir=tmp_path / "profiles",
        pageindex_dir=tmp_path / "pageindex",
        ledger_path=tmp_path / "ledger.jsonl",
    )
    result = pipeline.run(doc)

    assert result.facts_count == 5
    rows = pipeline.fact_store.query(
        "SELECT metric_name, metric_value, source_chunk_id "
        f"FROM fact_table WHERE document_id='{result.profile.document_id}'"
    )
    by_metric = {str(row["metric_name"]): float(row["metric_value"]) for row in rows}
    assert by_metric["total_revenue"] == 910000.0
    assert by_metric["net_income"] == 125000.0
    assert all(str(row["source_chunk_id"]).startswith("llm_openai:") for row in rows)


def test_pipeline_llm_fallback_does_not_override_regex_metrics(tmp_path: Path, monkeypatch) -> None:
    doc = tmp_path / "partial_financials.txt"
    doc.write_text(
        """
        Total Revenue: 1000
        Commentary section without exact net income label.
        """,
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline_module.LLMFinancialFactExtractor,
        "extract",
        lambda self, _raw_text: (
            {
                "total_revenue": 999999.0,
                "net_income": 200.0,
                "ebitda": None,
                "total_assets": None,
                "total_liabilities": None,
            },
            "openai",
        ),
    )

    pipeline = DocumentRefineryPipeline(
        rules_path=tmp_path / "rules.yaml",
        sqlite_db_path=tmp_path / "facts.db",
        profiles_dir=tmp_path / "profiles",
        pageindex_dir=tmp_path / "pageindex",
        ledger_path=tmp_path / "ledger.jsonl",
    )
    result = pipeline.run(doc)
    assert result.facts_count == 2

    rows = pipeline.fact_store.query(
        "SELECT metric_name, metric_value, source_chunk_id "
        f"FROM fact_table WHERE document_id='{result.profile.document_id}'"
    )
    by_metric = {str(row["metric_name"]): row for row in rows}
    assert float(by_metric["total_revenue"]["metric_value"]) == 1000.0
    assert not str(by_metric["total_revenue"]["source_chunk_id"]).startswith("llm_openai:")
    assert float(by_metric["net_income"]["metric_value"]) == 200.0
