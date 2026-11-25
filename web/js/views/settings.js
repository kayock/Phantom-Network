/* PHANTOM NETWORK — settings.js */

import { api } from '../api.js';
import { state } from '../state.js';
import { el, toast, confirmDialog, errText } from '../ui.js';

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
  container.appendChild(dataPanel);
  return () => {};
}
