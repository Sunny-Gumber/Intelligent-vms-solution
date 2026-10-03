import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from prometheus_client.parser import text_string_to_metric_families

from app.core.config import settings


class DiagnosticError(RuntimeError):
    """Raised when media diagnostics cannot obtain a valid telemetry sample."""


@dataclass
class ByteSample:
    """Store one monotonic byte-counter sample for throughput calculation.

    Attributes:
        at: Monotonic sample time in seconds.
        inbound: Cumulative inbound bytes, when reported.
        outbound: Cumulative outbound bytes, when reported.
    """

    at: float
    inbound: int | None
    outbound: int | None


class MediaDiagnostics:
    """Collect bounded MediaMTX path metrics and derive short-term throughput."""

    def __init__(self):
        self._previous: dict[str, ByteSample] = {}
        self._lock = asyncio.Lock()

    async def _metrics_text(self) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=settings.diagnostic_metrics_timeout_seconds
            ) as client:
                response = await client.get(settings.mediamtx_metrics_url)
                response.raise_for_status()
                return response.text
        except httpx.HTTPError as exc:
            raise DiagnosticError("Media metrics endpoint unavailable") from exc

    @staticmethod
    def _collect(text: str, path: str) -> dict:
        out = {
            "path_state": None,
            "inbound_bytes": None,
            "outbound_bytes": None,
            "rtp_packets": None,
            "rtp_packets_lost": None,
            "rtp_packets_in_error": None,
            "rtp_jitter": None,
        }
        rtp_packets = lost = in_error = 0
        jitter_values: list[float] = []

        for family in text_string_to_metric_families(text):
            for sample in family.samples:
                labels = sample.labels or {}
                sample_path = labels.get("name") or labels.get("path")
                if sample_path != path:
                    continue

                if sample.name == "paths":
                    out["path_state"] = labels.get("state")
                elif sample.name == "paths_inbound_bytes":
                    out["inbound_bytes"] = int(sample.value)
                elif sample.name == "paths_outbound_bytes":
                    out["outbound_bytes"] = int(sample.value)
                elif sample.name == "rtsp_sessions_inbound_rtp_packets":
                    rtp_packets += int(sample.value)
                elif sample.name == "rtsp_sessions_inbound_rtp_packets_lost":
                    lost += int(sample.value)
                elif sample.name == "rtsp_sessions_inbound_rtp_packets_in_error":
                    in_error += int(sample.value)
                elif sample.name == "rtsp_sessions_inbound_rtp_packets_jitter":
                    jitter_values.append(float(sample.value))

        out["rtp_packets"] = rtp_packets or None
        out["rtp_packets_lost"] = lost or None
        out["rtp_packets_in_error"] = in_error or None
        out["rtp_jitter"] = max(jitter_values) if jitter_values else None
        return out

    async def sample(self, path: str) -> dict:
        """Collect one diagnostic snapshot for a MediaMTX path.

        Args:
            path: MediaMTX path/stream key to inspect.

        Returns:
            Parsed path/RTP counters plus derived inbound/outbound Mbps and UTC
            sample time.

        Raises:
            DiagnosticError: If the MediaMTX metrics endpoint is unavailable.
            Exception: Metrics parsing failures propagate to the caller.
        """
        text = await self._metrics_text()
        values = self._collect(text, path)
        now_mono = time.monotonic()
        inbound_mbps = outbound_mbps = None

        async with self._lock:
            previous = self._previous.get(path)
            current = ByteSample(
                at=now_mono,
                inbound=values["inbound_bytes"],
                outbound=values["outbound_bytes"],
            )
            self._previous[path] = current

        if previous:
            elapsed = now_mono - previous.at
            if elapsed >= 0.5:
                if current.inbound is not None and previous.inbound is not None:
                    delta = max(0, current.inbound - previous.inbound)
                    inbound_mbps = delta * 8 / elapsed / 1_000_000
                if current.outbound is not None and previous.outbound is not None:
                    delta = max(0, current.outbound - previous.outbound)
                    outbound_mbps = delta * 8 / elapsed / 1_000_000

        values["inbound_mbps"] = inbound_mbps
        values["outbound_mbps"] = outbound_mbps
        values["sampled_at"] = datetime.now(timezone.utc)
        return values


media_diagnostics = MediaDiagnostics()
