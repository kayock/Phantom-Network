/* PHANTOM NETWORK — diagnostics.js */

import { api } from '../api.js';
import { state } from '../state.js';
import { el, toast, fmtSize, errText } from '../ui.js';

export async function render(container) {
  const checksWrap = el('div', { class: 'panel' },
    el('h3', {}, 'System self-test'),
    el('div', { class: 'row' },
      el('button', { class: 'btn primary', onclick: runFull }, '▶ Run full self-test'),
      el('span', { class: 'dim small' }, 'checks config, HTTP, loopback call, discovery socket, audio, hashing, shares, transfers, chat, telemetry')));
  const checksList = el('div', { class: 'log-view', style: { maxHeight: '260px' } }, 'loading quick checks…');
  checksWrap.appendChild(checksList);

  const perfPanel = el('div', { class: 'panel' },
    el('h3', {}, 'Companion resource usage (measured)'),
    el('div', { class: 'kv', id: 'perf-kv' }, el('div', { class: 'k' }, 'loading…')));

  const logsPanel = el('div', { class: 'panel' },
    el('h3', {}, 'Recent companion events (error/warn)'),
    el('div', { class: 'log-view', id: 'err-log', style: { maxHeight: '220px' } }, ''));

  const guide = el('div', { class: 'panel' },
    el('h3', {}, 'Common problems & what they mean'),
    el('div', { class: 'kv', id: 'guide-kv' }));
  const about = el('div', { class: 'panel' },
    el('h3', {}, 'About'),
    el('div', { class: 'row' },
      el('button', { class: 'btn small cyan', onclick: exportReport }, '⇩ Export diagnostic report (JSON, stays local)')),
    el('div', { class: 'kv mt' },
      kv('Product', 'PHANTOM NETWORK'),
      kv('Version', `v${state.self?.version || 'unknown'}`),
      kv('Protocol', 'v1 (UDP beacons + HTTP/1.1 + SSE)'),
      kv('This device', `${state.self?.name} (${state.self?.os_full})`),
      kv('Data directory', state.self?.data_dir || '—'),
      kv('Python', state.self ? '' : '')));

  async function quick() {
    try {
      const res = await api.diagnostics();
      paint(res.checks);
    } catch (e) { checksList.textContent = 'unreachable: ' + errText(e); }
  }

  function paint(checks) {
    checksList.innerHTML = '';
    for (const c of checks) {
      checksList.appendChild(el('div', {},
        el('span', { class: c.ok ? 'check-ok' : 'check-fail' }, c.ok ? '✓ ' : '✗ '),
        el('span', {}, c.check),
        c.detail ? el('span', { class: 'faint' }, `  — ${c.detail}`) : null,
        c.hint ? el('div', { class: 'note warn', style: { margin: '4px 0 4px 22px' } }, c.hint) : null));
    }
  }

  async function runFull() {
    toast('Running full self-test…');
    try {
      const res = await api.selfTest();
      paint(res.checks);
      const failed = res.checks.filter(c => !c.ok);
      toast(failed.length ? `Self-test: ${res.checks.length - failed.length} ok, ${failed.length} failed` : 'All checks passed', failed.length ? 'warn' : 'ok');
    } catch (e) { toast(errText(e), 'err'); }
  }

  async function perf() {
    try {
      const s = await api.stats();
      const kvEl = document.getElementById('perf-kv');
      kvEl.innerHTML = '';
      const rows = [
        ['Companion RSS memory', fmtSize(s.process?.rss_bytes)],
        ['Companion threads', s.process?.threads ?? '—'],
        ['Peers known / online', `${s.peers} / ${s.peers_online}`],
        ['Transfer engine', `${s.transfers?.active || 0} active · ${fmtSize(s.transfers?.aggregate_speed_bps) || '0 B'}/s aggregate`],
        ['Chat messages stored', s.chat?.messages_stored ?? 0],
        ['Discovery packets', `rx ${s.discovery?.rx_packets ?? 0} · tx ${s.discovery?.tx_packets ?? 0}`],
        ['Event types recorded', Object.keys(s.events || {}).length],
      ];
      for (const [k, v] of rows) {
        kvEl.appendChild(el('div', { class: 'k' }, k));
        kvEl.appendChild(el('div', { class: 'v' }, String(v)));
      }
    } catch { /* ignore */ }
  }

  async function errLog() {
    try {
      const ev = (await api.events('?severity=warn&limit=50')).events;
      const elx = document.getElementById('err-log');
      elx.textContent = ev.length
        ? ev.map(e => `${new Date(e.ts).toLocaleTimeString()} [${e.severity}] ${e.text}`).reverse().join('\n')
        : 'no warnings or errors recorded — clean run';
    } catch { /* ignore */ }
  }

  function kv(k, v) { return [el('div', { class: 'k' }, k), el('div', { class: 'v' }, String(v || '—'))]; }

  function exportReport() {
    const a = el('a', { href: '/api/diagnostics/report', download: 'phantom-diagnostics.json' });
    document.body.appendChild(a); a.click(); a.remove();
    toast('Diagnostic report saved by your browser (never leaves this machine)', 'ok');
  }

  function loadGuide() {
    api.get('', '/api/diagnostics/report').then(rep => {
      const kvEl = document.getElementById('guide-kv');
      kvEl.innerHTML = '';
      const expl = rep.explanations || {};
      for (const [key, text] of Object.entries(expl)) {
        kvEl.appendChild(el('div', { class: 'k' }, key.replace(/_/g, ' ')));
        kvEl.appendChild(el('div', { class: 'v', style: { whiteSpace: 'normal' } }, text));
      }
      // overview numbers from the report
      const perf = document.getElementById('perf-kv');
      if (perf) {
        perf.appendChild(el('div', { class: 'k' }, 'Listening ports'));
        perf.appendChild(el('div', { class: 'v' },
          `api ${rep.listening?.api_port}/tcp · discovery ${rep.listening?.discovery_port}/udp`));
        perf.appendChild(el('div', { class: 'k' }, 'Shared directories'));
        perf.appendChild(el('div', { class: 'v' },
          `${(rep.shares || []).length}${(rep.shares || []).length ? ': ' + rep.shares.map(x => x.path).join(', ') : ' — none (use Settings)'}`));
      }
    }).catch(() => {});
  }

  container.appendChild(checksWrap);
  container.appendChild(perfPanel);
  container.appendChild(logsPanel);
  container.appendChild(guide);
  container.appendChild(about);
  await quick();
  await perf();
  await errLog();
  loadGuide();
  const t = setInterval(() => { perf(); errLog(); }, 5000);
  return () => clearInterval(t);
}
