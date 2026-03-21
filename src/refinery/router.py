from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.refinery.models import DocumentProfile, ExtractedDocument
from src.refinery.strategies import FastTextExtractor, LayoutExtractor, VisionExtractor


@dataclass(slots=True)
class RoutedExtraction:
    extracted: ExtractedDocument
    attempted_strategies: list[str]


class ExtractionRouter:
    def __init__(
        self,
        *,
        rules_path: str | Path = "rubric/extraction_rules.yaml",
        ledger_path: str | Path = ".refinery/extraction_ledger.jsonl",
        vision_budget_usd: float = 1.5,
        gemini_api_key: str | None = None,
        gemini_model: str | None = None,
        openai_api_key: str | None = None,
        openai_model: str | None = None,
    ) -> None:
        self.rules = _load_rules(Path(rules_path))
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

        self.fast = FastTextExtractor()
        self.layout = LayoutExtractor()
        self.vision = VisionExtractor(
            max_cost_usd=vision_budget_usd,
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
        )

    def extract(self, document_path: str | Path, profile: DocumentProfile) -> RoutedExtraction:
        path = Path(document_path)
        start = time.time()
        attempted: list[str] = []

        chain = self._strategy_chain(profile)
        latest: ExtractedDocument | None = None
        for name in chain:
            strategy = self._by_name(name)
            candidate = strategy.extract(path, profile)
            attempted.append(name)
            latest = candidate

            confidence_gate = self._confidence_gate_for(name)
            if candidate.confidence_score >= confidence_gate:
                break

        if latest is None:
            raise RuntimeError("No extraction strategy was executed.")

        elapsed_ms = int((time.time() - start) * 1000)
        self._append_ledger_entry(
            profile=profile,
            extracted=latest,
            attempted=attempted,
            elapsed_ms=elapsed_ms,
        )

        return RoutedExtraction(extracted=latest, attempted_strategies=attempted)

    def _strategy_chain(self, profile: DocumentProfile) -> list[str]:
        if profile.estimated_extraction_cost == "fast_text_sufficient":
            return ["fast_text", "layout_aware", "vision_augmented"]
        if profile.estimated_extraction_cost == "needs_layout_model":
            return ["layout_aware", "vision_augmented"]
        return ["vision_augmented"]

    def _by_name(self, name: str):
        if name == "fast_text":
            return self.fast
        if name == "layout_aware":
            return self.layout
        if name == "vision_augmented":
            return self.vision
        raise KeyError(f"Unknown strategy '{name}'")

    def _confidence_gate_for(self, strategy_name: str) -> float:
        # Backward-compatibility: if a global gate is defined, use it for all stages.
        if "confidence_gate" in self.rules:
            return float(self.rules["confidence_gate"])

        key_by_strategy = {
            "fast_text": "fast_text_confidence_gate",
            "layout_aware": "layout_aware_confidence_gate",
            "vision_augmented": "vision_confidence_gate",
        }
        default_by_strategy = {
            "fast_text": 0.9,
            "layout_aware": 0.88,
            "vision_augmented": 0.72,
        }
        key = key_by_strategy.get(strategy_name)
        if key and key in self.rules:
            return float(self.rules[key])
        return default_by_strategy.get(strategy_name, 0.72)

    def _append_ledger_entry(
        self,
        *,
        profile: DocumentProfile,
        extracted: ExtractedDocument,
        attempted: list[str],
        elapsed_ms: int,
    ) -> None:
        entry = {
            "document_id": profile.document_id,
            "document_name": profile.document_name,
            "strategy_used": extracted.strategy_used,
            "attempted_strategies": attempted,
            "confidence_score": extracted.confidence_score,
            "cost_estimate_usd": extracted.estimated_cost_usd,
            "processing_time_ms": elapsed_ms,
            "text_blocks": len(extracted.text_blocks),
            "tables": len(extracted.tables),
        }
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


DEFAULT_RULES: dict[str, Any] = {
    "fast_text_confidence_gate": 0.9,
    "layout_aware_confidence_gate": 0.88,
    "vision_confidence_gate": 0.72,
    "fast_text_min_chars": 300,
    "fast_text_max_image_ratio": 0.5,
    "chunk_max_tokens": 450,
}


def _load_rules(path: Path) -> dict[str, Any]:
    if not path.exists():
        return dict(DEFAULT_RULES)

    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            merged = dict(DEFAULT_RULES)
            merged.update({k: v for k, v in loaded.items() if v is not None})
            return merged
    except Exception:
        pass

    # Minimal fallback parser: key: value per line.
    merged = dict(DEFAULT_RULES)
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.replace(".", "", 1).isdigit():
            merged[key] = float(value) if "." in value else int(value)
        elif value.lower() in {"true", "false"}:
            merged[key] = value.lower() == "true"
        else:
            merged[key] = value.strip('"\'')
    return merged
