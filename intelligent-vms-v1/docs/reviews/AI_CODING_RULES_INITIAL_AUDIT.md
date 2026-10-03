# AI Coding Rules — Initial VMS Compliance Audit

Date: 2026-09-27
Scope: intelligent-vms-v1 current main at the start of this audit.
Authority: docs/program/AI_CODING_RULES.md

## Executive result

Overall status: **PARTIALLY COMPLIANT — remediation required before claiming full compliance.**

The existing VMS has strong architecture, security boundaries, phase-gated testing, versioned APIs,
pinned dependencies, distributed ownership/fencing, and performance-evidence discipline. It was,
however, written before the 67-rule coding standard became mandatory. The audit found legacy
violations that must not be copied into new work.

This is an initial codebase audit based on repository-wide pattern searches plus direct review of
representative control API, ONVIF, recording, auth, configuration, benchmark, test, CI, Helm, and
dependency files. It is not a claim that every function has already received a manual line-by-line
review.

## Rule-level status

| Level | Area | Status | Initial finding |
|---|---|---|---|
| 1 | Syntax & formatting | PARTIAL | Existing style is mostly consistent, but there is no dedicated formatter/linter gate proving all style rules. |
| 2 | Naming | PASS / spot-check | Core names are generally descriptive and casing is consistent. Continue enforcing in review. |
| 3 | Function/unit design | PARTIAL | Several route/service functions combine orchestration, persistence, remote calls and rollback; touched code should be decomposed when safe. |
| 4 | Error handling | FAIL | Silent best-effort exception paths exist and violate rule 17. |
| 5 | Comments & documentation | FAIL | Public classes/functions broadly lack the required purpose/parameters/return/exceptions docstrings. |
| 6 | Testing | PARTIAL / STRONG | Phase tests are extensive and CI runs pytest, but some tests use real wall-clock time rather than injected/mocked time. |
| 7 | File/module organization | PASS | Control API, services, workers, tools, migrations and tests have clear separation. |
| 8 | Dependencies | PASS / advisory scan pending | Python requirements are version-pinned. Current vulnerability/advisory status still requires a dedicated dependency-security scan. |
| 9 | Security | PARTIAL / STRONG | RBAC, tenant/site scope, endpoint validation, secret redaction and parameterized SQL are strong. Predictable development encryption-key fallback and broad CORS need remediation/review. |
| 10 | Performance | PASS / evidence-gated | Bounded batches, no unsupported 100K claims, benchmark harness and measured-evidence rules align well. |
| 11 | Version-control hygiene | PASS | Existing branch/PR/CI/Reviewer workflow is strong and now references the coding rules. |
| 12 | API/interface design | PARTIAL | /api/v1 is versioned, but HTTP error detail shape is inconsistent between plain strings and structured objects. |
| 13 | Data handling | PARTIAL / STRONG | Pydantic boundary typing and bounded inputs are strong; full privacy/retention controls remain feature-roadmap work. |
| 14 | Architecture/design | PASS | Control/media separation, regional execution, fencing and independent AI scheduling are aligned with the rules. |
| 15 | Product/business alignment | PASS | Production Execution Program and market feature catalog keep implementation tied to actual product goals and evidence. |

## Confirmed remediation findings

### HIGH — predictable encryption-key fallback

File:
- services/control-api/app/core/config.py

Finding:
- vms_secret_key currently defaults to "development-only-change-me".
- Production Helm documentation correctly requires VMS_SECRET_KEY from an existing Kubernetes Secret,
  so this audit does not claim a production compromise.
- The in-code fallback still violates rule 40 and creates an unsafe failure mode if deployment
  configuration is incomplete.

Required correction:
- remove the usable hardcoded key fallback;
- fail clearly when credential encryption/decryption is requested without a configured key;
- preserve local/test usability through explicit test/dev environment injection;
- add regression tests for missing-key behavior.

Security impact: authentication/credential encryption related; must receive Security + Reviewer sign-off.

### HIGH — silent exception swallowing

Confirmed examples:
- services/control-api/app/routers/recordings.py
  - best-effort recording cleanup uses except Exception: pass;
  - existing-assignment NodeEndpointError can be ignored without an observable record.
- services/control-api/app/services/onvif_events.py
  - unsubscribe cleanup catches Exception and passes.
- services/control-api/app/services/onvif_discovery.py
  - malformed discovery responses catch Exception and continue without observability.
- tools/phase8_reconnect_benchmark.py
  - writer.wait_closed cleanup catches Exception and passes.

Rule impact:
- violates Level 4 rule 17: never silently swallow errors.

Required correction:
- retain safe best-effort behavior where intentional, but log/count the failure with non-sensitive
  context;
- use narrower expected exception types where practical;
- add regression tests where behavior affects product state.

### MEDIUM — public API documentation debt

Representative files:
- services/control-api/app/core/auth.py
- services/control-api/app/services/onvif_events.py
- services/control-api/app/services/network_policy.py
- services/control-api/app/models/schemas.py
- routers and service modules broadly

Finding:
- public classes/functions generally do not provide the rule-23 docstrings describing purpose,
  parameters, returns and exceptions.

Required correction:
- apply docstrings when a module is touched;
- run a dedicated documentation-debt workstream for existing public interfaces;
- do not add new public functions/classes without compliant documentation.

### MEDIUM — API error response inconsistency

Representative example:
- services/control-api/app/routers/recordings.py returns both string detail values and a structured
  {"code", "message"} detail for different errors.

Rule impact:
- Level 12 rule 53 requires a consistent error/response shape.

Required correction:
- define one versioned API error schema and migrate endpoints without silently breaking callers;
- document compatibility behavior and test response shapes.

### MEDIUM — deterministic-test debt

Representative example:
- tests/test_auth_phase2b.py constructs token expiry using time.time();
- several phase tests use datetime.now(timezone.utc).

Rule impact:
- Level 6 rule 29 says tests must not rely on real time without mocking/injection.

Required correction:
- inject/freeze clock values in tests where wall-clock behavior matters;
- keep true benchmark/soak tools separate from deterministic CI tests.

### MEDIUM — least-privilege CORS review

File:
- services/control-api/app/main.py

Finding:
- CORSMiddleware currently allows all origins and all methods/headers, with credentials disabled.

Required correction:
- make allowed origins/methods/headers explicit production configuration;
- retain a documented local-development profile if wildcard behavior is useful there;
- add configuration tests.

Security impact: browser/API access policy; Security review required.

## Existing strengths to preserve

- SQLAlchemy expression-based queries are parameterized rather than concatenated SQL.
- ONVIF/RTSP target validation limits SSRF/network reachability.
- tenant/site and node-scoped authorization is explicit.
- node-agent exception text is redacted before logging.
- dependencies in requirements files are pinned.
- API routes are already versioned under /api/v1 and internal/v1.
- recording is kept independent from AI orchestration.
- distributed placement uses generations, leases and fencing.
- Phase 8 prevents unsupported hardware/100K/GPU claims.
- CI runs compile, pytest, migration validation, feature-catalog verification, chaos qualification,
  benchmark smoke, Compose validation, Helm validation and image builds.

## Enforcement from this change forward

New or modified VMS code must:
1. read docs/program/AI_CODING_RULES.md;
2. comply with all applicable rules;
3. explicitly report security/auth/authorization/encryption/PII impact;
4. add tests for new logic;
5. fix or explicitly report legacy violations in any touched path;
6. use the priority Security -> Correctness -> Product alignment -> Architecture -> Readability/style.

No future milestone may be called coding-rule compliant solely because CI is green.

## Recommended remediation order

1. Security/high-risk legacy gaps: encryption-key fallback, CORS production policy.
2. Correctness/error handling: silent catches and cleanup observability.
3. API consistency: standardized error contract.
4. Deterministic tests/clock injection.
5. Public docstring debt and style/readability cleanup.
6. Add automated checks incrementally after legacy violations are removed, so CI enforcement does not
   encourage hiding or mass-suppressing debt.
