# TRP1 Week 5: High-Level Implementation Checklist

## 0) Foundation setup
- [ ] Set up project scaffold (`src/`, `tests/`, configs)
- [ ] Configure Python deps and lock with `uv`
- [ ] Configure PostgreSQL locally and connection settings
- [ ] Add `README.md` with run/test instructions

## 1) Domain and architecture definition
- [ ] Write `DOMAIN_NOTES.md` answering all required scenario questions
- [ ] Write initial `DESIGN.md` with tradeoffs and architecture decisions
- [ ] Define aggregate boundaries and stream ID strategy
- [ ] Confirm event catalogue coverage and identify missing events

## 2) Event store core (Phase 1)
- [ ] Implement schema (`events`, `event_streams`, checkpoints, outbox, indexes)
- [ ] Build async `EventStore` interface (append/load/version/archive/metadata)
- [ ] Enforce optimistic concurrency with `expected_version`
- [ ] Add base event models and typed exceptions
- [ ] Add concurrency collision test (exactly one winner)

## 3) Domain logic and command flow (Phase 2)
- [ ] Implement aggregates: `LoanApplication`, `AgentSession`, `ComplianceRecord`, `AuditLedger`
- [ ] Implement replay-based state loading and invariant enforcement
- [ ] Build command handlers using load -> validate -> append pattern
- [ ] Ensure Gas Town session preconditions for agent decision events

## 4) Projections and async daemon (Phase 3)
- [ ] Implement projection daemon with per-projection checkpoints
- [ ] Implement projections: application summary, compliance audit, agent performance
- [ ] Expose projection lag metrics and health checks
- [ ] Support rebuild-from-scratch and idempotent handlers
- [ ] Validate SLO-aligned query behavior

## 5) Upcasting, integrity, and recovery (Phase 4)
- [ ] Implement upcaster registry and version chains
- [ ] Add upcasters for required event version migrations
- [ ] Prove immutability (raw stored payload stays unchanged)
- [ ] Implement audit hash-chain verification and tamper detection
- [ ] Implement agent context reconstruction after restart

## 6) MCP server interface (Phase 5)
- [ ] Implement 8 command tools with precondition docs
- [ ] Implement 6 query resources backed by projections
- [ ] Return structured typed errors with recovery hints
- [ ] Pass full lifecycle integration test via MCP only

## 7) Bonus advanced capabilities (Phase 6)
- [ ] Build what-if projector with causal dependency filtering
- [ ] Build regulatory package generator (self-contained JSON)
- [ ] Demonstrate counterfactual outcome divergence

## 8) Submission and demo readiness
- [ ] Finalize `DOMAIN_NOTES.md` and `DESIGN.md`
- [ ] Add architecture diagram and evidence artifacts
- [ ] Ensure all required tests pass
- [ ] Prepare 6-minute demo covering required steps

