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
        section_nodes: list[PageIndexNode] = []
        ordered_chunks = sorted(
            chunks,
            key=lambda chunk: (
                min(chunk.page_refs or [1]),
                self._chunk_type_priority(chunk.chunk_type),
                chunk.chunk_id,
            ),
        )

        current_title: str | None = None
        current_chunks: list[LDU] = []

        def flush_section() -> None:
            if not current_chunks or current_title is None:
                return
            pages = sorted({page for chunk in current_chunks for page in chunk.page_refs})
            body = "\n".join(chunk.content for chunk in current_chunks[:4])
            node_idx = len(section_nodes) + 1
            section_nodes.append(
                PageIndexNode(
                    node_id=f"{extracted.document_id}-section-{node_idx}",
                    title=current_title,
                    page_start=pages[0],
                    page_end=pages[-1],
                    child_sections=[],
                    key_entities=self._extract_entities(body),
                    summary=self._summarize_text(body),
                    data_types_present=sorted({chunk.chunk_type for chunk in current_chunks}),
                )
            )

        for chunk in ordered_chunks:
            title = self._normalize_section_title(chunk)
            if current_title is None:
                current_title = title
                current_chunks = [chunk]
                continue

            current_pages = {
                page
                for section_chunk in current_chunks
                for page in section_chunk.page_refs
            }
            chunk_pages = set(chunk.page_refs)
            page_break = bool(
                chunk_pages and current_pages and min(chunk_pages) > max(current_pages)
            )
            should_split_general = current_title == "General" and page_break
            if title != current_title or should_split_general:
                flush_section()
                current_title = title
                current_chunks = [chunk]
                continue

            current_chunks.append(chunk)

        flush_section()
        return section_nodes

    def _extract_entities(self, text: str) -> list[str]:
        cleaned = re.sub(r"\s+", " ", text).strip()
        entities = re.findall(r"\b[A-Z][A-Za-z]{1,}(?:\s+[A-Z][A-Za-z]{1,})*\b", cleaned)
        deduped: list[str] = []
        seen: set[str] = set()
        for entity in entities:
            normalized = " ".join(entity.split()).strip()
            if len(normalized.split()) > 4:
                continue
            if normalized.lower() in seen:
                continue
            seen.add(normalized.lower())
            deduped.append(normalized)
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

    def _normalize_section_title(self, chunk: LDU) -> str:
        title = (chunk.parent_section or "").strip()
        if title and title.lower() != "document":
            return title

        lines = [line.strip() for line in chunk.content.splitlines() if line.strip()]
        for candidate in lines[:3]:
            normalized = re.sub(r"\s+", " ", candidate).strip(" -|")
            if not normalized or len(normalized) > 80:
                continue
            if normalized.lower().startswith("headers:"):
                return "Table"
            if normalized.isdigit():
                continue
            if re.match(r"^(\d+(\.\d+)*)\s+", normalized):
                return normalized[:120]
            words = normalized.split()
            if len(words) <= 10 and sum(
                1 for word in words if word[:1].isupper()
            ) >= max(1, len(words) // 2):
                return normalized[:120]
        return "General"

    def _chunk_type_priority(self, chunk_type: str) -> int:
        priority = {
            "paragraph": 0,
            "list": 1,
            "table": 2,
            "figure": 3,
            "section": 4,
        }
        return priority.get(chunk_type, 9)


def _token_overlap_score(query: str, text: str) -> float:
    q_tokens = {token.lower() for token in re.findall(r"[a-zA-Z0-9]+", query)}
    t_tokens = {token.lower() for token in re.findall(r"[a-zA-Z0-9]+", text)}
    if not q_tokens or not t_tokens:
        return 0.0
    return len(q_tokens & t_tokens) / len(q_tokens)
