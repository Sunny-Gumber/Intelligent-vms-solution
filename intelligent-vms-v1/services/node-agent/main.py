import asyncio
import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

import httpx
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

Role = Literal["media", "recording", "ai"]
ALLOWED_ROLES = {"media", "recording", "ai"}
LOG = logging.getLogger("node_agent")
MIN_BACKOFF_SLEEP_SECONDS = 0.001
# Pinned MediaMTX v1.21.1 paginate.go / openapi: page defaults to 0,
# itemsPerPage defaults to 100. The count contract lives in
# mediamtx_list_page.py, which this process loads and the node image copies.
_MEDIAMTX_LIST_DEFAULT_PAGE = 0
_MEDIAMTX_LIST_DEFAULT_ITEMS_PER_PAGE = 100


def _load_effective_authority():
    """Load the single effective-authority boundary implementation.

    Control imports app.core.effective_authority. This process loads that same
    file from the repository tree, or the copy placed beside main.py in the
    node-agent image. There is no second formula.

    Returns:
        The loaded effective-authority module.

    Raises:
        ImportError: If the shared module is not available.
    """
    import importlib.util

    candidates = (
        Path(__file__).resolve().parents[1] / "control-api" / "app" / "core" / "effective_authority.py",
        Path(__file__).resolve().with_name("effective_authority.py"),
    )
    for path in candidates:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("vms_effective_authority", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise ImportError("shared effective authority boundary module is not available")


_AUTHORITY = _load_effective_authority()


def _load_mediamtx_list_page():
    """Load the single MediaMTX list-page contract.

    Control imports app.services.mediamtx_list_page. This process loads that
    same file from the repository tree, or the copy placed beside main.py in
    the node image. There is no second count formula.

    Returns:
        The loaded list-page module.

    Raises:
        ImportError: If the shared module is not available.
    """
    import importlib.util

    candidates = (
        Path(__file__).resolve().parents[1] / "control-api" / "app" / "services" / "mediamtx_list_page.py",
        Path(__file__).resolve().with_name("mediamtx_list_page.py"),
    )
    for path in candidates:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("vms_mediamtx_list_page", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise ImportError("shared MediaMTX list-page module is not available")


_LIST_PAGE = _load_mediamtx_list_page()


def _safe_number(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number < 0:
        return 0.0
    return number


def _validate_http_url(value: str, *, field_name: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{field_name} must use http/https")
    if not parsed.netloc:
        raise ValueError(f"{field_name} must include host")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field_name} must not include query/fragment")
    if parsed.username or parsed.password:
        raise ValueError(f"{field_name} must not include credentials")
    return value.rstrip("/")


def _redact(text: str, *secrets: str) -> str:
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


def _next_backoff(backoff_seconds: float, max_seconds: float, jitter_ratio: float) -> float:
    capped = min(backoff_seconds, max_seconds)
    jitter = capped * jitter_ratio * random.uniform(-1.0, 1.0)
    return max(MIN_BACKOFF_SLEEP_SECONDS, capped + jitter)


@dataclass
class NetworkSnapshot:
    """Store one network-counter sample for throughput calculation.

    Attributes:
        rx_bytes: Cumulative received bytes.
        tx_bytes: Cumulative transmitted bytes.
        ts_monotonic: Monotonic sample timestamp.
    """

    rx_bytes: int
    tx_bytes: int
    ts_monotonic: float


class NodeAgentSettings(BaseSettings):
    """Load and validate node-agent runtime, fencing and heartbeat settings."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    node_id: str = Field(validation_alias="NODE_ID", min_length=1)
    region_id: str = Field(validation_alias="REGION_ID", min_length=1)
    node_roles: list[Role] = Field(validation_alias="NODE_ROLES", min_length=1)
    control_api_url: str = Field(validation_alias="CONTROL_API_URL", min_length=1)
    node_agent_token: SecretStr = Field(validation_alias="NODE_AGENT_TOKEN")

    node_name: str | None = Field(default=None, validation_alias="NODE_NAME")
    heartbeat_interval_seconds: float = Field(default=10.0, validation_alias="HEARTBEAT_INTERVAL_SECONDS")
    heartbeat_timeout_seconds: float = Field(default=3.0, validation_alias="HEARTBEAT_TIMEOUT_SECONDS")
    heartbeat_backoff_initial_seconds: float = Field(default=1.0, validation_alias="HEARTBEAT_BACKOFF_INITIAL_SECONDS")
    heartbeat_backoff_max_seconds: float = Field(default=60.0, validation_alias="HEARTBEAT_BACKOFF_MAX_SECONDS")
    heartbeat_backoff_jitter_ratio: float = Field(default=0.2, validation_alias="HEARTBEAT_BACKOFF_JITTER_RATIO")
    recording_mount_path: str = Field(default="/recordings", validation_alias="RECORDING_MOUNT_PATH")
    network_interface: str | None = Field(default=None, validation_alias="NETWORK_INTERFACE")
    mediamtx_api_url: str | None = Field(default=None, validation_alias="MEDIAMTX_API_URL")
    # VMS-FIX-013. Kept separate from fence settings. MediaMTX v1.21.1 lists
    # 100 paths per page. The probe stops here and does not publish a partial
    # count as placement load.
    mediamtx_list_max_items: int = Field(
        default=10000,
        ge=1,
        le=1_000_000,
        validation_alias="MEDIAMTX_LIST_MAX_ITEMS",
    )
    heartbeat_spool_url: str | None = Field(default=None, validation_alias="HEARTBEAT_SPOOL_URL")
    node_fencing_enabled: bool = Field(default=False, validation_alias="NODE_FENCING_ENABLED")
    fence_poll_interval_seconds: float = Field(default=5.0, validation_alias="FENCE_POLL_INTERVAL_SECONDS")
    fence_expiry_grace_seconds: float = Field(
        default=_AUTHORITY.DEFAULT_FENCE_EXPIRY_GRACE_SECONDS,
        validation_alias="FENCE_EXPIRY_GRACE_SECONDS",
    )
    fence_clock_skew_warn_seconds: float = Field(default=5.0, validation_alias="FENCE_CLOCK_SKEW_WARN_SECONDS")
    fence_state_path: str = Field(
        default="/var/lib/vms-node/fence-state.json",
        validation_alias="FENCE_STATE_PATH",
    )


    @field_validator("node_roles", mode="before")
    @classmethod
    def parse_roles(cls, value):
        """Normalize NODE_ROLES into a unique supported role list.

        Args:
            value: Raw environment/list role value.

        Returns:
            Normalized role strings.

        Raises:
            ValueError: If the value is not a list, contains duplicates or unsupported roles.
        """
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        if not isinstance(value, list):
            raise ValueError("NODE_ROLES must be a comma-separated list")
        normalized = [str(item).strip() for item in value if str(item).strip()]
        if len(set(normalized)) != len(normalized):
            raise ValueError("NODE_ROLES must be unique")
        invalid = sorted(set(normalized) - ALLOWED_ROLES)
        if invalid:
            raise ValueError(f"Invalid role(s): {', '.join(invalid)}")
        return normalized

    @field_validator("control_api_url")
    @classmethod
    def validate_control_api_url(cls, value: str):
        """Validate the central control-plane base URL.

        Args:
            value: Candidate CONTROL_API_URL.

        Returns:
            Validated credential-free HTTP/HTTPS base URL.

        Raises:
            ValueError: If the URL is unsafe or malformed.
        """
        return _validate_http_url(value, field_name="CONTROL_API_URL")

    @field_validator("mediamtx_api_url")
    @classmethod
    def validate_mediamtx_url(cls, value: str | None):
        """Validate the optional MediaMTX API base URL.

        Args:
            value: Candidate MEDIAMTX_API_URL.

        Returns:
            Validated base URL, or None when not configured.

        Raises:
            ValueError: If the configured URL is unsafe or malformed.
        """
        if value is None or not value.strip():
            return None
        return _validate_http_url(value, field_name="MEDIAMTX_API_URL")

    @field_validator("heartbeat_spool_url")
    @classmethod
    def validate_heartbeat_spool_url(cls, value: str | None):
        """Validate the optional regional heartbeat spool URL.

        Args:
            value: Candidate HEARTBEAT_SPOOL_URL.

        Returns:
            Validated base URL, or None when not configured.

        Raises:
            ValueError: If the configured URL is unsafe or malformed.
        """
        if value is None or not value.strip():
            return None
        return _validate_http_url(value, field_name="HEARTBEAT_SPOOL_URL")

    @model_validator(mode="after")
    def validate_role_requirements(self):
        """Validate cross-field timing, storage, role and fencing requirements.

        Returns:
            The validated settings instance.

        Raises:
            ValueError: If timing, path, role dependency or fencing settings conflict.
        """
        if self.node_name is None or not self.node_name.strip():
            self.node_name = self.node_id
        if self.heartbeat_interval_seconds < 2:
            raise ValueError("HEARTBEAT_INTERVAL_SECONDS must be >= 2")
        if self.heartbeat_timeout_seconds <= 0:
            raise ValueError("HEARTBEAT_TIMEOUT_SECONDS must be > 0")
        if self.heartbeat_backoff_initial_seconds <= 0:
            raise ValueError("HEARTBEAT_BACKOFF_INITIAL_SECONDS must be > 0")
        if self.heartbeat_backoff_max_seconds < self.heartbeat_backoff_initial_seconds:
            raise ValueError("HEARTBEAT_BACKOFF_MAX_SECONDS must be >= HEARTBEAT_BACKOFF_INITIAL_SECONDS")
        if not (0 <= self.heartbeat_backoff_jitter_ratio <= 1):
            raise ValueError("HEARTBEAT_BACKOFF_JITTER_RATIO must be between 0 and 1")

        path = Path(self.recording_mount_path)
        if not path.is_absolute():
            raise ValueError("RECORDING_MOUNT_PATH must be absolute")

        if any(role in self.node_roles for role in ("media", "recording")) and not self.mediamtx_api_url:
            raise ValueError("MEDIAMTX_API_URL is required for media/recording role")
        if self.fence_poll_interval_seconds < 1:
            raise ValueError("FENCE_POLL_INTERVAL_SECONDS must be >= 1")
        if self.fence_expiry_grace_seconds < 0:
            raise ValueError("FENCE_EXPIRY_GRACE_SECONDS must be >= 0")
        if self.fence_clock_skew_warn_seconds < 0:
            raise ValueError("FENCE_CLOCK_SKEW_WARN_SECONDS must be >= 0")
        if not Path(self.fence_state_path).is_absolute():
            raise ValueError("FENCE_STATE_PATH must be absolute")
        return self


class _MediaMTXListError(RuntimeError):
    """Raised when a MediaMTX list page is not the v1.21.1 object shape."""


def _mediamtx_list_payload(
    *,
    items: list,
    item_count: int | None,
    page_count: int | None,
    truncated: bool,
    inconsistent: bool,
) -> dict:
    return {
        "itemCount": item_count,
        "pageCount": page_count,
        "items": items,
        "truncated": truncated,
        "inconsistent": inconsistent,
    }


def _mediamtx_names_conflict(seen: set[str], names: list[str]) -> bool:
    if len(names) != len(set(names)):
        return True
    return any(name in seen for name in names)


def _count_runtime_paths(items: list) -> tuple[int, int]:
    live_sources = 0
    recording_paths = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name.endswith("-record"):
            recording_paths += 1
            continue
        if item.get("source"):
            live_sources += 1
    return live_sources, recording_paths


async def _walk_mediamtx_list_once(fetch_page, *, bound: int) -> dict:
    """Read one bounded pass of a MediaMTX list. Keep this aligned with mediamtx.py.

    Args:
        fetch_page: Coroutine ``(page, items_per_page) -> response``.
        bound: Maximum items to keep before reporting truncation.

    Returns:
        List payload. ``inconsistent`` means the pages are not one stable catalog.

    Raises:
        Exception: Transport and HTTP failures from ``fetch_page`` propagate.
        _MediaMTXListError: If a page body is not the v1.21.1 list object.
    """
    per_page = _MEDIAMTX_LIST_DEFAULT_ITEMS_PER_PAGE
    max_pages = max(1, (bound + per_page - 1) // per_page)
    collected: list = []
    seen: set[str] = set()
    baseline_items: int | None = None
    baseline_pages: int | None = None

    for page in range(_MEDIAMTX_LIST_DEFAULT_PAGE, _MEDIAMTX_LIST_DEFAULT_PAGE + max_pages):
        if baseline_items is not None and len(collected) >= bound and len(seen) < baseline_items:
            return _mediamtx_list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=True,
                inconsistent=False,
            )
        response = await fetch_page(page, per_page)
        response.raise_for_status()
        body = response.json()
        try:
            assessed = _LIST_PAGE.assess_mediamtx_list_page(body, page=page, per_page=per_page)
        except _LIST_PAGE.MediaMTXListPageError as exc:
            raise _MediaMTXListError(str(exc)) from exc
        if not assessed["accepted"]:
            if baseline_items is None:
                return _mediamtx_list_payload(
                    items=assessed["items"],
                    item_count=assessed["item_count"],
                    page_count=assessed["page_count"],
                    truncated=False,
                    inconsistent=True,
                )
            return _mediamtx_list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=False,
                inconsistent=True,
            )

        item_count = assessed["item_count"]
        page_count = assessed["page_count"]
        page_items = assessed["items"]
        names = assessed["names"]
        if baseline_items is None:
            baseline_items = item_count
            baseline_pages = page_count
        elif item_count != baseline_items or page_count != baseline_pages:
            return _mediamtx_list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=False,
                inconsistent=True,
            )

        if baseline_pages == 0:
            empty = page == 0 and baseline_items == 0 and not names
            return _mediamtx_list_payload(
                items=[],
                item_count=0,
                page_count=0,
                truncated=False,
                inconsistent=not empty,
            )
        if page >= baseline_pages:
            return _mediamtx_list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=False,
                inconsistent=True,
            )

        expected_len = len(names)
        room = bound - len(collected)
        if room < expected_len:
            if len(names) < room or _mediamtx_names_conflict(seen, names[:room]):
                return _mediamtx_list_payload(
                    items=collected,
                    item_count=baseline_items,
                    page_count=baseline_pages,
                    truncated=False,
                    inconsistent=True,
                )
            collected.extend(page_items[:room])
            return _mediamtx_list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=True,
                inconsistent=False,
            )
        if _mediamtx_names_conflict(seen, names):
            return _mediamtx_list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=False,
                inconsistent=True,
            )
        seen.update(names)
        collected.extend(page_items)
        if page + 1 >= baseline_pages:
            return _mediamtx_list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=False,
                inconsistent=len(seen) != baseline_items,
            )

    finished = baseline_items is not None and len(seen) == baseline_items
    truncated = baseline_items is not None and len(collected) >= bound and len(seen) < baseline_items
    return _mediamtx_list_payload(
        items=collected,
        item_count=baseline_items,
        page_count=baseline_pages,
        truncated=truncated,
        inconsistent=not finished and not truncated,
    )


async def _enumerate_mediamtx_list(fetch_page, *, bound: int) -> dict:
    """Walk a MediaMTX list once more if the first pass is inconsistent.

    Args:
        fetch_page: Coroutine ``(page, items_per_page) -> response``.
        bound: Maximum items kept in one pass.

    Returns:
        List payload. Transport errors propagate. A second inconsistent pass
        stays ``inconsistent`` and is not a complete catalog.

    Raises:
        Exception: Transport and HTTP failures propagate without a retry.
        _MediaMTXListError: If a page body is not the v1.21.1 list object.
    """
    first = await _walk_mediamtx_list_once(fetch_page, bound=bound)
    if not first["inconsistent"]:
        return first
    LOG.warning("mediamtx_list_inconsistent_retry")
    second = await _walk_mediamtx_list_once(fetch_page, bound=bound)
    if second["inconsistent"]:
        LOG.error(
            "mediamtx_list_inconsistent item_count=%s page_count=%s collected=%d",
            second.get("itemCount"),
            second.get("pageCount"),
            len(second.get("items") or []),
        )
    return second


class NodeAgent:
    """Collect node telemetry, send heartbeats and enforce placement fencing locally."""

    def __init__(self, settings: NodeAgentSettings):
        self.settings = settings
        self.started_monotonic = time.monotonic()
        self._last_network: NetworkSnapshot | None = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.heartbeat_timeout_seconds),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._fence_state = {
            "clock_offset_seconds": 0.0,
            "authority_mode": (
                "fenced_degraded"
                if self.settings.node_fencing_enabled
                else "central_online"
            ),
            "assignments": {},
        }
        if self.settings.node_fencing_enabled:
            self._load_fence_state()

    async def close(self):
        """Close the node agent HTTP client.

        Returns:
            None after client resources are released.
        """
        await self._client.aclose()

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.node_agent_token.get_secret_value()}",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, payload: dict | None = None) -> int:
        response = await self._client.request(
            method,
            f"{self.settings.control_api_url}{path}",
            json=payload,
            headers=self._auth_headers(),
            timeout=self.settings.heartbeat_timeout_seconds,
        )
        return response.status_code

    async def _request_json(self, method: str, path: str) -> dict:
        response = await self._client.request(
            method,
            f"{self.settings.control_api_url}{path}",
            headers=self._auth_headers(),
            timeout=self.settings.heartbeat_timeout_seconds,
        )
        if response.status_code != 200:
            raise RuntimeError(f"control API returned HTTP {response.status_code}")
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("control API returned invalid JSON object")
        return body

    def _load_fence_state(self) -> None:
        path = Path(self.settings.fence_state_path)
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(body, dict) and isinstance(body.get("assignments"), dict):
                self._fence_state = {
                    "clock_offset_seconds": float(body.get("clock_offset_seconds", 0.0) or 0.0),
                    "authority_mode": str(
                        body.get("authority_mode")
                        or (
                            "fenced_degraded"
                            if self.settings.node_fencing_enabled
                            else "central_online"
                        )
                    ),
                    "assignments": dict(body.get("assignments") or {}),
                }
        except FileNotFoundError:
            return
        except Exception as exc:
            LOG.warning("fence_state_load_failed reason=%s", exc.__class__.__name__)

    def _save_fence_state(self) -> None:
        path = Path(self.settings.fence_state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(
            self._fence_state,
            sort_keys=True,
            separators=(",", ":"),
        )
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)

        # Persist the rename itself before acknowledging a control-plane revoke.
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _fence_key(camera_id: str, role: str) -> str:
        return f"{camera_id}:{role}"

    def _effective_server_now(self) -> datetime:
        offset = float(self._fence_state.get("clock_offset_seconds", 0.0) or 0.0)
        return datetime.now(timezone.utc) + timedelta(seconds=offset)

    async def _delete_execution_key(self, role: str, execution_key: str) -> bool:
        if role == "ai":
            # There is currently no node-local AI executor in this repository.
            # Persisted generation/lease state is still exported for the future
            # AI worker; there is no process to stop in Step 1C-B.
            return True
        if role not in {"media", "recording"}:
            return False
        if not self.settings.mediamtx_api_url:
            return False
        url = (
            f"{self.settings.mediamtx_api_url}/v3/config/paths/delete/"
            f"{quote(execution_key, safe='')}"
        )
        response = await self._client.delete(
            url,
            timeout=self.settings.heartbeat_timeout_seconds,
        )
        return response.status_code in {200, 204, 404}

    async def _ack_revocation(self, revocation_id: str) -> bool:
        status = await self._request(
            "POST",
            (
                f"/api/v1/infrastructure/nodes/{self.settings.node_id}"
                f"/fences/revocations/{revocation_id}/ack"
            ),
        )
        return status in {200, 201, 204}

    async def _fetch_fence_snapshot(self) -> dict:
        return await self._request_json(
            "GET",
            f"/api/v1/infrastructure/nodes/{self.settings.node_id}/fences",
        )

    async def _apply_fence_snapshot(self, snapshot: dict) -> None:
        server_time = datetime.fromisoformat(
            str(snapshot["server_time"]).replace("Z", "+00:00")
        )
        local_now = datetime.now(timezone.utc)
        offset = (server_time - local_now).total_seconds()
        self._fence_state["clock_offset_seconds"] = offset
        if abs(offset) > self.settings.fence_clock_skew_warn_seconds:
            LOG.warning(
                "fence_clock_skew node_id=%s offset_seconds=%.3f",
                self.settings.node_id,
                offset,
            )

        assignments = self._fence_state.setdefault("assignments", {})
        for item in snapshot.get("assignments", []):
            if not isinstance(item, dict):
                continue
            key = self._fence_key(str(item["camera_id"]), str(item["role"]))
            incoming_generation = int(item["generation"])
            cached = assignments.get(key)
            cached_generation = int(cached.get("generation", -1)) if cached else -1
            if cached_generation > incoming_generation:
                # Fence generations are monotonic per camera/role. A delayed
                # snapshot must never downgrade locally persisted ownership.
                LOG.warning(
                    "fence_stale_assignment_ignored node_id=%s camera_id=%s role=%s incoming_generation=%s cached_generation=%s",
                    self.settings.node_id,
                    item.get("camera_id"),
                    item.get("role"),
                    incoming_generation,
                    cached_generation,
                )
                continue
            execution_keys = [
                str(value)
                for value in (item.get("execution_keys") or [item["execution_key"]])
                if str(value)
            ]
            if cached is not None and cached_generation == incoming_generation:
                # Source edits do not advance ownership. Delayed snapshots must
                # not forget paths already governed by this generation's lease.
                execution_keys = list(dict.fromkeys([
                    *execution_keys,
                    *(cached.get("execution_keys") or [cached["execution_key"]]),
                ]))
            assignments[key] = {
                "assignment_id": str(item["assignment_id"]),
                "camera_id": str(item["camera_id"]),
                "role": str(item["role"]),
                "generation": incoming_generation,
                "lease_expires_at": str(item["lease_expires_at"]),
                "autonomy_expires_at": (
                    str(item["autonomy_expires_at"])
                    if item.get("autonomy_expires_at") is not None
                    else None
                ),
                "execution_key": str(item["execution_key"]),
                "execution_keys": execution_keys,
                "fenced": False,
                "revoked": False,
            }

        for item in snapshot.get("revocations", []):
            if not isinstance(item, dict):
                continue
            role = str(item["role"])
            execution_key = str(item["execution_key"])
            execution_keys = [
                str(value)
                for value in (item.get("execution_keys") or [execution_key])
                if str(value)
            ]
            revoked_generation = int(item["revoked_generation"])
            key = self._fence_key(str(item["camera_id"]), role)
            cached = assignments.get(key)
            cached_generation = int(cached.get("generation", -1)) if cached else -1

            if (
                cached is not None
                and not bool(cached.get("revoked"))
                and cached_generation > revoked_generation
            ):
                # A delayed revoke for an older generation must never tear down
                # a newer failback/current generation already accepted locally.
                LOG.warning(
                    "fence_stale_revocation_ignored node_id=%s camera_id=%s role=%s revoked_generation=%s cached_generation=%s",
                    self.settings.node_id,
                    item.get("camera_id"),
                    role,
                    revoked_generation,
                    cached_generation,
                )
                continue

            removed = all(
                [
                    await self._delete_execution_key(role, key)
                    for key in execution_keys
                ]
            )
            if not removed:
                LOG.warning(
                    "fence_revoke_failed node_id=%s camera_id=%s role=%s generation=%s",
                    self.settings.node_id,
                    item.get("camera_id"),
                    role,
                    revoked_generation,
                )
                continue

            if cached_generation <= revoked_generation:
                assignments[key] = {
                    "assignment_id": str(item["assignment_id"]),
                    "camera_id": str(item["camera_id"]),
                    "role": role,
                    "generation": revoked_generation,
                    "lease_expires_at": None,
                    "autonomy_expires_at": None,
                    "execution_key": execution_key,
                    "execution_keys": execution_keys,
                    "fenced": True,
                    "revoked": True,
                }

            # Persist the stale-owner tombstone before ACKing the control plane.
            # If disk persistence fails, the revocation remains pending server-side.
            self._save_fence_state()

            if await self._ack_revocation(str(item["revocation_id"])):
                LOG.info(
                    "fence_revoked node_id=%s camera_id=%s role=%s generation=%s",
                    self.settings.node_id,
                    item.get("camera_id"),
                    role,
                    item.get("revoked_generation"),
                )
            else:
                LOG.warning(
                    "fence_revoke_ack_failed node_id=%s revocation_id=%s",
                    self.settings.node_id,
                    item.get("revocation_id"),
                )

        self._save_fence_state()

    def _autonomy_live(self, item: dict, now: datetime | None = None) -> bool:
        if bool(item.get("revoked")):
            return False
        raw = item.get("autonomy_expires_at")
        if not raw:
            return False
        try:
            expiry = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return False
        return expiry > (now or self._effective_server_now())

    def _has_live_autonomy(self) -> bool:
        now = self._effective_server_now()
        assignments = self._fence_state.setdefault("assignments", {})
        return any(
            self._autonomy_live(item, now)
            for item in assignments.values()
            if isinstance(item, dict) and not bool(item.get("fenced"))
        )

    def _set_authority_mode(self, mode: str) -> None:
        previous = str(self._fence_state.get("authority_mode") or "")
        if previous == mode:
            return
        self._fence_state["authority_mode"] = mode
        LOG.warning(
            "authority_mode_changed node_id=%s from=%s to=%s",
            self.settings.node_id,
            previous or "unknown",
            mode,
        )
        self._save_fence_state()

    async def _enforce_cached_expiry(self) -> None:
        assignments = self._fence_state.setdefault("assignments", {})
        now = self._effective_server_now()
        changed = False
        for key, item in list(assignments.items()):
            revoked = bool(item.get("revoked"))
            expired = False
            if revoked:
                # Acknowledged fencing ends authority even if the lease remains.
                expired = not _AUTHORITY.effective_authority_active(
                    None,
                    None,
                    now,
                    self.settings.fence_expiry_grace_seconds,
                    acknowledged_fenced=True,
                )
            else:
                try:
                    expiry = datetime.fromisoformat(
                        str(item["lease_expires_at"]).replace("Z", "+00:00")
                    )
                except Exception:
                    LOG.warning("fence_invalid_cached_lease key=%s", key)
                    continue
                autonomy_deadline = None
                autonomy_raw = item.get("autonomy_expires_at")
                if autonomy_raw:
                    try:
                        autonomy_deadline = datetime.fromisoformat(
                            str(autonomy_raw).replace("Z", "+00:00")
                        )
                    except Exception:
                        LOG.warning("fence_invalid_autonomy_deadline key=%s", key)
                # Same boundary control uses before it may authorize a successor.
                expired = not _AUTHORITY.effective_authority_active(
                    expiry,
                    autonomy_deadline,
                    now,
                    self.settings.fence_expiry_grace_seconds,
                )

            if not revoked and not expired:
                continue

            execution_keys = [
                str(value)
                for value in (
                    item.get("execution_keys")
                    or [item.get("execution_key", "")]
                )
                if str(value)
            ]
            removed = all(
                [
                    await self._delete_execution_key(
                        str(item.get("role", "")),
                        execution_key,
                    )
                    for execution_key in execution_keys
                ]
            )
            if removed and not bool(item.get("fenced")):
                item["fenced"] = True
                changed = True
                LOG.warning(
                    "fence_execution_stopped node_id=%s camera_id=%s role=%s generation=%s revoked=%s",
                    self.settings.node_id,
                    item.get("camera_id"),
                    item.get("role"),
                    item.get("generation"),
                    revoked,
                )
        if changed:
            self._save_fence_state()

    async def fence_once(self) -> None:
        """Run one fencing synchronization and cached-expiry enforcement cycle.

        Returns:
            None after central/cached authority state is reconciled.

        Raises:
            Exception: Unexpected snapshot, persistence or node-control failures propagate.
        """
        central_snapshot_ok = False
        try:
            snapshot = await self._fetch_fence_snapshot()
            await self._apply_fence_snapshot(snapshot)
            central_snapshot_ok = True
        except Exception as exc:
            LOG.warning(
                "fence_snapshot_failed node_id=%s reason=%s",
                self.settings.node_id,
                exc.__class__.__name__,
            )

        if central_snapshot_ok:
            self._set_authority_mode("central_online")
        elif self._has_live_autonomy():
            self._set_authority_mode("regional_autonomous")
        else:
            self._set_authority_mode("fenced_degraded")

        await self._enforce_cached_expiry()

        if (
            not central_snapshot_ok
            and self._fence_state.get("authority_mode") == "regional_autonomous"
            and not self._has_live_autonomy()
        ):
            self._set_authority_mode("fenced_degraded")

    async def _fence_loop(self) -> None:
        while True:
            await self.fence_once()
            await asyncio.sleep(self.settings.fence_poll_interval_seconds)

    def _network_counters(self) -> tuple[int, int]:
        if psutil is None:
            return 0, 0
        if self.settings.network_interface:
            counters = psutil.net_io_counters(pernic=True).get(self.settings.network_interface)
            if counters is None:
                raise RuntimeError(f"NETWORK_INTERFACE {self.settings.network_interface} not found")
            return int(counters.bytes_recv), int(counters.bytes_sent)
        counters = psutil.net_io_counters()
        return int(counters.bytes_recv), int(counters.bytes_sent)

    def _measure_host(self) -> dict[str, float]:
        if psutil is None:
            return {
                "host_cpu_utilization_pct": 0.0,
                "ram_total_bytes": 0.0,
                "ram_used_bytes": 0.0,
                "disk_total_bytes": 0.0,
                "disk_free_bytes": 0.0,
                "net_rx_bps": 0.0,
                "net_tx_bps": 0.0,
                "net_rx_bytes_total": 0.0,
                "net_tx_bytes_total": 0.0,
                "process_uptime_seconds": _safe_number(time.monotonic() - self.started_monotonic),
            }

        vm = psutil.virtual_memory()
        try:
            disk = psutil.disk_usage(self.settings.recording_mount_path)
        except OSError:
            disk = None
        cpu_pct = psutil.cpu_percent(interval=None)
        now = time.monotonic()
        rx_total, tx_total = self._network_counters()
        net_rx_bps, net_tx_bps = 0.0, 0.0
        if self._last_network is not None:
            delta_t = max(now - self._last_network.ts_monotonic, 0.0)
            if delta_t > 0:
                net_rx_bps = _safe_number((rx_total - self._last_network.rx_bytes) * 8.0 / delta_t)
                net_tx_bps = _safe_number((tx_total - self._last_network.tx_bytes) * 8.0 / delta_t)
        self._last_network = NetworkSnapshot(rx_bytes=rx_total, tx_bytes=tx_total, ts_monotonic=now)

        return {
            "host_cpu_utilization_pct": _safe_number(cpu_pct),
            "ram_total_bytes": _safe_number(vm.total),
            "ram_used_bytes": _safe_number(vm.used),
            "disk_total_bytes": _safe_number(0 if disk is None else disk.total),
            "disk_free_bytes": _safe_number(0 if disk is None else disk.free),
            "net_rx_bps": _safe_number(net_rx_bps),
            "net_tx_bps": _safe_number(net_tx_bps),
            "net_rx_bytes_total": _safe_number(rx_total),
            "net_tx_bytes_total": _safe_number(tx_total),
            "process_uptime_seconds": _safe_number(now - self.started_monotonic),
        }

    async def _probe_mediamtx(self) -> dict[str, float]:
        if not any(role in self.settings.node_roles for role in ("media", "recording")):
            return {
                "mediamtx_reachable": 0.0,
                "mediamtx_configured_paths": 0.0,
                "mediamtx_live_sources": 0.0,
                "mediamtx_recording_paths": 0.0,
            }
        assert self.settings.mediamtx_api_url
        url = f"{self.settings.mediamtx_api_url}/v3/paths/list"
        try:
            async def fetch_page(page: int, items_per_page: int):
                return await self._client.get(
                    url,
                    params={"page": page, "itemsPerPage": items_per_page},
                    timeout=self.settings.heartbeat_timeout_seconds,
                )

            listed = await _enumerate_mediamtx_list(
                fetch_page,
                bound=self.settings.mediamtx_list_max_items,
            )
        except Exception as exc:
            # Reachable zero is not a measured empty catalog. The zero counts
            # below stay on the probe result for the unreachable-probe contract;
            # the heartbeat strips them so placement never reads spare capacity.
            LOG.warning("mediamtx_probe_failed reason=%s", exc.__class__.__name__)
            return {
                "mediamtx_reachable": 0.0,
                "mediamtx_configured_paths": 0.0,
                "mediamtx_live_sources": 0.0,
                "mediamtx_recording_paths": 0.0,
                "mediamtx_list_failed": 1.0,
            }
        items = listed.get("items") or []
        item_count = listed.get("itemCount")
        page_count = listed.get("pageCount")
        if listed.get("inconsistent") is True:
            LOG.error(
                "mediamtx_list_inconsistent collected=%d item_count=%s page_count=%s",
                len(items),
                "unknown" if item_count is None else item_count,
                "unknown" if page_count is None else page_count,
            )
            return {
                "mediamtx_reachable": 1.0,
                "mediamtx_list_truncated": 0.0,
                "mediamtx_list_inconsistent": 1.0,
                "mediamtx_list_failed": 1.0,
            }
        if listed.get("truncated") is True:
            # A partial count would understate load and let placement overfill
            # the node. Omit the counts so missing load stays ineligible.
            LOG.warning(
                "mediamtx_list_truncated collected=%d item_count=%s page_count=%s bound=%d",
                len(items),
                "unknown" if item_count is None else item_count,
                "unknown" if page_count is None else page_count,
                self.settings.mediamtx_list_max_items,
            )
            return {
                "mediamtx_reachable": 1.0,
                "mediamtx_list_truncated": 1.0,
                "mediamtx_list_inconsistent": 0.0,
            }
        live_sources, recording_paths = _count_runtime_paths(items)
        return {
            "mediamtx_reachable": 1.0,
            "mediamtx_list_truncated": 0.0,
            "mediamtx_list_inconsistent": 0.0,
            "mediamtx_list_failed": 0.0,
            "mediamtx_configured_paths": float(len(items)),
            "mediamtx_live_sources": float(live_sources),
            "mediamtx_recording_paths": float(recording_paths),
        }

    async def build_heartbeat_payload(self) -> dict:
        """Collect bounded host/media load and authority state for one heartbeat.

        Returns:
            Heartbeat payload containing load, role readiness, authority mode
            and observation time.

        Raises:
            Exception: Media-node telemetry failures not handled internally propagate.
        """
        host = self._measure_host()
        mediamtx = await self._probe_mediamtx()
        load = dict(host)
        load.update(mediamtx)
        # Failure, truncation, and an inconsistent catalog are not zero load.
        # The published signal is role_readiness unknown. Omitting the counts
        # keeps a 0 from being read as spare capacity.
        list_untrusted = (
            bool(mediamtx.get("mediamtx_list_truncated"))
            or bool(mediamtx.get("mediamtx_list_inconsistent"))
            or bool(mediamtx.get("mediamtx_list_failed"))
            or not mediamtx.get("mediamtx_reachable")
        )
        if list_untrusted:
            for key in (
                "mediamtx_configured_paths",
                "mediamtx_live_sources",
                "mediamtx_recording_paths",
            ):
                load.pop(key, None)
        if "media" in self.settings.node_roles:
            load["ingress_mbps"] = _safe_number(load.get("net_rx_bps", 0.0) / 1_000_000.0)
            load["egress_mbps"] = _safe_number(load.get("net_tx_bps", 0.0) / 1_000_000.0)
            if not list_untrusted:
                load["active_sources"] = _safe_number(load.get("mediamtx_live_sources", 0.0))
        if "recording" in self.settings.node_roles and not list_untrusted:
            load["active_recordings"] = _safe_number(load.get("mediamtx_recording_paths", 0.0))
            # record_mbps is intentionally omitted until a trustworthy per-recording
            # byte-rate source is wired in. Placement treats missing configured
            # dimensions as ineligible rather than zero-load.
        # AI placement load must come from the AI runtime/scheduler; this generic
        # host agent does not invent ai_mpix_s or active_ai_jobs. A failed
        # media probe must not mark the AI role.
        readiness_state = "unknown" if list_untrusted else "ready"
        role_readiness: dict[str, str] = {}
        if "media" in self.settings.node_roles:
            role_readiness["media"] = readiness_state
        if "recording" in self.settings.node_roles:
            role_readiness["recording"] = readiness_state
        return {
            "load": load,
            "role_readiness": role_readiness,
            "authority_mode": str(
                self._fence_state.get("authority_mode") or "fenced_degraded"
            ),
            # Preserve collection time through a regional spool. The persisted
            # server offset keeps this close to central UTC during WAN loss.
            "observed_at": self._effective_server_now().isoformat(),
        }

    async def heartbeat(self) -> int:
        """Submit one node heartbeat to regional spool or the control plane.

        Returns:
            HTTP status code returned by the selected heartbeat endpoint.

        Raises:
            httpx.HTTPError: If the HTTP request fails.
            Exception: Heartbeat payload collection failures propagate.
        """
        payload = await self.build_heartbeat_payload()
        if self.settings.heartbeat_spool_url:
            response = await self._client.post(
                self.settings.heartbeat_spool_url,
                json=payload,
                headers=self._auth_headers(),
                timeout=self.settings.heartbeat_timeout_seconds,
            )
            return response.status_code
        return await self._request(
            "POST",
            f"/api/v1/infrastructure/nodes/{self.settings.node_id}/heartbeat",
            payload,
        )

    async def _heartbeat_loop(self) -> None:
        backoff = self.settings.heartbeat_backoff_initial_seconds
        interval = self.settings.heartbeat_interval_seconds
        while True:
            try:
                status = await self.heartbeat()
                if status < 400:
                    backoff = self.settings.heartbeat_backoff_initial_seconds
                    await asyncio.sleep(interval)
                    continue
                if status == 404:
                    LOG.warning(
                        "node_registration_required node_id=%s; trusted registration is admin-managed",
                        self.settings.node_id,
                    )
                elif status in {401, 403}:
                    LOG.warning(
                        "heartbeat_auth_failed node_id=%s status=%s",
                        self.settings.node_id,
                        status,
                    )
                else:
                    LOG.warning("heartbeat_failed node_id=%s status=%s", self.settings.node_id, status)
            except Exception as exc:
                message = _redact(str(exc), self.settings.node_agent_token.get_secret_value())
                LOG.warning("heartbeat_exception node_id=%s reason=%s", self.settings.node_id, message)
            sleep_seconds = _next_backoff(
                backoff_seconds=backoff,
                max_seconds=self.settings.heartbeat_backoff_max_seconds,
                jitter_ratio=self.settings.heartbeat_backoff_jitter_ratio,
            )
            backoff = min(backoff * 2, self.settings.heartbeat_backoff_max_seconds)
            await asyncio.sleep(sleep_seconds)

    async def run_forever(self) -> None:
        """Run heartbeat and optional fencing loops until cancellation.

        Returns:
            None under normal operation; the coroutine runs until cancelled.

        Raises:
            Exception: Unhandled heartbeat or fencing loop failures propagate.
        """
        if self.settings.node_fencing_enabled:
            await asyncio.gather(
                self._heartbeat_loop(),
                self._fence_loop(),
            )
        else:
            await self._heartbeat_loop()


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = NodeAgentSettings()
    agent = NodeAgent(settings)
    try:
        await agent.run_forever()
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(_main())
