/* ============================================================
   PHANTOM NETWORK — app.js
   Boot sequence, hash router, sidebar/topbar, SSE wiring.
   ============================================================ */

import { api, live } from './api.js';
import { state } from './state.js';
import { el, toast, errText, modal } from './ui.js';

const VIEWS = {
  dashboard: { title: 'Command Center', file: './views/dashboard.js' },
  radar: { title: 'Network Radar', file: './views/radar.js' },
  commander: { title: 'File Commander', file: './views/commander.js' },
  transfers: { title: 'Transfer Center', file: './views/transfers.js' },
  chat: { title: 'GhostChat', file: './views/chat.js' },
  audio: { title: 'Audio Command', file: './views/audio.js' },
  intercom: { title: 'Phantom Intercom', file: './views/intercom.js' },
  voice: { title: 'Phantom Voice', file: './views/voice.js' },
  broadcast: { title: 'Phantom Broadcast', file: './views/broadcast.js' },
  media: { title: 'Phantom Media', file: './views/media.js' },
  clipboard: { title: 'Phantom Clipboard', file: './views/clipboard.js' },
  mesh: { title: 'Phantom Mesh', file: './views/mesh.js' },
  signal: { title: 'Signal Laboratory', file: './views/signal.js' },
  fleet: { title: 'Fleet Monitor', file: './views/fleet.js' },
  events: { title: 'Event Center', file: './views/events.js' },
  settings: { title: 'Settings', file: './views/settings.js' },
  diagnostics: { title: 'Diagnostics', file: './views/diagnostics.js' },
};

let currentView = null;
let currentCleanup = null;

/* ---------------- boot ---------------- */
const bootBar = document.getElementById('boot-bar');
const bootErr = document.getElementById('boot-err');

async function boot() {
  bootBar.style.width = '30%';
  try {
    await state.loadSelf();
  } catch (e) {
    bootErr.textContent = `Cannot reach this companion: ${errText(e)}\n` +
      'Is the companion service running? Try: python -m companion';
    return;
  }
  bootBar.style.width = '55%';
  try { await state.loadConfig(); } catch { /* keep defaults */ }
  bootBar.style.width = '75%';
  try { await state.loadPeers(); } catch { /* offline ok */ }

  const chip = document.getElementById('self-chip');
  chip.innerHTML = '';
  chip.appendChild(el('div', {}, el('b', {}, state.self.name)));
  chip.appendChild(el('div', {}, `host: ${state.self.host}`));
  chip.appendChild(el('div', {}, `v${state.self.version} · ${state.self.os}`));

  live.connect();
  wireLive();
  wireShell();
  wirePalette();
  registerServiceWorker();
  bootBar.style.width = '100%';
  maybeFirstRunWizard();
  setTimeout(() => {
    document.getElementById('boot').remove();
    document.getElementById('app').classList.remove('hidden');
    window.addEventListener('hashchange', route);
    route();
    setInterval(refreshPeersLoop, 5000);
    clockTick();
    setInterval(clockTick, 1000);
  }, 250);
}

function clockTick() {
  const c = document.getElementById('status-clock');
  if (c) c.textContent = new Date().toTimeString().slice(0, 8);
}

async function refreshPeersLoop() {
  try { await state.loadPeers(); updatePeerPill(); } catch { /* companion busy */ }
}

function updatePeerPill() {
  const pill = document.getElementById('status-peers');
  const online = state.peers.filter(p => p.status === 'online' && p.participating).length;
  pill.textContent = `${online} device${online === 1 ? '' : 's'} online`;
  pill.className = 'status-pill ' + (online > 0 ? 'ok' : '');
}

/* ---------------- live stream wiring ---------------- */
function wireLive() {
  live.onStatus((up) => {
    const pill = document.getElementById('status-stream');
    pill.className = 'status-pill ' + (up === 'up' ? 'ok' : 'bad');
    pill.title = up === 'up' ? 'Live event stream connected' : 'Reconnecting…';
  });
  live.on('event', (ev) => {
    if (!ev) return;
    state.eventsSeen++;
    if (['device_connected', 'device_discovered', 'device_lost',
         'transfer_completed', 'transfer_failed', 'verification_failed',
         'companion_error'].includes(ev.type)) {
      state.loadPeers().then(updatePeerPill).catch(() => {});
    }
    // non-intrusive toasts for important happenings
    if (ev.type === 'transfer_completed' && ev.text) toast(ev.text, 'ok');
    if (ev.type === 'verification_failed') toast(ev.text, 'err', 6000);
    if (ev.type === 'companion_error') toast(ev.text, 'err', 6000);
    if (currentView === 'events' || currentView === 'dashboard') {
      window.dispatchEvent(new CustomEvent('pn-event', { detail: ev }));
    }
  });
  live.on('chat', (msg) => {
    if (!msg) return;
    if (msg.outgoing) return;
    state.chatUnread[msg.conv] = (state.chatUnread[msg.conv] || 0) + 1;
    updateChatBadge();
    if (currentView === 'chat') {
      window.dispatchEvent(new CustomEvent('pn-chat', { detail: msg }));
    } else if ('Notification' in window && Notification.permission === 'granted') {
      new Notification(`GhostChat — ${msg.from?.name || 'unknown'}`, {
        body: (msg.body || '[attachment]').slice(0, 120), tag: msg.id,
      });
    }
    window.dispatchEvent(new CustomEvent('pn-chat-any'));
  });
  live.on('transfer', () => {
    if (currentView === 'transfers' || currentView === 'dashboard') {
      window.dispatchEvent(new CustomEvent('pn-transfer'));
    }
  });
  live.on('intercom', (chunk) => {
    window.dispatchEvent(new CustomEvent('pn-intercom', { detail: chunk }));
  });
  live.on('mobile', (ev) => {
    // hosted mobile clients came online / registered / renamed
    state.loadPeers().then(updatePeerPill).catch(() => {});
  });
  live.on('alert', (alert) => {
    if (!alert) return;
    toast(`🔔 ${alert.text}`, alert.severity === 'error' ? 'err' : 'warn', 6000);
    if (alert.sound) playAlertCue();
    window.dispatchEvent(new CustomEvent('pn-alert', { detail: alert }));
  });
  live.on('media_party', (spec) => {
    window.dispatchEvent(new CustomEvent('pn-media-party', { detail: spec }));
  });
}

/* ---------------- Media Party local scheduler (v1.3.0) ----------------
   Every browser connected to a companion schedules the common play_at
   instant against its measured clock offset. Honest limits: ±0.1–0.3 s
   across typical LAN browsers; NOT sample-accurate. Volume is capped at
   the companion-side amplitude limit and playback is fully visible. */
const PARTY_SOUNDS = {
  red_alert: '/sounds/pn-red_alert.wav',
  incoming_transmission: '/sounds/pn-incoming_transmission.wav',
  device_connected: '/sounds/pn-device_connected.wav',
  device_disconnected: '/sounds/pn-device_disconnected.wav',
  transfer_complete: '/sounds/pn-transfer_complete.wav',
  warning: '/sounds/pn-warning.wav',
  morse_notification: '/sounds/pn-morse_notification.wav',
  scifi_chirp: '/sounds/pn-scifi_chirp.wav',
};
let partyOffsetMs = null;
let partyAudio = null;
let partyOsc = null;

async function measurePartyOffset() {
  const t0 = Date.now();
  try {
    const r = await fetch('/api/health', { cache: 'no-store' });
    const data = await r.json();
    const rtt = Date.now() - t0;
    partyOffsetMs = data.ts + Math.round(rtt / 2) - Date.now();
  } catch { partyOffsetMs = null; }
}
setInterval(measurePartyOffset, 30000);
measurePartyOffset();

window.addEventListener('pn-media-party', (ev) => {
  const spec = ev.detail || {};
  if (spec.kind === 'stop') {
    try { if (partyAudio) { partyAudio.pause(); partyAudio = null; } } catch {}
    try { if (partyOsc) { partyOsc.stop(); partyOsc = null; } } catch {}
    return;
  }
  if (!spec.play_at) return;
  const localAt = partyOffsetMs === null
    ? Date.now() + 2500                      // no offset measured: approximate
    : spec.play_at - partyOffsetMs;
  const delay = Math.max(0, localAt - Date.now());
  const vol = Math.min(0.8, Math.max(0, Number(spec.volume ?? 0.6)));
  setTimeout(() => {
    try {
      if (spec.kind === 'tone') {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.connect(g); g.connect(ctx.destination);
        o.type = spec.waveform || 'sine';
        o.frequency.value = Math.min(20000, Math.max(20, Number(spec.freq || 440)));
        g.gain.value = vol;
        o.start();
        partyOsc = o;
        setTimeout(() => { try { o.stop(); } catch {} }, Math.min(30000, (spec.duration || 1) * 1000));
      } else if (spec.kind === 'preset' && PARTY_SOUNDS[spec.name]) {
        partyAudio = new Audio(PARTY_SOUNDS[spec.name]);
        partyAudio.volume = vol;
        partyAudio.play().catch(() => { /* autoplay policy: user must interact once */ });
      }
    } catch { /* audio unavailable on this browser */ }
  }, delay);
  toast(`🎉 Media Party: ${spec.name || 'tone'} at ${new Date(spec.play_at).toLocaleTimeString()} (offset ${partyOffsetMs ?? '?'} ms)`, 'info');
});

/* short two-tone cue for alert rules that opted into sound */
let audioCtx2 = null;
function playAlertCue() {
  try {
    audioCtx2 = audioCtx2 || new (window.AudioContext || window.webkitAudioContext)();
    const now = audioCtx2.currentTime;
    [660, 990].forEach((freq, i) => {
      const o = audioCtx2.createOscillator();
      const g = audioCtx2.createGain();
      o.connect(g); g.connect(audioCtx2.destination);
      o.type = 'sine'; o.frequency.value = freq;
      g.gain.setValueAtTime(0.0001, now + i * 0.18);
      g.gain.exponentialRampToValueAtTime(0.15, now + i * 0.18 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, now + i * 0.18 + 0.16);
      o.start(now + i * 0.18); o.stop(now + i * 0.18 + 0.2);
    });
  } catch { /* audio unavailable */ }
}

/* ---------------- service worker (PWA) ---------------- */
function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('/sw.js').catch(() => {
    /* http origin or browser without SW: app keeps working normally */
  });
  navigator.serviceWorker.addEventListener('message', (ev) => {
    if (ev.data?.type === 'sw-updated') {
      toast('Phantom Network updated — reload to get the new version', 'ok', 8000);
    }
  });
}

export function updateChatBadge() {
  const badge = document.getElementById('chat-badge');
  const total = Object.values(state.chatUnread).reduce((a, b) => a + b, 0);
  badge.hidden = total === 0;
  badge.textContent = total > 99 ? '99+' : total;
}

/* ---------------- shell ---------------- */
function wireShell() {
  document.getElementById('nav').addEventListener('click', (e) => {
    const item = e.target.closest('.nav-item');
    if (!item) return;
    location.hash = '#/' + item.dataset.view;
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('sidebar-scrim').classList.add('hidden');
  });
  document.getElementById('menu-btn').addEventListener('click', () => {
    document.getElementById('sidebar').classList.toggle('open');
    document.getElementById('sidebar-scrim').classList.toggle('hidden');
  });
  document.getElementById('sidebar-scrim').addEventListener('click', () => {
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('sidebar-scrim').classList.add('hidden');
  });
}

/* ---------------- command palette (Ctrl+K / Cmd+K) ---------------- */
function wirePalette() {
  const layer = document.getElementById('palette-layer');
  const card = document.getElementById('palette-card');
  let items = [];
  let sel = 0;

  const open = () => {
    card.innerHTML = '';
    const input = el('input', { class: 'palette-input', placeholder: 'Search devices, views, conversations, commands…' });
    const list = el('div', { class: 'palette-list' });
    card.appendChild(input);
    card.appendChild(list);
    layer.classList.remove('hidden');
    input.focus();
    const draw = () => {
      list.innerHTML = '';
      items = buildItems(input.value);
      sel = Math.min(sel, Math.max(0, items.length - 1));
      items.forEach((item, i) => {
        const node = el('div', { class: 'palette-item' + (i === sel ? ' sel' : ''),
          onclick: () => { close(); item.run(); } },
          el('span', { class: 'pi-ico' }, item.icon),
          el('span', {}, item.label),
          el('span', { class: 'pi-hint' }, item.hint || ''));
        list.appendChild(node);
      });
      if (!items.length) list.appendChild(el('div', { class: 'palette-empty' }, 'No matches'));
    };
    input.addEventListener('input', draw);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') close();
      else if (e.key === 'ArrowDown') { sel = Math.min(sel + 1, items.length - 1); draw(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { sel = Math.max(sel - 1, 0); draw(); e.preventDefault(); }
      else if (e.key === 'Enter') { const it = items[sel]; if (it) { close(); it.run(); } }
    });
    draw();
  };
  const close = () => { layer.classList.add('hidden'); card.innerHTML = ''; };
  layer.addEventListener('mousedown', (e) => { if (e.target === layer) close(); });
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      if (layer.classList.contains('hidden')) open(); else close();
    }
  });

  function buildItems(q) {
    const needle = (q || '').toLowerCase().trim();
    const all = [];
    for (const [name, view] of Object.entries(VIEWS)) {
      all.push({ icon: '▤', label: 'Go to ' + view.title, hint: 'view', run: () => { location.hash = '#/' + name; } });
    }
    for (const p of state.peers) {
      all.push({
        icon: p.kind === 'mobile' ? '📱' : (p.os === 'windows' ? '🖥️' : '🐧'),
        label: p.display_name,
        hint: p.status + (p.kind === 'mobile' ? ' · mobile client' : ' · companion'),
        run: () => {
          location.hash = p.kind === 'mobile' ? '#/chat' : '#/radar';
          if (p.kind === 'mobile') toast(`Opening GhostChat — conversation with ${p.display_name}`, 'info');
        },
      });
      if (p.participating && p.status === 'online') {
        all.push({ icon: '◌', label: `Chat with ${p.display_name}`, hint: 'command',
          run: () => { location.hash = '#/chat'; setTimeout(() => window.dispatchEvent(new CustomEvent('pn-chat-open', { detail: p.device_id })), 400); } });
      }
    }
    all.push({ icon: '📱', label: 'Add Mobile Device (QR code)', hint: 'command', run: () => {
      location.hash = '#/settings';
      setTimeout(() => document.querySelector('.panel h3')?.scrollIntoView(), 300);
    } });
    all.push({ icon: '◉', label: 'Run subnet sweep (find devices)', hint: 'command', run: () => {
      api.radarSweep('').then(() => toast('Sweep started — see the Network Radar', 'ok')).catch(e => toast(errText(e), 'err'));
      location.hash = '#/radar';
    } });
    all.push({ icon: '✚', label: 'Run diagnostics self-test', hint: 'command', run: () => {
      location.hash = '#/diagnostics';
    } });
    all.push({ icon: '⇄', label: 'Open Transfer Center', hint: 'view', run: () => { location.hash = '#/transfers'; } });
    const filtered = needle
      ? all.filter(x => (x.label + ' ' + x.hint).toLowerCase().includes(needle))
      : all;
    return filtered.slice(0, 12);
  }
}

/* ---------------- router ---------------- */
async function route() {
  const name = (location.hash.replace(/^#\//, '') || 'dashboard').split('?')[0];
  const view = VIEWS[name] || VIEWS.dashboard;
  currentView = name;

  // nav highlight
  document.querySelectorAll('.nav-item').forEach(n =>
    n.classList.toggle('active', n.dataset.view === name));
  document.getElementById('view-title').textContent = view.title;

  const container = document.getElementById('view');
  if (currentCleanup) { try { currentCleanup(); } catch {} currentCleanup = null; }
  container.innerHTML = '';

  try {
    const mod = await import(view.file);
    currentCleanup = await mod.render(container) || null;
  } catch (e) {
    container.appendChild(el('div', { class: 'panel' },
      el('h3', {}, 'View failed to load'),
      el('div', { class: 'dim' }, errText(e))));
    console.error(e);
  }
}

/* First-run: with v1.1.0 nothing is shared automatically — walk the
   operator through explicit directory authorization (defect A fix). */
function maybeFirstRunWizard() {
  const cfg = state.config;
  if (!cfg || cfg.setup_complete || (cfg.shares || []).length > 0) return;
  modal('First-run setup — authorize directories', (body, close) => {
    body.appendChild(el('div', { class: 'note' },
      'Phantom Network shares NOTHING by default. Pick which directories on ',
      el('b', {}, state.self?.name || 'this device'),
      ' other participating devices may browse. You can change this any time in Settings.'));
    const list = el('div', {});
    body.appendChild(list);
    const done = async () => {
      await state.loadConfig();
      close();
      toast('Setup saved — File Commander is ready', 'ok');
    };
    api.setupStatus().then(st => {
      const renderList = (sug) => {
        list.innerHTML = '';
        for (const s of sug) {
          const row = el('div', { class: 'spread', style: { marginBottom: '8px' } },
            el('div', {},
              el('div', { style: { fontWeight: '600' } }, s.label),
              el('div', { class: 'faint mono small' }, s.path)),
            el('div', { class: 'row' },
              el('button', { class: 'btn small cyan', onclick: async () => {
                  await api.addShare(s.path, 'ro', s.label);
                  toast(`Authorized read-only: ${s.label}`, 'ok');
                  refresh();
                } }, 'Read-only'),
              el('button', { class: 'btn small primary', onclick: async () => {
                  await api.addShare(s.path, 'rw', s.label);
                  toast(`Authorized read-write: ${s.label}`, 'ok');
                  refresh();
                } }, 'Read-write')));
          list.appendChild(row);
        }
      };
      const custom = el('input', { placeholder: '/mnt/data or D:\\Archive', style: { flex: '1' } });
      const customMode = el('select', {},
        el('option', { value: 'rw' }, 'read-write'),
        el('option', { value: 'ro' }, 'read-only'));
      const addCustom = async () => {
        const path = custom.value.trim();
        if (!path) return;
        try { await api.addShare(path, customMode.value); toast('Share added', 'ok'); refresh(); }
        catch (e) { toast(errText(e), 'err', 6000); }
      };
      const refresh = () => api.setupStatus().then(st => {
        renderList(st.suggestions.filter(x =>
          !(st.shares || []).some(sh => sh.path === x.path)));
        if (!(st.shares || []).length && !list.children.length) {
          list.appendChild(el('div', { class: 'empty-note' }, 'No suggestions available — add a path below.'));
        }
      });
      refresh();
      body.appendChild(el('div', { class: 'fld-row mt' }, custom, customMode,
        el('button', { class: 'btn small', onclick: addCustom }, 'Add path')));
    }).catch(() => {});
    body.appendChild(el('div', { class: 'modal-actions' },
      el('button', { class: 'btn', onclick: () => {
        api.setConfig({ setup_complete: true }).then(done);
      } }, 'Continue without sharing'),
      el('button', { class: 'btn primary', onclick: () => {
        api.setConfig({ setup_complete: true }).then(done);
      } }, 'Finish setup')));
  }, [], { sticky: true });
}

boot();
