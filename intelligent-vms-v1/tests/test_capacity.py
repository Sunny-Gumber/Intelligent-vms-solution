import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('capacity', Path(__file__).parents[1] / 'tools' / 'capacity.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_100k_1mbps():
    gbps, tb_day, pb_total = mod.capacity(100_000, 1000, 180)
    assert round(gbps, 2) == 100.00
    assert round(tb_day, 2) == 1080.00
    assert round(pb_total, 2) == 194.40
