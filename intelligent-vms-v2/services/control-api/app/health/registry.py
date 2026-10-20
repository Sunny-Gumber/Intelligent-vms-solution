"""Component health registry.

Health is computed from real signals, never hardcoded: database connectivity,
media gateway reachability, worker heartbeats, storage state, supervisor task
status.

  /healthz                  liveness  — is the process alive
  /readyz                   readiness — can it serve traffic
  /api/v1/system/health     the detailed report
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from vms_contracts.dto import ComponentHealth, HealthReport
from vms_contracts.enums import HealthStatus, ServiceState

from app.core.timeutil import utcnow
from app.version import VERSION

Probe = Callable[[], Awaitable[ComponentHealth]]


def _rank(status: HealthStatus) -> int:
    return {HealthStatus.HEALTHY: 0, HealthStatus.UNKNOWN: 1,
            HealthStatus.DEGRADED: 2, HealthStatus.UNHEALTHY: 3}[status]


@dataclass
class HealthRegistry:
    started_at: dt.datetime = field(default_factory=utcnow)
    probes: dict[str, Probe] = field(default_factory=dict)
    critical: set[str] = field(default_factory=set)
    _last: dict[str, ComponentHealth] = field(default_factory=dict)

    def register(self, name: str, probe: Probe, *, critical: bool = False) -> None:
        self.probes[name] = probe
        if critical:
            self.critical.add(name)

    async def check(self) -> HealthReport:
        components: list[ComponentHealth] = []
        for name, probe in self.probes.items():
            try:
                component = await probe()
            except Exception as exc:  # noqa: BLE001
                component = ComponentHealth(name=name, status=HealthStatus.UNHEALTHY,
                                            state=ServiceState.FAILED,
                                            detail=f"probe raised: {exc}"[:300])
            self._last[name] = component
            components.append(component)

        overall = HealthStatus.HEALTHY
        for component in components:
            if component.status is HealthStatus.UNHEALTHY:
                candidate = (HealthStatus.UNHEALTHY if component.name in self.critical
                             else HealthStatus.DEGRADED)
            elif component.status is HealthStatus.DEGRADED:
                candidate = HealthStatus.DEGRADED
            else:
                continue
            if _rank(candidate) > _rank(overall):
                overall = candidate

        return HealthReport(
            status=overall, version=VERSION,
            uptime_s=round((utcnow() - self.started_at).total_seconds(), 1),
            checked_at=utcnow(), components=components)

    def is_ready(self) -> bool:
        for name in self.critical:
            component = self._last.get(name)
            if component is None or component.status is HealthStatus.UNHEALTHY:
                return False
        return True


def healthy(name: str, detail: str | None = None, **metrics: float) -> ComponentHealth:
    return ComponentHealth(name=name, status=HealthStatus.HEALTHY,
                           state=ServiceState.RUNNING, detail=detail,
                           last_ok_at=utcnow(), metrics=metrics)


def degraded(name: str, detail: str, **metrics: float) -> ComponentHealth:
    return ComponentHealth(name=name, status=HealthStatus.DEGRADED,
                           state=ServiceState.DEGRADED, detail=detail, metrics=metrics)


def unhealthy(name: str, detail: str, **metrics: float) -> ComponentHealth:
    return ComponentHealth(name=name, status=HealthStatus.UNHEALTHY,
                           state=ServiceState.FAILED, detail=detail, metrics=metrics)
