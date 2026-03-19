from __future__ import annotations

import hashlib
import re
from pathlib import Path

from src.refinery.models import DocumentProfile


class DocumentTriageAgent:
    def __init__(self, profiles_dir: str | Path = ".refinery/profiles") -> None:
        self.profiles_dir = Path(profiles_dir)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

    def profile_document(self, document_path: str | Path) -> DocumentProfile:
        path = Path(document_path)
        if not path.exists():
            raise FileNotFoundError(f"Document not found: {path}")

        text, page_count, char_density, image_area_ratio = self._sample_document_signals(path)
        origin = self._detect_origin(path, char_density, image_area_ratio, text)
        layout = self._detect_layout_complexity(text)
        domain_hint = self._detect_domain_hint(text)
        estimated_cost = self._estimate_cost(origin=origin, layout=layout)

        profile = DocumentProfile(
            document_id=self._stable_document_id(path),
            document_name=path.name,
            source_path=str(path),
            origin_type=origin,
            layout_complexity=layout,
            language="en",
            language_confidence=0.9,
            domain_hint=domain_hint,
            estimated_extraction_cost=estimated_cost,
            page_count=page_count,
            char_density=char_density,
            image_area_ratio=image_area_ratio,
        )
        self._write_profile(profile)
        return profile

    def _sample_document_signals(self, path: Path) -> tuple[str, int, float, float]:
        suffix = path.suffix.lower()
        if suffix in {".txt", ".md", ".csv", ".json"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            chars = len(text)
            line_count = max(text.count("\n"), 1)
            density = chars / line_count
            image_ratio = 0.0
            return text, 1, density, image_ratio

        if suffix == ".pdf":
            try:
                import pdfplumber  # type: ignore
            except Exception:
                raw = path.read_bytes()
                snippet = raw[:20000].decode("latin1", errors="ignore")
                return snippet, 1, float(len(snippet)), 0.8

            with pdfplumber.open(str(path)) as pdf:
                pages_text: list[str] = []
                image_ratios: list[float] = []
                for page in pdf.pages:
                    page_text = page.extract_text() or ""
                    pages_text.append(page_text)
                    page_area = max(float(page.width * page.height), 1.0)
                    image_area = 0.0
                    for image in page.images:
                        width = float(image.get("width") or 0.0)
                        height = float(image.get("height") or 0.0)
                        image_area += max(width * height, 0.0)
                    image_ratios.append(min(1.0, image_area / page_area))

            joined = "\n".join(pages_text)
            page_count = max(len(pages_text), 1)
            density = len(joined) / page_count
            image_ratio = sum(image_ratios) / page_count if image_ratios else 0.0
            return joined, page_count, density, image_ratio

        raw = path.read_bytes()
        snippet = raw[:20000].decode("utf-8", errors="ignore")
        return snippet, 1, float(len(snippet)), 0.0

    def _detect_origin(
        self,
        path: Path,
        char_density: float,
        image_area_ratio: float,
        text: str,
    ) -> str:
        if path.suffix.lower() == ".pdf":
            if char_density < 30 and image_area_ratio > 0.6:
                return "scanned_image"
            if image_area_ratio > 0.3:
                return "mixed"
            return "native_digital"

        if path.suffix.lower() in {".csv", ".xlsx"}:
            return "form_fillable"

        if len(text.strip()) < 40:
            return "mixed"
        return "native_digital"

    def _detect_layout_complexity(self, text: str) -> str:
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return "mixed"

        table_like = sum(1 for line in lines if line.count(",") >= 3 or "|" in line)
        figure_like = sum(1 for line in lines if re.search(r"\bfigure\b|\bchart\b", line, re.I))
        header_like = sum(1 for line in lines if re.match(r"^(\d+(\.\d+)*)\s+", line.strip()))

        if table_like / len(lines) > 0.2:
            return "table_heavy"
        if figure_like / len(lines) > 0.1:
            return "figure_heavy"
        if header_like > 8:
            return "multi_column"
        return "single_column"

    def _detect_domain_hint(self, text: str) -> str:
        lowered = text.lower()
        scores = {
            "financial": ["revenue", "ebitda", "balance sheet", "net income", "asset"],
            "legal": ["whereas", "agreement", "plaintiff", "statute", "clause"],
            "technical": ["system", "architecture", "latency", "throughput", "api"],
            "medical": ["patient", "diagnosis", "clinical", "treatment", "hospital"],
        }
        best = "general"
        best_score = 0
        for domain, keywords in scores.items():
            score = sum(1 for kw in keywords if kw in lowered)
            if score > best_score:
                best_score = score
                best = domain
        return best

    def _estimate_cost(self, *, origin: str, layout: str) -> str:
        if origin == "scanned_image":
            return "needs_vision_model"
        if layout in {"multi_column", "table_heavy", "mixed"} or origin == "mixed":
            return "needs_layout_model"
        return "fast_text_sufficient"

    def _stable_document_id(self, path: Path) -> str:
        seed = f"{path.resolve()}:{path.stat().st_mtime_ns}:{path.stat().st_size}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    def _write_profile(self, profile: DocumentProfile) -> None:
        profile_path = self.profiles_dir / f"{profile.document_id}.json"
        profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
