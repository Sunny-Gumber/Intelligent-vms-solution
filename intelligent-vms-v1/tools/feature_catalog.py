#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import re
from pathlib import Path

DEFAULT_SOURCE = Path("docs/product/VMS_COMPLETE_FEATURE_MASTER_CHECKLIST.md")
DEFAULT_OUTPUT = Path("docs/product/FEATURE_CATALOG.csv")
DEFAULT_OVERLAY = Path("docs/product/FEATURE_EVIDENCE_OVERLAY.csv")

SUPPORT_MODES = {"N", "C", "I", "A", "L", "CL", "OP", "P", "R", "NS", "NV"}
ENGINEERING_STATES = {
    "TARGET",
    "DESIGNED",
    "IMPLEMENTING",
    "QA",
    "VERIFIED",
    "EXTERNAL_EVIDENCE",
}
OVERLAY_FIELDS = (
    "feature_id",
    "support_mode",
    "engineering_state",
    "native_or_integrated",
    "camera_dependency",
    "server_dependency",
    "additional_licence",
    "on_prem",
    "cloud",
    "mobile",
    "web",
    "api",
    "maximum_supported_scale",
    "minimum_version",
    "tested_version",
    "automated_test_reference",
    "supporting_document",
    "evidence_reference",
    "verification_status",
    "presales_remarks",
    "known_limitations",
    "last_verification_date",
)


def track_for(category_id: int) -> str:
    """Map a numbered feature category to its engineering workstream.

    Args:
        category_id: Numbered master feature category.

    Returns:
        Stable workstream label used by the generated catalog.
    """
    if category_id <= 8:
        return "A-Core-VMS"
    if category_id <= 14:
        return "B-Professional-VMS"
    if category_id <= 19:
        return "C-Analytics-Metadata-Identity"
    if category_id <= 22:
        return "D-Physical-Security-Control-Room"
    if category_id <= 28:
        return "E-Enterprise-Platform"
    if category_id <= 31:
        return "F-Integration-GIS-Workflow"
    if category_id <= 35:
        return "G-AI-Command-Centre"
    if category_id <= 41:
        return "H-Cloud-BI-Evidence-Commercial"
    return "I-Extended-Market"


def parse_catalog(text: str) -> list[dict[str, str]]:
    """Parse the master Markdown checklist into normalized catalog rows.

    Args:
        text: Complete master feature checklist Markdown.

    Returns:
        Feature catalog row dictionaries.

    Raises:
        ValueError: If the source does not contain all 48 numbered categories.
    """
    rows: list[dict[str, str]] = []
    level = ""
    category_id: int | None = None
    category_name = ""
    index = 0

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# LEVEL "):
            level = line.removeprefix("# ")
            category_id = None
            continue
        if line.startswith("# ADDITIONAL CATEGORIES"):
            level = "ADDITIONAL CATEGORIES"
            category_id = None
            continue

        match = re.match(r"^##\s+(\d+)\.\s+(.+)$", line)
        if match:
            category_id = int(match.group(1))
            category_name = match.group(2)
            index = 0
            continue

        if line.startswith("#"):
            category_id = None
            continue

        if (
            category_id is None
            or not line
            or line == "---"
            or line.startswith(">")
            or line.startswith("*")
        ):
            continue

        for feature in (part.strip() for part in line.split("·")):
            if not feature:
                continue
            index += 1
            rows.append(
                {
                    "feature_id": f"F{category_id:02d}-{index:03d}",
                    "maturity_level": level,
                    "category_id": str(category_id),
                    "category": category_name,
                    "feature_name": feature,
                    "support_mode": "R",
                    "engineering_state": "TARGET",
                    "native_or_integrated": "",
                    "camera_dependency": "",
                    "server_dependency": "",
                    "additional_licence": "",
                    "on_prem": "",
                    "cloud": "",
                    "mobile": "",
                    "web": "",
                    "api": "",
                    "maximum_supported_scale": "",
                    "minimum_version": "",
                    "tested_version": "",
                    "automated_test_reference": "",
                    "supporting_document": "",
                    "evidence_reference": "",
                    "verification_status": "NV",
                    "presales_remarks": "",
                    "known_limitations": "",
                    "owner_workstream": track_for(category_id),
                    "last_verification_date": "",
                }
            )

    if len({row["category_id"] for row in rows}) != 48:
        raise ValueError("Feature source must contain all 48 numbered categories")
    return rows


def parse_overlay(text: str) -> list[dict[str, str]]:
    """Parse and validate the engineering evidence overlay.

    Args:
        text: CSV text keyed by stable Feature ID.

    Returns:
        Validated overlay rows in file order.

    Raises:
        ValueError: If CSV syntax, row widths, headers, IDs, or states are invalid.
    """
    reader = csv.DictReader(io.StringIO(text), strict=True)
    try:
        fieldnames = reader.fieldnames
        raw_rows = list(reader)
    except csv.Error as exc:
        raise ValueError(f"Invalid evidence overlay CSV at line {reader.line_num}: {exc}") from exc
    if fieldnames is None:
        raise ValueError("Evidence overlay must contain a CSV header")
    if tuple(fieldnames) != OVERLAY_FIELDS:
        raise ValueError(
            "Evidence overlay header must exactly match the supported overlay schema"
        )

    overlays: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for row_number, row in enumerate(raw_rows, start=2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"Evidence overlay row {row_number} must contain exactly {len(OVERLAY_FIELDS)} columns")
        feature_id = (row.get("feature_id") or "").strip()
        if not re.fullmatch(r"F\d{2}-\d{3}", feature_id):
            raise ValueError(
                f"Invalid feature_id at overlay row {row_number}: {feature_id!r}"
            )
        if feature_id in seen_ids:
            raise ValueError(f"Duplicate feature_id in overlay: {feature_id}")
        seen_ids.add(feature_id)

        support_mode = (row.get("support_mode") or "").strip()
        if support_mode and support_mode not in SUPPORT_MODES:
            raise ValueError(
                f"Invalid support_mode for {feature_id}: {support_mode!r}"
            )

        engineering_state = (row.get("engineering_state") or "").strip()
        if engineering_state and engineering_state not in ENGINEERING_STATES:
            raise ValueError(
                f"Invalid engineering_state for {feature_id}: {engineering_state!r}"
            )

        overlays.append({key: (value or "").strip() for key, value in row.items()})
    return overlays


def apply_overlay(
    rows: list[dict[str, str]],
    overlays: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Apply mutable evidence fields to generated catalog rows by Feature ID.

    Structural fields from the master checklist are intentionally immutable.

    Args:
        rows: Catalog rows generated from the master checklist.
        overlays: Validated evidence overlay rows.

    Returns:
        The same catalog rows with non-empty overlay values applied.

    Raises:
        ValueError: If an overlay references a Feature ID absent from the master list.
    """
    by_id = {row["feature_id"]: row for row in rows}
    for overlay in overlays:
        feature_id = overlay["feature_id"]
        if feature_id not in by_id:
            raise ValueError(f"Unknown feature_id in evidence overlay: {feature_id}")
        target = by_id[feature_id]
        for field in OVERLAY_FIELDS:
            if field == "feature_id":
                continue
            value = overlay[field]
            if value:
                target[field] = value
    return rows


def render_csv(rows: list[dict[str, str]]) -> str:
    """Render normalized feature catalog rows as deterministic CSV.

    Args:
        rows: Parsed feature catalog rows.

    Returns:
        CSV text using stable field order and line endings.

    Raises:
        IndexError: If no rows are supplied.
    """
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(rows[0]),
        quoting=csv.QUOTE_ALL,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def main() -> int:
    """Generate or verify FEATURE_CATALOG.csv from source plus evidence overlay.

    Returns:
        Process-style success code zero.

    Raises:
        SystemExit: If the overlay is missing, or the checked catalog is missing or stale.
        ValueError: If source or overlay data is structurally invalid.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overlay", type=Path, default=DEFAULT_OVERLAY)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    rows = parse_catalog(args.source.read_text(encoding="utf-8"))
    if not args.overlay.is_file():
        parser.error(f"missing evidence overlay: {args.overlay}")
    overlays = parse_overlay(args.overlay.read_text(encoding="utf-8"))
    rows = apply_overlay(rows, overlays)
    rendered = render_csv(rows)

    if args.check:
        if not args.output.exists():
            raise SystemExit(f"missing generated catalog: {args.output}")
        existing = args.output.read_text(encoding="utf-8")
        if existing.replace("\r\n", "\n") != rendered.replace("\r\n", "\n"):
            raise SystemExit("FEATURE_CATALOG.csv is out of date; regenerate it")
        print(f"feature catalog OK: {len(rows)} rows / 48 categories")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8", newline="")
    print(f"wrote {len(rows)} feature rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
