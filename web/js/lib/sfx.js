/* ============================================================
   PHANTOM NETWORK — lib/sfx.js
   Browser-side Web Audio synth: local previews, UI sounds,
   shared by Audio Command + Signal Lab.
   ============================================================ */

let ctx = null;

export function audioCtx() {
  if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
  if (ctx.state === 'suspended') ctx.resume();
  return ctx;
}

export function playTone({ freq = 440, duration = 0.5, type = 'sine',
                           volume = 0.6, when = 0 } = {}) {
  const ac = audioCtx();
  const t0 = ac.currentTime + when;
  const osc = ac.createOscillator();
  const gain = ac.createGain();
  osc.type = type;
  osc.frequency.value = Math.min(20000, Math.max(20, freq));
  const vol = Math.max(0, Math.min(0.8, volume));
  // 5 ms fade in/out to avoid clicks
  gain.gain.setValueAtTime(0.0001, t0);
  gain.gain.exponentialRampToValueAtTime(Math.max(0.0002, vol), t0 + 0.005);
  gain.gain.setValueAtTime(vol, t0 + Math.max(0.005, duration - 0.005));
  gain.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
  osc.connect(gain).connect(ac.destination);
  osc.start(t0);
  osc.stop(t0 + duration + 0.02);
  return osc;
}

export function playSfx({ freq, duration, type, volume } = {}) {
  return playTone({ freq, duration, type, volume });
}

/* Browser-side preset synth (mirrors companion PRESETS tables) */
const PRESETS = {
  red_alert: [[440,.35,'sine',.8],[622.25,.35,'sine',.8],[440,.35,'sine',.8],[622.25,.35,'sine',.8],[415.30,.6,'sine',.8],[440,.6,'sine',.8]],
  incoming_transmission: [[880,.09,'sine',.5],[1174.66,.09,'sine',.5],[880,.09,'sine',.5],[1174.66,.14,'sine',.6],[987.77,.30,'triangle',.4]],
  device_connected: [[523.25,.10,'triangle',.5],[659.25,.10,'triangle',.55],[783.99,.16,'triangle',.6]],
  device_disconnected: [[659.25,.12,'triangle',.5],[523.25,.12,'triangle',.5],[392,.22,'triangle',.45]],
  transfer_complete: [[783.99,.09,'sine',.5],[987.77,.09,'sine',.55],[1174.66,.09,'sine',.6],[1567.98,.22,'sine',.65]],
  warning: [[466.16,.28,'square',.30],[0,.10,'sine',0],[466.16,.28,'square',.30]],
  morse_notification: [[1000,.08,'sine',.6],[0,.08,'sine',0],[1000,.24,'sine',.6],[0,.12,'sine',0],[1000,.08,'sine',.6]],
  scifi_chirp: [[600,.05,'sawtooth',.35],[900,.05,'sawtooth',.40],[1350,.05,'sawtooth',.45],[2025,.09,'sine',.5],[50,.16,'sine',0]],
};

export function playPreset(name, volume = 0.7) {
  const notes = PRESETS[name];
  if (!notes) return;
  let t = 0;
  for (const [freq, dur, type, vol] of notes) {
    if (freq > 0) playTone({ freq, duration: dur, type, volume: vol * (volume / 0.8) });
    t += dur;
  }
  return t;
}
