from __future__ import annotations

import re
from pathlib import Path

from src.refinery.models import DocumentProfile, ExtractedDocument, TextBlock
from src.refinery.strategies.fast_text import FastTextExtractor


class LayoutExtractor:
    name = "layout_aware"

    def __init__(self) -> None:
        self.fast_text = FastTextExtractor()

    def extract(self, document_path: Path, profile: DocumentProfile) -> ExtractedDocument:
        base = self.fast_text.extract(document_path, profile)
        docling_text = self._extract_with_docling(document_path)
        docling_used = bool(docling_text)

        if docling_text:
            base = base.model_copy(
                update={
                    "raw_text": docling_text,
                    "text_blocks": self._text_to_blocks(docling_text),
                }
            )

        section_title = "Document"
        enriched_blocks: list[TextBlock] = []
        for block in base.text_blocks:
            text = block.text.strip()
            head = text.splitlines()[0] if text else ""
            if re.match(r"^(\d+(\.\d+)*)\s+", head) or head.isupper():
                section_title = head[:120]
            enriched_blocks.append(block.model_copy(update={"section_title": section_title}))

        base_conf = base.confidence_score
        complex_layouts = {"multi_column", "table_heavy", "mixed"}
        if docling_used:
            layout_bonus = 0.3 if profile.layout_complexity in complex_layouts else 0.22
        else:
            layout_bonus = 0.2 if profile.layout_complexity in complex_layouts else 0.1
        base = base.model_copy(
            update={
                "strategy_used": self.name,
                "confidence_score": min(1.0, base_conf + layout_bonus),
                "estimated_cost_usd": 0.01,
                "text_blocks": enriched_blocks,
                "metadata": {
                    **base.metadata,
                    "layout_reconstructed": True,
                    "docling_used": docling_used,
                    "docling_status": "ok" if docling_used else "fallback_local_only",
                },
            }
        )
        return base

    def _extract_with_docling(self, document_path: Path) -> str | None:
        try:
            from docling.document_converter import DocumentConverter  # type: ignore
        except Exception:
            return None

        try:
            converter = DocumentConverter()
            result = converter.convert(str(document_path))
        except Exception:
            return None

        document = getattr(result, "document", None)
        if document is None:
            return None

        # Handle docling API variations across versions.
        for method_name in ("export_to_markdown", "export_to_text", "to_markdown", "to_text"):
            method = getattr(document, method_name, None)
            if callable(method):
                try:
                    text = method()
                except Exception:
                    continue
                if isinstance(text, str) and text.strip():
                    return text

        for field_name in ("text", "content"):
            value = getattr(document, field_name, None)
            if isinstance(value, str) and value.strip():
                return value

        return None

    def _text_to_blocks(self, raw_text: str) -> list[TextBlock]:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw_text) if p.strip()]
        if not paragraphs:
            return [TextBlock(page_number=1, text=raw_text.strip() or "")]
        return [TextBlock(page_number=1, text=paragraph) for paragraph in paragraphs]
