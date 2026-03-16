# DOMAIN_NOTES.md

## Scope and assumptions
- Scenario: Apex Financial Services commercial loan decisioning platform.
- Streams follow the challenge naming convention (`loan-*`, `agent-*`, `compliance-*`, `audit-*`).
- Command handlers always validate against aggregate state reconstructed from the stream.

## 1) EDA vs ES distinction
The callback-style trace capture used in many agent frameworks is **Event-Driven Architecture (EDA)**, not Event Sourcing.

Why: callbacks are notifications around mutable process state; they are not the authoritative persistence model. If the process or trace sink fails, history can be incomplete while business state still changes elsewhere.

If redesigned with The Ledger:
- Every domain fact (submission, analysis result, compliance rule result, decision, review) is appended to an immutable stream first.
- The event stream becomes the source of truth; projections become rebuildable read models.
- Agent traces move into event `metadata` (correlation and causation) instead of being an external side channel.

What is gained:
- Deterministic replay and reproducibility.
- Complete audit history with temporal queries.
- Explicit concurrency safety (`expected_version`).
- Cross-system integration via outbox without dual-write risk.

## 2) The aggregate question
Alternative boundary considered and rejected: merge `ComplianceRecord` into `LoanApplication` so all application lifecycle facts are in one stream.

Why rejected:
- It couples high-frequency rule evaluation writes to lower-frequency lifecycle transitions.
- It increases write contention on `loan-{application_id}`.
- It mixes regulatory rule versioning concerns with business lifecycle concerns.

Coupling problem prevented by separate aggregates:
- A compliance burst (many `ComplianceRulePassed/Failed` events) cannot starve lifecycle transitions like `DecisionGenerated` or `HumanReviewCompleted`.
- OCC conflicts remain localized to the relevant consistency boundary instead of amplifying across all workflows touching the same application.

## 3) Concurrency in practice (`expected_version=3`)
Exact sequence with two agents appending to the same stream:
1. Agent A and Agent B both load `loan-123`, see version `3`.
2. Both compute a new event and call `append_events(..., expected_version=3)`.
3. Agent A transaction validates current stream version is `3`, inserts event at position `4`, updates `event_streams.current_version` to `4`, commits.
4. Agent B transaction validates stream version and finds actual version `4`, not `3`.
5. Agent B receives `OptimisticConcurrencyError` with:
   - `stream_id=loan-123`
   - `expected_version=3`
   - `actual_version=4`
   - `suggested_action=reload_stream_and_retry`
6. Agent B must reload stream state, re-run business validation, and then either:
   - append a revised event with `expected_version=4`, or
   - emit no event if the new state makes the action invalid/redundant.

## 4) Projection lag and consequences
Given `LoanApplication` projection lag of ~200 ms:
- Write side remains correct immediately after commit.
- Read model may briefly return stale values (for example, old available credit).

System behavior:
- Query response includes staleness metadata (`projection_lag_ms`, `projection_position`, `latest_global_position`).
- For critical reads, API supports `wait_for_projection=true` with a bounded wait budget (for example 300 ms).
- If budget is exceeded, response returns current projection plus a staleness flag.

UI communication:
- Show "Recent update processing..." when staleness flag is present.
- Render last updated timestamp and auto-refresh.
- Avoid presenting stale values as final by labeling them as "pending projection update."

## 5) Upcasting scenario (`CreditDecisionMade`)
Legacy shape (2024):
- `{application_id, decision, reason}`

Target shape (2026):
- `{application_id, decision, reason, model_version, confidence_score, regulatory_basis}`

Upcaster:
```python
from typing import Any

def upcast_credit_decision_v1_to_v2(payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    model_version = metadata.get("model_version")

    # Fallback inference only when there is one unambiguous deployment for the timestamp.
    if model_version is None:
        decision_ts = metadata.get("decision_timestamp")
        model_version = infer_model_from_release_calendar(decision_ts)  # returns str | None

    return {
        "application_id": payload["application_id"],
        "decision": payload["decision"],
        "reason": payload["reason"],
        "model_version": model_version,          # null if unknown
        "confidence_score": None,                # not recoverable from v1 reliably
        "regulatory_basis": metadata.get("regulatory_basis", "LEGACY_EVENT_NO_BASIS"),
    }
```

Inference strategy for historical events:
- Prefer exact metadata if present.
- Infer from release calendar only when unambiguous.
- Otherwise set `model_version=null` (do not fabricate).
- Attach inference metadata (`inference_method`, `inference_confidence`) so downstream compliance tooling can distinguish inferred from observed values.

## 6) Marten Async Daemon parallel in Python
Equivalent distributed projection execution approach:
- Use a `projection_workers` lease table plus PostgreSQL advisory locks.
- Split workload by projection name and global-position ranges (or stream hash shards).
- Workers claim shards with lease TTL and heartbeat updates.
- On crash/timeout, lease expires and another worker safely resumes from stored checkpoint.

Coordination primitive:
- Database-backed lease + advisory lock (`pg_try_advisory_lock`) per projection shard.

Failure mode this guards against:
- **Split-brain projection execution** (two nodes writing the same projection partition concurrently), which causes duplicate/non-deterministic read model state.
- Lease expiry also handles orphaned work when a node dies mid-batch.

