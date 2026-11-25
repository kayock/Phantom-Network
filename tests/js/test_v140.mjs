/* PHANTOM NETWORK v1.4.0 — shipped-JS regression suite
   (run with: node tests/js/test_v140.mjs)

   Validates the exact code SHIPPED in the release archive:
   * v1.4.0 DSP v2: Hamming(7,4) FEC vectors, ARQ NAK, v2 modem
     roundtrip, v1 backward compatibility, honest-failure sims
   * syntax integrity of every shipped JS module (v1.3.0 shipped a
     syntax defect that blocked its mobile UI — never again)
   * view wiring: every desktop view file exists and exports render;
     every mobile tab has a renderer
*/

import assert from 'node:assert/strict';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = new URL('../..', import.meta.url).pathname;
const dspPath = new URL('../../web/js/lib/dsp.js', import.meta.url).pathname;
const source = readFileSync(dspPath, 'utf8');
const dsp = await import('data:text/javascript;base64,' +
  Buffer.from(source).toString('base64'));

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log(`  ok  ${name}`); }
  catch (e) { failed++; console.log(`FAIL  ${name}\n      ${e.message}`); }
}

/* ---------- DSP v2 ---------- */
test('FEC: clean codeword decodes', () => {
  const w = dsp.ham74EncodeNibble(0b1011);
  assert.equal(w.join(''), '0110011');
  const d = dsp.ham74DecodeWord(w);
  assert.equal(d.ok, true);
  assert.equal(d.nibble, 0b1011);
  assert.equal(d.corrected, false);
});
test('FEC: single-bit error corrected', () => {
  const w = dsp.ham74EncodeNibble(0b1011);
  w[2] ^= 1;
  const d = dsp.ham74DecodeWord(w);
  assert.equal(d.corrected, true);
  assert.equal(d.nibble, 0b1011);
});
test('FEC: burst correction (seeded 50% single-bit)', () => {
  const bytes = Array.from({ length: 32 }, (_, i) => (i * 37 + 11) & 0xFF);
  const rnd = dsp.mulberry32(1234);
  const corrupted = dsp.fecEncodeBytes(bytes).map(word => {
    const c = word.slice();
    if (rnd() < 0.5) c[Math.floor(rnd() * 7)] ^= 1;
    return c;
  });
  const dec = dsp.fecDecodeWords(corrupted);
  assert.equal(JSON.stringify(dec.bytes), JSON.stringify(bytes));
  assert.equal(dec.uncorrectable, 0);
});
test('v2 modem: offline roundtrip decodes exactly', () => {
  const r = dsp.v2Roundtrip('phantom v2 fec test message');
  assert.equal(r.textMatches, true);
  assert.equal(r.packets_ok, r.packets_total);
});
test('ARQ: NAK reports exactly the missing packets', () => {
  const nak = dsp.buildNak([1, 3, 4, 5, 6], 8);
  const p = dsp.parseNak(nak);
  assert.deepEqual(p.missing, [0, 2, 7]);
  assert.equal(p.total, 8);
});
test('v1 modem frames still decode (backward compat)', () => {
  const packets = dsp.modemPackets('v1 still decodes');
  const r = dsp.MODEM_RATES.normal;
  const sr = 44100;
  const step = Math.round((r.tone_s + r.gap_s) * sr);
  const samples = [];
  for (const p of packets) {
    for (const f of dsp.modemPacketTones(p, 'normal')) {
      for (let i = 0; i < step; i++) {
        samples.push(Math.sin(2 * Math.PI * f * i / sr) * 0.8);
      }
    }
  }
  const dec = dsp.decodeModem(samples, sr, 'normal');
  assert.equal(dec.text, 'v1 still decodes');
});
test('honest failure: 48k audio decoded as 44.1k does NOT fake success', () => {
  const packets = dsp.modemPackets('drift test');
  const r = dsp.MODEM_RATES.normal;
  const sr = 44100;
  const step = Math.round((r.tone_s + r.gap_s) * sr);
  let samples = [];
  for (const p of packets) {
    for (const f of dsp.modemPacketTones(p, 'normal')) {
      for (let i = 0; i < step; i++) {
        samples.push(Math.sin(2 * Math.PI * f * i / sr) * 0.8);
      }
    }
  }
  samples = dsp.resampleLinear(samples, sr, 48000);
  const dec = dsp.decodeModem(samples, 44100, 'normal');
  assert.notEqual(dec.text, 'drift test');
});
test('morse: Farnsworth timeline at 15/10 has correct dot length', () => {
  const tl = dsp.morseTimelineFarnsworth(dsp.textToMorse('PARIS'),
                                         { wpm: 15, fwpm: 10 });
  const dot = tl.find(e => e.sym === '.');
  assert.ok(dot);
  const ms = Math.round(dot.len * 1000);
  assert.ok(ms >= 60 && ms <= 90, `dot ${ms}ms out of range`);
});

/* ---------- shipped-module syntax (the v1.3.0 rule) ---------- */
test('every shipped JS module passes node --check', () => {
  const files = [];
  const walk = (dir) => {
    for (const f of readdirSync(dir, { withFileTypes: true })) {
      const p = dir + '/' + f.name;
      if (f.isDirectory()) walk(p);
      else if (f.name.endsWith('.js')) files.push(p);
    }
  };
  walk(root + 'web');
  assert.ok(files.length >= 25, `only ${files.length} JS files found`);
  for (const f of files) {
    execFileSync('node', ['--check', f], { stdio: 'pipe' });
  }
});

/* ---------- view wiring ---------- */
test('every desktop view file exports render', () => {
  const views = readdirSync(root + 'web/js/views')
    .filter(f => f.endsWith('.js'));
  assert.ok(views.includes('voice.js') && views.includes('broadcast.js') &&
    views.includes('media.js') && views.includes('clipboard.js') &&
    views.includes('mesh.js') && views.includes('signal.js'),
    'v1.4.0 views missing');
  for (const f of views) {
    const src = readFileSync(root + 'web/js/views/' + f, 'utf8');
    assert.ok(src.includes('export async function render'),
              `${f} does not export render`);
  }
});
test('desktop nav + app registry include the v1.4.0 views', () => {
  const app = readFileSync(root + 'web/js/app.js', 'utf8');
  for (const v of ['voice', 'broadcast', 'media', 'clipboard', 'mesh']) {
    assert.ok(app.includes(`${v}: { title:`), `app.js missing view ${v}`);
  }
  const html = readFileSync(root + 'web/index.html', 'utf8');
  for (const v of ['voice', 'broadcast', 'media', 'clipboard', 'mesh']) {
    assert.ok(html.includes(`data-view="${v}"`), `nav missing ${v}`);
  }
});
test('mobile app registers voice/media/clip tabs + SSE voice events', () => {
  const m = readFileSync(root + 'web/js/mobile/app.js', 'utf8');
  assert.ok(m.includes('renderVoice'), 'mobile voice tab missing');
  assert.ok(m.includes('renderMedia'), 'mobile media tab missing');
  assert.ok(m.includes('renderClip'), 'mobile clip tab missing');
  assert.ok(m.includes("data.type === 'voice'"), 'voice SSE handling missing');
  const html = readFileSync(root + 'web/mobile.html', 'utf8');
  for (const t of ['voice', 'media', 'clip']) {
    assert.ok(html.includes(`data-tab="${t}"`), `mobile tab ${t} missing`);
  }
});
test('service worker version bumped + precaches new views', () => {
  const sw = readFileSync(root + 'web/sw.js', 'utf8');
  assert.ok(/PN_SW_VERSION = 'v1\.4\.\d+(\.\d+)?'/.test(sw),
            'SW version not bumped');
  assert.ok(sw.includes('/js/views/voice.js'));
  assert.ok(sw.includes('/js/lib/rtc.js'));
});
test('SSE client subscribes the v1.4.0 topics', () => {
  const api = readFileSync(root + 'web/js/api.js', 'utf8');
  for (const t of ['voice', 'broadcast', 'media_event', 'clipboard']) {
    assert.ok(api.includes(`'${t}'`), `api.js missing topic ${t}`);
  }
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
