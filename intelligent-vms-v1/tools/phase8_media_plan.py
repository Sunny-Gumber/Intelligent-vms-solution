#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path


def source_command(
    *,
    camera_id: int,
    server: str,
    width: int,
    height: int,
    fps: int,
    bitrate_kbps: int,
    codec: str,
) -> list[str]:
    """Build one ffmpeg synthetic RTSP source command.

    Args:
        camera_id: Synthetic camera number.
        server: RTSP server base URL.
        width: Video width.
        height: Video height.
        fps: Source frame rate.
        bitrate_kbps: Encoded target bitrate.
        codec: h264 or h265.

    Returns:
        Argument list suitable for subprocess/shell generation.
    """
    encoder = "libx264" if codec == "h264" else "libx265"
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-re",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={fps}",
        "-an",
        "-c:v",
        encoder,
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-b:v",
        f"{bitrate_kbps}k",
        "-maxrate",
        f"{bitrate_kbps}k",
        "-bufsize",
        f"{bitrate_kbps * 2}k",
        "-f",
        "rtsp",
        f"{server.rstrip('/')}/bench/cam-{camera_id:07d}",
    ]


def viewer_command(*, camera_id: int, server: str, transport: str) -> list[str]:
    """Build one ffmpeg RTSP viewer command.

    Args:
        camera_id: Synthetic camera number.
        server: RTSP server base URL.
        transport: RTSP transport, tcp or udp.

    Returns:
        Argument list that decodes the selected stream to a null sink.
    """
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        transport,
        "-i",
        f"{server.rstrip('/')}/bench/cam-{camera_id:07d}",
        "-map",
        "0:v:0",
        "-f",
        "null",
        "-",
    ]


def build_manifest(args) -> dict:
    """Build the repeatable Phase-8 media workload manifest.

    Args:
        args: Parsed source/viewer/video benchmark arguments.

    Returns:
        Manifest containing source/viewer command arrays and workload metadata.
    """
    sources = [
        source_command(
            camera_id=i,
            server=args.server,
            width=args.width,
            height=args.height,
            fps=args.fps,
            bitrate_kbps=args.bitrate_kbps,
            codec=args.codec,
        )
        for i in range(args.start, args.start + args.sources)
    ]
    viewers = [
        viewer_command(
            camera_id=args.start + (i % max(1, args.sources)),
            server=args.server,
            transport=args.transport,
        )
        for i in range(args.viewers)
    ]
    return {
        "schema_version": "phase8-media-plan-v1",
        "note": "Workload definition only; capacity is measured only when executed on target hardware.",
        "server": args.server,
        "source_count": args.sources,
        "viewer_count": args.viewers,
        "video": {
            "width": args.width,
            "height": args.height,
            "fps": args.fps,
            "bitrate_kbps": args.bitrate_kbps,
            "codec": args.codec,
        },
        "sources": sources,
        "viewers": viewers,
    }


def write_shell(path: str, commands: list[list[str]]) -> None:
    """Write a bash launcher for a command collection.

    Args:
        path: Output script path.
        commands: Argument lists to execute concurrently.

    Returns:
        None after the shell script is written.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    for command in commands:
        lines.append(shlex.join(command) + " &")
    lines += ["", "wait", ""]
    target.write_text("\n".join(lines), encoding="utf-8")


def main():
    """Parse media-plan arguments and write manifest/optional shell launchers."""
    parser = argparse.ArgumentParser(description="Generate repeatable Phase 8 RTSP source/viewer plans")
    parser.add_argument("--server", default="rtsp://127.0.0.1:8554")
    parser.add_argument("--sources", type=int, default=10)
    parser.add_argument("--viewers", type=int, default=10)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--bitrate-kbps", type=int, default=1024)
    parser.add_argument("--codec", choices=("h264", "h265"), default="h264")
    parser.add_argument("--transport", choices=("tcp", "udp"), default="tcp")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--source-shell")
    parser.add_argument("--viewer-shell")
    args = parser.parse_args()
    if args.sources < 1 or args.viewers < 0:
        raise SystemExit("sources must be >=1 and viewers >=0")

    manifest = build_manifest(args)
    target = Path(args.output_json)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if args.source_shell:
        write_shell(args.source_shell, manifest["sources"])
    if args.viewer_shell:
        write_shell(args.viewer_shell, manifest["viewers"])
    print(
        f"generated sources={manifest['source_count']} viewers={manifest['viewer_count']} "
        f"bitrate_kbps={args.bitrate_kbps}"
    )


if __name__ == "__main__":
    main()
