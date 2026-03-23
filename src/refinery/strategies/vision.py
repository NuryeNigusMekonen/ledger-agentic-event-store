from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib import error, request

from src.refinery.llm_provider import ChatProviderConfig, resolve_chat_provider
from src.refinery.models import DocumentProfile, ExtractedDocument
from src.refinery.strategies.fast_text import FastTextExtractor
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
        self.fast_text_extractor = FastTextExtractor()
        self.layout_extractor = LayoutExtractor()
        # If caller explicitly pins a Gemini model, favor Gemini flow over provider-first mode.
        self._gemini_model_explicit = gemini_model is not None
        resolved_api_key = (
            os.getenv("GEMINI_API_KEY", "") if gemini_api_key is None else gemini_api_key
        )
        resolved_model = (
            os.getenv("GEMINI_MODEL", "gemini-2.0-flash") if gemini_model is None else gemini_model
        )
        self.gemini_api_key = resolved_api_key.strip()
        self.gemini_model = resolved_model.strip()
        self.fallback_provider = resolve_chat_provider(
            openai_api_key=openai_api_key,
            openai_model=openai_model,
        )
        self.gemini_timeout_seconds = gemini_timeout_seconds

    def extract(self, document_path: Path, profile: DocumentProfile) -> ExtractedDocument:
        estimated_cost = min(self.max_cost_usd, max(0.05, 0.08 * profile.page_count))
        if (
            self.fallback_provider is not None
            and self.fallback_provider.provider == "openrouter"
            and not self._gemini_model_explicit
        ):
            provider_first_base = self.fast_text_extractor.extract(document_path, profile)
            provider_first = self._provider_first_extract(provider_first_base, estimated_cost)
            if provider_first is not None:
                return provider_first

        # Keep deterministic local extraction so the pipeline works offline.
        base = self.layout_extractor.extract(document_path, profile)
        confidence_boost = 0.1
        metadata: dict[str, str | int | float | bool] = {
            **base.metadata,
            "vision_fallback_mode": True,
            "vision_entry_mode": "layout_first",
            "budget_guard_max_cost_usd": self.max_cost_usd,
            "gemini_enabled": bool(self.gemini_api_key),
            "openai_enabled": bool(
                self.fallback_provider and self.fallback_provider.provider == "openai"
            ),
            "openrouter_enabled": bool(
                self.fallback_provider and self.fallback_provider.provider == "openrouter"
            ),
        }

        insight: dict[str, str | int | float] | None = None
        if self.gemini_api_key:
            insight = self._gemini_refine(base.raw_text)
            if insight is None:
                metadata["gemini_status"] = (
                    f"fallback_{self.fallback_provider.provider}"
                    if self.fallback_provider is not None
                    else "fallback_local_only"
                )
            else:
                confidence_boost = max(confidence_boost, float(insight["confidence_boost"]))
                metadata["gemini_status"] = "ok"
                metadata["gemini_model"] = self.gemini_model
                metadata["gemini_quality"] = str(insight["quality"])
                metadata["gemini_detected_metrics"] = int(insight["detected_metrics_count"])
                metadata["llm_provider"] = "gemini"
                if self.fallback_provider is not None:
                    metadata[f"{self.fallback_provider.provider}_status"] = "not_needed"
        else:
            metadata["gemini_status"] = "disabled_missing_api_key"

        if insight is None and self.fallback_provider is not None:
            provider_insight = self._chat_refine(base.raw_text, self.fallback_provider)
            status_key = f"{self.fallback_provider.provider}_status"
            model_key = f"{self.fallback_provider.provider}_model"
            quality_key = f"{self.fallback_provider.provider}_quality"
            metrics_key = f"{self.fallback_provider.provider}_detected_metrics"
            if provider_insight is None:
                metadata[status_key] = "fallback_local_only"
            else:
                confidence_boost = max(
                    confidence_boost,
                    float(provider_insight["confidence_boost"]),
                )
                metadata[status_key] = "ok"
                metadata[model_key] = self.fallback_provider.model
                metadata[quality_key] = str(provider_insight["quality"])
                metadata[metrics_key] = int(provider_insight["detected_metrics_count"])
                metadata["llm_provider"] = self.fallback_provider.provider
        elif self.fallback_provider is None:
            metadata["openai_status"] = "disabled_missing_api_key"
            metadata["openrouter_status"] = "disabled_missing_api_key"

        return base.model_copy(
            update={
                "strategy_used": self.name,
                "confidence_score": min(1.0, base.confidence_score + confidence_boost),
                "estimated_cost_usd": estimated_cost,
                "metadata": metadata,
            }
        )

    def _provider_first_extract(
        self,
        base: ExtractedDocument,
        estimated_cost: float,
    ) -> ExtractedDocument | None:
        if self.fallback_provider is None or self.fallback_provider.provider != "openrouter":
            return None
        if not base.raw_text.strip():
            return None

        provider_insight = self._chat_refine(base.raw_text, self.fallback_provider)
        if provider_insight is None:
            return None

        metadata: dict[str, str | int | float | bool] = {
            **base.metadata,
            "vision_fallback_mode": False,
            "vision_entry_mode": "provider_first",
            "budget_guard_max_cost_usd": self.max_cost_usd,
            "gemini_enabled": bool(self.gemini_api_key),
            "openai_enabled": False,
            "openrouter_enabled": True,
            "openrouter_status": "ok",
            "openrouter_model": self.fallback_provider.model,
            "openrouter_quality": str(provider_insight["quality"]),
            "openrouter_detected_metrics": int(provider_insight["detected_metrics_count"]),
            "llm_provider": "openrouter",
        }
        metadata["gemini_status"] = (
            "not_needed" if self.gemini_api_key else "disabled_missing_api_key"
        )
        metadata["openai_status"] = "disabled_missing_api_key"

        confidence_boost = max(0.1, float(provider_insight["confidence_boost"]))
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

    def _chat_refine(
        self,
        raw_text: str,
        provider: ChatProviderConfig,
    ) -> dict[str, str | int | float] | None:
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
            "model": provider.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"DOCUMENT_TEXT:\n{snippet}"},
            ],
            "temperature": 0.1,
            "max_tokens": 256,
            "response_format": {"type": "json_object"},
        }
        req = request.Request(
            provider.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=provider.headers(),
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
                provider.endpoint,
                data=json.dumps(retry_payload).encode("utf-8"),
                headers=provider.headers(),
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
