"""Evidence metadata and chain of custody.

Every export carries an auditable record: SHA-256 of the produced file
(integrity), the exact source segments (provenance), the ACTUAL covered time
range (which may be narrower than requested), and an append-only custody list
— requested -> produced -> token issued -> downloaded — each entry with actor,
UTC timestamp and client IP where known.

The custody list is append-only by construction: append_custody copies the
existing list and never mutates or removes entries.
"""

from __future__ import annotations

from typing import Any

from vms_contracts.dto import EvidenceRecord

from app.core.timeutil import utcnow


def append_custody(existing: list[dict] | None, *, action: str, actor: str,
                   actor_name: str | None = None, client_ip: str | None = None,
                   detail: dict[str, Any] | None = None) -> list[dict]:
    entry: dict[str, Any] = {"action": action, "actor": actor, "actor_name": actor_name,
                             "at": utcnow().isoformat(), "client_ip": client_ip}
    if detail:
        entry["detail"] = detail
    return [*(existing or []), entry]


def build_evidence(job) -> EvidenceRecord | None:  # noqa: ANN001
    if not job.sha256:
        return None
    return EvidenceRecord(sha256=job.sha256, size_bytes=job.size_bytes or 0,
                          created_at=job.updated_at or utcnow(),
                          created_by=job.requested_by, source_camera_id=job.camera_id,
                          source_start=job.requested_start, source_end=job.requested_end,
                          actual_start=job.actual_start, actual_end=job.actual_end,
                          segment_ids=list(job.segment_ids or []),
                          chain_of_custody=list(job.chain_of_custody or []))


def verify_file(path, expected_sha256: str) -> bool:  # noqa: ANN001
    """Re-verify an export before handing it to an investigator."""
    from app.core.crypto import constant_time_equals, sha256_file

    digest, _ = sha256_file(path)
    return constant_time_equals(digest, expected_sha256)


def coverage_shortfall(job) -> dict[str, float] | None:  # noqa: ANN001
    """How much of the requested window the export actually covers."""
    if not (job.actual_start and job.actual_end):
        return None
    requested = (job.requested_end - job.requested_start).total_seconds()
    actual = (job.actual_end - job.actual_start).total_seconds()
    if requested <= 0:
        return None
    return {"requested_s": round(requested, 2), "actual_s": round(actual, 2),
            "coverage_percent": round(actual / requested * 100.0, 2),
            "missing_s": round(max(0.0, requested - actual), 2)}
