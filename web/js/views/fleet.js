/* PHANTOM NETWORK — fleet.js (Fleet Monitor 2.0)
   "All Devices" fleet overview + per-device detail, storage views
   (useful / physical / all), vendor-aware GPU info, honest telemetry. */

import { api } from '../api.js';
import { state } from '../state.js';
import {
  el, fmtSize, fmtBps, fmtUptime, fmtPct, errText, deviceIcon, emptyNote,
} from '../ui.js';

export async function render(container) {
  const mode = el('select', { style: { maxWidth: '220px' } },
    el('option', { value: 'overview' }, '▦ All Devices overview'),
    ...state.devices().map(d =>
      el('option', { value: d.device_id }, 'Per-device: ' + d.display_name)));
  const body = el('div', {});
  const note = el('div', { class: 'dim small' }, '');

  async function refreshDevices() {
    const cur = mode.value;
    mode.innerHTML = '';
    mode.appendChild(el('option', { value: 'overview' }, '▦ All Devices overview'));
    for (const d of state.devices()) {
      mode.appendChild(el('option', { value: d.device_id }, 'Per-device: ' + d.display_name));
    }
    if (cur) mode.value = cur;
  }

  function spark(history, key, color, label) {
    const canvas = el('canvas', { class: 'spark' });
    const w = canvas.clientWidth || 300, h = 46;
    canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#05060a'; ctx.fillRect(0, 0, w, h);
    const pts = history.map(p => p[key]).filter(v => v !== null && v !== undefined);
    ctx.strokeStyle = color;
    ctx.beginPath();
    const max = Math.max(1, ...pts);
    history.forEach((p, i) => {
      const v = p[key];
      if (v == null) return;
      const x = (i / Math.max(1, history.length - 1)) * w;
      const y = h - (key === 'net_bps' ? (v / max) : v / 100) * (h - 4) - 2;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
    return el('div', {},
      canvas,
      el('div', { class: 'dim small' }, label));
  }

  /* ---------------- All Devices overview (v1.3.0) ---------------- */
  async function renderOverview() {
    body.innerHTML = '';
    const online = state.devices().filter(d => !d.self);
    const targets = [{ device_id: 'self', display_name: state.self?.name + ' (this device)' },
      ...online.filter(d => d.participating !== false)];
    const grid = el('div', { class: 'grid-3' });
    const snapshots = await Promise.all(targets.map(async (d) => {
      try { return { d, m: await api.monitor(d.device_id) }; }
      catch { return { d, m: null }; }
    }));
    for (const { d, m } of snapshots) {
      const onlineNow = !!m;
      const card = el('div', { class: 'panel' },
        el('h3', {}, `${deviceIcon({ os: m?.os?.split(' ')[0] }) || '📟'} ${d.display_name}`),
        onlineNow ? el('div', {},
          el('div', { class: 'spread small' },
            el('span', { class: 'dim' }, 'CPU'), el('b', {}, fmtPct(m.cpu?.percent))),
          el('div', { class: 'progress thin' },
            el('div', { style: { width: (m.cpu?.percent || 0) + '%' } })),
          el('div', { class: 'spread small mt' },
            el('span', { class: 'dim' }, 'RAM'), el('b', {}, fmtPct(m.memory?.percent))),
          el('div', { class: 'progress thin' },
            el('div', { style: { width: (m.memory?.percent || 0) + '%' } })),
          el('div', { class: 'kv mt' },
            kv('Disks', diskSummary(m.disks)),
            kv('Net', `${fmtBps(m.network?.rx_bps)} ↓ ${fmtBps(m.network?.tx_bps)} ↑`),
            kv('Companion', `${fmtSize(m.process?.rss_bytes)} · ${m.process?.threads ?? '—'} thr`),
            kv('GPU', gpuSummary(m.gpu_info, m.gpu)),
            kv('Uptime', fmtUptime(m.uptime_s))))
          : el('div', { class: 'empty-note' }, 'offline — no telemetry available, nothing invented'));
      card.addEventListener('click', () => { mode.value = d.device_id; refresh(); });
      grid.appendChild(card);
    }
    body.appendChild(grid);
    note.textContent = `overview refreshed ${new Date().toLocaleTimeString()} — click a card for the detailed view`;
  }

  function diskSummary(disks) {
    if (!disks || !disks.length) return '—';
    const minFree = Math.min(...disks.map(d => (d.percent == null ? 100 : 100 - d.percent)));
    return `${disks.length} mount${disks.length === 1 ? '' : 's'} · least free ${fmtPct(minFree === 100 ? null : minFree)}`;
  }
  function gpuSummary(gpuInfo, legacy) {
    const gpus = gpuInfo?.gpus || legacy?.gpus || [];
    if (!gpus.length) return 'not detected';
    return gpus.map(g =>
      `${g.vendor || '?'} ${g.name}` +
      (g.util_available ? ` (${fmtPct(g.util_percent)}, ${g.mem_used_mb ?? '—'} MB)` : ' (util n/a)')).join('; ');
  }

  /* ---------------- per-device detail ---------------- */
  async function renderDetail() {
    body.innerHTML = '';
    const device = mode.value;
    let m;
    try {
      m = await api.monitor(device);
    } catch (e) {
      body.appendChild(emptyNote(`${state.displayName(device)} is not reachable — is its companion running? (${errText(e)})`));
      return;
    }
    note.textContent = `measured ${new Date(m.ts).toLocaleTimeString()} · telemetry via ${m.psutil ? 'psutil' : 'platform APIs'} · unavailable metrics show —`;
    note.className = 'dim small';

    const topo = m.cpu_topology || {};
    const cpuPanel = el('div', { class: 'panel' },
      el('h3', {}, 'CPU'),
      el('div', { class: 'kv' },
        kv('Utilization', fmtPct(m.cpu.percent)),
        kv('Cores', topo.physical != null
          ? `${topo.physical} physical / ${topo.logical} logical`
          : (m.cpu.cores ?? '—')),
        kv('Load average', Array.isArray(m.cpu.loadavg) ? m.cpu.loadavg.map(x => x.toFixed(2)).join(' · ') : '—')));
    cpuPanel.appendChild(await sparkEl(device, 'cpu'));

    const mem = m.memory;
    const memPanel = el('div', { class: 'panel' },
      el('h3', {}, 'Memory'),
      el('div', { class: 'progress thin' }, el('div', { style: { width: (mem.percent || 0) + '%' } })),
      el('div', { class: 'kv mt' },
        kv('Physical RAM', fmtSize(mem.total)),
        kv('Available', fmtSize(mem.available)),
        kv('Used', `${fmtSize(mem.used)} (${fmtPct(mem.percent)})`),
        kv('Swap / pagefile', mem.swap_total ? `${fmtSize(mem.swap_used)} / ${fmtSize(mem.swap_total)} (${fmtPct(mem.swap_percent)})` : 'none')));
    memPanel.appendChild(await sparkEl(device, 'mem'));

    /* Disks with storage-view filtering (v1.3.0) */
    const view = state.uiPrefs.storage_view || 'useful';
    const viewSel = el('select', { style: { maxWidth: '160px' },
      onchange: () => { state.uiPrefs.storage_view = viewSel.value; state.savePrefs(); refresh(); } },
      el('option', { value: 'useful', ...(view === 'useful' ? { selected: true } : {}) }, 'Useful mounts (default)'),
      el('option', { value: 'physical', ...(view === 'physical' ? { selected: true } : {}) }, 'Physical disks only'),
      el('option', { value: 'all', ...(view === 'all' ? { selected: true } : {}) }, 'Everything (diagnostics)'));
    const diskPanel = el('div', { class: 'panel' },
      el('div', { class: 'spread' }, el('h3', {}, 'Disks'), viewSel));
    let mounts = m.disks || [];
    try {
      const st = await api.storage(device, view);
      mounts = st.mounts || [];
    } catch { /* older peer: fall back to snapshot list */ }
    if (!mounts.length) diskPanel.appendChild(el('div', { class: 'dim small' }, 'no mount points in this view'));
    for (const d of mounts) {
      const kindBadge = d.kind === 'external' || d.removable ? el('span', { class: 'badge cyan' }, 'REMOVABLE')
        : d.kind === 'network' ? el('span', { class: 'badge' }, 'NETWORK')
        : d.kind === 'snap' ? el('span', { class: 'badge' }, 'SNAP')
        : d.kind === 'virtual' ? el('span', { class: 'badge' }, 'VIRTUAL')
        : el('span', { class: 'badge vio' }, 'PHYSICAL');
      diskPanel.appendChild(el('div', { style: { marginBottom: '10px' } },
        el('div', { class: 'spread small' },
          el('span', {}, el('span', { class: 'mono' }, d.mount), ' ', kindBadge,
            d.read_only ? el('span', { class: 'badge warn' }, 'READ-ONLY') : null),
          el('span', { class: 'dim' }, `${fmtSize(d.used)} / ${fmtSize(d.total)} · ${fmtSize(d.free)} free`)),
        el('div', { class: 'progress thin' }, el('div', { style: { width: (d.percent || 0) + '%' } }))));
    }
    if (view !== 'all') {
      diskPanel.appendChild(el('div', { class: 'dim small mt' },
        'Snap loops and virtual mounts are hidden in this view — switch to "Everything" for complete diagnostics.'));
    }

    const netPanel = el('div', { class: 'panel' },
      el('h3', {}, 'Network'),
      el('div', { class: 'kv' },
        kv('Download', fmtBps(m.network.rx_bps)),
        kv('Upload', fmtBps(m.network.tx_bps)),
        kv('Total RX', fmtSize(m.network.rx_total)),
        kv('Total TX', fmtSize(m.network.tx_total)),
        kv('Addresses', (m.network.addresses || []).join(', ') || '—')),
      el('h3', { style: { marginTop: '10px' } }, 'Interfaces'),
      el('div', { class: 'log-view', style: { maxHeight: '90px' } },
        (m.interfaces || []).map(i => `${i.name || 'if'}: ${i.ipv4 || ''} ${i.mac || ''}`).join('\n') || '—'));
    netPanel.appendChild(await sparkEl(device, 'net'));

    const gpuInfo = m.gpu_info || { gpus: [] };
    const sysPanel = el('div', { class: 'panel' },
      el('h3', {}, 'System'),
      el('div', { class: 'kv' },
        kv('Hostname', m.host),
        kv('OS', m.os),
        kv('Uptime', fmtUptime(m.uptime_s)),
        kv('Companion', `v${state.self?.version} · python ${m.python}`),
        kv('Companion memory', fmtSize(m.process?.rss_bytes)),
        kv('Companion threads', m.process?.threads ?? '—'),
        kv('GPU', gpuSummary(gpuInfo, m.gpu))));
    for (const g of gpuInfo.gpus || []) {
      sysPanel.appendChild(el('div', { class: 'dim small' },
        `• ${g.vendor} ${g.name} — ${g.util_available
          ? `util ${fmtPct(g.util_percent)}, VRAM ${g.mem_used_mb ?? '—'}/${g.mem_total_mb ?? '—'} MB, ${g.temp_c ?? '—'}°C`
          : 'identity only (utilization needs the vendor tooling; reported honestly as unavailable)'}`));
    }
    if (!gpuInfo.gpus?.length) {
      sysPanel.appendChild(el('div', { class: 'dim small' },
        'No NVIDIA/Intel/AMD adapter detected through lightweight probes (nvidia-smi, sysfs drm).'));
    }

    const grid = el('div', { class: 'grid-3' });
    grid.appendChild(cpuPanel);
    grid.appendChild(memPanel);
    grid.appendChild(sysPanel);
    grid.appendChild(diskPanel);
    grid.appendChild(netPanel);
    body.appendChild(grid);
  }

  function kv(k, v) {
    return [el('div', { class: 'k' }, k), el('div', { class: 'v' }, String(v ?? '—'))];
  }

  async function sparkEl(device, kind) {
    try {
      const data = (await api.monitorHistory(device)).history || [];
      const map = { cpu: ['cpu', '#a78bfa', 'CPU % history (5 s samples)'],
                    mem: ['mem', '#22d3ee', 'Memory % history'],
                    net: ['net_bps', '#34d399', 'Network RX history'] }[kind];
      return spark(data, map[0], map[1], map[2]);
    } catch { return el('div', { class: 'dim small' }, 'no history yet'); }
  }

  async function refresh() {
    try {
      await refreshDevices();
      if (mode.value === 'overview') await renderOverview();
      else await renderDetail();
    } catch (e) {
      body.innerHTML = '';
      body.appendChild(el('div', { class: 'panel' },
        el('h3', {}, 'Fleet Monitor error'),
        el('div', { class: 'dim' }, errText(e))));
    }
  }

  container.appendChild(el('div', { class: 'row', style: { marginBottom: '10px' } },
    el('span', { class: 'dim small' }, 'View'), mode));
  container.appendChild(note);
  container.appendChild(body);
  mode.addEventListener('change', refresh);
  await refresh();
  const t = setInterval(refresh, 5000);
  return () => clearInterval(t);
}
