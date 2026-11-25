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
