"""Build and product version metadata.

BUILD_* values are injected by CI (tools/stamp_build.py). In a developer
checkout they intentionally read "dev" rather than inventing a commit hash.
"""

from __future__ import annotations

import os

PRODUCT_NAME = "Intelligent VMS"
VERSION = "0.1.0-alpha"
API_VERSION = "v1"
CONTRACT_VERSION = "1.0.0"

BUILD_COMMIT = os.getenv("VMS_BUILD_COMMIT", "dev")
BUILD_NUMBER = os.getenv("VMS_BUILD_NUMBER", "0")
BUILD_DATE = os.getenv("VMS_BUILD_DATE", "unset")

# Schema revision reported to clients and used by backup/restore validation.
# Apply Alembic upgrades separately before starting a new build.
REQUIRED_SCHEMA_REVISION = "0002_media_connections"

RELEASE_MATURITY = "alpha"
# Flipped to True only when the release gates in
# docs/verification/release-gates.md have demonstrably passed.
PRODUCTION_READY = False


def version_info() -> dict[str, str | bool]:
    return {
        "product": PRODUCT_NAME,
        "version": VERSION,
        "api_version": API_VERSION,
        "contract_version": CONTRACT_VERSION,
        "commit": BUILD_COMMIT,
        "build": BUILD_NUMBER,
        "build_date": BUILD_DATE,
        "schema_revision": REQUIRED_SCHEMA_REVISION,
        "maturity": RELEASE_MATURITY,
        "production_ready": PRODUCTION_READY,
    }
