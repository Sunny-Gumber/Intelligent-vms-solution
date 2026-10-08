"""Behavior tests for browser logout cancelling in-flight WHEP sessions.

The harness executes the real ``web/index.html`` script. These tests do not
assert on source text. A late WHEP POST or SDP answer must not leave the
signed-out page with a live session, and it must DELETE the session the late
response created.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


PAGE = Path(__file__).parents[1] / "web" / "index.html"
HARNESS = Path(__file__).with_name("browser_live_logout_harness.js")
SESSION_URL = "https://media.example/whep/sessions/pending-1"
GRANT = "Bearer live-grant-token"
HARNESS_TIMEOUT_SECONDS = 30


def run_scenario(scenario: str) -> dict:
    """Run one in-flight WHEP scenario and return the observed page state.

    Args:
        scenario: Harness scenario name.

    Returns:
        JSON observations from the real page script, including auth state,
        live-session count, tile text, and media DELETE calls.

    Raises:
        AssertionError: If the harness cannot execute the page.
    """
    completed = subprocess.run(
        ["node", str(HARNESS), scenario, str(PAGE)],
        check=False,
        capture_output=True,
        text=True,
        timeout=HARNESS_TIMEOUT_SECONDS,
    )
    stdout = completed.stdout.strip()
    if completed.returncode != 0 or not stdout:
        raise AssertionError(
            f"scenario {scenario} failed to run\n"
            f"exit={completed.returncode}\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )
    payload = json.loads(stdout.splitlines()[-1])
    if payload.get("error"):
        raise AssertionError(f"{payload['error']}\nrejections={payload.get('rejections')}")
    return payload


def assert_page_reached_inflight_whep(result: dict) -> None:
    """Confirm the scenario actually started a WHEP session before logout."""
    assert result["script_bytes"] > 10000
    assert str(result["page_path"]).endswith("web/index.html")
    assert result["access_role"] == "main"
    assert result["whep_post_count"] == 1
    assert result["peer_count"] == 1
    assert result["unknown_requests"] == []


def assert_late_response_cancelled(result: dict, *, remote_description_applied: bool) -> None:
    """Assert logout or expiry left no live session and deleted the late one.

    Args:
        result: Observations from the real page script.
        remote_description_applied: Whether the SDP answer was applied before
            the in-flight start was cancelled. A held ``setRemoteDescription``
            can finish and must still be torn down.
    """
    assert {
        "authenticated": result["authenticated"],
        "auth_required": result["auth_required"],
        "auth_panel_display": result["auth_panel_display"],
        "live_session_count": result["live_session_count"],
        "tile_state": result["tile_state"],
        "media_deletes": result["media_deletes"],
        "peer_closed": result["peer_closed"],
        "remote_description_applied": result["remote_description_applied"],
    } == {
        "authenticated": False,
        "auth_required": True,
        "auth_panel_display": "block",
        "live_session_count": 0,
        "tile_state": "EMPTY",
        "media_deletes": [{"url": SESSION_URL, "authorization": GRANT}],
        "peer_closed": True,
        "remote_description_applied": remote_description_applied,
    }


def test_deferred_whep_post_after_sign_out_does_not_leave_a_live_session():
    """A WHEP POST that resolves after sign-out must be deleted and not go live."""
    result = run_scenario("deferred-post-sign-out")
    assert_page_reached_inflight_whep(result)
    assert_late_response_cancelled(result, remote_description_applied=False)


def test_deferred_sdp_answer_after_sign_out_deletes_the_session():
    """An SDP answer body that arrives after sign-out must DELETE that session."""
    result = run_scenario("deferred-sdp-sign-out")
    assert_page_reached_inflight_whep(result)
    assert_late_response_cancelled(result, remote_description_applied=False)


def test_deferred_remote_description_after_sign_out_does_not_register_the_session():
    """A setRemoteDescription that finishes after sign-out must not stay live."""
    result = run_scenario("deferred-answer-sign-out")
    assert_page_reached_inflight_whep(result)
    assert_late_response_cancelled(result, remote_description_applied=True)


def test_deferred_whep_post_after_auth_expiry_does_not_leave_a_live_session():
    """Auth expiry (HTTP 401) must cancel an in-flight WHEP POST the same way."""
    result = run_scenario("deferred-post-auth-expiry")
    assert_page_reached_inflight_whep(result)
    assert_late_response_cancelled(result, remote_description_applied=False)
