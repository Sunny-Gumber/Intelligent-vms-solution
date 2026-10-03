import csv
import io
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import feature_catalog


def source_text():
    return (
        Path(__file__).parents[1]
        / "docs"
        / "product"
        / "VMS_COMPLETE_FEATURE_MASTER_CHECKLIST.md"
    ).read_text(encoding="utf-8")


def overlay_text(*rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=feature_catalog.OVERLAY_FIELDS,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def test_master_checklist_generates_all_48_categories():
    rows = feature_catalog.parse_catalog(source_text())
    assert len({row["category_id"] for row in rows}) == 48
    assert len(rows) == 623


def test_feature_ids_are_unique_and_deterministic():
    rows = feature_catalog.parse_catalog(source_text())
    ids = [row["feature_id"] for row in rows]
    assert len(ids) == len(set(ids))
    assert ids[0] == "F01-001"
    assert any(
        row["feature_id"].startswith("F48-")
        and "Self-service tenant onboarding" in row["feature_name"]
        for row in rows
    )


def test_import_defaults_are_roadmap_target_not_verified():
    rows = feature_catalog.parse_catalog(source_text())
    assert all(row["support_mode"] == "R" for row in rows)
    assert all(row["engineering_state"] == "TARGET" for row in rows)
    assert all(row["verification_status"] == "NV" for row in rows)


def test_known_features_land_in_expected_categories():
    rows = feature_catalog.parse_catalog(source_text())
    by_name = {row["feature_name"]: row for row in rows}
    assert by_name["ONVIF support"]["category_id"] == "1"
    assert by_name["Natural-language video search, conversational investigation"]["category_id"] == "33"
    assert by_name["NVIDIA TensorRT / GPU inference acceleration"]["category_id"] == "43"


def test_overlay_preserves_master_identity_fields_and_applies_evidence():
    rows = feature_catalog.parse_catalog(source_text())
    original = dict(rows[0])
    overlay = overlay_text(
        {
            "feature_id": "F01-001",
            "support_mode": "N",
            "engineering_state": "QA",
            "native_or_integrated": "native",
            "on_prem": "Yes",
            "cloud": "Yes",
            "web": "Yes",
            "api": "Yes",
            "automated_test_reference": "tests/test_onvif_discovery.py",
            "supporting_document": "docs/architecture/PHASE2_ONVIF_DESIGN.md",
            "evidence_reference": "CI:test_onvif_discovery",
            "verification_status": "PASS",
            "presales_remarks": "Existing software evidence only",
            "last_verification_date": "2026-09-28",
        }
    )

    overlays = feature_catalog.parse_overlay(overlay)
    result = feature_catalog.apply_overlay(rows, overlays)
    updated = result[0]

    assert updated["feature_id"] == original["feature_id"]
    assert updated["feature_name"] == original["feature_name"]
    assert updated["category"] == original["category"]
    assert updated["support_mode"] == "N"
    assert updated["engineering_state"] == "QA"
    assert updated["automated_test_reference"] == "tests/test_onvif_discovery.py"


def test_overlay_rejects_duplicate_feature_ids():
    row = {"feature_id": "F01-001", "support_mode": "N", "engineering_state": "QA"}
    overlay = overlay_text(row, row)

    with pytest.raises(ValueError, match="Duplicate feature_id"):
        feature_catalog.parse_overlay(overlay)


def test_overlay_rejects_invalid_state_and_unknown_feature_id():
    invalid_state = overlay_text(
        {"feature_id": "F01-001", "support_mode": "N", "engineering_state": "DONE"}
    )
    with pytest.raises(ValueError, match="Invalid engineering_state"):
        feature_catalog.parse_overlay(invalid_state)

    rows = feature_catalog.parse_catalog(source_text())
    unknown = overlay_text(
        {"feature_id": "F99-999", "support_mode": "N", "engineering_state": "QA"}
    )
    parsed = feature_catalog.parse_overlay(unknown)
    with pytest.raises(ValueError, match="Unknown feature_id"):
        feature_catalog.apply_overlay(rows, parsed)


def test_overlay_header_is_strict():
    overlay = "feature_id,engineering_state,feature_name\nF01-001,QA,changed\n"
    with pytest.raises(ValueError, match="header must exactly match"):
        feature_catalog.parse_overlay(overlay)


@pytest.mark.parametrize("column_delta", [-1, 1])
def test_overlay_rejects_truncated_and_extra_columns(column_delta):
    values = ["F01-001"] + [""] * (len(feature_catalog.OVERLAY_FIELDS) - 1 + column_delta)
    text = ",".join(feature_catalog.OVERLAY_FIELDS) + "\n" + ",".join(values) + "\n"
    with pytest.raises(ValueError, match="must contain exactly"):
        feature_catalog.parse_overlay(text)


def test_overlay_rejects_malformed_quoting():
    text = ",".join(feature_catalog.OVERLAY_FIELDS) + '\nF01-001,"unterminated\n'
    with pytest.raises(ValueError, match="Invalid evidence overlay CSV"):
        feature_catalog.parse_overlay(text)


@pytest.mark.parametrize(
    "row, message",
    [
        ({"feature_id": "F01-001", "support_mode": "YES"}, "Invalid support_mode"),
        ({"feature_id": "F1-1"}, "Invalid feature_id"),
        ({"feature_id": ""}, "Invalid feature_id"),
    ],
)
def test_overlay_rejects_invalid_boundary_values(row, message):
    with pytest.raises(ValueError, match=message):
        feature_catalog.parse_overlay(overlay_text(row))


def test_header_only_overlay_keeps_catalog_unchanged():
    rows = feature_catalog.parse_catalog(source_text())
    original = feature_catalog.render_csv(rows)
    assert feature_catalog.parse_overlay(overlay_text()) == []
    assert feature_catalog.render_csv(feature_catalog.apply_overlay(rows, [])) == original


def test_cli_regeneration_preserves_evidence_and_detects_stale_output(tmp_path, monkeypatch):
    source = tmp_path / "source.md"
    overlay = tmp_path / "evidence.csv"
    output = tmp_path / "catalog.csv"
    source.write_text(source_text(), encoding="utf-8")
    overlay.write_text(overlay_text({
        "feature_id": "F01-001", "engineering_state": "QA",
        "known_limitations": 'Software only,\nno device "certification"',
    }), encoding="utf-8")
    arguments = ["feature_catalog.py", "--source", str(source), "--overlay", str(overlay), "--output", str(output)]
    monkeypatch.setattr(sys, "argv", arguments)
    assert feature_catalog.main() == 0
    first = output.read_bytes()
    assert feature_catalog.main() == 0
    assert output.read_bytes() == first
    row = next(csv.DictReader(io.StringIO(first.decode("utf-8"))))
    assert row["engineering_state"] == "QA"
    assert row["known_limitations"] == 'Software only,\nno device "certification"'
    monkeypatch.setattr(sys, "argv", arguments + ["--check"])
    assert feature_catalog.main() == 0
    overlay.write_text(overlay_text(), encoding="utf-8")
    with pytest.raises(SystemExit, match="out of date"):
        feature_catalog.main()
    assert output.read_bytes() == first


def test_missing_overlay_cannot_erase_existing_evidence(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source.md"
    output = tmp_path / "catalog.csv"
    source.write_text(source_text(), encoding="utf-8")
    output.write_text("existing evidence\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "feature_catalog.py", "--source", str(source),
        "--overlay", str(tmp_path / "missing.csv"), "--output", str(output),
    ])
    with pytest.raises(SystemExit) as error:
        feature_catalog.main()
    assert error.value.code == 2
    assert "missing evidence overlay" in capsys.readouterr().err
    assert output.read_text(encoding="utf-8") == "existing evidence\n"


def test_category1_audit_is_complete_without_device_or_scale_claims():
    overlay = (Path(__file__).parents[1] / "docs" / "product" / "FEATURE_EVIDENCE_OVERLAY.csv")
    overlays = feature_catalog.parse_overlay(overlay.read_text(encoding="utf-8"))
    assert [row["feature_id"] for row in overlays if row["feature_id"].startswith("F01-")] == [f"F01-{index:03d}" for index in range(1, 44)]

    category1_overlays = [row for row in overlays if row["feature_id"].startswith("F01-")]
    rows = feature_catalog.apply_overlay(feature_catalog.parse_catalog(source_text()), category1_overlays)
    category1 = [row for row in rows if row["category_id"] == "1"]
    qa_ids = {row["feature_id"] for row in category1 if row["engineering_state"] == "QA"}
    assert qa_ids == {
        "F01-001",
        "F01-002",
        "F01-003",
        "F01-004",
        "F01-005",
        "F01-006",
        "F01-008",
        "F01-009",
        "F01-010",
        "F01-012",
        "F01-013",
        "F01-014",
        "F01-015",
        "F01-016",
        "F01-017",
        "F01-018",
        "F01-019",
        "F01-020",
        "F01-021",
        "F01-022",
        "F01-023",
        "F01-024",
        "F01-025",
        "F01-026",
        "F01-027",
        "F01-028",
        "F01-029",
        "F01-030",
        "F01-031",
        "F01-032",
        "F01-033",
        "F01-034",
        "F01-035",
        "F01-036",
        "F01-037",
        "F01-038",
        "F01-039",
        "F01-040",
        "F01-041",
        "F01-042",
        "F01-043",
    }
    assert all(row["engineering_state"] == "TARGET" for row in category1 if row["feature_id"] not in qa_ids)
    assert all(row["verification_status"] == "NV" and not row["maximum_supported_scale"] for row in rows)
    assert all(row["supporting_document"] for row in category1)
    assert all(row["engineering_state"] == "TARGET" and row["support_mode"] == "R" for row in rows[43:])


def test_category2_audit_covers_every_row_without_verification_claims():
    overlay = Path(__file__).parents[1] / "docs" / "product" / "FEATURE_EVIDENCE_OVERLAY.csv"
    overlays = feature_catalog.parse_overlay(overlay.read_text(encoding="utf-8"))
    category2 = [row for row in overlays if row["feature_id"].startswith("F02-")]

    assert [row["feature_id"] for row in category2] == [
        f"F02-{index:03d}" for index in range(1, 22)
    ]
    assert all(row["verification_status"] == "NV" for row in category2)
    assert {row["engineering_state"] for row in category2} <= {"TARGET", "QA"}
    assert {row["feature_id"] for row in category2 if row["engineering_state"] == "QA"} == {
        "F02-001",
        "F02-002",
        "F02-005",
        "F02-006",
        "F02-007",
        "F02-008",
        "F02-012",
        "F02-018",
        "F02-020",
        "F02-021",
    }
