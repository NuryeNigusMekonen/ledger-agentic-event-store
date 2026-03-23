from __future__ import annotations

import contextlib
import io
import logging
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
            replacement_blocks = self._text_to_blocks(docling_text)
            base_blocks = base.text_blocks
            preserve_original_paging = len({block.page_number for block in base_blocks}) > 1
            base = base.model_copy(
                update={
                    "raw_text": docling_text,
                    "text_blocks": base_blocks if preserve_original_paging else replacement_blocks,
                }
            )

        section_title = "Document"
        enriched_blocks: list[TextBlock] = []
        for block in base.text_blocks:
            inferred_title = self._infer_section_title(block.text)
            if inferred_title:
                section_title = inferred_title
            enriched_blocks.append(block.model_copy(update={"section_title": section_title}))

        base_conf = base.confidence_score
        complex_layouts = {"multi_column", "table_heavy", "mixed"}
        if docling_used:
            layout_bonus = 0.3 if profile.layout_complexity in complex_layouts else 0.22
        else:
            layout_bonus = 0.2 if profile.layout_complexity in complex_layouts else 0.1
        confidence_penalty = 0.0
        if not docling_used and profile.origin_type in {"mixed", "scanned_image"}:
            confidence_penalty += 0.12
        if not docling_used and profile.image_area_ratio >= 0.5:
            confidence_penalty += 0.08
        large_complex_report = profile.page_count >= 20 and profile.layout_complexity in {
            "multi_column",
            "table_heavy",
        }
        if large_complex_report:
            confidence_penalty += 0.35
        confidence_score = max(0.0, min(1.0, base_conf + layout_bonus - confidence_penalty))
        large_report_cap = None
        if large_complex_report:
            large_report_cap = 0.84
            confidence_score = min(confidence_score, large_report_cap)

        preserve_fast_text_pages = docling_used and len(
            {block.page_number for block in base.text_blocks}
        ) > 1

        base = base.model_copy(
            update={
                "strategy_used": self.name,
                "confidence_score": confidence_score,
                "estimated_cost_usd": 0.01,
                "text_blocks": enriched_blocks,
                "metadata": {
                    **base.metadata,
                    "layout_reconstructed": True,
                    "docling_used": docling_used,
                    "docling_status": "ok" if docling_used else "fallback_local_only",
                    "docling_paging_strategy": (
                        "preserve_fast_text_pages"
                        if preserve_fast_text_pages
                        else ("docling_blocks" if docling_used else "fast_text_only")
                    ),
                    "layout_confidence_penalty": round(confidence_penalty, 3),
                    "layout_confidence_cap": (
                        large_report_cap if large_report_cap is not None else "none"
                    ),
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
            rapidocr_loggers = [
                logging.getLogger("RapidOCR"),
                logging.getLogger("rapidocr"),
            ]
            previous_levels = [logger.level for logger in rapidocr_loggers]
            previous_propagation = [logger.propagate for logger in rapidocr_loggers]
            for logger in rapidocr_loggers:
                logger.setLevel(logging.ERROR)
                logger.propagate = False
            try:
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    result = converter.convert(str(document_path))
            finally:
                for logger, level, propagate in zip(
                    rapidocr_loggers,
                    previous_levels,
                    previous_propagation,
                    strict=False,
                ):
                    logger.setLevel(level)
                    logger.propagate = propagate
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

    def _infer_section_title(self, text: str) -> str | None:
        candidates = [line.strip() for line in text.splitlines() if line.strip()]
        for candidate in candidates[:8]:
            normalized = re.sub(r"\s+", " ", candidate).strip(" -|")
            if not normalized or len(normalized) > 90:
                continue
            if normalized.isdigit():
                continue
            if re.search(r"[.!?;:]\s*$", normalized):
                continue
            if re.match(r"^(\d+(\.\d+)*)\s+", normalized):
                return normalized[:120]
            words = normalized.split()
            if len(words) > 12:
                continue
            alpha_words = [word for word in words if any(ch.isalpha() for ch in word)]
            if not alpha_words:
                continue
            title_case_words = sum(1 for word in alpha_words if word[:1].isupper())
            uppercase_ratio = title_case_words / len(alpha_words)
            if normalized.isupper() or uppercase_ratio >= 0.7:
                return normalized[:120]
            if len(alpha_words) <= 5 and uppercase_ratio >= 0.5:
                return normalized[:120]
        return None
