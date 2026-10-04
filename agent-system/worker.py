"""Deterministic Worker stage for TEST-001."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from boss import SAFE_OUTPUT_PATH, TASK_ID


AUTOMATION_TEST_CONTENT = """# Agent Orchestrator Automation Test

Task ID: TEST-001

Automation test passed.

This file was created by the Worker stage.

No secrets or credentials are included.
"""


def _assert_safe_task(task: dict[str, Any]) -> None:
    if task.get("task_id") != TASK_ID:
        raise ValueError("Worker refused unexpected task ID")
    if task.get("output_path") != SAFE_OUTPUT_PATH:
        raise ValueError("Worker refused unsafe output path")


def run_worker(task: dict[str, Any], *, repo_root: Path = Path(".")) -> dict[str, Any]:
    """Create the single approved TEST-001 file."""

    _assert_safe_task(task)
    target = repo_root / SAFE_OUTPUT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(AUTOMATION_TEST_CONTENT, encoding="utf-8")
    return {
        "task_id": TASK_ID,
        "status": "completed",
        "changed_files": [SAFE_OUTPUT_PATH],
    }
