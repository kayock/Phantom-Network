/* ============================================================
   PHANTOM NETWORK — clipboard.js (v1.4.1)

   Phantom Clipboard view:
   * history with search + pinned snippets
   * quick send to devices/groups with size + duplicate honesty
   * sync status + participation controls (auto sync OFF by default,
     pause, private mode, retention, caps)
   * copy back locally via navigator.clipboard, with the companion's
     native os-copy helper as fallback for dashboard-less operation
   * images shown as thumbnails (small PNG/JPEG data URLs only)
   ============================================================ */

import { api } from '../api.js';
import { state } from '../state.js';
import { el, toast, errText, fmtSize, fmtTime } from '../ui.js';

let cleanupFns = [];
function cleanup() { cleanupFns.forEach(f => { try { f(); } catch {} }); cleanupFns = []; }

export async function render(container) {
  cleanup();
  let items = [];
  let status = null;
  let selected = new Set();

  const searchIn = el('input', { placeholder: 'Search clipboard history…',
    style: { maxWidth: '260px' } });
  const syncEl = el('span', { class: 'small mono' }, '');
  const itemList = el('div', {});
  const targetList = el('div', { class: 'row', style: { flexWrap: 'wrap',
    gap: '6px', maxHeight: '130px', overflow: 'auto' } });
  const sendBtn = el('button', { class: 'btn violet' }, '⇪ Send to selected');
  const pasteOsBtn = el('button', { class: 'btn' }, '⇩ From OS clipboard');
  const copyOsBtn = el('button', { class: 'btn' }, '⇧ To OS clipboard');
  const clearBtn = el('button', { class: 'btn danger' }, '🗑 Clear history');
  const autoChk = el('input', { type: 'checkbox' });
  const pauseChk = el('input', { type: 'checkbox' });
  const privChk = el('input', { type: 'checkbox' });
  const retention = el('input', { type: 'number', min: 1, max: 365,
    style: { width: '70px' } });
  const maxText = el('input', { type: 'number', min: 1024,
    style: { width: '110px' } });
  const settingsNote = el('div', { class: 'small dim' }, '');

  /* ---------------- data ---------------- */
  async function loadMobileClients() {
    try {
      const r = await api.get('', '/api/mobile/clients');
      window.__pn_mobile_clients = r.clients || [];
    } catch {
      window.__pn_mobile_clients = window.__pn_mobile_clients || [];
    }
  }

  async function loadTargets() {
    await loadMobileClients();
    renderTargets();
  }

  async function loadAll() {
    try {
      const [s, itemsR] = await Promise.all([
        api.get('', '/api/clipboard/status'),
        api.get('', '/api/clipboard/items?q=' +
          encodeURIComponent(searchIn.value.trim())),
      ]);
      status = s; items = itemsR.items || [];
      autoChk.checked = !!s.auto_enabled;
      pauseChk.checked = !!s.paused;
      privChk.checked = !!s.private_mode;
      retention.value = s.retention_days;
      maxText.value = s.max_text;
      renderStatus(); renderItems(); renderTargets(); renderSettings();
    } catch (e) { toast(errText(e), 'err'); }
  }

  function renderStatus() {
    const h = status.helpers || {};
    const helper = Object.keys(h).filter(k => h[k]);
    const active = status.active_helper ? ` (using ${status.active_helper})` : '';
    syncEl.textContent = `auto-sync ${status.auto_enabled ? 'ON' : 'OFF'} · ` +
      `${status.paused ? 'PAUSED' : 'active'} · ` +
      (helper.length ? `native helper: ${helper.join('/')}${active}` :
        'no native helper (dashboard-driven only)');
    syncEl.style.color = status.paused ? 'var(--warn)' : 'var(--ok)';
  }

  function renderSettings() {
    settingsNote.textContent =
      'Automatic synchronization uses native OS helpers (xclip/wl-clipboard/' +
      'PowerShell) and keeps working with the dashboard closed where the ' +
      'OS permits. It is OFF by default. Phones sync manually — mobile ' +
      'OSes do not let a web page watch the clipboard. The companion ' +
      'makes NO claim of detecting passwords or sensitive content.';
  }

  function renderTargets() {
    targetList.innerHTML = '';
    const me = state.self && state.self.device_id;
    const defs = [];
    for (const p of state.peers || []) {
      if (p.device_id === me || p.status !== 'online') continue;
      defs.push({ kind: 'computer', device_id: p.device_id,
                  label: '💻 ' + (p.display_name || p.name || p.device_id) });
    }
    for (const c of window.__pn_mobile_clients || []) {
      defs.push({ kind: 'mobile', client_id: c.client_id,
                  host_device_id: c.host_device_id,
                  label: '📱 ' + (c.name || c.client_id) });
    }
    if (!defs.length) {
      targetList.appendChild(el('span', { class: 'dim small' },
        'No other participating devices online.'));
    }
    for (const t of defs) {
      const key = t.kind === 'mobile'
        ? 'm:' + t.client_id : 'c:' + t.device_id;
      targetList.appendChild(el('button', {
        class: 'chip' + (selected.has(key) ? ' sel' : ''),
        onclick: () => {
          selected.has(key) ? selected.delete(key) : selected.add(key);
          renderTargets();
        } }, t.label));
    }
  }

  function renderItems() {
    itemList.innerHTML = '';
    if (!items.length) {
      itemList.appendChild(el('span', { class: 'dim small' },
        'Clipboard history is empty — copy something or press ' +
        '"From OS clipboard".'));
      return;
    }
    for (const it of items) {
      const pinBtn = el('button', { class: 'btn small-btn',
        onclick: async () => {
          try {
            await api.post('', '/api/clipboard/pin',
              { id: it.id, pinned: !it.pinned });
            await loadAll();
          } catch (e) { toast(errText(e), 'err'); }
        } }, it.pinned ? '★' : '☆');
      const copyBtn = el('button', { class: 'btn small-btn cyan',
        onclick: async () => {
          if (it.kind === 'text') {
            try {
              await navigator.clipboard.writeText(it.text);
              toast('Copied to browser clipboard', 'ok');
            } catch {
              try {
                const ok = await api.post('', '/api/clipboard/os-copy',
                  { text: it.text });
                toast(ok.ok ? 'Copied via native helper'
                  : 'Browser blocked clipboard write — no native helper '
                    + 'available either', ok.ok ? 'ok' : 'err');
              } catch (e) { toast(errText(e), 'err'); }
            }
          }
        } }, '⧉ copy');
      const row = el('div', { style: { borderBottom: '1px solid #221c2b',
        padding: '6px 0' } },
        el('div', { class: 'row', style: { gap: '8px',
          alignItems: 'baseline' } },
          it.kind === 'image'
            ? el('img', { src: it.data_url, style: { maxHeight: '46px',
                borderRadius: '4px' } })
            : el('span', { class: 'mono small', style: { color:
                'var(--cyan)' } }, '$'),
          it.pinned ? el('span', { style: { color: 'var(--violet)' } },
            '★ pinned') : null,
          el('span', { class: 'small dim mono' }, fmtTime(it.ts)),
          el('span', { class: 'small dim' },
            `from ${it.origin && it.origin.name || '?'} · ${fmtSize(it.size)}`)),
        el('div', { class: 'mono small', style: { whiteSpace: 'pre-wrap',
          maxHeight: '70px', overflow: 'auto', marginTop: '3px' } },
          it.kind === 'text' ? it.text : '[image]'),
        el('div', { class: 'row', style: { gap: '6px', marginTop: '4px' } },
          copyBtn, pinBtn));
      itemList.appendChild(row);
    }
  }

  /* ---------------- actions ---------------- */
  sendBtn.onclick = async () => {
    const t = [];
    for (const key of selected) {
      if (key.startsWith('m:')) {
        const cid = key.slice(2);
        const c = (window.__pn_mobile_clients || [])
          .find(x => x.client_id === cid);
        t.push({ kind: 'mobile', client_id: cid,
                 host_device_id: c && c.host_device_id });
      } else {
        t.push({ kind: 'computer', device_id: key.slice(2) });
      }
    }
    const latest = items[0];
    if (!latest) { toast('Nothing in history to send', 'err'); return; }
    if (!t.length) { toast('Select at least one target', 'err'); return; }
    try {
      const r = await api.post('', '/api/clipboard/send', {
        kind: latest.kind, text: latest.text || '',
        data_url: latest.data_url || '', name: latest.name || '',
        targets: t,
      });
      const ok = (r.results || []).filter(x => x.state !== 'unavailable').length;
      toast(`Sent to ${ok}/${(r.results || []).length} device(s)` +
        (r.note ? ` — ${r.note}` : ''), ok ? 'ok' : 'info');
      await loadAll();
    } catch (e) { toast(errText(e), 'err'); }
  };

  pasteOsBtn.onclick = async () => {
    try {
      let text = '';
      try {
        text = await navigator.clipboard.readText();
      } catch {}
      if (!text) {
        const r = await api.get('', '/api/clipboard/os-paste');
        text = (r && r.text) || '';
      }
      if (!text) {
        toast('OS clipboard is empty or no native paste helper is available',
          'err');
        return;
      }
      const me = (state.self && state.self.device_id)
        || (status && status.device_id);
      if (!me) {
        toast('Local device id unavailable — cannot store capture', 'err');
        return;
      }
      await api.post('', '/api/clipboard/send', {
        kind: 'text', text,
        targets: [{ kind: 'computer', device_id: me }],
      });
      await loadAll();
      toast('Captured into history', 'ok');
    } catch (e) { toast(errText(e), 'err'); }
  };

  copyOsBtn.onclick = async () => {
    const latest = items[0];
    if (!latest || latest.kind !== 'text') {
      toast('Latest entry is not text', 'err'); return;
    }
    try {
      const ok = await api.post('', '/api/clipboard/os-copy',
        { text: latest.text });
      toast(ok.ok ? 'Written to OS clipboard via native helper'
        : 'No native helper available on this machine', ok.ok ? 'ok' : 'err');
    } catch (e) { toast(errText(e), 'err'); }
  };

  clearBtn.onclick = async () => {
    try {
      const r = await api.post('', '/api/clipboard/clear', { pins_too: false });
      toast(`History cleared — kept ${r.kept} pinned snippet(s)`, 'ok');
      await loadAll();
    } catch (e) { toast(errText(e), 'err'); }
  };

  autoChk.onchange = () => save({ clipboard_auto_enabled: autoChk.checked });
  pauseChk.onchange = () => save({ clipboard_paused: pauseChk.checked });
  privChk.onchange = () => save({ clipboard_private_mode: privChk.checked });
  retention.onchange = () =>
    save({ clipboard_retention_days: parseInt(retention.value || '30', 10) });
  maxText.onchange = () =>
    save({ clipboard_max_bytes: parseInt(maxText.value || '262144', 10) });

  async function save(patch) {
    try {
      await api.post('', '/api/clipboard/settings', patch);
      await loadAll();
    } catch (e) { toast(errText(e), 'err'); }
  }

  searchIn.oninput = () => loadAll();

  /* ---------------- layout ---------------- */
  container.append(
    el('div', { class: 'card' },
      el('h2', {}, '⧉ Phantom Clipboard'),
      el('p', { class: 'dim small' },
        'One shared clipboard for the household: text, commands, links ' +
        'and small screenshots. Content is inert data — nothing is ever ' +
        'executed automatically. Auto-sync is OFF by default and needs ' +
        'a native helper + your explicit opt-in.'),
      el('div', { class: 'row', style: { gap: '10px', marginTop: '6px' } },
        syncEl),
      el('div', { class: 'row', style: { gap: '8px', marginTop: '8px',
        flexWrap: 'wrap' } },
        searchIn, pasteOsBtn, copyOsBtn, clearBtn)),
    el('div', { class: 'card' },
      el('h3', {}, 'Send latest entry to'),
      targetList,
      el('div', { class: 'row', style: { marginTop: '8px' } }, sendBtn)),
    el('div', { class: 'card' },
      el('h3', {}, 'History'),
      itemList),
    el('div', { class: 'card' },
      el('h3', {}, 'Synchronization & participation'),
      el('div', { class: 'row', style: { gap: '18px', flexWrap: 'wrap' } },
        el('label', { class: 'small' }, ' automatic sync ', autoChk),
        el('label', { class: 'small' }, ' pause everything ', pauseChk),
        el('label', { class: 'small' }, ' private mode (drop incoming) ',
          privChk)),
      el('div', { class: 'row', style: { gap: '8px', marginTop: '8px',
        alignItems: 'center' } },
        el('span', { class: 'small dim' }, 'retention days'), retention,
        el('span', { class: 'small dim' }, 'text cap (bytes)'), maxText),
      settingsNote));

  await Promise.all([loadAll(), loadTargets()]);
  const unPeers = state.on('peers', () => renderTargets());
  const refresh = setInterval(loadAll, 8000);
  cleanupFns.push(unPeers, () => clearInterval(refresh));
}
