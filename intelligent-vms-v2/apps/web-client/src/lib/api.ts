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

  constructor(private readonly baseUrl = '/api/v1') {}

  get isAuthenticated(): boolean {
    return this.tokens !== null;
  }

  async login(username: string, password: string, totpCode?: string): Promise<void> {
    const body = await this.raw<{ access_token: string; refresh_token: string }>(
      'POST',
      '/auth/login',
      { username, password, totp_code: totpCode ?? null },
      false,
    );
    this.tokens = { access: body.access_token, refresh: body.refresh_token };
  }

  async logout(): Promise<void> {
    if (!this.tokens) return;
    try {
      await this.request('POST', '/auth/logout', { refresh_token: this.tokens.refresh });
    } finally {
      this.tokens = null;
    }
  }

  /** Authenticated request with a single transparent refresh on 401. */
  async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    try {
      return await this.raw<T>(method, path, body, true);
    } catch (error) {
      if (error instanceof ApiError && error.problem.status === 401 && this.tokens) {
        await this.refresh();
        return this.raw<T>(method, path, body, true);
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
      this.refreshing = (async () => {
        const body = await this.raw<{ access_token: string; refresh_token: string }>(
          'POST',
          '/auth/refresh',
          { refresh_token: this.tokens!.refresh },
          false,
        );
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

  /** WebSocket event stream. The token goes in the query string because
   *  browsers cannot set headers on a WebSocket handshake. */
  openEventStream(onEvent: (event: unknown) => void): WebSocket | null {
    if (!this.tokens) return null;
    const url = new URL(`${this.baseUrl}/events/stream`, window.location.origin);
    url.protocol = url.protocol.replace('http', 'ws');
    url.searchParams.set('token', this.tokens.access);

    const socket = new WebSocket(url.toString());
    socket.onmessage = (message) => {
      const parsed = JSON.parse(message.data);
      if (parsed.type === 'event') onEvent(parsed.data);
    };
    return socket;
  }
}

export const api = new VmsApi();
