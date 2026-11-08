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

import { api } from '../lib/api';
import { startLiveView, type TileState } from '../lib/liveView';

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
  const [state, setState] = useState<TileState>('connecting');
  const [message, setMessage] = useState<string>('');

  useEffect(() => {
    if (!videoRef.current) return;
    const playback = startLiveView({ cameraId, width, height, video: videoRef.current, api,
      onState: (next, detail) => { setState(next); setMessage(detail); } });
    return () => playback.close();
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
