from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models.schemas import AIDetection, AIPolicyUpdate, AIZone
from app.services.ai import normalize_detection_event, validate_artifact_ref, validate_provider_config, validate_sha256


def test_model_refs_are_catalog_only():
    assert validate_artifact_ref("model://detector/v1")
    assert validate_artifact_ref("provider://gpu-pool/yolo")
    with pytest.raises(ValueError):
        validate_artifact_ref("https://example.com/model.onnx")


def test_model_sha256_validation():
    assert validate_sha256("A"*64) == "a"*64
    with pytest.raises(ValueError):
        validate_sha256("abc")


def test_provider_config_rejects_secrets():
    with pytest.raises(ValueError):
        validate_provider_config({"api_key":"secret"})
    assert validate_provider_config({"batch_size":4}) == {"batch_size":4}


def test_zone_geometry_validation():
    with pytest.raises(ValidationError):
        AIZone(id="z",name="bad",kind="polygon",points=[{"x":0,"y":0},{"x":1,"y":1}])
    AIZone(id="z",name="line",kind="line",points=[{"x":0,"y":0},{"x":1,"y":1}])


def test_enabled_inference_requires_model():
    with pytest.raises(ValidationError):
        AIPolicyUpdate(enabled=True,source_mode="inference",analytics=["human"])


def test_normalized_event_is_deterministic_and_scoped():
    camera=SimpleNamespace(id="cam-1",tenant_id="t1",site_id="s1")
    model=SimpleNamespace(id="m1",name="detector",version="1")
    det=AIDetection(event_type="human",confidence=.9,severity="medium",bbox=[.1,.1,.2,.3])
    a=normalize_detection_event(camera=camera,model=model,result_id="r1",observed_at=datetime(2026,1,1,tzinfo=timezone.utc),detection=det,index=0)
    b=normalize_detection_event(camera=camera,model=model,result_id="r1",observed_at=datetime(2026,1,1,tzinfo=timezone.utc),detection=det,index=0)
    assert a["event_id"] == b["event_id"]
    assert a["tenant_id"] == "t1" and a["camera_id"] == "cam-1"
    assert a["object_type"] == "person"


def test_disabled_inference_policy_can_clear_model():
    policy=AIPolicyUpdate(enabled=False,source_mode="inference",analytics=[])
    assert policy.model_id is None


def test_camera_metadata_is_not_a_server_inference_policy_mode():
    with pytest.raises(ValidationError):
        AIPolicyUpdate(enabled=True,source_mode="camera_metadata",analytics=["human"])
