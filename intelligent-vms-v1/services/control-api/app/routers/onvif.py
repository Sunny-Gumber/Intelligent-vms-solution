import asyncio
import logging
from urllib.parse import urlsplit
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_roles, require_scope
from app.services.rtsp import source_trust_options
from app.core.config import settings
from app.core.security import encrypt_secret, decrypt_secret
from app.db.session import get_session
from app.models.entities import CameraEntity, CameraCapabilityEntity
from app.models.schemas import (
    CameraRead,
    CameraCapabilityRead,
    OnvifCameraNameOsdUpdate,
    OnvifDateTimeRead,
    OnvifDateTimeUpdate,
    OnvifDiscoverRequest,
    OnvifDiscoveredDevice,
    OnvifEncoderRead,
    OnvifEncoderUpdate,
    OnvifImagingRead,
    OnvifImagingUpdate,
    OnvifIrUpdate,
    OnvifCodecProfileSelect,
    OnvifManagedProfileUpdate,
    OnvifOnboardRequest,
    OnvifOsdCreate,
    OnvifOsdUpdate,
    OnvifPrivacyMaskCreate,
    OnvifPrivacyMaskUpdate,
    OnvifProbeRead,
    OnvifProbeRequest,
    OnvifQrOnboardRequest,
    OnvifSerialOnboardRequest,
    OnvifRotationUpdate,
    OnvifVideoSourceModeUpdate,
    OnvifVideoStandardUpdate,
)
from app.routers.cameras import authorized_camera, to_read
from app.services.camera_lifecycle import (
    commit_source_mutation,
    main_live_source,
    main_live_stream_key,
    prepare_source_mutation,
)
from app.services.mediamtx import mediamtx
from app.services.network_policy import (
    TargetNotAllowed,
    pin_site_http_xaddr,
    require_local_discovery_site,
    require_site_network_policy,
)
from app.services.onvif_client import (
    OnvifError,
    identify_xaddr,
    inject_rtsp_credentials,
    probe_host,
    probe_xaddr,
    public_probe,
    sanitize_http_uri,
    stream_parts,
)
from app.services.onvif_configuration import (
    create_mask,
    create_osd,
    delete_mask,
    delete_osd,
    get_date_time,
    get_encoder,
    get_imaging,
    get_orientation,
    get_video_source_modes,
    get_video_standards,
    list_masks,
    list_osds,
    profile_by_token,
    set_date_time,
    set_encoder,
    set_imaging,
    set_ir_lamp,
    set_orientation,
    set_video_source_mode,
    set_video_standard,
    supported_auxiliary_commands,
    update_mask,
    update_osd,
)
from app.services.onvif_discovery import discover
from app.services.onvif_onboarding import connection_from_xaddr, decode_qr_payload
from app.services.stream_keys import make_role_stream_key, make_stream_key

router = APIRouter(prefix="/api/v1/onvif", tags=["onvif"])
log = logging.getLogger(__name__)



async def _cleanup_provisioned_paths(stream_keys: list[str]) -> None:
    """Best-effort cleanup of media paths created during failed onboarding.

    Args:
        stream_keys: Media path keys provisioned by the current onboarding attempt.

    Returns:
        None after every supplied path has been attempted.

    Notes:
        Cleanup failures are logged without upstream exception text so a
        credential-bearing MediaMTX error cannot leak camera secrets.
    """
    for stream_key in reversed(stream_keys):
        try:
            await mediamtx.delete_path(stream_key)
        except Exception as exc:
            log.warning(
                "onvif_onboard_cleanup_failed stream_key=%s reason=%s",
                stream_key,
                exc.__class__.__name__,
            )


def _http_error(exc: Exception) -> HTTPException:
    """Translate an ONVIF route failure into a public HTTP error.

    Args:
        exc: Failure caught by an ONVIF route.

    Returns:
        Public HTTP error for a blocked target, a bounded ONVIF error, or an
        unexpected device or transport failure.

    Raises:
        HTTPException: The original expected HTTP error, unchanged. Callers
            that use ``raise _http_error(exc)`` therefore keep its status and
            detail instead of replacing it with HTTP 502.
    """
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, TargetNotAllowed):
        return HTTPException(
            400,
            {"code": "TARGET_NOT_ALLOWED", "message": "ONVIF target is not allowed"},
        )
    if isinstance(exc, OnvifError):
        return HTTPException(exc.status_code, {"code": exc.code, "message": exc.message})
    return HTTPException(502, {"code": "ONVIF_ERROR", "message": "ONVIF operation failed"})


def _resolved_site_id(
    principal: Principal,
    tenant_id: str,
    requested_site_id: str | None,
) -> str:
    """Resolve one authorized site while preserving single-site client compatibility.

    Args:
        principal: Authenticated caller scope.
        tenant_id: Requested tenant.
        requested_site_id: Explicit site supplied by the request, when present.

    Returns:
        Explicit authorized site or the caller's single concrete site.

    Raises:
        HTTPException: If scope is invalid or a multi-site/wildcard caller omits site.
    """
    if requested_site_id is not None:
        require_scope(principal, tenant_id, requested_site_id)
        return requested_site_id

    require_scope(principal, tenant_id)
    concrete_sites = sorted(site for site in principal.site_ids if site != "*")
    if len(concrete_sites) == 1:
        return concrete_sites[0]
    raise HTTPException(
        422,
        {
            "code": "SITE_REQUIRED",
            "message": "site_id is required for multi-site or wildcard ONVIF access",
        },
    )


def _profile(probe: dict, token: str | None) -> dict | None:
    if token is None:
        return None
    return next((p for p in probe["profiles"] if p["token"] == token), None)


async def _camera_configuration_context(
    camera_id: str,
    session: AsyncSession,
    principal: Principal,
) -> tuple[CameraEntity, CameraCapabilityEntity, str | None, str | None]:
    """Load authorized camera/capability state plus decrypted device credentials.

    Args:
        camera_id: Managed camera identifier.
        session: Database session used for lookup.
        principal: Authenticated caller whose scope is enforced.

    Returns:
        Camera entity, capability snapshot, username and password.

    Raises:
        HTTPException: If the camera or ONVIF capability snapshot is unavailable.
        RuntimeError: If stored credentials cannot be decrypted.
    """
    camera = await authorized_camera(session, camera_id, principal)
    capability = (
        await session.execute(
            select(CameraCapabilityEntity).where(
                CameraCapabilityEntity.camera_id == camera_id
            )
        )
    ).scalar_one_or_none()
    if capability is None:
        raise HTTPException(
            404,
            {
                "code": "ONVIF_CAPABILITY_NOT_FOUND",
                "message": "ONVIF capability snapshot not found",
            },
        )
    return (
        camera,
        capability,
        decrypt_secret(camera.username_enc),
        decrypt_secret(camera.password_enc),
    )


def _managed_profile(
    capability: CameraCapabilityEntity,
    role: str,
) -> dict:
    """Resolve one managed main/sub/third profile from stored capability data.

    Args:
        capability: Stored ONVIF capability snapshot.
        role: Managed stream role.

    Returns:
        Matching stored profile dictionary.

    Raises:
        HTTPException: If role is invalid or no profile is selected.
        OnvifError: If selected token is stale/missing from the snapshot.
    """
    token_by_role = {
        "main": capability.main_profile_token,
        "sub": capability.sub_profile_token,
        "third": capability.third_profile_token,
    }
    if role not in token_by_role:
        raise HTTPException(
            422,
            {"code": "INVALID_STREAM_ROLE", "message": "Role must be main, sub or third"},
        )
    return profile_by_token(capability.profiles_json or [], token_by_role[role])


def _capability_read(capability: CameraCapabilityEntity) -> CameraCapabilityRead:
    """Serialize one stored ONVIF capability snapshot."""
    return CameraCapabilityRead(
        camera_id=capability.camera_id,
        onvif_xaddr=capability.onvif_xaddr,
        device_info=capability.device_info_json,
        services=capability.services_json,
        features=capability.features_json,
        profiles=capability.profiles_json,
        main_profile_token=capability.main_profile_token,
        sub_profile_token=capability.sub_profile_token,
        third_profile_token=capability.third_profile_token,
        probed_at=capability.probed_at,
    )


async def _apply_managed_profile(
    role: str,
    profile_token: str,
    camera: CameraEntity,
    capability: CameraCapabilityEntity,
    username: str | None,
    password: str | None,
    session: AsyncSession,
) -> CameraCapabilityRead:
    """Apply one existing ONVIF profile as main/sub/third VMS stream role."""
    if role not in {"main", "sub", "third"}:
        raise HTTPException(
            422,
            {"code": "INVALID_STREAM_ROLE", "message": "Role must be main, sub or third"},
        )
    other_tokens = {
        value
        for key, value in {
            "main": capability.main_profile_token,
            "sub": capability.sub_profile_token,
            "third": capability.third_profile_token,
        }.items()
        if key != role and value
    }
    if profile_token in other_tokens:
        raise HTTPException(
            422,
            {
                "code": "PROFILE_ROLE_CONFLICT",
                "message": "Managed stream roles must use distinct ONVIF profiles",
            },
        )

    snapshot, policy = await prepare_source_mutation(session, camera)
    try:
        probe = await probe_xaddr(
            capability.onvif_xaddr,
            username,
            password,
            tenant_id=camera.tenant_id,
            site_id=camera.site_id,
        )
        profile = _profile(probe, profile_token)
        if not profile or not profile.get("_raw_stream_uri"):
            raise HTTPException(
                422,
                {
                    "code": "NO_STREAM_URI",
                    "message": "Selected profile has no RTSP stream URI",
                },
            )
        host, port, path = stream_parts(profile["_raw_stream_uri"])
        if (host, port, urlsplit(profile["_raw_stream_uri"]).scheme) != (camera.host, camera.rtsp_port, camera.source_protocol or "rtsp"):
            raise HTTPException(
                422,
                {
                    "code": "PROFILE_TARGET_MISMATCH",
                    "message": "Selected profile must use the same camera RTSP endpoint",
                },
            )

        if role == "main":
            camera.main_path = path
            capability.main_profile_token = profile_token
        elif role == "sub":
            camera.sub_path = path
            capability.sub_profile_token = profile_token
        else:
            camera.third_path = path
            if not camera.third_stream_key:
                camera.third_stream_key = make_role_stream_key(camera.stream_key, "third")
            capability.third_profile_token = profile_token

        public = public_probe(probe)
        capability.device_info_json = public["device_info"]
        capability.services_json = public["services"]
        capability.features_json = public["features"]
        capability.profiles_json = public["profiles"]
        capability.probed_at = datetime.now(timezone.utc)

        await commit_source_mutation(session, camera, policy, snapshot)
        await session.refresh(capability)
        return _capability_read(capability)
    except HTTPException:
        await session.rollback()
        raise
    except Exception as exc:
        await session.rollback()
        raise _http_error(exc) from exc




@router.post("/discover", response_model=list[OnvifDiscoveredDevice])
async def discover_devices(
    payload: OnvifDiscoverRequest,
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Discover ONVIF devices through bounded WS-Discovery.

    Args:
        payload: Discovery timeout request.
        principal: Authorized administrator or operator.

    Returns:
        Vendor-neutral list of discovered ONVIF devices.

    Raises:
        HTTPException: HTTP 503 when the discovery socket/network is unavailable.
    """
    site_id = _resolved_site_id(principal, payload.tenant_id, payload.site_id)
    try:
        require_site_network_policy(payload.tenant_id, site_id)
        require_local_discovery_site(payload.tenant_id, site_id)
        discovered = await asyncio.to_thread(discover, payload.timeout_seconds)
        bounded = []
        for device in discovered:
            allowed_xaddrs = []
            for xaddr in device.get("xaddrs", []):
                try:
                    pinned = pin_site_http_xaddr(
                        xaddr,
                        payload.tenant_id,
                        site_id,
                    )
                    allowed_xaddrs.append(sanitize_http_uri(pinned))
                except (TargetNotAllowed, OSError, ValueError):
                    continue
            if allowed_xaddrs:
                bounded.append({**device, "xaddrs": allowed_xaddrs})
        return bounded
    except TargetNotAllowed as exc:
        raise _http_error(exc) from exc
    except OSError as exc:
        raise HTTPException(
            503,
            {"code": "DISCOVERY_UNAVAILABLE", "message": "WS-Discovery socket is unavailable on this host/network"},
        ) from exc


@router.post("/probe", response_model=OnvifProbeRead)
async def probe_device(
    payload: OnvifProbeRequest,
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Probe one ONVIF device and return public capability/profile metadata.

    Args:
        payload: Validated ONVIF connection parameters.
        principal: Authorized administrator or operator.

    Returns:
        Sanitized ONVIF probe result.

    Raises:
        HTTPException: Mapped network-policy, ONVIF or upstream probe failure.
    """
    site_id = _resolved_site_id(principal, payload.tenant_id, payload.site_id)
    try:
        result = await probe_host(
            payload.host,
            payload.port,
            payload.username,
            payload.password,
            payload.device_service_path,
            payload.scheme,
            tenant_id=payload.tenant_id,
            site_id=site_id,
        )
        return public_probe(result)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post(
    "/onboard/serial",
    response_model=CameraRead,
    status_code=status.HTTP_201_CREATED,
)
async def onboard_by_serial(
    payload: OnvifSerialOnboardRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Discover and onboard one site-local ONVIF device by exact serial number.

    Args:
        payload: Serial search scope, credentials and desired profile selections.
        session: Database session reused by the normal onboarding route.
        principal: Authorized administrator or operator.

    Returns:
        Created camera after an exact serial match and normal secure onboarding.

    Raises:
        HTTPException: If local discovery is unavailable, no exact serial match
            exists, multiple devices claim the same serial, or onboarding fails.
    """
    require_scope(principal, payload.tenant_id, payload.site_id)
    try:
        require_site_network_policy(payload.tenant_id, payload.site_id)
        require_local_discovery_site(payload.tenant_id, payload.site_id)
        discovered = await asyncio.to_thread(discover, payload.timeout_seconds)
        candidates: list[str] = []
        for device in discovered:
            for xaddr in device.get("xaddrs", []):
                try:
                    pinned = pin_site_http_xaddr(
                        xaddr,
                        payload.tenant_id,
                        payload.site_id,
                    )
                except (TargetNotAllowed, OSError, ValueError):
                    continue
                if pinned not in candidates:
                    candidates.append(pinned)
                if len(candidates) >= 32:
                    break
            if len(candidates) >= 32:
                break

        semaphore = asyncio.Semaphore(4)

        async def identify_candidate(xaddr: str) -> dict | None:
            async with semaphore:
                try:
                    return await identify_xaddr(
                        xaddr,
                        payload.username,
                        payload.password,
                        tenant_id=payload.tenant_id,
                        site_id=payload.site_id,
                        operation_timeout_seconds=payload.timeout_seconds,
                    )
                except (OnvifError, TargetNotAllowed, OSError, ValueError, TimeoutError):
                    return None

        search_timeout = max(5.0, min(20.0, payload.timeout_seconds * 4.0))
        try:
            identified = await asyncio.wait_for(
                asyncio.gather(
                    *(identify_candidate(xaddr) for xaddr in candidates)
                ),
                timeout=search_timeout,
            )
        except TimeoutError as exc:
            raise HTTPException(
                504,
                {
                    "code": "SERIAL_SEARCH_TIMEOUT",
                    "message": "Site-local ONVIF serial search exceeded its bounded deadline",
                },
            ) from exc

        wanted_serial = payload.serial_number.strip()
        matches = [
            result["xaddr"]
            for result in identified
            if result is not None
            and str(
                (result.get("device_info") or {}).get("SerialNumber") or ""
            ).strip()
            == wanted_serial
        ]

        if not matches:
            raise HTTPException(
                404,
                {
                    "code": "SERIAL_NOT_FOUND",
                    "message": "No site-local ONVIF device matched the requested serial number",
                },
            )
        if len(matches) > 1:
            raise HTTPException(
                409,
                {
                    "code": "SERIAL_NOT_UNIQUE",
                    "message": "Multiple site-local ONVIF devices reported the requested serial number",
                },
            )

        connection = connection_from_xaddr(matches[0])
        onboard = OnvifOnboardRequest(
            tenant_id=payload.tenant_id,
            site_id=payload.site_id,
            name=payload.name,
            username=payload.username,
            password=payload.password,
            main_profile_token=payload.main_profile_token,
            sub_profile_token=payload.sub_profile_token,
            third_profile_token=payload.third_profile_token,
            expected_serial_number=wanted_serial,
            **connection,
        )
        return await onboard_device(onboard, session, principal)
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(
            503,
            {
                "code": "DISCOVERY_UNAVAILABLE",
                "message": "WS-Discovery socket is unavailable on this host/network",
            },
        ) from exc
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post(
    "/onboard/qr",
    response_model=CameraRead,
    status_code=status.HTTP_201_CREATED,
)
async def onboard_by_qr(
    payload: OnvifQrOnboardRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Decode a credential-safe VMS QR payload and reuse normal ONVIF onboarding.

    Args:
        payload: Decoded QR text plus credentials supplied outside the QR code.
        session: Database session reused by the normal onboarding route.
        principal: Authorized administrator or operator.

    Returns:
        Created camera from the QR-described ONVIF endpoint.

    Raises:
        HTTPException: If QR format/model validation, scope or onboarding fails.
    """
    try:
        decoded = decode_qr_payload(payload.qr_payload)
        onboard = OnvifOnboardRequest(
            **decoded,
            username=payload.username,
            password=payload.password,
        )
    except (OnvifError, ValidationError) as exc:
        if isinstance(exc, OnvifError):
            raise _http_error(exc) from exc
        raise HTTPException(
            422,
            {
                "code": "INVALID_QR_PAYLOAD",
                "message": "QR payload does not contain valid onboarding fields",
            },
        ) from exc
    return await onboard_device(onboard, session, principal)


@router.post("/onboard", response_model=CameraRead, status_code=status.HTTP_201_CREATED)
async def onboard_device(
    payload: OnvifOnboardRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Probe and persist an ONVIF camera with selected media profiles.

    Args:
        payload: Validated ONVIF connection, scope and profile selection.
        session: Database session used to create camera/capability rows.
        principal: Authorized administrator or operator.

    Returns:
        Public camera representation after onboarding.

    Raises:
        HTTPException: If scope, duplicate detection, profile selection, probe or
            persistence/provisioning fails.
    """
    require_scope(principal, payload.tenant_id, payload.site_id)
    try:
        probe = await probe_host(
            payload.host,
            payload.port,
            payload.username,
            payload.password,
            payload.device_service_path,
            payload.scheme,
            tenant_id=payload.tenant_id,
            site_id=payload.site_id,
        )
        if payload.expected_serial_number is not None:
            observed_serial = str(
                (probe.get("device_info") or {}).get("SerialNumber") or ""
            ).strip()
            if observed_serial != payload.expected_serial_number.strip():
                raise HTTPException(
                    409,
                    {
                        "code": "SERIAL_CHANGED",
                        "message": "Camera serial no longer matches the requested device",
                    },
                )

        duplicate = (
            await session.execute(
                select(CameraEntity)
                .join(CameraCapabilityEntity, CameraCapabilityEntity.camera_id == CameraEntity.id)
                .where(
                    CameraEntity.tenant_id == payload.tenant_id,
                    CameraEntity.site_id == payload.site_id,
                    CameraCapabilityEntity.onvif_xaddr == probe["xaddr"],
                )
            )
        ).scalar_one_or_none()
        if duplicate:
            raise HTTPException(
                409,
                {"code": "DUPLICATE_CAMERA", "message": "This ONVIF device is already onboarded for the site"},
            )

        main_token = payload.main_profile_token or probe["recommended_main_profile_token"]
        sub_token = (
            payload.sub_profile_token
            if payload.sub_profile_token is not None
            else probe["recommended_sub_profile_token"]
        )
        third_token = payload.third_profile_token
        main = _profile(probe, main_token)
        sub = _profile(probe, sub_token)
        third = _profile(probe, third_token)
        if not main or not main.get("_raw_stream_uri"):
            raise HTTPException(422, {"code": "NO_STREAM_URI", "message": "Selected main profile has no RTSP URI"})
        if payload.sub_profile_token is not None and (not sub or not sub.get("_raw_stream_uri")):
            raise HTTPException(422, {"code": "NO_STREAM_URI", "message": "Selected sub profile has no RTSP URI"})
        if third_token is not None:
            if third_token in {main_token, sub_token}:
                raise HTTPException(
                    422,
                    {
                        "code": "PROFILE_ROLE_CONFLICT",
                        "message": "Third profile must be distinct from main and sub profiles",
                    },
                )
            if not third or not third.get("_raw_stream_uri"):
                raise HTTPException(
                    422,
                    {
                        "code": "NO_STREAM_URI",
                        "message": "Selected third profile has no RTSP URI",
                    },
                )

        main_host, main_port, main_path = stream_parts(main["_raw_stream_uri"])
        source_protocol = urlsplit(main["_raw_stream_uri"]).scheme
        sub_path = None
        third_path = None
        view_profile = sub if sub and sub.get("_raw_stream_uri") else main
        if sub and sub.get("_raw_stream_uri"):
            sub_host, sub_port, sub_path = stream_parts(sub["_raw_stream_uri"])
            if (sub_host, sub_port, urlsplit(sub["_raw_stream_uri"]).scheme) != (main_host, main_port, source_protocol):
                sub_path = None
                view_profile = main
        if third and third.get("_raw_stream_uri"):
            third_host, third_port, third_path = stream_parts(third["_raw_stream_uri"])
            if (third_host, third_port, urlsplit(third["_raw_stream_uri"]).scheme) != (main_host, main_port, source_protocol):
                raise HTTPException(
                    422,
                    {
                        "code": "THIRD_STREAM_TARGET_MISMATCH",
                        "message": "Third profile must use the same camera RTSP endpoint",
                    },
                )

        stream_key = make_stream_key(payload.site_id, payload.name)
        entity = CameraEntity(
            tenant_id=payload.tenant_id,
            site_id=payload.site_id,
            name=payload.name,
            host=main_host,
            rtsp_port=main_port,
            source_protocol=source_protocol,
            main_path=main_path,
            sub_path=sub_path,
            third_path=third_path,
            third_stream_key=(
                make_role_stream_key(stream_key, "third") if third_path else None
            ),
            username_enc=encrypt_secret(payload.username),
            password_enc=encrypt_secret(payload.password),
            stream_key=stream_key,
            desired_state="pending-placement" if settings.placement_execution_enabled else "provisioned",
        )
        session.add(entity)
        await session.flush()

        public = public_probe(probe)
        capability = CameraCapabilityEntity(
            camera_id=entity.id,
            onvif_xaddr=probe["xaddr"],
            device_info_json=public["device_info"],
            services_json=public["services"],
            features_json=public["features"],
            profiles_json=public["profiles"],
            main_profile_token=main_token,
            sub_profile_token=sub_token if sub_path else None,
            third_profile_token=third_token if third_path else None,
            probed_at=datetime.now(timezone.utc),
        )
        session.add(capability)

        provisioned_keys: list[str] = []
        try:
            if not settings.placement_execution_enabled:
                source = inject_rtsp_credentials(
                    view_profile["_raw_stream_uri"], payload.username, payload.password
                )
                provisioned_keys.append(entity.stream_key)
                await mediamtx.add_or_replace_path(entity.stream_key, source, **source_trust_options(entity))
                if entity.sub_path:
                    main_key = main_live_stream_key(entity)
                    provisioned_keys.append(main_key)
                    await mediamtx.add_or_replace_path(main_key, main_live_source(entity), **source_trust_options(entity))
                if third and entity.third_stream_key:
                    third_source = inject_rtsp_credentials(
                        third["_raw_stream_uri"],
                        payload.username,
                        payload.password,
                    )
                    provisioned_keys.append(entity.third_stream_key)
                    await mediamtx.add_or_replace_path(
                        entity.third_stream_key,
                        third_source,
                        **source_trust_options(entity),
                    )
            await session.commit()
        except Exception:
            await _cleanup_provisioned_paths(provisioned_keys)
            raise
        await session.refresh(entity)
        return to_read(entity)
    except HTTPException:
        await session.rollback()
        raise
    except Exception as exc:
        await session.rollback()
        raise _http_error(exc) from exc


@router.put(
    "/cameras/{camera_id}/profiles/{role}",
    response_model=CameraCapabilityRead,
)
async def select_managed_profile(
    camera_id: str,
    role: str,
    payload: OnvifManagedProfileUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Assign an existing ONVIF profile to main/sub/third and re-provision media."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    return await _apply_managed_profile(
        role,
        payload.profile_token,
        camera,
        capability,
        username,
        password,
        session,
    )


@router.delete(
    "/cameras/{camera_id}/profiles/third",
    response_model=CameraCapabilityRead,
)
async def clear_third_profile(
    camera_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Disable the managed third stream and remove its active media path.

    Args:
        camera_id: Managed camera whose third role is being cleared.
        session: Database session used for authorization and persistence.
        principal: Authorized administrator or operator.

    Returns:
        Updated ONVIF capability snapshot with no selected third profile.

    Raises:
        HTTPException: If authorization or safe media-source refresh fails.
    """
    camera, capability, _username, _password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    snapshot, policy = await prepare_source_mutation(session, camera)
    camera.third_path = None
    capability.third_profile_token = None
    try:
        await commit_source_mutation(session, camera, policy, snapshot)
    except Exception as exc:
        await session.rollback()
        raise _http_error(exc) from exc
    await session.refresh(capability)
    return _capability_read(capability)


@router.put(
    "/cameras/{camera_id}/profiles/{role}/codec",
    response_model=CameraCapabilityRead,
)
async def select_profile_by_codec(
    camera_id: str,
    role: str,
    payload: OnvifCodecProfileSelect,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Select an existing H264/H265/JPEG(MJPEG) profile for a managed stream role."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    if role not in {"main", "sub", "third"}:
        raise HTTPException(
            422,
            {"code": "INVALID_STREAM_ROLE", "message": "Role must be main, sub or third"},
        )
    try:
        probe = await probe_xaddr(
            capability.onvif_xaddr,
            username,
            password,
            tenant_id=camera.tenant_id,
            site_id=camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc

    wanted = "JPEG" if payload.encoding == "MJPEG" else payload.encoding
    candidates = [
        profile
        for profile in probe.get("profiles", [])
        if str(profile.get("encoding") or "").upper() == wanted
        and profile.get("_raw_stream_uri")
    ]
    used = {
        token
        for token in {
            capability.main_profile_token,
            capability.sub_profile_token,
            capability.third_profile_token,
        }
        if token
    }
    current = {
        "main": capability.main_profile_token,
        "sub": capability.sub_profile_token,
        "third": capability.third_profile_token,
    }[role]
    used.discard(current)
    candidates = [item for item in candidates if item.get("token") not in used]

    if payload.preferred_profile_token:
        selected = next(
            (
                item
                for item in candidates
                if item.get("token") == payload.preferred_profile_token
            ),
            None,
        )
        if selected is None:
            raise HTTPException(
                422,
                {
                    "code": "CODEC_PROFILE_NOT_AVAILABLE",
                    "message": "Preferred profile does not provide the requested codec for this role",
                },
            )
    else:
        if not candidates:
            raise HTTPException(
                422,
                {
                    "code": "CODEC_PROFILE_NOT_AVAILABLE",
                    "message": "Camera exposes no unused profile for the requested codec",
                },
            )

        def score(item: dict) -> tuple[int, int, float]:
            return (
                int(item.get("width") or 0) * int(item.get("height") or 0),
                int(item.get("bitrate_kbps") or 0),
                float(item.get("fps") or 0),
            )

        ordered = sorted(candidates, key=score)
        selected = ordered[-1] if role == "main" else ordered[0]

    return await _apply_managed_profile(
        role,
        selected["token"],
        camera,
        capability,
        username,
        password,
        session,
    )


@router.get(
    "/cameras/{camera_id}/encoder/{role}",
    response_model=OnvifEncoderRead,
)
async def read_encoder_configuration(
    camera_id: str,
    role: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Read ONVIF encoder state/options for a managed stream role.

    Args:
        camera_id: Managed camera identifier.
        role: main, sub or third.
        session: Database session used for authorization/capability lookup.
        principal: Authorized caller.

    Returns:
        Current encoder state plus device-advertised options.

    Raises:
        HTTPException: If authorization, capability metadata or ONVIF read fails.
    """
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        state = await get_encoder(
            capability.services_json or [],
            profile,
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
        return OnvifEncoderRead(
            role=role,
            profile_token=profile["token"],
            configuration_token=profile["video_encoder_configuration_token"],
            current=state["current"],
            options=state["options"],
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.put(
    "/cameras/{camera_id}/encoder/{role}",
    response_model=OnvifEncoderRead,
)
async def write_encoder_configuration(
    camera_id: str,
    role: str,
    payload: OnvifEncoderUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Validate, write and read back encoder settings for one managed role.

    Args:
        camera_id: Managed camera identifier.
        role: main, sub or third.
        payload: Validated partial encoder changes.
        session: Database session used for authorization/capability lookup.
        principal: Authorized administrator or operator.

    Returns:
        Encoder readback after successful device write.

    Raises:
        HTTPException: If scope, advertised options or ONVIF write/readback fails.
    """
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        state = await set_encoder(
            capability.services_json or [],
            profile,
            payload.model_dump(exclude_none=True),
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
        return OnvifEncoderRead(
            role=role,
            profile_token=profile["token"],
            configuration_token=profile["video_encoder_configuration_token"],
            current=state["current"],
            options=state["options"],
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get(
    "/cameras/{camera_id}/imaging/{role}",
    response_model=OnvifImagingRead,
)
async def read_imaging_configuration(
    camera_id: str,
    role: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Read ONVIF imaging state/options for a managed profile source.

    Args:
        camera_id: Managed camera identifier.
        role: main, sub or third profile role.
        session: Database session used for authorization/capability lookup.
        principal: Authorized caller.

    Returns:
        Current imaging state and advertised options.

    Raises:
        HTTPException: If capability metadata or ONVIF read fails.
    """
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        source_token = profile.get("video_source_token")
        state = await get_imaging(
            capability.services_json or [],
            source_token,
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
        return OnvifImagingRead(
            video_source_token=source_token,
            current=state["current"],
            options=state["options"],
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.put(
    "/cameras/{camera_id}/imaging/{role}",
    response_model=OnvifImagingRead,
)
async def write_imaging_configuration(
    camera_id: str,
    role: str,
    payload: OnvifImagingUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Validate, write and read back standard ONVIF imaging settings.

    Args:
        camera_id: Managed camera identifier.
        role: main, sub or third profile role.
        payload: Validated imaging patch.
        session: Database session used for authorization/capability lookup.
        principal: Authorized administrator or operator.

    Returns:
        Imaging readback after successful device write.

    Raises:
        HTTPException: If the camera does not advertise requested options/write.
    """
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        source_token = profile.get("video_source_token")
        state = await set_imaging(
            capability.services_json or [],
            source_token,
            payload.model_dump(exclude_none=True),
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
        return OnvifImagingRead(
            video_source_token=source_token,
            current=state["current"],
            options=state["options"],
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/cameras/{camera_id}/orientation/{role}")
async def read_orientation_configuration(
    camera_id: str,
    role: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Read standard ONVIF rotation/mirror state and options."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        state = await get_orientation(
            capability.services_json or [],
            profile,
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
        return {"current": state["current"], "options": state["options"]}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.put("/cameras/{camera_id}/orientation/{role}")
async def write_orientation_configuration(
    camera_id: str,
    role: str,
    payload: OnvifRotationUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Validate, write and read back standard ONVIF rotation/mirror settings."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        state = await set_orientation(
            capability.services_json or [],
            profile,
            payload.model_dump(exclude_none=True),
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
        return {"current": state["current"], "options": state["options"]}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/cameras/{camera_id}/video-source-modes/{role}")
async def read_video_source_modes(
    camera_id: str,
    role: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """List advertised ONVIF video-source modes for a managed profile source."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        return await get_video_source_modes(
            capability.services_json or [],
            profile.get("video_source_token"),
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.put("/cameras/{camera_id}/video-source-modes/{role}")
async def write_video_source_mode(
    camera_id: str,
    role: str,
    payload: OnvifVideoSourceModeUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Set one advertised ONVIF video-source mode and return readback."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        return await set_video_source_mode(
            capability.services_json or [],
            profile.get("video_source_token"),
            payload.mode_token,
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/cameras/{camera_id}/video-standard/{role}")
async def read_video_standard(
    camera_id: str,
    role: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Return explicit PAL/NTSC mappings advertised in source-mode descriptions."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        return await get_video_standards(
            capability.services_json or [],
            profile.get("video_source_token"),
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.put("/cameras/{camera_id}/video-standard/{role}")
async def write_video_standard(
    camera_id: str,
    role: str,
    payload: OnvifVideoStandardUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Set PAL/NTSC only when the camera explicitly identifies a matching mode."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        return await set_video_standard(
            capability.services_json or [],
            profile.get("video_source_token"),
            payload.standard,
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/cameras/{camera_id}/date-time", response_model=OnvifDateTimeRead)
async def read_camera_date_time(
    camera_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Read camera date/time through ONVIF Device Management."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        return OnvifDateTimeRead(
            **await get_date_time(
                capability.onvif_xaddr,
                username,
                password,
                camera.tenant_id,
                camera.site_id,
            )
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.put("/cameras/{camera_id}/date-time", response_model=OnvifDateTimeRead)
async def write_camera_date_time(
    camera_id: str,
    payload: OnvifDateTimeUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Write and read back camera date/time through ONVIF Device Management."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        return OnvifDateTimeRead(
            **await set_date_time(
                capability.onvif_xaddr,
                payload.model_dump(),
                username,
                password,
                camera.tenant_id,
                camera.site_id,
            )
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/cameras/{camera_id}/ir")
async def read_ir_capability(
    camera_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Return advertised standard ONVIF auxiliary IR-lamp commands."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        commands = await supported_auxiliary_commands(
            capability.onvif_xaddr,
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
        return {
            "supported_modes": [
                command.rsplit("|", 1)[-1]
                for command in commands
                if command.startswith("tt:IRLamp|")
            ]
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.put("/cameras/{camera_id}/ir")
async def write_ir_control(
    camera_id: str,
    payload: OnvifIrUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Send an advertised standard ONVIF IR-lamp auxiliary command."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        return await set_ir_lamp(
            capability.onvif_xaddr,
            payload.mode,
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/cameras/{camera_id}/osds")
async def read_osds(
    camera_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """List bounded ONVIF OSD configurations for an authorized camera."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        return await list_osds(
            capability.services_json or [],
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/cameras/{camera_id}/osds")
async def create_camera_osd(
    camera_id: str,
    payload: OnvifOsdCreate,
    role: str = "main",
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Create an ONVIF text/date/time OSD for one managed profile source."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        token = profile.get("video_source_configuration_token")
        if not token:
            raise OnvifError(
                "CAPABILITY_REFRESH_REQUIRED",
                "Video source configuration token is missing; refresh capabilities",
                409,
            )
        return await create_osd(
            capability.services_json or [],
            token,
            payload.model_dump(),
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.put("/cameras/{camera_id}/osds/camera-name")
async def write_camera_name_osd(
    camera_id: str,
    payload: OnvifCameraNameOsdUpdate,
    role: str = "main",
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Create or update an OSD whose text is the managed camera name."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        if payload.osd_token:
            return await update_osd(
                capability.services_json or [],
                payload.osd_token,
                {
                    "text": camera.name,
                    "x": payload.x,
                    "y": payload.y,
                },
                username,
                password,
                camera.tenant_id,
                camera.site_id,
            )
        profile = _managed_profile(capability, role)
        token = profile.get("video_source_configuration_token")
        if not token:
            raise OnvifError(
                "CAPABILITY_REFRESH_REQUIRED",
                "Video source configuration token is missing; refresh capabilities",
                409,
            )
        return await create_osd(
            capability.services_json or [],
            token,
            {
                "text": camera.name,
                "position_type": payload.position_type,
                "x": payload.x if payload.x is not None else 0.0,
                "y": payload.y if payload.y is not None else 0.0,
                "osd_type": "Plain",
            },
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.patch("/cameras/{camera_id}/osds/{osd_token}")
async def update_camera_osd(
    camera_id: str,
    osd_token: str,
    payload: OnvifOsdUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Modify an existing ONVIF text OSD and return list readback."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        return await update_osd(
            capability.services_json or [],
            osd_token,
            payload.model_dump(exclude_none=True),
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.delete("/cameras/{camera_id}/osds/{osd_token}")
async def delete_camera_osd(
    camera_id: str,
    osd_token: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Delete an ONVIF OSD and return list readback."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        return await delete_osd(
            capability.services_json or [],
            osd_token,
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/cameras/{camera_id}/privacy-masks/{role}")
async def read_privacy_masks(
    camera_id: str,
    role: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """List Media2 privacy masks for a managed profile source."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        token = profile.get("video_source_configuration_token")
        if not token:
            raise OnvifError(
                "CAPABILITY_REFRESH_REQUIRED",
                "Video source configuration token is missing; refresh capabilities",
                409,
            )
        return await list_masks(
            capability.services_json or [],
            token,
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/cameras/{camera_id}/privacy-masks/{role}")
async def create_privacy_mask(
    camera_id: str,
    role: str,
    payload: OnvifPrivacyMaskCreate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Create a capability-validated Media2 privacy mask."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        token = profile.get("video_source_configuration_token")
        if not token:
            raise OnvifError(
                "CAPABILITY_REFRESH_REQUIRED",
                "Video source configuration token is missing; refresh capabilities",
                409,
            )
        return await create_mask(
            capability.services_json or [],
            token,
            payload.model_dump(),
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.patch("/cameras/{camera_id}/privacy-masks/{role}/{mask_token}")
async def update_privacy_mask(
    camera_id: str,
    role: str,
    mask_token: str,
    payload: OnvifPrivacyMaskUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Modify a capability-validated Media2 privacy mask and return readback."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        token = profile.get("video_source_configuration_token")
        if not token:
            raise OnvifError(
                "CAPABILITY_REFRESH_REQUIRED",
                "Video source configuration token is missing; refresh capabilities",
                409,
            )
        return await update_mask(
            capability.services_json or [],
            mask_token,
            token,
            payload.model_dump(exclude_none=True),
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.delete("/cameras/{camera_id}/privacy-masks/{role}/{mask_token}")
async def delete_privacy_mask(
    camera_id: str,
    role: str,
    mask_token: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Delete a Media2 privacy mask and return source-mask readback."""
    camera, capability, username, password = await _camera_configuration_context(
        camera_id,
        session,
        principal,
    )
    try:
        profile = _managed_profile(capability, role)
        token = profile.get("video_source_configuration_token")
        if not token:
            raise OnvifError(
                "CAPABILITY_REFRESH_REQUIRED",
                "Video source configuration token is missing; refresh capabilities",
                409,
            )
        return await delete_mask(
            capability.services_json or [],
            mask_token,
            token,
            username,
            password,
            camera.tenant_id,
            camera.site_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/cameras/{camera_id}/capabilities", response_model=CameraCapabilityRead)
async def camera_capabilities(
    camera_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Return the stored ONVIF capability snapshot for an authorized camera.

    Args:
        camera_id: Camera identifier whose capabilities are requested.
        session: Database session used for authorization and capability lookup.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        CameraCapabilityRead for the camera.

    Raises:
        HTTPException: HTTP 404 when camera/capability data is unavailable or
            outside the caller scope.
    """
    await authorized_camera(session, camera_id, principal)
    row = (
        await session.execute(
            select(CameraCapabilityEntity).where(CameraCapabilityEntity.camera_id == camera_id)
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "ONVIF capability snapshot not found")
    return CameraCapabilityRead(
        camera_id=row.camera_id,
        onvif_xaddr=row.onvif_xaddr,
        device_info=row.device_info_json,
        services=row.services_json,
        features=row.features_json,
        profiles=row.profiles_json,
        main_profile_token=row.main_profile_token,
        sub_profile_token=row.sub_profile_token,
        third_profile_token=row.third_profile_token,
        probed_at=row.probed_at,
    )


@router.post("/cameras/{camera_id}/refresh", response_model=CameraCapabilityRead)
async def refresh_capabilities(
    camera_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Re-probe and persist ONVIF capabilities for an authorized camera.

    Args:
        camera_id: Camera identifier to refresh.
        session: Database session used for camera/capability persistence.
        principal: Authorized administrator or operator.

    Returns:
        Updated CameraCapabilityRead snapshot.

    Raises:
        HTTPException: If authorization, stored capability lookup, credential
            decryption or ONVIF probing fails.
    """
    camera = await authorized_camera(session, camera_id, principal)
    row = (
        await session.execute(
            select(CameraCapabilityEntity).where(CameraCapabilityEntity.camera_id == camera_id)
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "ONVIF capability snapshot not found")

    try:
        probe = await probe_xaddr(
            row.onvif_xaddr,
            decrypt_secret(camera.username_enc),
            decrypt_secret(camera.password_enc),
            tenant_id=camera.tenant_id,
            site_id=camera.site_id,
        )
        public = public_probe(probe)
        row.device_info_json = public["device_info"]
        row.services_json = public["services"]
        row.features_json = public["features"]
        row.profiles_json = public["profiles"]
        third_missing = bool(row.third_profile_token) and not any(
            profile.get("token") == row.third_profile_token
            for profile in row.profiles_json
        )
        if third_missing:
            snapshot, policy = await prepare_source_mutation(session, camera)
            camera.third_path = None
            row.third_profile_token = None
        row.probed_at = datetime.now(timezone.utc)
        if third_missing:
            await commit_source_mutation(session, camera, policy, snapshot)
        else:
            await session.commit()
        await session.refresh(row)
        return CameraCapabilityRead(
            camera_id=row.camera_id,
            onvif_xaddr=row.onvif_xaddr,
            device_info=row.device_info_json,
            services=row.services_json,
            features=row.features_json,
            profiles=row.profiles_json,
            main_profile_token=row.main_profile_token,
            sub_profile_token=row.sub_profile_token,
            third_profile_token=row.third_profile_token,
            probed_at=row.probed_at,
        )
    except Exception as exc:
        await session.rollback()
        raise _http_error(exc) from exc
