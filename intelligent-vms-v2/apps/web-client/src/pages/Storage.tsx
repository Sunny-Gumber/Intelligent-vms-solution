/**
 * Storage volume administration.
 *
 * RAID status is shown only for software RAID. Hardware controllers report
 * "unknown" and the UI says so rather than implying health it cannot verify.
 */
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Volume } from "../lib/types";

function bytes(value?: number | null) {
  if (!value) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let index = 0;
  let scaled = value;
  while (scaled >= 1000 && index < units.length - 1) { scaled /= 1000; index++; }
  return `${scaled.toFixed(1)} ${units[index]}`;
}

export function StoragePage() {
  const [volumes, setVolumes] = useState<Volume[]>([]);
  const [raidNote, setRaidNote] = useState<string>("");

  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.get<{ items: Volume[]; raid_note: string }>("/storage/volumes").then((response) => {
      setVolumes(response.items);
      setRaidNote(response.raid_note);
    }).catch((error) => setError(String(error)));
  }, []);

  return (
    <section className="page">
      <h1>Storage</h1>
      {error && <p role="alert">{error}</p>}
      <table className="data-table">
        <thead>
          <tr>
            <th>Name</th><th>Path</th><th>State</th>
            <th>Free</th><th>Total</th><th>Cameras</th><th>RAID</th>
          </tr>
        </thead>
        <tbody>
          {volumes.map((volume) => {
            const freePercent = volume.total_bytes
              ? (volume.free_bytes ?? 0) / volume.total_bytes * 100
              : null;
            return (
              <tr key={volume.id} className={volume.state !== "online" ? "row--warn" : ""}>
                <td>{volume.name}</td>
                <td><code>{volume.path}</code></td>
                <td>{volume.state}</td>
                <td>
                  {bytes(volume.free_bytes)}
                  {freePercent !== null && ` (${freePercent.toFixed(1)}%)`}
                </td>
                <td>{bytes(volume.total_bytes)}</td>
                <td>{volume.camera_count}</td>
                <td>{volume.raid_status ?? "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="muted">{raidNote}</p>
    </section>
  );
}
