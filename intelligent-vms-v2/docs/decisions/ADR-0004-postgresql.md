# ADR-0004 — PostgreSQL in production, SQLite for development and tests

**Status:** Accepted

## Context

A VMS writes a segment index row every few minutes per camera, forever. At 256
cameras with 5-minute segments that is ~74,000 rows/day and tens of millions
over a retention period, with time-range queries over them.

## Decision

PostgreSQL is the only supported production database. SQLite is supported for
developer checkouts and the automated test suite. Production startup **refuses
to run on SQLite** (`app/config.py::_production_safety`).

Portability is achieved in the schema itself:
- `String(32)` hex UUID primary keys generated in Python — no DB extension.
- `JSON` with a PostgreSQL `JSONB` variant.
- `BigInteger` for every byte count (PostgreSQL `INTEGER` is 32-bit; disk sizes
  pass 2 GB immediately).
- A `UtcDateTime` type decorator that normalizes to aware UTC on both sides.

## Consequences

**Good:** `git clone && pytest` works with no database server, and every test
builds its schema from the shipped Alembic migration, so the migration is
continuously exercised.

**Costs:** the test suite does not exercise PostgreSQL-specific behaviour
(JSONB operators, concurrent write contention, `SELECT … FOR UPDATE`
semantics). Running the suite against PostgreSQL is Gate 4 of the external
qualification plan. Until then, "works on SQLite" is not "works on PostgreSQL".
