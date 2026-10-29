/* VMS browser investigation preview: decoded-frame derivatives, not original evidence. */
const VMSForensicPreview = (() => {
  'use strict';
  const MAX_PIXELS = 1920 * 1080;
  const BOUNDS = Object.freeze({
    brightness: [-0.2, 0.2],
    contrast: [0.5, 3],
    gamma: [0.4, 3],
    histogramStrength: [0, 0.5],
  });

  function validateSettings(input) {
    if (!input || typeof input !== 'object') throw new TypeError('Filter settings required');
    const settings = {};
    for (const [key, bounds] of Object.entries(BOUNDS)) {
      const value = input[key];
      if (typeof value !== 'number' || !Number.isFinite(value) || value < bounds[0] || value > bounds[1]) {
        throw new RangeError(key + ' is outside safe bounds');
      }
      settings[key] = value;
    }
    return settings;
  }

  function validateFrame(frame) {
    if (!frame || !Number.isInteger(frame.width) || !Number.isInteger(frame.height) ||
        frame.width < 1 || frame.height < 1 || frame.width * frame.height > MAX_PIXELS ||
        !(frame.data instanceof Uint8ClampedArray) ||
        frame.data.length !== 4 * frame.width * frame.height) {
      throw new RangeError('Decoded frame is invalid or exceeds 1920x1080');
    }
    return frame.width * frame.height;
  }

  /** Validate an optional crop in original decoded-frame pixels; reject invalid coordinates. */
  function validateROI(roi, width, height) {
    if (roi == null) return {x: 0, y: 0, width, height};
    if (!roi || !['x', 'y', 'width', 'height'].every(key => Number.isInteger(roi[key])) ||
        roi.x < 0 || roi.y < 0 || roi.width < 1 || roi.height < 1 ||
        roi.x + roi.width > width || roi.y + roi.height > height) {
      throw new RangeError('ROI must be inside the original frame');
    }
    return {x: roi.x, y: roi.y, width: roi.width, height: roi.height};
  }

  /** Copy a validated crop without mutating or sharing the original pixel buffer. */
  function cropFrame(frame, roi) {
    validateFrame(frame);
    const crop = validateROI(roi, frame.width, frame.height);
    const data = new Uint8ClampedArray(crop.width * crop.height * 4);
    for (let row = 0; row < crop.height; row++) {
      const start = ((crop.y + row) * frame.width + crop.x) * 4;
      data.set(frame.data.subarray(start, start + crop.width * 4), row * crop.width * 4);
    }
    return {width: crop.width, height: crop.height, data};
  }

  const clamp = x => Math.max(0, Math.min(255, x));
  function luma(pixels, index) {
    return Math.round(0.2126 * pixels[index] + 0.7152 * pixels[index + 1] + 0.0722 * pixels[index + 2]);
  }

  function histogramMap(pixels, count) {
    const histogram = new Uint32Array(256);
    for (let pixel = 0; pixel < count; pixel++) histogram[luma(pixels, pixel * 4)]++;
    const mapped = new Uint8Array(256);
    const first = histogram.findIndex(value => value > 0);
    let accumulated = 0;
    const minCount = first === -1 ? 0 : histogram[first];
    for (let value = 0; value < 256; value++) {
      accumulated += histogram[value];
      mapped[value] = minCount === count ? value :
        Math.round(255 * Math.max(0, accumulated - minCount) / (count - minCount));
    }
    return mapped;
  }

  function enhanceFrame(frame, input) {
    const count = validateFrame(frame);
    const settings = validateSettings(input);
    const result = new Uint8ClampedArray(frame.data);
    const map = settings.histogramStrength ? histogramMap(frame.data, count) : null;

    for (let pixel = 0; pixel < count; pixel++) {
      const index = pixel * 4;
      const previousLuma = map ? luma(frame.data, index) : 0;
      const equalizationShift = map ?
        settings.histogramStrength * (map[previousLuma] - previousLuma) : 0;
      for (let channel = 0; channel < 3; channel++) {
        const equalized = clamp(frame.data[index + channel] + equalizationShift);
        const contrasted = clamp((equalized - 128) * settings.contrast + 128 + settings.brightness * 255);
        result[index + channel] = Math.round(255 * Math.pow(contrasted / 255, 1 / settings.gamma));
      }
      // The fourth channel, including transparency, is intentionally unchanged.
    }
    return {width: frame.width, height: frame.height, data: result};
  }

  return Object.freeze({MAX_PIXELS, validateSettings, validateFrame, validateROI, cropFrame, enhanceFrame});
})();
if (typeof window !== 'undefined') window.VMSForensicPreview = VMSForensicPreview;
if (typeof module !== 'undefined' && module.exports) module.exports = VMSForensicPreview;
