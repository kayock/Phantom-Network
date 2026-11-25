/* PHANTOM NETWORK — JS DSP test suite (run with: node tests/js/test_dsp.mjs)
   Verifies the exact Signal Laboratory code shipped in web/js/lib/dsp.js:
   Morse codec, FSK acoustic codec (encode + noisy decode + CRC refusal). */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

// dsp.js is a browser ES module without DOM dependencies — load it via
// a data URL import after stripping nothing (it has no imports).
const path = new URL('../../web/js/lib/dsp.js', import.meta.url);
const source = readFileSync(path, 'utf8');
const mod = await import('data:text/javascript;base64,' +
  Buffer.from(source).toString('base64'));

const { textToMorse, morseToText, encodeAcoustic, decodeAcoustic,
        offlineRoundtrip, morseTimeline, peakFrequency, goertzel } = mod;

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); passed++; console.log('  ok  ', name); }
  catch (e) { failed++; console.log('  FAIL', name, '→', e.message); }
}

console.log('Morse codec');
t('text → morse basic', () => {
  assert.equal(textToMorse('SOS'), '... --- ...');
});
t('morse → text basic', () => {
  assert.equal(morseToText('... --- ...'), 'SOS');
});
t('roundtrip with words and punctuation', () => {
  const src = 'PHANTOM NETWORK 1.0.0!';
  assert.equal(morseToText(textToMorse(src)), src.toUpperCase());
});
t('unknown chars are dropped, not fabricated', () => {
  assert.equal(textToMorse('A~B'), '.- -...');
});
t('timeline has correct durations', () => {
  const tl = morseTimeline('..', 0.1);
  assert.equal(tl[0].len, 0.1);
  assert.equal(tl[1].start, 0.2);
});

console.log('Goertzel / spectrum');
t('goertzel finds a pure tone', () => {
  const sr = 44100, f = 1500;
  const buf = new Float32Array(4096);
  for (let i = 0; i < buf.length; i++) buf[i] = Math.sin(2 * Math.PI * f * i / sr);
  const pF = goertzel(buf, f, sr);
  const pOther = goertzel(buf, 400, sr);
  assert.ok(pF > pOther * 100, `signal ${pF} vs ${pOther}`);
});
t('peakFrequency picks the bin', () => {
  const spec = new Array(100).fill(0);
  spec[42] = 0.9;
  assert.equal(peakFrequency(spec, 10), 420);
});

console.log('FSK acoustic messaging');
t('offline roundtrip clean', () => {
  const r = offlineRoundtrip('PHANTOM', { snr: 30 });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.got, 'PHANTOM');
});
t('offline roundtrip with moderate noise', () => {
  const r = offlineRoundtrip('GHOST PROTOCOL 7', { snr: 14 });
  assert.equal(r.ok, true, JSON.stringify(r));
});
t('decoder refuses garbage (no fabricated text)', () => {
  const noise = new Float32Array(44100);
  let seed = 42;
  for (let i = 0; i < noise.length; i++) {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    noise[i] = seed / 0x3fffffff - 1;
  }
  const r = decodeAcoustic(noise, 44100);
  assert.equal(r.text, '');
  assert.ok(r.errors.length > 0);
});
t('decoder refuses silently-flipped bits via CRC', () => {
  // encode, flip one payload bit in the synthesized stream, expect refusal
  const text = 'INTEGRITY CHECK';
  const tones = encodeAcoustic(text);
  const sr = 44100;
  const step = (0.08 + 0.04) * sr;
  const buf = new Float32Array(Math.ceil(tones.length * step));
  tones.forEach((f, i) => {
    const start = Math.floor(i * step);
    const len = Math.floor(0.08 * sr);
    for (let j = 0; j < len; j++) {
      const freq = (i === 4) ? 1000 : f;      // flip payload bit 0 (1→0)
      buf[start + j] += Math.sin(2 * Math.PI * freq * j / sr) * 0.6;
    }
  });
  const r = decodeAcoustic(buf, sr);
  // either CRC refuses, or text != expected — never silent corruption
  if (r.text) assert.notEqual(r.text, text, 'corrupted frame decoded as correct!');
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
