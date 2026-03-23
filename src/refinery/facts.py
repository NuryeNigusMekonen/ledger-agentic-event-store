from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

from src.refinery.llm_provider import ChatProviderConfig, resolve_chat_provider
from src.refinery.models import LDU


@dataclass(slots=True)
class FactRecord:
    document_id: str
    metric_name: str
    metric_value: float
    raw_value: str
    currency: str
    source_chunk_id: str
    page_number: int


class FinancialFactExtractor:
    METRIC_PATTERNS = {
        "total_revenue": r"(?:total\s+revenue|revenue)\s*[:=-]?\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
        "net_income": r"(?:net\s+income)\s*[:=-]?\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
        "ebitda": r"(?:ebitda)\s*[:=-]?\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
        "total_assets": r"(?:total\s+assets)\s*[:=-]?\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
        "total_liabilities": r"(?:total\s+liabilities)\s*[:=-]?\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
    }

    def extract(self, document_id: str, chunks: list[LDU]) -> list[FactRecord]:
        facts: list[FactRecord] = []
        for chunk in chunks:
            text = chunk.content.lower()
            for metric, pattern in self.METRIC_PATTERNS.items():
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if not match:
                    continue
                raw_value = match.group(1)
                normalized = float(raw_value.replace(",", ""))
                facts.append(
                    FactRecord(
                        document_id=document_id,
                        metric_name=metric,
                        metric_value=normalized,
                        raw_value=raw_value,
                        currency="USD",
                        source_chunk_id=chunk.chunk_id,
                        page_number=chunk.page_refs[0],
                    )
                )
        return facts


class LLMFinancialFactExtractor:
    METRIC_KEYS = (
        "total_revenue",
        "net_income",
        "ebitda",
        "total_assets",
        "total_liabilities",
    )

    def __init__(
        self,
        *,
        gemini_api_key: str | None = None,
        gemini_model: str | None = None,
        openai_api_key: str | None = None,
        openai_model: str | None = None,
        timeout_seconds: float = 12.0,
    ) -> None:
        resolved_gemini_key = (
            os.getenv("GEMINI_API_KEY", "") if gemini_api_key is None else gemini_api_key
        )
        resolved_gemini_model = (
            os.getenv("GEMINI_MODEL", "gemini-2.0-flash") if gemini_model is None else gemini_model
        )

        self.gemini_api_key = resolved_gemini_key.strip()
        self.gemini_model = resolved_gemini_model.strip()
        self.fallback_provider = resolve_chat_provider(
            openai_api_key=openai_api_key,
            openai_model=openai_model,
        )
        self.timeout_seconds = timeout_seconds

    def extract(self, raw_text: str) -> tuple[dict[str, float | None], str | None]:
        blank = {metric: None for metric in self.METRIC_KEYS}
        snippet = raw_text.strip()[:12000]
        if not snippet:
            return blank, None

        if self.gemini_api_key:
            gemini_result = self._extract_with_gemini(snippet)
            if gemini_result is not None and any(
                value is not None for value in gemini_result.values()
            ):
                return gemini_result, "gemini"

        if self.fallback_provider is not None:
            fallback_result = self._extract_with_chat_provider(snippet, self.fallback_provider)
            if fallback_result is not None and any(
                value is not None for value in fallback_result.values()
            ):
                return fallback_result, self.fallback_provider.provider

        return blank, None

    def _extract_with_gemini(self, snippet: str) -> dict[str, float | None] | None:
        prompt = (
            "Extract these financial metrics from the text and return strict JSON with keys:\n"
            "total_revenue, net_income, ebitda, total_assets, total_liabilities.\n"
            "Use numeric values only (no commas, no currency symbols), or null when missing."
        )
        payload = {
            "contents": [
                {"parts": [{"text": prompt}, {"text": f"DOCUMENT_TEXT:\n{snippet}"}]}
            ],
            "generationConfig": {
                "temperature": 0.0,
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
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except (TimeoutError, error.HTTPError, error.URLError, OSError):
            return None

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None

        candidate = _extract_gemini_candidate_text(parsed)
        if not candidate:
            return None
        obj = _load_first_json_object(candidate)
        if not isinstance(obj, dict):
            return None
        return _normalize_metric_payload(obj, self.METRIC_KEYS)

    def _extract_with_chat_provider(
        self,
        snippet: str,
        provider: ChatProviderConfig,
    ) -> dict[str, float | None] | None:
        prompt = (
            "Extract these financial metrics from the text and return strict JSON with keys:\n"
            "total_revenue, net_income, ebitda, total_assets, total_liabilities.\n"
            "Use numeric values only (no commas, no currency symbols), or null when missing."
        )
        payload = {
            "model": provider.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"DOCUMENT_TEXT:\n{snippet}"},
            ],
            "temperature": 0.0,
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
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
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
                with request.urlopen(retry_req, timeout=self.timeout_seconds) as response:
                    body = response.read().decode("utf-8")
            except (TimeoutError, error.HTTPError, error.URLError, OSError):
                return None
        except (TimeoutError, error.URLError, OSError):
            return None

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None

        candidate = _extract_openai_candidate_text(parsed)
        if not candidate:
            return None
        obj = _load_first_json_object(candidate)
        if not isinstance(obj, dict):
            return None
        return _normalize_metric_payload(obj, self.METRIC_KEYS)


class SQLiteFactStore:
    def __init__(self, db_path: str | Path = ".refinery/facts.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fact_table (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  document_id TEXT NOT NULL,
                  metric_name TEXT NOT NULL,
                  metric_value REAL NOT NULL,
                  raw_value TEXT NOT NULL,
                  currency TEXT NOT NULL,
                  source_chunk_id TEXT NOT NULL,
                  page_number INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_fact_doc_metric
                ON fact_table (document_id, metric_name)
                """
            )

    def upsert_facts(self, facts: list[FactRecord]) -> None:
        if not facts:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO fact_table (
                  document_id,
                  metric_name,
                  metric_value,
                  raw_value,
                  currency,
                  source_chunk_id,
                  page_number
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        fact.document_id,
                        fact.metric_name,
                        fact.metric_value,
                        fact.raw_value,
                        fact.currency,
                        fact.source_chunk_id,
                        fact.page_number,
                    )
                    for fact in facts
                ],
            )

    def query(self, sql: str) -> list[dict[str, str | int | float]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql).fetchall()
        return [dict(row) for row in rows]


def _extract_gemini_candidate_text(payload: dict[str, Any]) -> str:
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


def _normalize_metric_payload(
    payload: dict[str, Any],
    metric_keys: tuple[str, ...],
) -> dict[str, float | None]:
    normalized_payload = {
        _normalize_key(str(key)): value for key, value in payload.items()
    }
    aliases = {
        "total_revenue": ("totalrevenue", "revenue"),
        "net_income": ("netincome", "netprofit", "profitaftertax"),
        "ebitda": ("ebitda",),
        "total_assets": ("totalassets", "assets"),
        "total_liabilities": ("totalliabilities", "totalliability", "liabilities"),
    }

    output: dict[str, float | None] = {}
    for metric in metric_keys:
        aliases_for_metric = aliases.get(metric, (_normalize_key(metric),))
        raw_value: Any = None
        for alias in aliases_for_metric:
            if alias in normalized_payload:
                raw_value = normalized_payload[alias]
                break
        output[metric] = _coerce_metric_value(raw_value)
    return output


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.lower())


def _coerce_metric_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None

    raw = value.strip().lower()
    if raw in {"", "null", "none", "n/a", "na"}:
        return None

    negative = raw.startswith("(") and raw.endswith(")")
    if negative:
        raw = raw[1:-1]

    multiplier = 1.0
    if "billion" in raw or raw.endswith("bn"):
        multiplier = 1_000_000_000.0
    elif "million" in raw or raw.endswith("mn") or raw.endswith("m"):
        multiplier = 1_000_000.0
    elif "thousand" in raw or raw.endswith("k"):
        multiplier = 1_000.0

    numeric = re.sub(r"[^0-9.\-]", "", raw)
    if not numeric:
        return None

    try:
        parsed = float(numeric) * multiplier
    except ValueError:
        return None
    return -parsed if negative else parsed
