from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from src.refinery.models import LDU


@dataclass(slots=True)
class FactRecord:
    document_id: str
    metric_name: str
    metric_value: float
    raw_value: str
    currency: str
    source_chunk_id: str
    page_number: int


class FinancialFactExtractor:
    METRIC_PATTERNS = {
        "total_revenue": r"(?:total\s+revenue|revenue)\s*[:=-]?\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
        "net_income": r"(?:net\s+income)\s*[:=-]?\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
        "ebitda": r"(?:ebitda)\s*[:=-]?\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
        "total_assets": r"(?:total\s+assets)\s*[:=-]?\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
        "total_liabilities": r"(?:total\s+liabilities)\s*[:=-]?\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
    }

    def extract(self, document_id: str, chunks: list[LDU]) -> list[FactRecord]:
        facts: list[FactRecord] = []
        for chunk in chunks:
            text = chunk.content.lower()
            for metric, pattern in self.METRIC_PATTERNS.items():
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if not match:
                    continue
                raw_value = match.group(1)
                normalized = float(raw_value.replace(",", ""))
                facts.append(
                    FactRecord(
                        document_id=document_id,
                        metric_name=metric,
                        metric_value=normalized,
                        raw_value=raw_value,
                        currency="USD",
                        source_chunk_id=chunk.chunk_id,
                        page_number=chunk.page_refs[0],
                    )
                )
        return facts


class SQLiteFactStore:
    def __init__(self, db_path: str | Path = ".refinery/facts.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fact_table (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  document_id TEXT NOT NULL,
                  metric_name TEXT NOT NULL,
                  metric_value REAL NOT NULL,
                  raw_value TEXT NOT NULL,
                  currency TEXT NOT NULL,
                  source_chunk_id TEXT NOT NULL,
                  page_number INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_fact_doc_metric
                ON fact_table (document_id, metric_name)
                """
            )

    def upsert_facts(self, facts: list[FactRecord]) -> None:
        if not facts:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO fact_table (
                  document_id,
                  metric_name,
                  metric_value,
                  raw_value,
                  currency,
                  source_chunk_id,
                  page_number
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        fact.document_id,
                        fact.metric_name,
                        fact.metric_value,
                        fact.raw_value,
                        fact.currency,
                        fact.source_chunk_id,
                        fact.page_number,
                    )
                    for fact in facts
                ],
            )

    def query(self, sql: str) -> list[dict[str, str | int | float]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql).fetchall()
        return [dict(row) for row in rows]
