from __future__ import annotations

from pathlib import Path
from typing import Protocol

from src.refinery.models import DocumentProfile, ExtractedDocument


class ExtractorStrategy(Protocol):
    name: str

    def extract(self, document_path: Path, profile: DocumentProfile) -> ExtractedDocument:
        ...
