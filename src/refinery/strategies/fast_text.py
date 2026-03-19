from __future__ import annotations

import csv
import re
from pathlib import Path

from src.refinery.models import (
    BoundingBox,
    DocumentProfile,
    ExtractedDocument,
    ExtractedTable,
    TextBlock,
)


class FastTextExtractor:
    name = "fast_text"

    def extract(self, document_path: Path, profile: DocumentProfile) -> ExtractedDocument:
        suffix = document_path.suffix.lower()
        text_blocks: list[TextBlock] = []
        tables: list[ExtractedTable] = []
        raw_text = ""

        if suffix == ".csv":
            with document_path.open("r", encoding="utf-8", errors="ignore") as handle:
                rows = list(csv.reader(handle))
            if rows:
                headers = rows[0]
                values = rows[1:]
                table = ExtractedTable(page_number=1, headers=headers, rows=values)
                tables.append(table)
                raw_text = "\n".join(",".join(row) for row in rows)
        elif suffix in {".txt", ".md", ".json"}:
            raw_text = document_path.read_text(encoding="utf-8", errors="ignore")
        elif suffix == ".pdf":
            raw_text, pdf_tables, text_blocks = self._extract_pdf(document_path)
            tables.extend(pdf_tables)
        else:
            raw_text = document_path.read_text(encoding="utf-8", errors="ignore")

        if not text_blocks:
            text_blocks = self._text_to_blocks(raw_text)

        if not tables:
            tables = self._extract_inline_tables(raw_text)

        confidence = self._confidence(profile=profile, raw_text=raw_text, tables=tables)
        metadata = {
            "chars": len(raw_text),
            "text_blocks": len(text_blocks),
            "tables": len(tables),
        }

        return ExtractedDocument(
            document_id=profile.document_id,
            document_name=profile.document_name,
            source_path=str(document_path),
            strategy_used=self.name,
            confidence_score=confidence,
            estimated_cost_usd=0.001,
            text_blocks=text_blocks,
            tables=tables,
            raw_text=raw_text,
            metadata=metadata,
        )

    def _extract_pdf(
        self,
        document_path: Path,
    ) -> tuple[str, list[ExtractedTable], list[TextBlock]]:
        try:
            import pdfplumber  # type: ignore
        except Exception:
            fallback = document_path.read_bytes()[:50000].decode("latin1", errors="ignore")
            return fallback, [], self._text_to_blocks(fallback)

        text_parts: list[str] = []
        tables: list[ExtractedTable] = []
        blocks: list[TextBlock] = []
        with pdfplumber.open(str(document_path)) as pdf:
            for idx, page in enumerate(pdf.pages, start=1):
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
                if page_text.strip():
                    blocks.append(TextBlock(page_number=idx, text=page_text))

                try:
                    extracted_tables = page.extract_tables() or []
                except Exception:
                    extracted_tables = []
                for table in extracted_tables:
                    if not table:
                        continue
                    header_row = [str(cell or "").strip() for cell in table[0]]
                    body = [[str(cell or "").strip() for cell in row] for row in table[1:]]
                    tables.append(
                        ExtractedTable(
                            page_number=idx,
                            headers=header_row,
                            rows=body,
                            bbox=BoundingBox(
                                x0=0.0,
                                y0=0.0,
                                x1=float(page.width),
                                y1=float(page.height),
                            ),
                        )
                    )

        return "\n".join(text_parts), tables, blocks

    def _text_to_blocks(self, raw_text: str) -> list[TextBlock]:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw_text) if p.strip()]
        return [TextBlock(page_number=1, text=paragraph) for paragraph in paragraphs]

    def _extract_inline_tables(self, raw_text: str) -> list[ExtractedTable]:
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        csv_like = [line for line in lines if line.count(",") >= 2]
        if len(csv_like) < 2:
            return []
        parsed = [line.split(",") for line in csv_like]
        return [ExtractedTable(page_number=1, headers=parsed[0], rows=parsed[1:])]

    def _confidence(
        self,
        *,
        profile: DocumentProfile,
        raw_text: str,
        tables: list[ExtractedTable],
    ) -> float:
        score = 0.35
        if len(raw_text.strip()) > 300:
            score += 0.25
        if profile.char_density > 120:
            score += 0.15
        if profile.image_area_ratio < 0.25:
            score += 0.15
        if tables:
            score += 0.1
        return min(1.0, max(0.0, score))
