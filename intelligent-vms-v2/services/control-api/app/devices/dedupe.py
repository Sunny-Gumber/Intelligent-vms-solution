"""Duplicate camera detection.

A duplicate is costly: two recorders pulling the same device double the
bandwidth and produce two partial archives. Detection uses, in order of
confidence: serial number, MAC address, then host+port endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Camera


@dataclass(frozen=True)
class DuplicateMatch:
    camera_id: str
    camera_name: str
    reason: str
    confidence: str   # "certain" | "likely"


def normalize_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    return "".join(c for c in mac.lower() if c.isalnum()) or None


def find_duplicate(session: Session, *, host: str | None = None, port: int | None = None,
                   serial: str | None = None, mac: str | None = None,
                   exclude_id: str | None = None) -> DuplicateMatch | None:
    clauses = []
    if serial:
        clauses.append(Camera.serial == serial)
    norm_mac = normalize_mac(mac)
    if norm_mac:
        # Rows may hold normalized or colon-separated MACs depending on when
        # they were written, so both forms are matched.
        colon = ":".join(norm_mac[i:i + 2] for i in range(0, len(norm_mac), 2))
        clauses.append(Camera.mac.in_((norm_mac, colon, colon.upper())))
    if host:
        clauses.append(Camera.host == host)
    if not clauses:
        return None

    stmt = select(Camera).where(or_(*clauses))
    if exclude_id:
        stmt = stmt.where(Camera.id != exclude_id)

    for existing in session.execute(stmt).scalars().all():
        if serial and existing.serial and existing.serial == serial:
            return DuplicateMatch(existing.id, existing.name,
                                  "serial number matches", "certain")
        if norm_mac and existing.mac and normalize_mac(existing.mac) == norm_mac:
            return DuplicateMatch(existing.id, existing.name,
                                  "MAC address matches", "certain")
        if host and existing.host == host and (port is None or existing.port == port):
            return DuplicateMatch(existing.id, existing.name,
                                  f"another camera already uses {host}:{existing.port}",
                                  "likely")
    return None
