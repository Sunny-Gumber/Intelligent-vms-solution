"""Execute the desktop WHEP lifecycle page in Node.

The C# renderer contract only searches live.html for source text. These checks
load that page and run the start/stop race against a fake WHEP peer.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
NODE_TEST = ROOT / "clients" / "windows" / "tests" / "live-whep-lifecycle.test.mjs"


def test_desktop_whep_lifecycle_executes_live_html_in_node():
    node = shutil.which("node")
    assert node, "Node is required to execute live.html"
    completed = subprocess.run(
        [node, "--test", str(NODE_TEST)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    sys.stdout.write(output)
