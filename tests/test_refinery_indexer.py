from __future__ import annotations

from src.refinery.indexer import PageIndexBuilder
from src.refinery.models import LDU, BoundingBox, ExtractedDocument, TextBlock


def _chunk(
    chunk_id: str,
    content: str,
    chunk_type: str,
    page: int,
    parent_section: str | None,
) -> LDU:
    return LDU(
        chunk_id=chunk_id,
        content=content,
        chunk_type=chunk_type,  # type: ignore[arg-type]
        page_refs=[page],
        bounding_box=BoundingBox(),
        parent_section=parent_section,
        token_count=len(content.split()),
        content_hash=f"hash-{chunk_id}",
        relationships=[],
    )


def test_indexer_preserves_sequential_sections(tmp_path) -> None:
    extracted = ExtractedDocument(
        document_id="doc-index-1",
        document_name="deck.pdf",
        source_path="/tmp/deck.pdf",
        strategy_used="vision_augmented",
        confidence_score=0.9,
        text_blocks=[
            TextBlock(
                page_number=1,
                text="Disclaimer & Forward-looking Statements",
                section_title="Disclaimer & Forward-looking Statements",
            ),
            TextBlock(page_number=2, text="Agenda", section_title="Agenda"),
            TextBlock(page_number=3, text="About Raya Holding", section_title="About Raya Holding"),
        ],
        raw_text="Disclaimer & Forward-looking Statements\nAgenda\nAbout Raya Holding",
    )
    chunks = [
        _chunk(
            "c1",
            "Disclaimer & Forward-looking Statements",
            "paragraph",
            1,
            "Disclaimer & Forward-looking Statements",
        ),
        _chunk("c2", "Agenda", "paragraph", 2, "Agenda"),
        _chunk("c3", "About Raya Holding", "paragraph", 3, "About Raya Holding"),
    ]

    index = PageIndexBuilder(pageindex_dir=tmp_path / "pageindex").build(extracted, chunks)

    titles = [section.title for section in index.root.child_sections]
    assert titles == [
        "Disclaimer & Forward-looking Statements",
        "Agenda",
        "About Raya Holding",
    ]


def test_indexer_splits_general_sections_across_pages(tmp_path) -> None:
    extracted = ExtractedDocument(
        document_id="doc-index-2",
        document_name="deck.pdf",
        source_path="/tmp/deck.pdf",
        strategy_used="layout_aware",
        confidence_score=0.9,
        text_blocks=[],
        raw_text="Slide one content Slide two content",
    )
    chunks = [
        _chunk("c1", "Slide one body text", "paragraph", 1, None),
        _chunk("c2", "Slide two body text", "paragraph", 2, None),
    ]

    index = PageIndexBuilder(pageindex_dir=tmp_path / "pageindex").build(extracted, chunks)

    assert len(index.root.child_sections) == 2
    assert [section.page_start for section in index.root.child_sections] == [1, 2]
