from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.models.events import DomainError, StoredEvent


@dataclass
class ComplianceRecordAggregate:
    application_id: str | None = None
    regulation_set_version: str | None = None
    mandatory_checks: set[str] = field(default_factory=set)
    passed_checks: set[str] = field(default_factory=set)
    failed_checks: dict[str, str] = field(default_factory=dict)
    status: str = "NOT_STARTED"
    version: int = 0

    @classmethod
    def load(cls, events: list[StoredEvent]) -> ComplianceRecordAggregate:
        aggregate = cls()
        for event in events:
            aggregate.apply(event)
        return aggregate

    @property
    def is_pending(self) -> bool:
        return self.status == "PENDING"

    @property
    def is_cleared(self) -> bool:
        return self.status == "CLEARED"

    def apply(self, event: StoredEvent) -> None:
        self._apply(event.event_type, event.payload)
        self.version = event.stream_position

    def ensure_can_clear(self) -> None:
        if not self.mandatory_checks:
            raise DomainError("Cannot clear compliance without mandatory checks configured.")
        if self.mandatory_checks - self.passed_checks:
            raise DomainError("Cannot clear compliance with missing mandatory checks.")
        if self.failed_checks:
            raise DomainError("Cannot clear compliance while failed checks exist.")

    def _apply(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "ComplianceCheckRequested":
            self._apply_check_requested(payload)
            return
        if event_type == "ComplianceRulePassed":
            self._apply_rule_passed(payload)
            return
        if event_type == "ComplianceRuleFailed":
            self._apply_rule_failed(payload)
            return

    def _apply_check_requested(self, payload: dict[str, Any]) -> None:
        if self.status != "NOT_STARTED":
            raise DomainError(
                "ComplianceCheckRequested can only happen once per compliance stream."
            )
        checks_required = [str(item) for item in payload.get("checks_required", [])]
        if not checks_required:
            raise DomainError("ComplianceCheckRequested requires non-empty checks_required.")
        self.application_id = payload["application_id"]
        self.regulation_set_version = str(payload["regulation_set_version"])
        self.mandatory_checks = set(checks_required)
        self.status = "PENDING"

    def _apply_rule_passed(self, payload: dict[str, Any]) -> None:
        self._require_started()
        rule_id = str(payload["rule_id"])
        rule_version = str(payload.get("rule_version", "")).strip()
        if not rule_version:
            raise DomainError("ComplianceRulePassed requires rule_version.")
        self._validate_rule_membership(rule_id)
        if rule_id in self.failed_checks:
            raise DomainError(f"Rule '{rule_id}' already has a failed verdict.")
        self.passed_checks.add(rule_id)
        self._recompute_status()

    def _apply_rule_failed(self, payload: dict[str, Any]) -> None:
        self._require_started()
        rule_id = str(payload["rule_id"])
        rule_version = str(payload.get("rule_version", "")).strip()
        if not rule_version:
            raise DomainError("ComplianceRuleFailed requires rule_version.")
        self._validate_rule_membership(rule_id)
        reason = str(payload.get("failure_reason", "")).strip()
        if not reason:
            raise DomainError("ComplianceRuleFailed requires failure_reason.")
        self.failed_checks[rule_id] = reason
        self.passed_checks.discard(rule_id)
        self._recompute_status()

    def _require_started(self) -> None:
        if self.status == "NOT_STARTED":
            raise DomainError("Compliance check stream has not been initialized.")

    def _validate_rule_membership(self, rule_id: str) -> None:
        if self.mandatory_checks and rule_id not in self.mandatory_checks:
            raise DomainError(f"Rule '{rule_id}' is not part of mandatory checks for this stream.")

    def _recompute_status(self) -> None:
        if self.failed_checks:
            self.status = "FAILED"
            return
        if self.mandatory_checks and self.mandatory_checks <= self.passed_checks:
            self.status = "CLEARED"
            return
        self.status = "PENDING"
