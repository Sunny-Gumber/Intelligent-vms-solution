# ADR-0014: Stream-copy exports with SHA-256 and chain of custody

**Status:** Accepted · **Date:** 2026-10-19

## Context

An export is often the only artefact that leaves the system, and it may end up
in a legal proceeding. It must be provably the same video that was recorded.

## Decision

1. Exports **stream-copy** by default (`-c copy`), so the output is
   bit-identical to the recorded frames.
2. `-ss` is placed **after** `-i` (accurate seek), accepting the extra decode
   cost.
3. The output is SHA-256 hashed at finalization; the hash, the exact source
   segment ids and the **actual** covered time range are stored.
4. Chain of custody is append-only: `requested` → `produced` →
   `download_token_issued` → `downloaded`, each with actor, UTC timestamp and
   client IP.
5. If any source file is missing, the export **fails** with
   `incomplete_media`. It never produces a shorter clip.

## Rationale

`-ss` before `-i` is dramatically faster but seeks to the nearest keyframe and
can shift the clip by seconds. For evidence, a silent time shift is worse than
a slow export.

Point 5 is the most important rule in the module. An export that quietly omits
the three minutes where the index was damaged looks complete and is
evidentially worthless.

## Consequences

**Good**
- Exports are fast (an hour of H.265 copies in seconds) and lossless.
- The hash is independently recomputed and asserted in
  `test_export_produces_hashed_evidence`.
- The job records both requested and actual ranges, so an operator can see
  they received 14:03:12–14:05:00 when they asked for 14:00:00–14:05:00.

**Bad / accepted cost**
- Accurate seek decodes from the preceding keyframe, so a clip starting
  mid-GOP costs more CPU.
- Stream copy means the container may start on a non-keyframe boundary; some
  players show a brief artefact at the very start.
- Refusing partial exports will frustrate operators whose storage has already
  degraded. That is the correct trade for a security product, and the error
  names the missing segments so it can be investigated.
