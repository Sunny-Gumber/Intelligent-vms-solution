/**
 * Playback with a timeline that shows gaps honestly.
 *
 * A VMS timeline that draws a solid bar when footage is missing is actively
 * dangerous, so buckets below full coverage are rendered as gaps.
 */
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../lib/api";
import type { Camera, PlaybackManifest, TimelineBucket } from "../lib/types";

export function PlaybackPage() {
  const { cameraId: routeCameraId } = useParams();
  const [selectedCameraId, setSelectedCameraId] = useState("");
  const cameraId = routeCameraId ?? selectedCameraId;
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.get<{ items: Camera[] }>("/cameras?limit=500")
      .then((page) => setCameras(page.items)).catch((error) => setError(String(error)));
  }, []);
  const [manifest, setManifest] = useState<PlaybackManifest | null>(null);
  const [buckets, setBuckets] = useState<TimelineBucket[]>([]);
  const [range] = useState(() => {
    const end = new Date();
    const start = new Date(end.getTime() - 3600_000);
    return { start, end };
  });

  useEffect(() => {
    if (!cameraId) return;
    let cancelled = false;
    setManifest(null);
    setBuckets([]);
    setError(null);
    const query = new URLSearchParams({ start: range.start.toISOString(), end: range.end.toISOString() });
    (async () => {
      const [m, t] = await Promise.all([
        api.get<PlaybackManifest>(`/playback/${encodeURIComponent(cameraId)}/manifest?${query}`),
        api.get<{ buckets: TimelineBucket[] }>(`/playback/${encodeURIComponent(cameraId)}/timeline?${query}`),
      ]);
      if (cancelled) return;
      setManifest(m);
      setBuckets(t.buckets);
    })().catch((error) => { if (!cancelled) setError(String(error)); });
    return () => { cancelled = true; };
  }, [cameraId, range]);

  return (
    <section className="page">
      <h1>Playback</h1>
      {!routeCameraId && <select aria-label="Playback camera" value={selectedCameraId} onChange={(event) => setSelectedCameraId(event.target.value)}>
        <option value="">Select a camera</option>
        {cameras.map((camera) => <option key={camera.id} value={camera.id}>{camera.name}</option>)}
      </select>}
      {error && <p role="alert">{error}</p>}

      {/* The server states plainly when there is nothing to show. */}
      {manifest?.message && <p className="notice">{manifest.message}</p>}

      <div className="timeline" role="img" aria-label="Recording availability">
        {buckets.map((bucket, index) => (
          <span
            key={index}
            className={bucket.has_gap ? "timeline__gap" : "timeline__full"}
            title={`${new Date(bucket.start).toLocaleTimeString()} — ${
              bucket.has_gap ? "gap or partial coverage" : "recorded"
            }`}
          />
        ))}
      </div>

      {manifest && manifest.gaps.length > 0 && (
        <details className="gaps">
          <summary>{manifest.gaps.length} gap(s) in this period</summary>
          <ul>
            {manifest.gaps.map((gap, index) => (
              <li key={index}>
                {new Date(gap.start).toLocaleString()} → {new Date(gap.end).toLocaleString()}
                {" "}({Math.round(gap.duration_s)}s) — {gap.cause}
              </li>
            ))}
          </ul>
        </details>
      )}

      <ol className="segments">
        {manifest?.segments.map((segment) => (
          <li key={segment.id}>
            {new Date(segment.started_at).toLocaleTimeString()} ·{" "}
            {segment.duration_s ? `${Math.round(segment.duration_s)}s` : "open"} ·{" "}
            {segment.has_gap_before && <em>gap before </em>}
            {(segment.size_bytes ?? 0) / 1e6 > 0 &&
              `${((segment.size_bytes ?? 0) / 1e6).toFixed(1)} MB`}
          </li>
        ))}
      </ol>
    </section>
  );
}
