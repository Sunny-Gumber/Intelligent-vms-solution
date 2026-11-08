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
    '<label>ROI x <input id="fnRoiX" type="number" min="0" step="1" value="0"></label>',
    '<label>ROI y <input id="fnRoiY" type="number" min="0" step="1" value="0"></label>',
    '<label>ROI width <input id="fnRoiWidth" type="number" min="1" step="1" value="1"></label>',
    '<label>ROI height <input id="fnRoiHeight" type="number" min="1" step="1" value="1"></label>',
    '<button id="fnReset">Reset filters</button></div>',
    '<div class="actions"><button id="fnSaveOriginal">Save captured PNG</button>',
    '<button id="fnSaveEnhanced">Save enhanced PNG</button>',
    '<button id="fnSaveReport">Save provenance JSON</button></div>',
    '<p>Server clip enhancement uses the authorized recording export and separate bounded worker. Browser preview settings use a different algorithm.</p>',
    '<div class="actions"><button id="fnJobCreate">Process selected clip on server</button>',
    '<button id="fnJobRecover">Recover retained jobs</button><select id="fnJobSelect" aria-label="Retained server job"></select><button id="fnJobRefresh">Refresh job</button><button id="fnJobCancel">Cancel job</button>',
    '<button id="fnJobOriginal">Save original clip export</button>',
    '<button id="fnJobEnhanced">Save enhanced clip</button><button id="fnJobReport">Save server report</button><button id="fnJobDelete">Delete server job artifacts</button></div>',
    '<div id="fnJobMessage" role="status">Server processing requires an enabled isolated worker.</div>',
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
  let frame = null, operator = false, serverJob = null, jobRequest = null, retainedJobs = [];
  function isCurrent(item) {
    return !!item && item === frame && authenticated && operator && item.epoch === authEpoch &&
      item.generation === playbackState.playGeneration && item.cameraId === playbackState.cameraId &&
      item.cameraId === activeCameraId();
  }
  function matchesPlayback(selection) {
    if (!selection || !player.currentSrc) return false;
    const source = new URL(player.currentSrc, location.href);
    const expected = '/api/v1/recordings/cameras/' + encodeURIComponent(selection.cameraId) + '/play';
    return source.origin === location.origin && source.pathname === expected &&
      source.searchParams.get('start') === selection.start &&
      source.searchParams.get('duration') === String(selection.duration) &&
      source.searchParams.get('format') === 'mp4';
  }
  function updateCapture() {
    const selection = playbackSelection;
    button.disabled = !(authenticated && operator && selection &&
      selection.cameraId === playbackState.cameraId && selection.cameraId === activeCameraId() &&
      player.readyState >= 2 && player.videoWidth > 0 && matchesPlayback(selection));
  }
  function reset() {
    if (jobRequest) jobRequest.abort();
    jobRequest = null; serverJob = null; retainedJobs = [];
    el('fnJobSelect').replaceChildren(); el('fnJobSelect').disabled = false;
    el('fnJobMessage').textContent = 'Server processing requires an enabled isolated worker.';
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
      const values = settings();
      const roi = engine.validateROI({
        x: Number(el('fnRoiX').value), y: Number(el('fnRoiY').value),
        width: Number(el('fnRoiWidth').value), height: Number(el('fnRoiHeight').value),
      }, captured.width, captured.height);
      const output = engine.enhanceFrame(engine.cropFrame(captured.pixels, roi), values);
      const canvas = el('fnEnhanced');
      canvas.width = output.width;
      canvas.height = output.height;
      const context = canvas.getContext('2d');
      if (!context) throw new Error('Canvas unavailable');
      const image = context.createImageData(output.width, output.height);
      image.data.set(output.data);
      context.putImageData(image, 0, 0);
      captured.settings = values;
      captured.roi = roi;
      captured.valid = true;
      captured.revision++;
      captured.enhancedBlob = null;
      el('fnMessage').textContent = 'Captured at ' + captured.captureUtc +
        ' · approximate playback ' + captured.playbackUtc +
        ' · ' + captured.width + '×' + captured.height +
        ' · preview only; this is not an authenticated evidence record.';
    } catch (error) {
      captured.valid = false;
      captured.revision++;
      captured.enhancedBlob = null;
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
    if (!matchesPlayback(selection)) {
      el('pbmsg').textContent = 'Playback source or selected recorded interval changed; play this selection first.';
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
        selection: {...selection}, playbackSeconds: player.currentTime,
        revision: 0, originalBlob: null, enhancedBlob: null,
      };
      el('fnRoiX').value = '0'; el('fnRoiY').value = '0';
      el('fnRoiWidth').value = String(width); el('fnRoiHeight').value = String(height);
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
    if (kind === 'enhanced' && !item.valid) throw new Error('Correct invalid filters or ROI first');
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
        schema: 'vms-decoded-frame-preview-v2',
        camera_id: current.cameraId,
        captured_utc: current.captureUtc,
        approximate_playback_utc: current.playbackUtc,
        width: current.width, height: current.height,
        original_decoded_png_sha256: originalHash, enhanced_png_sha256: enhancedHash,
        roi: current.roi,
        recording_selection: current.selection,
        playback_offset_seconds: current.playbackSeconds,
        captured_png_filename: filename(current, 'original'),
        enhanced_png_filename: filename(current, 'enhanced'),
        reproduction: 'Decode the saved captured PNG, crop ROI in original pixels, then apply settings with the named browser algorithm. Browser video decoding and PNG encoding may vary; this does not reproduce camera-original evidence.',
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
  /** One manual job operation at a time; lifecycle changes abort requests and discard late results. */
  async function jobOperation(action) {
    const current = frame;
    if (!isCurrent(current) || jobRequest) return;
    if (action === 'create' && !current.valid) return;
    if (!['create', 'recover'].includes(action) && !serverJob) return;
    const revision = current.revision, expectedJob = serverJob;
    const controller = new AbortController();
    jobRequest = controller;
    el('fnJobSelect').disabled = true;
    const timer = window.setTimeout(() => controller.abort(), 65000);
    const stillCurrent = () => isCurrent(current) && jobRequest === controller;
    try {
      const create = action === 'create', cancel = action === 'cancel';
      const root = '/api/v1/forensic/jobs';
      const url = create || action === 'recover' ? root : root + '/' + encodeURIComponent(expectedJob.id) +
        (cancel ? '/cancel' : action === 'report' ? '/report' :
          ['original', 'enhanced'].includes(action) ? '/artifacts/' + action : '');
      const options = {signal: controller.signal, method: action === 'delete' ? 'DELETE' : create || cancel ? 'POST' : 'GET'};
      if (create) {
        if (serverJob && !['failed', 'cancelled', 'completed'].includes(serverJob.state)) {
          throw new Error('Cancel the active job before submitting another clip');
        }
        const roi = current.roi;
        options.headers = {'Content-Type': 'application/json'};
        options.body = JSON.stringify({camera_id: current.cameraId, start: current.selection.start,
          duration: current.selection.duration, settings: {
            brightness: current.settings.brightness, contrast: current.settings.contrast,
            gamma: current.settings.gamma, histogram_strength: current.settings.histogramStrength,
            roi: [roi.x, roi.y, roi.width, roi.height],
          }});
      }
      const response = await apiFetch(url, options);
      if (!stillCurrent()) return;
      if (response.status === 503) throw new Error('Server enhancement unavailable: isolated processing is disabled or unconfigured');
      if (!response.ok) throw new Error('Server job request failed (' + response.status + ')');
      if (action === 'delete') {
        serverJob = null; el('fnJobMessage').textContent = 'Server artifacts deleted.'; return;
      }
      if (['original', 'enhanced'].includes(action)) {
        const blob = await response.blob();
        if (stillCurrent() && serverJob === expectedJob) save(blob, 'forensic-' + expectedJob.id + '-' + action + '.mp4');
        return;
      }
      const result = await response.json();
      if (!stillCurrent()) return;
      if (action === 'recover') {
        if (!result || !Array.isArray(result.jobs) || result.jobs.length > 4) throw new Error('Invalid retained jobs response');
        retainedJobs = result.jobs.filter(job => job.camera_id === current.cameraId && /^[a-f0-9]{32}$/.test(job.id));
        el('fnJobSelect').replaceChildren();
        for (const job of retainedJobs) {
          const option = document.createElement('option');
          option.value = job.id; option.textContent = job.start + ' · ' + job.state + ' · ' + job.id;
          el('fnJobSelect').appendChild(option);
        }
        serverJob = retainedJobs[0] || null;
        el('fnJobMessage').textContent = retainedJobs.length ?
          'Recovered retained jobs for this camera. Select a job, then refresh or download.' : 'No retained jobs for this camera.';
        return;
      }
      if (action === 'report') {
        if (serverJob === expectedJob) save(new Blob([JSON.stringify(result, null, 2) + '\n'],
          {type: 'application/json'}), 'forensic-' + expectedJob.id + '-server-report.json');
        return;
      }
      if (!result || !/^[a-f0-9]{32}$/.test(result.id)) throw new Error('Invalid server job response');
      serverJob = result;
      el('fnJobMessage').textContent = 'Server job ' + result.id + ': ' + result.state +
        ' | recording ' + result.start + ' | duration ' + result.duration + ' seconds' +
        (create && revision !== current.revision ? ' (submitted settings differ from current preview)' : '') +
        '. Refresh manually. Enhanced interpretation only; retain the original export and report.';
    } catch (error) {
      if (stillCurrent()) el('fnJobMessage').textContent = error.name === 'AbortError' ?
        'Server request timed out. A submitted job may still exist; recover retained jobs before retrying.' : String(error.message);
    } finally {
      window.clearTimeout(timer);
      if (jobRequest === controller) {
        jobRequest = null; el('fnJobSelect').disabled = false;
      }
    }
  }
  for (const [id, action] of [['fnJobCreate', 'create'], ['fnJobRefresh', 'refresh'],
    ['fnJobCancel', 'cancel'], ['fnJobOriginal', 'original'], ['fnJobEnhanced', 'enhanced'],
    ['fnJobReport', 'report'], ['fnJobDelete', 'delete'], ['fnJobRecover', 'recover']]) el(id).addEventListener('click', () => jobOperation(action));
  el('fnJobSelect').addEventListener('change', () => {
    if (!jobRequest) serverJob = retainedJobs.find(job => job.id === el('fnJobSelect').value) || null;
  });
  button.addEventListener('click', capture);
  for (const name of ['loadeddata', 'emptied', 'error']) player.addEventListener(name, updateCapture);
  for (const name of ['fnBrightness', 'fnContrast', 'fnGamma', 'fnHistogram', 'fnRoiX', 'fnRoiY', 'fnRoiWidth', 'fnRoiHeight']) {
    el(name).addEventListener('input', render);
  }
  el('fnReset').addEventListener('click', () => {
    for (const [id, value] of [
      ['fnBrightness', '0'], ['fnContrast', '1'], ['fnGamma', '1'], ['fnHistogram', '0'],
    ]) el(id).value = value;
    if (frame) {
      el('fnRoiX').value = '0'; el('fnRoiY').value = '0';
      el('fnRoiWidth').value = String(frame.width); el('fnRoiHeight').value = String(frame.height);
    }
    render();
  });
  el('fnSaveOriginal').addEventListener('click', () => download('original'));
  el('fnSaveEnhanced').addEventListener('click', () => download('enhanced'));
  el('fnSaveReport').addEventListener('click', report);
  window.VMSForensicUI = Object.freeze({reset, updateCapture, updatePrivileges});
  // Initial session can finish before this external script is loaded.
  updatePrivileges(authenticated, {roles: window.vmsForensicCanInspect ? ['operator'] : []});
})();
