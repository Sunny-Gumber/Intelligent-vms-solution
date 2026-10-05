# Supply-chain readiness

Current release candidate identity:
- product: Intelligent VMS
- field-test installer: IntelligentVMS-FieldTest-Setup-x64-0.2.0.exe
- installer technology: NSIS 3.13
- MediaMTX: 1.21.1 with pinned SHA-256 in the accepted Windows installer
- Python server target: Python 3.12 x64 accepted field-test runtime
- PostgreSQL: supported local x64 service 14+; reused rather than silently installed/removed
- Windows client: self-contained .NET 10 win-x64 package
- WebView2: external Microsoft Evergreen prerequisite detected by setup

## Dependency and integrity policy
Build and qualification evidence must record exact Git SHA and installer SHA-256. MediaMTX integrity remains pinned. Compiler/runtime versions should be retained in release metadata. Arbitrary latest prerequisite downloads are not permitted in the accepted installer path.

## SBOM
SBOM generation is DEFERRED for this Layer A milestone unless existing release tooling can produce a stable SPDX or CycloneDX document without introducing fragile dependencies. This is a release-readiness gap, not a hidden PASS.

## Third-party license inventory
Existing dependency manifests and package files remain the authoritative technical inventory. A formal commercial third-party license notice/bundle is still a release-readiness item. No final product licensing claim is made here.

## Repository/product licensing
If final product license/EULA/publisher legal identity remains undecided, it requires owner/legal approval before a commercial GA release. This does not block acceptance of the qualification harness itself.

## Release manifest
The qualification CLI generates a machine-readable release manifest containing product version, Git SHA, installer hash, server/client/schema identity, MediaMTX identity, NSIS version, qualification state, signing state and known limitations.
