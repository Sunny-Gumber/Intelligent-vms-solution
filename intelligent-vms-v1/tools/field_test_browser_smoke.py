#!/usr/bin/env python3
"""Exercise real field-test browser authentication/camera APIs on loopback only.

No API mocking, trace, screenshot, storage-state file or credential output is used.
This is software smoke evidence, not real-camera or external qualification.
"""

import argparse
import importlib.util
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]


class _CheckpointFailure(RuntimeError):
    pass


def _require(value, checkpoint):
    if not value:
        raise _CheckpointFailure(checkpoint)


def _tokens(env_file):
    spec = importlib.util.spec_from_file_location("field_token", ROOT / "deploy/field-test/mint_access_token.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    values = module.env(env_file)
    _require(values.get("AUTH_DISABLED", "").lower() == "false", "authentication must be enabled")
    _require(values.get("AUTH_REQUIRE_OIDC", "").lower() != "true", "local field-test profile required")
    secret = values.get("AUTH_HS256_SECRET", "")
    _require(bool(secret), "private field-test signing configuration required")
    audience = values.get("AUTH_AUDIENCE", "intelligent-vms")

    def mint(key, ttl):
        return module.mint_token(key, audience, "browser-smoke", "field-test", "site-01", "admin", ttl)

    return mint(secret, 600), mint(secret, -60), mint(secrets.token_urlsafe(32), 600)


def _submit_token(page, token, status):
    page.locator("#authToken").fill(token)
    with page.expect_response(lambda r: r.url.endswith("/api/v1/auth/session")
                              and r.request.method == "POST") as response:
        page.get_by_role("button", name="Login", exact=True).click()
    _require(response.value.status == status, "session exchange status")
    expect(page.locator("#authToken")).to_have_value("")


def _check_browser_state(page, tokens, urls, console_messages):
    _require(page.evaluate("localStorage.length === 0 && sessionStorage.length === 0"), "no browser storage")
    visible = page.locator("body").inner_text()
    _require(r"\n" not in visible, "no visible generated markup escape")
    _require(all(token not in value for token in tokens for value in [visible, page.url, *urls, *console_messages]),
             "no token in DOM, URL or console")
    _require("vms_session=" not in page.evaluate("document.cookie"), "session remains HttpOnly")


def run_smoke(base_url: str, env_file: Path) -> None:
    """Run bounded Chromium login/session/security/camera-form checks.

    Args:
        base_url: Loopback URL of the running disposable field-test web stack.
        env_file: Private generated configuration used to mint temporary tokens.

    Raises:
        RuntimeError: A checkpoint failed or target/configuration is unsafe.
        Exception: A browser/transport assertion fails (CLI redacts its details).
    """
    parsed = urlsplit(base_url)
    _require(parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
             and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
             and parsed.path in {"", "/"}, "loopback field-test URL required")
    base_url = base_url.rstrip("/")
    valid, expired, invalid = tokens = _tokens(env_file)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            with browser.new_context() as context:
                context.set_default_timeout(15000)
                page = context.new_page()
                page_errors, urls, console_messages = [], [], []
                page.on("pageerror", lambda error: page_errors.append(type(error).__name__))
                page.on("request", lambda request: urls.append(request.url))
                page.on("console", lambda message: console_messages.append(message.text))
                page.goto(base_url, wait_until="networkidle")
                expect(page.locator("#system")).to_have_text("login required")
                expect(page.locator("#authPanel")).to_be_visible()
                page.get_by_role("button", name="Login", exact=True).click()
                expect(page.locator("#authMessage")).to_have_text("Paste a valid VMS access token.")
                for token in (invalid, expired):
                    _submit_token(page, token, 401)
                    expect(page.locator("#authPanel")).to_be_visible()
                    _require(not any(c["name"] == "vms_session" for c in context.cookies()), "no rejected session")
                _submit_token(page, valid, 200)
                expect(page.locator("#authPanel")).to_be_hidden()
                expect(page.locator("#identity")).to_contain_text("field-test")
                expect(page.locator("#identity")).to_contain_text("admin")
                expect(page.locator("#system")).to_contain_text("system: ok")
                expect(page.locator("#healthSummary")).to_contain_text("online")
                _check_browser_state(page, tokens, urls, console_messages)
                cookies = {c["name"]: c for c in context.cookies()}
                session, csrf = cookies["vms_session"], cookies["vms_csrf"]
                _require(session["httpOnly"] and session["sameSite"] == "Strict" and session["path"] == "/api",
                         "session cookie boundaries")
                _require(not csrf["httpOnly"] and csrf["sameSite"] == "Strict" and csrf["path"] == "/",
                         "CSRF cookie boundaries")
                page.reload(wait_until="networkidle")
                expect(page.locator("#authPanel")).to_be_hidden()
                expect(page.locator("#identity")).to_contain_text("field-test")

                payload = {"tenant_id": "field-test", "site_id": "site-01", "name": "Browser CI Loopback Camera",
                           "host": "127.0.0.1", "rtsp_port": 8554, "main_path": "/browser-ci-main"}
                camera_url = base_url + "/api/v1/cameras"
                for headers in ({}, {"X-VMS-CSRF": "mismatch"}):
                    response = context.request.post(camera_url, data=payload, headers=headers)
                    _require(response.status == 403, "missing/mismatched CSRF rejected")
                headers = {"X-VMS-CSRF": csrf["value"]}
                for change in ({"tenant_id": "other-tenant"}, {"site_id": "other-site"}):
                    response = context.request.post(camera_url, data={**payload, **change}, headers=headers)
                    _require(response.status == 404, "tenant/site scope preserved")

                page.get_by_role("button", name="Add IP camera", exact=True).click()
                expect(page.locator("#cameraSetup")).to_be_visible()
                for field, value in (("camName", payload["name"]), ("camHost", payload["host"]),
                                     ("camPort", "8554"), ("camMain", payload["main_path"])):
                    page.locator("#" + field).fill(value)
                camera_password = secrets.token_urlsafe(24)
                page.locator("#camUser").fill("browser-ci-user")
                page.locator("#camPassword").fill(camera_password)
                with page.expect_response(lambda r: r.url == camera_url and r.request.method == "POST") as created:
                    page.get_by_role("button", name="Add camera", exact=True).click()
                response = created.value
                _require(response.status == 201, "camera form dispatched successfully")
                request = response.request
                _require(request.header_value("X-VMS-CSRF") == csrf["value"], "camera form sends CSRF")
                _require(request.header_value("Authorization") is None, "camera form uses session only")
                body = request.post_data_json
                _require(all(body[key] == value for key, value in payload.items()), "camera form payload")
                _require(body["password"] == camera_password and body["sub_path"] is None, "camera form secret/optional fields")
                expect(page.locator("#camPassword")).to_have_value("")
                expect(page.locator("#cameraSetupMessage")).to_contain_text("Camera added.")
                expect(page.locator(".camera-picker").first).to_contain_text(payload["name"])
                _require(camera_password not in response.text(), "camera API does not expose credentials")
                camera_id = response.json()["id"]
                _require(context.request.delete(camera_url + "/" + camera_id, headers=headers).status == 204,
                         "disposable camera cleanup")

                ice = page.evaluate("""() => parseIceServers('<stun:stun.example.test:3478>; rel="ice-server", '
                    + '<turn:turn.example.test:3478>; rel="ice-server"; username="temporary"; '
                    + 'credential="temporary"; credential-type="password"')""")
                _require(len(ice) == 2 and ice[1].get("username") == "temporary", "ICE Link-header whitespace")
                _require(page.evaluate("parseIceServers('invalid').length") == 0, "malformed ICE header")
                _check_browser_state(page, tokens, urls, console_messages)
                with page.expect_response(lambda r: r.url.endswith("/api/v1/auth/session")
                                          and r.request.method == "DELETE") as logout:
                    page.locator("#signOut").click()
                _require(logout.value.status == 200, "sign-out accepted")
                expect(page.locator("#authPanel")).to_be_visible()
                _require(not any(c["name"] in {"vms_session", "vms_csrf"} for c in context.cookies()), "cookies cleared")
                _require(context.request.get(camera_url).status == 401, "signed-out API rejected")
                page.reload(wait_until="networkidle")
                expect(page.locator("#system")).to_have_text("login required")
                _check_browser_state(page, tokens, urls, console_messages)
                _require(not page_errors, "no browser JavaScript errors")
        finally:
            browser.close()
    print("field_test_browser_smoke_ok auth=cookie csrf=enforced camera=simulated external_qualification=pending")


def main() -> int:
    """Run the CLI without leaking request bodies, tokens or browser diagnostics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    try:
        run_smoke(args.base_url, args.env_file)
    except _CheckpointFailure as error:
        print("field_test_browser_smoke_failed checkpoint=" + str(error))
        return 1
    except Exception:
        # Browser exceptions can include submitted input/request data. Fail closed
        # with a safe summary rather than emitting a traceback or a trace artifact.
        print("field_test_browser_smoke_failed (credential-bearing diagnostics suppressed)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
