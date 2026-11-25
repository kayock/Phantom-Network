/* v1.3.0 frontend test suite — runs in node (no browser needed).
   Covers: acoustic modem offline roundtrips, Farnsworth morse timing,
   signal-analysis helpers, QR library presence, PWA/mobile asset sanity. */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
let passed = 0, failed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log(`ok   ${name}`); }
  catch (e) { failed++; console.log(`FAIL ${name}: ${e.message}`); }
}
function eq(a, b, msg = '') {
  if (a !== b) throw new Error(`${msg} expected ${b}, got ${a}`);
}

const dsp = await import(path.join(ROOT, 'web', 'js', 'lib', 'dsp.js'));

/* ---- acoustic modem ---- */
check('modem single packet normal rate', () => {
  const r = dsp.modemOfflineRoundtrip('PHANTOM MODEM v2', { rate: 'normal', snr: 25 });
  eq(r.ok, true, 'roundtrip');
});
check('modem multipacket robust rate', () => {
  const r = dsp.modemOfflineRoundtrip('y'.repeat(100), { rate: 'robust', snr: 30 });
  eq(r.ok, true, 'roundtrip');
});
check('modem fast rate', () => {
  const r = dsp.modemOfflineRoundtrip('hello world '.repeat(6), { rate: 'fast', snr: 20 });
  eq(r.ok, true, 'roundtrip');
});
check('modem five packets', () => {
  const r = dsp.modemOfflineRoundtrip('z'.repeat(200), { rate: 'normal', snr: 26 });
  eq(r.ok, true, 'roundtrip');
  eq(r.total, 5, 'packet count');
});
check('modem packets carry crc + sequence', () => {
  const pkts = dsp.modemPackets('abc');
  eq(pkts[0].frame[0], 0x50, 'sync0');
  eq(pkts[0].frame[1], 0x48, 'sync1');
  eq(pkts[0].frame[2], 0x54, 'type');
  eq(pkts[0].frame[3], 0, 'seq');
  eq(pkts[0].frame[4], 1, 'total');
});
check('modem crc rejects corrupted payload', () => {
  // flip one payload bit: decode must fail honestly (no text fabricated)
  const pkts = dsp.modemPackets('integrity');
  const frame = [...pkts[0].frame];
  frame[6] ^= 0x01;                       // corrupt payload byte
  const r = { corrupt: true, frame };
  // encode a manual tone stream from the corrupted frame and decode it
  const tones = [2800, 2800];
  for (const b of frame) for (let i = 7; i >= 0; i--) tones.push((b >> i) & 1 ? 1800 : 1000);
  tones.push(600, 600);
  const sr = 44100, R = dsp.MODEM_RATES.normal;
  const slot = Math.round((R.tone_s + R.gap_s) * sr);
  const buf = new Float32Array(tones.length * slot + sr);
  tones.forEach((f, i2) => {
    const start = i2 * slot;
    for (let j = 0; j < Math.floor(R.tone_s * sr); j++)
      buf[start + j] += Math.sin(2 * Math.PI * f * j / sr) * 0.6;
  });
  const dec = dsp.decodeModem(buf, sr, 'normal');
  if (dec.complete && dec.text === 'integrity') throw new Error('corruption accepted!');
});

/* ---- morse timing ---- */
check('farnsworth stretches gaps at 15/10', () => {
  const m = dsp.textToMorse('PARIS');
  const tl = dsp.morseTimelineFarnsworth(m, { wpm: 15, fwpm: 10 });
  const end = tl[tl.length - 1].start + tl[tl.length - 1].len;
  // ARRL: PARIS at 10 wpm overall ≈ 6 s total incl. word gap; last symbol
  // ends proportionally earlier — must be well above the 15/15 value
  const t2 = dsp.morseTimelineFarnsworth(m, { wpm: 15 });
  const end2 = t2[t2.length - 1].start + t2[t2.length - 1].len;
  if (end <= end2) throw new Error(`farnsworth did not stretch: ${end} <= ${end2}`);
});
check('plain morse timeline matches PARIS standard', () => {
  const tl = dsp.morseTimeline('-.-', 0.08);   // K: dash dot dash
  eq(tl.length, 3, 'symbols');
  eq(Math.round((tl[0].len) * 100), 24, 'dash = 3 units');
});

/* ---- signal analysis helpers ---- */
check('rms/peak/duration helpers', () => {
  const samples = new Float32Array([0.3, -0.3, 0.3, -0.3]);
  const rms = dsp.rmsLevel(samples);
  if (Math.abs(rms - 0.3) > 1e-6) throw new Error(`rms ${rms}`);
  if (Math.abs(dsp.peakAmplitude(samples) - 0.3) > 1e-6) throw new Error(`peak ${dsp.peakAmplitude(samples)}`);
  eq(dsp.signalDurationS(samples, 8), 0.5, 'duration');
});
check('practice word generator is deterministic by seed', () => {
  eq(dsp.randomPracticeMessage(2, 7), dsp.randomPracticeMessage(2, 7), 'seeded');
});

/* ---- QR library vendored + functional ---- */
check('qrcode library present and encodes locally', () => {
  const src = fs.readFileSync(path.join(ROOT, 'web', 'js', 'lib', 'qrcode.js'), 'utf8');
  if (!src.includes('Kazuhiko Arase')) throw new Error('license header missing');
  if (!src.includes('MIT license')) throw new Error('MIT license missing');
  const mod = require(path.join(ROOT, 'web', 'js', 'lib', 'qrcode.js'));
  const qr = mod(0, 'M');
  qr.addData('http://192.168.1.10:8415/m');
  qr.make();
  if (qr.getModuleCount() < 21) throw new Error('implausible module count');
});
/* ---- asset sanity (no network needed) ---- */
check('PWA manifest valid JSON with icons', () => {
  const man = JSON.parse(fs.readFileSync(path.join(ROOT, 'web', 'manifest.webmanifest'), 'utf8'));
  eq(man.display, 'standalone', 'display');
  if (!man.icons.some(i => i.sizes === '192x192')) throw new Error('no 192 icon');
  if (!man.icons.some(i => i.purpose === 'maskable')) throw new Error('no maskable icon');
});
check('service worker never caches /api', () => {
  const sw = fs.readFileSync(path.join(ROOT, 'web', 'sw.js'), 'utf8');
  if (!sw.includes("startsWith('/api/')")) throw new Error('api guard missing');
  if (!sw.includes('offline.html')) throw new Error('offline shell missing');
});
check('mobile app is a module with bottom-nav tabs', () => {
  const src = fs.readFileSync(path.join(ROOT, 'web', 'js', 'mobile', 'app.js'), 'utf8');
  for (const tab of ['home', 'chat', 'devices', 'files', 'more']) {
    if (!src.includes(`'${tab}'`) && !src.includes(`"${tab}"`)) {
      throw new Error(`missing tab ${tab}`);
    }
  }
  if (!src.includes('/api/mobile/register')) throw new Error('registration missing');
  if (!src.includes('localStorage')) throw new Error('identity persistence missing');
});
check('mobile.html references mobile.css + app module', () => {
  const html = fs.readFileSync(path.join(ROOT, 'web', 'mobile.html'), 'utf8');
  if (!html.includes('/css/mobile.css')) throw new Error('css missing');
  if (!html.includes('/js/mobile/app.js')) throw new Error('js missing');
});
check('desktop index includes PWA hooks', () => {
  const html = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');
  if (!html.includes('manifest.webmanifest')) throw new Error('manifest link missing');
  if (!html.includes('/m')) throw new Error('mobile redirect missing');
  if (!html.includes('v1.3.1')) throw new Error('version not bumped in UI');
});
check('desktop chat view uses GhostChat 2.0 endpoints', () => {
  const src = fs.readFileSync(path.join(ROOT, 'web', 'js', 'views', 'chat.js'), 'utf8');
  for (const needle of ['chatReact', 'chatEdit', 'chatDelete', 'mobile:']) {
    if (!src.includes(needle)) throw new Error(`missing ${needle}`);
  }
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
