#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class Scenario:
    """Describe one deterministic regional chaos qualification scenario.

    Attributes:
        number: Stable scenario number.
        name: Human-readable scenario identifier.
        invariant: Safety/correctness invariant being qualified.
        tests: Pytest node IDs that prove the invariant.
    """

    number: int
    name: str
    invariant: str
    tests: tuple[str, ...]


SCENARIOS = (
    Scenario(
        1,
        "media-node-loss",
        "A stale media owner is not renewed; failover rotates generation and records durable revocation.",
        (
            "tests/test_placement_phase7.py::test_stale_node_is_not_eligible",
            "tests/test_fencing_phase7_step1cb.py::test_assignment_change_creates_durable_revocation",
        ),
    ),
    Scenario(
        2,
        "recording-node-loss",
        "A new recording generation is re-applied and stale node/generation evidence is rejected.",
        (
            "tests/test_reconciler_distributed_phase7.py::test_recording_path_is_reapplied_when_generation_changes",
            "tests/test_fencing_phase7_step1cb.py::test_recording_fence_accepts_only_current_node_generation_and_live_lease",
        ),
    ),
    Scenario(
        3,
        "controller-interruption-restart",
        "Persisted fence/autonomy state survives process restart and expired ownership is fenced.",
        (
            "tests/test_fencing_phase7_step1cb.py::test_node_agent_reloads_cached_lease_after_restart_and_fences_offline",
            "tests/test_autonomy_phase7_step1cc.py::test_restart_restores_offline_grant_and_authority_state",
        ),
    ),
    Scenario(
        4,
        "temporary-postgres-failure",
        "A temporary DB exception does not permanently kill the durable outbox worker.",
        (
            "tests/test_phase7_chaos_qualification.py::test_temporary_postgres_failure_does_not_kill_outbox_worker",
        ),
    ),
    Scenario(
        5,
        "kafka-outage-recovery",
        "Broker outage retains/retries valid events without DLQ loss and publishes after recovery.",
        (
            "tests/test_outbox_phase7.py::test_broker_outage_never_dead_letters_valid_message",
            "tests/test_phase7_chaos_qualification.py::test_kafka_outage_then_recovery_retries_same_outbox_message",
        ),
    ),
    Scenario(
        6,
        "stale-heartbeat",
        "Delayed telemetry remains stale and cannot renew placement/offline authority.",
        (
            "tests/test_autonomy_phase7_step1cc.py::test_delayed_spooled_heartbeat_preserves_observation_freshness",
            "tests/test_placement_phase7.py::test_stale_node_is_not_eligible",
        ),
    ),
    Scenario(
        7,
        "old-node-unreachable-cleanup",
        "Unreachable stale owner remains a cleanup obligation and is removed after it returns.",
        (
            "tests/test_phase7_chaos_qualification.py::test_unreachable_old_recording_node_keeps_cleanup_obligation_for_retry",
        ),
    ),
    Scenario(
        8,
        "repeated-failover-failback",
        "A -> B -> C -> A preserves cleanup history and failback does not delete the current owner.",
        (
            "tests/test_placement_phase7.py::test_repeated_failover_keeps_all_stale_nodes",
            "tests/test_placement_phase7.py::test_failback_removes_new_current_node_from_cleanup",
            "tests/test_fencing_phase7_step1cb.py::test_failback_cancels_obsolete_revocation_for_new_owner",
        ),
    ),
    Scenario(
        9,
        "delayed-fence-snapshot-revocation",
        "Old snapshots/revocations cannot downgrade or delete a newer accepted generation.",
        (
            "tests/test_fencing_phase7_step1cb.py::test_delayed_assignment_snapshot_cannot_downgrade_local_generation",
            "tests/test_fencing_phase7_step1cb.py::test_delayed_revocation_cannot_delete_newer_failback_generation",
        ),
    ),
    Scenario(
        10,
        "wan-control-isolation",
        "A node enters bounded regional autonomy only with a live grant and fences at the hard deadline.",
        (
            "tests/test_autonomy_phase7_step1cc.py::test_control_loss_enters_regional_autonomous_only_with_live_grant",
            "tests/test_autonomy_phase7_step1cc.py::test_autonomy_hard_deadline_fences_cached_owner",
        ),
    ),
    Scenario(
        11,
        "reconnect-storm-backlog-drain",
        "Repeated WAN failures retain a bounded/idempotent backlog and drain it once connectivity returns.",
        (
            "tests/test_phase7_chaos_qualification.py::test_reconnect_storm_backlog_drains_without_duplicate_spool_items",
        ),
    ),
    Scenario(
        12,
        "regional-spool-full",
        "A full spool fails closed and dead-letter retention remains bounded.",
        (
            "tests/test_regional_spool_phase7_step1cc.py::test_spool_fails_closed_when_capacity_is_full",
            "tests/test_regional_spool_phase7_step1cc.py::test_dead_letters_are_bounded",
        ),
    ),
    Scenario(
        13,
        "fence-state-disk-failure",
        "Revocation is never ACKed when durable local tombstone persistence fails.",
        (
            "tests/test_fencing_phase7_step1cb.py::test_revocation_is_not_acked_when_tombstone_persistence_fails",
        ),
    ),
    Scenario(
        14,
        "tenant-site-service-isolation",
        "Tenant/site and node-scoped service identities cannot cross authorization boundaries.",
        (
            "tests/test_auth_phase2b.py::test_principal_tenant_site_scope",
            "tests/test_auth_phase2b.py::test_out_of_scope_is_hidden_as_not_found",
            "tests/test_auth_phase2b.py::test_node_routes_reject_service_token_for_other_node",
            "tests/test_auth_phase2b.py::test_global_service_scope_rejects_node_scoped_service_principal",
        ),
    ),
)


def run_scenario(scenario: Scenario) -> tuple[bool, str]:
    """Execute one chaos scenario's deterministic pytest evidence.

    Args:
        scenario: Scenario definition to run.

    Returns:
        Tuple of pass/fail status and bounded result detail.
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--disable-warnings",
        *scenario.tests,
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {TIMEOUT_SECONDS}s"

    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    lowered = output.lower()
    if result.returncode != 0:
        return False, output[-4000:]
    if "skipped" in lowered:
        return False, "scenario contains skipped tests:\n" + output[-4000:]
    return True, output.splitlines()[-1] if output else "pytest passed"


def main() -> int:
    """Execute the complete deterministic Phase-7 chaos gate.

    Returns:
        Zero when all scenarios pass without skips, otherwise one.
    """
    print("Phase 7 Regional Chaos Qualification")
    print("=" * 72)
    failures = []

    for scenario in SCENARIOS:
        print(f"[RUN ] {scenario.number:02d} {scenario.name}")
        print(f"       invariant: {scenario.invariant}")
        ok, detail = run_scenario(scenario)
        if ok:
            print(f"[PASS] {scenario.number:02d} {scenario.name} — {detail}")
        else:
            print(f"[FAIL] {scenario.number:02d} {scenario.name}")
            print(detail)
            failures.append(scenario.name)

    print("=" * 72)
    if failures:
        print(f"PHASE 7 CHAOS GATE: FAIL ({len(failures)} scenario(s))")
        print("failed:", ", ".join(failures))
        return 1

    print(f"PHASE 7 CHAOS GATE: PASS ({len(SCENARIOS)}/{len(SCENARIOS)})")
    print("Scope: deterministic software qualification only; real hardware/network qualification remains Phase 8.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
