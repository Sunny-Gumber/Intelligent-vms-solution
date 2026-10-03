#!/usr/bin/env python3
"""Post one MediaMTX segment-complete hook without shell-specific interpolation."""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


def read_env(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE settings from the protected field-test environment file."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def post_segment(env_file: Path, retries: int = 3) -> None:
    """Send one authenticated recording-completion notification with bounded retries."""
    values = read_env(env_file)
    callback = values.get("RECORDING_HOOK_CALLBACK_URL", "")
    token = values.get("RECORDING_HOOK_TOKEN", "")
    parsed = urlsplit(callback)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("recording hook callback is invalid")
    if parsed.username or parsed.password or parsed.fragment:
        raise RuntimeError("recording hook callback must not contain credentials or fragments")
    if not token:
        raise RuntimeError("recording hook token is missing")

    required = {
        "path": os.environ.get("MTX_PATH", ""),
        "segment_path": os.environ.get("MTX_SEGMENT_PATH", ""),
        "duration": os.environ.get("MTX_SEGMENT_DURATION", ""),
    }
    if not all(required.values()):
        raise RuntimeError("MediaMTX segment hook environment is incomplete")

    body = urlencode(required).encode("utf-8")
    request = Request(
        callback,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Recording-Hook-Token": token,
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            with urlopen(request, timeout=4.0) as response:
                if 200 <= response.status < 300:
                    return
                raise RuntimeError(f"recording hook returned HTTP {response.status}")
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(1.0, 0.25 * (2**attempt)))
    raise RuntimeError("recording completion hook failed") from last_error


def main() -> None:
    """Parse CLI arguments and submit one segment completion callback."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args()
    post_segment(args.env_file)


if __name__ == "__main__":
    main()
