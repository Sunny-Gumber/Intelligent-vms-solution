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
RESTARTED_SESSION_URL = "https://media.example/whep/sessions/restarted-2"
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


def test_whep_post_that_completes_while_authenticated_stays_live():
    """A WHEP POST that finishes while the operator is signed in stays live."""
    result = run_scenario("authenticated-post-stays-live")
    assert_page_reached_inflight_whep(result)
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
        "authenticated": True,
        "auth_required": False,
        "auth_panel_display": "none",
        "live_session_count": 1,
        "tile_state": "LIVE",
        "media_deletes": [],
        "peer_closed": False,
        "remote_description_applied": True,
    }


def assert_retry_overlap_stays_signed_out(result: dict) -> None:
    """A cleanup still in flight must not restart live video after sign-out.

    Args:
        result: Observations after the held cleanup DELETE is released.
    """
    assert result["authenticated"] is False
    assert result["auth_required"] is True
    assert result["auth_panel_display"] == "block"
    assert result["live_session_count"] == 0
    assert result["tile_state"] == "EMPTY"
    assert result["whep_post_count"] == 1
    assert result["open_peer_count"] == 0
    assert result["media_deletes"] == [{"url": SESSION_URL, "authorization": GRANT}]
    assert result["unknown_requests"] == []


def test_retry_overlap_failed_logout_does_not_go_live():
    """A rejected logout must not let the in-flight restart become LIVE."""
    result = run_scenario("retry-overlap-logout-rejected")
    assert_retry_overlap_stays_signed_out(result)


def test_retry_overlap_logout_http_401_does_not_go_live():
    """An HTTP 401 from logout must not let the in-flight restart become LIVE."""
    result = run_scenario("retry-overlap-logout-401")
    assert_retry_overlap_stays_signed_out(result)


def test_sign_out_during_pending_sdp_body_closes_peer_and_deletes():
    """Sign-out closes the peer and DELETEs a known Location before the SDP body arrives."""
    result = run_scenario("sign-out-during-sdp-body")
    assert result["during_sign_out"] == {
        "authenticated": False,
        "live_session_count": 0,
        "tile_state": "EMPTY",
        "media_deletes": [{"url": SESSION_URL, "authorization": GRANT}],
        "peer_closed": True,
        "open_peer_count": 0,
    }
    assert_late_response_cancelled(result, remote_description_applied=False)


def test_relogin_before_old_post_deletes_old_session_and_keeps_new_live():
    """Re-login before the old POST lands deletes that session and keeps the new one LIVE."""
    result = run_scenario("relogin-before-old-post")
    assert result["script_bytes"] > 10000
    assert str(result["page_path"]).endswith("web/index.html")
    assert result["unknown_requests"] == []
    assert {
        "authenticated": result["authenticated"],
        "auth_required": result["auth_required"],
        "auth_panel_display": result["auth_panel_display"],
        "live_session_count": result["live_session_count"],
        "tile_state": result["tile_state"],
        "whep_post_count": result["whep_post_count"],
        "media_deletes": result["media_deletes"],
        "open_peer_count": result["open_peer_count"],
    } == {
        "authenticated": True,
        "auth_required": False,
        "auth_panel_display": "none",
        "live_session_count": 1,
        "tile_state": "LIVE",
        "whep_post_count": 2,
        "media_deletes": [{"url": SESSION_URL, "authorization": GRANT}],
        "open_peer_count": 1,
    }
    assert RESTARTED_SESSION_URL not in {item["url"] for item in result["media_deletes"]}


def assert_signed_out_without_live(result: dict) -> None:
    """A resumed layout, refresh, or login must not leave live video after sign-out.

    Args:
        result: Observations after every held step was released.
    """
    assert result["script_bytes"] > 10000
    assert str(result["page_path"]).endswith("web/index.html")
    assert result["authenticated"] is False
    assert result["auth_required"] is True
    assert result["auth_panel_display"] == "block"
    assert result["live_session_count"] == 0
    assert result["open_peer_count"] == 0
    assert result["unknown_requests"] == []
    assert result["whep_sessions"]
    assert all(not str(state).startswith("LIVE") for state in result["tile_states"])
    deleted = {item["url"] for item in result["media_deletes"]}
    assert set(result["whep_sessions"]) <= deleted
    assert all(item["authorization"] == GRANT for item in result["media_deletes"])


def test_layout_shrink_during_failed_logout_does_not_go_live():
    """setLayout must not restart a tile when logout fails while a shrink DELETE is held."""
    result = run_scenario("layout-shrink-logout-rejected")
    assert_signed_out_without_live(result)
    assert result["whep_post_count"] == 2


def test_relogin_camera_refresh_during_failed_logout_does_not_go_live():
    """A camera list that arrives after a second failed logout must not go live."""
    result = run_scenario("relogin-cameras-held-logout-rejected")
    assert_signed_out_without_live(result)
    assert result["whep_post_count"] == 1


def test_held_layout_refresh_and_login_do_not_go_live_after_sign_out():
    """Held setLayout, refreshCameras, and login steps must not start video after sign-out."""
    result = run_scenario("auth-epoch-held-steps")
    assert_signed_out_without_live(result)
    assert result["whep_post_count"] == 2


def assert_no_auth_delete_after_login(result: dict) -> None:
    """A stale sign-out must not DELETE the browser session after the new login POST.

    Args:
        result: Observations that include auth request order.
    """
    posts = [index for index, item in enumerate(result["auth_requests"]) if item.startswith("POST ")]
    assert posts, result["auth_requests"]
    last_login = posts[-1]
    assert all(not item.startswith("DELETE ") for item in result["auth_requests"][last_login + 1 :])


def test_sign_out_overlapping_login_keeps_the_new_session():
    """Login during a held sign-out DELETE must not be revoked or left with a signed-out banner."""
    result = run_scenario("sign-out-overlaps-login")
    assert result["authenticated"] is True
    assert result["auth_message"] == "Authenticated."
    assert result["auth_panel_display"] == "none"
    assert result["live_session_count"] == 1
    assert result["open_peer_count"] == 1
    assert str(result["tile_state"]).startswith("LIVE")
    assert result["whep_post_count"] == 2
    assert result["media_deletes"] == [{"url": "https://media.example/whep/sessions/s-1", "authorization": GRANT}]
    assert "https://media.example/whep/sessions/s-2" not in {item["url"] for item in result["media_deletes"]}
    assert_no_auth_delete_after_login(result)
    assert result["unknown_requests"] == []


def test_pagehide_during_layout_does_not_restart_live():
    """pagehide must drop tile labels and stop a layout resume from opening a new session."""
    result = run_scenario("pagehide-during-layout")
    assert result["during_pagehide"]["live_session_count"] == 0
    assert result["during_pagehide"]["open_peer_count"] == 0
    assert all(not str(state).startswith("LIVE") for state in result["during_pagehide"]["tile_states"])
    assert result["live_session_count"] == 0
    assert result["open_peer_count"] == 0
    assert result["whep_post_count"] == 2
    assert all(not str(state).startswith("LIVE") for state in result["tile_states"])
    deleted = {item["url"] for item in result["media_deletes"]}
    assert set(result["whep_sessions"]) <= deleted
    assert "https://media.example/whep/sessions/s-3" not in deleted
    assert "https://media.example/whep/sessions/s-3" not in result["whep_sessions"]
    assert result["unknown_requests"] == []


def test_stale_health_401_does_not_sign_out_a_newer_login():
    """A health 401 from before re-login must not clear the new authenticated session."""
    result = run_scenario("stale-health-401-after-login")
    assert result["authenticated"] is True
    assert result["auth_message"] == "Authenticated."
    assert result["auth_required"] is False
    assert result["live_session_count"] == 1
    assert result["open_peer_count"] == 1
    assert str(result["tile_state"]).startswith("LIVE")
    assert "https://media.example/whep/sessions/s-1" in {item["url"] for item in result["media_deletes"]}
    assert "https://media.example/whep/sessions/s-2" not in {item["url"] for item in result["media_deletes"]}
    assert result["unknown_requests"] == []


def test_stale_cameras_401_does_not_wipe_the_new_live_grid():
    """A camera-list 401 from the previous login must not replace the new live grid."""
    result = run_scenario("stale-cameras-401-after-login")
    assert result["authenticated"] is True
    assert result["auth_message"] == "Authenticated."
    assert result["auth_required"] is False
    assert result["live_session_count"] == 1
    assert result["open_peer_count"] == 1
    assert result["whep_post_count"] == 2
    assert str(result["tile_state"]).startswith("LIVE")
    assert result["tile_states"]
    assert all(not str(state).startswith("FAILED") for state in result["tile_states"])
    assert "Control API unavailable" not in result["content_text"]
    assert "Gate" in result["content_text"]
    assert result["media_deletes"] == [{"url": "https://media.example/whep/sessions/s-1", "authorization": GRANT}]
    assert "https://media.example/whep/sessions/s-2" not in {item["url"] for item in result["media_deletes"]}
    assert_no_auth_delete_after_login(result)
    assert result["unknown_requests"] == []


def test_stale_cameras_success_does_not_overwrite_the_new_grid():
    """A camera list from the previous login must not rename the new login's grid."""
    result = run_scenario("stale-cameras-success-after-login")
    assert result["authenticated"] is True
    assert result["auth_message"] == "Authenticated."
    assert result["live_session_count"] == 1
    assert result["open_peer_count"] == 1
    assert str(result["tile_state"]).startswith("LIVE")
    assert "Gate" in result["content_text"]
    assert "Stale Yard" not in result["content_text"]
    assert "Control API unavailable" not in result["content_text"]
    assert len(result["tile_states"]) == 4
    assert "https://media.example/whep/sessions/s-2" not in {item["url"] for item in result["media_deletes"]}
    assert result["unknown_requests"] == []


def test_stale_panel_fetches_do_not_overwrite_the_new_login():
    """Events, alarms, health panels, and similar stale fetches must not rewrite the new login."""
    result = run_scenario("stale-panels-after-login")
    assert result["authenticated"] is True
    assert result["auth_message"] == "Authenticated."
    assert result["live_session_count"] == 1
    assert result["open_peer_count"] == 1
    assert str(result["tile_state"]).startswith("LIVE")
    assert "STALE_EVENT" not in result["event_text"]
    assert "No events in the selected window." in result["event_text"]
    assert "STALE_ALARM" not in result["alarm_text"]
    assert "No open alarms." in result["alarm_text"]
    assert "77" not in result["ai_text"]
    assert "AI enabled 0" in result["ai_text"]
    assert result["events_display"] == "block"
    assert result["alarms_display"] == "block"
    assert result["ai_display"] == "flex"
    assert result["ai_message"] == "Loading…"
    assert result["diag_text"] == "Loading…"
    assert "STALE_DIAG" not in result["diag_text"]
    assert result["manual_status"] == "No active manual recording"
    assert result["recording_status"] == "Continuous recording not configured · default retention 7 days · source MAIN"
    assert result["alerts"] == []
    assert "Control API unavailable" not in result["content_text"]
    assert result["unknown_requests"] == []


def test_stale_recording_policy_after_pagehide_does_not_apply():
    """A recording policy response that resumes after pagehide must not change the controls."""
    result = run_scenario("stale-policy-after-pagehide")
    assert result["authenticated"] is True
    assert "Continuous MAIN enabled" not in result["recording_status"]
    assert "9 days" not in result["recording_status"]
    assert result["live_session_count"] == 0
    assert result["open_peer_count"] == 0
    assert all(not str(state).startswith("LIVE") for state in result["tile_states"])
    assert result["unknown_requests"] == []


def test_deferred_whep_post_after_auth_expiry_does_not_leave_a_live_session():
    """Auth expiry (HTTP 401) must cancel an in-flight WHEP POST the same way."""
    result = run_scenario("deferred-post-auth-expiry")
    assert_page_reached_inflight_whep(result)
    assert_late_response_cancelled(result, remote_description_applied=False)
