/* PHANTOM NETWORK — events.js (Event Center) */

import { api } from '../api.js';
import { state } from '../state.js';
import { el, fmtTime, errText, emptyNote, confirmDialog, toast } from '../ui.js';

const TYPE_LABELS = {
  device_connected: 'device connected', device_disconnected: 'device disconnected',
  device_discovered: 'device discovered', device_lost: 'device lost',
  transfer_started: 'transfer started', transfer_completed: 'transfer completed',
  transfer_failed: 'transfer failed', verification_failed: 'verification failed',
  message: 'chat message', audio_playback: 'audio playback', audio_stopped: 'audio stopped',
  intercom_start: 'intercom session', intercom_stop: 'intercom ended',
  companion_error: 'companion error', config_changed: 'config changed',
  network_changed: 'network change', sync_completed: 'sync completed', system: 'system',
};

export async function render(container) {
  let events = [];
  const typeSel = el('select', { style: { maxWidth: '190px' } });
  typeSel.appendChild(el('option', { value: '' }, 'All event types'));
  for (const [id, label] of Object.entries(TYPE_LABELS)) {
    typeSel.appendChild(el('option', { value: id }, label));
  }
  const sevSel = el('select', { style: { maxWidth: '120px' } },
    ['', 'info', 'warn', 'error'].map(s => el('option', { value: s }, s || 'all severities')));
  const search = el('input', { placeholder: 'Search events…', style: { width: '200px' } });
  const listWrap = el('div', { class: 'panel' });
  const statsWrap = el('div', { class: 'row', style: { marginBottom: '10px' } });

  async function refresh() {
    try {
      const params = new URLSearchParams({ limit: '400' });
      if (typeSel.value) params.set('types', typeSel.value);
      if (sevSel.value) params.set('severity', sevSel.value);
      if (search.value.trim()) params.set('q', search.value.trim());
      events = (await api.events('?' + params.toString())).events || [];
    } catch { /* keep old */ }
    draw();
  }

  function draw() {
    listWrap.innerHTML = '';
    if (!events.length) {
      listWrap.appendChild(emptyNote('No matching events.'));
    } else {
      for (const ev of events.slice(0, 300)) {
        listWrap.appendChild(el('div', { class: 'feed-item' },
          el('span', { class: `feed-dot sev-${ev.severity}`, title: ev.severity }),
          el('span', { class: 'feed-ts' }, fmtTime(ev.ts)),
          el('span', { class: 'feed-txt' },
            el('span', { class: 'badge' }, ev.type),
            ' ', ev.text)));
      }
    }
  }

  function drawStats() {
    api.stats().then(s => {
      statsWrap.innerHTML = '';
      const counts = s.events || {};
      const total = Object.values(counts).reduce((a, b) => a + b, 0);
      statsWrap.appendChild(el('span', { class: 'badge vio' }, `${total} events recorded`));
      const top = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 8);
      for (const [t, n] of top) {
        statsWrap.appendChild(el('span', { class: 'badge' }, `${TYPE_LABELS[t] || t}: ${n}`));
      }
    }).catch(() => {});
  }

  const toolbar = el('div', { class: 'panel' },
    el('div', { class: 'fld-row' },
      typeSel, sevSel, search,
      el('button', { class: 'btn primary small', onclick: refresh }, '↻ Refresh'),
      el('span', { class: 'grow' }),
      el('button', { class: 'btn small', onclick: exportEvents }, '⇩ Export (JSON)'),
      el('button', { class: 'btn small danger', onclick: async () => {
        if (await confirmDialog('About clearing events',
          'Event logs deliberately have no remote clear endpoint: each device keeps its own local ' +
          'events.jsonl (auto-trimmed to the newest 5000 entries) and nothing is ever transmitted externally.',
          'Understood', false)) {
          toast('Use the export button to archive events locally.', 'ok');
        }
      } }, 'Clear')));

  function exportEvents() {
    const a = el('a', { href: '/api/events/export', download: 'phantom-events.json' });
    document.body.appendChild(a); a.click(); a.remove();
    toast('Export started — file saved by your browser', 'ok');
  }

  container.appendChild(toolbar);
  container.appendChild(statsWrap);
  container.appendChild(listWrap);
  container.appendChild(el('div', { class: 'note' },
    'Events stay on each device (events.jsonl) and are never sent to any external service. ',
    'This view shows the log of the companion serving this page; open the UI on another device to see its log.'));

  drawStats();
  await refresh();
  const t = setInterval(refresh, 4000);
  search.addEventListener('input', () => { clearTimeout(search._t); search._t = setTimeout(refresh, 350); });
  typeSel.addEventListener('change', refresh);
  sevSel.addEventListener('change', refresh);
  const onEvent = () => refresh();
  window.addEventListener('pn-event', onEvent);
  return () => {
    clearInterval(t);
    window.removeEventListener('pn-event', onEvent);
  };
}
