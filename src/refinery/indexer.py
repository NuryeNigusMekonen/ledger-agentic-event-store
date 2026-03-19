from __future__ import annotations

import re
from pathlib import Path

from src.refinery.models import LDU, ExtractedDocument, PageIndex, PageIndexNode


class PageIndexBuilder:
    def __init__(self, pageindex_dir: str | Path = ".refinery/pageindex") -> None:
        self.pageindex_dir = Path(pageindex_dir)
        self.pageindex_dir.mkdir(parents=True, exist_ok=True)

    def build(self, extracted: ExtractedDocument, chunks: list[LDU]) -> PageIndex:
        page_numbers = [*{page for chunk in chunks for page in chunk.page_refs}] or [1]
        min_page = min(page_numbers)
        max_page = max(page_numbers)

        section_nodes = self._build_section_nodes(extracted, chunks)
        root = PageIndexNode(
            node_id=f"{extracted.document_id}-root",
            title=extracted.document_name,
            page_start=min_page,
            page_end=max_page,
            child_sections=section_nodes,
            key_entities=self._extract_entities(extracted.raw_text),
            summary=self._summarize_text(extracted.raw_text),
            data_types_present=self._data_types(extracted, chunks),
        )
        index = PageIndex(document_id=extracted.document_id, root=root)
        self._persist(index)
        return index

    def navigate(self, index: PageIndex, topic: str, top_k: int = 3) -> list[PageIndexNode]:
        scored: list[tuple[float, PageIndexNode]] = []
        for node in index.root.child_sections:
            score = _token_overlap_score(topic, " ".join([node.title, node.summary]))
            scored.append((score, node))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [node for score, node in scored[:top_k] if score > 0]

    def _build_section_nodes(
        self,
        extracted: ExtractedDocument,
        chunks: list[LDU],
    ) -> list[PageIndexNode]:
        by_title: dict[str, list[LDU]] = {}
        for chunk in chunks:
            title = chunk.parent_section or "General"
            by_title.setdefault(title, []).append(chunk)

        section_nodes: list[PageIndexNode] = []
        for idx, (title, section_chunks) in enumerate(by_title.items(), start=1):
            pages = sorted({page for chunk in section_chunks for page in chunk.page_refs})
            body = "\n".join(chunk.content for chunk in section_chunks[:4])
            section_nodes.append(
                PageIndexNode(
                    node_id=f"{extracted.document_id}-section-{idx}",
                    title=title,
                    page_start=pages[0],
                    page_end=pages[-1],
                    child_sections=[],
                    key_entities=self._extract_entities(body),
                    summary=self._summarize_text(body),
                    data_types_present=sorted({chunk.chunk_type for chunk in section_chunks}),
                )
            )
        return section_nodes

    def _extract_entities(self, text: str) -> list[str]:
        entities = re.findall(r"\b[A-Z][A-Za-z]{2,}(?:\s+[A-Z][A-Za-z]{2,})*\b", text)
        deduped: list[str] = []
        seen: set[str] = set()
        for entity in entities:
            if entity.lower() in seen:
                continue
            seen.add(entity.lower())
            deduped.append(entity)
            if len(deduped) >= 8:
                break
        return deduped

    def _summarize_text(self, text: str) -> str:
        cleaned = " ".join(text.split())
        if not cleaned:
            return "No summary available."
        pieces = re.split(r"(?<=[.!?])\s+", cleaned)
        return " ".join(pieces[:2])[:320]

    def _data_types(self, extracted: ExtractedDocument, chunks: list[LDU]) -> list[str]:
        types = {chunk.chunk_type for chunk in chunks}
        if extracted.tables:
            types.add("table")
        if extracted.figures:
            types.add("figure")
        if extracted.text_blocks:
            types.add("paragraph")
        return sorted(types)

    def _persist(self, index: PageIndex) -> None:
        out = self.pageindex_dir / f"{index.document_id}.json"
        out.write_text(index.model_dump_json(indent=2), encoding="utf-8")


def _token_overlap_score(query: str, text: str) -> float:
    q_tokens = {token.lower() for token in re.findall(r"[a-zA-Z0-9]+", query)}
    t_tokens = {token.lower() for token in re.findall(r"[a-zA-Z0-9]+", text)}
    if not q_tokens or not t_tokens:
        return 0.0
    return len(q_tokens & t_tokens) / len(q_tokens)
