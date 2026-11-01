# ADR-0005: Synchronous SQLAlchemy executed in FastAPI's threadpool

**Status:** Accepted · **Date:** 2026-10-19

## Context

FastAPI supports both `async def` handlers with an async ORM and `def`
handlers run in a threadpool. The control plane mixes database work with
blocking subprocess calls (`ffprobe`) and SOAP requests to cameras.

## Decision

Database-touching handlers are declared `def` (not `async def`) and use the
synchronous SQLAlchemy ORM. Handlers that are purely I/O-bound on the network
(device probes, gateway programming, PTZ) are `async def` and never hold a
database session across an await.

## Rationale

- The control plane's request rate is low — hundreds per minute, not hundreds
  of thousands. Per-request latency is dominated by device I/O, which is
  already bounded and retried.
- Mixing an async ORM with blocking subprocess calls is a well-known source of
  event-loop stalls that are hard to diagnose in production.
- The synchronous ORM is the mature, well-documented path, which matters for
  a codebase that a commercial team must maintain.

## Consequences

**Good**
- No risk of blocking the event loop with a slow query.
- Simpler, more readable transaction boundaries (`session_scope`).

**Bad / accepted cost**
- Concurrency is bounded by the threadpool size (default 40), which must be
  raised in step with the database pool for large deployments.
- Background async loops that need the database must use `session_scope()`
  inside the loop body, not hold a session across awaits — a rule reviewers
  have to enforce by reading, because the type system does not express it.
