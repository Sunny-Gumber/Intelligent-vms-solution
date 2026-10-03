import sys
from pathlib import Path

CONTROL = Path(__file__).parents[1] / "services" / "control-api"
if str(CONTROL) not in sys.path:
    sys.path.insert(0, str(CONTROL))
