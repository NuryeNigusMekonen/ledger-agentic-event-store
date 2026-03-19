from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OriginType = Literal["native_digital", "scanned_image", "mixed", "form_fillable"]
LayoutComplexity = Literal[
    "single_column",
    "multi_column",
    "table_heavy",
    "figure_heavy",
    "mixed",
]
ExtractionCostTier = Literal[
    "fast_text_sufficient",
    "needs_layout_model",
    "needs_vision_model",
]
ChunkType = Literal["paragraph", "table", "figure", "list", "section"]


class BoundingBox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0


class DocumentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    document_name: str
    source_path: str
    origin_type: OriginType
    layout_complexity: LayoutComplexity
    language: str = "en"
    language_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    domain_hint: Literal["financial", "legal", "technical", "medical", "general"]
    estimated_extraction_cost: ExtractionCostTier
    page_count: int = Field(default=1, ge=1)
    char_density: float = Field(default=0.0, ge=0.0)
    image_area_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1)
    text: str
    bbox: BoundingBox = Field(default_factory=BoundingBox)
    section_title: str | None = None


class ExtractedTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1)
    headers: list[str]
    rows: list[list[str]]
    bbox: BoundingBox = Field(default_factory=BoundingBox)


class ExtractedFigure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1)
    caption: str
    bbox: BoundingBox = Field(default_factory=BoundingBox)


class ExtractedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    document_name: str
    source_path: str
    strategy_used: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    text_blocks: list[TextBlock] = Field(default_factory=list)
    tables: list[ExtractedTable] = Field(default_factory=list)
    figures: list[ExtractedFigure] = Field(default_factory=list)
    raw_text: str = ""
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class LDU(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    content: str
    chunk_type: ChunkType
    page_refs: list[int]
    bounding_box: BoundingBox = Field(default_factory=BoundingBox)
    parent_section: str | None = None
    token_count: int = Field(ge=0)
    content_hash: str
    relationships: list[str] = Field(default_factory=list)


class PageIndexNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    title: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    child_sections: list[PageIndexNode] = Field(default_factory=list)
    key_entities: list[str] = Field(default_factory=list)
    summary: str = ""
    data_types_present: list[str] = Field(default_factory=list)


class PageIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    root: PageIndexNode


class ProvenanceCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_name: str
    page_number: int = Field(ge=1)
    bbox: BoundingBox = Field(default_factory=BoundingBox)
    content_hash: str


class ProvenanceChain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citations: list[ProvenanceCitation] = Field(default_factory=list)


PageIndexNode.model_rebuild()
