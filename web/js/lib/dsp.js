/* ============================================================
   PHANTOM NETWORK — lib/dsp.js
   Signal laboratory DSP: Morse codec, FSK acoustic codec,
   Goertzel tone detection. Pure browser JS, no dependencies.

   Acoustic messaging design (documented in KNOWN_LIMITATIONS.md):
   - carrier bits at BIT1=1800 Hz ("1"), BIT0=1000 Hz ("0"),
     LEAD=2800 Hz, each tone 80 ms, 40 ms silence between bits
   - frame: LEAD (4 tones) + 16-bit length + payload (UTF-8) +
     CRC-16/CCITT + END marker
   - decode uses Goertzel power on both bit frequencies with a
     fixed decision threshold and reports bit confidence = ratio
     between best and worst tone power.
   ============================================================ */

export const MORSE = {
  A: '.-', B: '-...', C: '-.-.', D: '-..', E: '.', F: '..-.', G: '--.',
  H: '....', I: '..', J: '.---', K: '-.-', L: '.-..', M: '--', N: '-.',
  O: '---', P: '.--.', Q: '--.-', R: '.-.', S: '...', T: '-', U: '..-',
  V: '...-', W: '.--', X: '-..-', Y: '-.--', Z: '--..',
  0: '-----', 1: '.----', 2: '..---', 3: '...--', 4: '....-',
  5: '.....', 6: '-....', 7: '--...', 8: '---..', 9: '----.',
  '.': '.-.-.-', ',': '--..--', '?': '..--..', "'": '.----.',
  '!': '-.-.--', '/': '-..-.', '(': '-.--.', ')': '-.--.-',
  '&': '.-...', ':': '---...', ';': '-.-.-.', '=': '-...-',
  '+': '.-.-.', '-': '-....-', '_': '..--.-', '"': '.-..-.',
  '@': '.--.-.', ' ': '/',
};

const MORSE_REV = {};
for (const [ch, code] of Object.entries(MORSE)) MORSE_REV[code] = ch;

export function textToMorse(text) {
  const out = [];
  for (const ch of text.toUpperCase()) {
    const code = MORSE[ch];
    if (code) out.push(code);
    else if (ch === ' ') { /* handled by '/' */ }
  }
  // word separation: encode spaces by splitting on original spacing
  const words = text.toUpperCase().split(/\s+/);
  return words.map(w => [...w].map(c => MORSE[c] || '').filter(Boolean).join(' '))
              .filter(Boolean).join(' / ');
}

export function morseToText(morse) {
  return morse.trim().split(/\s*\/\s*|\s{3,}/).map(word =>
    word.trim().split(/\s+/).map(code => MORSE_REV[code] || '?').join('')
  ).join(' ').replace(/\s+/g, ' ').trim();
}

/* Play morse as audio.  dot=unit; returns total seconds. */
export function playMorse(morse, { unit = 0.09, freq = 700, volume = 0.5, playFn } = {}) {
  let t = 0;
  const q = unit;
  for (const ch of morse) {
    if (ch === '.') { playFn({ freq, duration: q, volume, when: t }); t += q + q; }
    else if (ch === '-') { playFn({ freq, duration: q * 3, volume, when: t }); t += q * 3 + q; }
    else if (ch === ' ') t += q * 2;          // letter gap (already +1 from symbol)
    else if (ch === '/') t += q * 4;          // word gap
  }
  return t;
}

/* Morse visual timeline: [{sym, start, len}] */
export function morseTimeline(morse, unit = 0.09) {
  const out = [];
  let t = 0;
  for (const ch of morse) {
    if (ch === '.') { out.push({ sym: '.', start: t, len: unit }); t += unit * 2; }
    else if (ch === '-') { out.push({ sym: '-', start: t, len: unit * 3 }); t += unit * 4; }
    else if (ch === ' ') t += unit * 2;
    else if (ch === '/') t += unit * 4;
  }
  return out;
}

/* ---------------- FSK acoustic messaging ---------------- */
const BIT0 = 1000, BIT1 = 1800, LEAD = 2800, END = 600;
const TONE_S = 0.08, GAP_S = 0.04;

function crc16(data) {
  let crc = 0xFFFF;
  for (const b of data) {
    crc ^= b;
    for (let i = 0; i < 8; i++) {
      crc = (crc & 1) ? (crc >> 1) ^ 0x8408 : crc >> 1;
    }
  }
  return crc & 0xFFFF;
}

export function encodeAcoustic(text) {
  /* Returns array of tone frequencies to transmit (one per symbol). */
  const bytes = new TextEncoder().encode(text.slice(0, 120));
  const len = [ (bytes.length >> 8) & 0xFF, bytes.length & 0xFF ];
  const crc = crc16(bytes);
  const frame = [0xA5, 0x5A, ...len, ...bytes,
                 (crc >> 8) & 0xFF, crc & 0xFF];
  const tones = [LEAD, LEAD, LEAD, LEAD];
  for (const b of frame) {
    for (let i = 7; i >= 0; i--) {
      tones.push((b >> i) & 1 ? BIT1 : BIT0);
    }
  }
  tones.push(END, END);
  return tones;
}

export function acousticDuration(tones) {
  return tones.length * (TONE_S + GAP_S);
}

export function playAcoustic(tones, volume = 0.6, playFn) {
  let t = 0;
  for (const f of tones) {
    playFn({ freq: f, duration: TONE_S, type: 'sine', volume, when: t });
    t += TONE_S + GAP_S;
  }
  return t;
}

/* Goertzel power at freq for a sample block */
export function goertzel(samples, freq, sampleRate) {
  const k = Math.round((freq * samples.length) / sampleRate);
  const w = 2 * Math.PI * k / samples.length;
  const coeff = 2 * Math.cos(w);
  let s1 = 0, s2 = 0;
  for (const x of samples) {
    const s0 = x + coeff * s1 - s2;
    s2 = s1; s1 = s0;
  }
  return s1 * s1 + s2 * s2 - coeff * s1 * s2;
}

/* Decode a recorded Float32 mono buffer at sampleRate.
   Returns { text, confidence, bits, errors } — never fabricates success:
   confidence < threshold yields text:'' and an error note. */
export function decodeAcoustic(samples, sampleRate = 44100) {
  const result = { text: '', confidence: 0, bits: 0, errors: [], raw_bits: [] };
  const symbol = TONE_S * sampleRate;
  const step = TONE_S * sampleRate + GAP_S * sampleRate;

  // 1) find lead-in: first window whose LEAD power dominates
  let startIdx = -1;
  for (let i = 0; i + symbol < samples.length; i += symbol / 2) {
    const win = samples.slice(i, i + symbol);
    const lead = goertzel(win, LEAD, sampleRate);
    const noise = goertzel(win, BIT1, sampleRate) + goertzel(win, BIT0, sampleRate);
    if (lead > noise * 3 && lead > 1e-4) { startIdx = i; break; }
  }
  if (startIdx < 0) {
    result.errors.push('no lead tone found — is the microphone hearing the signal?');
    return result;
  }
  // skip the 4 lead tones
  let idx = startIdx + 4 * step;

  // 2) collect bits until END detected or data exhausted
  const bits = [];
  let endSeen = false;
  while (idx + symbol <= samples.length && !endSeen) {
    const win = samples.slice(idx, idx + symbol);
    const p1 = goertzel(win, BIT1, sampleRate);
    const p0 = goertzel(win, BIT0, sampleRate);
    const pe = goertzel(win, END, sampleRate);
    const pl = goertzel(win, LEAD, sampleRate);
    if (pe > p1 * 2 && pe > p0 * 2) { endSeen = true; break; }
    if (pl > p1 * 2 && pl > p0 * 2) { // repeat start (bad read) — bail
      result.errors.push('unexpected lead tone in payload');
      break;
    }
    const best = p1 > p0 ? 1 : 0;
    const power = Math.max(p1, p0);
    const other = Math.min(p1, p0);
    const conf = power > 0 ? Math.min(1, Math.log10(1 + power / (other + 1e-9)) / 2) : 0;
    bits.push({ bit: best, conf, power });
    idx += step;
  }
  result.bits = bits.length;
  if (!endSeen) result.errors.push('end marker not found (transmission cut off?)');
  if (bits.length < 32) {
    result.errors.push(`only ${bits.length} bits decoded — need ≥ 32`);
    return result;
  }

  result.raw_bits = bits.map(b => b.bit);
  const confs = bits.map(b => b.conf);
  result.confidence = confs.reduce((a, b) => a + b, 0) / confs.length;

  // 3) frame parse: 8+8 sync + 16 len + payload + 16 crc
  const readByte = (off) => {
    let v = 0;
    for (let i = 0; i < 8; i++) v = (v << 1) | bits[off + i].bit;
    return v;
  };
  if (readByte(0) !== 0xA5 || readByte(8) !== 0x5A) {
    result.errors.push('sync bytes mismatch — background noise corrupted the frame');
    return result;
  }
  const len = (readByte(16) << 8) | readByte(24);
  if (len > 120) {
    result.errors.push('length field implausible — frame corrupted');
    return result;
  }
  const need = 32 + len * 8 + 16;
  if (bits.length < need) {
    result.errors.push(`frame incomplete: have ${bits.length} bits, need ${need}`);
    return result;
  }
  const payload = [];
  for (let i = 0; i < len; i++) payload.push(readByte(32 + i * 8));
  const crcHi = readByte(32 + len * 8);
  const crcLo = readByte(32 + len * 8 + 8);
  const crc = (crcHi << 8) | crcLo;
  if (crc16(payload) !== crc) {
    result.errors.push(`CRC mismatch (got ${crc.toString(16)}, expected ${crc16(payload).toString(16)}) — decoded text unreliable, refusing to display`);
    return result;
  }
  if (result.confidence < 0.22) {
    result.errors.push(`average bit confidence ${result.confidence.toFixed(2)} below reliable threshold`);
    return result;
  }
  result.text = new TextDecoder().decode(new Uint8Array(payload));
  return result;
}

/* Offline roundtrip: simulate decode over tones with noise. */
export function offlineRoundtrip(text, { snr = 20, sampleRate = 44100 } = {}) {
  const tones = encodeAcoustic(text);
  // synthesize + decode entirely in JS (deterministic w/ seed)
  const total = Math.ceil(acousticDuration(tones) * sampleRate);
  const buf = new Float32Array(total);
  let seed = 12345;
  const rand = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff - 0.5; };
  const step = (TONE_S + GAP_S) * sampleRate;
  tones.forEach((f, i) => {
    const start = Math.floor(i * step);
    const len = Math.floor(TONE_S * sampleRate);
    for (let j = 0; j < len && start + j < total; j++) {
      const env = Math.min(1, j / (sampleRate * 0.005)) * Math.min(1, (len - j) / (sampleRate * 0.005));
      buf[start + j] += Math.sin(2 * Math.PI * f * j / sampleRate) * 0.6 * env;
    }
  });
  // add noise at requested SNR
  const signalPower = buf.reduce((a, x) => a + x * x, 0) / (buf.length || 1);
  const noisePower = signalPower / Math.pow(10, snr / 10);
  for (let i = 0; i < total; i++) buf[i] += rand() * Math.sqrt(noisePower * 3);
  const dec = decodeAcoustic(buf, sampleRate);
  return { expected: text, got: dec.text, ok: dec.text === text,
           confidence: dec.confidence, errors: dec.errors, snr };
}

/* Peak frequency of a spectrum array (fftBinHz resolution) */
export function peakFrequency(spectrum, binHz) {
  let maxI = -1, maxV = -Infinity;
  for (let i = 1; i < spectrum.length; i++) {
    if (spectrum[i] > maxV) { maxV = spectrum[i]; maxI = i; }
  }
  return maxI >= 0 ? maxI * binHz : null;
}
