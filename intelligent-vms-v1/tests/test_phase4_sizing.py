import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "phase4_sizing", Path(__file__).parents[1] / "tools" / "phase4_sizing.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_100k_structural_health_event_load():
    out = mod.structural_load(
        cameras=100_000,
        cameras_per_media_node=2_000,
        health_poll_seconds=10,
        health_heartbeat_seconds=300,
        events_per_camera_hour=2,
        avg_event_bytes=1500,
    )
    assert out["media_nodes"] == 50
    assert out["health_observations_per_second"] == 10_000
    assert round(out["steady_health_heartbeat_writes_per_second"], 3) == 333.333
    assert round(out["events_per_second"], 3) == 55.556
