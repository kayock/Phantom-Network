/* PHANTOM NETWORK — intercom.js (opt-in push-to-talk)

   Audio path: browser mic (16 kHz Int16 PCM) → local companion
   /api/intercom/talk → peer companions /api/intercom/relay → SSE
   'intercom' events → peer browsers decode + play through a jitter buffer.
   Capture happens ONLY while the PTT button is held.
   */

import { api } from '../api.js';
import { state } from '../state.js';
import { el, toast, errText, fmtSize } from '../ui.js';

const SAMPLE_RATE = 16000;
const CHUNK_MS = 60;

export async function render(container) {
  let session = null;
  let talking = false;
  let micStream = null;
  let processorNode = null;
  let audioCtxNode = null;
  let playCtx = null;
  let seq = 0;
  let sendBuffer = null;
  let sendPos = 0;
  let recvQueue = [];
  let jitterTimer = null;

  /* secure context check — honest about browser limits */
  const secure = window.isSecureContext;
  const micLikelyOK = secure || location.hostname === 'localhost' || location.hostname === '127.0.0.1';

  const sessionInfo = el('div', { class: 'dim small mono' }, 'no active session');
  const targetSel = el('select', { multiple: true, style: { width: '100%', minHeight: '90px' } });
  const statusEl = el('div', { class: 'row', style: { marginTop: '8px' } });

  const ptt = el('button', { class: 'ptt-btn' }, 'HOLD TO\nTALK');
  ptt.innerHTML = 'HOLD TO<br>TALK';

  /* ---------------- UI ---------------- */
  const setupPanel = el('div', { class: 'panel' },
    el('h3', {}, '1 · Choose participants'),
    el('div', { class: 'dim small', style: { marginBottom: '6px' } },
      'Hold Ctrl/Cmd to select several devices (group announcement). Your own device is always included.'),
    targetSel,
    el('div', { class: 'row mt' },
      el('button', { class: 'btn primary', onclick: startSession }, 'Open intercom session'),
      el('button', { class: 'btn danger', onclick: endSession }, 'End session'),
      sessionInfo));

  const talkPanel = el('div', { class: 'panel' },
    el('h3', {}, '2 · Push to talk'),
    el('div', { class: 'row', style: { alignItems: 'flex-start' } },
      ptt,
      el('div', { class: 'grow' },
        el('div', { class: 'note', id: 'mic-note' }, micNoteText()),
        statusEl,
        el('div', { class: 'row mt' },
          el('label', { class: 'fld' }, 'Speaker volume',
            el('input', { type: 'range', id: 'icom-vol', min: '0', max: '1', step: '0.05', value: '0.9', style: { width: '200px' } })),
          el('button', { class: 'btn small', onclick: muteAll }, '🔇 Mute playback')))));

  function micNoteText() {
    if (micLikelyOK) {
      return 'Browser microphone capture requires your permission and happens ONLY while the ' +
        'push-to-talk button is held. Nothing is recorded or stored — audio is relayed live and dropped.';
    }
    return '⚠ This page is NOT a secure context (' + location.origin + '), so the browser will BLOCK ' +
      'microphone access on this device. Fixes: open the UI via localhost, use the companion HTTPS mode ' +
      '(docs/DEPLOYMENT.md §TLS), or (Chrome, trusted LAN only) add this origin to ' +
      'chrome://flags/#unsafely-treat-insecure-origin-as-secure. Mobile browsers additionally require ' +
      'HTTPS for getUserMedia — iPhone Safari will not open the mic over plain HTTP LAN addresses.';
  }

  function refreshTargets() {
    targetSel.innerHTML = '';
    const devices = state.devices().filter(d => !d.self);
    for (const d of devices) targetSel.appendChild(el('option', { value: d.device_id }, d.display_name));
    if (!devices.length) {
      targetSel.appendChild(el('option', { value: '', disabled: true }, '— no other devices online —'));
    }
  }

  async function startSession() {
    const participants = [...targetSel.selectedOptions].map(o => o.value).filter(Boolean);
    if (!participants.length) { toast('Select at least one participant', 'warn'); return; }
    try {
      const res = await api.intercomStart(participants, 'session');
      if (!res.ok) { toast(res.error, 'err'); return; }
      session = res.session;
      seq = 0;
      paintStatus();
      toast(`Intercom session open with ${participants.length} device(s)`, 'ok');
    } catch (e) { toast(errText(e), 'err'); }
  }

  async function endSession() {
    stopCapture();
    if (session) { try { await api.intercomEnd(session.id); } catch {} }
    session = null;
    paintStatus();
  }

  function paintStatus() {
    sessionInfo.textContent = session
      ? `session ${session.id} · members: ${session.members.map(m => m.name).join(', ')}`
      : 'no active session';
    statusEl.innerHTML = '';
    if (!session) {
      statusEl.appendChild(el('span', { class: 'badge off' }, 'NO SESSION'));
    } else {
      statusEl.appendChild(el('span', { class: 'badge vio' }, 'SESSION OPEN'));
      statusEl.appendChild(el('span', { class: 'badge ' + (talking ? 'err' : 'on') },
        talking ? '🎤 MIC LIVE (PTT held)' : 'mic idle'));
      statusEl.appendChild(el('span', { class: 'badge' }, `rx queue: ${recvQueue.length}`));
    }
    ptt.classList.toggle('ready', !!session && !talking);
  }

  /* ---------------- capture (PTT) ---------------- */
  ptt.addEventListener('pointerdown', async (e) => {
    e.preventDefault();
    if (!session) { toast('Open a session first', 'warn'); return; }
    if (!micLikelyOK) { toast('Microphone blocked by browser (insecure context) — see note', 'err', 6000); return; }
    await beginTalk();
  });
  ['pointerup', 'pointerleave', 'pointercancel'].forEach(ev =>
    ptt.addEventListener(ev, () => endTalk()));
  window.addEventListener('keydown', (e) => {
    if (e.code === 'Space' && session && !talking && document.activeElement.tagName !== 'TEXTAREA'
        && document.activeElement.tagName !== 'INPUT' && currentView()) { e.preventDefault(); beginTalk(); }
  });
  window.addEventListener('keyup', (e) => {
    if (e.code === 'Space' && talking) endTalk();
  });
  function currentView() { return location.hash.includes('intercom'); }

  /* input/output device selection (v1.3.0) */
  async function buildDevicePickers(containerRef) {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const mics = devices.filter(d => d.kind === 'audioinput');
      const outs = devices.filter(d => d.kind === 'audiooutput');
      const micSel = el('select', { id: 'icom-mic', style: { maxWidth: '220px' } },
        el('option', { value: '' }, 'default microphone'),
        ...mics.map(m => el('option', { value: m.deviceId }, m.label || `mic ${m.deviceId.slice(0, 8)}`)));
      const outSel = el('select', { id: 'icom-out', style: { maxWidth: '220px' } },
        el('option', { value: '' }, 'default output'),
        ...outs.map(o => el('option', { value: o.deviceId }, o.label || `out ${o.deviceId.slice(0, 8)}`)));
      outSel.addEventListener('change', () => {
        document.querySelectorAll('audio').forEach(a => {
          try { if (outSel.value) a.setSinkId(outSel.value); } catch { /* not supported */ }
        });
      });
      containerRef.appendChild(el('div', { class: 'fld-row' },
        el('label', { class: 'fld' }, 'Microphone', micSel),
        el('label', { class: 'fld' }, 'Output', outSel),
        el('div', { class: 'dim small' },
          'Labels appear after granting mic permission once. Output selection uses setSinkId (Chromium; Safari falls back to the system default).')));
    } catch { /* enumerateDevices unavailable */ }
  }
  buildDevicePickers(container);

  async function beginTalk() {
    if (talking || !session) return;
    try {
      const audioConstraints = { channelCount: 1, echoCancellation: true,
                                 noiseSuppression: true, autoGainControl: true,
                                 sampleRate: SAMPLE_RATE };
      const micSel = document.getElementById('icom-mic');
      if (micSel && micSel.value) audioConstraints.deviceId = micSel.value;
      micStream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });
    } catch (e) {
      toast('Microphone unavailable: ' + errText(e) + ' — check permission/secure context', 'err', 7000);
      return;
    }
    talking = true;
    seq = 0;
    playCtx = playCtx || new (window.AudioContext || window.webkitAudioContext)();
    audioCtxNode = playCtx;
    const source = playCtx.createMediaStreamSource(micStream);
    processorNode = playCtx.createScriptProcessor(4096, 1, 1);
    sendBuffer = null; sendPos = 0;
    processorNode.onaudioprocess = (ev) => {
      if (!talking) return;
      // resample to 16 kHz mono Int16 via simple linear sampling
      const input = ev.inputBuffer.getChannelData(0);
      const ratio = input.length / SAMPLE_RATE / (ev.inputBuffer.duration);
      const out = downsampleInt16(input, ev.inputBuffer.sampleRate, SAMPLE_RATE);
      appendAndFlush(out);
    };
    source.connect(processorNode);
    processorNode.connect(playCtx.destination); // script processors need a sink; volume ~0
    paintStatus();
  }

  function endTalk() {
    if (!talking) return;
    talking = false;
    stopCapture();
    if (session) api.intercomStop(session.id).catch(() => {});
    paintStatus();
  }

  function stopCapture() {
    if (processorNode) { try { processorNode.disconnect(); } catch {} processorNode = null; }
    if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  }

  function downsampleInt16(f32, fromRate, toRate) {
    const ratio = fromRate / toRate;
    const n = Math.floor(f32.length / ratio);
    const out = new Int16Array(n);
    let last = 0;
    for (let i = 0; i < n; i++) {
      const idx = Math.floor(i * ratio);
      const v = Math.max(-1, Math.min(1, f32[idx]));
      out[i] = v < 0 ? v * 0x8000 : v * 0x7FFF;
      last = v;
    }
    return out;
  }

  const BYTES_PER_CHUNK = SAMPLE_RATE * 2 * (CHUNK_MS / 1000); // Int16 mono
  function appendAndFlush(int16) {
    const bytes = new Uint8Array(int16.buffer);
    if (!sendBuffer) sendBuffer = new Uint8Array(BYTES_PER_CHUNK);
    let srcPos = 0;
    while (srcPos < bytes.length) {
      const space = sendBuffer.length - sendPos;
      const take = Math.min(space, bytes.length - srcPos);
      sendBuffer.set(bytes.subarray(srcPos, srcPos + take), sendPos);
      sendPos += take;
      srcPos += take;
      if (sendPos >= sendBuffer.length) {
        flushChunk();
      }
    }
  }

  function flushChunk() {
    if (!session || sendPos === 0) { sendPos = 0; return; }
    const chunk = sendBuffer.subarray(0, sendPos);
    sendPos = 0;
    const b64 = bytesToB64(chunk);
    seq++;
    api.intercomTalk(session.id, b64, seq).catch(() => {});
  }

  function bytesToB64(u8) {
    let s = '';
    const CH = 0x8000;
    for (let i = 0; i < u8.length; i += CH) {
      s += String.fromCharCode.apply(null, u8.subarray(i, i + CH));
    }
    return btoa(s);
  }
  function b64ToBytes(b64) {
    const s = atob(b64);
    const out = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  }

  /* ---------------- playback (jitter buffer) ---------------- */
  const onIntercom = (chunk) => {
    if (!chunk || !chunk.pcm_b64) return;
    if (session && chunk.from === state.self?.device_id) return;  // never echo self
    recvQueue.push(chunk);
    if (recvQueue.length > 50) recvQueue.shift();
    ensureJitter();
  };
  window.addEventListener('pn-intercom', (e) => onIntercom(e.detail));

  function ensureJitter() {
    if (jitterTimer) return;
    jitterTimer = setInterval(() => {
      if (!recvQueue.length) return;
      const chunk = recvQueue.shift();
      try {
        const bytes = b64ToBytes(chunk.pcm_b64);
        const int16 = new Int16Array(bytes.buffer);
        const ac = playCtx || (playCtx = new (window.AudioContext || window.webkitAudioContext)());
        const buf = ac.createBuffer(1, int16.length, SAMPLE_RATE);
        const data = buf.getChannelData(0);
        for (let i = 0; i < int16.length; i++) data[i] = int16[i] / 32768;
        const src = ac.createBufferSource();
        const vol = Number(document.getElementById('icom-vol')?.value ?? 0.9);
        const gain = ac.createGain();
        gain.gain.value = vol;
        src.connect(gain).connect(ac.destination);
        src.start();
      } catch { /* drop bad chunk */ }
      paintStatus();
    }, 60);
  }

  function muteAll() {
    const vol = document.getElementById('icom-vol');
    if (vol) vol.value = '0';
    toast('Playback muted', 'ok', 1500);
  }

  /* ---------------- render ---------------- */
  refreshTargets();
  paintStatus();
  container.appendChild(el('div', { class: 'panel' },
    el('h3', {}, 'Phantom Intercom — opt-in voice'),
    el('div', { class: 'dim', style: { lineHeight: 1.7 } },
      'Computer-to-computer push-to-talk over the LAN. Phones can participate where their browser ' +
      'allows microphone capture (secure context required — see the note below). Audio is relayed ' +
      'live and never recorded. Microphone activation is always visible here.')));
  container.appendChild(setupPanel);
  container.appendChild(talkPanel);

  const t = setInterval(refreshTargets, 8000);
  return () => {
    clearInterval(t);
    stopCapture();
    endTalk();
    if (jitterTimer) clearInterval(jitterTimer);
    window.removeEventListener('pn-intercom', null);
  };
}
