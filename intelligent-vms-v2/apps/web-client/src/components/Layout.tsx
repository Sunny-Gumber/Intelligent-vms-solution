import { NavLink, Outlet } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';

import { api } from '../lib/api';

const LINKS = [
  { to: '/dashboard', label: 'Dashboard' },
  { to: '/live', label: 'Live' },
  { to: '/playback', label: 'Playback' },
  { to: '/cameras', label: 'Cameras' },
  { to: '/alarms', label: 'Alarms' },
  { to: '/storage', label: 'Storage' },
];

export default function Layout() {
  const version = useQuery({
    queryKey: ['version'],
    queryFn: () => api.get<{ version: string; maturity: string; production_ready: boolean }>(
      '/system/version'),
    staleTime: 60_000,
  });

  return (
    <div className="app">
      <aside className="sidebar">
        <h1>Intelligent VMS</h1>
        <div className="build">
          {version.data ? `${version.data.version} (${version.data.maturity})` : 'connecting...'}
        </div>
        <nav>
          {LINKS.map((link) => (
            <NavLink key={link.to} to={link.to}
                     className={({ isActive }) => (isActive ? 'active' : '')}>
              {link.label}
            </NavLink>
          ))}
        </nav>
        <div style={{ marginTop: 24 }}>
          <button className="secondary" onClick={() => api.logout()}>Sign out</button>
        </div>
      </aside>
      <main className="content">
        {version.data && !version.data.production_ready && (
          <div className="banner">
            Alpha build. Not production ready. See docs/requirements/feature-matrix.md
            for the implementation status of every feature.
          </div>
        )}
        <Outlet />
      </main>
    </div>
  );
}
