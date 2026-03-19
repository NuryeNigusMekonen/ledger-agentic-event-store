from __future__ import annotations

from src.refinery.chunker import ChunkingEngine
from src.refinery.models import ExtractedDocument, ExtractedTable, TextBlock


def test_chunker_preserves_table_headers_and_cross_refs() -> None:
    extracted = ExtractedDocument(
        document_id="doc-1",
        document_name="doc-1.txt",
        source_path="/tmp/doc-1.txt",
        strategy_used="layout_aware",
        confidence_score=0.9,
        text_blocks=[
            TextBlock(page_number=1, text="See Table 1 for total revenue trends.")
        ],
        tables=[
            ExtractedTable(
                page_number=1,
                headers=["Metric", "Value"],
                rows=[["Total Revenue", "1200000"]],
            )
        ],
    )

    engine = ChunkingEngine(max_tokens=450)
    chunks = engine.chunk_document(extracted)

    table_chunk = next(chunk for chunk in chunks if chunk.chunk_type == "table")
    assert "headers:" in table_chunk.content.lower()

    paragraph_chunk = next(chunk for chunk in chunks if chunk.chunk_type == "paragraph")
    assert table_chunk.chunk_id in paragraph_chunk.relationships
