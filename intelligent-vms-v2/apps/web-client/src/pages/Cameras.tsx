/**
 * Camera inventory and onboarding.
 *
 * NOTE ON STATUS: this source has not been built or run — no npm toolchain
 * existed in the build environment. Treat it as a reviewed starting point,
 * not as verified UI behaviour.
 */
import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import type { Camera } from "../lib/types";

const STATE_STYLE: Record<string, string> = {
  online: "badge badge--ok",
  offline: "badge badge--error",
  unauthorized: "badge badge--warn",
  provisioning: "badge badge--muted",
  disabled: "badge badge--muted",
  error: "badge badge--error",
};

export function CamerasPage() {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setBusy(true);
    try {
      setCameras((await api.get<{ items: Camera[] }>(`/cameras?${new URLSearchParams({ search })}`)).items);
      setError(null);
    } catch (e: any) {
      // The API returns RFC 7807 problem documents; `code` is stable, `detail`
      // is for humans. Always show the detail, never a generic message.
      setError(e?.detail ?? "Could not load cameras");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    const handle = setTimeout(load, 250); // debounce the search box
    return () => clearTimeout(handle);
  }, [search]);

  const summary = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const camera of cameras) counts[camera.state] = (counts[camera.state] ?? 0) + 1;
    return counts;
  }, [cameras]);

  return (
    <section className="page">
      <header className="page__header">
        <h1>Cameras</h1>
        <div className="summary">
          {Object.entries(summary).map(([state, count]) => (
            <span key={state} className={STATE_STYLE[state] ?? "badge"}>
              {state}: {count}
            </span>
          ))}
        </div>
        <input
          aria-label="Search cameras"
          placeholder="Search by name or address"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </header>

      {error && <p role="alert" className="error">{error}</p>}
      {busy && <p className="muted">Loading…</p>}

      <table className="data-table">
        <thead>
          <tr>
            <th>Name</th><th>Address</th><th>Vendor / model</th>
            <th>State</th><th>Recording</th><th>PTZ</th><th>Last seen</th>
          </tr>
        </thead>
        <tbody>
          {cameras.map((camera) => (
            <tr key={camera.id}>
              <td>{camera.name}</td>
              <td><code>{camera.host}:{camera.port}</code></td>
              <td>{[camera.vendor, camera.model].filter(Boolean).join(" ") || "—"}</td>
              <td><span className={STATE_STYLE[camera.state] ?? "badge"}>{camera.state}</span></td>
              <td>{camera.recording_state}</td>
              <td>
                {/* Capability flags are only true when the device proved it. */}
                {camera.capabilities?.ptz_continuous ? "Yes" : "No"}
              </td>
              <td>{camera.last_seen_at ? new Date(camera.last_seen_at).toLocaleString() : "never"}</td>
            </tr>
          ))}
          {!busy && cameras.length === 0 && (
            <tr><td colSpan={7} className="muted">No cameras match.</td></tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
