# ADR-0002 — Python + FastAPI for the control plane

**Status:** Accepted

## Context

The baseline suggested "Python/FastAPI or Go". The control plane is I/O-bound
coordination: database writes, ONVIF calls, subprocess supervision. It is not
on the media data path — no video bytes flow through this process.

## Decision

Python 3.11+ with FastAPI, Pydantic v2 and SQLAlchemy 2.0.

## Reasoning

- The OpenAPI contract is generated from the same Pydantic models that validate
  requests, so the published contract cannot drift from the implementation.
- The ONVIF/RTSP/media ecosystem tooling is strongest in Python.
- Request rates are hundreds per minute, not hundreds of thousands. Python's
  throughput ceiling is irrelevant here.

## Consequences

**Costs, stated plainly:** Python is 10–50x slower than Go for CPU-bound work,
so no CPU-bound work is permitted in this process. Frame decoding is FFmpeg's
job; transport is MediaMTX's job; inference belongs in a separate worker.
Deployment carries a Python runtime rather than a single static binary, which
the installers must handle.

If profiling ever shows the control plane CPU-bound, the correct response is to
move that work out of the process, not to rewrite the API.
