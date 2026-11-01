import { openEventStream, type EventStreamHandle } from './eventStream';

/**
 * Typed client for the Intelligent VMS control API.
 *
 * Responsibilities kept deliberately narrow:
 *  - hold tokens IN MEMORY (never localStorage: XSS would otherwise exfiltrate
 *    a long-lived refresh token)
 *  - refresh exactly once per 401 and replay the original request
 *  - surface the server's stable `code` so callers branch on codes, not text
 */

export interface Problem {
  title: string;
  status: number;
  code: string;
  detail?: string;
  correlation_id?: string;
}

export class ApiError extends Error {
  constructor(readonly problem: Problem) {
    super(problem.detail ?? problem.title);
    this.name = 'ApiError';
  }
  /** Stable machine-readable token. Branch on this, never on the message. */
  get code(): string {
    return this.problem.code;
  }
}

interface Tokens {
  access: string;
  refresh: string;
}

export class VmsApi {
  private tokens: Tokens | null = null;
  private refreshing: Promise<void> | null = null;
  private authGeneration = 0;
  private streams = new Set<EventStreamHandle>();

  constructor(private readonly baseUrl = '/api/v1') {}

  get isAuthenticated(): boolean {
    return this.tokens !== null;
  }

  async login(username: string, password: string, totpCode?: string): Promise<void> {
    const generation = ++this.authGeneration;
    for (const stream of this.streams) stream.close();
    this.tokens = null;
    const body = await this.raw<{ access_token: string; refresh_token: string }>(
      'POST',
      '/auth/login',
      { username, password, totp_code: totpCode ?? null },
      false,
    );
    if (generation === this.authGeneration) {
      this.tokens = { access: body.access_token, refresh: body.refresh_token };
    }
  }

  async logout(): Promise<void> {
    ++this.authGeneration;
    for (const stream of this.streams) stream.close();
    if (!this.tokens) return;
    // raw captures the current Authorization header before its first await.
    const request = this.raw('POST', '/auth/logout', { refresh_token: this.tokens.refresh }, true);
    this.tokens = null;
    await request;
  }

  /** Authenticated request with a single transparent refresh on 401. */
  async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const generation = this.authGeneration;
    try {
      const result = await this.raw<T>(method, path, body, true);
      if (generation !== this.authGeneration) throw new Error('Authentication changed');
      return result;
    } catch (error) {
      if (generation !== this.authGeneration) throw new Error('Authentication changed');
      if (error instanceof ApiError && error.problem.status === 401 && this.tokens) {
        await this.refresh();
        if (generation !== this.authGeneration) throw new Error('Authentication changed');
        const result = await this.raw<T>(method, path, body, true);
        if (generation !== this.authGeneration) throw new Error('Authentication changed');
        return result;
      }
      throw error;
    }
  }

  get<T>(path: string): Promise<T> {
    return this.request<T>('GET', path);
  }

  post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>('POST', path, body);
  }

  patch<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>('PATCH', path, body);
  }

  delete<T>(path: string): Promise<T> {
    return this.request<T>('DELETE', path);
  }

  /** Concurrent 401s must trigger only ONE refresh, or the rotation chain breaks. */
  private async refresh(): Promise<void> {
    if (!this.refreshing) {
      const generation = this.authGeneration;
      const refreshToken = this.tokens?.refresh;
      if (!refreshToken) throw new Error('Not authenticated');
      this.refreshing = (async () => {
        const body = await this.raw<{ access_token: string; refresh_token: string }>(
          'POST',
          '/auth/refresh',
          { refresh_token: refreshToken },
          false,
        );
        if (generation !== this.authGeneration) throw new Error('Authentication changed');
        this.tokens = { access: body.access_token, refresh: body.refresh_token };
      })().finally(() => {
        this.refreshing = null;
      });
    }
    return this.refreshing;
  }

  private async raw<T>(
    method: string,
    path: string,
    body: unknown,
    authenticated: boolean,
  ): Promise<T> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (authenticated && this.tokens) {
      headers.Authorization = `Bearer ${this.tokens.access}`;
    }

    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      signal: AbortSignal.timeout(15_000),
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });

    if (response.status === 204) return undefined as T;

    const text = await response.text();
    const payload = text ? JSON.parse(text) : {};

    if (!response.ok) {
      throw new ApiError({
        title: payload.title ?? response.statusText,
        status: response.status,
        code: payload.code ?? 'unknown_error',
        detail: payload.detail,
        correlation_id: payload.correlation_id ?? response.headers.get('X-Correlation-Id') ?? undefined,
      });
    }
    return payload as T;
  }

  /** Authenticates after open; disposal/logout cancels all retry work. */
  openEventStream(onEvent: (event: unknown) => void): EventStreamHandle | null {
    if (!this.tokens) return null;
    const url = new URL(`${this.baseUrl}/events/stream`, window.location.origin);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    if (url.protocol === 'ws:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) {
      throw new Error('Event streaming requires HTTPS outside loopback development');
    }
    let stream: EventStreamHandle | null = null;
    stream = openEventStream({
      url: url.toString(), getToken: () => this.tokens?.access ?? null,
      refresh: () => this.refresh(), onEvent,
      onClose: () => { if (stream) this.streams.delete(stream); },
    });
    this.streams.add(stream);
    return stream;
  }
}

export const api = new VmsApi();
