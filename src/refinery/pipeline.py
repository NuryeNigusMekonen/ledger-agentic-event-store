from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.refinery.chunker import ChunkingEngine
from src.refinery.facts import (
    FactRecord,
    FinancialFactExtractor,
    LLMFinancialFactExtractor,
    SQLiteFactStore,
)
from src.refinery.indexer import PageIndexBuilder
from src.refinery.models import LDU, DocumentProfile, ExtractedDocument, PageIndex
from src.refinery.query_agent import DocumentQueryAgent
from src.refinery.router import ExtractionRouter
from src.refinery.triage import DocumentTriageAgent


@dataclass(slots=True)
class PipelineResult:
    profile: DocumentProfile
    extracted: ExtractedDocument
    chunks: list[LDU]
    page_index: PageIndex
    facts_count: int


class DocumentRefineryPipeline:
    def __init__(
        self,
        *,
        rules_path: str | Path = "rubric/extraction_rules.yaml",
        sqlite_db_path: str | Path = ".refinery/facts.db",
        profiles_dir: str | Path = ".refinery/profiles",
        pageindex_dir: str | Path = ".refinery/pageindex",
        ledger_path: str | Path = ".refinery/extraction_ledger.jsonl",
        gemini_api_key: str | None = None,
        gemini_model: str | None = None,
        openai_api_key: str | None = None,
        openai_model: str | None = None,
    ) -> None:
        self.triage = DocumentTriageAgent(profiles_dir=profiles_dir)
        self.router = ExtractionRouter(
            rules_path=rules_path,
            ledger_path=ledger_path,
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
        )
        self.chunker = ChunkingEngine()
        self.indexer = PageIndexBuilder(pageindex_dir=pageindex_dir)
        self.fact_extractor = FinancialFactExtractor()
        self.llm_fact_extractor = LLMFinancialFactExtractor(
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
        )
        self.fact_store = SQLiteFactStore(db_path=sqlite_db_path)

    def run(self, document_path: str | Path) -> PipelineResult:
        profile = self.triage.profile_document(document_path)
        routed = self.router.extract(document_path, profile)
        extracted = routed.extracted

        chunks = self.chunker.chunk_document(extracted)
        page_index = self.indexer.build(extracted, chunks)

        facts = self.fact_extractor.extract(profile.document_id, chunks)
        facts = self._augment_with_llm_facts(
            document_id=profile.document_id,
            chunks=chunks,
            raw_text=extracted.raw_text,
            facts=facts,
        )
        self.fact_store.upsert_facts(facts)

        return PipelineResult(
            profile=profile,
            extracted=extracted,
            chunks=chunks,
            page_index=page_index,
            facts_count=len(facts),
        )

    def build_query_agent(self, page_index: PageIndex, chunks: list[LDU]) -> DocumentQueryAgent:
        return DocumentQueryAgent(
            page_index=page_index,
            chunks=chunks,
            fact_store=self.fact_store,
        )

    def _augment_with_llm_facts(
        self,
        *,
        document_id: str,
        chunks: list[LDU],
        raw_text: str,
        facts: list[FactRecord],
    ) -> list[FactRecord]:
        existing_metrics = {fact.metric_name for fact in facts}
        missing_metrics = [
            metric
            for metric in FinancialFactExtractor.METRIC_PATTERNS
            if metric not in existing_metrics
        ]
        if not missing_metrics:
            return facts

        llm_metrics, provider = self.llm_fact_extractor.extract(raw_text)
        if provider is None:
            return facts

        default_chunk_id = chunks[0].chunk_id if chunks else f"{document_id}-llm"
        default_page = chunks[0].page_refs[0] if chunks and chunks[0].page_refs else 1

        for metric_name in missing_metrics:
            metric_value = llm_metrics.get(metric_name)
            if metric_value is None:
                continue
            facts.append(
                FactRecord(
                    document_id=document_id,
                    metric_name=metric_name,
                    metric_value=float(metric_value),
                    raw_value=str(metric_value),
                    currency="USD",
                    source_chunk_id=f"llm_{provider}:{default_chunk_id}",
                    page_number=default_page,
                )
            )
        return facts


def extract_financial_facts(
    document_path: str | Path,
    *,
    rules_path: str | Path = "rubric/extraction_rules.yaml",
    sqlite_db_path: str | Path = ".refinery/facts.db",
    profiles_dir: str | Path = ".refinery/profiles",
    pageindex_dir: str | Path = ".refinery/pageindex",
    ledger_path: str | Path = ".refinery/extraction_ledger.jsonl",
    gemini_api_key: str | None = None,
    gemini_model: str | None = None,
    openai_api_key: str | None = None,
    openai_model: str | None = None,
) -> dict[str, float | None]:
    """Week 3 integration entry point for downstream Week 5 document processing.

    Returns high-value financial facts when present. Missing values remain None.
    """
    pipeline = DocumentRefineryPipeline(
        rules_path=rules_path,
        sqlite_db_path=sqlite_db_path,
        profiles_dir=profiles_dir,
        pageindex_dir=pageindex_dir,
        ledger_path=ledger_path,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
    )
    result = pipeline.run(document_path)

    rows = pipeline.fact_store.query(
        "SELECT metric_name, metric_value FROM fact_table "
        f"WHERE document_id='{result.profile.document_id}'"
    )
    lookup = {str(row["metric_name"]): float(row["metric_value"]) for row in rows}

    return {
        "total_revenue": lookup.get("total_revenue"),
        "net_income": lookup.get("net_income"),
        "ebitda": lookup.get("ebitda"),
        "total_assets": lookup.get("total_assets"),
        "total_liabilities": lookup.get("total_liabilities"),
    }
