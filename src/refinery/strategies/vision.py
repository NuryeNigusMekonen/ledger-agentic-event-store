from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib import error, request

from src.refinery.models import DocumentProfile, ExtractedDocument
from src.refinery.strategies.layout import LayoutExtractor


class VisionExtractor:
    name = "vision_augmented"

    def __init__(
        self,
        max_cost_usd: float = 1.5,
        *,
        gemini_api_key: str | None = None,
        gemini_model: str | None = None,
        openai_api_key: str | None = None,
        openai_model: str | None = None,
        gemini_timeout_seconds: float = 12.0,
    ) -> None:
        self.max_cost_usd = max_cost_usd
        self.layout_extractor = LayoutExtractor()
        resolved_api_key = os.getenv("GEMINI_API_KEY", "") if gemini_api_key is None else gemini_api_key
        resolved_model = (
            os.getenv("GEMINI_MODEL", "gemini-2.0-flash") if gemini_model is None else gemini_model
        )
        # Only auto-load OpenAI key from env when gemini_api_key wasn't explicitly set by caller.
        # This keeps explicit test/dev usage predictable (e.g., gemini_api_key="").
        if openai_api_key is None:
            resolved_openai_api_key = (
                os.getenv("OPENAI_API_KEY", "") if gemini_api_key is None else ""
            )
        else:
            resolved_openai_api_key = openai_api_key
        resolved_openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini") if openai_model is None else openai_model
        self.gemini_api_key = resolved_api_key.strip()
        self.gemini_model = resolved_model.strip()
        self.openai_api_key = resolved_openai_api_key.strip()
        self.openai_model = resolved_openai_model.strip()
        self.gemini_timeout_seconds = gemini_timeout_seconds

    def extract(self, document_path: Path, profile: DocumentProfile) -> ExtractedDocument:
        # Keep deterministic local extraction so the pipeline works offline.
        base = self.layout_extractor.extract(document_path, profile)
        estimated_cost = min(self.max_cost_usd, max(0.05, 0.08 * profile.page_count))
        confidence_boost = 0.1
        metadata: dict[str, str | int | float | bool] = {
            **base.metadata,
            "vision_fallback_mode": True,
            "budget_guard_max_cost_usd": self.max_cost_usd,
            "gemini_enabled": bool(self.gemini_api_key),
            "openai_enabled": bool(self.openai_api_key),
        }

        insight: dict[str, str | int | float] | None = None
        if self.gemini_api_key:
            insight = self._gemini_refine(base.raw_text)
            if insight is None:
                metadata["gemini_status"] = "fallback_openai" if self.openai_api_key else "fallback_local_only"
            else:
                confidence_boost = max(confidence_boost, float(insight["confidence_boost"]))
                metadata["gemini_status"] = "ok"
                metadata["gemini_model"] = self.gemini_model
                metadata["gemini_quality"] = str(insight["quality"])
                metadata["gemini_detected_metrics"] = int(insight["detected_metrics_count"])
                metadata["llm_provider"] = "gemini"
                metadata["openai_status"] = "not_needed"
        else:
            metadata["gemini_status"] = "disabled_missing_api_key"

        if insight is None and self.openai_api_key:
            openai_insight = self._openai_refine(base.raw_text)
            if openai_insight is None:
                metadata["openai_status"] = "fallback_local_only"
            else:
                confidence_boost = max(confidence_boost, float(openai_insight["confidence_boost"]))
                metadata["openai_status"] = "ok"
                metadata["openai_model"] = self.openai_model
                metadata["openai_quality"] = str(openai_insight["quality"])
                metadata["openai_detected_metrics"] = int(openai_insight["detected_metrics_count"])
                metadata["llm_provider"] = "openai"
        elif "openai_status" not in metadata:
            metadata["openai_status"] = "disabled_missing_api_key"

        return base.model_copy(
            update={
                "strategy_used": self.name,
                "confidence_score": min(1.0, base.confidence_score + confidence_boost),
                "estimated_cost_usd": estimated_cost,
                "metadata": metadata,
            }
        )

    def _gemini_refine(self, raw_text: str) -> dict[str, str | int | float] | None:
        snippet = raw_text.strip()[:12000]
        if not snippet:
            return None

        prompt = (
            "You evaluate financial-document extraction quality.\n"
            "Return JSON only with keys:\n"
            "- confidence_boost: number between 0 and 0.25\n"
            "- quality: low, medium, or high\n"
            "- detected_metric_names: list of metric names found in the text\n"
            "Do not include markdown fences."
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"text": f"DOCUMENT_TEXT:\n{snippet}"},
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 256,
                "responseMimeType": "application/json",
            },
        }

        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.gemini_model}:generateContent?key={self.gemini_api_key}"
        )
        req = request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.gemini_timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except (TimeoutError, error.HTTPError, error.URLError, OSError):
            return None

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None

        candidate_text = _extract_candidate_text(parsed)
        if not candidate_text:
            return None
        obj = _load_first_json_object(candidate_text)
        if not isinstance(obj, dict):
            return None

        boost = _clamp_float(obj.get("confidence_boost"), min_value=0.0, max_value=0.25)
        quality = str(obj.get("quality", "unknown")).strip().lower()[:16]
        metrics = obj.get("detected_metric_names")
        metrics_count = len(metrics) if isinstance(metrics, list) else 0

        return {
            "confidence_boost": boost,
            "quality": quality,
            "detected_metrics_count": metrics_count,
        }

    def _openai_refine(self, raw_text: str) -> dict[str, str | int | float] | None:
        snippet = raw_text.strip()[:12000]
        if not snippet:
            return None

        prompt = (
            "You evaluate financial-document extraction quality.\n"
            "Return JSON only with keys:\n"
            "- confidence_boost: number between 0 and 0.25\n"
            "- quality: low, medium, or high\n"
            "- detected_metric_names: list of metric names found in the text\n"
            "Do not include markdown fences."
        )
        payload = {
            "model": self.openai_model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"DOCUMENT_TEXT:\n{snippet}"},
            ],
            "temperature": 0.1,
            "max_tokens": 256,
            "response_format": {"type": "json_object"},
        }
        endpoint = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.openai_api_key}",
        }

        req = request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.gemini_timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            # Some models reject response_format=json_object. Retry without it.
            if exc.code != 400:
                return None
            retry_payload = dict(payload)
            retry_payload.pop("response_format", None)
            retry_req = request.Request(
                endpoint,
                data=json.dumps(retry_payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with request.urlopen(retry_req, timeout=self.gemini_timeout_seconds) as response:
                    body = response.read().decode("utf-8")
            except (TimeoutError, error.HTTPError, error.URLError, OSError):
                return None
        except (TimeoutError, error.URLError, OSError):
            return None

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None

        candidate_text = _extract_openai_candidate_text(parsed)
        if not candidate_text:
            return None
        obj = _load_first_json_object(candidate_text)
        if not isinstance(obj, dict):
            return None

        boost = _clamp_float(obj.get("confidence_boost"), min_value=0.0, max_value=0.25)
        quality = str(obj.get("quality", "unknown")).strip().lower()[:16]
        metrics = obj.get("detected_metric_names")
        metrics_count = len(metrics) if isinstance(metrics, list) else 0

        return {
            "confidence_boost": boost,
            "quality": quality,
            "detected_metrics_count": metrics_count,
        }


def _extract_candidate_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return ""
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return str(part["text"])
    return ""


def _extract_openai_candidate_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(str(item["text"]))
        return "\n".join(chunks)
    return ""


def _load_first_json_object(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json", "", 1).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        obj = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _clamp_float(value: Any, *, min_value: float, max_value: float) -> float:
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return min_value
    return min(max_value, max(min_value, raw))
