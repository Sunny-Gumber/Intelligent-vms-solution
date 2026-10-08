from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.effective_authority import DEFAULT_FENCE_EXPIRY_GRACE_SECONDS
from app.core.placement_renewal import assert_settings_renewal_budget


class Settings(BaseSettings):
    """Load validated control-plane configuration from environment settings.\n\n    Parameters are supplied through environment variables or the local .env file.\n    Instantiation returns a validated Settings object and may raise Pydantic\n    validation errors when configured values do not match their declared types.\n    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Intelligent VMS Control API"
    database_url: str = "sqlite+aiosqlite:///./vms.db"
    vms_secret_key: str = ""
    auto_create_schema: bool = False
    deployment_profile: str = "enterprise-distributed"
    web_static_dir: str = ""

    mediamtx_api_url: str = "http://localhost:9997"
    mediamtx_webrtc_public_base: str = "http://localhost:8889"
    mediamtx_hls_public_base: str = "http://localhost:8888"
    mediamtx_playback_internal_url: str = "http://mediamtx:9996"
    mediamtx_metrics_url: str = "http://mediamtx:9998/metrics"
    diagnostic_metrics_timeout_seconds: float = 3.0
    # VMS-FIX-013. Kept separate from placement settings. MediaMTX v1.21.1 list
    # APIs default to 100 items per page at page 0. Enumeration stops here and
    # reports truncation instead of requesting pages without a limit.
    mediamtx_list_max_items: int = Field(default=10000, ge=1, le=1_000_000)
    live_view_token_private_key_b64: str = ""
    live_view_token_verification_public_keys_b64_json: str = "[]"
    live_view_token_ttl_seconds: int = Field(default=60, ge=15, le=300)
    # Safety ceiling, not a measured viewer-capacity claim. Applied only to
    # browser-facing live/third MediaMTX paths; recording paths are unchanged.
    live_view_max_readers_per_path: int = Field(default=16, ge=1, le=10000)

    kafka_bootstrap_servers: str = ""
    kafka_topic_events: str = "vms.events.v1"
    kafka_topic_recordings: str = "vms.recordings.v1"
    kafka_request_timeout_ms: int = 5000

    outbox_enabled: bool = True
    outbox_poll_interval_seconds: float = 0.5
    outbox_batch_size: int = 100
    outbox_claim_seconds: int = 30
    outbox_max_attempts: int = 12
    outbox_backoff_max_seconds: float = 300.0
    outbox_cleanup_interval_seconds: float = 60.0
    outbox_cleanup_batch_size: int = 500
    outbox_delivered_retention_hours: int = 24
    outbox_dead_retention_days: int = 30
    outbox_max_payload_bytes: int = 262144
    event_pipeline_enabled: bool = True
    event_history_enabled: bool = True
    event_local_store_enabled: bool = False
    event_local_retention_days: int = Field(default=7, ge=1, le=365)
    alarm_processing_enabled: bool = True
    ai_ui_enabled: bool = True
    recording_metadata_events_enabled: bool = True

    clickhouse_url: str = "http://clickhouse:8123"
    clickhouse_database: str = "vms"
    event_query_max_window_hours: int = 168
    event_query_max_limit: int = 500

    recording_hook_token: str = ""
    regional_spool_token: str = ""
    recording_hook_callback_url: str = "http://control-api:8000/internal/v1/recording/segments/complete"
    recording_hook_command: str = ""
    recording_path_template: str = "/recordings/%path/%Y/%m/%d/%H/%s"
    recording_query_max_window_hours: int = 168
    recording_query_max_segments: int = 10000
    # Export safety guardrails, not measured production-capacity claims.
    recording_export_max_duration_seconds: int = Field(default=900, ge=1, le=14400)
    recording_export_max_concurrent_per_process: int = Field(default=4, ge=1, le=64)
    recording_export_io_timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    observability_recording_gap_min_seconds: int = Field(default=120, ge=60, le=86400)
    observability_recording_gap_segment_multiplier: float = Field(
        default=2.0,
        ge=1.0,
        le=10.0,
    )

    onvif_allowed_cidrs: str = (
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
        "127.0.0.0/8,169.254.0.0/16,100.64.0.0/10,"
        "::1/128,fc00::/7,fe80::/10"
    )
    onvif_allow_public_hosts: bool = False
    # JSON object keyed by "tenant_id/site_id", values are CIDR lists.
    # Scoped ONVIF probe/onboard operations require an exact site entry.
    onvif_site_allowed_cidrs_json: str = "{}"
    # Comma-separated tenant/site keys whose L2 discovery network is local to
    # this API process. "*" is allowed only when deliberately configured.
    onvif_discovery_local_sites: str = ""
    onvif_connect_timeout_seconds: float = 3.0
    onvif_operation_timeout_seconds: float = 8.0
    onvif_max_response_bytes: int = 2_000_000
    onvif_discovery_max_seconds: float = 5.0

    auth_disabled: bool = False
    auth_require_oidc: bool = False
    auth_browser_session_enabled: bool = False
    auth_browser_session_cookie_secure: bool = True
    auth_browser_session_max_age_seconds: int = Field(default=28800, ge=300, le=86400)
    auth_jwks_url: str = ""
    auth_issuer: str = ""
    auth_audience: str = "intelligent-vms"
    auth_hs256_secret: str = ""
    auth_desktop_oidc_enabled: bool = False
    auth_oidc_client_id: str = ""
    auth_oidc_scopes: str = "openid profile offline_access"

    cors_allowed_origins: str = ""
    cors_allowed_methods: str = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
    cors_allowed_headers: str = "Authorization,Content-Type,Range"

    media_reconcile_enabled: bool = True
    media_reconcile_interval_seconds: float = 30.0
    media_reconcile_batch_size: int = 500
    media_reconcile_max_changes_per_run: int = 200

    health_monitor_enabled: bool = True
    health_monitor_interval_seconds: float = 5.0
    health_monitor_batch_size: int = 500
    health_probe_timeout_seconds: float = 1.5
    health_probe_concurrency: int = 100
    health_failure_threshold: int = 3
    health_recovery_threshold: int = 2
    health_heartbeat_seconds: int = 300

    placement_default_region: str = "default-region"
    placement_node_stale_seconds: float = 30.0
    placement_headroom: float = 0.75
    placement_lease_seconds: int = 60
    # Same grace the node-agent adds after lease expiry. Failover waits until
    # lease plus this grace, or a later autonomy deadline. Do not shorten the
    # lease to close that window; a healthy node would be fenced early.
    placement_fence_expiry_grace_seconds: float = Field(
        default=DEFAULT_FENCE_EXPIRY_GRACE_SECONDS,
        ge=0,
        validation_alias="FENCE_EXPIRY_GRACE_SECONDS",
    )
    # 0 disables offline authority extension. Production enables this only
    # after Step 1C-C validation.
    placement_offline_autonomy_seconds: int = 0
    placement_batch_size: int = 1000
    placement_max_moves_per_run: int = 200
    # Lease renewal budget (VMS-FIX-014). Independent of the camera-scan batch.
    # required = pages * (retry_sleep + max_run + interval)
    #   + missed_lock_cycles * (retry_sleep + interval)
    #   + max_run
    #   + fence_poll + clock_skew + safety_margin, and required < lease.
    # retry_sleep is (lock attempts - 1) * retry delay. Later cycles pay it
    # before the attempt that acquires the lock. The trailing max_run is the
    # revisit of the first page, which pages * max_run does not include.
    # pages = ceil(ceiling / batch). missed_lock_cycles is at least 1.
    # Fence poll 5s and clock skew 5s match the node-agent defaults; this block
    # does not change the node. Fence grace is not extra lease life.
    # The batch equals the ceiling so the reviewed 21000 assignments are one
    # page. Lease writes on PostgreSQL are chunked statements, and a run that
    # passes max_run rolls back instead of committing a partial page. max_run
    # is that deadline, not a measured rate and not a fleet-capacity claim.
    # Other fixes may add settings in this file; keep this block together.
    placement_interval_seconds: float = Field(default=10.0, gt=0)
    placement_renewal_batch_size: int = Field(default=21000, ge=1)
    placement_renewal_max_assignments: int = Field(default=21000, ge=1)
    # 4s, not 2s: one PostgreSQL 16 sample of a 21000-row page on this host
    # finished in 1.743s. That is inside 2s and leaves no margin, so the
    # deadline the budget charges is 4s. The sample is this host only, not a
    # capacity claim. 0 is accepted by the formula and arms no deadline.
    placement_renewal_max_run_seconds: float = Field(default=4.0, ge=0)
    placement_renewal_missed_lock_cycles: int = Field(default=1, ge=1)
    placement_renewal_fence_poll_seconds: float = Field(default=5.0, ge=1)
    placement_renewal_clock_skew_seconds: float = Field(default=5.0, ge=0)
    placement_renewal_safety_margin_seconds: float = Field(default=5.0, ge=1)
    placement_renewal_lock_retry_seconds: float = Field(default=0.25, gt=0)
    placement_renewal_lock_retry_limit: int = Field(default=4, ge=1)
    placement_execution_enabled: bool = False
    placement_local_node_id: str = "media-local-01"
    node_fence_snapshot_max_items: int = 10000

    @model_validator(mode="after")
    def reject_unsatisfiable_placement_renewal_budget(self) -> "Settings":
        """Reject a renewal cadence that is not strictly inside the lease.

        Returns:
            This settings instance when the conservative budget holds.

        Raises:
            PlacementRenewalBudgetError: When pages, the retry sleep later cycles
                may spend before they acquire, reserved missed cycles, the
                revisit's own max run, fence poll, clock skew and the safety
                margin do not fit before lease expiry. The lease is not reduced
                and fence grace is not spent to fit.
        """
        assert_settings_renewal_budget(self)
        return self


settings = Settings()
