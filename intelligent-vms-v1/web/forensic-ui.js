/* VMS-only browser investigation UI; no camera streams or recording changes. */
(function () {
  'use strict';
  const playback = document.getElementById('playback');
  const player = document.getElementById('player');
  const engine = window.VMSForensicPreview;
  if (!playback || !player || !engine) return;
  const button = document.createElement('button');
  button.id = 'fnCapture';
  button.textContent = 'Inspect playback frame';
  button.disabled = true;
  playback.querySelector('.actions').appendChild(button);
  const panel = document.createElement('section');
  panel.className = 'panel';
  panel.id = 'forensicPanel';
  panel.hidden = true;
  panel.setAttribute('aria-label', 'Recorded frame enhancement');
  panel.innerHTML = [
    '<div style="padding:12px"><strong>Recorded frame enhancement</strong>',
    '<p class="muted">Browser-only decoded frame preview. Not the camera-original file or verified forensic evidence.</p>',
    '<div class="forensic-pair">',
    '<div><div class="muted">Captured playback frame</div><canvas id="fnOriginal"></canvas></div>',
    '<div><div class="muted">Enhanced derivative</div><canvas id="fnEnhanced"></canvas></div></div>',
    '<div class="forensic-controls">',
    '<label>Brightness <input id="fnBrightness" type="range" min="-0.2" max="0.2" step="0.01" value="0"></label>',
    '<label>Contrast <input id="fnContrast" type="range" min="0.5" max="3" step="0.05" value="1"></label>',
    '<label>Gamma <input id="fnGamma" type="range" min="0.4" max="3" step="0.05" value="1"></label>',
    '<label>Histogram <input id="fnHistogram" type="range" min="0" max="0.5" step="0.05" value="0"></label>',
    '<button id="fnReset">Reset filters</button></div>',
    '<div class="actions"><button id="fnSaveOriginal">Save captured PNG</button>',
    '<button id="fnSaveEnhanced">Save enhanced PNG</button>',
    '<button id="fnSaveReport">Save provenance JSON</button></div>',
    '<div class="muted" id="fnMessage" role="status" aria-live="polite">No frame captured.</div></div>',
  ].join('');
  player.insertAdjacentElement('afterend', panel);
  const css = document.createElement('style');
  css.textContent = '#forensicPanel{margin-top:12px}#forensicPanel canvas{width:100%;height:auto;display:block;background:#000;border:1px solid #344252}' +
    '.forensic-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}' +
    '.forensic-controls{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;margin:12px 0}' +
    '.forensic-controls label{display:grid;gap:5px;font-size:12px;color:#bdcddd}.forensic-controls input{width:120px}' +
    '@media(max-width:720px){.forensic-pair{grid-template-columns:1fr}}';
  document.head.appendChild(css);
  const el = id => document.getElementById(id);
  let frame = null, operator = false;
  function isCurrent(item) {
    return !!item && item === frame && authenticated && operator && item.epoch === authEpoch &&
      item.generation === playbackState.playGeneration && item.cameraId === playbackState.cameraId &&
      item.cameraId === activeCameraId();
  }
  function updateCapture() {
    const selection = playbackSelection;
    button.disabled = !(authenticated && operator && selection &&
      selection.cameraId === playbackState.cameraId && selection.cameraId === activeCameraId() &&
      player.readyState >= 2 && player.videoWidth > 0);
  }
  function reset() {
    frame = null;
    panel.hidden = true;
    for (const id of ['fnOriginal', 'fnEnhanced']) { el(id).width = 0; el(id).height = 0; }
    updateCapture();
  }
  function updatePrivileges(enabled, info) {
    operator = !!enabled && !!info && Array.isArray(info.roles) &&
      info.roles.some(role => role === 'admin' || role === 'operator');
    if (!operator) reset();
    updateCapture();
  }
  function settings() {
    return engine.validateSettings({
      brightness: Number(el('fnBrightness').value),
      contrast: Number(el('fnContrast').value),
      gamma: Number(el('fnGamma').value),
      histogramStrength: Number(el('fnHistogram').value),
    });
  }
  function render() {
    const captured = frame;
    if (!isCurrent(captured)) return;
    try {
      const values = settings(), output = engine.enhanceFrame(captured.pixels, values);
      const canvas = el('fnEnhanced');
      canvas.width = captured.width;
      canvas.height = captured.height;
      const context = canvas.getContext('2d');
      if (!context) throw new Error('Canvas unavailable');
      const image = context.createImageData(captured.width, captured.height);
      image.data.set(output.data);
      context.putImageData(image, 0, 0);
      captured.settings = values;
      captured.revision++;
      captured.enhancedBlob = null;
      el('fnMessage').textContent = 'Captured at ' + captured.captureUtc +
        ' · approximate playback ' + captured.playbackUtc +
        ' · ' + captured.width + '×' + captured.height +
        ' · preview only; this is not an authenticated evidence record.';
    } catch (error) {
      el('fnMessage').textContent = 'Enhancement error: ' + String(error.message || 'invalid input');
    }
  }
  function capture() {
    const selection = playbackSelection;
    if (!authenticated || !operator || !selection ||
        selection.cameraId !== playbackState.cameraId || selection.cameraId !== activeCameraId() ||
        player.readyState < 2 || !player.currentSrc) {
      el('pbmsg').textContent = 'Play authorized footage before capturing an investigation frame.';
      return;
    }
    const source = new URL(player.currentSrc, location.href);
    const expected = '/api/v1/recordings/cameras/' + encodeURIComponent(selection.cameraId) + '/play';
    if (source.origin !== location.origin || source.pathname !== expected) {
      el('pbmsg').textContent = 'Only same-origin VMS playback is eligible for inspection.';
      return;
    }
    const width = player.videoWidth, height = player.videoHeight;
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 ||
        width * height > engine.MAX_PIXELS) {
      el('pbmsg').textContent = 'Frame inspection limit: 1920×1080 pixels.';
      return;
    }
    try {
      reset();
      const canvas = el('fnOriginal');
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext('2d', {willReadFrequently: true});
      if (!context) throw new Error('Canvas unavailable');
      context.drawImage(player, 0, 0, width, height);
      const pixels = context.getImageData(0, 0, width, height);
      const approximate = new Date(new Date(selection.start).getTime() + player.currentTime * 1000);
      if (Number.isNaN(approximate.getTime())) throw new Error('Playback timestamp invalid');
      frame = {
        cameraId: selection.cameraId, epoch: authEpoch, generation: playbackState.playGeneration,
        width, height, pixels, captureUtc: new Date().toISOString(), playbackUtc: approximate.toISOString(),
        revision: 0, originalBlob: null, enhancedBlob: null,
      };
      panel.hidden = false;
      render();
    } catch (error) {
      reset();
      el('pbmsg').textContent = 'Frame capture blocked: ' + String(error.message || 'unsupported video');
    }
  }
  function filename(item, kind) {
    const safeCamera = String(item.cameraId).replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 64) || 'camera';
    return 'forensic-' + safeCamera + '-' + item.captureUtc.replace(/[:.]/g, '-') + '-' + kind + '.png';
  }
  async function encoded(item, kind) {
    const key = kind === 'original' ? 'originalBlob' : 'enhancedBlob';
    if (item[key]) return item[key];
    const revision = item.revision, canvas = el(kind === 'original' ? 'fnOriginal' : 'fnEnhanced');
    const blob = await new Promise((resolve, reject) => canvas.toBlob(
      value => value ? resolve(value) : reject(new Error('PNG encoding failed')), 'image/png',
    ));
    if (!isCurrent(item) || (kind === 'enhanced' && revision !== item.revision)) return null;
    item[key] = blob;
    return blob;
  }
  function save(blob, name) {
    const url = URL.createObjectURL(blob), anchor = document.createElement('a');
    try { anchor.href = url; anchor.download = name; anchor.rel = 'noopener'; anchor.click(); }
    finally { URL.revokeObjectURL(url); }
  }
  async function download(kind) {
    const current = frame;
    if (!isCurrent(current)) return;
    try {
      const blob = await encoded(current, kind);
      if (blob && isCurrent(current)) save(blob, filename(current, kind));
    } catch (error) {
      if (isCurrent(current)) el('fnMessage').textContent = 'PNG download failed: ' + String(error.message);
    }
  }
  async function hash(blob) {
    const bytes = new Uint8Array(await window.crypto.subtle.digest('SHA-256', await blob.arrayBuffer()));
    return Array.from(bytes, part => part.toString(16).padStart(2, '0')).join('');
  }
  async function report() {
    const current = frame;
    if (!isCurrent(current)) return;
    try {
      if (!window.crypto || !window.crypto.subtle) throw new Error('SHA-256 requires HTTPS or localhost');
      const revision = current.revision, original = await encoded(current, 'original');
      if (!original) return;
      const enhanced = await encoded(current, 'enhanced');
      if (!enhanced || !isCurrent(current) || current.revision !== revision) return;
      const originalHash = await hash(original), enhancedHash = await hash(enhanced);
      if (!isCurrent(current) || current.revision !== revision) return;
      const manifest = {
        schema: 'vms-decoded-frame-preview-v1',
        camera_id: current.cameraId,
        captured_utc: current.captureUtc,
        approximate_playback_utc: current.playbackUtc,
        width: current.width, height: current.height,
        original_decoded_png_sha256: originalHash, enhanced_png_sha256: enhancedHash,
        settings: current.settings, algorithm: 'browser-luminance-histogram-v1',
        source: 'same-origin authorized decoded VMS playback',
        disclaimer: 'Not original recording media. Hashes refer to PNG outputs only, not the source recording. Playback time is approximate. Not an authenticated server audit record. Enhancement may distort pixels.',
      };
      save(new Blob([JSON.stringify(manifest, null, 2) + '\n'], {type: 'application/json'}),
        filename(current, 'enhanced').replace(/\.png$/, '.forensic.json'));
    } catch (error) {
      if (isCurrent(current)) el('fnMessage').textContent = 'Provenance report failed: ' + String(error.message);
    }
  }
  button.addEventListener('click', capture);
  for (const name of ['loadeddata', 'emptied', 'error']) player.addEventListener(name, updateCapture);
  for (const name of ['fnBrightness', 'fnContrast', 'fnGamma', 'fnHistogram']) {
    el(name).addEventListener('input', render);
  }
  el('fnReset').addEventListener('click', () => {
    for (const [id, value] of [
      ['fnBrightness', '0'], ['fnContrast', '1'], ['fnGamma', '1'], ['fnHistogram', '0'],
    ]) el(id).value = value;
    render();
  });
  el('fnSaveOriginal').addEventListener('click', () => download('original'));
  el('fnSaveEnhanced').addEventListener('click', () => download('enhanced'));
  el('fnSaveReport').addEventListener('click', report);
  window.VMSForensicUI = Object.freeze({reset, updateCapture, updatePrivileges});
})();
