from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SYSTEM = REPO_ROOT / "agent-system"
sys.path.insert(0, str(AGENT_SYSTEM))

import boss  # noqa: E402
import openai_responses  # noqa: E402
import orchestrator  # noqa: E402
import reviewer  # noqa: E402
import state  # noqa: E402
import worker  # noqa: E402


def valid_task() -> dict[str, object]:
    return {
        "task_id": "TEST-001",
        "title": "Agent Orchestrator Automation Test",
        "output_path": "docs/agents/AUTOMATION_TEST.md",
        "instructions": "Create the TEST-001 automation test file with the required content.",
        "acceptance_criteria": {
            "output_path": "docs/agents/AUTOMATION_TEST.md",
            "required_phrases": [
                "Agent Orchestrator Automation Test",
                "TEST-001",
                "Automation test passed",
                "created by the Worker stage",
            ],
            "worker_stage_statement": True,
            "forbidden_content": ["secrets", "credentials", "OPENAI_API_KEY"],
            "allow_product_code_changes": False,
        },
    }


def test_state_loading_saving(tmp_path: Path) -> None:
    path = tmp_path / "agent-state.json"
    loaded = state.load_state(path)
    assert loaded["stage"] == "INITIALIZED"

    loaded["stage"] = "READY_FOR_WORKER"
    state.save_state(path, loaded)

    saved = state.load_state(path)
    assert saved["stage"] == "READY_FOR_WORKER"
    assert saved["task_id"] == "TEST-001"


def test_valid_test_001_task() -> None:
    assert boss.validate_task(valid_task())["task_id"] == "TEST-001"


def test_worker_refuses_unexpected_task_id(tmp_path: Path) -> None:
    task = valid_task()
    task["task_id"] = "TEST-999"

    with pytest.raises(ValueError, match="unexpected task ID"):
        worker.run_worker(task, repo_root=tmp_path)


def test_worker_refuses_unsafe_output_path(tmp_path: Path) -> None:
    task = valid_task()
    task["output_path"] = "intelligent-vms-v1/services/control-api/app/main.py"

    with pytest.raises(ValueError, match="unsafe output path"):
        worker.run_worker(task, repo_root=tmp_path)


def test_worker_creates_expected_file(tmp_path: Path) -> None:
    result = worker.run_worker(valid_task(), repo_root=tmp_path)
    target = tmp_path / "docs/agents/AUTOMATION_TEST.md"

    assert result["changed_files"] == ["docs/agents/AUTOMATION_TEST.md"]
    text = target.read_text(encoding="utf-8")
    assert "Agent Orchestrator Automation Test" in text
    assert "TEST-001" in text
    assert "Automation test passed" in text
    assert "created by the Worker stage" in text


def test_reviewer_acceptance_parsing() -> None:
    assert reviewer.parse_review_decision("ACCEPTED") == "ACCEPTED"
    assert reviewer.parse_review_decision("CHANGES_REQUIRED") == "CHANGES_REQUIRED"


def test_reviewer_invalid_response_rejection() -> None:
    with pytest.raises(ValueError, match="invalid decision"):
        reviewer.parse_review_decision("ACCEPTED because it looks good")


def test_maximum_attempt_handling(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prompts = tmp_path / "agent-system/prompts"
    prompts.mkdir(parents=True)
    (prompts / "boss.md").write_text("boss", encoding="utf-8")
    (prompts / "reviewer.md").write_text("reviewer", encoding="utf-8")

    monkeypatch.setattr(orchestrator, "run_boss", lambda **_: valid_task())
    monkeypatch.setattr(orchestrator, "run_worker", lambda task, repo_root: {"changed_files": [worker.SAFE_OUTPUT_PATH]})
    monkeypatch.setattr(orchestrator, "run_reviewer", lambda *args, **kwargs: "CHANGES_REQUIRED")

    final = orchestrator.run_orchestrator(repo_root=tmp_path)

    assert final["attempt"] == 2
    assert final["stage"] == "FAILED"
    assert final["status"] == "FAILED"
    assert final["review_result"] == "CHANGES_REQUIRED"


def test_missing_openai_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(openai_responses.MissingOpenAIKeyError):
        openai_responses.require_openai_api_key()


def test_no_secret_value_appearing_in_logs_or_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret = "dummy-openai-secret-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    prompts = tmp_path / "agent-system/prompts"
    prompts.mkdir(parents=True)
    (prompts / "boss.md").write_text("boss", encoding="utf-8")
    (prompts / "reviewer.md").write_text("reviewer", encoding="utf-8")

    monkeypatch.setattr(orchestrator, "run_boss", lambda **_: valid_task())
    monkeypatch.setattr(orchestrator, "run_reviewer", lambda *args, **kwargs: "ACCEPTED")

    final = orchestrator.run_orchestrator(repo_root=tmp_path)
    output = capsys.readouterr().out
    state_text = (tmp_path / "agent-state.json").read_text(encoding="utf-8")

    assert final["status"] == "ACCEPTED"
    assert secret not in output
    assert secret not in state_text
    assert "OPENAI_API_KEY" not in state_text
    json.loads(state_text)


def test_reviewer_local_check_requires_expected_file_text(tmp_path: Path) -> None:
    worker_result = worker.run_worker(valid_task(), repo_root=tmp_path)
    file_text = (tmp_path / "docs/agents/AUTOMATION_TEST.md").read_text(encoding="utf-8")

    assert reviewer.local_acceptance_check(valid_task(), worker_result, file_text) is True
    assert reviewer.local_acceptance_check(valid_task(), worker_result, "bad text") is False


def test_openai_json_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        output_text = json.dumps(valid_task())

    class Responses:
        @staticmethod
        def create(**kwargs: object) -> Response:
            return Response()

    class Client:
        responses = Responses()

    module = type(sys)("openai")
    module.OpenAI = lambda: Client()
    monkeypatch.setitem(sys.modules, "openai", module)
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-test-key")

    result = openai_responses.call_openai_json(
        instructions="boss",
        user_input="{}",
        schema=boss.TASK_SCHEMA,
        model="gpt-6-luna",
    )

    assert result["task_id"] == "TEST-001"
