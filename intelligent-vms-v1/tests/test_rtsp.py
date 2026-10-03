import importlib.util
from pathlib import Path

path = Path(__file__).parents[1] / 'services' / 'control-api' / 'app' / 'services' / 'rtsp.py'
spec = importlib.util.spec_from_file_location('rtsp', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_credentials_are_url_encoded():
    uri = mod.build_rtsp_uri('10.0.0.10', 554, '/stream1', 'admin@example', 'p@ss word')
    assert uri == 'rtsp://admin%40example:p%40ss%20word@10.0.0.10:554/stream1'
