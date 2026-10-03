from pathlib import Path
import re
import yaml
from tools.field_test_diagnostics import redactor
ROOT=Path(__file__).parents[1]; REPO=ROOT.parent
COMPOSE=yaml.safe_load((ROOT/"compose.yaml").read_text(encoding="utf-8"))
DOCKERFILE=(ROOT/"services/control-api/Dockerfile").read_text(encoding="utf-8")
WEB_DOCKERFILE=(ROOT/"services/web/Dockerfile").read_text(encoding="utf-8")
VMSCTL=(ROOT/"deploy/field-test/vmsctl.sh").read_text(encoding="utf-8")
GENERATOR=(ROOT/"deploy/field-test/generate_env.py").read_text(encoding="utf-8")
DIAGNOSTICS=(ROOT/"tools/field_test_diagnostics.py").read_text(encoding="utf-8")
WEB=(ROOT/"web/index.html").read_text(encoding="utf-8")
WORKFLOW=(REPO/".github/workflows/intelligent-vms-ubuntu-field-test.yml").read_text(encoding="utf-8")
DOC=(ROOT/"docs/operations/UBUNTU_FIELD_TEST.md").read_text(encoding="utf-8")
WINDOWS=(ROOT/"docs/reviews/WINDOWS_PORTABILITY_BLOCKERS.md").read_text(encoding="utf-8")
MEDIAMTX=yaml.safe_load((ROOT/"infra/mediamtx/mediamtx.yml").read_text(encoding="utf-8"))
def test_compose_field_test_hooks_reuse_existing_services_and_persist_recordings():
    s=COMPOSE["services"]
    for name in ("postgres","redpanda","clickhouse","mediamtx","control-api","web"):
        assert name in s and s[name]["restart"]=="${VMS_RESTART_POLICY:-no}"
    assert s["postgres"]["environment"]["POSTGRES_PASSWORD"]=="${POSTGRES_PASSWORD:-vms}"
    assert "${VMS_RECORDING_VOLUME:-recordings}:/recordings" in s["mediamtx"]["volumes"]
    for name in ("mediamtx","control-api","web"): assert "healthcheck" in s[name]
    assert "mediamtx" not in s["control-api"].get("depends_on", {})
    assert s["control-api"]["build"]=={"context":".","dockerfile":"services/control-api/Dockerfile"}
def test_control_api_image_contains_explicit_migration_assets():
    assert "COPY alembic.ini ./alembic.ini" in DOCKERFILE and "COPY migrations ./migrations" in DOCKERFILE and "USER 10001:10001" in DOCKERFILE
def test_default_lifecycle_is_non_destructive_and_purge_guarded():
    assert '"${COMPOSE[@]}" down --remove-orphans' in VMSCTL and "DELETE_FIELD_TEST_DATABASE_VOLUMES" in VMSCTL
    before,purge=VMSCTL.split("purge(){",1); assert "down -v" not in before and "down -v" in purge
    assert "rm -rf" not in VMSCTL and "rm .env" not in VMSCTL
def test_generator_uses_runtime_random_secrets_and_safe_storage():
    for marker in ("secrets.token_hex","openssl","AUTO_CREATE_SCHEMA","AUTH_DISABLED","AUTH_HS256_SECRET","AUTH_BROWSER_SESSION_ENABLED","VMS_RECORDING_VOLUME","os.chmod(OUTPUT"): assert marker in GENERATOR
    assert 'recordings_dir==Path("/")' in GENERATOR and "secret values were generated but intentionally not printed" in GENERATOR
def test_browser_login_and_camera_setup_do_not_persist_token():
    for marker in ('id="authToken"',"loginWithToken(event)","credentials:'same-origin'","vms_csrf","openCameraSetup()","saveCamera(event)"): assert marker in WEB
    assert "localStorage.setItem" not in WEB and "localStorage.getItem" not in WEB and "?token=" not in WEB
def test_diagnostics_is_bounded_and_redacted():
    assert '"--tail","500"' in DIAGNOSTICS and '"[REDACTED]"' in DIAGNOSTICS and "config-presence.json" in DIAGNOSTICS and "tarfile.open" in DIAGNOSTICS and "os.chmod(output,0o600)" in DIAGNOSTICS
def test_ubuntu_matrix_and_qualification_boundary():
    assert "ubuntu-22.04" in WORKFLOW and "ubuntu-24.04" in WORKFLOW
    for marker in ("External Qualification Pending","physical-machine qualification","x86_64","Production Qualified"): assert marker in DOC
def test_windows_inventory_required_classifications():
    for value in ("PORTABLE ALREADY","CONFIGURATION CHANGE","CODE CHANGE","SERVICE WRAPPER REQUIRED","INSTALLER REQUIRED","STORAGE/PATH CHANGE","EXTERNAL QUALIFICATION"): assert value in WINDOWS
    assert "/recordings" in WINDOWS and "/var/lib" in WINDOWS and re.search(r"Windows 11",WINDOWS)

def test_mediamtx_global_defaults_do_not_enable_on_demand_for_publisher_source():
    defaults=MEDIAMTX.get("pathDefaults",{})
    assert defaults.get("sourceOnDemand",False) is False
    assert defaults.get("record",False) is False
    media=(ROOT/"services/control-api/app/services/mediamtx.py").read_text(encoding="utf-8")
    assert '"sourceOnDemand": True' in media
    assert '"sourceOnDemand": False' in media

def test_non_root_web_container_uses_writable_pid_path():
    assert "pid /tmp/nginx.pid" in WEB_DOCKERFILE
    assert "grep -q '^pid /tmp/nginx.pid;'" in WEB_DOCKERFILE
    assert WEB_DOCKERFILE.index("pid /tmp/nginx.pid") < WEB_DOCKERFILE.index("USER 101:101")

def test_health_requires_media_node_ok():
    assert 'data.get("status")=="ok"' in VMSCTL
    assert 'data.get("media_node")=="ok"' in VMSCTL

def test_diagnostics_redacts_camera_uri_credentials():
    scrub=redactor({"VMS_SECRET_KEY":"top-secret"})
    value=scrub("camera=rtsp://admin:camera-pass@192.0.2.5/stream Bearer top-secret")
    assert "camera-pass" not in value
    assert "top-secret" not in value
    assert "rtsp://[REDACTED]@192.0.2.5/stream" in value
