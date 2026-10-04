"""Run the bounded Boss -> Worker -> Reviewer TEST-001 flow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from boss import TASK_ID, run_boss
from openai_responses import DEFAULT_MODEL
from reviewer import ACCEPTED, CHANGES_REQUIRED, run_reviewer
from state import DEFAULT_STATE, load_state, save_state, update_state
from worker import run_worker


MAX_ATTEMPTS = 2


def _safe_error(exc: BaseException) -> str:
    return f"{exc.__class__.__name__}: {exc}"


def run_orchestrator(
    *,
    repo_root: Path = Path("."),
    state_path: Path = Path("agent-state.json"),
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Run the TEST-001 state machine and persist state after every stage."""

    state_path = repo_root / state_path
    state = load_state(state_path)
    state = dict(DEFAULT_STATE, **state)
    state["task_id"] = TASK_ID
    state["status"] = "RUNNING"
    save_state(state_path, state)

    print("Agent Orchestrator V1")
    print(f"Task: {TASK_ID}")
    print()

    task: dict[str, Any] | None = None
    worker_result: dict[str, Any] | None = None

    try:
        while state["attempt"] < MAX_ATTEMPTS:
            attempt = state["attempt"] + 1
            state = update_state(
                state_path,
                state,
                attempt=attempt,
                stage="BOSS_RUNNING",
                current_agent="Boss",
                last_error=None,
            )
            task = run_boss(prompt_path=repo_root / "agent-system/prompts/boss.md", model=model)
            state = update_state(state_path, state, stage="READY_FOR_WORKER", current_agent=None)
            print("Boss: completed")

            state = update_state(state_path, state, stage="WORKER_RUNNING", current_agent="Worker")
            worker_result = run_worker(task, repo_root=repo_root)
            state = update_state(
                state_path,
                state,
                stage="READY_FOR_REVIEW",
                current_agent=None,
                changed_files=worker_result["changed_files"],
            )
            print("Worker: completed")

            state = update_state(state_path, state, stage="REVIEW_RUNNING", current_agent="Reviewer")
            decision = run_reviewer(
                task,
                worker_result,
                repo_root=repo_root,
                prompt_path=repo_root / "agent-system/prompts/reviewer.md",
                model=model,
            )
            state = update_state(state_path, state, review_result=decision, current_agent=None)
            print(f"Reviewer: {decision}")

            if decision == ACCEPTED:
                state = update_state(state_path, state, stage=ACCEPTED, status=ACCEPTED)
                break

            state = update_state(state_path, state, stage=CHANGES_REQUIRED, status=CHANGES_REQUIRED)
            if attempt >= MAX_ATTEMPTS:
                state = update_state(
                    state_path,
                    state,
                    stage="FAILED",
                    status="FAILED",
                    last_error="maximum attempts reached",
                )
                break
        else:
            state = update_state(
                state_path,
                state,
                stage="FAILED",
                status="FAILED",
                last_error="maximum attempts reached",
            )
    except Exception as exc:  # noqa: BLE001 - final state must record safe failures.
        state = update_state(
            state_path,
            state,
            stage="FAILED",
            status="FAILED",
            current_agent=None,
            last_error=_safe_error(exc),
        )
        raise
    finally:
        print()
        final = load_state(state_path)
        print(f"Final status: {final['status']}")

    return load_state(state_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the TEST-001 agent orchestrator")
    parser.add_argument("--state-path", default="agent-state.json")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    final_state = run_orchestrator(
        repo_root=Path("."),
        state_path=Path(args.state_path),
        model=args.model,
    )
    print(json.dumps({"stage": final_state["stage"], "status": final_state["status"]}, sort_keys=True))
    return 0 if final_state["status"] == ACCEPTED else 1


if __name__ == "__main__":
    raise SystemExit(main())
