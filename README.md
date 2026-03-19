# Ledger Agentic Event Store

Production-style implementation of **TRP1 Week 5: The Ledger**.

This repository implements the challenge end-to-end across:
- Event store core (PostgreSQL + optimistic concurrency)
- Domain aggregates and command handlers
- Projections + async projection daemon
- Upcasting, integrity hash chain, and Gas Town recovery
- MCP tools/resources surface
- Bonus what-if projection and regulatory package generation
- Phase 7 production surface: FastAPI + React operations dashboard

## Implementation Status

All code phases are implemented:
- Phase 0: `DOMAIN_NOTES.md`, `DESIGN.md`
- Phase 1: schema + event store core + concurrency test
- Phase 2: aggregates + command handlers
- Phase 3: projections + daemon + lag + rebuild
- Phase 4: upcasting + integrity + Gas Town recovery
- Phase 5: MCP tools/resources + lifecycle test
- Phase 6: what-if projector + regulatory package
- Phase 7: API service + dashboard UI for interactive operations
- Phase 7.1: JWT auth + RBAC + auth audit trail + role-aware dashboard

Current integration validation:
- `14 passed` (full suite with database running)

## Architecture Diagram

```mermaid
flowchart TB
  U[User or Dashboard] --> API[FastAPI API]

  API --> AUTH[JWT auth RBAC auth audit log]
  AUTH --> CMD[Command endpoints]
  AUTH --> QRY[Query endpoints]

  CMD --> MCPW[Ledger MCP write side]
  QRY --> MCPR[Ledger MCP read side]

  subgraph COMMAND_SIDE[Command side MCP tools]
    T1[submit application]
    T2[start agent session]
    T3[record credit analysis]
    T4[record fraud screening]
    T5[record compliance check]
    T6[generate decision]
    T7[record human review]
    T8[run integrity check]
  end

  MCPW --> T1
  MCPW --> T2
  MCPW --> T3
  MCPW --> T4
  MCPW --> T5
  MCPW --> T6
  MCPW --> T7
  MCPW --> T8

  T1 --> V1[Tool validation]
  T2 --> V2[Tool validation]
  T3 --> V3[Tool validation]
  T4 --> V4[Tool validation]
  T5 --> V5[Tool validation]
  T6 --> V6[Tool validation]
  T7 --> V7[Tool validation]
  T8 --> V8[Tool validation]

  V1 --> H1[Write handler load validate decide]
  V2 --> H2[Write handler load validate decide]
  V3 --> H3[Write handler load validate decide]
  V4 --> H4[Write handler load validate decide]
  V5 --> H5[Write handler load validate decide]
  V6 --> H6[Write handler load validate decide]
  V7 --> H7[Write handler load validate decide]
  V8 --> H8[Write handler load validate decide]

  H1 --> AGG1[Replay aggregates before write]
  H2 --> AGG2[Replay aggregates before write]
  H3 --> AGG3[Replay aggregates before write]
  H4 --> AGG4[Replay aggregates before write]
  H5 --> AGG5[Replay aggregates before write]
  H6 --> AGG6[Replay aggregates before write]
  H7 --> AGG7[Replay aggregates before write]
  H8 --> AGG8[Replay aggregates before write]

  AGG1 --> DOCFLAG{Process documents after submit}
  AGG2 --> S1[Append AgentContextLoaded]
  AGG3 --> C1[Append CreditAnalysisCompleted]
  AGG4 --> F1[Append FraudScreeningCompleted]
  AGG5 --> CO1[Append ComplianceCheckRequested]
  AGG5 --> CO2[Append ComplianceRulePassed or ComplianceRuleFailed]
  AGG6 --> G1[Append DecisionGenerated]
  AGG7 --> HR1[Append HumanReviewCompleted]
  AGG7 --> HR2[Append ApplicationApproved or ApplicationDeclined]
  AGG8 --> I1[Append AuditIntegrityCheckRun]

  DOCFLAG -- No --> E1[Append ApplicationSubmitted]
  E1 --> E2[Append CreditAnalysisRequested]

  DOCFLAG -- Yes --> E3[Append ApplicationSubmitted]
  E3 --> REFINERY[Start Week3 refinery]

  subgraph WEEK3[Week3 refinery integration]
    REFINERY --> TRIAGE[Triage profile document]
    TRIAGE --> ROUTER[Select extraction strategy]
    ROUTER --> FAST[Fast text extraction]
    ROUTER --> LAYOUT[Layout aware extraction]
    ROUTER --> VISION[Vision augmented extraction]
    ROUTER --> XLEDGER[Write extraction ledger]
    FAST --> CHUNK[Chunk document]
    LAYOUT --> CHUNK
    VISION --> CHUNK
    CHUNK --> INDEX[Build page index]
    CHUNK --> FACTS[Extract financial facts]
    INDEX --> PAGEIDX[Write page index json]
    FACTS --> FACTDB[Write fact table sqlite]
  end

  PAGEIDX --> D1[Append ExtractionCompleted]
  FACTDB --> D1
  D1 --> D2[Append QualityAssessmentCompleted]
  D2 --> D3[Append PackageReadyForAnalysis]
  D3 --> E4[Append CreditAnalysisRequested from package ready]

  subgraph STORE_AREA[Ledger core]
    STORE[(Postgres event store)]
    OCC[Optimistic concurrency expected version]
    META[Correlation causation actor metadata]
    UPCAST[Upcaster registry on reads]
    INTEGRITY[Integrity hash chain]
    OUTBOX[Outbox schema optional not wired in main flow]
  end

  E2 --> STORE
  E3 --> STORE
  E4 --> STORE
  S1 --> STORE
  C1 --> STORE
  F1 --> STORE
  CO1 --> STORE
  CO2 --> STORE
  G1 --> STORE
  HR1 --> STORE
  HR2 --> STORE
  I1 --> STORE

  STORE --> OCC
  STORE --> META
  STORE --> UPCAST
  STORE --> INTEGRITY
  STORE --> OUTBOX

  STORE --> DAEMON[Projection daemon]
  DAEMON --> CHECKPOINTS[Projection checkpoints lag rebuild]
  DAEMON --> P1[Application summary projection]
  DAEMON --> P2[Compliance audit view projection]
  DAEMON --> P3[Agent performance projection]
  DAEMON --> LAGS[Ledger health lag state]

  subgraph QUERY_SIDE[Query side MCP resources]
    R1[applications by id]
    R2[applications compliance]
    R3[applications audit trail]
    R4[agents performance]
    R5[agents session replay]
    R6[ledger health]
  end

  MCPR --> R1
  MCPR --> R2
  MCPR --> R3
  MCPR --> R4
  MCPR --> R5
  MCPR --> R6

  R1 --> P1
  R2 --> P2
  R4 --> P3
  R6 --> LAGS
  R3 --> AUDITSTREAM[Direct audit stream load justified exception]
  R5 --> SESSIONSTREAM[Direct agent session load justified exception]
  AUDITSTREAM --> STORE
  SESSIONSTREAM --> STORE

  STORE --> WHATIF[What if projector]
  STORE --> REGPKG[Regulatory package generator]
  WHATIF --> WHATIFOUT[Counterfactual outcome comparison]
  REGPKG --> REGJSON[Self contained regulatory json package]
```

## Tech Stack

- Python 3.11+
- PostgreSQL 16 (project-local instance scripts included)
- `uv` for dependency and environment management
- Backend: `asyncpg`, `pydantic`, `fastapi`, `uvicorn`
- Frontend: React + TypeScript + Vite
- Testing: `pytest`, `pytest-asyncio`

## Repository Layout

```text
apps/
  api/
  web/
src/
  event_store.py
  schema.sql
  models/
  aggregates/
  commands/
  projections/
  upcasting/
  integrity/
  mcp/
  what_if/
  regulatory/
tests/
  test_api_auth.py
  test_api_surface.py
  test_concurrency.py
  test_phase2_aggregates.py
  test_projections.py
  test_upcasting.py
  test_integrity.py
  test_gas_town.py
  test_mcp_lifecycle.py
  test_what_if.py
  test_regulatory_package.py
migrations/
scripts/
```

## Week 3 Refinery Integration

This repo now includes a full local **Document Intelligence Refinery** pipeline under `src/refinery/`:

- Triage agent (`DocumentProfile` generation)
- Multi-strategy extraction (`fast_text`, `layout_aware`, `vision_augmented`) with confidence-gated escalation
- Strategy B (`layout_aware`) uses Docling when available, with local fallback when Docling is not installed
- Extraction ledger output at `.refinery/extraction_ledger.jsonl`
- Semantic chunking and chunk validation rules
- PageIndex generation at `.refinery/pageindex/{document_id}.json`
- Structured financial fact extraction into SQLite (`.refinery/facts.db`)
- Query interface methods: `pageindex_navigate`, `semantic_search`, `structured_query`

Run refinery on a document:

```bash
python scripts/run_refinery.py /path/to/document.pdf
```

Enable Gemini-backed vision calibration (optional):

```bash
GEMINI_API_KEY=your_key_here python scripts/run_refinery.py /path/to/document.pdf
```

or:

```bash
python scripts/run_refinery.py /path/to/document.pdf --gemini-api-key your_key_here --gemini-model gemini-2.0-flash
```

When `GEMINI_API_KEY` is not set (or if Gemini is unreachable), the pipeline falls back to deterministic local extraction.

Week 5 integration entry point (compatible with support doc import style):

```python
from document_refinery.pipeline import extract_financial_facts

facts = extract_financial_facts("/path/to/document.pdf")
```

MCP/API integration:

- `submit_application` accepts optional fields:
- `document_path` (local file path)
- `process_documents_after_submit` (`true`/`false`)
- When enabled, submit flow will:
- run refinery extraction
- append `docpkg-{application_id}` events (`ExtractionCompleted`, `QualityAssessmentCompleted`, `PackageReadyForAnalysis`)
- append `CreditAnalysisRequested` on `loan-{application_id}` after package readiness

Configurable thresholds live in:

```text
rubric/extraction_rules.yaml
```

## Prerequisites

- Python 3.11+
- `uv` installed
- PostgreSQL binaries available at `/usr/lib/postgresql/16/bin`

## Quick Start (Recommended: Project-Local DB)

1. Install dependencies:
```bash
uv sync --dev
```
or
```bash
make setup
```

2. Start the project-local PostgreSQL instance:
```bash
bash scripts/db-start.sh
bash scripts/db-status.sh
```
or
```bash
make db-start
make db-status
```

3. Ensure `.env` is present.

If missing, create one:
```bash
cp .env.example .env
```

Example values for local project DB:
```env
PGHOST=localhost
PGPORT=55432
PGUSER=postgres
PGPASSWORD=
PGDATABASE=ledger_event_store
DATABASE_URL=postgresql://postgres@localhost:55432/ledger_event_store
```

4. Apply migrations:
```bash
bash scripts/migrate.sh
```
or
```bash
make migrate
```

5. Start API service:
```bash
bash scripts/api-start.sh
```
or
```bash
make api
```

6. Run tests:
```bash
DATABASE_URL=postgresql://postgres@localhost:55432/ledger_event_store .venv/bin/pytest -q
```
or
```bash
make test
```

7. Stop local DB when done:
```bash
bash scripts/db-stop.sh
```
or
```bash
make db-stop
```

## Phase 7: API + Dashboard

### Backend API

- Entry module: `apps/api/main.py`
- Start command: `bash scripts/api-start.sh`
- Default base URL: `http://127.0.0.1:8000/api/v1`

Important routes:
- `POST /auth/login`, `GET /auth/me`, `GET /auth/audit`
- `GET /health`
- `POST /commands/{tool_name}` (all 8 tool commands)
- `POST /bootstrap/demo` (creates full end-to-end demo lifecycle)
- `GET /applications`, `GET /applications/{id}`
- `GET /applications/{id}/compliance`
- `GET /agents/{id}/performance`
- `GET /ledger/health`
- `GET /events/recent`
- `GET /metrics` (Prometheus-style text)
- `GET /stream/lag` (SSE lag stream)

API env knobs:
```env
DATABASE_URL=postgresql://postgres@localhost:55432/ledger_event_store
API_HOST=127.0.0.1
API_PORT=8000
API_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173,http://0.0.0.0:5173,http://[::1]:5173
API_APPLY_SCHEMA_ON_START=true
# LEDGER_API_KEY=optional-api-key
GEMINI_API_KEY=optional-gemini-key
GEMINI_MODEL=gemini-2.0-flash
JWT_SECRET=replace-with-long-random-secret
JWT_ISSUER=ledger-api
JWT_TTL_MINUTES=120
SEED_DEMO_USERS=true
```

If your dashboard is opened via LAN IP (example `http://10.69.158.161:5173`), add that origin to
`API_CORS_ORIGINS` in `.env` and restart API.

Default seeded users when `SEED_DEMO_USERS=true`:
- `analyst / analyst123!`
- `compliance / compliance123!`
- `ops / ops123!`
- `admin / admin123!`

Role policy highlights:
- `analyst`: can run application/agent/decision commands, cannot run integrity checks or rebuild
- `compliance`: can run compliance + integrity commands and view auth audit logs
- `ops`: can run operational write commands and rebuild projections
- `admin`: full access

### React Dashboard

1. Install dashboard deps:
```bash
cd apps/web
npm install
```
or from repo root:
```bash
make web-install
```

2. Optional env:
```bash
cp .env.example .env
```

3. Start dashboard:
```bash
cd ../..
bash scripts/dashboard-start.sh
```
or from repo root:
```bash
make web
```

4. Open:
- `http://localhost:5173`

The dashboard provides:
- Login/logout screen with JWT session
- Role-based tool visibility
- Compliance/admin-only auth audit panel
- One-click demo scenario generation
- Command console for tool invocation
- Application/compliance views
- Agent performance view
- Projection lag and recent event feed

## pgAdmin Connection

If you want to inspect tables in pgAdmin, use:
- Host: `127.0.0.1`
- Port: `55432`
- Username: `postgres`
- Password: your configured postgres password
- Database: `ledger_event_store`

If pgAdmin points to `5432`, it will fail unless a separate system PostgreSQL is running there.

## Migration Flow

- Migration files live in `migrations/` and are applied in filename order.
- `src/schema.sql` is the canonical latest schema snapshot.
- Migrations are idempotent (`IF NOT EXISTS`) and can be re-run safely.

Run migrations:
```bash
bash scripts/migrate.sh
```

## MCP Surface

The MCP layer is implemented in `src/mcp/` as an in-process server interface:
- Tools (command side): 8
- Resources (query side): 6
- Structured typed errors with `suggested_action`
- Lifecycle covered by `tests/test_mcp_lifecycle.py`

### Minimal usage example

```python
import asyncio
from src.event_store import EventStore
from src.mcp.server import LedgerMCPServer

async def main():
    store = await EventStore.from_dsn("postgresql://postgres@localhost:55432/ledger_event_store")
    server = LedgerMCPServer(store=store, auto_project=True)
    await server.initialize()

    result = await server.call_tool(
        "submit_application",
        {
            "application_id": "app-123",
            "applicant_id": "customer-1",
            "requested_amount_usd": 10000,
            "loan_purpose": "equipment",
            "submission_channel": "portal",
            "submitted_at": "2026-03-17T12:00:00+00:00",
        },
    )
    print(result)
    await store.close()

asyncio.run(main())
```

## Testing

Run everything:
```bash
DATABASE_URL=postgresql://postgres@localhost:55432/ledger_event_store .venv/bin/pytest -q
```

Run key integration slices:
```bash
.venv/bin/pytest -q tests/test_api_auth.py
.venv/bin/pytest -q tests/test_api_surface.py
.venv/bin/pytest -q tests/test_concurrency.py
.venv/bin/pytest -q tests/test_projections.py
.venv/bin/pytest -q tests/test_mcp_lifecycle.py
.venv/bin/pytest -q tests/test_what_if.py tests/test_regulatory_package.py
```

## Troubleshooting

### `db-start` says another server might already be running

Check status first:
```bash
bash scripts/db-status.sh
```

If status says `accepting connections`, you can continue.

### Connection refused on `127.0.0.1:5432`

This project-local DB runs on `55432`, not `5432`.
Update your connection string/pgAdmin server accordingly.

### Tests are skipped

Integration tests skip when `DATABASE_URL` is missing or PostgreSQL is unreachable.
Set `DATABASE_URL` and start DB first.

## Notes

- Root-level `models/` is an unused scaffold folder; active models are in `src/models/`.
- Local DB data and runtime files are ignored via `.gitignore` under `.local/postgres/`.
