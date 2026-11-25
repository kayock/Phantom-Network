/* PHANTOM NETWORK — dashboard.js (Command Center overview) */

import { api } from '../api.js';
import { state } from '../state.js';
import { el, fmtSize, fmtBps, fmtTime, toast, errText, deviceIcon } from '../ui.js';

export async function render(container) {
  const wrap = el('div', {});
  container.appendChild(wrap);

  const stats = { peers_online: 0, transfers: { active: 0 }, chat: {}, events: {} };
  let recentEvents = [];
  let jobStats = { aggregate_speed_bps: 0 };

  async function refresh() {
    try {
      const s = await api.stats();
      Object.assign(stats, s);
    } catch { /* companion busy */ }
    try {
      const ev = await api.events('?limit=12');
      recentEvents = ev.events || [];
    } catch {}
    draw();
  }

  function draw() {
    wrap.innerHTML = '';

    const onlinePeers = state.peers.filter(p => p.participating && p.status === 'online');
    const total = state.peers.filter(p => p.participating);

    // headline
    wrap.appendChild(el('div', { class: 'panel', style: { background: 'linear-gradient(120deg, rgba(109,40,217,.18), rgba(34,211,238,.05))' } },
      el('div', { class: 'spread' },
        el('div', {},
          el('div', { style: { fontSize: '19px', fontWeight: '800', letterSpacing: '.14em' } }, 'PHANTOM NETWORK'),
          el('div', { class: 'dim small mono', style: { marginTop: '4px' } },
            `${state.self?.name || ''} · ${state.self?.host || ''} · v${state.self?.version || 'unknown'} · ${state.self?.os_full || ''}`)),
        el('div', { class: 'row' },
          el('span', { class: 'badge vio' }, `LAN COMMAND CENTER`),
          el('span', { class: 'badge cyan' }, `NO CLOUD · NO ACCOUNTS`)))));

    // stat cards
    const cards = el('div', { class: 'stat-cards' });
    cards.appendChild(statCard(total.length, 'Known devices', `${onlinePeers.length} online · ${total.length - onlinePeers.length} offline`));
    cards.appendChild(statCard(stats.transfers?.active || 0, 'Active transfers', fmtBps(stats.transfers?.aggregate_speed_bps)));
    cards.appendChild(statCard(stats.chat?.messages_stored || 0, 'Messages stored', `${stats.chat?.outbox_pending || 0} queued for offline peers`));
    cards.appendChild(statCard(stats.events ? Object.values(stats.events).reduce((a, b) => a + b, 0) : 0, 'Events recorded', `${recentEvents.length} recent`));
    wrap.appendChild(cards);

    // device strip
    const devicePanel = el('div', { class: 'panel' },
      el('div', { class: 'panel-h' },
        el('h3', {}, 'Participating devices'),
        el('a', { class: 'btn small cyan', href: '#/radar' }, 'Open Radar →')));
    const grid = el('div', { class: 'device-grid' });
    if (!state.self) { /* unreachable */ }
    grid.appendChild(deviceCard({
      name: state.self?.name || 'This device', host: state.self?.host,
      os: state.self?.os, display_name: state.self?.name,
      status: 'online', self: true, addresses: state.self?.addresses || [],
    }));
    for (const p of total.slice(0, 11)) grid.appendChild(deviceCard(p));
    if (total.length === 0) {
      grid.appendChild(el('div', { class: 'empty-note' },
        'No other companions discovered yet. Install the companion on your other computers, then open the Network Radar.'));
    }
    devicePanel.appendChild(grid);
    wrap.appendChild(devicePanel);

    // event feed
    const feed = el('div', { class: 'panel' },
      el('div', { class: 'panel-h' },
        el('h3', {}, 'Live activity'),
        el('a', { class: 'btn small cyan', href: '#/events' }, 'Event Center →')));
    if (recentEvents.length === 0) {
      feed.appendChild(el('div', { class: 'empty-note' }, 'No events yet.'));
    }
    for (const ev of recentEvents) {
      feed.appendChild(el('div', { class: 'feed-item' },
        el('span', { class: `feed-dot sev-${ev.severity}` }),
        el('span', { class: 'feed-ts' }, fmtTime(ev.ts)),
        el('span', { class: 'feed-txt' }, ev.text)));
    }
    wrap.appendChild(feed);
  }

  function statCard(num, label, sub) {
    return el('div', { class: 'stat-card' },
      el('div', { class: 'stat-num' }, String(num ?? '—')),
      el('div', { class: 'stat-lbl' }, label),
      sub ? el('div', { class: 'stat-sub' }, sub) : null);
  }

  function deviceCard(p) {
    const online = p.status === 'online';
    return el('div', {
      class: `device-card ${online ? '' : 'off'}`,
      onclick: () => { location.hash = '#/radar'; },
      title: online ? `${p.display_name} — online` : `${p.display_name || p.name} — offline`,
    },
      el('div', { class: 'd-head' },
        el('div', { class: 'd-ico' }, p.self ? '⌂' : deviceIcon(p)),
        el('div', {},
          el('div', { class: 'd-name' }, p.display_name || p.name),
          el('div', { class: 'd-sub' }, `${p.host || ''}${p.self ? ' · this device' : ''}`))),
      el('div', { class: 'd-meta' },
        el('span', { class: `badge ${online ? 'on' : 'off'}` }, online ? 'ONLINE' : 'OFFLINE'),
        p.latency_ms != null ? el('span', { class: 'badge cyan' }, `${p.latency_ms.toFixed(0)} ms`) : null,
        p.os ? el('span', { class: 'badge' }, p.os) : null));
  }

  await refresh();
  const onEvent = () => refresh();
  window.addEventListener('pn-event', onEvent);
  const onTransfer = () => refresh();
  window.addEventListener('pn-transfer', onTransfer);
  const t = setInterval(refresh, 8000);
  return () => {
    clearInterval(t);
    window.removeEventListener('pn-event', onEvent);
    window.removeEventListener('pn-transfer', onTransfer);
  };
}
