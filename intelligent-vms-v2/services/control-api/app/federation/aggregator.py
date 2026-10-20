"""Multi-server federation (centralized monitoring).

SCOPE OF THIS RELEASE — stated precisely:

  IMPLEMENTED: registering remote Intelligent VMS nodes, authenticating to
  them with a signed node token over TLS, polling health/camera/alarm
  summaries, and presenting an aggregated view.

  NOT IMPLEMENTED: distributed recording, cross-node failover, a distributed
  control plane, or proxying live video between nodes. Those require a
  consensus and media-routing design that is written up in
  docs/architecture/multi-site.md but is NOT built. The API returns 501 for
  those operations rather than silently doing something partial.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.crypto import CredentialVault
from app.core.errors import NotImplementedFeatureError
from app.core.timeutil import utcnow
from app.db.models import FederationNode
from app.logging_setup import get_logger

log = get_logger("federation")


@dataclass
class NodeSummary:
    node_id: str
    name: str
    reachable: bool
    cameras_total: int = 0
    cameras_online: int = 0
    alarms_open: int = 0
    version: str | None = None
    error: str | None = None
    checked_at: dt.datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id, "name": self.name, "reachable": self.reachable,
            "cameras_total": self.cameras_total, "cameras_online": self.cameras_online,
            "alarms_open": self.alarms_open, "version": self.version,
            "error": self.error, "checked_at": self.checked_at.isoformat(),
        }


class FederationAggregator:
    def __init__(self, vault: CredentialVault, settings: Settings | None = None) -> None:
        self.vault = vault
        self.settings = settings or get_settings()

    async def poll_node(self, node: FederationNode) -> NodeSummary:
        token = None
        if node.token_blob:
            token = self.vault.decrypt(node.token_blob, aad=f"node:{node.id}")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        url = node.base_url.rstrip("/") + "/api/v1/system/summary"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0),
                                         verify=True) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            return NodeSummary(node.id, node.name, False, error=str(exc)[:200])

        if response.status_code != 200:
            return NodeSummary(node.id, node.name, False,
                               error=f"HTTP {response.status_code}")
        data = response.json()
        return NodeSummary(
            node_id=node.id, name=node.name, reachable=True,
            cameras_total=int(data.get("cameras_total", 0)),
            cameras_online=int(data.get("cameras_online", 0)),
            alarms_open=int(data.get("alarms_open", 0)),
            version=data.get("version"))

    async def poll_all(self, session: Session) -> list[NodeSummary]:
        nodes = session.execute(
            select(FederationNode)
            .where(FederationNode.enabled.is_(True))).scalars().all()
        if not nodes:
            return []
        summaries = await asyncio.gather(*(self.poll_node(node) for node in nodes))
        for node, summary in zip(nodes, summaries):
            if summary.reachable:
                node.last_contact_at = utcnow()
            node.state = "online" if summary.reachable else "unreachable"
            node.last_error = summary.error
            node.summary = summary.to_dict()
        return list(summaries)

    @staticmethod
    def distributed_recording(*_: Any, **__: Any) -> None:
        raise NotImplementedFeatureError(
            "Distributed recording across federated nodes is designed but not "
            "implemented in this release. See docs/architecture/multi-site.md.",
            code="distributed_recording_not_implemented")

    @staticmethod
    def cross_node_failover(*_: Any, **__: Any) -> None:
        raise NotImplementedFeatureError(
            "Cross-node recording failover (N+1) is designed but not implemented "
            "in this release. See docs/architecture/ha-design.md.",
            code="failover_not_implemented")
