# ADR-0003 — Shared contract package and additive-only evolution

**Status:** Accepted

## Context

Four consumers must agree on types: the control API, the recording service, the
React client and the .NET client. Divergent enum spellings between a server and
a client is a classic, hard-to-debug VMS integration failure.

## Decision

A standalone package, `vms-contracts`, holds every cross-service enumeration
and DTO. Services import it; they never redefine these types.

Evolution rules inside a major version:
- Enum members may be **added**, never removed or renamed.
- DTO fields may be **added as optional**, never removed or made required.
- Breaking changes require a new module and a new API path (`/api/v2`).

## Consequences

**Good:** one place to read the vocabulary; `/api/v1/openapi.json` is generated
from it; client code generation is possible.

**Costs:** contract changes touch a shared package, so they need more care than
a local edit. The .NET and TypeScript clients currently mirror these types by
hand — a generator from the OpenAPI document is the obvious next step and is
not yet built.
