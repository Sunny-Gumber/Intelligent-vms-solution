from app.models.entities import CameraEntity
from app.models.placement import InfrastructureNodeEntity
from app.routers.cameras import to_read
from tests.time_control import FIXED_NOW


def camera(state: str):
    return CameraEntity(
        id="cam-1",
        tenant_id="t",
        site_id="s",
        name="Gate",
        host="10.0.0.10",
        rtsp_port=554,
        main_path="/main",
        stream_key="gate-1",
        media_node_id="media-a",
        desired_state=state,
        enabled=True,
        created_at=FIXED_NOW,
    )


def node():
    return InfrastructureNodeEntity(
        id="media-a",
        name="Media A",
        region_id="r1",
        roles_json=["media"],
        endpoints_json={
            "webrtc_public_base": "https://media-a.example/webrtc",
            "hls_public_base": "https://media-a.example/hls",
        },
        capacity_json={},
        load_json={},
        state="active",
        enabled=True,
        heartbeat_at=FIXED_NOW,
        generation=1,
    )


def test_provisioned_camera_uses_assigned_node_public_urls():
    result = to_read(camera("provisioned"), node())
    assert result.webrtc_url == "https://media-a.example/webrtc/gate-1"
    assert result.hls_url == "https://media-a.example/hls/gate-1"


def test_assigned_but_not_provisioned_camera_has_no_public_stream_url():
    result = to_read(camera("assigned"), node())
    assert result.webrtc_url is None
    assert result.hls_url is None
