import { Navigate, Route, Routes } from 'react-router-dom';

import Layout from './components/Layout';
import RequireAuth from './components/RequireAuth';
import { AlarmsPage as Alarms } from './pages/Alarms';
import { CamerasPage as Cameras } from './pages/Cameras';
import { DashboardPage as Dashboard } from './pages/Dashboard';
import Live from './pages/Live';
import { LoginPage as Login } from './pages/Login';
import { PlaybackPage as Playback } from './pages/Playback';
import { StoragePage as Storage } from './pages/Storage';

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/live" element={<Live />} />
        <Route path="/playback" element={<Playback />} />
        <Route path="/playback/:cameraId" element={<Playback />} />
        <Route path="/cameras" element={<Cameras />} />
        <Route path="/alarms" element={<Alarms />} />
        <Route path="/storage" element={<Storage />} />
      </Route>
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  );
}
