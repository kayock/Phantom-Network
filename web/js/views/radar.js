/* PHANTOM NETWORK — radar.js
   Interactive LAN map + device management + discovery controls. */

import { api } from '../api.js';
import { state } from '../state.js';
import {
  el, toast, modal, confirmDialog, promptDialog, contextMenu,
  fmtUptime, errText, deviceIcon,
} from '../ui.js';

export async function render(container) {
  let raf = null;
  let selected = null;

  const canvas = el('canvas', { id: 'radar-canvas' });
  const canvasPanel = el('div', { class: 'panel radar-wrap' },
    el('div', { class: 'panel-h' },
      el('h3', {}, 'Live network map'),
      el('div', { class: 'row' },
        el('button', { class: 'btn small cyan', onclick: manualRegister }, '+ Register device'),
        el('button', { class: 'btn small', onclick: doSweep }, '⌖ Sweep subnet'))),
    canvas,
    el('div', { class: 'legend', style: { marginTop: '8px' } },
      legendDot('#a78bfa', 'this device'),
      legendDot('#34d399', 'companion online'),
      legendDot('#565f85', 'offline'),
      legendDot('#fbbf24', 'host reachable (not participating)'),
      el('span', { class: 'dim', style: { marginLeft: 'auto' } }, 'animated links = live beacons')));

  const listPanel = el('div', { class: 'panel' },
    el('div', { class: 'panel-h' },
      el('h3', {}, 'Devices'),
      el('div', { class: 'row' },
        el('select', { id: 'radar-filter', style: { maxWidth: '170px' },
          onchange: () => { selected = null; drawList(); } },
          el('option', { value: 'all' }, 'All devices'),
          el('option', { value: 'online' }, 'Online companions'),
          el('option', { value: 'offline' }, 'Offline'),
          el('option', { value: 'new' }, 'Newly discovered'))),
      el('span', { id: 'radar-count', class: 'badge' })));

  const details = el('div', {});
  listPanel.appendChild(details);

  container.appendChild(canvasPanel);
  container.appendChild(listPanel);

  const note = el('div', { class: 'note' },
    'Discovery is passive: companions announce themselves over UDP broadcast every 3 s. ',
    'Only computers running the Phantom companion are controllable — a plain device may appear ',
    'after a manual registration probe, but Phantom will never pretend to manage it.');
  container.insertBefore(note, listPanel);

  function legendDot(color, label) {
    return el('span', {}, el('span', { class: 'dot', style: { background: color } }), label);
  }

  /* ---------------- map ---------------- */
  function nodes() {
    const self = state.self;
    const list = [{ id: 'self', name: self?.name || 'This device', self: true, status: 'online', os: self?.os }];
    const filter = document.getElementById('radar-filter')?.value || 'all';
    for (const p of state.peers) {
      const isNew = Date.now() - (p.first_seen || 0) < 60000;
      if (filter === 'online' && p.status !== 'online') continue;
      if (filter === 'offline' && p.status === 'online') continue;
      if (filter === 'new' && !isNew) continue;
      list.push({ id: p.device_id, name: p.display_name || p.name, peer: p, status: p.status,
                  os: p.os, participating: p.participating });
    }
    return list;
  }

  function drawMap() {
    const ctx = canvas.getContext('2d');
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * devicePixelRatio;
    canvas.height = rect.height * devicePixelRatio;
    ctx.scale(devicePixelRatio, devicePixelRatio);
    const w = rect.width, h = rect.height;
    const cx = w / 2, cy = h / 2;

    ctx.fillStyle = 'rgba(5,6,10,0.9)';
    ctx.fillRect(0, 0, w, h);

    // radar rings
    ctx.strokeStyle = 'rgba(139,92,246,0.15)';
    for (const r of [70, 130, 190]) {
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
    }
    // sweeping beam
    const t = performance.now() / 2400;
    const beam = ctx.createLinearGradient(cx, cy, cx + Math.cos(t) * 200, cy + Math.sin(t) * 200);
    beam.addColorStop(0, 'rgba(34,211,238,0.35)');
    beam.addColorStop(1, 'rgba(34,211,238,0)');
    ctx.strokeStyle = beam;
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(t) * 200, cy + Math.sin(t) * 200); ctx.stroke();
    ctx.lineWidth = 1;

    const ns = nodes();
    const radius = Math.min(w, h) / 2 - 80;
    ns.forEach((n, i) => {
      if (n.self) { n.x = cx; n.y = cy; return; }
      const a = (i / (ns.length - 1)) * Math.PI * 2 - Math.PI / 2;
      n.x = cx + Math.cos(a) * radius;
      n.y = cy + Math.sin(a) * radius;
    });

    // links
    for (const n of ns.slice(1)) {
      const online = n.status === 'online';
      ctx.strokeStyle = online ? 'rgba(52,211,153,0.45)' : 'rgba(86,95,133,0.25)';
      ctx.setLineDash(online ? [] : [4, 4]);
      if (online) {
        ctx.lineWidth = 1.4;
        const pulse = 0.5 + 0.5 * Math.sin(performance.now() / 600 + n.x);
        ctx.strokeStyle = `rgba(52,211,153,${0.25 + pulse * 0.3})`;
      }
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(n.x, n.y); ctx.stroke();
      ctx.setLineDash([]); ctx.lineWidth = 1;
      // moving packet dot on online links
      if (online) {
        const k = (performance.now() / 1400 + n.x * 0.01) % 1;
        ctx.fillStyle = 'rgba(34,211,238,0.9)';
        ctx.beginPath();
        ctx.arc(cx + (n.x - cx) * k, cy + (n.y - cy) * k, 2.4, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // nodes
    for (const n of ns) {
      const online = n.status === 'online';
      const isSel = selected === n.id;
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.self ? 26 : 20, 0, Math.PI * 2);
      ctx.fillStyle = n.self ? 'rgba(139,92,246,0.25)'
        : online ? 'rgba(52,211,153,0.18)' : 'rgba(86,95,133,0.15)';
      ctx.fill();
      ctx.strokeStyle = n.self ? '#a78bfa'
        : online ? '#34d399' : '#565f85';
      ctx.lineWidth = isSel ? 3 : 1.6;
      if (isSel) { ctx.shadowColor = '#a78bfa'; ctx.shadowBlur = 14; }
      ctx.stroke();
      ctx.shadowBlur = 0;
      ctx.fillStyle = online ? '#dfe4f5' : '#8b93b8';
      ctx.font = `${n.self ? 15 : 12.5}px "Segoe UI", sans-serif`;
      ctx.textAlign = 'center';
      ctx.fillText(deviceIcon(n) || '📟', n.x, n.y + 4.5);
      ctx.font = '11.5px "Cascadia Code", monospace';
      ctx.fillStyle = isSel ? '#a78bfa' : '#8b93b8';
      const label = (n.name || '').slice(0, 22);
      ctx.fillText(label, n.x, n.y + (n.self ? 40 : 36));
      if (n.peer?.latency_ms != null) {
        ctx.fillStyle = '#67e8f9';
        ctx.fillText(`${n.peer.latency_ms.toFixed(0)} ms`, n.x, n.y + 50);
      }
    }
    raf = requestAnimationFrame(drawMap);
  }

  canvas.addEventListener('click', (e) => {
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    for (const n of nodes()) {
      const d = Math.hypot(mx - n.x, my - n.y);
      if (d < (n.self ? 30 : 24)) {
        selected = n.id;
        if (n.peer) showDetails(n.peer);
        else if (n.self) showSelfDetails();
        return;
      }
    }
    selected = null;
    showDetails(null);
  });

  canvas.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    for (const n of nodes()) {
      if (Math.hypot(mx - n.x, my - n.y) < 26 && n.peer) {
        selected = n.id;
        deviceMenu(e.clientX, e.clientY, n.peer);
        return;
      }
    }
  });

  /* ---------------- device list + actions ---------------- */
  function drawList() { /* list redraw */
    details.innerHTML = '';
    const ns = nodes();
    document.getElementById('radar-count').textContent = `${ns.length} shown`;
    if (ns.length <= 1) {
      details.appendChild(el('div', { class: 'empty-note' },
        'No other devices yet. Companions appear automatically within ~10 s. ',
        'You can also register a device by IP address.'));
    }
    const wrap = el('div', { class: 'device-grid' });
    for (const n of ns) {
      if (n.self) {
        wrap.appendChild(deviceCardEl({
          display_name: state.self.name + ' (this device)', status: 'online',
          os_full: state.self.os_full, version: state.self.version,
          host: state.self.host, self: true, addresses: state.self.addresses,
        }, null));
        continue;
      }
      wrap.appendChild(deviceCardEl(n.peer, () => showDetails(n.peer)));
    }
    details.appendChild(wrap);
  }

  function deviceCardEl(p, onClick) {
    const online = p.status === 'online';
    const isNew = Date.now() - (p.first_seen || 0) < 60000;
    const card = el('div', {
      class: `device-card ${online ? '' : 'off'}`,
      onclick: (e) => {
        if (onClick) onClick();
        if (!p.self && e.detail === 2) deviceMenu(e.clientX, e.clientY, p);
      },
      oncontextmenu: (e) => { e.preventDefault(); if (!p.self) deviceMenu(e.clientX, e.clientY, p); },
    },
      el('div', { class: 'd-head' },
        el('div', { class: 'd-ico' }, p.self ? '⌂' : deviceIcon(p)),
        el('div', {},
          el('div', { class: 'd-name' }, p.display_name || p.name,
            p.custom_name ? el('span', { class: 'faint small' }, '  (custom)') : null),
          el('div', { class: 'd-sub' },
            `${p.host || '?'} · ${p.addresses?.[0] || 'no address'} · v${p.version || '?'}`))),
      el('div', { class: 'd-meta' },
        el('span', { class: `badge ${online ? 'on' : 'off'}` }, online ? 'ONLINE' : 'OFFLINE'),
        p.participating ? el('span', { class: 'badge vio' }, 'COMPANION') : el('span', { class: 'badge warn' }, 'NOT PARTICIPATING'),
        isNew ? el('span', { class: 'badge cyan' }, 'NEW') : null,
        p.os ? el('span', { class: 'badge' }, p.os) : null,
        p.latency_ms != null ? el('span', { class: 'badge cyan' }, `${p.latency_ms.toFixed(0)} ms`) : null,
        p.uptime_s != null ? el('span', { class: 'badge' }, `up ${fmtUptime(p.uptime_s)}`) : null,
        p.group ? el('span', { class: 'badge vio' }, p.group) : null));
    return card;
  }

  function showSelfDetails() {
    showInfoPanel({
      display_name: state.self.name, host: state.self.host,
      os_full: state.self.os_full, version: state.self.version,
      addresses: state.self.addresses, status: 'online', self: true,
    });
  }

  function showDetails(p) {
    if (!p) { showInfoPanel(null); return; }
    showInfoPanel(p);
    if (p.status !== 'online') return;
  }

  function showInfoPanel(p) {
    const old = document.getElementById('radar-info');
    if (old) old.remove();
    if (!p) return;
    const panel = el('div', { class: 'panel', id: 'radar-info' },
      el('div', { class: 'panel-h' },
        el('h3', {}, `${p.display_name || p.name} — device information`),
        el('div', { class: 'row' },
          !p.self ? el('button', { class: 'btn small primary', onclick: () => openDeviceActions(p) }, 'Actions ▾') : null,
          !p.self ? el('button', { class: 'btn small', onclick: () => editDevice(p) }, 'Rename / icon') : null)));
    const kv = el('div', { class: 'kv' });
    const rows = [
      ['Hostname', p.host || '—'],
      ['Address', (p.addresses || [])[0] || '—'],
      ['OS', p.os_full || p.os || '—'],
      ['Companion', p.self ? 'this browser session' : (p.participating ? `v${p.version} online` : 'no companion')],
      ['Latency', p.latency_ms != null ? `${p.latency_ms.toFixed(1)} ms` : '—'],
      ['Uptime', p.uptime_s != null ? fmtUptime(p.uptime_s) : '—'],
      ['First seen', p.first_seen ? new Date(p.first_seen).toLocaleString() : '—'],
      ['Last seen', p.last_seen ? new Date(p.last_seen).toLocaleString() : 'now'],
      ['Group', p.group || '—'],
      ['Registered', p.manual ? 'manual' : 'discovered'],
    ];
    for (const [k, v] of rows) kv.appendChild(el('div', { class: 'k' }, k));
    for (const [k, v] of rows) kv.appendChild(el('div', { class: 'v' }, String(v)));
    panel.appendChild(kv);
    if (!p.self && p.status === 'online') {
      panel.appendChild(el('div', { class: 'row', style: { marginTop: '12px' } },
        actionBtn('▤ Browse files', 'violet', () => openFiles(p)),
        actionBtn('◌ Message', 'cyan', () => openChat(p)),
        actionBtn('⇄ Send file', '', () => openTransfer(p)),
        actionBtn('♪ Play sound', '', () => openAudio(p)),
        actionBtn('◐ Push-to-talk', '', () => openIntercom(p))));
    }
    listPanel.appendChild(panel);
  }

  function actionBtn(label, cls, fn) {
    return el('button', { class: `btn small ${cls}`, onclick: fn }, label);
  }

  function deviceMenu(x, y, p) {
    const online = p.status === 'online';
    contextMenu(x, y, [
      online && { label: 'Browse files', icon: '▤', handler: () => openFiles(p) },
      online && { label: 'Send message', icon: '◌', handler: () => openChat(p) },
      online && { label: 'Initiate file transfer', icon: '⇄', handler: () => openTransfer(p) },
      online && { label: 'Play a sound', icon: '♪', handler: () => openAudio(p) },
      online && { label: 'Push-to-talk', icon: '◐', handler: () => openIntercom(p) },
      'sep',
      { label: 'View device info', icon: '✚', handler: () => showDetails(p) },
      { label: 'Rename / icon / group', icon: '⚙', handler: () => editDevice(p) },
      { label: 'Re-probe now', icon: '↻', handler: () => probeNow(p) },
      p.manual && { label: 'Remove registration', icon: '✕', danger: true, handler: () => removeManual(p) },
    ].filter(Boolean));
  }

  async function openDeviceActions(p) {
    // small modal reusing the same action list as the map spec requires
    modal(`Actions — ${p.display_name || p.name}`, (body, close) => {
      const mk = (label, fn) => el('button', {
        class: 'btn', style: { width: '100%', marginBottom: '8px', justifyContent: 'flex-start' },
        onclick: () => { close(); fn(); },
      }, label);
      body.appendChild(mk('▤  Browse files on this device', () => openFiles(p)));
      body.appendChild(mk('◌  Send a message', () => openChat(p)));
      body.appendChild(mk('⇄  Initiate a file transfer', () => openTransfer(p)));
      body.appendChild(mk('♪  Play a sound on this device', () => openAudio(p)));
      body.appendChild(mk('◐  Start push-to-talk with this device', () => openIntercom(p)));
      body.appendChild(mk('✚  View device information', () => showDetails(p)));
      body.appendChild(mk('↻  Probe connection status', () => probeNow(p)));
    });
  }

  async function openFiles(p) {
    state.uiPrefs.fc_left = p.device_id;
    state.savePrefs();
    location.hash = '#/commander';
    toast(`File Commander → ${p.display_name} (left pane)`, 'ok');
  }
  async function openChat(p) {
    state.uiPrefs.chat_target = p.device_id;
    state.savePrefs();
    location.hash = '#/chat';
  }
  async function openTransfer(p) {
    state.uiPrefs.tr_target = p.device_id;
    state.savePrefs();
    location.hash = '#/commander';
    toast(`Select the file to send to ${p.display_name}`, 'ok');
  }
  async function openAudio(p) {
    state.uiPrefs.audio_target = p.device_id;
    state.savePrefs();
    location.hash = '#/audio';
  }
  async function openIntercom(p) {
    state.uiPrefs.icom_target = p.device_id;
    state.savePrefs();
    location.hash = '#/intercom';
  }

  async function editDevice(p) {
    const icons = ['', '🖥️', '🐧', '', '📟', '📦', '🧠', '🎮', '🛰', '💾', '🔥', '⭐'];
    const res = await promptDialog(`Edit device — ${p.display_name}`, [
      { name: 'name', label: 'Custom name', value: p.custom_name || '' },
      { name: 'group', label: 'Group (e.g. workstation, mobile)', value: p.group || '' },
      { name: 'icon', label: `Icon (emoji): ${icons.join(' ')}`, value: p.icon || '' },
    ], 'Save');
    if (!res) return;
    try {
      await api.peerMetadata(p.device_id, { custom_name: res.name, group: res.group, icon: res.icon });
      await state.loadPeers();
      drawList();
      toast('Device updated', 'ok');
    } catch (e) { toast(errText(e), 'err'); }
  }

  async function probeNow(p) {
    try {
      await api.peerProbe(p.device_id);
      await state.loadPeers();
      drawList();
      toast(`Probed ${p.display_name}`, 'ok');
    } catch (e) { toast(errText(e), 'err'); }
  }

  async function removeManual(p) {
    if (!await confirmDialog('Remove registration',
      `Remove the manual registration of ${p.display_name}? The device may reappear if it runs a companion.`)) return;
    try {
      await api.post('', '/api/config', {});
      // manual peers are stored in config.manual_peers; ask companion to forget
      const cfg = await api.config();
      const manual = (cfg.config.manual_peers || []).filter(a => a !== p.addresses?.[0]);
      await api.setConfig({ manual_peers: manual });
      await state.loadPeers();
      drawList();
      toast('Registration removed', 'ok');
    } catch (e) { toast(errText(e), 'err'); }
  }

  async function manualRegister(e) {
    const res = await promptDialog('Register a device by IP or hostname', [
      { name: 'address', label: 'IP address or hostname', placeholder: '192.168.1.50' },
      { name: 'port', label: 'Port (optional — default 8415)', placeholder: '8415' },
      { name: 'name', label: 'Name (optional)', placeholder: 'Ubuntu Station' },
    ], 'Register');
    if (!res || !res.address) return;
    toast('Probing…');
    try {
      const result = await api.registerPeer(res.address, res.name,
        res.port && /^\\d+$/.test(res.port.trim()) ? Number(res.port.trim()) : null);
      await state.loadPeers();
      drawList();
      if (result.ok) toast(`Companion registered: ${result.identity?.name || res.address}`, 'ok');
      else toast(result.error || 'No Phantom companion answered there. Device recorded as non-participating.', 'warn', 6000);
    } catch (err) { toast(errText(err), 'err'); }
  }

  async function doSweep() {
    toast('Sweeping local /24 for companions (TCP probe, single port)…');
    try {
      const result = await api.radarSweep('');
      if (!result.ok) { toast(result.error, 'warn'); return; }
      toast(`Sweep started on ${result.network} (${result.hosts} hosts)`, 'ok');
      setTimeout(async () => {
        await state.loadPeers();
        drawList();
      }, 4000);
    } catch (e) { toast(errText(e), 'err'); }
  }

  drawList();
  raf = requestAnimationFrame(drawMap);
  const t = setInterval(async () => {
    try { await state.loadPeers(); drawList(); } catch {}
  }, 6000);
  const resize = () => { /* raf loop redraws */ };
  window.addEventListener('resize', resize);

  return () => {
    clearInterval(t);
    if (raf) cancelAnimationFrame(raf);
    window.removeEventListener('resize', resize);
  };
}
