# Intelligent VMS

A commercial-grade Video Management System: camera onboarding, 24/7 recording,
playback, evidentiary export, PTZ, events and alarms, storage management,
analytics integration and enterprise administration.

> ## Release maturity: **0.1.0-alpha**
>
> This is an engineering build, not a production release.
> `app/version.py` sets `PRODUCTION_READY = False` and the API reports it.
>
> **157 automated tests pass**, including real FFmpeg recording that produces
> playable MP4 files and a real export whose SHA-256 is independently
> re-verified. **No part of this has been run against a physical camera**, a
> MediaMTX gateway, a PostgreSQL server, or a Windows or Ubuntu host.
>
> Read [`docs/requirements/feature-matrix.md`](docs/requirements/feature-matrix.md)
> for the status of all 301 tracked requirements and
> [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) for what does not work.

## Quick start

```bash
git clone <repository> && cd intelligent-vms

python -m venv .venv && source .venv/bin/activate
pip install -e packages/shared-contracts -e services/control-api -e services/recording-service

export VMS_DATA_ROOT=./var/data
export VMS_DATABASE_URL="sqlite+pysqlite:///$PWD/var/vms.db"
export VMS_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"

(cd database && python -m alembic -c alembic.ini upgrade head)
python -m uvicorn app.main:create_app --factory --app-dir services/control-api --port 8080
```

Then open <http://localhost:8080/api/v1/docs>. The first-run administrator
password is written to `$VMS_DATA_ROOT/first-run-credentials.txt` (mode 0600).
**Sign in, change it, delete the file.**

Live video additionally needs MediaMTX — see
[`deploy/docker`](deploy/docker). Without it, recording, playback and export
work normally and live view returns a clear `503 media_gateway_unavailable`.

## Running the tests

```bash
pip install pytest pytest-asyncio PyYAML
python -m pytest            # 157 tests; needs ffmpeg on PATH for ~6 of them
```

Every test builds its schema from the shipped Alembic migration, so the
migration is exercised on every run.

## Repository layout

| Path | Contents |
| --- | --- |
| `packages/shared-contracts/` | Enums and DTOs shared by every service and client |
| `services/control-api/` | Control plane: identity, devices, events, storage, API |
| `services/recording-service/` | Supervised FFmpeg segment recorders |
| `database/` | Alembic environment and migrations |
| `apps/web-client/` | React + TypeScript administration client (**not built**) |
| `apps/windows-client/` | .NET 8 WPF operator client (**not built**) |
| `deploy/` | Docker Compose, systemd units, Windows service scripts |
| `contracts/openapi.json` | Generated API contract (88 paths) |
| `docs/` | Architecture, ADRs, deployment, security, requirements, verification |
| `tools/` | Feature-matrix generator, SBOM generator, diagnostic bundler |
| `tests/` | Unit and integration tests |

## Architecture in one paragraph

Three process boundaries, chosen so that **a failure in live streaming or
analytics can never stop recording**: the control API (Python/FastAPI), the
recording service (supervised FFmpeg processes), and a third-party media
gateway (MediaMTX) for RTSP/WebRTC transport. FFmpeg is invoked as a separate
process rather than linked, which isolates faults and keeps its licence
obligation at the packaging boundary. Reasoning and costs are in
[`docs/decisions/`](docs/decisions/README.md).

## Security posture

| Control | Implementation |
| --- | --- |
| Camera credentials | AES-256-GCM, master key outside the database, AAD-bound per camera |
| Passwords | Argon2id (OWASP parameters; production refuses weaker) |
| Sessions | Short JWTs with server-side revocation; refresh rotation with reuse detection |
| Authorization | Server-enforced permissions plus site/camera scope, **fail-closed** |
| SSRF | Every camera-supplied URL passes a destination guard with DNS pinning |
| Discovery | Hard range cap — this product will not scan large networks |
| Audit | Append-only trail with correlation IDs; separate privacy log for FR/ANPR |

Details and threat model: [`docs/security/`](docs/security/threat-model.md).

## Honest scope

What is genuinely implemented, what is partial, and what is merely designed is
published per requirement in the feature matrix — generated from
`docs/requirements/requirements.yaml`, never hand-edited. Endpoints backing
unimplemented capabilities return `501 not_implemented` with an explanation
rather than a partial result. Capacity figures are labelled as model output,
not benchmarks. Analytics results always record whether a camera, this server,
or an external engine produced them.

## Licence

`LICENSE` is a placeholder. Choose and apply a real licence before
distribution, and review the third-party obligations in `NOTICE` and
`docs/operations/third-party-licenses.md` — particularly FFmpeg and the
H.264/H.265 patent pools.
