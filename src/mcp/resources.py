from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.event_store import EventStore
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon


class LedgerMCPResources:
    def __init__(
        self,
        store: EventStore,
        daemon: ProjectionDaemon,
        compliance_projection: ComplianceAuditViewProjection,
    ) -> None:
        self.store = store
        self.daemon = daemon
        self.compliance_projection = compliance_projection

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "uri": "ledger://applications/{id}",
                "description": "ApplicationSummary projection (current state only).",
            },
            {
                "uri": "ledger://applications/{id}/compliance?as_of={timestamp}",
                "description": "ComplianceAuditView projection with optional temporal query.",
            },
            {
                "uri": "ledger://applications/{id}/audit-trail",
                "description": "AuditLedger stream direct load (justified exception).",
            },
            {
                "uri": "ledger://agents/{id}/performance",
                "description": "AgentPerformance projection (current metrics).",
            },
            {
                "uri": "ledger://agents/{id}/sessions/{session_id}",
                "description": "AgentSession stream direct load (justified exception).",
            },
            {
                "uri": "ledger://ledger/health",
                "description": "Projection lag watchdog endpoint.",
            },
        ]

    async def read(self, uri: str) -> dict[str, Any]:
        try:
            parsed = urlparse(uri)
            if parsed.scheme != "ledger":
                return _resource_error(
                    "InvalidResourceURI",
                    f"Unsupported resource scheme '{parsed.scheme}'.",
                    "use_ledger_scheme",
                )

            if parsed.netloc == "applications":
                return await self._applications_resource(parsed.path, parsed.query)
            if parsed.netloc == "agents":
                return await self._agents_resource(parsed.path)
            if parsed.netloc == "ledger":
                return await self._ledger_resource(parsed.path)

            return _resource_error(
                "UnknownResource",
                f"Unknown netloc '{parsed.netloc}' in URI.",
                "use_list_resources",
            )
        except Exception as exc:  # pragma: no cover - defensive
            return _resource_error(
                "InternalError",
                f"Resource read failed: {exc}",
                "inspect_logs_and_retry",
            )

    async def _applications_resource(self, path: str, query: str) -> dict[str, Any]:
        parts = [segment for segment in path.strip("/").split("/") if segment]
        if len(parts) == 1:
            application_id = parts[0]
            return await self._get_application_summary(application_id)
        if len(parts) == 2 and parts[1] == "compliance":
            application_id = parts[0]
            return await self._get_application_compliance(application_id, query)
        if len(parts) == 2 and parts[1] == "audit-trail":
            application_id = parts[0]
            return await self._get_application_audit_trail(application_id)
        return _resource_error(
            "UnknownResourcePath",
            f"Unknown applications path '{path}'.",
            "use_supported_applications_paths",
        )

    async def _agents_resource(self, path: str) -> dict[str, Any]:
        parts = [segment for segment in path.strip("/").split("/") if segment]
        if len(parts) == 2 and parts[1] == "performance":
            return await self._get_agent_performance(parts[0])
        if len(parts) == 3 and parts[1] == "sessions":
            return await self._get_agent_session(parts[0], parts[2])
        return _resource_error(
            "UnknownResourcePath",
            f"Unknown agents path '{path}'.",
            "use_supported_agents_paths",
        )

    async def _ledger_resource(self, path: str) -> dict[str, Any]:
        if path.strip("/") != "health":
            return _resource_error(
                "UnknownResourcePath",
                f"Unknown ledger path '{path}'.",
                "use_ledger_health_resource",
            )
        lags = await self.daemon.get_all_lags()
        return {
            "ok": True,
            "result": {
                "projections": {
                    name: {
                        "checkpoint_position": lag.checkpoint_position,
                        "latest_position": lag.latest_position,
                        "events_behind": lag.events_behind,
                        "lag_ms": lag.lag_ms,
                        "status": lag.status,
                        "updated_at": lag.updated_at.isoformat(),
                    }
                    for name, lag in lags.items()
                }
            },
        }

    async def _get_application_summary(self, application_id: str) -> dict[str, Any]:
        async with self.store._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM application_summary_projection
                WHERE application_id = $1
                """,
                application_id,
            )
        if row is None:
            return _resource_error(
                "NotFound",
                f"No projected application summary for '{application_id}'.",
                "verify_application_exists_or_projection_catchup",
            )
        return {"ok": True, "result": dict(row)}

    async def _get_application_compliance(self, application_id: str, query: str) -> dict[str, Any]:
        params = parse_qs(query)
        as_of_values = params.get("as_of")

        async with self.store._pool.acquire() as conn:
            if as_of_values:
                as_of = _parse_iso_timestamp(as_of_values[0])
                if as_of is None:
                    return _resource_error(
                        "ValidationError",
                        "Invalid as_of timestamp format.",
                        "use_iso_8601_timestamp",
                    )
                snapshot = await self.compliance_projection.get_compliance_at(
                    conn,
                    application_id,
                    as_of,
                )
            else:
                snapshot = await self.compliance_projection.get_current(conn, application_id)

            timeline_rows = await conn.fetch(
                """
                SELECT
                  global_position,
                  recorded_at,
                  event_type,
                  compliance_status,
                  rule_id,
                  rule_version,
                  failure_reason
                FROM compliance_audit_view_projection
                WHERE application_id = $1
                ORDER BY global_position ASC
                """,
                application_id,
            )

        if snapshot is None:
            return _resource_error(
                "NotFound",
                f"No compliance projection found for '{application_id}'.",
                "record_compliance_events_then_retry",
            )

        return {
            "ok": True,
            "result": {
                "application_id": application_id,
                "as_of": as_of_values[0] if as_of_values else None,
                "snapshot": dict(snapshot),
                "timeline": [dict(row) for row in timeline_rows],
            },
        }

    async def _get_application_audit_trail(self, application_id: str) -> dict[str, Any]:
        # Justified exception: audit trail resource reads directly from audit stream.
        stream_id = f"audit-application-{application_id}"
        events = await self.store.load_stream(stream_id)
        return {
            "ok": True,
            "result": {
                "stream_id": stream_id,
                "events": [event.model_dump(mode="json") for event in events],
            },
        }

    async def _get_agent_performance(self, agent_id: str) -> dict[str, Any]:
        async with self.store._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM agent_performance_projection
                WHERE agent_id = $1
                ORDER BY model_version ASC
                """,
                agent_id,
            )
        return {
            "ok": True,
            "result": {
                "agent_id": agent_id,
                "models": [dict(row) for row in rows],
            },
        }

    async def _get_agent_session(self, agent_id: str, session_id: str) -> dict[str, Any]:
        # Justified exception: full replay resource reads directly from session stream.
        stream_id = f"agent-{agent_id}-{session_id}"
        events = await self.store.load_stream(stream_id)
        return {
            "ok": True,
            "result": {
                "stream_id": stream_id,
                "events": [event.model_dump(mode="json") for event in events],
            },
        }


def _parse_iso_timestamp(value: str) -> datetime | None:
    try:
        # Accept trailing Z as UTC indicator.
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _resource_error(error_type: str, message: str, suggested_action: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "error_type": error_type,
            "message": message,
            "suggested_action": suggested_action,
        },
    }

