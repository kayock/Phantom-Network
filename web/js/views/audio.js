/* PHANTOM NETWORK — audio.js (Audio Command: remote playback) */

import { api } from '../api.js';
import { state } from '../state.js';
import {
  el, toast, fmtSize, errText, deviceIcon,
} from '../ui.js';
import { playSfx } from '../lib/sfx.js';

const PRESET_META = {
  red_alert: { label: 'Red alert', ico: '🚨' },
  incoming_transmission: { label: 'Incoming transmission', ico: '📡' },
  device_connected: { label: 'Device connected', ico: '🔌' },
  device_disconnected: { label: 'Device disconnected', ico: '📉' },
  transfer_complete: { label: 'Transfer complete', ico: '✅' },
  warning: { label: 'Warning', ico: '⚠️' },
  morse_notification: { label: 'Morse notification', ico: '·−·' },
  scifi_chirp: { label: 'Sci-fi computer chirp', ico: '🛰' },
};

export async function render(container) {
  const prefs = state.loadPrefs();
  let target = prefs.audio_target || 'self';
  if (!state.devices().find(d => d.device_id === target)) target = 'self';
  let status = null;

  const deviceSel = el('select', { style: { maxWidth: '240px' }, onchange: () => { target = deviceSel.value; prefs.audio_target = target; state.savePrefs(); refresh(); } });
  const statusLine = el('div', { class: 'dim small mono' }, '—');

  const devPanel = el('div', { class: 'panel' },
    el('h3', {}, 'Target device'),
    el('div', { class: 'row' }, deviceSel, statusLine,
      el('span', { class: 'grow' }),
      el('button', { class: 'btn small danger', onclick: async () => {
        try { await api.audioStop(target); toast('Playback stopped', 'ok'); refresh(); }
        catch (e) { toast(errText(e), 'err'); }
      } }, '■ Stop playback')));

  // local enable/disable on the TARGET device
  const enableBox = el('input', { type: 'checkbox', onchange: async () => {
    try {
      await api.audioEnabled(target, enableBox.checked);
      toast(`Remote audio ${enableBox.checked ? 'enabled' : 'disabled'} on ${state.displayName(target)}`, 'ok');
      refresh();
    } catch (e) { toast(errText(e), 'err'); }
  } });

  /* ---- tone generator ---- */
  const freqSlider = el('input', { type: 'range', min: '20', max: '20000', value: '440', style: { width: '220px' } });
  const freqNum = el('span', { class: 'vu' }, '440 Hz');
  freqSlider.addEventListener('input', () => { freqNum.textContent = `${freqSlider.value} Hz`; });
  const waveSel = el('select', {}, ['sine', 'square', 'triangle', 'sawtooth'].map(w =>
    el('option', { value: w }, w)));
  const durInput = el('input', { type: 'number', value: '1.5', min: '0.1', max: '30', step: '0.1', style: { width: '90px' } });
  const volSlider = el('input', { type: 'range', min: '0', max: '0.8', step: '0.05', value: '0.6', style: { width: '180px' } });
  const volNum = el('span', { class: 'vu' }, '0.60');
  volSlider.addEventListener('input', () => { volNum.textContent = Number(volSlider.value).toFixed(2); });

  const tonePanel = el('div', { class: 'panel' },
    el('h3', {}, 'Tone generator'),
    el('div', { class: 'fld-row' },
      el('label', { class: 'fld' }, 'Frequency', freqSlider, freqNum),
      el('label', { class: 'fld' }, 'Waveform', waveSel),
      el('label', { class: 'fld' }, 'Duration (s, max 30)', durInput),
      el('label', { class: 'fld' }, 'Volume (0–0.8)', volSlider, volNum),
      el('button', { class: 'btn primary', onclick: playTone }, '▶ Play on device'),
      el('button', { class: 'btn', onclick: () => playSfx({ freq: Number(freqSlider.value),
        duration: Math.min(2, Number(durInput.value) || 1), type: waveSel.value,
        volume: Number(volSlider.value) }) }, '▷ Preview here')));

  async function playTone() {
    const peer = state.peerById(target);
    if (peer && peer.audio_enabled === false) {
      toast('Remote audio is switched OFF on that device (enable it there first)', 'warn', 6000);
      return;
    }
    try {
      const r = await api.audioTone(target, Number(freqSlider.value),
        Number(durInput.value), waveSel.value, Number(volSlider.value));
      toast(`Tone generated & handed to the player on ${state.displayName(target)}`, 'ok');
      refresh();
    } catch (e) { toast(errText(e), 'err', 5000); }
  }

  /* ---- presets ---- */
  const presetGrid = el('div', { class: 'preset-grid' });
  const presetPanel = el('div', { class: 'panel' },
    el('h3', {}, 'Original sound presets'),
    presetGrid,
    el('div', { class: 'dim small', style: { marginTop: '8px' } },
      'All presets are synthesized on the companion — original Phantom Network audio, no copyrighted assets.'));

  for (const [name, meta] of Object.entries(PRESET_META)) {
    presetGrid.appendChild(el('div', { class: 'preset-card', onclick: async () => {
      try {
        await api.audioPreset(target, name, 0.7);
        toast(`${meta.label} → ${state.displayName(target)}`, 'ok');
        refresh();
      } catch (e) { toast(errText(e), 'err', 5000); }
    } },
      el('div', { class: 'p-ico' }, meta.ico),
      el('div', { class: 'p-name' }, meta.label),
      el('div', { class: 'faint small mono' }, name)));
  }

  /* ---- audio files ---- */
  const fileInput = el('input', { type: 'file', accept: 'audio/*', style: { display: 'none' } });
  const filePanel = el('div', { class: 'panel' },
    el('h3', {}, 'Play an audio file on the device'),
    el('div', { class: 'row' },
      el('button', { class: 'btn', onclick: () => fileInput.click() }, ' ⇧ Choose file from this computer'),
      el('span', { class: 'dim small' }, 'WAV recommended (Windows SoundPlayer); mp3/ogg/flac where paplay/ffplay exists. Duration capped for safety.')));
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files[0];
    if (!file) return;
    toast(`Uploading ${file.name} (${fmtSize(file.size)})…`);
    try {
      const fin = await api.uploadFile(target, '~', file);
      const dir = fin.target.replace(/[\\/][^\\/]+$/, '');
      await api.post(target, '/api/audio/file', { path: fin.target });
      toast(`Playing ${file.name} on ${state.displayName(target)}`, 'ok');
      refresh();
    } catch (e) { toast(errText(e), 'err', 6000); }
  });

  /* ---- audible notification ---- */
  const notifyPanel = el('div', { class: 'panel' },
    el('h3', {}, 'Send audible notification'),
    el('div', { class: 'row' },
      el('button', { class: 'btn cyan', onclick: () => fire('device_connected') }, '🔔 Device connected chime'),
      el('button', { class: 'btn cyan', onclick: () => fire('morse_notification') }, '🔔 Morse ping')));

  async function fire(name) {
    try { await api.audioPreset(target, name, 0.6); toast(`Notification sent to ${state.displayName(target)}`, 'ok'); }
    catch (e) { toast(errText(e), 'err'); }
  }

  async function refresh() {
    // devices
    deviceSel.innerHTML = '';
    for (const d of state.devices()) {
      deviceSel.appendChild(el('option', { value: d.device_id }, d.display_name));
    }
    deviceSel.value = target;
    try {
      status = await api.audioStatus(target);
      const cur = status.current;
      // honest playback lifecycle: generated → accepted by companion →
      // handed to a player process. Whether sound physically came out of a
      // speaker can only be verified by a human — the UI never claims it.
      statusLine.textContent = status.playing
        ? `▶ handed to player "${status.player}": ${cur?.label} — if the device has speakers, this should be audible`
        : `idle · player: ${status.player} · ${status.enabled ? 'remote audio ENABLED' : 'remote audio DISABLED'}`;
      statusLine.className = 'dim small mono ' + (status.enabled ? '' : 'faint');
      enableBox.checked = !!status.enabled;
      const peer = state.peerById(target);
      if (peer && peer.audio_enabled === false) {
        statusLine.textContent += ' · device advertises remote audio OFF';
      }
    } catch (e) {
      statusLine.textContent = 'device unreachable';
    }
  }

  /* ---- Media Party (v1.3.0) ---- */
  const partyList = el('div', {});
  const partyPreset = el('select', {}, Object.entries(PRESET_META).map(([n, m]) =>
    el('option', { value: n }, `${m.ico} ${m.label}`)));
  const partyStatus = el('div', { class: 'dim small' },
    'Pick computers, choose a sound, launch — every connected browser schedules the same instant.');
  async function drawParty() {
    partyList.innerHTML = '';
    const boxes = [];
    for (const d of state.devices()) {
      if (d.self || d.kind === 'mobile') continue;
      const peer = state.peerById(d.device_id);
      const online = peer?.status === 'online';
      const box = el('input', { type: 'checkbox', disabled: !online });
      box.checked = online;
      boxes.push({ box, id: d.device_id, name: d.display_name });
      partyList.appendChild(el('label', { class: 'row small', style: { gap: '8px', opacity: online ? 1 : .45 } },
        box, `${online ? '🖥️' : '💤'} ${d.display_name}${online ? '' : ' (offline)'}`));
    }
    partyList._boxes = boxes;
  }
  const launchParty = async () => {
    const boxes = (partyList._boxes || []).filter(b => b.box.checked);
    if (!boxes.length) { toast('Select at least one computer', 'warn'); return; }
    const playAt = Date.now() + 3000;
    const spec = {
      kind: 'preset', name: partyPreset.value,
      volume: 0.6, play_at: playAt,
      party_id: 'party_' + Math.random().toString(16).slice(2, 10),
    };
    let ok = 0;
    for (const b of boxes) {
      try { await api.mediaParty(b.id, spec); ok++; }
      catch (e) { toast(`${b.name}: ${errText(e)}`, 'err'); }
    }
    partyStatus.textContent =
      `launched on ${ok}/${boxes.length} companion(s) · play_at ${new Date(playAt).toLocaleTimeString()} · ` +
      `each browser schedules against its measured clock offset — expect ±0.1–0.3 s drift, not sample-accuracy`;
    toast(`Media Party launched on ${ok} device(s)`, 'ok');
  };
  const stopParty = async () => {
    const boxes = (partyList._boxes || []).filter(b => b.box.checked);
    for (const b of boxes) { api.mediaPartyStop(b.id).catch(() => {}); api.audioStop(b.id).catch(() => {}); }
    partyStatus.textContent = 'stop sent to all selected devices';
  };
  const partyPanel = el('div', { class: 'panel' },
    el('h3', {}, '🎉 Media Party (group playback)'),
    el('div', { class: 'fld-row' },
      el('label', { class: 'fld' }, 'Sound', partyPreset),
      el('button', { class: 'btn primary', onclick: launchParty }, '🎉 Launch in 3 s'),
      el('button', { class: 'btn danger', onclick: stopParty }, '■ Stop all')),
    partyList,
    partyStatus,
    el('div', { class: 'dim small mt' },
      'How it works: each participating companion publishes the scheduled command to its own browsers over SSE; ',
      'each browser measures its clock offset against its companion and plays locally at the common instant. ',
      'Playback respects the existing audio participation controls, the 0.8 amplitude cap and the 30 s limit.'));

  container.appendChild(devPanel);
  devPanel.appendChild(el('div', { class: 'row mt' },
    el('label', { class: 'row small', style: { gap: '8px' } },
      enableBox,
      el('span', {}, `Allow remote audio playback on ${state.displayName(target)} (visible control on this device)`))));
  container.appendChild(partyPanel);
  container.appendChild(tonePanel);
  container.appendChild(presetPanel);
  container.appendChild(filePanel);
  container.appendChild(notifyPanel);
  container.appendChild(fileInput);
  container.appendChild(el('div', { class: 'note' },
    'Safety: volume is amplitude-capped at 0.8, duration at 30 s, frequencies restricted to audible range (20 Hz–20 kHz). ',
    'No ultrasonic or covert signals are generated. The remote-audio switch lives on every participating device.'));

  await refresh();
  await drawParty();
  const t = setInterval(refresh, 4000);
  const t2 = setInterval(drawParty, 8000);
  return () => { clearInterval(t); clearInterval(t2); };
}
