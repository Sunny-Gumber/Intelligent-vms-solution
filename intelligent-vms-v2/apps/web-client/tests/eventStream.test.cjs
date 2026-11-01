const { test } = require('node:test');
const assert = require('node:assert/strict');
const { openEventStream } = require('../.test-build/eventStream.js');
const { VmsApi } = require('../.test-build/api.js');

function fixture(overrides = {}) {
  const sockets = [], events = [];
  function factory(url) {
    const socket = { url, sent: [], closed: false,
      send(value) { this.sent.push(JSON.parse(value)); },
      close() { this.closed = true; },
      open() { this.onopen?.({}); },
      message(value) { this.onmessage?.({ data: JSON.stringify(value) }); },
      disconnect(code) { this.onclose?.({ code }); },
    };
    sockets.push(socket);
    return socket;
  }
  const handle = openEventStream({ url: 'wss://vms.test/api/v1/events/stream',
    getToken: () => 'secret', refresh: async () => {}, onEvent: e => events.push(e),
    socketFactory: factory, retryDelay: () => 10, ...overrides });
  return { sockets, events, handle };
}

test('token is only in first frame; event delivery requires acknowledgement', () => {
  const f = fixture();
  try {
    const socket = f.sockets[0];
    assert.equal(new URL(socket.url).search, '');
    assert.deepEqual(socket.sent, []);
    socket.open();
    assert.deepEqual(socket.sent, [{ type: 'authenticate', token: 'secret' }]);
    socket.message({ type: 'connected' });
    socket.message({ type: 'event', data: { id: 1 } });
    assert.deepEqual(f.events, [{ id: 1 }]);
  } finally { f.handle.close(); }
});

test('pre-auth event and malformed frame close without delivery', () => {
  for (const frame of ['{"type":"event","data":"secret"}', '{bad']) {
    const f = fixture();
    f.sockets[0].onmessage({ data: frame });
    assert.equal(f.sockets[0].closed, true);
    assert.deepEqual(f.events, []);
  }
});

test('permanent denial does not reconnect', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture();
  f.sockets[0].disconnect(4403);
  t.mock.timers.tick(60_000);
  assert.equal(f.sockets.length, 1);
  f.handle.close();
});

test('network retries are bounded and disposal cancels pending retry', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture();
  for (let i = 0; i < 12; i++) {
    f.sockets.at(-1).disconnect(1006);
    t.mock.timers.tick(10);
  }
  assert.equal(f.sockets.length, 9);
  const g = fixture();
  g.sockets[0].disconnect(1006);
  g.handle.close();
  t.mock.timers.tick(60_000);
  assert.equal(g.sockets.length, 1);
});

test('unmount during token refresh cannot resurrect a socket', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let finish;
  const f = fixture({ refresh: () => new Promise(resolve => { finish = resolve; }) });
  f.sockets[0].disconnect(4408);
  f.handle.close();
  finish();
  await Promise.resolve();
  t.mock.timers.tick(60_000);
  assert.equal(f.sockets.length, 1);
});

test('expiry refreshes and reconnects with new credentials', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let token = 'old';
  const f = fixture({ getToken: () => token, refresh: async () => { token = 'new'; } });
  f.sockets[0].disconnect(4408);
  await Promise.resolve();
  t.mock.timers.tick(10);
  f.sockets[1].open();
  assert.equal(f.sockets[1].sent[0].token, 'new');
  f.handle.close();
});

test('authentication acknowledgement timeout closes the socket', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture();
  t.mock.timers.tick(6001);
  assert.equal(f.sockets[0].closed, true);
  f.handle.close();
});

test('logout fences an in-flight refresh', async t => {
  let finishRefresh;
  const response = (data, status = 200) => new Response(JSON.stringify(data), { status });
  t.mock.method(globalThis, 'fetch', async url => {
    if (url.endsWith('/auth/login')) return response({ access_token: 'a', refresh_token: 'r' });
    if (url.endsWith('/auth/refresh')) return new Promise(resolve => { finishRefresh = resolve; });
    if (url.endsWith('/auth/logout')) return response({});
    return response({ title: 'Expired' }, 401);
  });
  const api = new VmsApi();
  await api.login('user', 'password');
  const request = api.get('/expired');
  const rejection = assert.rejects(request, /Authentication changed/);
  // Flush the fetch/Response.text chain without a wall-clock delay.
  for (let i = 0; i < 20 && !finishRefresh; i++) await new Promise(setImmediate);
  assert.ok(finishRefresh);
  await api.logout();
  finishRefresh(response({ access_token: 'late', refresh_token: 'late-r' }));
  await rejection;
  assert.equal(api.isAuthenticated, false);
});
