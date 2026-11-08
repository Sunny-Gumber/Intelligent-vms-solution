"""Commercial edition definitions."""

from __future__ import annotations

from dataclasses import dataclass

from vms_contracts.enums import Edition, FeatureFlag


@dataclass(frozen=True)
class EditionSpec:
    edition: Edition
    display_name: str
    max_cameras: int
    features: frozenset[FeatureFlag]
    description: str


EDITIONS: dict[Edition, EditionSpec] = {
    Edition.LITE: EditionSpec(
        edition=Edition.LITE, display_name="Intelligent VMS Lite", max_cameras=16,
        features=frozenset({FeatureFlag.API_ACCESS}),
        description="Single site, single server, core recording and playback."),
    Edition.PRO: EditionSpec(
        edition=Edition.PRO, display_name="Intelligent VMS Pro", max_cameras=128,
        features=frozenset({FeatureFlag.API_ACCESS, FeatureFlag.MULTI_SITE,
                            FeatureFlag.SERVER_ANALYTICS, FeatureFlag.OIDC}),
        description="Multi-site hierarchy, server-side analytics workers, SSO."),
    Edition.ENTERPRISE: EditionSpec(
        edition=Edition.ENTERPRISE, display_name="Intelligent VMS Enterprise",
        max_cameras=100_000, features=frozenset(FeatureFlag),
        description="Federation, redundancy, regulated analytics capabilities."),
}


def spec_for(edition: Edition) -> EditionSpec:
    return EDITIONS[edition]
