#!/usr/bin/env python3
"""Generate a CycloneDX software bill of materials.

Sources, in order of trust:
  1. Installed distribution metadata (authoritative - what is actually present)
  2. Declared dependencies in each pyproject.toml (what we intend to ship)
  3. apps/web-client/package.json (frontend)
  4. A hand-maintained list of non-Python runtime components that are invoked
     as external processes (FFmpeg, MediaMTX, PostgreSQL) - these never appear
     in Python metadata but absolutely must appear in an SBOM.

Usage:
    python tools/generate_sbom.py [--output release/sbom]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tomllib
import uuid
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PYPROJECTS = [
    ROOT / "packages" / "shared-contracts" / "pyproject.toml",
    ROOT / "services" / "control-api" / "pyproject.toml",
    ROOT / "services" / "recording-service" / "pyproject.toml",
    ROOT / "services" / "analytics-service" / "pyproject.toml",
]

# External processes the product invokes. Absent from Python metadata but part
# of the shipped system, so they belong in the SBOM and the NOTICE file.
EXTERNAL_COMPONENTS = [
    {"name": "ffmpeg", "version": "unpinned-system", "type": "application",
     "licenses": ["LGPL-2.1-or-later OR GPL-2.0-or-later"],
     "purl": "pkg:generic/ffmpeg",
     "note": ("Invoked as a separate process; not linked. The effective licence "
              "depends on the build flags of the binary you ship. Confirm H.264/"
              "H.265 patent obligations separately.")},
    {"name": "mediamtx", "version": "unpinned-system", "type": "application",
     "licenses": ["MIT"], "purl": "pkg:generic/mediamtx",
     "note": "Media gateway; separate process with an HTTP control API."},
    {"name": "postgresql", "version": ">=14", "type": "application",
     "licenses": ["PostgreSQL"], "purl": "pkg:generic/postgresql",
     "note": "Supported production database."},
]


def declared_dependencies() -> dict[str, str]:
    declared: dict[str, str] = {}
    for path in PYPROJECTS:
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        for requirement in data.get("project", {}).get("dependencies", []):
            name = _requirement_name(requirement)
            declared.setdefault(name, requirement)
        for extra in data.get("project", {}).get("optional-dependencies", {}).values():
            for requirement in extra:
                declared.setdefault(_requirement_name(requirement), requirement)
    return declared


def _requirement_name(requirement: str) -> str:
    for separator in (">=", "==", "<=", "~=", ">", "<", "[", ";"):
        requirement = requirement.split(separator)[0]
    return requirement.strip().lower()


def installed_component(name: str) -> dict | None:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None
    meta = dist.metadata
    licenses = [meta.get("License")] if meta.get("License") else []
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            licenses.append(classifier.split("::")[-1].strip())
    return {
        "type": "library",
        "name": dist.metadata["Name"],
        "version": dist.version,
        "purl": f"pkg:pypi/{dist.metadata['Name'].lower()}@{dist.version}",
        "licenses": [{"license": {"name": lic}} for lic in dict.fromkeys(licenses) if lic],
        "externalReferences": (
            [{"type": "website", "url": meta.get("Home-page")}]
            if meta.get("Home-page") else []),
    }


def frontend_components() -> list[dict]:
    package_json = ROOT / "apps" / "web-client" / "package.json"
    if not package_json.is_file():
        return []
    data = json.loads(package_json.read_text(encoding="utf-8"))
    components = []
    for section in ("dependencies", "devDependencies"):
        for name, version in (data.get(section) or {}).items():
            components.append({
                "type": "library", "name": name,
                "version": str(version).lstrip("^~"),
                "purl": f"pkg:npm/{name}@{str(version).lstrip('^~')}",
                "licenses": [],
                "properties": [{"name": "vms:scope", "value": section}],
            })
    return components


def build_sbom() -> dict:
    components: list[dict] = []
    unresolved: list[str] = []

    for name, requirement in sorted(declared_dependencies().items()):
        if name.startswith("vms-"):
            continue        # first-party
        component = installed_component(name)
        if component is None:
            unresolved.append(requirement)
            components.append({
                "type": "library", "name": name, "version": "not-installed",
                "purl": f"pkg:pypi/{name}", "licenses": [],
                "properties": [{"name": "vms:status", "value": "declared-not-installed"}],
            })
        else:
            components.append(component)

    components.extend(frontend_components())

    for external in EXTERNAL_COMPONENTS:
        components.append({
            "type": external["type"], "name": external["name"],
            "version": external["version"], "purl": external["purl"],
            "licenses": [{"license": {"name": lic}} for lic in external["licenses"]],
            "properties": [{"name": "vms:note", "value": external["note"]}],
        })

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "tools": [{"vendor": "Intelligent VMS", "name": "generate_sbom.py",
                       "version": "1.0"}],
            "component": {"type": "application", "name": "Intelligent VMS",
                          "version": _product_version()},
        },
        "components": components,
        "properties": [
            {"name": "vms:unresolved-declared-dependencies",
             "value": str(len(unresolved))},
            {"name": "vms:note",
             "value": ("External process components (FFmpeg, MediaMTX, PostgreSQL) are "
                       "included because they ship with or are required by the product "
                       "even though they are not Python packages.")},
        ],
    }


def _product_version() -> str:
    sys.path.insert(0, str(ROOT / "services" / "control-api"))
    try:
        from app.version import VERSION

        return VERSION
    except Exception:  # noqa: BLE001
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "release" / "sbom")
    args = parser.parse_args()

    sbom = build_sbom()
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / "intelligent-vms-sbom.cdx.json"
    target.write_text(json.dumps(sbom, indent=2), encoding="utf-8")

    print(f"SBOM written: {target}")
    print(f"  components: {len(sbom['components'])}")
    missing = [c for c in sbom["components"] if c.get("version") == "not-installed"]
    if missing:
        print(f"  WARNING: {len(missing)} declared dependencies are not installed in "
              "this environment, so their versions could not be resolved:")
        for component in missing:
            print(f"    - {component['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
