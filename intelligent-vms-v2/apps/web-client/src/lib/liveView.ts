/** One lifecycle owner per tile; late responses can only clean up their own run. */
import Hls from 'hls.js';
import { ApiError } from './api';

export interface LiveSession {
  session_id: string;
  whep_url: string | null;
  hls_url: string | null;
  play_token: string;
}
export type TileState = 'connecting' | 'playing' | 'error';
interface Client {
  post<T>(path: string, body?: unknown): Promise<T>;
  delete<T>(path: string): Promise<T>;
}
interface Options {
  cameraId: string;
  width?: number;
  height?: number;
  video: HTMLVideoElement;
  api: Client;
  onState: (state: TileState, message: string) => void;
  peerFactory?: () => RTCPeerConnection;
  hlsFactory?: () => Hls;
  hlsSupported?: () => boolean;
}
interface Run {
  cancelled: boolean;
  controller: AbortController;
  session: LiveSession | null;
  released: boolean;
  peer: RTCPeerConnection | null;
  hls: Hls | null;
  resource: string | null;
  keepalive?: ReturnType<typeof setTimeout>;
  startup?: ReturnType<typeof setTimeout>;
  disconnect?: ReturnType<typeof setTimeout>;
  removers: Array<() => void>;
  fallingBack: boolean;
  playing: boolean;
}

export function mediaURL(raw: string, base?: string): URL {
  const url = new URL(raw, base);
  if (url.username || url.password || url.search || url.hash ||
      (url.protocol !== 'https:' && !(url.protocol === 'http:' &&
        ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)))) {
    throw new Error('Media requires a credential-free HTTPS URL (HTTP only on loopback)');
  }
  return url;
}

export function hlsResourceURL(raw: string, base: string): URL {
  const url = new URL(raw, base);
  for (const [key, value] of url.searchParams) {
    if (!((['_HLS_msn', '_HLS_part'].includes(key) && /^\d+$/.test(value)) ||
          (key === '_HLS_skip' && ['YES', 'v2'].includes(value)) ||
          (key === 'cookieCheck' && value === '1') ||
          (key === 'session' && /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value)))) {
      throw new Error('Unexpected media query parameter');
    }
  }
  const clean = new URL(url);
  clean.search = '';
  mediaURL(clean.href);
  if (url.origin !== new URL(base).origin) throw new Error('Cross-origin media resource refused');
  return url;
}

export function waitForIce(peer: RTCPeerConnection, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout>;
    const finish = (error?: Error) => {
      clearTimeout(timer);
      peer.removeEventListener('icegatheringstatechange', check);
      signal.removeEventListener('abort', abort);
      error ? reject(error) : resolve();
    };
    const check = () => { if (peer.iceGatheringState === 'complete') finish(); };
    const abort = () => finish(new Error('Playback cancelled'));
    peer.addEventListener('icegatheringstatechange', check);
    signal.addEventListener('abort', abort, { once: true });
    timer = setTimeout(() => finish(new Error('ICE gathering timed out')), 5_000);
    if (signal.aborted) abort(); else check();
  });
}

export function startLiveView(options: Options): { close(): void } {
  const { video, api } = options;
  let disposed = false;
  let active: Run | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let attempts = 0;

  function release(run: Run) {
    if (!run.session || run.released) return;
    run.released = true;
    void api.delete(`/live/sessions/${encodeURIComponent(run.session.session_id)}`).catch(() => {});
  }
  function deleteResource(run: Run) {
    const url = run.resource;
    run.resource = null;
    if (url) void fetch(url, { method: 'DELETE', redirect: 'error',
      headers: { Authorization: `Bearer ${run.session!.play_token}` },
      signal: AbortSignal.timeout(5_000) }).catch(() => {});
  }
  function stopWebRTC(run: Run) {
    run.peer?.close();
    run.peer = null;
    deleteResource(run);
    if (active === run) video.srcObject = null;
  }
  function cleanup(run: Run) {
    if (run.cancelled) return;
    run.cancelled = true;
    run.controller.abort();
    clearTimeout(run.keepalive);
    clearTimeout(run.startup);
    clearTimeout(run.disconnect);
    for (const remove of run.removers) remove();
    run.removers = [];
    stopWebRTC(run);
    run.hls?.destroy();
    run.hls = null;
    if (active === run) {
      video.pause();
      video.removeAttribute('src');
      video.load();
    }
    release(run);
  }
  function fail(run: Run, error: unknown, permanent = false) {
    if (disposed || run.cancelled || active !== run) return;
    cleanup(run);
    options.onState('error', error instanceof Error ? error.message : 'Playback unavailable');
    if (!permanent && attempts < 3) {
      attempts += 1;
      retryTimer = setTimeout(() => void start(), Math.random() * Math.min(10_000, 1000 * 2 ** attempts));
    }
  }
  function armStartup(run: Run) {
    clearTimeout(run.startup);
    if (run.playing) return;
    run.startup = setTimeout(() => fail(run, new Error('No playable media received')), 20_000);
  }
  function fallback(run: Run) {
    if (disposed || run.cancelled || run.fallingBack || active !== run) return;
    run.fallingBack = true;
    run.playing = false;
    stopWebRTC(run);
    const url = run.session?.hls_url;
    try {
      if (!url || !(options.hlsSupported ?? Hls.isSupported)()) {
        throw new Error('WebRTC unavailable; authenticated HLS is unsupported in this browser');
      }
      const endpoint = mediaURL(url);
      const configure = (xhr: XMLHttpRequest, target: string) => {
        hlsResourceURL(target, endpoint.href);
        xhr.setRequestHeader('Authorization', `Bearer ${run.session!.play_token}`);
      };
      const hls = options.hlsFactory?.() ?? new Hls({ xhrSetup: configure,
        maxBufferLength: 15, maxMaxBufferLength: 30, backBufferLength: 0 });
      // Custom factories used by tests also receive the credential boundary.
      hls.config.xhrSetup = configure;
      run.hls = hls;
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (data.fatal) fail(run, new Error('HLS playback failed'),
          data.response?.code === 401 || data.response?.code === 403);
      });
      hls.loadSource(endpoint.href);
      hls.attachMedia(video);
      armStartup(run);
    } catch (error) { fail(run, error); }
  }
  async function keepalive(run: Run) {
    if (disposed || run.cancelled) return;
    try {
      await api.post(`/live/sessions/${encodeURIComponent(run.session!.session_id)}/keepalive`);
      if (!disposed && !run.cancelled) run.keepalive = setTimeout(() => void keepalive(run), 60_000);
    } catch (error) {
      const denied = error instanceof ApiError && [401, 403, 404].includes(error.problem.status);
      fail(run, error, denied);
    }
  }
  async function start() {
    if (disposed) return;
    const run: Run = { cancelled: false, controller: new AbortController(), session: null,
      released: false, peer: null, hls: null, resource: null, removers: [], fallingBack: false, playing: false };
    active = run;
    options.onState('connecting', '');
    const playing = () => {
      if (disposed || run.cancelled || active !== run) return;
      run.playing = true;
      clearTimeout(run.startup);
      options.onState('playing', '');
    };
    const stalled = () => {
      if (disposed || run.cancelled) return;
      run.playing = false;
      options.onState('connecting', 'Waiting for media');
      armStartup(run);
    };
    video.addEventListener('playing', playing);
    video.addEventListener('waiting', stalled);
    run.removers.push(() => video.removeEventListener('playing', playing),
      () => video.removeEventListener('waiting', stalled));
    try {
      run.session = await api.post<LiveSession>('/live/sessions', {
        camera_id: options.cameraId, tile_width: options.width, tile_height: options.height });
      if (disposed || run.cancelled) { release(run); return; }
      run.keepalive = setTimeout(() => void keepalive(run), 60_000);
      if (!run.session.whep_url) { fallback(run); return; }
      const endpoint = mediaURL(run.session.whep_url);
      const peer = (options.peerFactory ?? (() => new RTCPeerConnection({ iceServers: [] })))();
      run.peer = peer;
      peer.addTransceiver('video', { direction: 'recvonly' });
      peer.addTransceiver('audio', { direction: 'recvonly' });
      peer.ontrack = event => {
        if (disposed || run.cancelled || run.peer !== peer) return;
        video.srcObject = event.streams[0] ?? new MediaStream([event.track]);
      };
      peer.onconnectionstatechange = () => {
        if (disposed || run.cancelled || run.peer !== peer) return;
        if (peer.connectionState === 'failed') fallback(run);
        else if (peer.connectionState === 'disconnected') {
          clearTimeout(run.disconnect);
          run.disconnect = setTimeout(() => fallback(run), 5_000);
        } else if (peer.connectionState === 'connected') clearTimeout(run.disconnect);
      };
      await peer.setLocalDescription(await peer.createOffer());
      await waitForIce(peer, run.controller.signal);
      if (disposed || run.cancelled || run.peer !== peer) return;
      const response = await fetch(endpoint.href, { method: 'POST', redirect: 'error',
        headers: { 'Content-Type': 'application/sdp', Authorization: `Bearer ${run.session.play_token}` },
        body: peer.localDescription?.sdp,
        signal: AbortSignal.any([run.controller.signal, AbortSignal.timeout(15_000)]) });
      if (!response.ok) {
        if ([401, 403].includes(response.status)) {
          fail(run, new Error('Playback permission denied'), true); return;
        }
        throw new Error(`Gateway refused WebRTC (${response.status})`);
      }
      const location = response.headers.get('Location');
      if (!location) throw new Error('Gateway did not expose its WHEP resource URL');
      const resource = mediaURL(location, endpoint.href);
      if (resource.origin !== endpoint.origin) throw new Error('Cross-origin WHEP resource refused');
      run.resource = resource.href;
      if (disposed || run.cancelled || run.peer !== peer) { deleteResource(run); return; }
      await peer.setRemoteDescription({ type: 'answer', sdp: await response.text() });
      if (disposed || run.cancelled || run.peer !== peer) return;
      armStartup(run);
    } catch (error) {
      if (disposed || run.cancelled) return;
      if (!run.session) fail(run, error,
        error instanceof ApiError && [401, 403, 404].includes(error.problem.status));
      else fallback(run);
    }
  }
  void start();
  return { close() {
    disposed = true;
    clearTimeout(retryTimer);
    if (active) cleanup(active);
  } };
}
