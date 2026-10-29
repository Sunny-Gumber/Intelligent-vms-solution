/* No external packages. Exercise pure CPU/browser enhancement and VMS route wiring. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const engine = require(path.join(root, 'web/forensic-preview.js'));
const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
const ui = fs.readFileSync(path.join(root, 'web/forensic-ui.js'), 'utf8');

const base = Object.freeze({brightness: 0, contrast: 1, gamma: 1, histogramStrength: 0});
function sample(values) {
  return {width: 2, height: 1, data: new Uint8ClampedArray(values)};
}
const source = sample([12, 25, 30, 255, 120, 130, 140, 99]);
const identity = engine.enhanceFrame(source, base);
assert.deepEqual(Array.from(identity.data), Array.from(source.data), 'identity must preserve all pixels');
assert.notStrictEqual(identity.data, source.data, 'never mutate or alias the original buffer');

const brighter = engine.enhanceFrame(source, {...base, gamma: 1.8});
assert.ok(brighter.data[0] > source.data[0], 'gamma should brighten low-light samples');
assert.equal(brighter.data[3], 255);
assert.equal(brighter.data[7], 99, 'alpha channel must not change');
assert.deepEqual(Array.from(source.data), [12, 25, 30, 255, 120, 130, 140, 99]);

const equalized = engine.enhanceFrame(source, {...base, histogramStrength: 0.5});
assert.ok(equalized.data.some((value, index) => value !== source.data[index]));
const flat = sample([30, 30, 30, 255, 30, 30, 30, 255]);
assert.deepEqual(Array.from(engine.enhanceFrame(flat, {...base, histogramStrength: 0.4}).data),
  Array.from(flat.data), 'uniform frames should not invent contrast');

for (const invalid of [
  {brightness: NaN}, {contrast: Infinity}, {histogramStrength: 0.8}, {gamma: 0},
]) {
  assert.throws(() => engine.enhanceFrame(source, {...base, ...invalid}), /bounds/);
}
assert.throws(() => engine.enhanceFrame(sample([1, 2, 3, 4]), base), /invalid/);
assert.throws(() => engine.enhanceFrame({
  width: 1921, height: 1080, data: new Uint8ClampedArray(1921 * 1080 * 4),
}, base), /exceeds/);
assert.throws(() => engine.enhanceFrame({width: 2, height: 1, data: new Uint8ClampedArray(3)}, base), /invalid/);

const inline = html.match(/<script>\s*([\s\S]*?)\s*<\/script>/);
assert.ok(inline, 'VMS inline controller exists');
new vm.Script(inline[1], {filename: 'index.html inline controller'});
new vm.Script(ui, {filename: 'forensic-ui.js'});

assert.ok(html.includes('<script src="/forensic-preview.js"></script>'));
assert.ok(html.includes('<script src="/forensic-ui.js"></script>'));
assert.ok(html.includes('VMSForensicUI.updatePrivileges(next,info)'));
assert.ok(html.includes('if(window.VMSForensicUI)window.VMSForensicUI.reset()'));
assert.ok(ui.includes("role === 'admin' || role === 'operator'"));
assert.ok(ui.includes('candidate') === false || ui.includes('candidate.authEpoch') === false);
assert.ok(ui.includes("source.pathname === expected") && ui.includes("source.origin === location.origin"));
assert.ok(ui.includes("source.searchParams.get('start') === selection.start"));
assert.ok(ui.includes("source.searchParams.get('duration') === String(selection.duration)"));
assert.ok(ui.includes("source.searchParams.get('format') === 'mp4'"));
assert.ok(ui.includes('authenticated && operator'));
assert.ok(ui.includes('cameraId === playbackState.cameraId'));
assert.ok(ui.includes('player.videoWidth') && ui.includes('engine.MAX_PIXELS'));
assert.ok(ui.includes('original_decoded_png_sha256') && ui.includes('enhanced_png_sha256'));
assert.ok(ui.includes('NOT') || ui.includes('not the source recording'));
assert.ok(!ui.includes('fetch(') && !ui.includes('RTSP'));
assert.ok(!ui.includes('MediaRecorder'));

const crop = engine.cropFrame(source, {x: 1, y: 0, width: 1, height: 1});
assert.deepEqual(Array.from(crop.data), [120, 130, 140, 99]);
assert.equal(crop.width, 1);
assert.equal(crop.height, 1);
crop.data[0] = 0;
assert.equal(source.data[4], 120, 'ROI must not alias original evidence');
assert.deepEqual(engine.validateROI(null, 2, 1), {x: 0, y: 0, width: 2, height: 1});
for (const roi of [
  {x: -1, y: 0, width: 1, height: 1}, {x: 0, y: 0, width: 0, height: 1},
  {x: 1, y: 0, width: 2, height: 1}, {x: 0.5, y: 0, width: 1, height: 1},
  {x: 0, y: NaN, width: 1, height: 1},
]) assert.throws(() => engine.cropFrame(source, roi), /ROI/);
assert.deepEqual(Array.from(engine.cropFrame(source, null).data), Array.from(source.data));
assert.ok(ui.includes('current.roi') && ui.includes('recording_selection: current.selection'));
assert.ok(ui.includes("kind === 'enhanced' && !item.valid"));

// Execute UI listeners against a small DOM double: exercise selection and export guards.
function uiHarness() {
  const nodes = new Map();
  const downloads = [];
  function element(id) {
    if (nodes.has(id)) return nodes.get(id);
    const node = {
      id, value: '0', width: 0, height: 0, hidden: false, listeners: {},
      addEventListener(name, handler) { this.listeners[name] = handler; },
      appendChild() {}, replaceChildren() {}, insertAdjacentElement() {}, setAttribute() {},
      querySelector() { return element('actions'); },
      click() { downloads.push(this.download); },
      toBlob(callback) { callback(new Blob(['png'])); },
      getContext() {
        return {
          drawImage() {}, putImageData(image) { node.lastImage = image; },
          getImageData() { return {width: 2, height: 1, data: new Uint8ClampedArray(source.data)}; },
          createImageData(width, height) { return {width, height, data: new Uint8ClampedArray(width * height * 4)}; },
        };
      },
    };
    nodes.set(id, node);
    return node;
  }
  const player = element('player');
  Object.assign(player, {readyState: 2, videoWidth: 2, videoHeight: 1, currentTime: 0,
    currentSrc: 'https://vms.test/api/v1/recordings/cameras/camera/play?start=2026-01-01T00%3A00%3A00Z&duration=5&format=mp4'});
  element('fnContrast').value = '1'; element('fnGamma').value = '1';
  const context = {
    window: {VMSForensicPreview: engine, vmsForensicCanInspect: true, setTimeout, clearTimeout},
    document: {getElementById: element, createElement: element, head: {appendChild() {}}},
    authenticated: true, authEpoch: 1,
    playbackState: {playGeneration: 1, cameraId: 'camera'},
    playbackSelection: {cameraId: 'camera', start: '2026-01-01T00:00:00Z', duration: 5},
    activeCameraId: () => 'camera', location: {href: 'https://vms.test/', origin: 'https://vms.test'},
    AbortController,
    apiFetch: async () => ({ok: false, status: 503}),
    URL: class extends URL {
      static createObjectURL() { return 'blob:test'; }
      static revokeObjectURL() {}
    }, Blob, console,
  };
  vm.runInNewContext(ui, context);
  return {element, context, downloads};
}
async function testROIInteraction() {
  const {element, context, downloads} = uiHarness();
  element('button').listeners.click();
  assert.equal(element('fnRoiWidth').value, '2');
  element('fnRoiX').value = '1'; element('fnRoiWidth').value = '1';
  element('fnRoiWidth').listeners.input();
  assert.equal(element('fnEnhanced').width, 1);
  assert.deepEqual(Array.from(element('fnEnhanced').lastImage.data), [120, 130, 140, 99]);
  element('fnRoiWidth').value = '2'; element('fnRoiWidth').listeners.input();
  assert.match(element('fnMessage').textContent, /Enhancement error/);
  await element('fnSaveEnhanced').listeners.click();
  assert.equal(downloads.length, 0, 'invalid ROI cannot export stale derivative');
  element('fnReset').listeners.click();
  assert.equal(element('fnEnhanced').width, 2);
  await element('fnJobCreate').listeners.click();
  assert.match(element('fnJobMessage').textContent, /unavailable/);
  const calls = [];
  const job = {id: 'a'.repeat(32), state: 'queued'};
  context.apiFetch = async (url, options) => {
    calls.push({url, options});
    return {ok: true, status: 200, json: async () => job, blob: async () => new Blob(['clip'])};
  };
  await element('fnJobCreate').listeners.click();
  const payload = JSON.parse(calls[0].options.body);
  assert.equal(payload.camera_id, 'camera');
  assert.deepEqual(payload.settings.roi, [0, 0, 2, 1]);
  assert.equal(calls[0].options.method, 'POST');
  await element('fnJobRefresh').listeners.click();
  assert.equal(calls[1].url, '/api/v1/forensic/jobs/' + job.id);
  await element('fnJobCancel').listeners.click();
  assert.equal(calls[2].options.method, 'POST');
  assert.ok(calls[2].url.endsWith('/cancel'));
  await element('fnJobOriginal').listeners.click();
  assert.equal(downloads.length, 1);
  await element('fnJobReport').listeners.click();
  assert.equal(downloads.length, 2);
  await element('fnJobDelete').listeners.click();
  assert.equal(calls.at(-1).options.method, 'DELETE');
  assert.match(element('fnJobMessage').textContent, /deleted/);
  context.apiFetch = async () => ({ok: true, status: 200,
    json: async () => ({jobs: [{...job, camera_id: 'camera', start: '2026-01-01T00:00:00Z'}]})});
  await element('fnJobRecover').listeners.click();
  assert.match(element('fnJobMessage').textContent, /Recovered/);
  // A late create reply must not resurrect evidence after auth/camera reset.
  let finish;
  context.apiFetch = () => new Promise(resolve => { finish = resolve; });
  const pending = element('fnJobRefresh').listeners.click();
  context.window.VMSForensicUI.reset();
  finish({ok: true, status: 202, json: async () => job});
  await pending;
  assert.ok(!element('fnJobMessage').textContent.includes(job.id));
  context.window.VMSForensicUI.reset();
  assert.equal(element('fnEnhanced').width, 0, 'session reset clears captured image');
}
testROIInteraction().then(() => {
  console.log('PASS: frame enhancement, ROI pixels and DOM interaction, bounds, syntax, role/source/lifecycle wiring');
}).catch(error => { console.error(error); process.exitCode = 1; });
