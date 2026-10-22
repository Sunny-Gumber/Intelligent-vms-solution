"""Configuration.

Precedence (highest first):
  1. Environment variables prefixed with VMS_
  2. The TOML config file at $VMS_CONFIG_FILE, or the platform default
  3. Built-in defaults

Security rules enforced here:
  * No credential, key or token has a usable default.
  * The master key is NEVER read from a literal in source.
  * Production mode refuses to start with a development secret, with SQLite,
    with a wildcard host list, or with a designed-but-unimplemented provider.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# List settings arrive from environment variables as comma-separated text
# (VMS_TRUSTED_HOSTS=a.example,b.example). pydantic-settings would otherwise
# JSON-decode them before validation and refuse to start on plain CSV.
CsvList = Annotated[list[str], NoDecode]
CsvIntList = Annotated[list[int], NoDecode]


def _default_data_root() -> Path:
    if sys.platform.startswith("win"):
        return Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "IntelligentVMS"
    return Path("/var/lib/intelligent-vms")


def _default_config_file() -> Path:
    if sys.platform.startswith("win"):
        return _default_data_root() / "config" / "vms.toml"
    return Path("/etc/intelligent-vms/vms.toml")


def _file_defaults() -> dict[str, Any]:
    path = Path(os.getenv("VMS_CONFIG_FILE", str(_default_config_file())))
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    flat: dict[str, Any] = {}
    for section, body in raw.items():
        if isinstance(body, dict):
            for key, value in body.items():
                flat[key if section == "vms" else f"{section}_{key}"] = value
        else:
            flat[section] = body
    return flat


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VMS_",
        env_file=os.getenv("VMS_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------- runtime ----------------
    environment: Literal["development", "staging", "production"] = "development"
    data_root: Path = Field(default_factory=_default_data_root)
    log_level: str = "INFO"
    log_json: bool = True
    log_file: Path | None = None
    log_max_bytes: int = 64 * 1024 * 1024
    log_backup_count: int = 10

    api_host: str = "0.0.0.0"  # noqa: S104 - appliance binds all interfaces by design
    api_port: int = 8080
    api_root_path: str = ""
    cors_origins: CsvList = Field(default_factory=list)
    trusted_hosts: CsvList = Field(default_factory=lambda: ["*"])

    # ---------------- database ----------------
    # SQLite default so a developer checkout runs immediately; PostgreSQL is the
    # only supported production backend (ADR-0004).
    database_url: str = "sqlite+pysqlite:///./var/vms.db"
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_pool_recycle_s: int = 1800
    database_connect_retries: int = 10
    database_connect_backoff_s: float = 1.5

    # ---------------- security ----------------
    secret_key: str = "dev-insecure-change-me"  # noqa: S105 - development-only sentinel rejected in production
    access_token_ttl_s: int = 900
    refresh_token_ttl_s: int = 7 * 24 * 3600
    play_token_ttl_s: int = 60
    download_token_ttl_s: int = 300
    password_min_length: int = 12
    # Argon2id cost. Defaults follow OWASP guidance (64 MiB, t=3), targeting
    # roughly 100 ms on a modern server core. They are configurable because the
    # right cost depends on the hardware, and because CI needs a cheaper
    # setting to run hundreds of authentications quickly. Production startup
    # REFUSES anything below the OWASP minimum (see _production_safety).
    password_hash_time_cost: int = 3
    password_hash_memory_kib: int = 65536
    password_hash_parallelism: int = 2
    lockout_threshold: int = 5
    lockout_window_s: int = 900
    lockout_duration_s: int = 900
    rate_limit_login_per_minute: int = 10
    rate_limit_api_per_minute: int = 600
    require_mfa_for_admins: bool = False
    bootstrap_admin_username: str = "admin"
    # If unset, a random password is generated at first boot and written to
    # <data_root>/first-run-credentials.txt with 0600 permissions.
    bootstrap_admin_password: str | None = None

    # ---------------- network guard (SSRF) ----------------
    allow_private_camera_networks: bool = True
    camera_allow_cidrs: CsvList = Field(default_factory=list)
    camera_deny_cidrs: CsvList = Field(
        default_factory=lambda: [
            "127.0.0.0/8", "::1/128",
            "169.254.0.0/16", "fe80::/10",   # link-local / cloud metadata
            "0.0.0.0/8",
        ]
    )
    camera_allowed_ports: CsvIntList = Field(
        default_factory=lambda: [80, 443, 554, 322, 8000, 8080, 8443, 8554, 2020, 37777]
    )
    discovery_max_hosts: int = 1024
    discovery_timeout_s: float = 5.0
    device_monitor_interval_s: int = 60
    device_monitor_concurrency: int = 8

    # ---------------- media ----------------
    media_gateway_url: str = "http://127.0.0.1:9997"     # MediaMTX control API
    media_public_webrtc_url: str = "http://127.0.0.1:8889"
    media_public_hls_url: str = "http://127.0.0.1:8888"
    media_rtsp_port: int = 8554
    media_gateway_user: str | None = None
    media_gateway_password: str | None = None
    max_live_sessions: int = 256
    max_live_sessions_per_user: int = 32
    live_session_ttl_s: int = 300
    # ---------------- media gateway admission (ADR-0015) ----------------
    # MediaMTX calls POST /internal/media/auth for every read and every
    # gateway-API request. The caller must present HTTP Basic credentials
    # (media_auth_hook_user / media_auth_hook_secret) AND connect from an
    # address inside media_auth_allowed_callers. There is no permissive
    # default: if either is missing the hook denies everything.
    media_auth_hook_user: str = "mediamtx"
    media_auth_hook_secret: str | None = None
    media_auth_allowed_callers: CsvList = Field(default_factory=list)
    # Bounded per-session gateway connection bindings; the oldest binding is
    # superseded (and kicked) when a session reconnects beyond this number.
    media_connection_max_per_session: int = 4
    media_revocation_interval_s: float = 5.0
    media_revocation_batch: int = 200
    media_revocation_kick_concurrency: int = 16
    media_revocation_kick_timeout_s: float = 5.5
    media_kick_max_attempts: int = 5
    media_connection_retention_s: int = 24 * 3600
    # Peers whose X-Forwarded-For header uvicorn will honour. Defaults to
    # loopback only; "*" lets any client spoof its source address.
    forwarded_allow_ips: str = "127.0.0.1"

    # ---------------- recording ----------------
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    segment_seconds: int = 300
    recording_root: Path | None = None          # defaults to <data_root>/recordings
    recorder_max_restart_backoff_s: float = 60.0
    recorder_start_timeout_s: float = 20.0
    recorder_stall_timeout_s: float = 45.0
    post_event_seconds: int = 30
    pre_event_seconds: int = 0                  # see docs/LIMITATIONS.md
    gap_tolerance_s: float = 3.0
    retention_default_days: int = 14
    retention_scan_interval_s: int = 300
    retention_min_free_percent: float = 10.0

    # ---------------- exports ----------------
    export_root: Path | None = None             # defaults to <data_root>/exports
    max_concurrent_exports: int = 2
    export_ttl_hours: int = 72
    export_max_duration_s: int = 4 * 3600
    export_disk_headroom_factor: float = 1.25

    # ---------------- PTZ ----------------
    ptz_max_move_seconds: float = 5.0
    ptz_lock_ttl_s: float = 10.0
    ptz_deadman_interval_s: float = 2.0

    # ---------------- events ----------------
    event_dedupe_window_s: int = 10
    event_queue_max: int = 10000
    alarm_auto_close_hours: int = 72
    escalation_interval_s: int = 300
    webhook_timeout_s: float = 10.0
    webhook_max_retries: int = 5
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True

    # ---------------- analytics ----------------
    analytics_enabled: bool = True
    analytics_queue_max: int = 5000
    analytics_model_dir: Path | None = None
    analytics_providers: CsvList = Field(default_factory=lambda: ["CPUExecutionProvider"])
    analytics_max_fps_per_camera: float = 2.0
    face_recognition_enabled: bool = False
    anpr_enabled: bool = False
    privacy_retention_days: int = 30

    # ---------------- storage ----------------
    storage_check_interval_s: int = 60
    storage_warn_free_percent: float = 15.0
    storage_critical_free_percent: float = 5.0

    # ---------------- licensing ----------------
    license_file: Path | None = None
    license_public_key_file: Path | None = None
    unlicensed_camera_limit: int = 8

    # ---------------- federation ----------------
    federation_enabled: bool = False
    federation_node_name: str = "primary"
    federation_poll_interval_s: int = 30

    # ---------------- identity ----------------
    oidc_enabled: bool = False
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_redirect_uri: str | None = None
    ldap_enabled: bool = False   # designed only; production start refuses (ADR-0011)

    # ---------------- validators ----------------
    @field_validator("cors_origins", "trusted_hosts", "camera_allow_cidrs",
                     "camera_deny_cidrs", "analytics_providers",
                     "media_auth_allowed_callers", mode="before")
    @classmethod
    def _split_csv(cls, v: Any) -> Any:
        if isinstance(v, str):
            text = v.strip()
            if text.startswith("["):
                return json.loads(text)
            return [p.strip() for p in text.split(",") if p.strip()]
        return v

    @field_validator("camera_allowed_ports", mode="before")
    @classmethod
    def _split_ports(cls, v: Any) -> Any:
        if isinstance(v, str):
            text = v.strip()
            if text.startswith("["):
                return json.loads(text)
            return [int(p) for p in text.split(",") if p.strip()]
        return v

    @model_validator(mode="after")
    def _production_safety(self) -> Settings:
        if self.environment == "production":
            problems = []
            if self.secret_key == "dev-insecure-change-me" or len(self.secret_key) < 32:  # noqa: S105 - reject the insecure development sentinel in production
                problems.append("VMS_SECRET_KEY must be unique and at least 32 characters")
            if self.database_url.startswith("sqlite"):
                problems.append("SQLite is not supported in production; configure PostgreSQL")
            if "*" in self.trusted_hosts:
                problems.append("VMS_TRUSTED_HOSTS must be an explicit list in production")
            if self.ldap_enabled:
                problems.append("LDAP is designed but NOT implemented in this release")
            if self.password_hash_memory_kib < 19456:
                problems.append("VMS_PASSWORD_HASH_MEMORY_KIB is below the OWASP "
                                "minimum of 19456 KiB")
            if (self.password_hash_memory_kib < 19456
                    or self.password_hash_time_cost < 2):
                problems.append(
                    "Argon2 cost parameters are below the OWASP minimum "
                    "(memory >= 19456 KiB, time_cost >= 2)")
            if problems:
                raise ValueError("Refusing to start in production mode: " + "; ".join(problems))
        return self

    # ---------------- media admission validation ----------------
    def media_auth_problems(self) -> list[str]:
        """Return reasons the media admission hook cannot be trusted.

        An empty list means the hook may admit. Any problem makes the hook deny
        every request (fail closed); production startup refuses to continue.
        Kept out of the model validator so non-media tooling (migrations, CI
        contract generation) can still construct Settings.
        """
        import ipaddress

        problems: list[str] = []
        secret = self.media_auth_hook_secret or ""
        if len(secret) < 32:
            problems.append("VMS_MEDIA_AUTH_HOOK_SECRET must be set (>= 32 characters)")
        if not self.media_auth_hook_user:
            problems.append("VMS_MEDIA_AUTH_HOOK_USER must not be empty")
        if not self.media_auth_allowed_callers:
            problems.append("VMS_MEDIA_AUTH_ALLOWED_CALLERS must list the gateway address(es)")
        for entry in self.media_auth_allowed_callers:
            try:
                network = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                problems.append(f"VMS_MEDIA_AUTH_ALLOWED_CALLERS entry {entry!r} is not a CIDR")
                continue
            if network.prefixlen == 0:
                problems.append("VMS_MEDIA_AUTH_ALLOWED_CALLERS must not contain a /0 network")
        if not self.media_gateway_user or len(self.media_gateway_password or "") < 16:
            problems.append("VMS_MEDIA_GATEWAY_USER and VMS_MEDIA_GATEWAY_PASSWORD "
                            "(>= 16 characters) are required for gateway administration")
        if self.media_connection_max_per_session < 1:
            problems.append("VMS_MEDIA_CONNECTION_MAX_PER_SESSION must be >= 1")
        if self.media_revocation_batch < 1:
            problems.append("VMS_MEDIA_REVOCATION_BATCH must be >= 1")
        if self.media_revocation_kick_concurrency < 1:
            problems.append("VMS_MEDIA_REVOCATION_KICK_CONCURRENCY must be >= 1")
        if self.media_revocation_kick_timeout_s <= 0:
            problems.append("VMS_MEDIA_REVOCATION_KICK_TIMEOUT_S must be > 0")
        return problems

    # ---------------- derived paths ----------------
    @property
    def recordings_dir(self) -> Path:
        return self.recording_root or (self.data_root / "recordings")

    @property
    def exports_dir(self) -> Path:
        return self.export_root or (self.data_root / "exports")

    @property
    def snapshots_dir(self) -> Path:
        return self.data_root / "snapshots"

    @property
    def keys_dir(self) -> Path:
        return self.data_root / "keys"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def spool_dir(self) -> Path:
        return self.data_root / "spool"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_directories(self) -> None:
        for path in (self.data_root, self.recordings_dir, self.exports_dir,
                     self.snapshots_dir, self.keys_dir, self.logs_dir, self.spool_dir):
            path.mkdir(parents=True, exist_ok=True)
        if not sys.platform.startswith("win"):
            os.chmod(self.keys_dir, 0o700)


@lru_cache
def get_settings() -> Settings:
    return Settings(**_file_defaults())


def reset_settings_cache() -> None:
    """Used by tests and by the configuration reload endpoint."""
    get_settings.cache_clear()
