from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from src.refinery.models import LDU, BoundingBox, ExtractedDocument


@dataclass(slots=True)
class ChunkValidator:
    max_tokens: int = 450

    def validate(self, chunks: list[LDU]) -> None:
        for chunk in chunks:
            if chunk.token_count > self.max_tokens and chunk.chunk_type in {"table", "list"}:
                raise ValueError(
                    "Chunk "
                    f"{chunk.chunk_id} violates constitution: "
                    f"{chunk.chunk_type} over max_tokens"
                )
            if chunk.chunk_type == "table" and "headers:" not in chunk.content.lower():
                raise ValueError(f"Chunk {chunk.chunk_id} table content missing header context")


class ChunkingEngine:
    def __init__(self, *, max_tokens: int = 450) -> None:
        self.max_tokens = max_tokens
        self.validator = ChunkValidator(max_tokens=max_tokens)

    def chunk_document(self, extracted: ExtractedDocument) -> list[LDU]:
        chunks: list[LDU] = []
        table_reference_chunks: list[LDU] = []

        for idx, table in enumerate(extracted.tables, start=1):
            table_chunks = self._chunk_table(
                document_id=extracted.document_id,
                table_idx=idx,
                headers=table.headers,
                rows=table.rows,
                page_number=table.page_number,
                bbox=table.bbox,
            )
            if table_chunks:
                table_reference_chunks.append(table_chunks[0])
            chunks.extend(table_chunks)

        for idx, figure in enumerate(extracted.figures, start=1):
            chunks.append(
                self._build_chunk(
                    chunk_id=f"{extracted.document_id}-figure-{idx}",
                    content=figure.caption,
                    chunk_type="figure",
                    page_refs=[figure.page_number],
                    bbox=figure.bbox,
                    parent_section="Figure",
                )
            )

        for block_idx, block in enumerate(extracted.text_blocks, start=1):
            block_chunks = self._chunk_text_block(
                extracted.document_id,
                block_idx,
                block.text,
                block.page_number,
                block.section_title,
                block.bbox,
            )
            chunks.extend(block_chunks)

        self._resolve_cross_references(chunks, table_reference_chunks)
        self.validator.validate(chunks)
        return chunks

    def _chunk_text_block(
        self,
        document_id: str,
        block_idx: int,
        text: str,
        page_number: int,
        section_title: str | None,
        bbox: BoundingBox,
    ) -> list[LDU]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        numbered_list = all(re.match(r"^\d+[\).]", line) for line in lines) and len(lines) > 1

        if numbered_list:
            content = "\n".join(lines)
            chunk = self._build_chunk(
                chunk_id=f"{document_id}-list-{block_idx}",
                content=content,
                chunk_type="list",
                page_refs=[page_number],
                bbox=bbox,
                parent_section=section_title,
            )
            if chunk.token_count <= self.max_tokens:
                return [chunk]

        return self._split_into_paragraph_chunks(
            document_id=document_id,
            block_idx=block_idx,
            text=text,
            page_number=page_number,
            section_title=section_title,
            bbox=bbox,
        )

    def _split_into_paragraph_chunks(
        self,
        *,
        document_id: str,
        block_idx: int,
        text: str,
        page_number: int,
        section_title: str | None,
        bbox: BoundingBox,
    ) -> list[LDU]:
        words = text.split()
        if len(words) <= self.max_tokens:
            return [
                self._build_chunk(
                    chunk_id=f"{document_id}-paragraph-{block_idx}-1",
                    content=text,
                    chunk_type="paragraph",
                    page_refs=[page_number],
                    bbox=bbox,
                    parent_section=section_title,
                )
            ]

        chunks: list[LDU] = []
        for i in range(0, len(words), self.max_tokens):
            part = " ".join(words[i : i + self.max_tokens])
            chunks.append(
                self._build_chunk(
                    chunk_id=f"{document_id}-paragraph-{block_idx}-{(i // self.max_tokens) + 1}",
                    content=part,
                    chunk_type="paragraph",
                    page_refs=[page_number],
                    bbox=bbox,
                    parent_section=section_title,
                )
            )
        return chunks

    def _resolve_cross_references(
        self,
        chunks: list[LDU],
        table_reference_chunks: list[LDU] | None = None,
    ) -> None:
        table_chunks = table_reference_chunks or [
            chunk for chunk in chunks if chunk.chunk_type == "table"
        ]
        if not table_chunks:
            return

        for idx, chunk in enumerate(chunks):
            refs = re.findall(r"table\s+(\d+)", chunk.content, flags=re.IGNORECASE)
            relationships = list(chunk.relationships)
            for ref in refs:
                table_index = int(ref) - 1
                if 0 <= table_index < len(table_chunks):
                    relationships.append(table_chunks[table_index].chunk_id)
            chunks[idx] = chunk.model_copy(update={"relationships": sorted(set(relationships))})

    def _table_to_text(self, headers: list[str], rows: list[list[str]]) -> str:
        lines = ["headers: " + " | ".join(headers)]
        for row in rows:
            lines.append("row: " + " | ".join(row))
        return "\n".join(lines)

    def _chunk_table(
        self,
        *,
        document_id: str,
        table_idx: int,
        headers: list[str],
        rows: list[list[str]],
        page_number: int,
        bbox: BoundingBox,
    ) -> list[LDU]:
        header_line = "headers: " + " | ".join(headers)
        header_tokens = len(header_line.split())
        if header_tokens >= self.max_tokens:
            header_line = (
                "headers: "
                + " ".join(header_line.split()[: max(self.max_tokens - 4, 1)])
                + " ..."
            )
            header_tokens = len(header_line.split())

        row_lines: list[str] = []
        for row in rows:
            row_text = "row: " + " | ".join(row)
            if len(row_text.split()) + header_tokens <= self.max_tokens:
                row_lines.append(row_text)
                continue

            allowed_tokens = max(self.max_tokens - header_tokens - 2, 1)
            words = row_text.split()
            for idx in range(0, len(words), allowed_tokens):
                suffix = " row_cont:" if idx > 0 else ""
                row_lines.append(f"{suffix} {' '.join(words[idx: idx + allowed_tokens])}".strip())

        if not row_lines:
            row_lines = ["row: "]

        chunks: list[LDU] = []
        current_lines = [header_line]
        current_tokens = header_tokens
        part = 1

        for row_line in row_lines:
            row_tokens = len(row_line.split())
            if current_tokens + row_tokens > self.max_tokens and len(current_lines) > 1:
                chunks.append(
                    self._build_chunk(
                        chunk_id=f"{document_id}-table-{table_idx}-{part}",
                        content="\n".join(current_lines),
                        chunk_type="table",
                        page_refs=[page_number],
                        bbox=bbox,
                        parent_section=f"Table {table_idx}",
                    )
                )
                part += 1
                current_lines = [header_line]
                current_tokens = header_tokens

            current_lines.append(row_line)
            current_tokens += row_tokens

        if current_lines:
            chunks.append(
                self._build_chunk(
                    chunk_id=f"{document_id}-table-{table_idx}-{part}",
                    content="\n".join(current_lines),
                    chunk_type="table",
                    page_refs=[page_number],
                    bbox=bbox,
                    parent_section=f"Table {table_idx}",
                )
            )

        return chunks

    def _build_chunk(
        self,
        *,
        chunk_id: str,
        content: str,
        chunk_type: str,
        page_refs: list[int],
        bbox: BoundingBox,
        parent_section: str | None,
    ) -> LDU:
        token_count = len(content.split())
        digest = hashlib.sha256(
            f"{chunk_id}|{chunk_type}|{parent_section}|{content}".encode()
        ).hexdigest()
        return LDU(
            chunk_id=chunk_id,
            content=content,
            chunk_type=chunk_type,
            page_refs=page_refs,
            bounding_box=bbox,
            parent_section=parent_section,
            token_count=token_count,
            content_hash=digest,
        )
