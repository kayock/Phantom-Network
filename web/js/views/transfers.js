/* PHANTOM NETWORK — transfers.js (Transfer Center) */

import { api } from '../api.js';
import { state } from '../state.js';
import {
  el, toast, modal, confirmDialog, fmtSize, fmtBps, fmtEta, fmtTime,
  errText, emptyNote,
} from '../ui.js';

export async function render(container) {
  let jobs = [], history = [], stats = {};
  let filter = 'active';

  const tabs = el('div', { class: 'tabbar' });
  const tableWrap = el('div', { class: 'tbl-wrap' });
  const statsBar = el('div', { class: 'row', style: { marginBottom: '10px' } });

  const bwInput = el('input', { type: 'number', style: { width: '110px' },
    value: Math.round((state.config?.bandwidth_limit_bps || 0) / 1024 / 1024), min: '0' });
  const bwPanel = el('div', { class: 'panel' },
    el('h3', {}, 'Bandwidth limit (this device’s transfers)'),
    el('div', { class: 'row' },
      el('label', { class: 'fld' }, 'Limit in MB/s (0 = unlimited)',
        bwInput),
      el('button', { class: 'btn primary small', onclick: async () => {
        const mbps = Number(bwInput.value || 0);
        const bps = Math.round(mbps * 1024 * 1024);
        try {
          await api.setConfig({ bandwidth_limit_bps: bps });
          state.config.bandwidth_limit_bps = bps;
          toast(bps ? `Bandwidth limited to ${mbps} MB/s` : 'Bandwidth limit removed', 'ok');
        } catch (e) { toast(errText(e), 'err'); }
      } }, 'Apply'),
      el('span', { class: 'dim small' },
        'Large AI-model transfers can run beside local inference workloads without saturating the LAN.')));

  // sync tool
  const sync = el('div', { class: 'panel' },
    el('h3', {}, 'Directory synchronization (with preview)'),
    el('div', { class: 'fld-row' },
      syncDeviceSel('sync-src'), el('input', { id: 'sync-src-path', placeholder: 'source dir', style: { width: '170px' } }),
      el('span', { class: 'dim' }, '→'),
      syncDeviceSel('sync-dst'), el('input', { id: 'sync-dst-path', placeholder: 'destination dir', style: { width: '170px' } }),
      el('select', { id: 'sync-compare', title: 'Comparison strategy' },
        el('option', { value: 'hash' }, 'Verified (SHA-256)'),
        el('option', { value: 'size' }, 'Fast (size only)'),
        el('option', { value: 'mtime' }, 'Newer-wins (mtime)')),
      el('button', { class: 'btn primary small', onclick: previewSync }, 'Preview changes')));

  container.appendChild(statsBar);
  container.appendChild(tabs);
  container.appendChild(tableWrap);
  container.appendChild(el('div', { class: 'grid-2' }, bwPanel, sync));
  container.appendChild(historyBar());

  function syncDeviceSel(id) {
    const sel = el('select', { id });
    for (const d of state.devices()) sel.appendChild(el('option', { value: d.device_id }, d.display_name));
    return sel;
  }

  function srcBase(deviceId) {
    if (state.isSelf(deviceId)) return '';
    const p = state.peerById(deviceId);
    return `http://${p.addresses[0]}:${p.api_port}`;
  }

  async function previewSync() {
    const srcDev = document.getElementById('sync-src').value;
    const dstDev = document.getElementById('sync-dst').value;
    const srcPath = document.getElementById('sync-src-path').value.trim();
    const dstPath = document.getElementById('sync-dst-path').value.trim();
    if (!srcPath || !dstPath) { toast('Enter source and destination directories', 'warn'); return; }
    modal('Sync preview — computing…', (body) => {
      body.appendChild(emptyNote('Walking both directories…'));
      api.syncPreview({
        src_base: srcBase(srcDev), dst_base: srcBase(dstDev),
        src_path: srcPath, dst_path: dstPath,
        compare: document.getElementById('sync-compare')?.value || 'hash',
        src_device: state.displayName(srcDev), dst_device: state.displayName(dstDev),
      }).then(plan => {
        body.innerHTML = '';
        body.appendChild(el('div', { class: 'row', style: { marginBottom: '8px' } },
          el('span', { class: 'badge vio' }, `${plan.copy.length} file(s) to copy`),
          el('span', { class: 'badge' }, `${plan.skip.length} identical`),
          el('span', { class: 'badge cyan' }, fmtSize(plan.copy_bytes) + ' to transfer'),
          el('span', { class: 'badge' }, `compare: ${plan.compare || 'size'}`)));
        const list = el('div', { class: 'log-view', style: { maxHeight: '240px' } },
          plan.copy.map(c => `COPY  ${c.rel}  (${c.reason})`).join('\n') +
          (plan.copy.length ? '\n' : '') +
          plan.skip.slice(0, 40).map(s => `SKIP  ${s.rel}`).join('\n') || 'nothing to do');
        body.appendChild(list);
        (plan.notes || []).forEach(n => body.appendChild(el('div', { class: 'note' }, n)));
        body.appendChild(el('div', { class: 'note warn' },
          'Sync copies differing files to the destination. Nothing is ever deleted by sync.'));
        body.appendChild(el('div', { class: 'modal-actions' },
          el('button', { class: 'btn', onclick: () => document.getElementById('modal-layer').classList.add('hidden') }, 'Cancel'),
          el('button', { class: 'btn primary', onclick: async () => {
            try {
              await api.syncApply(plan, false);
              document.getElementById('modal-layer').classList.add('hidden');
              toast(`Sync started: ${plan.copy.length} file(s) queued`, 'ok');
              refresh();
            } catch (e) { toast(errText(e), 'err'); }
          } }, `Apply sync (${plan.copy.length} files)`)));
      }).catch(e => {
        body.innerHTML = '';
        body.appendChild(el('div', { class: 'dim' }, 'Sync preview failed: ' + errText(e)));
      });
    }, [], { sticky: true });
  }

  function historyBar() {
    return el('div', { class: 'row mt' },
      el('button', { class: 'btn small', onclick: exportReport }, '⇩ Export report (JSON)'),
      el('button', { class: 'btn small danger', onclick: async () => {
        if (await confirmDialog('Clear history', 'Remove all completed transfer history from this device?')) {
          await api.post('', '/api/jobs/history/clear');
          refresh();
        }
      } }, 'Clear history'));
  }

  function exportReport() {
    const report = {
      exported: new Date().toISOString(),
      device: state.self?.name,
      stats, active_jobs: jobs, history,
    };
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
    const a = el('a', { href: URL.createObjectURL(blob),
      download: `phantom-transfer-report-${Date.now()}.json` });
    document.body.appendChild(a); a.click(); a.remove();
    toast('Report exported', 'ok');
  }

  async function refresh() {
    try {
      const data = await api.jobs('');
      jobs = data.jobs || [];
      history = data.history || [];
      stats = data.stats || {};
    } catch (e) { /* companion busy */ }
    draw();
  }

  function draw() {
    // stats bar
    statsBar.innerHTML = '';
    statsBar.appendChild(el('span', { class: 'badge vio' }, `${stats.active || 0} running`));
    statsBar.appendChild(el('span', { class: 'badge cyan' }, fmtBps(stats.aggregate_speed_bps)));
    statsBar.appendChild(el('span', { class: 'badge' },
      stats.bandwidth_limit_bps ? `limit ${fmtBps(stats.bandwidth_limit_bps)}` : 'no limit'));
    statsBar.appendChild(el('span', { class: 'badge' }, `${history.length} in history`));

    // tabs
    tabs.innerHTML = '';
    const buckets = {
      active: jobs.filter(j => ['running', 'queued', 'verifying', 'hashing'].includes(j.status)),
      paused: jobs.filter(j => j.status === 'paused'),
      interrupted: jobs.filter(j => j.status === 'interrupted'),
      completed: jobs.filter(j => ['completed', 'completed_partial'].includes(j.status)),
      failed: jobs.filter(j => ['failed', 'cancelled'].includes(j.status)),
      history: history,
    };
    for (const [name, list] of Object.entries(buckets)) {
      tabs.appendChild(el('div', {
        class: 'tab' + (filter === name ? ' active' : ''),
        onclick: () => { filter = name; draw(); },
      }, `${name} (${list.length})`));
    }

    // table
    const list = buckets[filter] || [];
    tableWrap.innerHTML = '';
    if (!list.length) { tableWrap.appendChild(emptyNote(`No ${filter} transfers.`)); return; }
    const table = el('table', { class: 'tbl' });
    table.appendChild(el('thead', {}, el('tr', {},
      ...['File', 'Route', 'Progress', 'Speed', 'ETA', 'Verify', 'Status'].map(h => el('th', {}, h)),
      el('th', {}, 'Controls'))));
    const tbody = el('tbody', {});
    for (const j of list) {
      const prep = j.prepare;
      const hashing = j.status === 'hashing' || (prep && prep.phase === 'source_checksum' && ['queued', 'running'].includes(prep.status));
      const prepSize = prep?.size || j.size || 0;
      const prepDone = prep?.processed || 0;
      const pct = hashing
        ? (prepSize ? Math.min(100, (prepDone / prepSize) * 100) : 0)
        : (j.size ? Math.min(100, (j.transferred / j.size) * 100) : 0);
      const verifyBadge = j.verify?.status === 'ok' ? el('span', { class: 'badge on' }, 'SHA-256 OK')
        : j.verify?.status === 'failed' ? el('span', { class: 'badge err' }, 'FAILED')
        : j.verify?.status === 'pending' && j.status === 'verifying' ? el('span', { class: 'badge vio' }, 'verifying…')
        : hashing ? el('span', { class: 'badge cyan' }, 'checksum…')
        : el('span', { class: 'badge off' }, '—');
      const progressNote = hashing
        ? (prep?.cached
            ? 'Source checksum ready (cached)'
            : prepSize
              ? `Checksum ${fmtSize(prepDone)} / ${fmtSize(prepSize)} (${pct.toFixed(0)}%) · ${fmtBps(prep?.speed_bps || 0)} · elapsed ${Math.round(prep?.elapsed_s || 0)}s`
              : 'Calculating source checksum…')
        : j.size
          ? `${fmtSize(j.transferred)} / ${fmtSize(j.size)} · ${fmtSize(Math.max(0, j.size - j.transferred))} left (${pct.toFixed(0)}%)`
          : `${fmtSize(j.transferred)} transferred (size unknown)`;
      tbody.appendChild(el('tr', {},
        el('td', {},
          el('div', {}, baseOf(j.dest_path)),
          el('div', { class: 'faint small mono', style: { maxWidth: '260px', overflow: 'hidden', textOverflow: 'ellipsis' } },
            j.src_path)),
        el('td', { class: 'small' },
          el('div', { class: 'dim' }, `${j.src_device || '?'} → ${j.dest_device || '?'}`),
          el('div', { class: 'faint small mono' }, j.kind === 'pull_dir' ? `directory · ${j.files_done}/${j.files_total} files` : 'file')),
        el('td', { style: { minWidth: '160px' } },
          el('div', { class: 'progress thin' }, el('div', { style: { width: pct + '%' } })),
          el('div', { class: 'small dim', style: { marginTop: '4px' } }, progressNote)),
        el('td', {}, hashing ? fmtBps(prep?.speed_bps || 0) : fmtBps(j.speed_bps)),
        el('td', {}, hashing ? fmtEta(prep?.eta_s) : fmtEta(j.eta_s)),
        el('td', {}, verifyBadge),
        el('td', {},
          el('span', { class: 'badge ' + statusClass(j.status) }, statusLabel(j)),
          j.error ? el('div', { class: 'small', style: { color: 'var(--red)', maxWidth: '220px' } }, j.error) : null,
          j.move_error ? el('div', { class: 'small', style: { color: 'var(--amber)' } }, j.move_error) : null),
        el('td', {}, controlsFor(j))));
    }
    table.appendChild(tbody);
    tableWrap.appendChild(table);
  }

  function baseOf(p) { return p.split(/[\\/]/).pop() || p; }
  function statusClass(s) {
    return { running: 'vio', queued: '', verifying: 'cyan', hashing: 'cyan',
             paused: 'warn', interrupted: 'warn', completed: 'on',
             completed_partial: 'warn', failed: 'err', cancelled: 'off' }[s] || '';
  }

  function statusLabel(j) {
    if (j.status === 'completed_partial') return 'COPY OK · SOURCE RETAINED';
    if (j.status === 'verifying') return 'VERIFYING DESTINATION';
    if (j.status === 'hashing') return 'CALCULATING SOURCE CHECKSUM';
    if (j.status === 'running') return 'TRANSFERRING';
    if (j.status === 'queued') return 'PREPARING';
    return (j.status || '').toUpperCase();
  }

  function controlsFor(j) {
    const btn = (label, action, cls = '') => el('button', {
      class: `btn small ${cls}`,
      onclick: async () => {
        try {
          await api.jobControl('', j.id, action);
          toast(`${action} → ${baseOf(j.dest_path)}`, 'ok');
          refresh();
        } catch (e) { toast(errText(e), 'err'); }
      },
    }, label);
    const row = el('div', { class: 'row', style: { gap: '4px' } });
    if (['running', 'queued', 'verifying', 'hashing'].includes(j.status)) row.appendChild(btn('⏸', 'pause'));
    if (['paused', 'interrupted', 'failed'].includes(j.status)) row.appendChild(btn('▶', 'resume', 'cyan'));
    if (!['completed'].includes(j.status)) row.appendChild(btn('✕', 'cancel', 'danger'));
    if (['failed', 'interrupted', 'cancelled', 'completed'].includes(j.status)) row.appendChild(btn('↻', 'retry'));
    return row;
  }

  await refresh();
  const t = setInterval(refresh, 1500);
  const onTransfer = () => refresh();
  window.addEventListener('pn-transfer', onTransfer);
  return () => {
    clearInterval(t);
    window.removeEventListener('pn-transfer', onTransfer);
  };
}
