from __future__ import annotations

from typing import Any

from src.commands.handlers import WriteCommandHandlers
from src.event_store import EventStore
from src.mcp.resources import LedgerMCPResources
from src.mcp.tools import LedgerMCPTools
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.client_analytics import ClientAnalyticsProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon


class LedgerMCPServer:
    """Lightweight MCP-like surface for tools/resources."""

    def __init__(self, store: EventStore, auto_project: bool = True) -> None:
        self.store = store
        self.auto_project = auto_project
        self._initialized = False

        self.handlers = WriteCommandHandlers(store=self.store)
        self.compliance_projection = ComplianceAuditViewProjection()
        self.daemon = ProjectionDaemon(
            store=self.store,
            projections=[
                ApplicationSummaryProjection(),
                self.compliance_projection,
                AgentPerformanceLedgerProjection(),
                ClientAnalyticsProjection(),
            ],
            batch_size=200,
            max_retries=3,
            retry_delay_seconds=0.1,
        )
        self.tools = LedgerMCPTools(
            store=self.store,
            handlers=self.handlers,
            after_write=self._project_after_write,
        )
        self.resources = LedgerMCPResources(
            store=self.store,
            daemon=self.daemon,
            compliance_projection=self.compliance_projection,
        )

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self.daemon.initialize()
        if self.auto_project:
            await self.daemon.run_once()
        self._initialized = True

    def list_tools(self) -> list[dict[str, Any]]:
        return self.tools.definitions()

    def list_resources(self) -> list[dict[str, Any]]:
        return self.resources.definitions()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        return await self.tools.call(name, arguments)

    async def read_resource(self, uri: str) -> dict[str, Any]:
        await self.initialize()
        if self.auto_project:
            await self.daemon.run_once()
        return await self.resources.read(uri)

    async def _project_after_write(self) -> None:
        if not self.auto_project:
            return
        await self.daemon.run_once()
