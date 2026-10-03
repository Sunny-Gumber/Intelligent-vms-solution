from app.services.diagnostics import MediaDiagnostics


METRICS = """
paths{name="site-cam-abc",state="ready"} 1
paths_inbound_bytes{name="site-cam-abc",state="ready"} 1000000
paths_outbound_bytes{name="site-cam-abc",state="ready"} 500000
rtsp_sessions_inbound_rtp_packets{id="s1",path="site-cam-abc",state="read"} 1000
rtsp_sessions_inbound_rtp_packets_lost{id="s1",path="site-cam-abc",state="read"} 12
rtsp_sessions_inbound_rtp_packets_in_error{id="s1",path="site-cam-abc",state="read"} 2
rtsp_sessions_inbound_rtp_packets_jitter{id="s1",path="site-cam-abc",state="read"} 4.5
"""


def test_mediamtx_metric_collection_is_path_scoped():
    values = MediaDiagnostics._collect(METRICS, "site-cam-abc")
    assert values["path_state"] == "ready"
    assert values["inbound_bytes"] == 1000000
    assert values["outbound_bytes"] == 500000
    assert values["rtp_packets"] == 1000
    assert values["rtp_packets_lost"] == 12
    assert values["rtp_packets_in_error"] == 2
    assert values["rtp_jitter"] == 4.5
