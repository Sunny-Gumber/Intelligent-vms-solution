const { test } = require('node:test');
const assert = require('node:assert/strict');
const { startLiveView, hlsResourceURL, waitForIce } = require('../.test-build/liveView.js');
const { ApiError } = require('../.test-build/api.js');
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return { promise, resolve, reject }; };
class Video extends EventTarget {
  srcObject = null; resets = 0;
  pause() {} removeAttribute() {} load() { this.resets++; }
}
class Peer extends EventTarget {
  iceGatheringState = 'complete'; closed = false;
  addTransceiver() {} async createOffer() { return { type: 'offer', sdp: 'incomplete' }; }
  async setLocalDescription() { this.localDescription = { sdp: 'complete-with-candidates' }; }
  async setRemoteDescription(value) { this.answer = value; }
  close() { this.closed = true; }
}
const session = { session_id: 's1', play_token: 'secret', whep_url: 'https://media.test/cam/whep', hls_url: 'https://media.test/cam/index.m3u8' };
function fixture(overrides = {}) {
  const video = new Video(), peer = new Peer(), states = [], deleted = [], posts = [];
  const hls = { config: {}, on() {}, loadSource(url) { this.url = url; }, attachMedia() {}, destroy() { this.destroyed = true; } };
  const api = { async post(path) { posts.push(path); return session; }, async delete(path) { deleted.push(path); } };
  const handle = startLiveView({ video, cameraId: 'c1', api, onState: s => states.push(s), peerFactory: () => peer,
    hlsSupported: () => true, hlsFactory: () => hls, ...overrides });
  return { video, peer, states, deleted, posts, hls, handle };
}
function gateway(t) {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, ...options });
    return options.method === 'POST' ? new Response('answer', { status: 201, headers: { Location: '/cam/whep/resource' } }) : new Response(null, { status: 204 });
  });
  return calls;
}
test('late acquisition after disposal releases its own session without touching a replacement tile', async () => {
  const late = deferred(), deleted = [], video = new Video();
  const f = fixture({ video, api: { post: () => late.promise, delete: async p => deleted.push(p) } });
  f.handle.close();
  video.srcObject = 'replacement';
  late.resolve(session); await flush();
  assert.deepEqual(deleted, ['/live/sessions/s1']);
  assert.equal(video.srcObject, 'replacement');
  f.handle.close();
  assert.equal(video.srcObject, 'replacement');
});
test('WHEP sends gathered SDP and header; playing requires media, cleanup deletes both resources', async t => {
  const calls = gateway(t), f = fixture(); await flush();
  assert.equal(calls[0].body, 'complete-with-candidates');
  assert.equal(calls[0].headers.Authorization, 'Bearer secret');
  assert.equal(new URL(calls[0].url).search, '');
  assert.deepEqual(f.states, ['connecting']);
  f.video.dispatchEvent(new Event('playing'));
  assert.equal(f.states.at(-1), 'playing');
  f.handle.close(); await flush();
  assert.equal(f.peer.closed, true);
  assert.equal(calls[1].method, 'DELETE');
  assert.equal(calls[1].url, 'https://media.test/cam/whep/resource');
  assert.deepEqual(f.deleted, ['/live/sessions/s1']);
});
test('late WHEP response is deleted after disposal', async t => {
  const late = deferred(), calls = [];
  t.mock.method(globalThis, 'fetch', (url, init) => { calls.push({ url, ...init }); return init.method === 'POST' ? late.promise : Promise.resolve(new Response(null, { status: 204 })); });
  const f = fixture(); await flush(); f.handle.close();
  late.resolve(new Response('answer', { status: 201, headers: { Location: '/resource' } })); await flush();
  assert.equal(calls[1].method, 'DELETE');
  assert.equal(f.peer.answer, undefined);
});
test('cancellation during ICE gathering removes listeners', async () => {
  const peer = new Peer(); peer.iceGatheringState = 'gathering';
  let adds = 0, removes = 0;
  const add = peer.addEventListener.bind(peer), remove = peer.removeEventListener.bind(peer);
  peer.addEventListener = (...a) => { adds++; add(...a); };
  peer.removeEventListener = (...a) => { removes++; remove(...a); };
  const controller = new AbortController(), pending = waitForIce(peer, controller.signal);
  controller.abort(); await assert.rejects(pending, /cancelled/);
  assert.equal(adds, removes);
});
test('WHEP authentication denial is terminal and never falls back', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  t.mock.method(globalThis, 'fetch', async () => new Response('', { status: 401 }));
  const f = fixture(); await flush(); t.mock.timers.tick(120_000); await flush();
  assert.equal(f.hls.url, undefined);
  assert.equal(f.states.at(-1), 'error');
  assert.equal(f.posts.length, 1); f.handle.close();
});
test('HLS fallback attaches headers to allowed resources and rejects credentials or foreign origins', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response('', { status: 503 }));
  const f = fixture(); await flush();
  assert.equal(f.hls.url, session.hls_url);
  const headers = {}, xhr = { setRequestHeader(k,v) { headers[k]=v; } };
  f.hls.config.xhrSetup(xhr, 'https://media.test/cam/index.m3u8?_HLS_msn=12&_HLS_part=0&_HLS_skip=YES');
  assert.equal(headers.Authorization, 'Bearer secret');
  for (const url of ['https://foreign.test/a', 'https://media.test/a?token=secret', 'http://media.test/a', 'https://user:secret@media.test/a']) {
    assert.throws(() => f.hls.config.xhrSetup(xhr, url));
  }
  f.handle.close(); assert.equal(f.hls.destroyed, true);
});
test('HLS permits only validated low-latency control parameters', () => {
  assert.equal(hlsResourceURL('segment.mp4', session.hls_url).pathname, '/cam/segment.mp4');
  assert.throws(() => hlsResourceURL('index.m3u8?_HLS_msn=secret', session.hls_url));
  assert.equal(hlsResourceURL('video.m3u8?session=81d5ac00-7a23-4eba-ba37-44a20a7ceac9', session.hls_url).searchParams.get('session'), '81d5ac00-7a23-4eba-ba37-44a20a7ceac9');
  assert.throws(() => hlsResourceURL('video.m3u8?session=secret', session.hls_url));
});
test('keepalive never overlaps and authorization failure releases the session', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] }); gateway(t);
  const pending = deferred(); let count = 0;
  const f = fixture({ api: { post: p => { if (p.endsWith('keepalive')) { count++; return pending.promise; } return Promise.resolve(session); }, delete: async () => {} } });
  await flush(); f.video.dispatchEvent(new Event('playing'));
  t.mock.timers.tick(60_000); await flush();
  t.mock.timers.tick(120_000); await flush(); assert.equal(count, 1);
  pending.reject(new ApiError({ status: 403, title: 'Denied' })); await flush();
  assert.equal(f.states.at(-1), 'error'); assert.equal(f.peer.closed, true);
  t.mock.timers.tick(120_000); await flush(); assert.equal(count, 1); f.handle.close();
});
test('startup failures retry a bounded number of times and disposal cancels retries', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  t.mock.method(Math, 'random', () => 1);
  let calls = 0;
  const f = fixture({ api: { post: async () => { calls++; throw new Error('offline'); }, delete: async () => {} } });
  for (let i=0; i<6; i++) { await flush(); t.mock.timers.tick(10_000); }
  await flush(); assert.equal(calls, 4); f.handle.close();
});
