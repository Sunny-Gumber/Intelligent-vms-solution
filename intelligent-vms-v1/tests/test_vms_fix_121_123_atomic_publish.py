"""VMS-FIX-121 / VMS-FIX-123: shared fail-closed atomic publish.

These tests cover the FIX-030 and FIX-031 residuals: private ``mkstemp``
staging for both benchmarks, no write after ``os.replace``, cleanup that
cannot mask the original error, fsync, deterministic CSV columns, zero-work
argument rejection, bounded shutdown, and the storage ownership and interrupt
cleanups. They use temporary directories and monkeypatches. They do not open
a network connection and they do not report a hardware qualification.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

TOOLS = Path(__file__).parents[1] / "tools"
ROOT = Path(__file__).parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import phase8_reconnect_benchmark as reconnect_bench
import phase8_storage_benchmark as storage_bench


MIB = 1024 * 1024
CHILD_TIMEOUT_SECONDS = 8.0
SHUTDOWN_BOUND_SECONDS = 0.25


def _helper():
    """Import the shared publisher, failing the test when it is absent.

    Returns:
        The ``phase8_atomic_publish`` module.

    Raises:
        pytest.fail: The helper module cannot be imported.
    """
    try:
        import phase8_atomic_publish as helper
    except ImportError as exc:
        pytest.fail(f"phase8_atomic_publish is missing: {exc}")
    return helper


def _result(benchmark_id: str) -> dict:
    """Build a minimal benchmark record for CSV rendering.

    Args:
        benchmark_id: Value placed in the first CSV column.

    Returns:
        A dictionary with the keys the CSV row reads.
    """
    return {
        "benchmark_id": benchmark_id,
        "environment": {"commit_sha": "abc123"},
        "workload": {"type": "synthetic-storage-write", "duration_seconds": 1.5},
        "result": {
            "operations_ok": 2,
            "operations_failed": 0,
            "throughput_ops_s": 1.25,
            "latency": {"p50_ms": 1, "p95_ms": 2, "p99_ms": 3},
        },
        "resources": {
            "cpu_pct": {"p95": 4},
            "ram_used_bytes": {"p95": 5},
            "net_rx_mbps": {"p95": 6},
            "net_tx_mbps": {"p95": 7},
            "disk_read_mbps": {"p95": 8},
            "disk_write_mbps": {"p95": 9},
            "gpu_measured": False,
        },
    }


def _run_tool(script: str, *args: str, timeout: float = CHILD_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    """Run a child interpreter with the tools directory on its path.

    Args:
        script: Python source. ``sys.argv[1]`` is the tools directory.
        *args: Extra arguments after the tools directory.
        timeout: Seconds before the child is killed.

    Returns:
        The completed process.

    Raises:
        AssertionError: The child did not exit within ``timeout``.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(TOOLS), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise AssertionError(f"child hung\nstdout={stdout}\nstderr={stderr}")
    return subprocess.CompletedProcess(proc.args, proc.returncode or 0, stdout, stderr)


def _storage_namespace(path: Path):
    """Arguments for one 32-byte storage stream.

    Args:
        path: Directory that receives the stream file.

    Returns:
        Simple namespace consumed by ``storage_bench.run``.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        path=str(path),
        streams=1,
        mib_per_stream=32 / MIB,
        chunk_mib=32 / MIB,
        fsync=False,
        keep_files=False,
        sample_interval=30.0,
    )


def test_readme_documents_sigkill_leak_and_latest_run_removal() -> None:
    """README states the latest-run removal and does not promise temp cleanup."""
    readme = (ROOT / "benchmarks" / "README.md").read_text(encoding="utf-8")
    assert "SIGKILL" in readme
    assert "does not delete arbitrary" in readme
    assert "latest invocation" in readme


def test_storage_run_doc_says_keep_files_survives_cancellation() -> None:
    """Cancellation docs must mention that ``--keep-files`` leaves stream files."""
    text = storage_bench.run.__doc__ or ""
    assert "--keep-files" in text
    assert "cancel" in text.lower()


def test_csv_column_order_is_stable_and_existing_rows_stay_in_order() -> None:
    """Appending a row does not reshuffle columns or earlier lines."""
    helper = _helper()
    assert list(helper.CSV_COLUMNS) != sorted(helper.CSV_COLUMNS)
    first = helper.render_appended_csv("", _result("b-second-alphabetically"))
    second = helper.render_appended_csv(first, _result("a-first-alphabetically"))
    lines = second.splitlines()
    assert lines[0] == ",".join(helper.CSV_COLUMNS)
    assert lines[1].split(",")[0] == "b-second-alphabetically"
    assert lines[2].split(",")[0] == "a-first-alphabetically"
    prefixed = helper.render_appended_csv("ORIGINAL_CSV", _result("row-id"))
    assert prefixed.startswith("ORIGINAL_CSV\r\n")
    assert prefixed.splitlines()[-1].split(",")[0] == "row-id"


def test_publish_fsyncs_then_replaces_then_fsyncs_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful publish fsyncs the file, replaces it, then fsyncs the directory."""
    helper = _helper()
    destination = tmp_path / "evidence.json"
    destination.write_text("ORIGINAL", encoding="utf-8")
    events: list[str] = []
    real_fsync = helper.os.fsync
    real_replace = helper.os.replace

    def track_fsync(fd: int) -> None:
        events.append(f"fsync:{os.fstat(fd).st_mode & 0o170000}")
        real_fsync(fd)

    def track_replace(src: str, dst: str) -> None:
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(helper.os, "fsync", track_fsync)
    monkeypatch.setattr(helper.os, "replace", track_replace)
    helper.publish_file(destination, lambda staging: staging.write_text("NEW", encoding="utf-8"))
    assert destination.read_text(encoding="utf-8") == "NEW"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert events[0].startswith("fsync:")
    assert events[1] == "replace"
    assert events[2].startswith("fsync:")
    assert list(tmp_path.glob("*.partial")) == []


def test_file_fsync_failure_keeps_destination_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An fsync error before replace leaves the destination bytes alone."""
    helper = _helper()
    destination = tmp_path / "evidence.json"
    destination.write_text("ORIGINAL", encoding="utf-8")

    def boom(_fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(helper.os, "fsync", boom)
    with pytest.raises(OSError, match="fsync failed"):
        helper.publish_file(destination, lambda staging: staging.write_text("NEW", encoding="utf-8"))
    assert destination.read_text(encoding="utf-8") == "ORIGINAL"
    assert list(tmp_path.glob("*.partial")) == []


def test_directory_fsync_failure_after_replace_keeps_new_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory fsync error does not truncate the file that replace committed."""
    helper = _helper()
    destination = tmp_path / "evidence.json"
    calls = {"n": 0}
    real_fsync = helper.os.fsync

    def flaky(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            real_fsync(fd)
            return
        raise OSError("dir fsync failed")

    monkeypatch.setattr(helper.os, "fsync", flaky)
    with pytest.raises(OSError, match="dir fsync failed"):
        helper.publish_file(destination, lambda staging: staging.write_text("COMMITTED", encoding="utf-8"))
    assert destination.read_text(encoding="utf-8") == "COMMITTED"
    assert list(tmp_path.glob("*.partial")) == []


def test_mkstemp_failure_does_not_touch_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mkstemp failure leaves the destination and creates no temporary file."""
    helper = _helper()
    destination = tmp_path / "evidence.json"
    destination.write_text("ORIGINAL", encoding="utf-8")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("mkstemp failed")

    monkeypatch.setattr(helper.tempfile, "mkstemp", boom)
    with pytest.raises(OSError, match="mkstemp failed"):
        helper.publish_file(destination, lambda staging: staging.write_text("NEW", encoding="utf-8"))
    assert destination.read_text(encoding="utf-8") == "ORIGINAL"
    assert list(tmp_path.glob("*.partial")) == []


def test_unlink_failure_preserves_replace_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleanup must not replace the original publish exception."""
    helper = _helper()
    destination = tmp_path / "evidence.json"
    destination.write_text("ORIGINAL", encoding="utf-8")

    def boom_replace(_src: str, _dst: str) -> None:
        raise OSError("replace failed")

    def boom_unlink(_path: str) -> None:
        raise PermissionError(errno.EACCES, "Permission denied", _path)

    monkeypatch.setattr(helper.os, "replace", boom_replace)
    monkeypatch.setattr(helper.os, "unlink", boom_unlink)
    with pytest.raises(OSError, match="replace failed") as caught:
        helper.publish_file(destination, lambda staging: staging.write_text("NEW", encoding="utf-8"))
    assert not isinstance(caught.value, PermissionError)
    assert destination.read_text(encoding="utf-8") == "ORIGINAL"
    notes = getattr(caught.value, "__notes__", [])
    assert any("temporary file remains" in note for note in notes)
    assert list(tmp_path.glob("*.partial")) != []


def test_publish_does_not_write_destination_after_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper does not open the committed path for a later rewrite."""
    helper = _helper()
    destination = tmp_path / "evidence.json"
    committed = {"value": False}
    real_replace = helper.os.replace
    real_write = Path.write_text

    def track_replace(src: str, dst: str) -> None:
        real_replace(src, dst)
        if Path(dst) == destination:
            committed["value"] = True

    def track_write(self: Path, data: str, *args: object, **kwargs: object) -> int:
        if committed["value"] and Path(self) == destination:
            raise AssertionError("write after replace")
        return real_write(self, data, *args, **kwargs)

    monkeypatch.setattr(helper.os, "replace", track_replace)
    monkeypatch.setattr(Path, "write_text", track_write)
    helper.publish_file(destination, lambda staging: staging.write_text("NEW", encoding="utf-8"))
    assert committed["value"] is True
    assert destination.read_text(encoding="utf-8") == "NEW"


def test_reconnect_attempts_zero_is_rejected_before_deleting_json(tmp_path: Path) -> None:
    """``--attempts 0`` is an argument error and leaves the previous JSON in place."""
    output = tmp_path / "reconnect.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_reconnect_benchmark.py"),
            "--attempts",
            "0",
            "--output-json",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode != 0
    assert output.read_text(encoding="utf-8") == "ORIGINAL_JSON"
    assert "attempts must be >= 1" in completed.stderr
    assert "ok=" not in completed.stdout


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--mib-per-stream", "0"), ("--chunk-mib", "0"), ("--mib-per-stream", "1e-12")],
)
def test_storage_zero_work_flags_are_rejected_before_deleting_json(tmp_path: Path, flag: str, value: str) -> None:
    """A zero-byte storage size is an argument error and keeps the previous JSON."""
    output = tmp_path / "result.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_storage_benchmark.py"),
            "--path",
            str(tmp_path / "data"),
            "--streams",
            "1",
            "--mib-per-stream",
            "1",
            "--chunk-mib",
            "1",
            flag,
            value,
            "--output-json",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode != 0
    assert output.read_text(encoding="utf-8") == "ORIGINAL_JSON"
    assert "must be positive" in completed.stderr


def test_storage_partial_directory_does_not_delete_json(tmp_path: Path) -> None:
    """A directory at the legacy partial path fails before the JSON is removed."""
    output = tmp_path / "result.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    partial = tmp_path / "result.json.partial"
    partial.mkdir()
    (partial / "do-not-lose").write_text("keep", encoding="utf-8")
    mib = 32 / MIB
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_storage_benchmark.py"),
            "--path",
            str(tmp_path / "data"),
            "--streams",
            "1",
            "--mib-per-stream",
            str(mib),
            "--chunk-mib",
            str(mib),
            "--sample-interval",
            "30",
            "--output-json",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode != 0
    assert output.read_text(encoding="utf-8") == "ORIGINAL_JSON"
    assert (partial / "do-not-lose").read_text(encoding="utf-8") == "keep"
    assert "directory" in completed.stderr


def test_storage_legacy_partial_symlink_is_left_in_place(tmp_path: Path) -> None:
    """A symlink at ``<name>.partial`` stays, and its target is not rewritten."""
    output = tmp_path / "result.json"
    target = tmp_path / "foreign.txt"
    partial = tmp_path / "result.json.partial"
    target.write_text("FOREIGN-PARTIAL-TARGET", encoding="utf-8")
    partial.symlink_to(target)
    mib = 32 / MIB
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "phase8_storage_benchmark.py"),
            "--path",
            str(tmp_path / "data"),
            "--streams",
            "1",
            "--mib-per-stream",
            str(mib),
            "--chunk-mib",
            str(mib),
            "--sample-interval",
            "30",
            "--output-json",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert partial.is_symlink()
    assert target.read_text(encoding="utf-8") == "FOREIGN-PARTIAL-TARGET"
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["result"]["bytes_written"] == 32
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_storage_failed_replace_does_not_append_csv_or_drop_winner(tmp_path: Path) -> None:
    """CSV grows only after replace, and a lost replace keeps the other run's JSON."""
    output = tmp_path / "result.json"
    csv_path = tmp_path / "result.csv"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    csv_path.write_text("ORIGINAL_CSV", encoding="utf-8")
    mib = 32 / MIB
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import phase8_storage_benchmark as bench\n"
        "def lose_replace(src, dst):\n"
        "    if str(dst).endswith('.json'):\n"
        "        Path(dst).write_text('WINNER_JSON')\n"
        "    raise OSError('replace failed')\n"
        "bench.os.replace = lose_replace\n"
        "sys.argv = ['phase8_storage_benchmark.py', '--path', sys.argv[2], '--streams', '1',\n"
        "            '--mib-per-stream', sys.argv[3], '--chunk-mib', sys.argv[3],\n"
        "            '--sample-interval', '30', '--output-json', sys.argv[4],\n"
        "            '--output-csv', sys.argv[5]]\n"
        "bench.main()\n"
    )
    completed = _run_tool(script, str(tmp_path / "data"), str(mib), str(output), str(csv_path))
    assert completed.returncode != 0
    assert output.read_text(encoding="utf-8") == "WINNER_JSON"
    assert csv_path.read_text(encoding="utf-8") == "ORIGINAL_CSV"
    assert "bytes=" not in completed.stdout


def test_storage_success_locks_rates_mode_and_csv_order(tmp_path: Path) -> None:
    """Published rates match the byte formula, and both files are mode 0600."""
    output = tmp_path / "result.json"
    csv_path = tmp_path / "result.csv"
    mib = 32 / MIB
    old_umask = os.umask(0)
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOLS / "phase8_storage_benchmark.py"),
                "--path",
                str(tmp_path / "data"),
                "--streams",
                "1",
                "--mib-per-stream",
                str(mib),
                "--chunk-mib",
                str(mib),
                "--sample-interval",
                "30",
                "--output-json",
                str(output),
                "--output-csv",
                str(csv_path),
            ],
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT_SECONDS,
            check=False,
        )
    finally:
        os.umask(old_umask)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    written = payload["result"]["bytes_written"]
    duration = payload["workload"]["duration_seconds"]
    assert written == 32
    assert duration > 0
    assert payload["result"]["aggregate_write_mbps"] == written * 8 / duration / 1_000_000
    assert payload["result"]["aggregate_write_MBps"] == written / duration / 1_000_000
    assert payload["result"]["aggregate_write_mbps"] == payload["result"]["aggregate_write_MBps"] * 8
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(csv_path.stat().st_mode) == 0o600
    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    helper = _helper()
    assert header == ",".join(helper.CSV_COLUMNS)


def test_reconnect_post_replace_write_cannot_truncate_json(tmp_path: Path) -> None:
    """A write aimed at the committed JSON must not be how the CSV row is stored."""
    output = tmp_path / "reconnect.json"
    csv_path = tmp_path / "reconnect.csv"
    csv_path.write_text("ORIGINAL_CSV", encoding="utf-8")
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import phase8_reconnect_benchmark as bench\n"
        "def sample(self):\n"
        "    return {'monotonic': 0.0, 'cpu_pct': 0.0, 'cpu_freq_mhz': None,\n"
        "            'cpu_freq_max_mhz': None, 'max_temperature_c': None, 'temperatures': [],\n"
        "            'ram_used_bytes': 1, 'ram_pct': 0.0, 'net_rx_mbps': 0.0, 'net_tx_mbps': 0.0,\n"
        "            'disk_read_mbps': 0.0, 'disk_write_mbps': 0.0, 'gpu': []}\n"
        "bench.SystemSampler.sample = sample\n"
        "async def healthy(*_args, **_kwargs):\n"
        "    return True, 0.0\n"
        "bench.attempt = healthy\n"
        "output = Path(sys.argv[2])\n"
        "real_replace = bench.os.replace\n"
        "real_write = Path.write_text\n"
        "committed = {'json': False}\n"
        "def tracking_replace(src, dst):\n"
        "    real_replace(src, dst)\n"
        "    if Path(dst) == output:\n"
        "        committed['json'] = True\n"
        "def tracking_write(self, data, *args, **kwargs):\n"
        "    if committed['json'] and Path(self) == output:\n"
        "        real_write(self, '')\n"
        "        raise OSError(28, 'No space left on device')\n"
        "    return real_write(self, data, *args, **kwargs)\n"
        "bench.os.replace = tracking_replace\n"
        "Path.write_text = tracking_write\n"
        "sys.argv = ['phase8_reconnect_benchmark', '--host', '127.0.0.1', '--port', '9',\n"
        "            '--attempts', '2', '--concurrency', '1', '--warmup-attempts', '0',\n"
        "            '--timeout', '0.2', '--sample-interval', '30', '--output-json', sys.argv[2],\n"
        "            '--output-csv', sys.argv[3]]\n"
        "bench.main()\n"
    )
    completed = _run_tool(script, str(output), str(csv_path))
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "phase8-benchmark-v1"
    assert payload["workload"]["type"] == "tcp-reconnect-storm"
    raw = csv_path.read_bytes()
    assert raw.startswith(b"ORIGINAL_CSV\r\n")
    assert b"tcp-reconnect-storm" in raw
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_noncooperative_shutdown_is_bounded_and_fails(tmp_path: Path) -> None:
    """A workload that ignores cancellation must fail inside the shutdown bound."""
    script = (
        "import asyncio\n"
        "import logging\n"
        "import sys\n"
        "from types import SimpleNamespace\n"
        "logging.basicConfig(level=logging.ERROR)\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import phase8_reconnect_benchmark as bench\n"
        "bench._SHUTDOWN_JOIN_SECONDS = "
        + str(SHUTDOWN_BOUND_SECONDS)
        + "\n"
        "entered = asyncio.Event()\n"
        "def sample(self):\n"
        "    return {'monotonic': 0.0, 'cpu_pct': 0.0, 'cpu_freq_mhz': None,\n"
        "            'cpu_freq_max_mhz': None, 'max_temperature_c': None, 'temperatures': [],\n"
        "            'ram_used_bytes': 1, 'ram_pct': 0.0, 'net_rx_mbps': 0.0, 'net_tx_mbps': 0.0,\n"
        "            'disk_read_mbps': 0.0, 'disk_write_mbps': 0.0, 'gpu': []}\n"
        "bench.SystemSampler.sample = sample\n"
        "async def ignore(*_args, **_kwargs):\n"
        "    entered.set()\n"
        "    while True:\n"
        "        try:\n"
        "            await asyncio.sleep(30)\n"
        "        except asyncio.CancelledError:\n"
        "            continue\n"
        "bench.attempt = ignore\n"
        "async def body():\n"
        "    args = SimpleNamespace(host='127.0.0.1', port=9, attempts=4, warmup_attempts=0,\n"
        "                           concurrency=1, timeout=0.2, sample_interval=30.0)\n"
        "    task = asyncio.create_task(bench.run(args))\n"
        "    await asyncio.wait_for(entered.wait(), 2)\n"
        "    task.cancel()\n"
        "    await task\n"
        "runner = getattr(bench, '_run_benchmark', None)\n"
        "if runner is None:\n"
        "    runner = asyncio.run\n"
        "runner(body())\n"
    )
    output = tmp_path / "reconnect.json"
    output.write_text("ORIGINAL_JSON", encoding="utf-8")
    started = time.monotonic()
    completed = _run_tool(script, timeout=4.0)
    elapsed = time.monotonic() - started
    assert elapsed < 4.0
    assert completed.returncode != 0
    assert "ignored cancellation" in completed.stderr
    assert "ok=" not in completed.stdout
    assert output.read_text(encoding="utf-8") == "ORIGINAL_JSON"


def test_eexist_after_ownership_check_is_not_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file refused by ``O_EXCL`` stays on disk even if it appears mid-call."""
    data = tmp_path / "data"
    data.mkdir()
    real_open = storage_bench.os.open
    state = {"used": False}

    def planted_open(path: object, flags: int, mode: int = 0o777, *args: object, **kwargs: object) -> int:
        text = os.fspath(path)
        if text.endswith(".bin") and not state["used"]:
            state["used"] = True
            descriptor = real_open(path, flags, mode)
            os.close(descriptor)
            Path(text).write_bytes(b"VICTIM-FILE-NOT-OURS")
            raise FileExistsError(errno.EEXIST, "File exists", text)
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(storage_bench.os, "open", planted_open)
    with pytest.raises(storage_bench.StorageBenchmarkWriteError, match="pre-existing"):
        asyncio.run(storage_bench.run(_storage_namespace(data)))
    victim = list(data.glob("phase8-storage-*.bin"))
    assert len(victim) == 1
    assert victim[0].read_bytes() == b"VICTIM-FILE-NOT-OURS"


def test_keyboard_interrupt_during_join_still_unlinks_stream_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second interrupt during the join must not skip stream cleanup."""

    def boom(_state: object, _timeout: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(storage_bench, "_join_write_workers", boom)
    data = tmp_path / "data"
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(storage_bench.run(_storage_namespace(data)))
    assert list(data.glob("phase8-storage-*.bin")) == []
