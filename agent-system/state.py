"""State handling for the bounded agent orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


STAGES = {
    "INITIALIZED",
    "BOSS_RUNNING",
    "READY_FOR_WORKER",
    "WORKER_RUNNING",
    "READY_FOR_REVIEW",
    "REVIEW_RUNNING",
    "ACCEPTED",
    "CHANGES_REQUIRED",
    "FAILED",
}

DEFAULT_STATE = {
    "task_id": "TEST-001",
    "stage": "INITIALIZED",
    "current_agent": None,
    "status": "INITIALIZED",
    "attempt": 0,
    "review_result": None,
    "changed_files": [],
    "last_error": None,
}


def _validate_state(state: dict[str, Any]) -> None:
    missing = set(DEFAULT_STATE) - set(state)
    if missing:
        raise ValueError(f"state is missing required keys: {sorted(missing)}")
    if state["stage"] not in STAGES:
        raise ValueError(f"invalid state stage: {state['stage']}")
    if not isinstance(state["attempt"], int) or state["attempt"] < 0:
        raise ValueError("state attempt must be a non-negative integer")
    if not isinstance(state["changed_files"], list):
        raise ValueError("state changed_files must be a list")


def load_state(path: Path) -> dict[str, Any]:
    """Load state from disk or return a fresh initial state."""

    if not path.exists():
        return dict(DEFAULT_STATE)
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("state file must contain a JSON object")
    merged = dict(DEFAULT_STATE)
    merged.update(state)
    _validate_state(merged)
    return merged


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Validate and persist state as deterministic JSON."""

    _validate_state(state)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def update_state(path: Path, state: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """Apply explicit state changes and save immediately."""

    next_state = dict(state)
    next_state.update(changes)
    save_state(path, next_state)
    return next_state
