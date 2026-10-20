# ADR-0008 — AES-256-GCM credential vault with an out-of-database master key

**Status:** Accepted

## Context

Camera passwords cannot be hashed — the product must replay them to the device.
They must therefore be *encrypted*, reversibly, and a database dump alone must
not be enough to recover them.

## Decision

- AES-256-GCM via `cryptography`'s AESGCM.
- The 32-byte master key lives in `<data_root>/keys/vms-master.key`, mode 0600,
  **outside the database**.
- Ciphertext format: `v1 | key_id(4) | nonce(12) | ciphertext+tag`, so keys can
  rotate while old secrets stay readable.
- **Additional authenticated data binds each secret to its owner**
  (`camera:<id>`, `stream:<id>`, `mfa:<user>`, `channel:<id>`). A blob copied to
  another row fails authentication instead of silently decrypting.

## Consequences

**Good:** a stolen database dump yields no camera passwords. Tampering is
detected, not silently tolerated. Rotation does not require re-encrypting
everything at once.

**Costs, stated plainly:** **losing the master key makes every stored camera
credential permanently unreadable.** This is called out in `docs/LIMITATIONS.md`
and in the disaster-recovery procedure. The key must be backed up separately
from the database — if they are backed up together, the separation is lost.

A production hardening step not yet taken is to source the master key from an
HSM or KMS instead of a file.
