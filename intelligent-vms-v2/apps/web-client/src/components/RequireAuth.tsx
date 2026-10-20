import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import { api } from '../lib/api';

/**
 * Route guard. This is CONVENIENCE ONLY, not security: every endpoint
 * re-validates the caller's permissions server-side. Hiding a control the user
 * cannot use is a UX nicety; the server is the authority.
 */
export default function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation();
  if (!api.isAuthenticated()) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}
