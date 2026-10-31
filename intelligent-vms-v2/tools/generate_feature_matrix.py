#!/usr/bin/env python3
"""Generate docs/requirements/feature-matrix.md from requirements.yaml.

The matrix is GENERATED, never hand-edited, so the published implementation
status cannot drift away from the traceability source of truth.

It also performs two consistency checks and fails loudly:

  1. Every requirement claiming `verification: automated` must name a concrete
     evidence string. A claim with no evidence is a lie waiting to happen.
  2. Every `component` path that points inside the repository must exist.
     This catches requirements that reference code which was renamed or never
     written.

Usage:
    python tools/generate_feature_matrix.py [--check]

    --check   verify the committed matrix is up to date (used in CI) and
              report nothing else; exit 1 if regeneration is needed.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "requirements" / "requirements.yaml"
TARGET = ROOT / "docs" / "requirements" / "feature-matrix.md"

STATUS_LABEL = {
    "implemented": "Implemented",
    "partial": "Partial",
    "designed": "Designed only",
    "not_implemented": "Not implemented",
    "blocked": "Blocked",
}

VERIFICATION_LABEL = {
    "automated": "Automated test",
    "manual": "Manual / documented",
    "none": "**Not independently verified**",
}


def load() -> dict:
    with SOURCE.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


TEST_REFERENCE = re.compile(r"(tests/[\w/]+\.py)::(\w+)")


def collect_test_names() -> set[str]:
    """Every test FUNCTION and every test MODULE name defined under tests/.

    Used to prove that an `evidence` string naming a test actually refers to
    something that exists. Evidence pointing at a deleted or renamed test is
    how a traceability matrix quietly becomes fiction.

    Module stems are included because evidence legitimately cites a whole file
    (for example "tests/integration/test_api_flows.py authz tests") as well as
    individual functions.
    """
    names: set[str] = set()
    for path in (ROOT / "tests").rglob("test_*.py"):
        names.add(path.stem)
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("def test_", "async def test_")):
                names.add(stripped.split("def ", 1)[1].split("(")[0].strip())
    return names


def validate(document: dict) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    known_tests = collect_test_names()

    for module in document["modules"]:
        for requirement in module["requirements"]:
            rid = requirement["id"]
            if rid in seen:
                problems.append(f"{rid}: duplicate requirement id")
            seen.add(rid)

            if requirement["status"] not in STATUS_LABEL:
                problems.append(f"{rid}: unknown status {requirement['status']!r}")
            if requirement["verification"] not in VERIFICATION_LABEL:
                problems.append(
                    f"{rid}: unknown verification {requirement['verification']!r}")

            evidence = (requirement.get("evidence") or "").strip()

            for _file, test_name in TEST_REFERENCE.findall(evidence):
                if test_name not in known_tests:
                    problems.append(
                        f"{rid}: evidence names a test that does not exist: {test_name}")
            for match in re.findall(r"\b(test_\w+)", evidence):
                if match not in known_tests:
                    problems.append(
                        f"{rid}: evidence names a test that does not exist: {match}")
            if requirement["verification"] == "automated" and len(evidence) < 10:
                problems.append(f"{rid}: claims automated verification but gives no evidence")
            if requirement["status"] == "implemented" and not evidence:
                problems.append(f"{rid}: claims implemented but gives no evidence")

            for component in str(requirement.get("component", "")).split(","):
                component = component.strip().split()[0] if component.strip() else ""
                if not component or not component[0].isalpha():
                    continue
                if "/" not in component:
                    continue
                if _component_exists(component):
                    continue
                problems.append(f"{rid}: component path not found: {component}")
    return problems


def render(document: dict) -> str:
    meta = document["meta"]
    all_requirements = [r for m in document["modules"] for r in m["requirements"]]
    statuses = Counter(r["status"] for r in all_requirements)
    verifications = Counter(r["verification"] for r in all_requirements)

    lines: list[str] = []
    add = lines.append

    add("# Feature Implementation Matrix")
    add("")
    add("> **This file is generated.** Edit "
        "`docs/requirements/requirements.yaml` and run "
        "`python tools/generate_feature_matrix.py`.")
    add("")
    add(f"- Product: **{meta['product']} {meta['version']}**")
    add(f"- Statuses reconciled: **{meta['statuses_reconciled']}**")
    add(f"- Production ready: **{'yes' if meta.get('production_ready') else 'NO'}**")
    add(f"- Test evidence: `{meta.get('test_evidence') or meta.get('evidence_run', 'n/a')}`")
    add("")

    add("## Summary")
    add("")
    add("| Status | Count | Share |")
    add("| --- | ---: | ---: |")
    total = len(all_requirements)
    for status in ("implemented", "partial", "designed", "not_implemented", "blocked"):
        count = statuses.get(status, 0)
        if count:
            add(f"| {STATUS_LABEL[status]} | {count} | {count / total * 100:.1f}% |")
    add(f"| **Total** | **{total}** | 100% |")
    add("")

    add("| Verification | Count | Share |")
    add("| --- | ---: | ---: |")
    for verification in ("automated", "manual", "none"):
        count = verifications.get(verification, 0)
        if count:
            label = VERIFICATION_LABEL[verification].replace("**", "")
            add(f"| {label} | {count} | {count / total * 100:.1f}% |")
    add("")

    add("### How to read this")
    add("")
    add("- **Implemented** means the code exists, is integrated, is reachable from a "
        "caller and its configuration is documented.")
    add("- **Implemented never means \"validated against physical camera hardware\".** "
        "No ONVIF or RTSP camera was available during development. Device "
        "compatibility is an external qualification activity "
        "(`docs/verification/external-qualification-plan.md`).")
    add("- **Partial** means a meaningful subset works and the specific gap is stated "
        "in the evidence column.")
    add("- **Designed only** means a written design exists and no working "
        "implementation does. The API returns `501 not_implemented` for these.")
    add("")

    add("## Per-module status")
    add("")
    add("| Module | Implemented | Partial | Designed | Not implemented |")
    add("| --- | ---: | ---: | ---: | ---: |")
    for module in document["modules"]:
        counts = Counter(r["status"] for r in module["requirements"])
        add(f"| {module['id']} {module['name']} | {counts.get('implemented', 0)} | "
            f"{counts.get('partial', 0)} | {counts.get('designed', 0)} | "
            f"{counts.get('not_implemented', 0)} |")
    add("")

    add("## Full matrix")
    add("")
    for module in document["modules"]:
        add(f"### {module['id']} - {module['name']}")
        add("")
        add("| ID | Requirement | Status | Verification | Component | Evidence / gap |")
        add("| --- | --- | --- | --- | --- | --- |")
        for requirement in module["requirements"]:
            status = STATUS_LABEL[requirement["status"]]
            if requirement["status"] in ("designed", "not_implemented"):
                status = f"**{status}**"
            add(
                f"| {requirement['id']} "
                f"| {_escape(requirement['text'])} "
                f"| {status} "
                f"| {VERIFICATION_LABEL[requirement['verification']]} "
                f"| `{_escape(str(requirement.get('component', '')))}` "
                f"| {_escape(str(requirement.get('evidence', '')))} |"
            )
        add("")

    return "\n".join(lines) + "\n"


def _component_exists(component: str) -> bool:
    """Resolve a component reference to a real path.

    References may be repo-relative ("app/core/rbac.py" lives under
    services/control-api) and may use a glob ("apps/*") when a requirement is
    satisfied by more than one client.
    """
    candidates = [component]
    for prefix in ("services/control-api", "services/recording-service",
                   "services/analytics-service", "packages/shared-contracts"):
        candidates.append(f"{prefix}/{component}")

    for candidate in candidates:
        if any(ch in candidate for ch in "*?["):
            if list(ROOT.glob(candidate)):
                return True
        elif (ROOT / candidate).exists():
            return True
    return False


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail if the committed matrix is stale")
    args = parser.parse_args()

    document = load()
    problems = validate(document)
    if problems:
        print("Requirements traceability problems:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    rendered = render(document)

    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != rendered:
            print("feature-matrix.md is out of date; run "
                  "python tools/generate_feature_matrix.py", file=sys.stderr)
            return 1
        print("feature matrix is up to date")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8")

    counts = Counter(r["status"] for m in document["modules"] for r in m["requirements"])
    total = sum(counts.values())
    print(f"wrote {TARGET.relative_to(ROOT)}: {total} requirements across "
          f"{len(document['modules'])} modules")
    print("  " + ", ".join(f"{STATUS_LABEL[k]}={v}" for k, v in counts.most_common()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
