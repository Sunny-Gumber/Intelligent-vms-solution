"""Shared contracts for Intelligent VMS.

Single source of truth for cross-service enumerations and data-transfer
objects. Services MUST NOT redefine these types locally.

Contract stability rules (docs/decisions/ADR-0003-contract-versioning.md):
  * Enum members may be ADDED, never removed or renamed within a major version.
  * DTO fields may be ADDED as optional, never removed within a major version.
  * Breaking changes require a new module (vms_contracts.v2) and a new API path.
"""

from .dto import *
from .enums import *
from .version import API_PREFIX, CONTRACT_VERSION  # noqa: F401
