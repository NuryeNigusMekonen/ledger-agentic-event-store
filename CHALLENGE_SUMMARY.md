# TRP1 Week 5: High-Level Challenge Summary

## What this week is about
Build **The Ledger**: an enterprise-grade event store that gives multi-agent AI systems immutable memory, reproducible decisions, and audit-ready governance.

## Core outcome
You should be able to show the complete decision history for a loan application in under 60 seconds:
- Every agent action
- Every compliance check
- Every human review
- Causal links between decisions
- Temporal state at a chosen point in time
- Cryptographic integrity verification

## Architectural pillars
- Event sourcing as source of truth (append-only events)
- CQRS (commands write events; queries read projections)
- Optimistic concurrency (`expected_version`) for conflict safety
- Async projections with checkpoints and lag visibility
- Upcasting for schema evolution without rewriting history
- Audit hash chains and tamper detection
- MCP tools/resources as enterprise integration surface

## Business scenario
Apex Financial Services runs four collaborating AI agents on loan applications:
- CreditAnalysis
- FraudDetection
- ComplianceAgent
- DecisionOrchestrator

The system must satisfy strict regulatory auditability and state reconstruction requirements by design, not by retrofitted logging.

## Delivery shape
- Interim submission: event store core + early domain logic + proof of concurrency safety
- Final submission: full aggregates, projections, daemon, upcasting, integrity, MCP interface, and end-to-end tests
- Bonus: what-if counterfactual projections and regulatory package generation

