# DESIGN.md

## 1) Aggregate Boundary Justification
`ComplianceRecord` is intentionally separate from `LoanApplication`.

Reason:
- `LoanApplication` owns lifecycle transitions and credit decision invariants.
- `ComplianceRecord` owns mandatory check completeness and regulation-version traceability.

If merged, coupling failure appears under concurrency:
- Compliance engines can emit many rule events in bursts.
- Human review and orchestration writes contend with those bursts on one stream.
- Result: higher OCC collision rates on lifecycle events, delayed final decisions, and avoidable retry storms.

Specific failure mode prevented:
- A `HumanReviewCompleted` append racing a batch of compliance rule events repeatedly loses OCC and misses UI/API latency targets, even though the domain concerns are logically separate.

## 2) Projection Strategy (Inline vs Async + SLO)
Projection choices:
- `ApplicationSummary`: **Async**, p99 target `<50ms` query.
  - Justification: write path should stay low-latency; summary can tolerate sub-second eventual consistency.
- `ComplianceAuditView`: **Async**, p99 target `<200ms` query with temporal lookup.
  - Justification: temporal query workload is read-heavy and replay-friendly.
- `AgentPerformanceLedger`: **Async**, p99 target `<50ms` query.
  - Justification: analytical metrics, no write-time consistency requirement.
- `LedgerHealth`: **Direct checkpoint query**, p99 target `<10ms`.
  - Justification: simple read over checkpoint table for operational monitoring.

Snapshot strategy for `ComplianceAuditView`:
- Hybrid trigger:
  - every 100 events per `compliance-{application_id}` stream, or
  - every 24 hours, whichever comes first.

Snapshot invalidation logic:
- Invalidate and rebuild if:
  - upcaster chain for any compliance event changes,
  - projection schema version changes,
  - detected checkpoint corruption,
  - manual backfill introduces historical corrections.

## 3) Concurrency Analysis
Assumptions at peak:
- 100 active applications in flight.
- 4 agents per application.
- ~3 writes/minute on `loan-{id}` stream (analysis outcome, decision generation, human review).
- ~300 loan-stream append attempts/minute.

Expected OCC errors:
- Estimated conflict probability per append attempt: ~6%.
- Estimated OCC errors: `300 * 0.06 = 18` errors/minute.

Retry strategy:
- Exponential backoff with full jitter: 25 ms, 75 ms, 175 ms, 400 ms.
- Reload aggregate state before each retry.
- Max retry budget: 4 retries (5 total attempts).
- If exhausted, return typed failure:
  - `error_type=OptimisticConcurrencyError`
  - `suggested_action=manual_orchestrator_retry`

Rationale:
- Keeps median retries fast while capping tail latency and reducing synchronized retry storms.

## 4) Upcasting Inference Decisions
Primary upcasters:
- `CreditAnalysisCompleted v1 -> v2`
- `DecisionGenerated v1 -> v2`

Inferred fields and risk:
- `model_version` inferred from metadata/release window:
  - estimated error rate: 2-5%
  - consequence: moderate (incorrect attribution in model audits)
- `confidence_score` inferred from legacy tier mapping:
  - estimated error rate: 10-15%
  - consequence: high (can distort retrospective risk analytics)
- `regulatory_basis` inferred from regulation set in metadata:
  - estimated error rate: 3-8%
  - consequence: high for compliance narratives if wrong

Null vs inference rule:
- Use `null` when inference is ambiguous or could change regulatory interpretation.
- Infer only when mapping is deterministic and provenance can be recorded.
- Always annotate inference metadata (`inference_method`, `inference_confidence`).

## 5) EventStoreDB Comparison
Mapping:
- PostgreSQL `events.stream_id` -> EventStoreDB stream ID
- `events.global_position` + `load_all()` -> EventStoreDB `$all` subscription
- `event_streams.current_version` -> EventStoreDB stream revision
- Python `ProjectionDaemon` + `projection_checkpoints` -> EventStoreDB persistent subscriptions
- Postgres outbox publisher -> ESDB integrations/subscriber pipelines

What EventStoreDB gives natively:
- Managed persistent subscriptions and consumer group coordination
- Stream ACL patterns
- Purpose-built event APIs and operational ergonomics for projection catch-up

What PostgreSQL implementation must do explicitly:
- Build lease/checkpoint/rebalance logic for distributed projection workers
- Implement stronger subscription backpressure and retry controls
- Operate schema/index tuning to preserve append/query performance at scale

## 6) What I Would Do Differently
Single most significant decision to reconsider:
- I would separate large agent reasoning traces from hot event payload rows on day one.

Current risk:
- Storing full reasoning artifacts in `events.payload` increases row size, index bloat, and replay IO costs.

Improved approach:
- Store compact event payload + deterministic hash pointer to blob storage/object store.
- Keep only governance-critical fields in hot JSONB.
- Retain immutable linkage via content hash in event metadata.

Why this matters:
- Better write throughput, faster projection replay, and cleaner hot/cold storage separation without losing audit integrity.

## 7) DocumentPackage as a Future Aggregate Boundary
Current implementation:
- The system currently runs with four aggregates: `LoanApplication`, `AgentSession`, `ComplianceRecord`, and `AuditLedger`.

Observation:
- Document-related events form a distinct lifecycle: `PackageCreated`, `DocumentAdded`, `DocumentFormatValidated`, `ExtractionStarted`, `ExtractionCompleted`, `QualityAssessmentCompleted`, `PackageReadyForAnalysis`, `DocumentUploadRequested`, and `DocumentUploaded`.

Problem:
- These events represent their own state transitions and business rules (document validation, extraction flow, quality checks, and readiness gating).
- They are not purely loan lifecycle transitions even when they are correlated to a loan application.

Conclusion:
- `DocumentPackage` is a natural aggregate boundary because it owns:
- document state
- extraction progress
- quality assessment
- readiness for analysis

Current decision:
- The implementation intentionally keeps the current four-aggregate model for delivery stability and to avoid introducing migration risk in the current release.

Future refinement:
- Extract `DocumentPackage` as a 5th aggregate with its own stream.
- Example stream format: `docpkg-{package_id}`.

Event ownership mapping (current vs future):

| Event | Current Aggregate | Future Aggregate |
|---|---|---|
| `DocumentUploadRequested` | `LoanApplication` | `DocumentPackage` |
| `DocumentUploaded` | `LoanApplication` | `DocumentPackage` |
| `PackageCreated` | (implicit / document workflow stream) | `DocumentPackage` |
| `DocumentAdded` | (implicit / document workflow stream) | `DocumentPackage` |
| `DocumentFormatValidated` | (implicit / document workflow stream) | `DocumentPackage` |
| `ExtractionStarted` | (implicit / document workflow stream) | `DocumentPackage` |
| `ExtractionCompleted` | (implicit / document workflow stream) | `DocumentPackage` |
| `QualityAssessmentCompleted` | (implicit / document workflow stream) | `DocumentPackage` |
| `PackageReadyForAnalysis` | (implicit / document workflow stream) | `DocumentPackage` |
