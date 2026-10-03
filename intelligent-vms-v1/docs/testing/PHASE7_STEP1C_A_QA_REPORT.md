# Phase 7 Step 1C-A QA Report (Independent)

Date: 2026-09-26  
Scope: node-agent + node-scoped auth hardening on current branch

## Result
**PASS** (with one non-blocking precondition note on compose env file)

## Evidence

### 1) Python compile success (services/tests)
Command:
`python -m compileall -q intelligent-vms-v1/services intelligent-vms-v1/tests`

Result:
- `compileall_ok`
- Exit code `0`

### 2) Focused tests (node-agent + auth hardening)
Command:
`python -m pytest -q intelligent-vms-v1/tests/test_node_agent_phase7_step1c.py intelligent-vms-v1/tests/test_auth_phase2b.py`

Result:
- `17 passed`
- Exit code `0`
- Warning observed: HS256 test secret length below RFC recommendation (test fixture only)

### 3) Full intelligent-vms-v1 test suite
Command:
`python -m pytest -q intelligent-vms-v1/tests`

Result:
- `63 passed`
- Exit code `0`
- Same HS256 key-length warning in auth test fixture

### 4) Alembic upgrade head check
Command:
`cd intelligent-vms-v1 && alembic -c alembic.ini upgrade head`

Result:
- Upgrades applied through `0007 (head)` successfully
- Exit code `0`

Idempotency/current-schema check:
`cd intelligent-vms-v1 && alembic -c alembic.ini upgrade head && alembic -c alembic.ini current`

Result:
- Current revision reported: `0007 (head)`
- Exit code `0`

### 5) Compose config validity (without/with node-agent profile)
Initial attempt without `.env`:
- `docker compose ... config -q` failed because `.env` missing.

Deterministic fixture used:
- copied `.env.example` -> `.env` temporarily for validation.

Commands:
- `docker compose -f compose.yaml config -q`
- `docker compose -f compose.yaml --profile regional-node-agent config -q`

Result:
- both commands passed, exit code `0`

Service list evidence:
- Default config excludes `node-agent`
- `--profile regional-node-agent` includes `node-agent`

### 6) Node-agent image build check
Command:
`cd intelligent-vms-v1 && docker build -f services/node-agent/Dockerfile -t vms-node-agent:qa .`

Result:
- Build completed successfully
- Image tagged: `vms-node-agent:qa`
- Exit code `0`

## Regression Risks / Notes

1. **Compose precondition**: `compose.yaml` expects `.env` via `env_file`. Running config validation without creating `.env` fails.  
   - Recommendation: CI QA step should explicitly seed `.env` from `.env.example` (or provide env file path) before compose validation.

2. **Auth test fixture warning**: HS256 test key is intentionally short and emits `InsecureKeyLengthWarning`.  
   - Recommendation: use >=32-byte fixture secret to keep security lint/warning budgets clean.

## Required Fixes
- **No release-blocking failures found in this QA run.**

## Suggested Regression Tests to Keep
- `tests/test_node_agent_phase7_step1c.py`
- `tests/test_auth_phase2b.py`
- Full suite `tests/` gate in CI
- Alembic `upgrade head` + `current` check
- Compose config check with and without `regional-node-agent` profile (with deterministic `.env` fixture)
