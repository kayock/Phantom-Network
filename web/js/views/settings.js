/* PHANTOM NETWORK — settings.js */

import { api } from '../api.js';
import { state } from '../state.js';
import { el, toast, confirmDialog, errText, modal, fmtTime } from '../ui.js';

export async function render(container) {
  let cfg = state.config;
  if (!cfg) { try { cfg = (await api.config()).config; } catch {} }

  const fld = (label, node) => el('label', { class: 'fld', style: { minWidth: '220px' } }, label, node);

  /* identity */
  const nameInput = el('input', { value: cfg?.device_name || '', style: { width: '220px' } });
  const identity = el('div', { class: 'panel' },
    el('h3', {}, 'Device identity'),
    el('div', { class: 'fld-row' },
      fld('Device name (shown to other devices)', nameInput),
      el('button', { class: 'btn primary', onclick: async () => {
        try { await api.setConfig({ device_name: nameInput.value.trim() || 'unnamed' });
          toast('Saved — other devices will see the new name within ~10 s', 'ok');
          await state.loadSelf(); await state.loadPeers();
        } catch (e) { toast(errText(e), 'err'); }
      } }, 'Save'),
      el('div', { class: 'kv grow' },
        kv('Device ID', cfg?.device_id || '—'),
        kv('Hostname', state.self?.host || '—'),
        kv('Version', `v${state.self?.version || 'unknown'}`))));

  /* network */
  const discBox = el('input', { type: 'checkbox', checked: !!cfg?.discovery_enabled });
  const sweepBox = el('input', { type: 'checkbox', checked: !!cfg?.active_sweep_enabled });
  const bcastInput = el('input', { value: (cfg?.broadcast_addresses || []).join(', '),
    placeholder: '192.168.1.255, 192.168.0.255', style: { width: '260px' } });
  const network = el('div', { class: 'panel' },
    el('h3', {}, 'Discovery & network'),
    el('div', { class: 'fld-row' },
      el('label', { class: 'row small', style: { gap: '8px' } }, discBox, 'UDP beacon discovery (port 8414)'),
      el('label', { class: 'row small', style: { gap: '8px' } }, sweepBox, 'Allow user-triggered subnet sweep')),
    el('div', { class: 'fld-row mt' },
      fld('Extra directed-broadcast addresses (comma-sep)', bcastInput),
      el('button', { class: 'btn primary', onclick: async () => {
        const addrs = bcastInput.value.split(',').map(s => s.trim()).filter(Boolean);
        try {
          await api.setConfig({ discovery_enabled: discBox.checked,
            active_sweep_enabled: sweepBox.checked, broadcast_addresses: addrs });
          toast('Network settings saved (restarting the companion applies discovery changes)', 'ok');
        } catch (e) { toast(errText(e), 'err'); }
      } }, 'Save'),
      el('div', { class: 'dim small grow' },
        'API port 8415/tcp and discovery 8414/udp are the only ports Phantom uses. ',
        'Firewall changes are never made automatically — see DEPLOYMENT.md.')));

  /* shares */
  const sharesWrap = el('div', {});
  const sharePanel = el('div', { class: 'panel' },
    el('h3', {}, 'Authorized directories (shares)'),
    sharesWrap,
    el('div', { class: 'fld-row mt' },
      el('input', { id: 'share-path', placeholder: 'e.g. /mnt/media or C:\\Users\\me\\Videos', style: { width: '280px' } }),
      el('select', { id: 'share-mode' },
        el('option', { value: 'rw' }, 'read-write'),
        el('option', { value: 'ro' }, 'read-only')),
      el('button', { class: 'btn primary small', onclick: addShare }, 'Add share')));

  async function drawShares() {
    try {
      const data = await api.fsRoots('self');
      sharesWrap.innerHTML = '';
      for (const s of data.shares) {
        sharesWrap.appendChild(el('div', { class: 'row', style: { marginBottom: '6px' } },
          el('span', { class: 'badge vio' }, s.mode.toUpperCase()),
          el('span', { class: 'mono small' }, s.path),
          el('span', { class: 'grow' }),
          el('button', { class: 'btn small danger', onclick: async () => {
            if (await confirmDialog('Remove share', `Remove ${s.path} from the authorized list? Files are not deleted.`)) {
              await api.post('', '/api/fs/unshare', { path: s.path });
              drawShares();
            }
          } }, 'Remove')));
      }
      if (!data.shares.length) sharesWrap.appendChild(el('div', { class: 'dim small' }, 'No shares configured.'));
      sharesWrap.appendChild(el('div', { class: 'dim small mt' },
        'File Commander can only see paths inside these roots. The sandbox resolves symlinks and rejects traversal outside them.'));
    } catch (e) { toast(errText(e), 'err'); }
  }

  async function addShare() {
    const path = document.getElementById('share-path').value.trim();
    const mode = document.getElementById('share-mode').value;
    if (!path) { toast('Enter a directory path', 'warn'); return; }
    try {
      await api.post('', '/api/fs/share', { path, mode });
      toast('Share added', 'ok');
      drawShares();
    } catch (e) { toast(errText(e), 'err', 6000); }
  }

  /* transfer engine */
  const concInput = el('input', { type: 'number', value: cfg?.max_concurrent_jobs || 2, min: '1', max: '8', style: { width: '80px' } });
  const chunkSel = el('select', {},
    [256, 512, 1024, 4096].map(n => el('option', { value: n * 1024,
      selected: (cfg?.chunk_size || 1048576) === n * 1024 }, `${n} KB`)));
  const verifyBox = el('input', { type: 'checkbox', checked: cfg?.verify_sha256 !== false });
  const transfers = el('div', { class: 'panel' },
    el('h3', {}, 'Transfer engine'),
    el('div', { class: 'fld-row' },
      fld('Max concurrent jobs', concInput),
      fld('Chunk size', chunkSel),
      el('label', { class: 'row small', style: { gap: '8px' } }, verifyBox, 'SHA-256 verification (recommended)'),
      el('button', { class: 'btn primary', onclick: async () => {
        try {
          await api.setConfig({ max_concurrent_jobs: Number(concInput.value),
            chunk_size: Number(chunkSel.value), verify_sha256: verifyBox.checked });
          toast('Transfer engine settings saved', 'ok');
        } catch (e) { toast(errText(e), 'err'); }
      } }, 'Save')));

  /* privacy toggles */
  const audioBox = el('input', { type: 'checkbox', checked: !!cfg?.remote_audio_enabled });
  const icomBox = el('input', { type: 'checkbox', checked: !!cfg?.intercom_enabled });
  const monBox = el('input', { type: 'checkbox', checked: !!cfg?.monitoring_enabled });
  const tokBox = el('input', { type: 'checkbox', checked: !!cfg?.require_token });
  const tokenInput = el('input', { value: '', placeholder: 'leave empty to keep current', style: { width: '240px' } });
  const privacy = el('div', { class: 'panel' },
    el('h3', {}, 'Participation & privacy controls'),
    el('div', { class: 'fld-row' },
      el('label', { class: 'row small', style: { gap: '8px' } }, audioBox, 'Allow remote audio playback on this device'),
      el('label', { class: 'row small', style: { gap: '8px' } }, icomBox, 'Allow intercom relay on this device')),
    el('div', { class: 'fld-row mt' },
      el('label', { class: 'row small', style: { gap: '8px' } }, monBox, 'Report hardware telemetry to participating devices'),
      el('label', { class: 'row small', style: { gap: '8px' } }, tokBox, 'Require auth token from peers')),
    el('div', { class: 'fld-row mt' },
      fld('Shared token (set the SAME value on every device)', tokenInput),
      el('button', { class: 'btn primary', onclick: async () => {
        try {
          const patch = { remote_audio_enabled: audioBox.checked,
            intercom_enabled: icomBox.checked, monitoring_enabled: monBox.checked,
            require_token: tokBox.checked };
          if (tokenInput.value.trim()) patch.auth_token = tokenInput.value.trim();
          await api.setConfig(patch);
          toast('Participation settings saved', 'ok');
        } catch (e) { toast(errText(e), 'err'); }
      } }, 'Save'),
      el('div', { class: 'dim small grow' },
        'The token is optional protection for shared networks. Every participating device must use the same value. ',
        'Phantom never exposes services to the internet and never starts automatically unless you install it that way.')));

  /* ---------- Phantom Mobile (v1.3.0) ---------- */
  const mobileWrap = el('div', {});
  const mobilePanel = el('div', { class: 'panel' },
    el('h3', {}, 'Phantom Mobile — phones & tablets'),
    el('div', { class: 'dim small', style: { marginBottom: '10px' } },
      'Add Mobile Device shows a QR code. Scanning it with a phone camera opens the ',
      el('b', {}, 'mobile interface'),
      ' and registers the phone as an independent participant with its own identity, ',
      'inbox and presence — hosted and relayed by this companion. No pairing codes, ',
      'tokens or cloud services are involved.'),
    el('div', { class: 'fld-row' },
      el('button', { class: 'btn primary', onclick: showMobileQR }, '📱 Add Mobile Device (QR code)'),
      el('button', { class: 'btn', onclick: () => location.href = '/m' }, 'Open mobile interface here'),
      el('label', { class: 'row small', style: { gap: '8px' } },
        el('input', { type: 'checkbox', id: 'mobile-host-box', checked: cfg?.mobile_host_enabled !== false,
          onchange: async (e) => {
            try { await api.setConfig({ mobile_host_enabled: e.target.checked });
              toast(e.target.checked ? 'Mobile hosting enabled' : 'Mobile hosting disabled — existing clients stay registered but cannot reconnect', 'ok');
            } catch (err) { toast(errText(err), 'err'); }
          } }), 'Host mobile clients on this companion')),
    mobileWrap,
    el('div', { class: 'dim small mt' },
      'Manual registration alternative: open ', el('span', { class: 'mono' }, 'http://<this-computer-ip>:8415/m'),
      ' in any phone browser on the same LAN. Duplicate identities are handled on registration; ',
      'refreshing the browser never creates a new identity (it is stored in localStorage).'));

  async function drawMobile() {
    try {
      const data = await api.mobileClients();
      mobileWrap.innerHTML = '';
      for (const c of data.clients || []) {
        mobileWrap.appendChild(el('div', { class: 'row', style: { marginBottom: '6px', flexWrap: 'wrap' } },
          el('span', {}, c.icon || '📱'),
          el('span', { style: { fontWeight: '600' } }, c.name || c.client_id),
          el('span', { class: 'badge ' + (c.online ? 'grn' : 'gry') }, c.online ? 'ONLINE' : 'OFFLINE ' + fmtAgo(c.last_seen)),
          el('span', { class: 'faint small mono' }, c.device_type),
          el('span', { class: 'grow' }),
          el('button', { class: 'btn small', onclick: () => renameClient(c) }, 'Rename'),
          el('button', { class: 'btn small danger', onclick: async () => {
            if (await confirmDialog('Remove mobile client',
              `Remove ${c.name || c.client_id}? Its stored inbox is deleted. The phone re-registers as a NEW participant next time it opens /m.`)) {
              await api.mobileRemove(c.client_id); drawMobile();
            }
          } }, 'Remove')));
      }
      if (!(data.clients || []).length) {
        mobileWrap.appendChild(el('div', { class: 'dim small' },
          'No mobile clients registered yet. A phone that opens this page\'s mobile interface appears here.'));
      }
    } catch (e) { mobileWrap.appendChild(el('div', { class: 'dim small' }, 'Mobile registry unavailable')); }
  }

  function fmtAgo(ts) {
    if (!ts) return 'never';
    const s = Math.round((Date.now() - ts) / 1000);
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.round(s / 60) + 'm ago';
    if (s < 86400) return Math.round(s / 3600) + 'h ago';
    return Math.round(s / 86400) + 'd ago';
  }

  function renameClient(c) {
    modal('Rename mobile client', (body) => {
      const input = el('input', { value: c.name || '', maxlength: 40, style: { width: '240px' } });
      body.appendChild(el('label', { class: 'fld' }, 'Device name', input));
      body._input = input;
    }, [
      { label: 'Cancel', handler: (close) => close() },
      { label: 'Save', kind: 'primary', handler: async (close, body) => {
        await api.mobileRename(c.client_id, { name: body._input.value.trim() });
        close(); drawMobile(); toast('Renamed', 'ok');
      } },
    ]);
  }

  function showMobileQR() {
    modal('Add Mobile Device — scan with the phone camera', (body, close) => {
      const qrBox = el('div', { style: { display: 'grid', placeItems: 'center', padding: '10px', background: '#fff', borderRadius: '12px' } });
      const urlInput = el('input', { value: '', style: { width: '300px' }, readonly: true });
      const addr = (state.self?.addresses || [])[0];
      const port = state.self?.api_port || 8415;
      const regUrl = addr ? `http://${addr}:${port}/m` : '';
      urlInput.value = regUrl;
      body.appendChild(el('div', { class: 'dim small', style: { marginBottom: '8px' } },
        'The QR contains the LAN URL of the MOBILE interface on this companion only. ',
        'It embeds no authentication secrets and no tokens — if you enable token auth, ',
        'enter it on the phone manually (optional).'));
      body.appendChild(qrBox);
      body.appendChild(el('div', { class: 'fld-row mt' }, urlInput,
        el('button', { class: 'btn small', onclick: () => { navigator.clipboard?.writeText(regUrl); toast('URL copied', 'ok'); } }, 'Copy URL')));
      // local QR rendering (vendored qrcode-generator, MIT) — no external service
      const s = document.createElement('script');
      s.src = '/js/lib/qrcode.js';
      s.onload = () => {
        try {
          const qr = window.qrcode(0, 'M');
          qr.addData(regUrl);
          qr.make();
          const count = qr.getModuleCount();
          const cell = 6, margin = 8;
          const size = count * cell + margin * 2;
          const canvas = document.createElement('canvas');
          canvas.width = canvas.height = size;
          const ctx = canvas.getContext('2d');
          ctx.fillStyle = '#ffffff';
          ctx.fillRect(0, 0, size, size);
          ctx.fillStyle = '#0a0c14';
          for (let r = 0; r < count; r++) {
            for (let c2 = 0; c2 < count; c2++) {
              if (qr.isDark(r, c2)) ctx.fillRect(margin + c2 * cell, margin + r * cell, cell, cell);
            }
          }
          canvas.style.maxWidth = '240px';
          qrBox.appendChild(canvas);
        } catch (e) {
          qrBox.appendChild(el('div', { class: 'dim small', style: { color: '#333' } },
            'QR unavailable — use the URL below.'));
        }
      };
      s.onerror = () => {
        qrBox.appendChild(el('div', { class: 'dim small', style: { color: '#333' } },
          'QR library missing — type the URL below into the phone browser.'));
      };
      document.head.appendChild(s);
      if (!addr) {
        qrBox.appendChild(el('div', { class: 'dim small', style: { color: '#333' } },
          'No LAN address detected — this companion may be running in an isolated network.'));
      }
      body.appendChild(el('div', { class: 'dim small mt' },
        'After scanning: the phone picks a device name and registers. It then appears in ',
        'the Network Radar and GhostChat as an independent participant.'));
    });
  }

  /* ---------- monitoring alerts (v1.3.0) ---------- */
  const alertsWrap = el('div', {});
  const alertsPanel = el('div', { class: 'panel' },
    el('h3', {}, 'Monitoring alerts'),
    el('div', { class: 'dim small', style: { marginBottom: '8px' } },
      'Optional local rules — nothing is enabled automatically. Alerts surface in the ',
      'Event Center, a 🔔 System Alerts GhostChat conversation, and optionally as a sound cue.'),
    alertsWrap,
    el('div', { class: 'fld-row mt' },
      el('button', { class: 'btn primary small', onclick: () => alertEditor(null) }, '+ New alert rule')));

  async function drawAlerts() {
    try {
      const data = await api.alerts();
      alertsWrap.innerHTML = '';
      for (const r of data.rules || []) {
        alertsWrap.appendChild(el('div', { class: 'row', style: { marginBottom: '6px', flexWrap: 'wrap' } },
          el('span', { class: 'badge ' + (r.enabled ? 'vio' : 'gry') }, r.enabled ? 'ON' : 'OFF'),
          el('span', { style: { fontWeight: '600' } }, r.name),
          el('span', { class: 'faint small' }, `${r.type} · threshold ${r.threshold} · every ${r.cooldown_s}s`),
          el('span', { class: 'grow' }),
          el('button', { class: 'btn small', onclick: () => alertEditor(r) }, 'Edit'),
          el('button', { class: 'btn small danger', onclick: async () => {
            await api.alertDelete(r.id); drawAlerts();
          } }, 'Delete')));
      }
      if (!(data.rules || []).length) alertsWrap.appendChild(el('div', { class: 'dim small' }, 'No rules configured.'));
      const hist = (data.history || []).slice(0, 5);
      if (hist.length) {
        alertsWrap.appendChild(el('div', { class: 'dim small mt' }, 'Recent firings:'));
        for (const h of hist) {
          alertsWrap.appendChild(el('div', { class: 'faint small mono' },
            `${fmtTime(h.ts)} — ${h.name}: ${h.text}`));
        }
      }
    } catch (e) { alertsWrap.appendChild(el('div', { class: 'dim small' }, 'Alert engine unavailable')); }
  }

  function alertEditor(existing) {
    modal(existing ? 'Edit alert rule' : 'New alert rule', (body) => {
      const name = el('input', { value: existing?.name || '', placeholder: 'CPU running hot', style: { width: '240px' } });
      const type = el('select', {},
        el('option', { value: 'cpu_high', ...(existing?.type === 'cpu_high' ? { selected: true } : {}) }, 'CPU usage above threshold'),
        el('option', { value: 'ram_high', ...(existing?.type === 'ram_high' ? { selected: true } : {}) }, 'RAM usage above threshold'),
        el('option', { value: 'disk_low', ...(existing?.type === 'disk_low' ? { selected: true } : {}) }, 'Disk free space below threshold'),
        el('option', { value: 'device_offline', ...(existing?.type === 'device_offline' ? { selected: true } : {}) }, 'Device goes offline'),
        el('option', { value: 'device_online', ...(existing?.type === 'device_online' ? { selected: true } : {}) }, 'Device comes online'),
        el('option', { value: 'transfer_failed', ...(existing?.type === 'transfer_failed' ? { selected: true } : {}) }, 'Transfer fails'),
        el('option', { value: 'transfer_completed', ...(existing?.type === 'transfer_completed' ? { selected: true } : {}) }, 'Transfer completes'),
        el('option', { value: 'companion_error', ...(existing?.type === 'companion_error' ? { selected: true } : {}) }, 'Companion error logged'));
      const threshold = el('input', { type: 'number', value: existing?.threshold ?? 90, style: { width: '90px' } });
      const duration = el('input', { type: 'number', value: existing?.duration_s ?? 30, style: { width: '90px' } });
      const cooldown = el('input', { type: 'number', value: existing?.cooldown_s ?? 300, style: { width: '90px' } });
      const sound = el('input', { type: 'checkbox', checked: existing?.notify?.sound || false });
      const chat = el('input', { type: 'checkbox', checked: existing?.notify?.chat !== false });
      body.appendChild(el('div', { class: 'fld-row' },
        el('label', { class: 'fld' }, 'Rule name', name),
        el('label', { class: 'fld' }, 'Watch', type)));
      body.appendChild(el('div', { class: 'fld-row' },
        el('label', { class: 'fld' }, 'Threshold (%) for metric rules', threshold),
        el('label', { class: 'fld' }, 'Hold duration (s)', duration),
        el('label', { class: 'fld' }, 'Cooldown (s)', cooldown)));
      body.appendChild(el('div', { class: 'fld-row' },
        el('label', { class: 'row small', style: { gap: '6px' } }, chat, 'GhostChat alert entry'),
        el('label', { class: 'row small', style: { gap: '6px' } }, sound, 'Sound cue')));
      body._f = { name, type, threshold, duration, cooldown, sound, chat };
    }, [
      { label: 'Cancel', handler: (close) => close() },
      { label: 'Save rule', kind: 'primary', handler: async (close, body) => {
        const f = body._f;
        await api.alertSet({
          id: existing?.id, name: f.name.value.trim() || undefined,
          type: f.type.value, enabled: true,
          threshold: Number(f.threshold.value), duration_s: Number(f.duration.value),
          cooldown_s: Number(f.cooldown.value),
          notify: { event: true, chat: f.chat.checked, sound: f.sound.checked },
        });
        close(); drawAlerts(); toast('Alert rule saved', 'ok');
      } },
    ]);
  }

  /* ---------- automation: watched folders & schedules (v1.3.0) ---------- */
  const autoWrap = el('div', {});
  const autoPanel = el('div', { class: 'panel' },
    el('h3', {}, 'Watched folders & scheduled jobs'),
    el('div', { class: 'dim small', style: { marginBottom: '8px' } },
      'Optional automation executed through the verified transfer engine. One-way and ',
      'additive only: deletions are never mirrored, backups are never pruned automatically.'),
    autoWrap,
    el('div', { class: 'fld-row mt' },
      el('button', { class: 'btn primary small', onclick: () => jobEditor(null) }, '+ New job')));

  async function drawAuto() {
    try {
      const data = await api.automation();
      autoWrap.innerHTML = '';
      for (const j of data.jobs || []) {
        autoWrap.appendChild(el('div', { class: 'row', style: { marginBottom: '6px', flexWrap: 'wrap' } },
          el('span', { class: 'badge ' + (j.enabled && !j.paused ? 'grn' : 'gry') },
            j.running ? 'RUNNING' : (j.enabled && !j.paused ? 'ACTIVE' : 'PAUSED')),
          el('span', { style: { fontWeight: '600' } }, j.name),
          el('span', { class: 'faint small' },
            `${j.kind} · ${j.mode} · ${j.src_path || ''} → ${j.dst_path || ''}`),
          el('span', { class: 'grow' }),
          el('button', { class: 'btn small', onclick: () => api.automationControl(j.id, 'run').then(() => toast('Job started', 'ok')) }, 'Run now'),
          el('button', { class: 'btn small', onclick: () => api.automationControl(j.id, j.paused ? 'resume' : 'pause').then(drawAuto) },
            j.paused ? 'Resume' : 'Pause'),
          el('button', { class: 'btn small', onclick: () => jobEditor(j) }, 'Edit'),
          el('button', { class: 'btn small danger', onclick: async () => {
            await api.automationDelete(j.id); drawAuto();
          } }, 'Delete')));
        if (j.last_result) {
          autoWrap.appendChild(el('div', { class: 'faint small mono', style: { marginBottom: '6px' } },
            `last run: ${fmtTime(j.last_run)} — ${j.last_result}`));
        }
      }
      if (!(data.jobs || []).length) autoWrap.appendChild(el('div', { class: 'dim small' }, 'No jobs configured.'));
    } catch (e) { autoWrap.appendChild(el('div', { class: 'dim small' }, 'Automation engine unavailable')); }
  }

  function jobEditor(existing) {
    modal(existing ? 'Edit job' : 'New automation job', (body) => {
      const name = el('input', { value: existing?.name || '', placeholder: 'Nightly photo backup', style: { width: '240px' } });
      const kind = el('select', {},
        el('option', { value: 'schedule', ...(existing?.kind === 'schedule' ? { selected: true } : {}) }, 'Scheduled run (interval)'),
        el('option', { value: 'watch', ...(existing?.kind === 'watch' ? { selected: true } : {}) }, 'Watched folder (run on changes)'));
      const srcDevice = el('input', { value: existing?.src_device || '', placeholder: 'empty = this computer', style: { width: '200px' } });
      const dstDevice = el('input', { value: existing?.dst_device || '', placeholder: 'empty = this computer', style: { width: '200px' } });
      const srcPath = el('input', { value: existing?.src_path || '', placeholder: '/home/me/Photos or C:\Users\me\Pictures', style: { width: '260px' } });
      const dstPath = el('input', { value: existing?.dst_path || '', placeholder: '/mnt/backup/Photos', style: { width: '260px' } });
      const interval = el('input', { type: 'number', value: existing?.interval_minutes ?? 60, style: { width: '90px' } });
      const exts = el('input', { value: (existing?.extensions || []).join(','), placeholder: 'jpg,png (empty = all files)', style: { width: '200px' } });
      body.appendChild(el('div', { class: 'fld-row' },
        el('label', { class: 'fld' }, 'Job name', name),
        el('label', { class: 'fld' }, 'Kind', kind)));
      body.appendChild(el('div', { class: 'fld-row' },
        el('label', { class: 'fld' }, 'Source device (name)', srcDevice),
        el('label', { class: 'fld' }, 'Destination device (name)', dstDevice)));
      body.appendChild(el('div', { class: 'fld-row' },
        el('label', { class: 'fld' }, 'Source path (must be authorized)', srcPath),
        el('label', { class: 'fld' }, 'Destination path (must be authorized)', dstPath)));
      body.appendChild(el('div', { class: 'fld-row' },
        el('label', { class: 'fld' }, 'Interval (minutes, schedule kind)', interval),
        el('label', { class: 'fld' }, 'File extensions filter', exts)));
      body.appendChild(el('div', { class: 'dim small' },
        'Device names must match the Network Radar exactly (e.g. "Ubuntu Station"). ',
        'Both paths must be inside authorized shares. Nothing is deleted at the destination.'));
      body._f = { name, kind, srcDevice, dstDevice, srcPath, dstPath, interval, exts };
    }, [
      { label: 'Cancel', handler: (close) => close() },
      { label: 'Save job', kind: 'primary', handler: async (close, body) => {
        const f = body._f;
        const devId = (label) => {
          const v = label.value.trim();
          if (!v) return '';
          const p = state.peers.find(x => (x.display_name || '').toLowerCase() === v.toLowerCase()
            || (x.name || '').toLowerCase() === v.toLowerCase());
          if (!p) throw new Error(`Device "${v}" not found in the radar`);
          return p.device_id;
        };
        try {
          await api.automationSet({
            id: existing?.id,
            name: f.name.value.trim() || undefined,
            kind: f.kind.value, mode: 'sync', enabled: true, paused: existing?.paused || false,
            src_device: devId(f.srcDevice), dst_device: devId(f.dstDevice),
            src_path: f.srcPath.value.trim(), dst_path: f.dstPath.value.trim(),
            compare: 'size', conflict: 'replace',
            interval_minutes: Number(f.interval.value),
            extensions: f.exts.value.split(',').map(x => x.trim()).filter(Boolean),
          });
          close(); drawAuto(); toast('Job saved', 'ok');
        } catch (e) { toast(errText(e), 'err', 6000); }
      } },
    ]);
  }

  /* data */
  const dataPanel = el('div', { class: 'panel' },
    el('h3', {}, 'Local data'),
    el('div', { class: 'row' },
      el('span', { class: 'dim small mono' }, state.self?.data_dir || ''),
      el('span', { class: 'grow' }),
      el('button', { class: 'btn small', onclick: () => {
        const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: 'application/json' });
        const a = el('a', { href: URL.createObjectURL(blob), download: 'phantom-config.json' });
        document.body.appendChild(a); a.click(); a.remove();
      } }, '⇩ Export config.json')));

  function kv(k, v) { return [el('div', { class: 'k' }, k), el('div', { class: 'v' }, String(v))]; }

  container.appendChild(identity);
  container.appendChild(network);
  container.appendChild(sharePanel);
  await drawShares();
  container.appendChild(transfers);
  container.appendChild(privacy);
  container.appendChild(mobilePanel);
  await drawMobile();
  container.appendChild(alertsPanel);
  await drawAlerts();
  container.appendChild(autoPanel);
  await drawAuto();
  container.appendChild(dataPanel);
  return () => {};
}
