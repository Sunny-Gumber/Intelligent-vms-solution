"""Boss stage for TEST-001 cloud automation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openai_responses import DEFAULT_MODEL, call_openai_json


TASK_ID = "TEST-001"
SAFE_OUTPUT_PATH = "docs/agents/AUTOMATION_TEST.md"
REQUIRED_PHRASES = [
    "Agent Orchestrator Automation Test",
    "TEST-001",
    "Automation test passed",
    "created by the Worker stage",
]

TASK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task_id", "title", "output_path", "instructions", "acceptance_criteria"],
    "properties": {
        "task_id": {"type": "string", "enum": [TASK_ID]},
        "title": {"type": "string"},
        "output_path": {"type": "string", "enum": [SAFE_OUTPUT_PATH]},
        "instructions": {"type": "string"},
        "acceptance_criteria": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "output_path",
                "required_phrases",
                "worker_stage_statement",
                "forbidden_content",
                "allow_product_code_changes",
            ],
            "properties": {
                "output_path": {"type": "string", "enum": [SAFE_OUTPUT_PATH]},
                "required_phrases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": len(REQUIRED_PHRASES),
                    "maxItems": len(REQUIRED_PHRASES),
                },
                "worker_stage_statement": {"type": "boolean"},
                "forbidden_content": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 3,
                    "maxItems": 3,
                },
                "allow_product_code_changes": {"type": "boolean"},
            },
        },
    },
}


def validate_task(task: dict[str, Any]) -> dict[str, Any]:
    """Validate the Boss task before Worker execution."""

    if task.get("task_id") != TASK_ID:
        raise ValueError("Boss task_id must be TEST-001")
    if task.get("output_path") != SAFE_OUTPUT_PATH:
        raise ValueError("Boss output_path is not the approved TEST-001 target")

    criteria = task.get("acceptance_criteria")
    if not isinstance(criteria, dict):
        raise ValueError("Boss acceptance_criteria must be an object")
    if criteria.get("output_path") != SAFE_OUTPUT_PATH:
        raise ValueError("Boss acceptance criteria must use the approved output path")

    required_phrases = criteria.get("required_phrases")
    if required_phrases != REQUIRED_PHRASES:
        raise ValueError("Boss acceptance criteria required_phrases do not match TEST-001")
    if criteria.get("forbidden_content") != ["secrets", "credentials", "OPENAI_API_KEY"]:
        raise ValueError("Boss forbidden_content must protect secrets and credentials")
    if criteria.get("worker_stage_statement") is not True:
        raise ValueError("Boss must require the Worker stage statement")
    if criteria.get("allow_product_code_changes") is not False:
        raise ValueError("Boss must forbid VMS product code changes")

    return task


def run_boss(
    *,
    prompt_path: Path = Path("agent-system/prompts/boss.md"),
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Call OpenAI and return a validated structured TEST-001 task."""

    prompt = prompt_path.read_text(encoding="utf-8")
    user_input = json.dumps(
        {
            "objective": "Create the bounded TEST-001 task only.",
            "task_id": TASK_ID,
            "safe_output_path": SAFE_OUTPUT_PATH,
            "required_phrases": REQUIRED_PHRASES,
        },
        indent=2,
    )
    task = call_openai_json(
        instructions=prompt,
        user_input=user_input,
        schema=TASK_SCHEMA,
        model=model,
        max_output_tokens=600,
    )
    return validate_task(task)
