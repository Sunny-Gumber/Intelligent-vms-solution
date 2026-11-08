/** First-frame authentication; credentials never appear in the socket URL. */
export interface EventStreamHandle { close(): void }
interface Options {
  url: string;
  getToken: () => string | null;
  refresh: () => Promise<void>;
  onEvent: (event: unknown) => void;
  onClose?: () => void;
  socketFactory?: (url: string) => WebSocket;
  retryDelay?: (attempt: number) => number;
}

export function openEventStream(options: Options): EventStreamHandle {
  let disposed = false;
  let socket: WebSocket | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let watchdog: ReturnType<typeof setTimeout> | undefined;
  let attempts = 0;
  const createSocket = options.socketFactory ?? ((url: string) => new WebSocket(url));
  const handle = { close };

  function close() {
    if (disposed) return;
    disposed = true;
    clearTimeout(timer);
    clearTimeout(watchdog);
    if (socket) {
      socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
      socket.close();
      socket = null;
    }
    options.onClose?.();
  }

  function armWatchdog(current: WebSocket, ms: number) {
    clearTimeout(watchdog);
    watchdog = setTimeout(() => {
      if (!disposed && socket === current) current.close(4000, 'stream timeout');
    }, ms);
  }

  async function retry(code: number) {
    if (disposed || !options.getToken()) return close();
    if ([4401, 4403, 1008].includes(code) || attempts >= 8) return close();
    attempts += 1;
    if (code === 4408) {
      try { await options.refresh(); } catch { return close(); }
      if (disposed || !options.getToken()) return close();
    }
    const delay = options.retryDelay?.(attempts)
      ?? Math.random() * Math.min(30_000, 500 * 2 ** (attempts - 1));
    timer = setTimeout(connect, delay);
  }

  function connect() {
    if (disposed || !options.getToken()) return close();
    let current: WebSocket;
    try { current = createSocket(options.url); } catch { void retry(1006); return; }
    socket = current;
    let authenticated = false;
    armWatchdog(current, 6_000);
    current.onopen = () => {
      if (disposed || socket !== current) return;
      const token = options.getToken();
      if (!token) return close();
      current.send(JSON.stringify({ type: 'authenticate', token }));
    };
    current.onmessage = (message) => {
      if (disposed || socket !== current) return;
      try {
        const parsed = JSON.parse(message.data);
        if (parsed?.type === 'connected' && !authenticated) {
          authenticated = true;
          // Reset backoff only after authentication, not just TCP connection.
          attempts = 0;
        } else if (!authenticated) {
          return close();
        } else if (parsed?.type === 'event') {
          options.onEvent(parsed.data);
        } else if (parsed?.type !== 'heartbeat') {
          return close();
        }
        armWatchdog(current, 30_000);
      } catch { close(); }
    };
    current.onclose = (event) => {
      if (disposed || socket !== current) return;
      clearTimeout(watchdog);
      socket = null;
      void retry(event.code);
    };
    current.onerror = () => { /* onclose owns retry; avoid duplicate attempts. */ };
  }
  connect();
  return handle;
}
