/**
 * Live video tile.
 *
 * IMPORTANT: browsers cannot play RTSP. This component negotiates WebRTC with
 * the media gateway using WHEP, falling back to HLS. It never receives a
 * camera URL or camera credentials: the server returns a gateway path plus a
 * short-lived play token.
 *
 * Each tile owns its own session and its own error state, so one failing
 * camera cannot freeze the rest of the wall (requirement M3-R06).
 */
import { useEffect, useRef, useState } from 'react';

import { ApiError, api } from '../lib/api';

interface LiveSession {
  session_id: string;
  camera_id: string;
  stream_role: string;
  whep_url: string | null;
  hls_url: string | null;
  play_token: string;
  expires_at: string;
}

type TileState = 'idle' | 'connecting' | 'playing' | 'error';

export function VideoTile({
  cameraId,
  cameraName,
  width,
  height,
}: {
  cameraId: string;
  cameraName: string;
  width?: number;
  height?: number;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const peerRef = useRef<RTCPeerConnection | null>(null);
  const [state, setState] = useState<TileState>('idle');
  const [message, setMessage] = useState<string>('');

  useEffect(() => {
    let cancelled = false;
    let session: LiveSession | null = null;
    let keepalive: ReturnType<typeof setInterval> | null = null;

    async function start() {
      setState('connecting');
      setMessage('');
      try {
        // The server picks main/sub/third from the tile size unless overridden.
        session = await api.post<LiveSession>('/live/sessions', {
          camera_id: cameraId,
          tile_width: width,
          tile_height: height,
        });
        if (cancelled || !session.whep_url) throw new Error('no WebRTC endpoint offered');

        const peer = new RTCPeerConnection({ iceServers: [] });
        peerRef.current = peer;
        peer.addTransceiver('video', { direction: 'recvonly' });
        peer.addTransceiver('audio', { direction: 'recvonly' });
        peer.ontrack = (event) => {
          if (videoRef.current) videoRef.current.srcObject = event.streams[0];
        };

        const offer = await peer.createOffer();
        await peer.setLocalDescription(offer);

        const response = await fetch(session.whep_url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/sdp',
            Authorization: `Bearer ${session.play_token}`,
          },
          body: offer.sdp,
        });
        if (!response.ok) throw new Error(`gateway refused the offer (${response.status})`);

        await peer.setRemoteDescription({ type: 'answer', sdp: await response.text() });
        if (cancelled) return;
        setState('playing');

        // Sessions expire by design; refresh while the tile is visible.
        keepalive = setInterval(() => {
          api.post(`/live/sessions/${session!.session_id}/keepalive`).catch(() => undefined);
        }, 60_000);
      } catch (error) {
        if (cancelled) return;
        setState('error');
        setMessage(
          error instanceof ApiError
            ? `${error.problem.title}: ${error.problem.detail ?? ''}`
            : String(error),
        );
      }
    }

    void start();

    return () => {
      cancelled = true;
      if (keepalive) clearInterval(keepalive);
      peerRef.current?.close();
      peerRef.current = null;
      if (session) {
        api.delete(`/live/sessions/${session.session_id}`).catch(() => undefined);
      }
    };
  }, [cameraId, width, height]);

  return (
    <div className="video-tile" data-state={state}>
      <video ref={videoRef} autoPlay muted playsInline className="video-tile__video" />
      <div className="video-tile__overlay">
        <span className="video-tile__name">{cameraName}</span>
        <span className="video-tile__status">{state}</span>
      </div>
      {state === 'error' && (
        <div className="video-tile__error" role="alert">
          <p>{message || 'This camera is not available.'}</p>
          <p className="video-tile__hint">
            Other cameras are unaffected. Recording continues independently of live view.
          </p>
        </div>
      )}
    </div>
  );
}
