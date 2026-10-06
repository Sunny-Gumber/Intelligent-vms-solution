"""Camera protocol, trust, migration and security regression coverage."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from pydantic import ValidationError
import pytest
import sqlalchemy as sa
import yaml

from app.core.config import settings
from app.core.security import encrypt_secret
from app.models.entities import CameraEntity
from app.models.schemas import CameraCreate, CameraCredentialUpdate, CameraReplacement
from app.routers import cameras
from app.services import camera_lifecycle, mediamtx, network_policy, recording, reconciler
from app.services.rtsp import build_rtsp_uri
from tests.test_camera_lifecycle_track_a1 import Session, camera, principal

ROOT = Path(__file__).parents[1]
PIN = 'ab' * 32


def test_runtime_docker_build_context_contains_verified_builder_inputs():
    """Resolve actual Compose context and ensure Docker COPY inputs exist there."""
    build = yaml.safe_load((ROOT / 'compose.yaml').read_text())['services']['mediamtx']['build']
    context = ROOT / build['context']
    dockerfile = context / build['dockerfile']
    for line in dockerfile.read_text().splitlines():
        if line.startswith('COPY ') and '--from=' not in line:
            assert (context / line.split()[1]).is_file()


@pytest.fixture(autouse=True)
def source_policy(monkeypatch):
    """Configure isolated synthetic credentials and an exact site allowlist."""
    monkeypatch.setattr(settings, 'vms_secret_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    monkeypatch.setattr(settings, 'placement_execution_enabled', False)
    monkeypatch.setattr(settings, 'onvif_site_allowed_cidrs_json', '{"tenant-a/site-a":["192.168.1.0/24"]}')


@pytest.mark.parametrize('protocol', ['rtsp', 'rtsps'])
def test_protocol_uri_credentials_and_ipv6(protocol):
    uri = build_rtsp_uri('192.168.1.20', 554, '/0?channel=1', 'synthetic@/é', 'p@ss:/?#', source_protocol=protocol)
    assert uri == protocol + '://synthetic%40%2F%C3%A9:p%40ss%3A%2F%3F%23@192.168.1.20:554/0?channel=1'
    assert build_rtsp_uri('2001:db8::1', 554, '/1', None, None, source_protocol=protocol) == protocol + '://[2001:db8::1]:554/1'


@pytest.mark.parametrize('protocol', ['http', 'https', 'file', 'ftp', 'javascript', 'RTSPS', 'arbitrary', ''])
def test_invalid_protocol_rejected_before_provisioning(protocol):
    for model in (CameraCreate, CameraReplacement):
        with pytest.raises(ValidationError):
            model(site_id='site-a', name='Synthetic', host='192.168.1.20', main_path='/0', source_protocol=protocol)
    with pytest.raises(ValueError, match='unsupported camera source protocol'):
        build_rtsp_uri('192.168.1.20', 554, '/0', 'synthetic', 'private-marker', source_protocol=protocol)


@pytest.mark.parametrize('host,port,path', [('host@elsewhere', 554, '/0'), ('host/path', 554, '/0'),
                                        ('192.168.1.20', 0, '/0'), ('192.168.1.20', 65536, '/0'),
                                        ('192.168.1.20', 554, '/0#fragment'), ('192.168.1.20', 554, '/0\\escape'),
                                        ('192.168.1.20', 554, '/0?%70assword=private-marker'),
                                        ('192.168.1.20', 554, '/http://elsewhere'),
                                        ('192.168.1.20', 554, '/0\r\nprivate-marker')])
def test_uri_injection_rejected_without_echo(host, port, path):
    with pytest.raises(ValueError) as error:
        build_rtsp_uri(host, port, path, 'synthetic', 'private-marker', source_protocol='rtsps')
    assert 'private-marker' not in str(error.value)


@pytest.mark.parametrize('protocol', ['rtsp', 'rtsps'])
def test_target_policy_has_no_protocol_bypass(protocol):
    CameraCreate(site_id='site-a', name='Synthetic', host='203.0.113.5', main_path='/0', source_protocol=protocol)
    with pytest.raises(network_policy.TargetNotAllowed):
        network_policy.validate_site_camera_rtsp_target('203.0.113.5', 554, '/0', 'tenant-a', 'site-a')
    with pytest.raises(network_policy.TargetNotAllowed):
        network_policy.validate_site_camera_rtsp_target('192.168.1.20', 554, '/0', 'tenant-a', 'other-site')


@pytest.mark.parametrize('protocol', ['rtsp', 'rtsps'])
def test_create_and_public_serialization(protocol, monkeypatch):
    class CreateSession(Session):
        def add(self, value):
            self.cam = value

        async def flush(self):
            from tests.time_control import FIXED_NOW
            self.cam.id = 'synthetic-camera'
            self.cam.created_at = FIXED_NOW
            self.cam.media_node_id = 'media-local-01'
            self.cam.enabled = True

    session = CreateSession()
    provision = AsyncMock()
    monkeypatch.setattr(cameras.mediamtx, 'add_or_replace_path', provision)
    payload = CameraCreate(tenant_id='tenant-a', site_id='site-a', name='Synthetic', host='192.168.1.20',
                           main_path='/0', sub_path='/1', third_path='/2', username='synthetic',
                           password='private-marker', source_protocol=protocol,
                           source_fingerprint=PIN if protocol == 'rtsps' else None)
    result = asyncio.run(cameras.create_camera(payload, session, principal()))
    assert result.source_protocol == protocol
    assert 'private-marker' not in result.model_dump_json()
    assert 'username' not in result.model_dump() and 'password' not in result.model_dump()
    assert session.cam.password_enc != 'private-marker'
    assert len(provision.await_args_list) == 3
    assert [c.args[1].split('@')[1].split(':554')[1] for c in provision.await_args_list] == ['/1', '/0', '/2']
    assert all(c.args[1].startswith(protocol + '://') for c in provision.await_args_list)
    assert all(c.kwargs.get('source_fingerprint') == (PIN if protocol == 'rtsps' else None) for c in provision.await_args_list)
    with pytest.raises(HTTPException) as error:
        asyncio.run(cameras.create_camera(payload, session, principal(tenant_id='other')))
    assert error.value.status_code == 404


@pytest.mark.parametrize('protocol', ['rtsp', 'rtsps'])
def test_provisioning_errors_and_logs_never_echo_credentials(protocol, monkeypatch, caplog):
    class CreateSession(Session):
        def add(self, value):
            self.cam = value

        async def flush(self):
            self.cam.id = 'synthetic-camera'

    monkeypatch.setattr(cameras.mediamtx, 'add_or_replace_path', AsyncMock(side_effect=RuntimeError('private-marker')))
    monkeypatch.setattr(cameras.mediamtx, 'delete_path', AsyncMock())
    payload = CameraCreate(tenant_id='tenant-a', site_id='site-a', name='Synthetic', host='192.168.1.20',
                           main_path='/0', username='synthetic', password='private-marker', source_protocol=protocol)
    with pytest.raises(HTTPException) as error:
        asyncio.run(cameras.create_camera(payload, CreateSession(), principal()))
    assert 'private-marker' not in str(error.value.detail) + caplog.text


def test_replacement_protocol_rotation_and_rollback(monkeypatch):
    cam = camera()
    cam.source_protocol = 'rtsp'
    cam.source_fingerprint = None
    session = Session(cam=cam)
    monkeypatch.setattr(cameras, 'commit_source_mutation', AsyncMock())
    monkeypatch.setattr(cameras, 'to_read', lambda entity, node=None: entity)
    for protocol in ('rtsps', 'rtsp', 'rtsps'):
        payload = CameraReplacement(host='192.168.1.20', main_path='/0', sub_path='/1',
                                    source_protocol=protocol, source_fingerprint=PIN if protocol == 'rtsps' else None)
        asyncio.run(cameras.replace_camera(cam.id, payload, session, principal()))
        assert cam.id == 'camera-1' and cam.stream_key == 'site-a-gate'
        assert cam.source_protocol == protocol
    snapshot = camera_lifecycle.source_snapshot(cam)
    asyncio.run(cameras.update_camera_credentials(cam.id, CameraCredentialUpdate(password='temporary'), session, principal()))
    assert cam.source_protocol == 'rtsps' and cam.source_fingerprint == PIN
    asyncio.run(cameras.replace_camera(cam.id, CameraReplacement(host='192.168.1.20', main_path='/0'), session, principal()))
    assert cam.source_protocol == 'rtsps'  # Omission never silently downgrades TLS.
    cam.source_protocol = 'rtsp'
    assert camera_lifecycle._recording_source_changed(cam, snapshot)
    camera_lifecycle.restore_source_snapshot(cam, snapshot)
    assert cam.source_protocol == 'rtsps' and cam.source_fingerprint == PIN


def test_all_roles_rebuild_from_persisted_secure_source(monkeypatch):
    cam = camera()
    cam.source_protocol, cam.source_fingerprint = 'rtsps', PIN
    cam.main_path, cam.sub_path, cam.third_path = '/0', '/1', '/2'
    cam.username_enc, cam.password_enc = encrypt_secret('synthetic'), encrypt_secret('private-marker')
    for function, path in [(camera_lifecycle.live_source, '/1'), (reconciler.source_for_camera, '/1'),
                           (camera_lifecycle.main_live_source, '/0'), (camera_lifecycle.third_source, '/2'),
                           (recording.recording_source, '/0')]:
        assert function(cam).startswith('rtsps://') and function(cam).endswith(path)


def test_media_options_clear_stale_pin_and_reject_invalid_input():
    assert mediamtx.source_options('rtsps://192.168.1.20:554/0', PIN.upper())['sourceFingerprint'] == PIN
    assert mediamtx.source_options('rtsp://192.168.1.20:554/0', None)['sourceFingerprint'] == ''
    for uri, pin in [('http://synthetic:private-marker@192.168.1.20/0', None),
                     ('rtsps://192.168.1.20/0', 'invalid'), ('rtsp://192.168.1.20/0', PIN)]:
        with pytest.raises(mediamtx.MediaMTXError) as error:
            mediamtx.source_options(uri, pin)
        assert 'private-marker' not in str(error.value)
    with pytest.raises(ValidationError):
        CameraCreate(site_id='site-a', name='Synthetic', host='192.168.1.20', main_path='/0', source_fingerprint=PIN)


def test_distributed_reconciliation_and_recording_use_protocol_and_pin(monkeypatch):
    from tests.test_category1_review_regressions import _camera, _assignment, _reconcile, _MediaClient
    from tests.test_category1_safe_camera_lifecycle import policy
    from tests.time_control import FrozenDateTime
    monkeypatch.setattr(reconciler, 'datetime', FrozenDateTime)
    cam = _camera()
    cam.source_protocol, cam.source_fingerprint = 'rtsps', PIN

    class Client(_MediaClient):
        async def add_or_replace_path(self, key, source, **trust):
            assert trust == {'source_fingerprint': PIN}
            assert source.startswith('rtsps://')
            await super().add_or_replace_path(key, source)

    assignment = _assignment(cam, dirty=True)
    client = Client()
    changed, failed, _ = _reconcile(monkeypatch, cam, assignment, {'media-new': client})
    assert changed and failed == 0 and len(client.added) == 3
    assert assignment.applied_generation == assignment.generation
    add_record = AsyncMock()
    recorder = mediamtx.MediaMTXClient()
    monkeypatch.setattr(recorder, 'add_or_replace_recording_path', add_record)
    asyncio.run(recording.provision_recording(cam, policy(), client=recorder))
    assert add_record.await_args.args[1].endswith(cam.main_path)
    assert add_record.await_args.kwargs['source_fingerprint'] == PIN


def test_protocol_and_trust_mutations_refresh_distributed_recording(monkeypatch):
    cam = camera()
    cam.source_protocol, cam.source_fingerprint = 'rtsps', PIN
    snapshot = camera_lifecycle.source_snapshot(cam)
    cam.source_fingerprint = 'cd' * 32
    monkeypatch.setattr(settings, 'placement_execution_enabled', True)
    dirty = AsyncMock()
    monkeypatch.setattr(camera_lifecycle, 'mark_distributed_source_dirty', dirty)
    asyncio.run(camera_lifecycle.finalize_source_mutation(Session(cam), cam, None, snapshot))
    assert dirty.await_args.kwargs['refresh_recording'] is True


def test_rtsps_diagnostics_redact_encoded_credentials():
    from tools.field_test_diagnostics import redactor
    value = redactor({})('rtsps://synthetic%40user:private-marker@192.0.2.5:554/0')
    assert 'private-marker' not in value and 'synthetic%40user' not in value


def test_migrate_existing_row_default_constraint_and_downgrade_guard(tmp_path, monkeypatch):
    database = tmp_path / 'migration.db'
    monkeypatch.setattr(settings, 'database_url', 'sqlite+aiosqlite:///' + database.as_posix())
    # Avoid Alembic's fileConfig disabling application loggers in later tests.
    config = Config()
    config.set_main_option('script_location', str(ROOT / 'migrations'))
    command.upgrade(config, '0018')
    engine = sa.create_engine('sqlite:///' + database.as_posix())
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO cameras(id,tenant_id,site_id,name,host,rtsp_port,main_path,stream_key,media_node_id,enabled,desired_state,created_at,updated_at) VALUES ('legacy','tenant-a','site-a','Synthetic','192.168.1.20',554,'/0','legacy','media-local-01',1,'provisioned',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    command.upgrade(config, '0019')
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT source_protocol FROM cameras WHERE id='legacy'")).scalar() == 'rtsp'
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text("UPDATE cameras SET source_protocol='http' WHERE id='legacy'"))
        connection.rollback()
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE cameras SET source_protocol='rtsps' WHERE id='legacy'"))
    with pytest.raises(RuntimeError, match='explicit reconfiguration'):
        command.downgrade(config, '0018')
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE cameras SET source_protocol='rtsp' WHERE id='legacy'"))
    command.downgrade(config, '0018')
    command.upgrade(config, '0019')
    engine.dispose()
