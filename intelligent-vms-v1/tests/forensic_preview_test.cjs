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
assert.ok(ui.includes("source.pathname !== expected") && ui.includes("source.origin !== location.origin"));
assert.ok(ui.includes('authenticated && operator'));
assert.ok(ui.includes('cameraId === playbackState.cameraId'));
assert.ok(ui.includes('player.videoWidth') && ui.includes('engine.MAX_PIXELS'));
assert.ok(ui.includes('original_decoded_png_sha256') && ui.includes('enhanced_png_sha256'));
assert.ok(ui.includes('NOT') || ui.includes('not the source recording'));
assert.ok(!ui.includes('fetch(') && !ui.includes('RTSP'));
assert.ok(!ui.includes('MediaRecorder'));
console.log('PASS: pure frame enhancement, bounds, browser JS syntax, role/source/lifecycle wiring');
