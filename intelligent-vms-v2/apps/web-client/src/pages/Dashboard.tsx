/**
 * Operations dashboard.
 *
 * Every figure here comes from GET /reports/dashboard, which reports measured
 * values. Bandwidth fields prefixed `estimated_` are explicitly labelled in the
 * UI, because they are derived from configured bitrates rather than measured
 * on the wire.
 */
import { useEffect, useState } from 'react';

import { api } from '../lib/api';

interface Dashboard {
  cameras: {
    total: number;
    online: number;
    offline: number;
    unauthorized: number;
    recording: number;
    recording_failed: number;
  };
  storage: { total_bytes: number; free_bytes: number; used_percent: number | null };
  alarms: { open: number; acknowledged: number };
  system: { cpu_percent: number; memory_percent: number };
  bandwidth: { estimated_ingest_mbps: number; estimated_total_mbps: number };
  license: { edition: string; valid: boolean; cameras_in_use: number; licensed_cameras: number };
}

const terabytes = (bytes: number) => (bytes / 1e12).toFixed(2);

export function DashboardPage() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () =>
      api
        .get<Dashboard>('/reports/dashboard')
        .then(setData)
        .catch((caught) => setError(String(caught)));
    void load();
    const timer = setInterval(load, 15_000);
    return () => clearInterval(timer);
  }, []);

  if (error) return <div role="alert">Could not load the dashboard: {error}</div>;
  if (!data) return <div>Loading…</div>;

  return (
    <div className="dashboard">
      <section className="dashboard__cards">
        <Card label="Cameras online" value={`${data.cameras.online} / ${data.cameras.total}`} />
        <Card label="Recording" value={data.cameras.recording} />
        <Card
          label="Recording failed"
          value={data.cameras.recording_failed}
          tone={data.cameras.recording_failed > 0 ? 'critical' : 'normal'}
        />
        <Card
          label="Open alarms"
          value={data.alarms.open}
          tone={data.alarms.open > 0 ? 'warning' : 'normal'}
        />
        <Card label="Storage free" value={`${terabytes(data.storage.free_bytes)} TB`} />
        <Card label="CPU" value={`${data.system.cpu_percent.toFixed(0)}%`} />
      </section>

      <section className="dashboard__detail">
        <h2>Bandwidth</h2>
        <p>
          {data.bandwidth.estimated_total_mbps.toFixed(1)} Mbps{' '}
          <em>(estimated from configured stream bitrates, not measured on the wire)</em>
        </p>

        <h2>Licence</h2>
        <p>
          {data.license.edition} — {data.license.cameras_in_use} of{' '}
          {data.license.licensed_cameras} cameras
          {!data.license.valid && <strong> (running in limited mode)</strong>}
        </p>
      </section>
    </div>
  );
}

function Card({
  label,
  value,
  tone = 'normal',
}: {
  label: string;
  value: string | number;
  tone?: 'normal' | 'warning' | 'critical';
}) {
  return (
    <div className={`card card--${tone}`}>
      <span className="card__label">{label}</span>
      <span className="card__value">{value}</span>
    </div>
  );
}
