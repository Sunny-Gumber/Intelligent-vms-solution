import importlib.util
import json
from pathlib import Path
import datetime as dt

ROOT=Path(__file__).parents[1]
SCRIPT=ROOT/"qualification/windows/scripts/qualify.py"
spec=importlib.util.spec_from_file_location("qualify",SCRIPT)
q=importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)

def base():
    return {"qualification_run_id":"VMS-WIN-20261005-001","started_utc":"2026-10-05T02:00:00+00:00","tester_id":"TESTER-001","machine_id":"MACHINE-001","test_profile":"UNIT_TEST","build":{"git_sha":"a"*40,"installer":"x.exe","installer_version":"0.2.0.0","installer_sha256":"b"*64},"evidence_source":"SIMULATED","overall_result":"NOT_RUN","approval":{"automated_result":"NOT_RUN","tester_signoff":"NOT_RUN","independent_review":"NOT_RUN"},"results":[]}

def test_run_id_contract():
    assert q.run_id(7,dt.date(2026,10,5))=="VMS-WIN-20261005-007"

def test_simulated_cannot_create_external_pass():
    for tid in ("WIN11-INSTALL","WIN10-INSTALL","CAM-FIXED","PTZ-STOP","EVENT-HW-MOTION","SOAK-24H","PERF-REAL-P16","STORAGE-REAL-SSD"):
        d=base(); d["results"]=[{"test_id":tid,"state":"PASS","evidence_source":"SIMULATED"}]
        assert any("cannot create real external PASS" in x for x in q.validate_result(d))

def test_external_pass_is_allowed_with_real_source():
    d=base(); d["evidence_source"]="PHYSICAL_MACHINE"; d["overall_result"]="PASS"
    d["results"]=[{"test_id":"WIN11-INSTALL","state":"PASS","evidence_source":"PHYSICAL_MACHINE"}]
    assert q.validate_result(d)==[]

def test_blocked_requires_reason():
    d=base(); d["results"]=[{"test_id":"CAM-FIXED","state":"BLOCKED_EXTERNAL","evidence_source":"SIMULATED"}]
    assert any("reason required" in x for x in q.validate_result(d))

def test_fail_requires_expected_actual_severity():
    d=base(); d["results"]=[{"test_id":"PTZ-STOP","state":"FAIL","evidence_source":"REAL_CAMERA"}]
    errors=q.validate_result(d)
    assert any("expected" in x for x in errors)
    assert any("actual" in x for x in errors)
    assert any("severity" in x for x in errors)
    assert any("retest_status" in x for x in errors)

def test_redaction_removes_known_secret_shapes():
    raw="password=hunter2\nAuthorization: Bearer abc.def.ghi\nrtsp://admin:secret@10.0.0.5/live\nprivate_key=XYZ"
    safe=q.redact(raw)
    for forbidden in ("hunter2","abc.def.ghi","admin:secret","XYZ"):
        assert forbidden not in safe
    assert "[REDACTED]" in safe

def test_summary_and_release_manifest_bind_build():
    d=base(); d["build"].update({"product_version":"0.2.0","server_version":"0.2.0","client_version":"0.2.0","db_schema_revision":"0018"})
    assert "a"*40 in q.summary(d)
    m=q.release_manifest(d)
    assert m["git_sha"]=="a"*40 and m["installer_sha256"]=="b"*64
    assert m["signing_status"]=="UNSIGNED_EXPECTED"

def test_schemas_and_example_are_truthful():
    for name in ("machine-manifest.schema.json","camera-manifest.schema.json","qualification-result.schema.json"):
        json.loads((ROOT/"qualification/windows/schemas"/name).read_text())
    example=json.loads((ROOT/"qualification/windows/templates/qualification-result.example.json").read_text())
    assert example["overall_result"]=="NOT_RUN"
    assert example["results"][0]["state"]=="BLOCKED_EXTERNAL"
