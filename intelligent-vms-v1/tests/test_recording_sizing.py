import importlib.util
from pathlib import Path

path = Path(__file__).parents[1] / "tools" / "recording_sizing.py"
spec = importlib.util.spec_from_file_location("recording_sizing", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_100k_1mbps_15min_segments():
    result = mod.calculate(
        mod.RecordingInputs(
            cameras=100_000,
            bitrate_kbps=1000,
            retention_days=180,
            segment_seconds=900,
        )
    )
    assert round(result["record_ingress_gbps"], 2) == 100.00
    assert round(result["raw_tb_day"], 2) == 1080.00
    assert round(result["raw_pb_retention"], 2) == 194.40
    assert round(result["segment_closes_per_second"], 2) == 111.11
    assert round(result["average_segment_mb"], 2) == 112.50
