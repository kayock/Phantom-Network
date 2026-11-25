/* ============================================================
   PHANTOM NETWORK — ui.js
   DOM helpers, formatting, toasts, modals, context menus.
   Views import { el, fmt*, toast, modal, confirmDialog, ... }
   ============================================================ */

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') node.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'html') node.innerHTML = v;
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? '' : v);
  }
  for (const child of children.flat(9)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

/* ---------------- formatting ---------------- */
export function fmtSize(n) {
  if (n === null || n === undefined) return '—';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let v = n;
  let i = -1;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

export function fmtBps(n) {
  if (!n || n <= 0) return '—';
  return fmtSize(n) + '/s';
}

export function fmtEta(s) {
  if (s === null || s === undefined || !isFinite(s)) return '—';
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  const h = Math.floor(s / 3600);
  return `${h}h ${Math.floor((s % 3600) / 60)}m`;
}

export function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const hm = d.toTimeString().slice(0, 5);
  return sameDay ? hm : `${d.toLocaleDateString()} ${hm}`;
}

export function fmtUptime(s) {
  if (s === null || s === undefined) return '—';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export function fmtPct(x) { return x === null || x === undefined ? '—' : `${x.toFixed(0)}%`; }

export function pathJoin(dir, name) {
  const sep = /[\\/]/.test(dir) && /\\/.test(dir) ? '\\' : (dir.includes('\\') ? '\\' : '/');
  if (/^[a-zA-Z]:\\?$/.test(dir) || /^[a-zA-Z]:$/.test(dir)) return dir.endsWith('\\') ? dir + name : dir + '\\' + name;
  return dir.endsWith('/') ? dir + name : dir.endsWith('\\') ? dir + name : dir + sep + name;
}

export function parentPath(p) {
  if (p === '/' || /^[a-zA-Z]:\\?$/.test(p)) return null;
  const norm = p.replace(/\\/g, '/');
  if (/^[a-zA-Z]:\/$/.test(norm)) return null;
  const idx = norm.replace(/\/$/, '').lastIndexOf('/');
  if (idx < 0) return null;
  const parent = norm.slice(0, idx + 1);
  // restore windows backslashes if the original used them
  if (p.includes('\\')) return parent.replace(/\//g, '\\');
  return parent || '/';
}

export function baseName(p) {
  const norm = p.replace(/[\\/]+$/, '');
  const idx = Math.max(norm.lastIndexOf('/'), norm.lastIndexOf('\\'));
  return idx >= 0 ? norm.slice(idx + 1) : norm;
}

export function fmtBytes(n) { return fmtSize(n); }

/* ---------------- toasts ---------------- */
export function toast(message, type = 'info', ms = 3800) {
  const layer = document.getElementById('toast-layer');
  const t = el('div', { class: `toast ${type}` }, message);
  layer.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; setTimeout(() => t.remove(), 320); }, ms);
}

/* ---------------- modal ---------------- */
export function modal(title, buildBody, actions = [], opts = {}) {
  const layer = document.getElementById('modal-layer');
  const card = document.getElementById('modal-card');
  card.innerHTML = '';
  card.appendChild(el('h3', {}, title));
  const body = el('div', { class: 'modal-body' });
  card.appendChild(body);
  const close = () => { layer.classList.add('hidden'); card.innerHTML = ''; };
  if (typeof buildBody === 'function') buildBody(body, close);
  else if (buildBody) body.appendChild(buildBody);
  const acts = el('div', { class: 'modal-actions' });
  for (const a of actions) {
    acts.appendChild(el('button', {
      class: `btn ${a.kind || ''}`,
      onclick: () => a.handler ? Promise.resolve(a.handler(close, body)).catch(e => toast(String(e.message || e), 'err')) : close(),
    }, a.label));
  }
  if (actions.length) card.appendChild(acts);
  layer.classList.remove('hidden');
  layer.onclick = (e) => { if (e.target === layer && !opts.sticky) close(); };
  return close;
}

export function confirmDialog(title, text, confirmLabel = 'Confirm', danger = true) {
  return new Promise(resolve => {
    modal(title, (body) => {
      body.appendChild(el('div', { class: 'dim', style: { lineHeight: '1.6' } }, text));
    }, [
      { label: 'Cancel', kind: '', handler: (close) => { close(); resolve(false); } },
      { label: confirmLabel, kind: danger ? 'danger' : 'primary', handler: (close) => { close(); resolve(true); } },
    ], { sticky: true });
  });
}

export function promptDialog(title, fields, confirmLabel = 'OK') {
  /* fields: [{name, label, value, type, placeholder}] → resolves object */
  return new Promise(resolve => {
    const inputs = {};
    modal(title, (body) => {
      for (const f of fields) {
        const input = el('input', {
          type: f.type || 'text', value: f.value || '',
          placeholder: f.placeholder || '', style: { width: '100%' },
        });
        if (f.type === 'textarea') {
          const ta = el('textarea', { placeholder: f.placeholder || '', style: { width: '100%', minHeight: '70px' } });
          ta.value = f.value || '';
          inputs[f.name] = ta;
          body.appendChild(el('label', { class: 'fld', style: { marginBottom: '10px' } }, f.label, ta));
        } else {
          inputs[f.name] = input;
          body.appendChild(el('label', { class: 'fld', style: { marginBottom: '10px' } }, f.label, input));
        }
      }
    }, [
      { label: 'Cancel', handler: (close) => { close(); resolve(null); } },
      {
        label: confirmLabel, kind: 'primary',
        handler: (close) => {
          const out = {};
          for (const [k, node] of Object.entries(inputs)) out[k] = node.value;
          close(); resolve(out);
        },
      },
    ], { sticky: true });
  });
}

/* ---------------- context menu ---------------- */
export function contextMenu(x, y, items) {
  const layer = document.getElementById('ctx-layer');
  layer.innerHTML = '';
  for (const item of items) {
    if (!item) continue;
    if (item === 'sep') { layer.appendChild(el('div', { style: { borderTop: '1px solid var(--line-soft)', margin: '4px 0' } })); continue; }
    layer.appendChild(el('div', {
      class: `ctx-item ${item.danger ? 'danger' : ''}`,
      onclick: () => { layer.classList.add('hidden'); item.handler(); },
    }, item.icon ? el('span', { class: 'nav-ico' }, item.icon) : null, item.label));
  }
  layer.classList.remove('hidden');
  const r = layer.getBoundingClientRect();
  layer.style.left = Math.min(x, window.innerWidth - r.width - 8) + 'px';
  layer.style.top = Math.min(y, window.innerHeight - r.height - 8) + 'px';
  const dismiss = (e) => {
    if (!layer.contains(e.target)) { layer.classList.add('hidden'); document.removeEventListener('mousedown', dismiss); }
  };
  setTimeout(() => document.addEventListener('mousedown', dismiss), 0);
}

/* ---------------- icons ---------------- */
const ICONS = {
  dir: '📁', file: '🗎', image: '🖼', audio: '🎵', video: '🎬',
  archive: '🗜', code: '📜', text: '📄', pdf: '📕', drive: '💽',
  link: '🔗', model: '🧠', exe: '⚙',
};
export function fileIcon(entry) {
  if (entry.type === 'dir') return ICONS.dir;
  if (entry.type === 'link') return ICONS.link;
  const ext = (entry.ext || '').toLowerCase();
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico'].includes(ext)) return ICONS.image;
  if (['mp3', 'wav', 'ogg', 'flac', 'm4a', 'aac'].includes(ext)) return ICONS.audio;
  if (['mp4', 'mkv', 'avi', 'mov', 'webm'].includes(ext)) return ICONS.video;
  if (['zip', 'tar', 'gz', '7z', 'rar', 'xz', 'bz2'].includes(ext)) return ICONS.archive;
  if (['py', 'js', 'ts', 'c', 'cpp', 'h', 'go', 'rs', 'java', 'sh', 'bat', 'ps1', 'json', 'yaml', 'yml', 'xml', 'html', 'css'].includes(ext)) return ICONS.code;
  if (['txt', 'md', 'log', 'csv', 'ini', 'cfg', 'conf', 'toml'].includes(ext)) return ICONS.text;
  if (ext === 'pdf') return ICONS.pdf;
  if (['safetensors', 'ckpt', 'pt', 'bin', 'gguf', 'onnx'].includes(ext)) return ICONS.model;
  if (['exe', 'msi', 'deb', 'appimage', 'rpm', 'apk'].includes(ext)) return ICONS.exe;
  return ICONS.file;
}

export function deviceIcon(peer) {
  const os = (peer.os || '').toLowerCase();
  if (os === 'windows') return '🖥️';
  if (os === 'linux') return '🐧';
  if (os === 'macos') return '';
  return '📟';
}

/* ---------------- misc ---------------- */
export function spinner(text = 'Working…') {
  return el('div', { class: 'empty-note' }, text);
}

export function emptyNote(text) { return el('div', { class: 'empty-note' }, text); }

export function debounce(fn, ms = 300) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

export function errText(e) { return e?.message || String(e); }
