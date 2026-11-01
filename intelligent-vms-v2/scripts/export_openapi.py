#!/usr/bin/env python3
"""Export the OpenAPI contract from the running application.

The contract is generated from the real app, never hand-maintained, so it
cannot drift from the implementation. CI diffs the output against the
committed `contracts/openapi.json`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "control-api"))
sys.path.insert(0, str(ROOT / "packages" / "shared-contracts"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(ROOT / "contracts" / "openapi.json"))
    args = parser.parse_args()

    os.environ.setdefault("VMS_DATA_ROOT", str(ROOT / "var" / "openapi"))
    os.environ.setdefault("VMS_DATABASE_URL",
                          f"sqlite+pysqlite:///{ROOT / 'var' / 'openapi.db'}")
    os.environ.setdefault("VMS_SECRET_KEY",
                          "openapi-generation-only-not-a-real-secret-0123456789")

    from app.config import get_settings
    from app.main import create_app

    settings = get_settings()
    settings.ensure_directories()
    spec = create_app(settings).openapi()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(f"wrote {output} — {len(spec['paths'])} paths, "
          f"{len(spec['components']['schemas'])} schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
