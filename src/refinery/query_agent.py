from __future__ import annotations

import re
from dataclasses import dataclass

from src.refinery.facts import SQLiteFactStore
from src.refinery.indexer import PageIndexBuilder
from src.refinery.models import LDU, PageIndex, ProvenanceChain, ProvenanceCitation


@dataclass(slots=True)
class QueryAnswer:
    answer: str
    provenance: ProvenanceChain
    supporting_chunks: list[str]


class DocumentQueryAgent:
    def __init__(
        self,
        *,
        page_index: PageIndex,
        chunks: list[LDU],
        fact_store: SQLiteFactStore,
    ) -> None:
        self.page_index = page_index
        self.chunks = chunks
        self.fact_store = fact_store
        self.index_builder = PageIndexBuilder()

    def pageindex_navigate(self, topic: str, top_k: int = 3):
        return self.index_builder.navigate(self.page_index, topic, top_k=top_k)

    def semantic_search(self, query: str, top_k: int = 5) -> list[LDU]:
        scored = [(_score(query, chunk.content), chunk) for chunk in self.chunks]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [chunk for score, chunk in scored[:top_k] if score > 0]

    def structured_query(self, sql: str):
        if not sql.strip().lower().startswith("select"):
            raise ValueError("Only SELECT is allowed for structured_query")
        return self.fact_store.query(sql)

    def ask(self, question: str) -> QueryAnswer:
        sections = self.pageindex_navigate(question, top_k=2)
        section_titles = {section.title.lower() for section in sections}

        scoped_chunks = [
            chunk
            for chunk in self.chunks
            if (chunk.parent_section or "").lower() in section_titles
        ]
        if not scoped_chunks:
            scoped_chunks = self.semantic_search(question, top_k=4)
        else:
            scoped_chunks = sorted(
                scoped_chunks,
                key=lambda chunk: _score(question, chunk.content),
                reverse=True,
            )[:4]

        answer_text = self._compose_answer(question, scoped_chunks)
        provenance = ProvenanceChain(
            citations=[
                ProvenanceCitation(
                    document_name=self.page_index.root.title,
                    page_number=chunk.page_refs[0],
                    bbox=chunk.bounding_box,
                    content_hash=chunk.content_hash,
                )
                for chunk in scoped_chunks[:3]
            ]
        )

        return QueryAnswer(
            answer=answer_text,
            provenance=provenance,
            supporting_chunks=[chunk.chunk_id for chunk in scoped_chunks],
        )

    def _compose_answer(self, question: str, chunks: list[LDU]) -> str:
        if not chunks:
            return "No supporting evidence found in the document."

        question_lower = question.lower()
        if "revenue" in question_lower:
            rows = self.structured_query(
                "SELECT metric_name, metric_value FROM fact_table "
                "WHERE metric_name='total_revenue' ORDER BY id DESC LIMIT 1"
            )
            if rows:
                return f"Latest extracted total revenue is {rows[0]['metric_value']:.2f} USD."

        sample = " ".join(chunk.content for chunk in chunks[:2])
        sentences = re.split(r"(?<=[.!?])\s+", sample)
        return " ".join(sentences[:2]).strip() or chunks[0].content[:280]


def _score(query: str, text: str) -> float:
    q = {token.lower() for token in re.findall(r"[a-zA-Z0-9]+", query)}
    t = {token.lower() for token in re.findall(r"[a-zA-Z0-9]+", text)}
    if not q or not t:
        return 0.0
    overlap = len(q & t)
    return overlap / len(q)
