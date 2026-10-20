/** Mirrors packages/shared-contracts/vms_contracts/dto.py. */

export type CameraState =
  | 'provisioning' | 'online' | 'offline' | 'unauthorized' | 'disabled' | 'error';

export type StreamRole = 'main' | 'sub' | 'third';

export interface Stream {
  id: string;
  role: StreamRole;
  enabled: boolean;
  codec: string | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  bitrate_kbps: number | null;
  // NOTE: there is deliberately no `source_url` here. The server never sends
  // camera URLs or credentials to a browser.
}

export interface Camera {
  id: string;
  name: string;
  site_id: string | null;
  host: string;
  port: number;
  protocol: 'onvif' | 'rtsp' | 'vendor_adapter';
  vendor: string | null;
  model: string | null;
  state: CameraState;
  enabled: boolean;
  recording_mode: string;
  recording_state: string;
  capabilities: Record<string, boolean | string | null>;
  streams: Stream[];
  tags: string[];
  last_seen_at: string | null;
}

export interface LiveSession {
  session_id: string;
  camera_id: string;
  stream_role: StreamRole;
  path: string;
  whep_url: string | null;
  hls_url: string | null;
  play_token: string;
  expires_at: string;
  estimated_kbps: number | null;
}

export interface Alarm {
  id: string;
  state: 'new' | 'active' | 'acknowledged' | 'resolved' | 'closed';
  severity: 'info' | 'low' | 'medium' | 'high' | 'critical';
  camera_id: string | null;
  title: string;
  first_event_at: string;
  last_event_at: string;
  event_count: number;
  acknowledged_by: string | null;
  /** Optimistic concurrency token - must be echoed on every mutation. */
  version: number;
}

export interface Problem {
  title: string;
  status: number;
  code: string;
  detail: string | null;
  correlation_id: string | null;
  [key: string]: unknown;
}


export interface TimelineBucket {
  start: string;
  end: string;
  recorded_s: number;
  event_count: number;
  has_gap: boolean;
}

export interface PlaybackManifest {
  camera_id: string;
  message: string | null;
  gaps: { start: string; end: string; duration_s: number; cause: string }[];
  segments: {
    id: string;
    started_at: string;
    duration_s: number | null;
    size_bytes: number | null;
    has_gap_before: boolean;
  }[];
}

export interface Volume {
  id: string;
  name: string;
  path: string;
  state: string;
  total_bytes: number | null;
  free_bytes: number | null;
  camera_count: number;
  raid_status: string | null;
}
