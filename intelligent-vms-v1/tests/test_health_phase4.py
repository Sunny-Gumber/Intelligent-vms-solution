from app.services.health_monitor import Counters, evaluate_state, safe_path_detail


def test_health_hysteresis_offline_and_recovery():
    state = "online"
    counters = Counters()
    state, counters = evaluate_state(state, ready=False, counters=counters, failure_threshold=3, recovery_threshold=2)
    assert state == "degraded"
    state, counters = evaluate_state(state, ready=False, counters=counters, failure_threshold=3, recovery_threshold=2)
    assert state == "degraded"
    state, counters = evaluate_state(state, ready=False, counters=counters, failure_threshold=3, recovery_threshold=2)
    assert state == "offline"

    state, counters = evaluate_state(state, ready=True, counters=counters, failure_threshold=3, recovery_threshold=2)
    assert state == "offline"
    state, counters = evaluate_state(state, ready=True, counters=counters, failure_threshold=3, recovery_threshold=2)
    assert state == "online"


def test_safe_path_detail_does_not_store_source_credentials():
    detail = safe_path_detail(
        {
            "name": "cam",
            "ready": True,
            "tracks": ["H264"],
            "source": {"url": "rtsp://admin:secret@10.0.0.2/stream"},
            "bytesReceived": 123,
        }
    )
    assert detail["ready"] is True
    assert "source" not in detail
    assert "secret" not in str(detail)
