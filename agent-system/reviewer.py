"""Reviewer stage for TEST-001 cloud automation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from boss import REQUIRED_PHRASES, SAFE_OUTPUT_PATH, TASK_ID
from openai_responses import DEFAULT_MODEL, call_openai_text


ACCEPTED = "ACCEPTED"
CHANGES_REQUIRED = "CHANGES_REQUIRED"
VALID_DECISIONS = {ACCEPTED, CHANGES_REQUIRED}


def parse_review_decision(text: str) -> str:
    """Parse the Reviewer decision, rejecting anything except exact decisions."""

    decision = text.strip()
    if decision not in VALID_DECISIONS:
        raise ValueError("Reviewer returned an invalid decision")
    return decision


def local_acceptance_check(task: dict[str, Any], worker_result: dict[str, Any], file_text: str) -> bool:
    """Apply deterministic acceptance checks before honoring ACCEPTED."""

    criteria = task.get("acceptance_criteria", {})
    if task.get("task_id") != TASK_ID:
        return False
    if criteria.get("output_path") != SAFE_OUTPUT_PATH:
        return False
    if worker_result.get("changed_files") != [SAFE_OUTPUT_PATH]:
        return False
    if "secret" in file_text.lower() and "No secrets or credentials are included." not in file_text:
        return False
    return all(phrase in file_text for phrase in REQUIRED_PHRASES)


def run_reviewer(
    task: dict[str, Any],
    worker_result: dict[str, Any],
    *,
    repo_root: Path = Path("."),
    prompt_path: Path = Path("agent-system/prompts/reviewer.md"),
    model: str = DEFAULT_MODEL,
) -> str:
    """Call OpenAI for an independent review and return the bounded decision."""

    target = repo_root / SAFE_OUTPUT_PATH
    file_text = target.read_text(encoding="utf-8")
    prompt = prompt_path.read_text(encoding="utf-8")
    user_input = json.dumps(
        {
            "boss_task": task,
            "worker_result": worker_result,
            "generated_file_path": SAFE_OUTPUT_PATH,
            "generated_file_text": file_text,
            "allowed_decisions": sorted(VALID_DECISIONS),
        },
        indent=2,
    )
    decision = parse_review_decision(
        call_openai_text(
            instructions=prompt,
            user_input=user_input,
            model=model,
            max_output_tokens=20,
        )
    )
    if decision == ACCEPTED and not local_acceptance_check(task, worker_result, file_text):
        return CHANGES_REQUIRED
    return decision
