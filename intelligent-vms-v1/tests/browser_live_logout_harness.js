/**
 * Execute the real web/index.html script against an in-memory DOM.
 *
 * This is a behavior harness: it does not assert on source text. The page
 * script runs unchanged, then a small inspector reads the live closures.
 * Node has no DOM, so the harness supplies only the document surface the
 * page touches. No third-party browser dependency is required.
 */
"use strict";

const fs = require("fs");
const vm = require("vm");

const SESSION_URL = "https://media.example/whep/sessions/pending-1";
const RESTARTED_SESSION_URL = "https://media.example/whep/sessions/restarted-2";
const SDP_ANSWER = "v=0\r\n";
const SCENARIOS = new Set([
  "deferred-post-sign-out",
  "deferred-sdp-sign-out",
  "deferred-answer-sign-out",
  "deferred-post-auth-expiry",
  "authenticated-post-stays-live",
  "retry-overlap-logout-rejected",
  "retry-overlap-logout-401",
  "sign-out-during-sdp-body",
  "relogin-before-old-post",
  "layout-shrink-logout-rejected",
  "relogin-cameras-held-logout-rejected",
  "auth-epoch-held-steps",
  "sign-out-overlaps-login",
  "pagehide-during-layout",
  "stale-health-401-after-login",
  "stale-cameras-401-after-login",
  "stale-cameras-success-after-login",
  "stale-panels-after-login",
  "stale-policy-after-pagehide",
]);

const harness = {
  requests: [],
  unknown: [],
  mediaDeletes: [],
  peers: [],
  alerts: [],
  rejections: [],
  intervals: [],
  accessRole: null,
  whepPostCount: 0,
  reachedHold: false,
  releaseHold: null,
  failNextHealth: false,
  logoutMode: null,
  inflight: 0,
  holds: [],
  holdTarget: 1,
  holdCameras: false,
  holdLogin: false,
  holdLayoutDelete: false,
  holdHealth: false,
  healthStatus: 200,
  cameraHoldMode: "",
  holdKeys: {},
  holdResults: {},
  createdSessions: [],
  pageListeners: {},
};

function decodeEntities(text) {
  return text.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (all, entity) => {
    const named = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
    if (Object.prototype.hasOwnProperty.call(named, entity)) return named[entity];
    if (entity[0] !== "#") return all;
    const code = entity[1] === "x" || entity[1] === "X"
      ? parseInt(entity.slice(2), 16)
      : parseInt(entity.slice(1), 10);
    return Number.isFinite(code) ? String.fromCodePoint(code) : all;
  });
}

function parseStyle(value) {
  const style = {};
  for (const part of String(value).split(";")) {
    const splitAt = part.indexOf(":");
    if (splitAt === -1) continue;
    const key = part.slice(0, splitAt).trim().replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    if (key) style[key] = part.slice(splitAt + 1).trim();
  }
  return style;
}

function createClassList(element) {
  const read = () => String(element.className || "").split(/\s+/).filter(Boolean);
  const write = (names) => {
    element.className = names.join(" ");
  };
  return {
    add(...names) {
      const present = new Set(read());
      for (const name of names) present.add(name);
      write([...present]);
    },
    remove(...names) {
      const drop = new Set(names);
      write(read().filter((name) => !drop.has(name)));
    },
    toggle(name, force) {
      const present = new Set(read());
      const enabled = force === undefined ? !present.has(name) : Boolean(force);
      if (enabled) present.add(name);
      else present.delete(name);
      write([...present]);
      return enabled;
    },
    contains(name) {
      return read().includes(name);
    },
  };
}

class DomElement {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.parent = null;
    this.id = "";
    this.className = "";
    this.classList = createClassList(this);
    this.style = {};
    this.attrs = {};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this._value = "";
    this._text = null;
    this.srcObject = null;
    this.readyState = 0;
    this.videoWidth = 0;
    this.videoHeight = 0;
    this.rel = "";
    this.href = "";
    this.download = "";
  }

  get value() {
    return this._value;
  }

  set value(next) {
    this._value = next == null ? "" : String(next);
  }

  get textContent() {
    if (this.children.length) {
      return this.children.map((child) => child.textContent || "").join("");
    }
    return this._text == null ? "" : this._text;
  }

  set textContent(next) {
    this._text = next == null ? "" : String(next);
    this.children = [];
  }

  set innerHTML(html) {
    const children = parseFragment(String(html));
    this.children = [];
    for (const child of children) {
      child.parent = this;
      this.children.push(child);
    }
    this._text = null;
  }

  replaceChildren(...nodes) {
    this.children = [];
    for (const node of nodes) {
      if (node == null) continue;
      node.parent = this;
      this.children.push(node);
    }
    this._text = null;
  }

  setAttribute(name, value) {
    this.attrs[String(name).toLowerCase()] = String(value);
  }

  getAttribute(name) {
    const key = String(name).toLowerCase();
    return Object.prototype.hasOwnProperty.call(this.attrs, key) ? this.attrs[key] : null;
  }

  removeAttribute(name) {
    delete this.attrs[String(name).toLowerCase()];
  }

  addEventListener() {}

  removeEventListener() {}

  click() {}

  pause() {}

  load() {}

  play() {}

  focus() {}

  scrollIntoView() {}
}

function createElement(tag) {
  return new DomElement(tag);
}

function applyAttributes(element, raw) {
  const pattern = /([^\s=\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
  let match = pattern.exec(raw);
  while (match) {
    const name = match[1].toLowerCase();
    const hasValue = match[2] !== undefined || match[3] !== undefined || match[4] !== undefined;
    const value = decodeEntities(match[2] ?? match[3] ?? match[4] ?? "");
    if (name === "id") element.id = value;
    else if (name === "class") element.className = value;
    else if (name === "style") Object.assign(element.style, parseStyle(value));
    else if (name === "value") element.value = value;
    else if (name === "hidden") element.hidden = true;
    else if (name === "disabled") element.disabled = true;
    else if (name === "checked") element.checked = true;
    else if (hasValue) element.attrs[name] = value;
    else element.attrs[name] = "";
    match = pattern.exec(raw);
  }
}

function parseFragment(html) {
  const root = createElement("fragment");
  const stack = [root];
  const voidTags = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"]);
  let index = 0;
  while (index < html.length) {
    if (html.startsWith("<!--", index)) {
      const end = html.indexOf("-->", index + 4);
      index = end === -1 ? html.length : end + 3;
      continue;
    }
    if (html[index] === "<") {
      if (html[index + 1] === "/") {
        const end = html.indexOf(">", index);
        const name = html.slice(index + 2, end === -1 ? html.length : end).trim().toLowerCase();
        index = end === -1 ? html.length : end + 1;
        while (stack.length > 1 && stack[stack.length - 1].tagName.toLowerCase() !== name) stack.pop();
        if (stack.length > 1) stack.pop();
        continue;
      }
      const end = html.indexOf(">", index);
      let raw = html.slice(index + 1, end === -1 ? html.length : end).trim();
      index = end === -1 ? html.length : end + 1;
      let selfClosing = false;
      if (raw.endsWith("/")) {
        selfClosing = true;
        raw = raw.slice(0, -1).trim();
      }
      const nameMatch = /^([A-Za-z][\w:-]*)/.exec(raw);
      if (!nameMatch) continue;
      const name = nameMatch[1].toLowerCase();
      const element = createElement(name);
      applyAttributes(element, raw.slice(nameMatch[1].length));
      stack[stack.length - 1].children.push(element);
      element.parent = stack[stack.length - 1];
      if (!selfClosing && !voidTags.has(name)) stack.push(element);
      continue;
    }
    const next = html.indexOf("<", index);
    const text = html.slice(index, next === -1 ? html.length : next);
    index = next === -1 ? html.length : next;
    if (!text.trim()) continue;
    const node = createElement("#text");
    node.nodeType = 3;
    node.textContent = decodeEntities(text);
    stack[stack.length - 1].children.push(node);
  }
  return root.children;
}

function walk(element, visit) {
  if (!element || element.nodeType === 3) return;
  if (visit(element)) return;
  for (const child of element.children) walk(child, visit);
}

function findById(root, id) {
  let found = null;
  walk(root, (element) => {
    if (element.id === id) {
      found = element;
      return true;
    }
    return false;
  });
  return found;
}

function findTag(root, tagName) {
  let found = null;
  walk(root, (element) => {
    if (element.tagName === tagName) {
      found = element;
      return true;
    }
    return false;
  });
  return found;
}

function buildDocument(html) {
  const withoutExecutable = html
    .replace(/<script>[\s\S]*?<\/script>/gi, "")
    .replace(/<style>[\s\S]*?<\/style>/gi, "");
  const top = parseFragment(withoutExecutable);
  const documentElement = top.find((element) => element.tagName === "HTML") || top[0];
  const body = findTag(documentElement, "BODY");
  const document = {
    documentElement,
    body,
    cookie: "",
    activeElement: null,
    getElementById(id) {
      return findById(documentElement, id);
    },
    createElement(tag) {
      return createElement(tag);
    },
    querySelectorAll(selector) {
      if (selector !== "#timeline .span") return [];
      const timeline = findById(documentElement, "timeline");
      const matches = [];
      if (timeline) {
        walk(timeline, (element) => {
          if (element.classList.contains("span")) matches.push(element);
          return false;
        });
      }
      return matches;
    },
  };
  return document;
}

function DomOption(text, value) {
  const option = createElement("option");
  option.textContent = text == null ? "" : String(text);
  if (value !== undefined) option.value = String(value);
  return option;
}

function headerValue(headers, name) {
  if (!headers) return "";
  if (typeof headers.get === "function") {
    const value = headers.get(name);
    return value == null ? "" : String(value);
  }
  const key = Object.keys(headers).find((item) => item.toLowerCase() === name.toLowerCase());
  return key ? String(headers[key]) : "";
}

function pathnameOf(href) {
  return new URL(href, "http://vms.local").pathname;
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function whepResponse(deferBody, location = SESSION_URL) {
  harness.createdSessions.push(location);
  const headers = new Headers({
    Location: location,
    "Content-Type": "application/sdp",
  });
  const bodyPromise = deferBody
    ? new Promise((resolve) => {
      harness.releaseHold = resolve;
    })
    : Promise.resolve(SDP_ANSWER);
  return {
    status: 201,
    ok: true,
    headers,
    text() {
      if (deferBody) harness.reachedHold = true;
      return bodyPromise.then((value) => String(value));
    },
    json() {
      return this.text().then((text) => JSON.parse(text));
    },
  };
}

function isWhep(href) {
  return pathnameOf(href).endsWith("/whep");
}

function sessionLocation(scenario) {
  const numbered = new Set([
    "layout-shrink-logout-rejected",
    "relogin-cameras-held-logout-rejected",
    "auth-epoch-held-steps",
    "sign-out-overlaps-login",
    "pagehide-during-layout",
    "stale-health-401-after-login",
    "stale-cameras-401-after-login",
    "stale-cameras-success-after-login",
    "stale-panels-after-login",
    "stale-policy-after-pagehide",
  ]);
  if (numbered.has(scenario)) return `https://media.example/whep/sessions/s-${harness.whepPostCount}`;
  return SESSION_URL;
}

function cameraList() {
  return [
    { id: "cam-1", name: "Gate", site_id: "site-01", tenant_id: "tenant-a", available_live_roles: ["main"] },
    { id: "cam-2", name: "Dock", site_id: "site-01", tenant_id: "tenant-a", available_live_roles: ["main"] },
  ];
}

function holdUntilRelease(build) {
  return new Promise((resolve) => {
    harness.holds.push(() => resolve(build()));
    if (harness.holds.length >= harness.holdTarget) harness.reachedHold = true;
  });
}

function releaseHolds() {
  const pending = harness.holds.splice(0);
  for (const release of pending) release();
}

function consumeHold(key, fallback) {
  if (!harness.holdKeys[key]) return fallback();
  delete harness.holdKeys[key];
  return holdUntilRelease(() => {
    const spec = harness.holdResults[key];
    if (!spec) return fallback();
    if (Object.prototype.hasOwnProperty.call(spec, "status")) {
      return new Response(spec.text == null ? "" : String(spec.text), { status: spec.status });
    }
    return jsonResponse(spec.json);
  });
}

function route(method, href, scenario) {
  const pathname = pathnameOf(href);
  if (method === "OPTIONS" && isWhep(href)) return new Response("", { status: 200 });
  if (method === "POST" && isWhep(href)) {
    harness.whepPostCount += 1;
    const holdFirstPost = scenario === "deferred-post-sign-out"
      || scenario === "deferred-post-auth-expiry"
      || scenario === "authenticated-post-stays-live"
      || (scenario === "relogin-before-old-post" && harness.whepPostCount === 1);
    if (holdFirstPost) {
      harness.reachedHold = true;
      return new Promise((resolve) => {
        harness.releaseHold = () => resolve(whepResponse(false, SESSION_URL));
      });
    }
    if (scenario === "relogin-before-old-post") return whepResponse(false, RESTARTED_SESSION_URL);
    if (scenario === "deferred-sdp-sign-out" || scenario === "sign-out-during-sdp-body") return whepResponse(true);
    return whepResponse(false, sessionLocation(scenario));
  }
  if (method === "DELETE" && href.startsWith("https://media.example/")) {
    if (harness.holdLayoutDelete && harness.mediaDeletes.length === 1) {
      harness.holdLayoutDelete = false;
      return holdUntilRelease(() => new Response(null, { status: 200 }));
    }
    const holdCleanupDelete = scenario.startsWith("retry-overlap-") && harness.mediaDeletes.length === 1;
    if (holdCleanupDelete) {
      harness.reachedHold = true;
      return new Promise((resolve) => {
        harness.releaseHold = () => resolve(new Response(null, { status: 200 }));
      });
    }
    return new Response(null, { status: 200 });
  }
  if (pathname === "/api/v1/auth/session" && method === "GET") {
    return jsonResponse({
      tenant_id: "tenant-a",
      roles: ["operator"],
      browser_session_enabled: true,
    });
  }
  if (pathname === "/api/v1/auth/session" && method === "POST") {
    const body = () => jsonResponse({
      tenant_id: "tenant-a",
      roles: ["operator"],
      browser_session_enabled: true,
    });
    if (harness.holdLogin) {
      harness.holdLogin = false;
      return holdUntilRelease(body);
    }
    return body();
  }
  if (pathname === "/api/v1/auth/session" && method === "DELETE") {
    if (harness.logoutMode === "reject") throw new Error("logout failed");
    if (harness.logoutMode === "401") return new Response("unauthorized", { status: 401 });
    return new Response(null, { status: 204 });
  }
  if (pathname === "/api/v1/system/capabilities") {
    return consumeHold("capabilities", () => jsonResponse({
      deployment_profile: "enterprise-distributed",
      event_history: true,
      alarm_processing: true,
      ai_ui: true,
      distributed_placement: false,
    }));
  }
  if (pathname === "/api/v1/system/health") {
    if (harness.holdHealth) {
      harness.holdHealth = false;
      return holdUntilRelease(() => (
        harness.healthStatus === 401
          ? new Response("expired", { status: 401 })
          : jsonResponse({ status: "ok", media_node: "media-local-01" })
      ));
    }
    if (harness.failNextHealth) {
      harness.failNextHealth = false;
      return new Response("expired", { status: 401 });
    }
    return jsonResponse({ status: "ok", media_node: "media-local-01" });
  }
  if (pathname === "/api/v1/health/summary") {
    return jsonResponse({
      online: 1,
      degraded: 0,
      offline: 0,
      unknown: 0,
      monitor_last_scanned: 1,
      monitor_last_changed: 1,
      monitor_media_errors: 0,
    });
  }
  if (pathname === "/api/v1/health/cameras") return jsonResponse([]);
  if (pathname === "/api/v1/cameras" && method === "GET") {
    if (harness.holdCameras) {
      harness.holdCameras = false;
      return holdUntilRelease(() => {
        if (harness.cameraHoldMode === "401") return new Response("expired", { status: 401 });
        if (harness.cameraHoldMode === "stale") {
          return jsonResponse([{
            id: "cam-1",
            name: "Stale Yard",
            site_id: "site-01",
            tenant_id: "tenant-a",
            available_live_roles: ["main"],
          }]);
        }
        return jsonResponse(cameraList());
      });
    }
    return jsonResponse(cameraList());
  }
  if (pathname === "/api/v1/events") return consumeHold("events", () => jsonResponse([]));
  if (pathname === "/api/v1/alarms" && method === "GET") return consumeHold("alarms", () => jsonResponse([]));
  if (method === "POST" && pathname.startsWith("/api/v1/alarms/") && pathname.endsWith("/acknowledge")) {
    return consumeHold("ack", () => new Response(null, { status: 204 }));
  }
  if (pathname === "/api/v1/ai/status") {
    return consumeHold("ai", () => jsonResponse({ enabled_policies: 0, inference_policies: 0 }));
  }
  if (pathname === "/api/v1/ai/models") return consumeHold("ai-models", () => jsonResponse([]));
  if (pathname.startsWith("/api/v1/ai/cameras/") && pathname.endsWith("/policy") && method === "GET") {
    return jsonResponse({ enabled: false, source_mode: "inference", model_id: null, stream_role: "sub", sample_fps: 1, min_confidence: 0.5, analytics: [], zones: [], provider_config: {} });
  }
  if (pathname.startsWith("/api/v1/diagnostics/cameras/")) {
    return consumeHold("diagnostics", () => jsonResponse({
      path_state: "ready",
      inbound_mbps: 1,
      rtp_packets_lost: 0,
      rtp_packets_in_error: 0,
      rtp_jitter: 1,
    }));
  }
  if (pathname === "/api/v1/manual-recordings/active") return consumeHold("manual", () => jsonResponse([]));
  if (method === "POST" && pathname.includes("/manual-recordings/cameras/") && pathname.endsWith("/start")) {
    return consumeHold("manual-start", () => jsonResponse({
      id: "m-new",
      camera_id: "cam-1",
      state: "ACTIVE",
      started_at: "2026-10-08T00:00:00.000Z",
    }));
  }
  if (pathname === "/api/v1/manual-recordings/recent") return jsonResponse([]);
  if (pathname.startsWith("/api/v1/recordings/cameras/") && pathname.endsWith("/policy") && method === "GET") {
    return consumeHold("policy", () => new Response("not found", { status: 404 }));
  }
  if (method === "POST" && pathname.includes("/live/cameras/") && pathname.endsWith("/access")) {
    harness.accessRole = new URL(href, "http://vms.local").searchParams.get("stream_role");
    return jsonResponse({
      access_token: "live-grant-token",
      webrtc_url: "https://media.example/live/cam",
    });
  }
  harness.unknown.push(`${method} ${href}`);
  return new Response("not found", { status: 404 });
}

function installPeerConnection(scenario) {
  class FakePeerConnection {
    constructor() {
      this.iceGatheringState = "complete";
      this.connectionState = "new";
      this.localDescription = null;
      this.remoteDescription = null;
      this.ontrack = null;
      this.onconnectionstatechange = null;
      this.closed = false;
      this.remoteDescriptionApplied = false;
      this._listeners = {};
      harness.peers.push(this);
    }

    addTransceiver() {}

    addEventListener(type, listener) {
      const listeners = this._listeners[type] || [];
      listeners.push(listener);
      this._listeners[type] = listeners;
    }

    removeEventListener(type, listener) {
      this._listeners[type] = (this._listeners[type] || []).filter((item) => item !== listener);
    }

    createOffer() {
      return Promise.resolve({ type: "offer", sdp: "v=0\r\n" });
    }

    setLocalDescription(description) {
      this.localDescription = description;
      return Promise.resolve();
    }

    setRemoteDescription(description) {
      const apply = () => {
        this.remoteDescription = description;
        this.remoteDescriptionApplied = true;
      };
      if (scenario === "deferred-answer-sign-out") {
        harness.reachedHold = true;
        return new Promise((resolve) => {
          harness.releaseHold = () => {
            apply();
            resolve();
          };
        });
      }
      apply();
      return Promise.resolve();
    }

    close() {
      this.closed = true;
      this.connectionState = "closed";
    }
  }
  return FakePeerConnection;
}

function pageFetch(scenario) {
  return async function fetch(url, opts = {}) {
    harness.inflight += 1;
    const method = String(opts.method || "GET").toUpperCase();
    const href = String(url);
    const authorization = headerValue(opts.headers, "authorization");
    harness.requests.push({ method, url: href, authorization });
    if (method === "DELETE" && href.startsWith("https://media.example/")) {
      harness.mediaDeletes.push({ url: href, authorization });
    }
    try {
      return await route(method, href, scenario);
    } finally {
      harness.inflight -= 1;
    }
  };
}

async function waitFor(predicate, label) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
  }
  const detail = {
    label,
    inflight: harness.inflight,
    reachedHold: harness.reachedHold,
    whepPostCount: harness.whepPostCount,
    unknown: harness.unknown,
    requests: harness.requests.map((item) => `${item.method} ${item.url}`),
    rejections: harness.rejections,
    peers: harness.peers.length,
  };
  throw new Error(`timed out waiting for ${label}: ${JSON.stringify(detail)}`);
}

function extractScript(html) {
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (scripts.length !== 1) {
    throw new Error(`expected one inline script in web/index.html, found ${scripts.length}`);
  }
  return scripts[0][1];
}

async function runScenario(scenario, htmlPath) {
  if (!SCENARIOS.has(scenario)) throw new Error(`unknown scenario ${scenario}`);
  const html = fs.readFileSync(htmlPath, "utf8");
  const script = extractScript(html);
  const document = buildDocument(html);
  const requiredIds = [
    "authPanel", "authMessage", "signOut", "identity", "system", "content", "camExisting",
    "events", "alarms", "aiSummary", "healthSummary", "recordingRetention", "recordingEnable",
    "recordingDisable", "recordingSaveRetention", "recordingStatus", "manualStart", "manualStop",
    "manualDownload", "manualStatus", "player", "pbPlay", "pbPause", "pbexport", "pbClipStart",
    "pbClipEnd", "timeline", "playback", "cameraSetup", "camPassword",
  ];
  const missing = requiredIds.filter((id) => !document.getElementById(id));
  if (!document.body || missing.length) {
    throw new Error(`DOM parse missed ids: ${missing.join(",")}`);
  }

  const context = vm.createContext({
    console: {
      log: (...args) => console.error("[page]", ...args),
      error: (...args) => console.error("[page]", ...args),
      warn: (...args) => console.error("[page]", ...args),
      info() {},
      debug() {},
    },
    URL,
    URLSearchParams,
    Headers,
    Response,
    Request,
    setTimeout,
    clearTimeout,
    setInterval: (fn, delay) => {
      harness.intervals.push(delay);
      return harness.intervals.length;
    },
    clearInterval() {},
    queueMicrotask,
    document,
    AbortController,
    window: {
      addEventListener(type, listener) {
        const list = harness.pageListeners[type] || [];
        list.push(listener);
        harness.pageListeners[type] = list;
      },
      removeEventListener(type, listener) {
        harness.pageListeners[type] = (harness.pageListeners[type] || []).filter((item) => item !== listener);
      },
    },
    RTCPeerConnection: installPeerConnection(scenario),
    Option: DomOption,
    alert(message) {
      harness.alerts.push(String(message));
    },
    fetch: pageFetch(scenario),
  });

  const source = `${script}\nglobalThis.__vms = {\n  signOut,\n  assignCamera,\n  setLayout,\n  refreshCameras,\n  refreshHealth,\n  refreshEvents,\n  refreshAlarms,\n  refreshAIStatus,\n  refreshCapabilities,\n  refreshManualRecordings,\n  refreshRecordingControls,\n  showDiagnostics,\n  openAI,\n  ackAlarm,\n  startManualRecording,\n  retryTile,\n  loginWithToken,\n  inspect() {\n    return {\n      authenticated,\n      liveSessionCount: liveSessions.size,\n    };\n  },\n};\n`;
  vm.runInContext(source, context, { filename: htmlPath });
  await waitFor(
    () => harness.inflight === 0 && context.__vms.inspect().authenticated === true,
    "authenticated idle",
  );

  if (scenario.startsWith("retry-overlap-")) {
    return runRetryOverlap(scenario, context, document, htmlPath, script.length);
  }
  if (scenario === "sign-out-during-sdp-body") {
    return runSignOutDuringSdpBody(context, document, htmlPath, script.length);
  }
  if (scenario === "relogin-before-old-post") {
    return runReloginBeforeOldPost(context, document, htmlPath, script.length);
  }
  if (scenario === "layout-shrink-logout-rejected") {
    return runLayoutShrinkLogout(context, document, htmlPath, script.length);
  }
  if (scenario === "relogin-cameras-held-logout-rejected") {
    return runReloginCamerasHeld(context, document, htmlPath, script.length);
  }
  if (scenario === "auth-epoch-held-steps") {
    return runAuthEpochHeldSteps(context, document, htmlPath, script.length);
  }
  if (scenario === "sign-out-overlaps-login") {
    return runSignOutOverlapsLogin(context, document, htmlPath, script.length);
  }
  if (scenario === "pagehide-during-layout") {
    return runPagehideDuringLayout(context, document, htmlPath, script.length);
  }
  if (scenario === "stale-health-401-after-login") {
    return runStaleHealth401(context, document, htmlPath, script.length);
  }
  if (scenario === "stale-cameras-401-after-login") {
    return runStaleCameras401(context, document, htmlPath, script.length);
  }
  if (scenario === "stale-cameras-success-after-login") {
    return runStaleCamerasSuccess(context, document, htmlPath, script.length);
  }
  if (scenario === "stale-panels-after-login") {
    return runStalePanels(context, document, htmlPath, script.length);
  }
  if (scenario === "stale-policy-after-pagehide") {
    return runStalePolicyAfterPagehide(context, document, htmlPath, script.length);
  }

  const assigned = context.__vms.assignCamera(0, "cam-1");
  await assigned;
  await waitFor(() => harness.reachedHold, "in-flight WHEP hold");

  if (scenario.endsWith("auth-expiry")) {
    harness.failNextHealth = true;
    await context.__vms.refreshHealth();
  } else if (scenario !== "authenticated-post-stays-live") {
    await context.__vms.signOut();
  }

  if (scenario.startsWith("deferred-post") || scenario === "authenticated-post-stays-live") harness.releaseHold(whepResponse(false));
  else if (scenario.startsWith("deferred-sdp")) harness.releaseHold(SDP_ANSWER);
  else harness.releaseHold();

  await waitFor(() => {
    const state = context.__vms.inspect();
    const peer = harness.peers[0];
    return state.liveSessionCount > 0 || harness.mediaDeletes.length > 0 || Boolean(peer && peer.closed);
  }, "late WHEP settlement");
  await new Promise((resolve) => {
    setImmediate(resolve);
  });

  return observe(scenario, context, document, htmlPath, script.length);
}

function observe(scenario, context, document, htmlPath, scriptBytes) {
  const inspected = context.__vms.inspect();
  const tile = document.getElementById("tile-state-0");
  const panel = document.getElementById("authPanel");
  const peer = harness.peers[0] || null;
  return {
    scenario,
    page_path: htmlPath,
    script_bytes: scriptBytes,
    authenticated: inspected.authenticated,
    auth_required: document.body.classList.contains("auth-required"),
    auth_panel_display: panel ? panel.style.display : null,
    live_session_count: inspected.liveSessionCount,
    tile_state: tile ? tile.textContent : null,
    access_role: harness.accessRole,
    whep_post_count: harness.whepPostCount,
    media_deletes: harness.mediaDeletes.map((item) => ({
      url: item.url,
      authorization: item.authorization,
    })),
    peer_count: harness.peers.length,
    open_peer_count: harness.peers.filter((item) => !item.closed).length,
    peer_closed: Boolean(peer && peer.closed),
    remote_description_applied: Boolean(peer && peer.remoteDescriptionApplied),
    unknown_requests: harness.unknown,
    tile_states: tileStates(document),
    whep_sessions: harness.createdSessions.slice(),
    auth_message: (document.getElementById("authMessage") || {}).textContent || "",
    content_text: (document.getElementById("content") || {}).textContent || "",
    event_text: (document.getElementById("eventList") || {}).textContent || "",
    alarm_text: (document.getElementById("alarmList") || {}).textContent || "",
    ai_text: (document.getElementById("aiSummary") || {}).textContent || "",
    ai_message: (document.getElementById("aiMessage") || {}).textContent || "",
    diag_text: (document.getElementById("diagBody") || {}).textContent || "",
    manual_status: (document.getElementById("manualStatus") || {}).textContent || "",
    recording_status: (document.getElementById("recordingStatus") || {}).textContent || "",
    events_display: (document.getElementById("events") || { style: {} }).style.display || "",
    alarms_display: (document.getElementById("alarms") || { style: {} }).style.display || "",
    ai_display: (document.getElementById("aiSummary") || { style: {} }).style.display || "",
    alerts: harness.alerts.slice(),
    auth_requests: harness.requests
      .filter((item) => item.url.includes("/api/v1/auth/session"))
      .map((item) => `${item.method} ${item.url}`),
  };
}

function dispatchPageHide() {
  for (const listener of harness.pageListeners.pagehide || []) listener();
}

function tileStates(document) {
  const states = [];
  for (let index = 0; index < 16; index += 1) {
    const tile = document.getElementById(`tile-state-${index}`);
    if (!tile) break;
    states.push(tile.textContent);
  }
  return states;
}

async function settleSignedOut(context) {
  let idleTurns = 0;
  await waitFor(() => {
    const openPeer = harness.peers.some((peer) => !peer.closed);
    if (context.__vms.inspect().liveSessionCount > 0 || openPeer) return true;
    if (harness.inflight === 0) idleTurns += 1;
    else idleTurns = 0;
    return idleTurns >= 10;
  }, "signed-out settlement");
}

async function establishLiveTiles(context, count) {
  const cameras = ["cam-1", "cam-2"];
  for (let index = 0; index < count; index += 1) {
    await context.__vms.assignCamera(index, cameras[index]);
  }
  await waitFor(() => context.__vms.inspect().liveSessionCount === count, `${count} live tiles`);
}

async function runLayoutShrinkLogout(context, document, htmlPath, scriptBytes) {
  await establishLiveTiles(context, 2);
  harness.holdLayoutDelete = true;
  harness.holdTarget = 1;
  harness.holds = [];
  harness.reachedHold = false;
  const layout = context.__vms.setLayout(1);
  await waitFor(() => harness.reachedHold, "removed tile DELETE hold");
  harness.logoutMode = "reject";
  await context.__vms.signOut();
  releaseHolds();
  await layout;
  await settleSignedOut(context);
  return observe("layout-shrink-logout-rejected", context, document, htmlPath, scriptBytes);
}

async function runReloginCamerasHeld(context, document, htmlPath, scriptBytes) {
  await establishLiveTiles(context, 1);
  await context.__vms.signOut();
  harness.holdCameras = true;
  harness.holdTarget = 1;
  harness.holds = [];
  harness.reachedHold = false;
  document.getElementById("authToken").value = "field-token";
  const login = context.__vms.loginWithToken({ preventDefault() {} });
  await waitFor(() => harness.reachedHold, "camera list hold");
  harness.logoutMode = "reject";
  await context.__vms.signOut();
  releaseHolds();
  await login;
  await settleSignedOut(context);
  return observe("relogin-cameras-held-logout-rejected", context, document, htmlPath, scriptBytes);
}

async function runSignOutOverlapsLogin(context, document, htmlPath, scriptBytes) {
  await establishLiveTiles(context, 1);
  harness.holdLayoutDelete = true;
  harness.holdTarget = 1;
  harness.holds = [];
  harness.reachedHold = false;
  const signingOut = context.__vms.signOut();
  await waitFor(() => harness.reachedHold, "sign-out WHEP DELETE hold");
  document.getElementById("authToken").value = "field-token";
  await context.__vms.loginWithToken({ preventDefault() {} });
  await waitFor(() => harness.whepPostCount >= 2 && context.__vms.inspect().liveSessionCount === 1, "replacement session");
  releaseHolds();
  await signingOut;
  await new Promise((resolve) => { setImmediate(resolve); });
  return observe("sign-out-overlaps-login", context, document, htmlPath, scriptBytes);
}

async function runPagehideDuringLayout(context, document, htmlPath, scriptBytes) {
  await establishLiveTiles(context, 2);
  harness.holdLayoutDelete = true;
  harness.holdTarget = 1;
  harness.holds = [];
  harness.reachedHold = false;
  const layout = context.__vms.setLayout(1);
  await waitFor(() => harness.reachedHold, "removed tile DELETE hold");
  dispatchPageHide();
  const during = observe("pagehide-during-layout", context, document, htmlPath, scriptBytes);
  releaseHolds();
  await layout;
  await settleSignedOut(context);
  const finalState = observe("pagehide-during-layout", context, document, htmlPath, scriptBytes);
  finalState.during_pagehide = {
    tile_states: during.tile_states,
    live_session_count: during.live_session_count,
    open_peer_count: during.open_peer_count,
    media_deletes: during.media_deletes,
  };
  return finalState;
}

async function reloginLive(context, document) {
  document.getElementById("authToken").value = "field-token";
  await context.__vms.loginWithToken({ preventDefault() {} });
  await waitFor(
    () => context.__vms.inspect().authenticated === true && context.__vms.inspect().liveSessionCount === 1,
    "relogin live",
  );
}

async function runStaleCameras401(context, document, htmlPath, scriptBytes) {
  await establishLiveTiles(context, 1);
  harness.holdCameras = true;
  harness.holdTarget = 1;
  harness.holds = [];
  harness.reachedHold = false;
  const refresh = context.__vms.refreshCameras();
  await waitFor(() => harness.reachedHold, "camera list hold");
  await context.__vms.signOut();
  await reloginLive(context, document);
  harness.cameraHoldMode = "401";
  releaseHolds();
  await refresh;
  await settleSignedOut(context);
  return observe("stale-cameras-401-after-login", context, document, htmlPath, scriptBytes);
}

async function runStaleCamerasSuccess(context, document, htmlPath, scriptBytes) {
  await establishLiveTiles(context, 1);
  harness.holdCameras = true;
  harness.holdTarget = 1;
  harness.holds = [];
  harness.reachedHold = false;
  const refresh = context.__vms.refreshCameras();
  await waitFor(() => harness.reachedHold, "camera list hold");
  await context.__vms.signOut();
  await reloginLive(context, document);
  harness.cameraHoldMode = "stale";
  releaseHolds();
  await refresh;
  await context.__vms.setLayout(4);
  await settleSignedOut(context);
  return observe("stale-cameras-success-after-login", context, document, htmlPath, scriptBytes);
}

async function runStalePanels(context, document, htmlPath, scriptBytes) {
  await establishLiveTiles(context, 1);
  await waitFor(() => harness.inflight === 0, "idle before stale panels");
  const keys = ["events", "alarms", "ai", "capabilities", "manual", "diagnostics", "policy", "ack", "ai-models", "manual-start"];
  harness.holdKeys = Object.fromEntries(keys.map((key) => [key, true]));
  harness.holdResults = {};
  harness.holdTarget = keys.length;
  harness.holds = [];
  harness.reachedHold = false;
  const pending = [
    context.__vms.refreshEvents(),
    context.__vms.refreshAlarms(),
    context.__vms.refreshAIStatus(),
    context.__vms.refreshCapabilities(),
    context.__vms.refreshManualRecordings(),
    context.__vms.showDiagnostics("cam-1"),
    context.__vms.refreshRecordingControls(),
    context.__vms.ackAlarm("alarm-1"),
    context.__vms.openAI("cam-1"),
    context.__vms.startManualRecording(),
  ];
  await waitFor(() => harness.holds.length >= keys.length, "stale panel holds");
  await context.__vms.signOut();
  await reloginLive(context, document);
  harness.holdResults = {
    events: { json: [{ timestamp: "2026-10-08T00:00:00Z", event_type: "STALE_EVENT", object_type: "person", zone_id: "", severity: "low" }] },
    alarms: { json: [{ id: "alarm-stale", opened_at: "2026-10-08T00:00:00Z", event_type: "motion", message: "STALE_ALARM", severity: "low" }] },
    ai: { json: { enabled_policies: 77, inference_policies: 88 } },
    capabilities: { json: { deployment_profile: "stale", event_history: false, alarm_processing: false, ai_ui: false, distributed_placement: false } },
    manual: { json: [{ id: "m-stale", camera_id: "cam-1", state: "ACTIVE", started_at: "2026-10-08T00:00:00.000Z" }] },
    diagnostics: { json: { path_state: "STALE_DIAG", inbound_mbps: 1, rtp_packets_lost: 0, rtp_packets_in_error: 0, rtp_jitter: 1 } },
    policy: { status: 500, text: "stale policy" },
    ack: { status: 401, text: "expired" },
    "ai-models": { status: 401, text: "expired" },
    "manual-start": { status: 401, text: "expired" },
  };
  releaseHolds();
  await Promise.all(pending);
  await settleSignedOut(context);
  return observe("stale-panels-after-login", context, document, htmlPath, scriptBytes);
}

async function runStalePolicyAfterPagehide(context, document, htmlPath, scriptBytes) {
  await establishLiveTiles(context, 1);
  await waitFor(() => harness.inflight === 0, "idle before policy hold");
  harness.holdKeys = { policy: true };
  harness.holdResults = {};
  harness.holdTarget = 1;
  harness.holds = [];
  harness.reachedHold = false;
  const pending = context.__vms.refreshRecordingControls();
  await waitFor(() => harness.holds.length >= 1, "recording policy hold");
  dispatchPageHide();
  harness.holdResults = {
    policy: {
      json: {
        enabled: true,
        mode: "continuous",
        retention_days: 9,
        part_duration_ms: 1000,
        segment_duration_seconds: 900,
        max_part_size_mb: 50,
      },
    },
  };
  releaseHolds();
  await pending;
  await settleSignedOut(context);
  return observe("stale-policy-after-pagehide", context, document, htmlPath, scriptBytes);
}

async function runStaleHealth401(context, document, htmlPath, scriptBytes) {
  await establishLiveTiles(context, 1);
  harness.holdHealth = true;
  harness.healthStatus = 401;
  harness.holdTarget = 1;
  harness.holds = [];
  harness.reachedHold = false;
  const health = context.__vms.refreshHealth();
  await waitFor(() => harness.reachedHold, "health request hold");
  await context.__vms.signOut();
  document.getElementById("authToken").value = "field-token";
  await context.__vms.loginWithToken({ preventDefault() {} });
  await waitFor(() => context.__vms.inspect().authenticated === true && context.__vms.inspect().liveSessionCount === 1, "relogin live");
  releaseHolds();
  await health;
  await settleSignedOut(context);
  return observe("stale-health-401-after-login", context, document, htmlPath, scriptBytes);
}

async function runAuthEpochHeldSteps(context, document, htmlPath, scriptBytes) {
  await establishLiveTiles(context, 2);
  harness.holdLayoutDelete = true;
  harness.holdCameras = true;
  harness.holdLogin = true;
  harness.holdTarget = 3;
  harness.holds = [];
  harness.reachedHold = false;
  const layout = context.__vms.setLayout(1);
  const refresh = context.__vms.refreshCameras();
  document.getElementById("authToken").value = "field-token";
  const login = context.__vms.loginWithToken({ preventDefault() {} });
  await waitFor(() => harness.holds.length >= 3, "layout, camera, and login holds");
  harness.logoutMode = "reject";
  await context.__vms.signOut();
  releaseHolds();
  await Promise.allSettled([layout, refresh, login]);
  await settleSignedOut(context);
  return observe("auth-epoch-held-steps", context, document, htmlPath, scriptBytes);
}

async function settleOverlap(context) {
  let idleTurns = 0;
  await waitFor(() => {
    if (context.__vms.inspect().liveSessionCount > 0) return true;
    if (harness.inflight === 0 && harness.whepPostCount === 1) idleTurns += 1;
    else idleTurns = 0;
    return idleTurns >= 5;
  }, "retry overlap settlement");
}

async function runRetryOverlap(scenario, context, document, htmlPath, scriptBytes) {
  await context.__vms.assignCamera(0, "cam-1");
  await waitFor(() => context.__vms.inspect().liveSessionCount === 1, "first live tile");
  context.__vms.retryTile(0);
  await waitFor(() => harness.reachedHold, "cleanup DELETE hold");
  harness.logoutMode = scenario.endsWith("401") ? "401" : "reject";
  await context.__vms.signOut();
  harness.releaseHold();
  await settleOverlap(context);
  return observe(scenario, context, document, htmlPath, scriptBytes);
}

async function runSignOutDuringSdpBody(context, document, htmlPath, scriptBytes) {
  await context.__vms.assignCamera(0, "cam-1");
  await waitFor(() => harness.reachedHold, "SDP body hold");
  await context.__vms.signOut();
  const during = observe("sign-out-during-sdp-body", context, document, htmlPath, scriptBytes);
  harness.releaseHold(SDP_ANSWER);
  await waitFor(() => {
    const state = context.__vms.inspect();
    const peer = harness.peers[0];
    return state.liveSessionCount > 0 || harness.mediaDeletes.length > 0 || Boolean(peer && peer.closed);
  }, "SDP body settlement");
  await new Promise((resolve) => {
    setImmediate(resolve);
  });
  const finalState = observe("sign-out-during-sdp-body", context, document, htmlPath, scriptBytes);
  finalState.during_sign_out = {
    authenticated: during.authenticated,
    live_session_count: during.live_session_count,
    tile_state: during.tile_state,
    media_deletes: during.media_deletes,
    peer_closed: during.peer_closed,
    open_peer_count: during.open_peer_count,
  };
  return finalState;
}

async function runReloginBeforeOldPost(context, document, htmlPath, scriptBytes) {
  await context.__vms.assignCamera(0, "cam-1");
  await waitFor(() => harness.reachedHold, "old WHEP POST hold");
  await context.__vms.signOut();
  document.getElementById("authToken").value = "field-token";
  await context.__vms.loginWithToken({ preventDefault() {} });
  for (let turn = 0; turn < 30; turn += 1) {
    if (harness.whepPostCount >= 2 && context.__vms.inspect().liveSessionCount === 1) break;
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
  }
  harness.releaseHold();
  await waitFor(() => harness.mediaDeletes.some((item) => item.url === SESSION_URL), "old session DELETE");
  await new Promise((resolve) => {
    setImmediate(resolve);
  });
  return observe("relogin-before-old-post", context, document, htmlPath, scriptBytes);
}

async function main() {
  const scenario = process.argv[2];
  const htmlPath = process.argv[3];
  if (!scenario || !htmlPath) {
    throw new Error("usage: node browser_live_logout_harness.js <scenario> <web/index.html>");
  }
  process.on("unhandledRejection", (error) => {
    harness.rejections.push(String(error && error.stack ? error.stack : error));
  });
  const result = await runScenario(scenario, htmlPath);
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

main().catch((error) => {
  const payload = { error: String(error && error.stack ? error.stack : error), rejections: harness.rejections };
  process.stdout.write(`${JSON.stringify(payload)}\n`);
  process.exitCode = 1;
});
