/* PHANTOM NETWORK — fleet.js (Fleet Monitor) */

import { api } from '../api.js';
import { state } from '../state.js';
import {
  el, fmtSize, fmtBps, fmtUptime, fmtPct, errText, deviceIcon, emptyNote,
} from '../ui.js';

export async function render(container) {
  const sel = el('select', { style: { maxWidth: '240px' } });
  const body = el('div', {});
  const note = el('div', { class: 'dim small' }, '');

  async function refreshDevices() {
    const cur = sel.value;
    sel.innerHTML = '';
    for (const d of state.devices()) {
      sel.appendChild(el('option', { value: d.device_id }, d.display_name));
    }
    if (cur) sel.value = cur;
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

  async function refresh() {
    await refreshDevices();
    const device = sel.value || 'self';
    body.innerHTML = '';
    let m;
    try {
      m = await api.monitor(device);
    } catch (e) {
      body.appendChild(emptyNote(`${state.displayName(device)} is not reachable — is its companion running? (${errText(e)})`));
      return;
    }
    note.textContent = `measured ${new Date(m.ts).toLocaleTimeString()} · telemetry via ${m.psutil ? 'psutil' : 'platform APIs'} · unavailable metrics show —`;
    note.className = 'dim small';

    // CPU
    const cpuPanel = el('div', { class: 'panel' },
      el('h3', {}, 'CPU'),
      el('div', { class: 'kv' },
        kv('Utilization', fmtPct(m.cpu.percent)),
        kv('Cores', m.cpu.cores ?? '—'),
        kv('Load average', Array.isArray(m.cpu.loadavg) ? m.cpu.loadavg.map(x => x.toFixed(2)).join(' · ') : '—')));
    cpuPanel.appendChild(sparkEl(device, 'cpu'));

    // Memory
    const mem = m.memory;
    const memPanel = el('div', { class: 'panel' },
      el('h3', {}, 'Memory'),
      el('div', { class: 'progress thin' }, el('div', { style: { width: (mem.percent || 0) + '%' } })),
      el('div', { class: 'kv mt' },
        kv('Physical RAM', fmtSize(mem.total)),
        kv('Available', fmtSize(mem.available)),
        kv('Used', `${fmtSize(mem.used)} (${fmtPct(mem.percent)})`),
        kv('Swap / pagefile', mem.swap_total ? `${fmtSize(mem.swap_used)} / ${fmtSize(mem.swap_total)} (${fmtPct(mem.swap_percent)})` : 'none')));
    memPanel.appendChild(sparkEl(device, 'mem'));

    // Disks
    const diskPanel = el('div', { class: 'panel' },
      el('h3', {}, 'Disks'));
    if (!m.disks.length) diskPanel.appendChild(el('div', { class: 'dim small' }, 'no mount points reported'));
    for (const d of m.disks) {
      diskPanel.appendChild(el('div', { style: { marginBottom: '10px' } },
        el('div', { class: 'spread small' },
          el('span', { class: 'mono' }, d.mount),
          el('span', { class: 'dim' }, `${fmtSize(d.used)} / ${fmtSize(d.total)} · ${fmtSize(d.free)} free`)),
        el('div', { class: 'progress thin' }, el('div', { style: { width: (d.percent || 0) + '%' } }))));
    }

    // Network
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
    netPanel.appendChild(sparkEl(device, 'net'));

    // System + GPU + process
    const sysPanel = el('div', { class: 'panel' },
      el('h3', {}, 'System'),
      el('div', { class: 'kv' },
        kv('Hostname', m.host),
        kv('OS', m.os),
        kv('Uptime', fmtUptime(m.uptime_s)),
        kv('Companion', `v${state.self?.version} · python ${m.python}`),
        kv('Companion memory', fmtSize(m.process?.rss_bytes)),
        kv('Companion threads', m.process?.threads ?? '—'),
        kv('GPU', m.gpu ? m.gpu.gpus.map(g => `${g.name} (${fmtPct(g.util_percent)}, ${g.mem_used_mb ?? '—'} MB)`).join('; ') : 'not detected (no nvidia-smi)')));

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

  sel.addEventListener('change', refresh);
  await refresh();
  const t = setInterval(refresh, 5000);
  return () => clearInterval(t);
}
