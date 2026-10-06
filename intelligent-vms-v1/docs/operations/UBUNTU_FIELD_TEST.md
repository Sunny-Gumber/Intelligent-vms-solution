# Ubuntu Field-Test Deployment Baseline

Issue: #301

## Qualification boundary

This is the deterministic field-test installation path for Intelligent VMS. Product
status remains **Release Candidate / External Qualification Pending**. Green automation
is container/deployment evidence, not physical-machine qualification, camera/codec
qualification, production capacity evidence, or **Production Qualified** evidence.

| Target | Architecture | Automated validation | Physical-machine qualification |
|---|---|---|---|
| Ubuntu Server/Desktop 22.04 LTS | x86_64 | Required CI matrix | Pending |
| Ubuntu Server/Desktop 24.04 LTS | x86_64 | Required CI matrix | Pending |

ARM64 and other distributions are outside this baseline.

## Reused architecture

The field-test runtime reuses the existing Compose stack: PostgreSQL, Redpanda,
ClickHouse, MediaMTX, control API, event/ONVIF/alarm workers, placement controller and
web client. It creates no second database, media layer, recorder or playback service.
Kubernetes/Helm remains the production-oriented deployment contract.

## Prerequisites and install

Install Git, Python 3, OpenSSL, curl, Docker Engine and Docker Compose v2. Docker must
be running. No CPU/RAM/channel number here is a certified production sizing claim.

Fresh checkout procedure:

1. Change into intelligent-vms-v1.
2. Run: python3 deploy/field-test/generate_env.py --recordings-dir /srv/intelligent-vms/recordings --camera-cidr 192.168.1.0/24
3. Run: bash deploy/field-test/vmsctl.sh install

The generator creates gitignored .env mode 0600, high-entropy runtime secrets, a
2048-bit live-view signing key and an explicit host recording directory. Secret values
are not printed. Keep VMS_SECRET_KEY stable because stored camera credentials depend on
it. Installation performs OS/architecture/runtime preflight, Compose render/build,
explicit Alembic upgrade from the deployed control-api image, service startup and health
wait. Field-test mode uses AUTO_CREATE_SCHEMA=false.

## Login and camera onboarding

Run python3 deploy/field-test/mint_access_token.py and paste the sensitive short-lived
token only into the VMS Login field at http://localhost:8080. The existing JWT
validation exchanges it for a same-origin HttpOnly session plus CSRF token. The bearer
token is not stored in localStorage or a URL. Local HS256 is field-test bootstrap only;
production OIDC rules remain unchanged and fail closed.

The browser includes a minimal Add IP camera form that reuses the existing authorized
camera-create API, encrypted credential storage and target validation. Use tenant
field-test and site site-01. Existing ONVIF APIs remain available; this does not claim
a complete browser ONVIF discovery wizard or named-device qualification.

## Ports and firewall

8080/TCP web, 8000/TCP API, 9998/TCP MediaMTX metrics, 8123/TCP ClickHouse admin,
8889/TCP WebRTC signaling, 8554/TCP RTSP and 8888/TCP HLS bind to localhost by
default. WebRTC media uses 8189/UDP and may require a site-specific firewall rule.
For Ubuntu Server, prefer SSH TCP tunneling rather than exposing the local HS256/HTTP
field-test session on an untrusted LAN.

## Storage and persistence

PostgreSQL and ClickHouse use Compose named volumes. Recordings use the explicit host
path selected at generation time and mounted internally at /recordings. Configuration
and secrets remain in gitignored .env. Stop, restart, upgrade and normal uninstall
preserve named volumes, recordings and .env. Purge requires the exact
VMS_CONFIRM_PURGE=DELETE_FIELD_TEST_DATABASE_VOLUMES confirmation and still does not
delete the host recording directory or .env. Do not use docker compose down -v for
routine administration. Recording filesystem durability remains external qualification.

## Lifecycle and reboot

Use deploy/field-test/vmsctl.sh with status, stop, start, restart, health, diagnostics,
backup, restore-postgres, upgrade, uninstall or purge. Generated field-test configuration
uses restart: unless-stopped. CI verifies service restart plus database/config/recording
path persistence. Actual host reboot and physical-camera recording continuity remain
manual external evidence.

## Diagnostics

Diagnostics creates a mode-0600 tar.gz with bounded service state, OS/runtime versions,
Alembic current state, API/media health, recording-volume free space and the latest
500 lines from selected service logs. It records configuration presence only, never
.env contents, and redacts configured secret/token/password/private-key values plus
credential-like patterns.

## Backup, restore, upgrade and rollback

Field-test backup captures a PostgreSQL custom dump/checksum, Compose file and repository
commit. By default it does NOT back up .env/secrets, ClickHouse history or recording
media. Database/config backup is not video backup. Use protected filesystem/storage
snapshot or replication for recording media and the existing Phase-9 ClickHouse/
regional/Helm tools when those data sets are required.

PostgreSQL restore requires CONFIRM_RESTORE=YES, verifies checksum when present, stops
writers, restores PostgreSQL, migrates to current head and restarts. It never alters
recording media. Preserve the original VMS_SECRET_KEY when restoring encrypted camera
credentials.

Upgrade first creates a field-test PostgreSQL backup, then rebuilds/migrates/health
checks. Roll back the application only when the schema is backward compatible;
otherwise restore the approved pre-upgrade state instead of forcing an Alembic
downgrade.

Uninstall removes containers/network only; named database volumes, recordings, .env
and backups are preserved. Follow docs/testing/UBUNTU_FIELD_TEST_SMOKE.md for the
complete field workflow.

## Secure source configuration

See [RTSP/RTSPS source and certificate trust](../operations/SECURE_CAMERA_SOURCES.md). Existing sources default to RTSP; select RTSPS explicitly for a secure camera. Recording uses MAIN; normal browser live prefers SUB. Real-camera qualification remains external.
