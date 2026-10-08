/**
 * Executes clients/windows live.html in Node.
 * A C# string search does not cover the WHEP generation race.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const htmlPath = join(
  dirname(fileURLToPath(import.meta.url)),
  '../src/IntelligentVMS.Desktop/Media/live.html',
);

const rejections = [];
process.on('unhandledRejection', (error) => {
  rejections.push(error);
});

function extractScript(html) {
  const match = html.match(/<script>([\s\S]*?)<\/script>/);
  if (!match) throw new Error('live.html has no inline script');
  return match[1];
}

function authorizationOf(headers) {
  if (!headers) return undefined;
  if (typeof headers.get === 'function') {
    return headers.get('Authorization') ?? headers.get('authorization');
  }
  return headers.Authorization ?? headers.authorization;
}

function abortError() {
  return new DOMException('The operation was aborted.', 'AbortError');
}

async function settle() {
  for (let i = 0; i < 30; i += 1) {
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
  }
}

function loadPage() {
  const messages = [];
  const requests = [];
  const peers = [];
  const messageListeners = [];
  const video = { srcObject: null };
  const timers = new Map();
  let nextTimer = 1;
  let failDeletes = false;
  const script = extractScript(readFileSync(htmlPath, 'utf8'));

  class FakePeerConnection {
    constructor() {
      this.iceGatheringState = 'new';
      this.connectionState = 'new';
      this.localDescription = null;
      this.remoteDescription = null;
      this.ontrack = null;
      this.onconnectionstatechange = null;
      this.failRemote = false;
      this.listeners = new Map();
      peers.push(this);
    }

    addTransceiver() {
      return {};
    }

    createOffer() {
      return Promise.resolve({ type: 'offer', sdp: 'v=0\r\noffer\r\n' });
    }

    setLocalDescription(description) {
      this.localDescription = description;
      this.iceGatheringState = 'complete';
      return Promise.resolve();
    }

    setRemoteDescription(description) {
      if (this.failRemote) return Promise.reject(new Error('bad answer'));
      this.remoteDescription = description;
      return Promise.resolve();
    }

    addEventListener(type, listener) {
      const bucket = this.listeners.get(type) ?? [];
      bucket.push(listener);
      this.listeners.set(type, bucket);
    }

    removeEventListener(type, listener) {
      const bucket = this.listeners.get(type) ?? [];
      this.listeners.set(type, bucket.filter((candidate) => candidate !== listener));
    }

    close() {
      this.connectionState = 'closed';
      if (typeof this.onconnectionstatechange === 'function') this.onconnectionstatechange();
    }
  }

  function responseFor(payload) {
    const status = payload.status;
    const location = payload.location ?? null;
    return {
      status,
      ok: status >= 200 && status < 300,
      headers: {
        get(name) {
          return String(name).toLowerCase() === 'location' ? location : null;
        },
      },
      text: async () => {
        if (payload.textError) throw new Error('response body failed');
        return payload.sdp ?? '';
      },
    };
  }

  function fetch(url, init = {}) {
    const signal = init.signal;
    if (signal?.aborted) return Promise.reject(abortError());
    const method = String(init.method || 'GET').toUpperCase();
    const entry = {
      url: String(url),
      method,
      headers: init.headers || {},
      authorization: authorizationOf(init.headers),
      body: init.body ?? null,
      keepalive: Boolean(init.keepalive),
    };
    requests.push(entry);
    if (method === 'DELETE') {
      if (failDeletes) return Promise.reject(new Error('delete failed'));
      return Promise.resolve(responseFor({ status: 200, sdp: '' }));
    }
    if (method === 'OPTIONS') {
      return new Promise((resolve, reject) => {
        const onAbort = () => reject(abortError());
        if (signal) signal.addEventListener('abort', onAbort, { once: true });
        queueMicrotask(() => {
          if (signal) signal.removeEventListener('abort', onAbort);
          if (signal?.aborted) {
            reject(abortError());
            return;
          }
          resolve(responseFor({ status: 200, sdp: '' }));
        });
      });
    }
    if (method === 'POST') {
      return new Promise((resolve, reject) => {
        const onAbort = () => reject(abortError());
        if (signal) signal.addEventListener('abort', onAbort, { once: true });
        entry.finish = (payload) => {
          if (signal) signal.removeEventListener('abort', onAbort);
          if (signal?.aborted) {
            reject(abortError());
            return;
          }
          resolve(responseFor(payload));
        };
      });
    }
    return Promise.reject(new Error(`unexpected ${method}`));
  }

  const chrome = {
    webview: {
      postMessage(payload) {
        messages.push(payload);
      },
      addEventListener(type, listener) {
        if (type !== 'message') throw new Error(`unexpected listener ${type}`);
        messageListeners.push(listener);
      },
    },
  };
  const windowListeners = [];
  const context = {
    console,
    URL,
    AbortController,
    DOMException,
    setTimeout(fn, delay) {
      const id = nextTimer;
      nextTimer += 1;
      timers.set(id, { fn, delay });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    fetch,
    RTCPeerConnection: FakePeerConnection,
    document: {
      getElementById(id) {
        if (id !== 'video') throw new Error(`missing element ${id}`);
        return video;
      },
    },
    chrome,
  };
  context.window = {
    addEventListener(type, listener) {
      windowListeners.push({ type, listener });
    },
  };
  context.globalThis = context;
  vm.createContext(context);
  try {
    vm.runInContext(script, context, { filename: 'live.html' });
  } catch (error) {
    throw new Error(`live.html failed to evaluate: ${error.message}`);
  }

  function dispatch(data) {
    if (messageListeners.length !== 1) {
      throw new Error(`expected one webview listener, found ${messageListeners.length}`);
    }
    messageListeners[0]({ data });
  }

  return {
    messages,
    requests,
    peers,
    video,
    timers,
    dispatch,
    setFailDeletes(value) {
      failDeletes = value;
    },
    posts() {
      return requests.filter((entry) => entry.method === 'POST');
    },
    deletes() {
      return requests.filter((entry) => entry.method === 'DELETE');
    },
    states() {
      return messages.filter((message) => Object.hasOwn(message, 'state')).map((message) => message.state);
    },
    events() {
      return messages.filter((message) => message.event).map((message) => message.event);
    },
  };
}

async function waitForPosts(page, count) {
  for (let i = 0; i < 50; i += 1) {
    if (page.posts().length >= count) return page.posts();
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
  }
  throw new Error(`timed out waiting for ${count} POST(s); saw ${JSON.stringify(page.requests)}`);
}

function rejectionBaseline() {
  return rejections.length;
}

function assertNoNewRejection(baseline) {
  assert.equal(
    rejections.length,
    baseline,
    `unexpected rejection: ${rejections.slice(baseline).map((error) => error && error.message).join('; ')}`,
  );
}

test('deferred POST after stop deletes the late session with its original token', async () => {
  const baseline = rejectionBaseline();
  const page = loadPage();
  const token = 'token-original';
  const location = 'https://media.example/cam/whep/session-late';
  page.dispatch({ action: 'start', webrtcUrl: 'https://media.example/cam', accessToken: token });
  const posts = await waitForPosts(page, 1);
  page.dispatch({ action: 'stop' });
  await settle();
  posts[0].finish({ status: 201, location, sdp: 'v=0\r\nanswer\r\n' });
  await settle();

  const latePeer = page.peers[0];
  latePeer.ontrack?.({ streams: [{ id: 'late' }] });
  latePeer.connectionState = 'connected';
  latePeer.onconnectionstatechange?.();
  await settle();

  assert.deepEqual(page.deletes().map((entry) => entry.url), [location]);
  assert.equal(page.deletes()[0].authorization, `Bearer ${token}`);
  assert.equal(page.deletes()[0].keepalive, true);
  assert.equal(page.video.srcObject, null);
  assert.equal(page.states().includes('LIVE'), false);
  assert.equal(page.events().includes('session-lifecycle-error'), false);
  assertNoNewRejection(baseline);
});

test('overlapping starts delete the superseded session with its own token and keep the newer session', async () => {
  const baseline = rejectionBaseline();
  const page = loadPage();
  const locationA = 'https://media.example/a/whep/session-a';
  const locationB = 'https://media.example/b/whep/session-b';
  page.dispatch({ action: 'start', webrtcUrl: 'https://media.example/a', accessToken: 'token-a' });
  await waitForPosts(page, 1);
  page.dispatch({ action: 'start', webrtcUrl: 'https://media.example/b', accessToken: 'token-b' });
  const posts = await waitForPosts(page, 2);
  const postA = posts.find((entry) => entry.url === 'https://media.example/a/whep');
  const postB = posts.find((entry) => entry.url === 'https://media.example/b/whep');
  assert.equal(postA.authorization, 'Bearer token-a');
  assert.equal(postB.authorization, 'Bearer token-b');

  postB.finish({ status: 201, location: locationB, sdp: 'v=0\r\nanswer-b\r\n' });
  await settle();
  const peerB = page.peers[1];
  const streamB = { id: 'stream-b' };
  peerB.connectionState = 'connected';
  peerB.onconnectionstatechange();
  peerB.ontrack({ streams: [streamB] });
  await settle();
  assert.equal(page.states().at(-1), 'LIVE');
  assert.equal(page.video.srcObject, streamB);

  postA.finish({ status: 201, location: locationA, sdp: 'v=0\r\nanswer-a\r\n' });
  await settle();
  const peerA = page.peers[0];
  peerA.ontrack?.({ streams: [{ id: 'stream-a' }] });
  peerA.connectionState = 'connected';
  peerA.onconnectionstatechange?.();
  await settle();

  assert.deepEqual(page.deletes().map((entry) => [entry.url, entry.authorization]), [
    [locationA, 'Bearer token-a'],
  ]);
  assert.equal(page.video.srcObject, streamB);
  assert.equal(peerB.connectionState, 'connected');
  assert.equal(page.states().at(-1), 'LIVE');

  page.dispatch({ action: 'stop' });
  await settle();
  assert.deepEqual(page.deletes().map((entry) => [entry.url, entry.authorization]), [
    [locationA, 'Bearer token-a'],
    [locationB, 'Bearer token-b'],
  ]);
  assert.equal(page.states().at(-1), 'IDLE');
  assert.equal(page.events().includes('session-lifecycle-error'), false);
  assertNoNewRejection(baseline);
});

test('failed DELETE is observable, does not throw, and does not corrupt the next attempt', async () => {
  const baseline = rejectionBaseline();
  const page = loadPage();
  const locationA = 'https://media.example/a/whep/session-a';
  const locationB = 'https://media.example/b/whep/session-b';
  page.dispatch({ action: 'start', webrtcUrl: 'https://media.example/a', accessToken: 'token-a' });
  const first = await waitForPosts(page, 1);
  first[0].finish({ status: 201, location: locationA, sdp: 'v=0\r\nanswer-a\r\n' });
  await settle();
  page.peers[0].connectionState = 'connected';
  page.peers[0].onconnectionstatechange();
  await settle();
  assert.equal(page.states().at(-1), 'LIVE');

  page.setFailDeletes(true);
  page.dispatch({ action: 'stop' });
  await settle();
  assert.deepEqual(page.deletes().map((entry) => [entry.url, entry.authorization]), [
    [locationA, 'Bearer token-a'],
  ]);
  assert.equal(page.events().includes('session-delete-failed'), true);
  assert.equal(page.events().includes('session-lifecycle-error'), false);
  assert.equal(page.states().at(-1), 'IDLE');
  assertNoNewRejection(baseline);

  page.setFailDeletes(false);
  page.dispatch({ action: 'start', webrtcUrl: 'https://media.example/b', accessToken: 'token-b' });
  const posts = await waitForPosts(page, 2);
  const postB = posts.find((entry) => entry.url === 'https://media.example/b/whep');
  assert.equal(postB.authorization, 'Bearer token-b');
  postB.finish({ status: 201, location: locationB, sdp: 'v=0\r\nanswer-b\r\n' });
  await settle();
  const peerB = page.peers.at(-1);
  peerB.connectionState = 'connected';
  peerB.onconnectionstatechange();
  await settle();
  assert.equal(page.states().at(-1), 'LIVE');
  assert.equal(page.events().filter((event) => event === 'session-delete-failed').length, 1);

  page.dispatch({ action: 'stop' });
  await settle();
  assert.deepEqual(page.deletes().map((entry) => [entry.url, entry.authorization]), [
    [locationA, 'Bearer token-a'],
    [locationB, 'Bearer token-b'],
  ]);
  assert.equal(page.states().at(-1), 'IDLE');
  assert.equal(page.events().filter((event) => event === 'session-delete-failed').length, 1);
  assert.equal(page.events().includes('session-lifecycle-error'), false);
  assertNoNewRejection(baseline);
});

test('a failed attempt reports FAILED and does not leak a session', async () => {
  const baseline = rejectionBaseline();
  const active = loadPage();
  const location = 'https://media.example/cam/whep/session-bad';
  active.dispatch({ action: 'start', webrtcUrl: 'https://media.example/cam', accessToken: 'token-bad' });
  const posts = await waitForPosts(active, 1);
  active.peers[0].failRemote = true;
  posts[0].finish({ status: 201, location, sdp: 'v=0\r\nnot-an-answer\r\n' });
  await settle();
  assert.equal(active.states().includes('FAILED'), true);
  assert.equal(active.states().includes('LIVE'), false);
  assert.deepEqual(active.deletes().map((entry) => [entry.url, entry.authorization]), [
    [location, 'Bearer token-bad'],
  ]);
  assert.equal(active.video.srcObject, null);
  assert.equal(active.peers[0].connectionState, 'closed');
  active.dispatch({ action: 'stop' });
  await settle();
  assert.equal(active.deletes().length, 1);
  assert.equal(active.events().includes('session-lifecycle-error'), false);
  assertNoNewRejection(baseline);

  const stale = loadPage();
  const staleLocation = 'https://media.example/cam/whep/session-stale-fail';
  stale.dispatch({ action: 'start', webrtcUrl: 'https://media.example/cam', accessToken: 'token-stale' });
  const stalePosts = await waitForPosts(stale, 1);
  stale.dispatch({ action: 'stop' });
  await settle();
  stalePosts[0].finish({
    status: 201,
    location: staleLocation,
    sdp: 'v=0\r\nignored\r\n',
    textError: true,
  });
  await settle();
  stale.peers[0].ontrack?.({ streams: [{ id: 'stale-fail' }] });
  stale.peers[0].connectionState = 'connected';
  stale.peers[0].onconnectionstatechange?.();
  await settle();
  assert.deepEqual(stale.deletes().map((entry) => [entry.url, entry.authorization, entry.keepalive]), [
    [staleLocation, 'Bearer token-stale', true],
  ]);
  assert.equal(stale.states().includes('FAILED'), false);
  assert.equal(stale.states().includes('LIVE'), false);
  assert.equal(stale.video.srcObject, null);
  assert.equal(stale.events().includes('session-lifecycle-error'), false);

  const foreign = loadPage();
  foreign.dispatch({ action: 'start', webrtcUrl: 'https://media.example/cam', accessToken: 'token-foreign' });
  const foreignPosts = await waitForPosts(foreign, 1);
  foreignPosts[0].finish({ status: 201, location: 'https://evil.example/steal', sdp: 'v=0\r\nnope\r\n' });
  await settle();
  assert.equal(foreign.states().includes('FAILED'), true);
  assert.equal(foreign.states().includes('LIVE'), false);
  assert.equal(foreign.deletes().length, 0);
  assert.equal(foreign.events().includes('session-lifecycle-error'), false);
  assertNoNewRejection(baseline);
});
