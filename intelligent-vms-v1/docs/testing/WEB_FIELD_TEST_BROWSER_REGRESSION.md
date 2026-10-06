# Web field-test login blocker and browser regression gate

Tracking: [issue #25](https://github.com/Sunny-Gumber/Intelligent-vms-solution/issues/25).
The issue's acceptance record identifies the fixing PR, exact reviewed head,
merge/main SHA and executable post-merge workflow evidence. Use that merge SHA
for the next external retest; do not infer acceptance from this source document.

Product status remains **Release Candidate / External Qualification Pending**.

## Root cause and complete source audit

Accepted starting main was `c93e55040b2596c9da21de513037b739c8f6cd3b`.
The checked-in inline script used `${API}` outside a template literal. A browser
rejects the entire script before initialization or Login can execute. Successful
curl session calls therefore proved the backend but missed an unusable browser.
Existing frontend marker assertions did not parse JavaScript.

All interpolation/backslash occurrences, inline event handlers and neighboring
markup in `web/index.html` were audited. Source defects and original line numbers:

| Original line | Source location | Correction |
|---|---|---|
| 83 | healthSummary / aiSummary boundary | Actual newline replaces visible literal `\n` |
| 177 | initializeSession | Valid API/session template literal |
| 191 | loginWithToken | Valid API/session template literal |
| 198 | signOut | Valid API/session template literal |
| 220 | saveCamera | Valid API/cameras template literal |
| 347 | parseIceServers | Single-escaped regex whitespace; double escaping matched literal backslashes |
| 363 | startTile WHEP URL | Correct regex slash escape; previous expression also prevented parsing |

These are malformed checked-in source/escaping defects. No claim is made about
an unverified generator or author that introduced them. No unrelated UI redesign,
backend/auth change, migration, installer change or feature expansion is included.

## Regression execution

From `intelligent-vms-v1/`, with Node 22 and the existing Python test dependencies:

```sh
python tools/check_frontend_syntax.py
python -m pytest -q tests/test_frontend_syntax.py tests/test_field_test_browser_session.py
```

The HTML parser extracts complete inline scripts and inline event handlers; Node
performs a real JavaScript syntax check with UTF-8 input and bounded execution.
Missing/unclosed scripts and unsupported external scripts fail closed. Visible
literal markup escapes are rejected without rejecting valid JavaScript escapes.
Negative regressions independently reject all four old interpolation forms and
the old WHEP regex; a complete-page mutation reinstates the original login defect.

For a **disposable loopback field-test stack**, generated private `.env` and the
existing `127.0.0.1/32` test-camera allowlist:

```sh
python -m pip install -r tests/requirements-browser.txt
python -m playwright install --with-deps chromium
python tools/field_test_browser_smoke.py --base-url http://127.0.0.1:8080 --env-file .env
```

Playwright 1.63.0 is a pinned test-only Chromium driver. It is separate from
production dependencies and uses the existing Python test ecosystem rather than
adding an application frontend framework. Both Ubuntu 22.04 and 24.04 workflows
run it against the deployed nginx/Control API/PostgreSQL/MediaMTX stack without API
mocks. Existing persistence, health, redaction and uninstall assertions remain.
VMS CI also runs syntax checking and prior-defect regressions.

The browser smoke verifies initialization, empty/invalid/expired login rejection,
valid bearer exchange, cleared input, visible field-test/admin identity, API-backed
health and camera UI, session restoration on reload, no local/session storage or
token URLs/console output, HttpOnly `/api` and Strict SameSite session cookies,
double-submit CSRF on mutation, tenant/site rejection, real camera-form POST and
password clearing, public-response credential exclusion, ICE-header parsing and
sign-out cookie removal followed by a 401 and locked UI after reload.
The loopback camera is simulated; it is never selected for live video and is
deleted through the authorized API after the request regression.

Temporary tokens are minted in memory from that job's newly generated secrets.
No operator-exposed token is reused. No screenshots, traces, storage-state files,
token output or credential-bearing exception details are persisted. Successful
sign-out clears browser cookies; this preserves the existing stateless JWT design
and does not claim server-side revocation of an already issued bearer token.

## External smoke truth and next checkpoint

The supplied external Ubuntu 24.04.5.1 Desktop x86_64 VM evidence at starting main:

| Check | External result |
|---|---|
| Configuration generation / preflight / deployment | PASS |
| Docker/Compose stack / database migration through 0018 | PASS |
| Server health / MediaMTX / web container / page load | PASS |
| GUI JavaScript initialization | FAIL |
| Login / camera onboarding / live / recording / playback / clip export | BLOCKED |
| Functional restart and host reboot | NOT YET TESTED |

CI does not replace the external run. After the fixing PR is accepted and its
post-merge CI passes, stop engineering work. Resume the **existing Ubuntu VM from
browser Login**, using the exact merge SHA recorded in issue #25:

```sh
cd ~/Intelligent-vms-solution
git fetch origin
git checkout <accepted-merge-sha-from-issue-25>
cd intelligent-vms-v1
bash deploy/field-test/vmsctl.sh upgrade
bash deploy/field-test/vmsctl.sh health
python3 deploy/field-test/mint_access_token.py --ttl-seconds 600
```

Open `http://localhost:8080`; verify no syntax error, successful Login, hidden auth
panel, field-test/admin identity and available Add IP camera. Use a new temporary
token only, without screenshots/logging. Then follow `UBUNTU_FIELD_TEST_SMOKE.md`
for real camera, real footage, playback/export, restart/reboot and safe diagnostics.

Windows 10/11, Ubuntu external functional smoke, real-camera/codec compatibility,
capacity/performance, production signing and production security qualification
remain pending. Do not start another feature after this blocker milestone.
