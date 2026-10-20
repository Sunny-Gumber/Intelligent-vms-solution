"""ANPR / LPR support.

SCOPE (explicit): this release ingests, normalizes, stores and searches plate
reads produced by ANPR-capable cameras or external engines. It does NOT
perform plate detection or OCR on the server — no OCR model is bundled and
none is downloaded. AnalyticsSource always records where a read came from.

Implemented here: plate normalization, watchlist matching (with OCR-confusion
tolerance), and hit search.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from vms_contracts.enums import AnalyticsClass

from app.core.timeutil import ensure_utc
from app.db.models import AnalyticsEvent, SystemSetting

PLATE_CLEAN = re.compile(r"[^A-Z0-9]")
WATCHLIST_KEY = "anpr.watchlist"

# Characters OCR engines confuse most often. Used for fuzzy MATCHING only;
# the stored read is never altered.
CONFUSIONS = {"0": "O", "O": "0", "1": "I", "I": "1", "5": "S", "S": "5",
              "8": "B", "B": "8", "2": "Z", "Z": "2"}


def normalize_plate(text: str | None) -> str | None:
    if not text:
        return None
    return PLATE_CLEAN.sub("", text.upper()) or None


def fuzzy_variants(plate: str) -> set[str]:
    """Single-character confusion variants for watchlist matching."""
    variants = {plate}
    for index, char in enumerate(plate):
        swap = CONFUSIONS.get(char)
        if swap:
            variants.add(plate[:index] + swap + plate[index + 1:])
    return variants


@dataclass(frozen=True)
class WatchlistEntry:
    plate: str
    label: str
    severity: str = "high"
    note: str | None = None


def load_watchlist(session: Session) -> list[WatchlistEntry]:
    setting = session.get(SystemSetting, WATCHLIST_KEY)
    if setting is None:
        return []
    return [WatchlistEntry(plate=normalize_plate(i.get("plate")) or "",
                           label=i.get("label", "watchlist"),
                           severity=i.get("severity", "high"), note=i.get("note"))
            for i in (setting.value or {}).get("entries", []) if i.get("plate")]


def save_watchlist(session: Session, entries: list[dict], actor_id: str) -> None:
    normalized = [{"plate": normalize_plate(e.get("plate")),
                   "label": e.get("label", "watchlist"),
                   "severity": e.get("severity", "high"), "note": e.get("note")}
                  for e in entries if normalize_plate(e.get("plate"))]
    setting = session.get(SystemSetting, WATCHLIST_KEY)
    if setting is None:
        setting = SystemSetting(key=WATCHLIST_KEY, value={"entries": normalized})
        session.add(setting)
    else:
        setting.value = {"entries": normalized}
    setting.updated_by = actor_id


def match_watchlist(plate: str | None, watchlist: list[WatchlistEntry], *,
                    fuzzy: bool = True) -> WatchlistEntry | None:
    normalized = normalize_plate(plate)
    if not normalized:
        return None
    for entry in watchlist:
        if entry.plate == normalized:
            return entry
    if fuzzy:
        variants = fuzzy_variants(normalized)
        for entry in watchlist:
            if entry.plate in variants:
                return entry
    return None


def search_plate(session: Session, plate: str, *, camera_ids: list[str] | None = None,
                 since: dt.datetime | None = None, until: dt.datetime | None = None,
                 limit: int = 200) -> list[AnalyticsEvent]:
    normalized = normalize_plate(plate) or ""
    stmt = select(AnalyticsEvent).where(
        AnalyticsEvent.object_class == AnalyticsClass.LICENSE_PLATE.value,
        AnalyticsEvent.plate_text.like(f"%{normalized}%"))
    if camera_ids is not None:
        stmt = stmt.where(AnalyticsEvent.camera_id.in_(camera_ids))
    if since:
        stmt = stmt.where(AnalyticsEvent.occurred_at >= ensure_utc(since))
    if until:
        stmt = stmt.where(AnalyticsEvent.occurred_at <= ensure_utc(until))
    return list(session.execute(
        stmt.order_by(AnalyticsEvent.occurred_at.desc()).limit(limit)).scalars().all())
