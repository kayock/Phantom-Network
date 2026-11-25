/* ============================================================
   PHANTOM NETWORK — mobile/app.js
   Dedicated mobile interface (independent browser client).

   Identity: this phone registers with its host companion and keeps
   {client_id, client_secret} in localStorage. Refreshing never
   re-registers. Recovering (cleared storage) mints a fresh identity;
   the orphaned one can be removed from the desktop UI.

   Everything runs against the HOST companion (same origin) — the
   phone runs no server of its own. See MOBILE_CLIENT_GUIDE.md.
   ============================================================ */

const LS_KEY = 'pn_mobile_identity';
const LS_CACHE = 'pn_mobile_msgcache';
const LS_PREFS = 'pn_mobile_prefs';

/* ---------------- tiny DOM helpers ---------------- */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') node.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? '' : v);
  }
  for (const child of children.flat(9)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}
function toast(message, type = 'info', ms = 3200) {
  const layer = document.getElementById('m-toasts');
  const t = el('div', { class: `mtoast ${type}` }, message);
  layer.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; setTimeout(() => t.remove(), 320); }, ms);
}
function sheet(items) {
  const layer = document.getElementById('m-sheet');
  const card = document.getElementById('m-sheet-card');
  card.innerHTML = '';
  for (const item of items) {
    card.appendChild(el('button', { class: item.danger ? 'danger' : '', onclick: () => { close(); item.handler(); } },
      item.icon ? `${item.icon}  ` : '', item.label));
  }
  layer.classList.remove('hidden');
  layer.onclick = (e) => { if (e.target === layer) close(); };
  function close() { layer.classList.add('hidden'); card.innerHTML = ''; }
}
function modal(title, buildBody, actions = []) {
  const layer = document.getElementById('m-modal');
  const card = document.getElementById('m-modal-card');
  card.innerHTML = '';
  card.appendChild(el('h3', { style: { margin: '0 0 10px', color: '#e9d5ff' } }, title));
  const body = el('div', {});
  card.appendChild(body);
  const close = () => { layer.classList.add('hidden'); card.innerHTML = ''; };
  buildBody(body, close);
  const acts = el('div', { class: 'row', style: { marginTop: '14px', justifyContent: 'flex-end' } });
  for (const a of actions) {
    acts.appendChild(el('button', { class: `btn ${a.kind || ''}`, onclick: () => a.handler ? Promise.resolve(a.handler(close, body)).catch(e => toast(String(e.message || e), 'err')) : close() }, a.label));
  }
  if (actions.length) card.appendChild(acts);
  layer.classList.remove('hidden');
}
function fmtSize(n) {
  if (n === null || n === undefined) return '—';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB']; let v = n, i = -1;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}
function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  const hm = d.toTimeString().slice(0, 5);
  return d.toDateString() === new Date().toDateString() ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}
function fmtAgo(ts) {
  if (!ts) return 'never';
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 45) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function errText(e) { return e?.message || String(e); }

/* ============================================================
   state
   ============================================================ */
export const M = {
  ident: null,          // {client_id, client_secret, name, device_type, icon}
  host: null,           // host companion identity snapshot
  sse: null,
  connected: false,
  peers: [],            // computers
  clients: [],          // other hosted mobile clients
  convs: {},            // conv → [messages] (local cache, newest last)
  unread: {},           // conv → count
  prefs: { sounds: true, mentions: false, transfers: false, silent: false },
  activeConv: null,     // conv key when conversation open
  lastSync: {},         // conv → ts fetched
  deviceCache: null,    // {device: peerDict, path: {entries}}
  tab: 'home',

  save() { try { localStorage.setItem(LS_KEY, JSON.stringify(this.ident)); } catch {} },
  load() {
    try { this.ident = JSON.parse(localStorage.getItem(LS_KEY) || 'null'); } catch { this.ident = null; }
    try { this.prefs = { ...this.prefs, ...JSON.parse(localStorage.getItem(LS_PREFS) || '{}') }; } catch {}
  },
  savePrefs() { try { localStorage.setItem(LS_PREFS, JSON.stringify(this.prefs)); } catch {} },
  cacheSave() {
    try { localStorage.setItem(LS_CACHE, JSON.stringify({ convs: this.convs, unread: this.unread, lastSync: this.lastSync })); } catch {}
  },
  cacheLoad() {
    try {
      const d = JSON.parse(localStorage.getItem(LS_CACHE) || 'null');
      if (d) { this.convs = d.convs || {}; this.unread = d.unread || {}; this.lastSync = d.lastSync || {}; }
    } catch {}
  },

  async api(path, body = {}, method = 'POST') {
    const resp = await fetch(path, {
      method, headers: { 'Content-Type': 'application/json' },
      body: method === 'GET' ? undefined : JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({ ok: false, message: 'bad JSON' }));
    if (!resp.ok || data.ok === false) throw new Error(data.message || `HTTP ${resp.status}`);
    return data;
  },

  authed(body = {}) {
    return { client_id: this.ident.client_id, client_secret: this.ident.client_secret, ...body };
  },

  async refreshFleet() {
    try {
      const [peersData, clientsData] = await Promise.all([
        this.api('/api/peers', {}, 'GET'),
        this.api('/api/mobile/clients', {}, 'GET'),
      ]);
      this.peers = [peersData.self, ...(peersData.peers || [])];
      this.clients = (clientsData.clients || []).filter(c => c.client_id !== this.ident.client_id);
      document.dispatchEvent(new CustomEvent('m-refresh'));
    } catch (e) { /* host busy */ }
  },
};

/* device helpers */
const TYPE_ICONS = { phone: '📱', tablet: '📲', laptop: '💻', watch: '⌚', other: '📟' };
function computerIcon(p) {
  const os = (p.os || '').toLowerCase();
  if (p.device_id === M.ident?.host_device_id) return '🏠';
  if (os === 'windows') return '🖥️';
  if (os === 'linux') return '🐧';
  if (os === 'darwin') return '🍎';
  return '📟';
}
export function devices() {
  /* every logical participant: self first, computers, other mobile clients */
  const self = {
    device_id: M.ident.client_id, name: M.ident.name, kind: 'mobile',
    self: true, online: true, device_type: M.ident.device_type,
    icon: M.ident.icon, host_device_id: M.ident.host_device_id,
  };
  const computers = M.peers.map(p => ({
    device_id: p.device_id, name: p.name, kind: 'computer',
    online: p.status === 'online', status: p.status,
    os: p.os, host_device_id: p.device_id,
    version: p.version, shares_count: p.shares_count, latency_ms: p.latency_ms,
    mobile_clients: p.mobile_clients || [],
    participating: p.participating !== false,
  }));
  const mobiles = M.clients.map(c => ({
    device_id: c.client_id, name: c.name, kind: 'mobile',
    online: c.online, device_type: c.device_type, icon: c.icon,
    host_device_id: c.host_device_id || c.host_name,
  }));
  return [self, ...computers, ...mobiles];
}
function deviceById(id) {
  return devices().find(d => d.device_id === id) || null;
}
function deviceName(id) {
  if (!id) return 'everyone';
  const d = deviceById(id);
  if (d) return d.name + (d.self ? ' (this device)' : '');
  return id;
}
function deviceIconFor(d) {
  if (d.kind === 'mobile') return d.icon || TYPE_ICONS[d.device_type] || '📱';
  return computerIcon(d);
}

/* ============================================================
   boot + onboarding
   ============================================================ */
async function boot() {
  M.load();
  M.cacheLoad();
  if (!M.ident) return showOnboarding();
  await startApp();
}

function showOnboarding() {
  document.getElementById('onboard').classList.remove('hidden');
  document.getElementById('mapp').classList.add('hidden');
  let icon = '📱';
  const icons = document.getElementById('ob-icons');
  for (const ic of Object.values(TYPE_ICONS)) {
    const b = el('button', { onclick: () => { icon = ic; icons.querySelectorAll('button').forEach(x => x.classList.remove('sel')); b.classList.add('sel'); } }, ic);
    icons.appendChild(b);
    if (ic === '📱') b.classList.add('sel');
  }
  document.getElementById('ob-type').addEventListener('change', (e) => {
    const map = { phone: '📱', tablet: '📲', laptop: '💻', watch: '⌚', other: '📟' };
    icon = map[e.target.value];
    icons.querySelectorAll('button').forEach(x => x.classList.remove('sel'));
  });
  // v1.4.0 — restore an identity from another origin (HTTP↔HTTPS move):
  // re-attach with client_id + client_secret via the existing secret-
  // verified register path (same id, same inbox; no new auth model).
  const restoreCard = document.createElement('div');
  restoreCard.innerHTML = `<details style="margin-top:12px">
    <summary style="cursor:pointer;color:#9b7fd4">Restore an existing identity</summary>
    <div style="margin-top:8px;display:flex;flex-direction:column;gap:8px">
      <input id="ob-rest-id" placeholder="client_id (mob_…)" autocomplete="off">
      <input id="ob-rest-secret" placeholder="client_secret" autocomplete="off">
      <button id="ob-rest-btn" class="btn"> Restore identity </button>
      <div class="faint small">Paste the identity JSON contents copied from
      the previous origin. The host verifies the secret and re-attaches the
      SAME id with its full inbox.</div>
    </div></details>`;
  document.getElementById('onboard').querySelector('.ocard, #ob-register')?.parentNode?.appendChild(restoreCard);
  document.getElementById('ob-rest-btn')?.addEventListener('click', async () => {
    const idv = (document.getElementById('ob-rest-id').value || '').trim();
    const secv = (document.getElementById('ob-rest-secret').value || '').trim();
    const btn = document.getElementById('ob-rest-btn');
    if (!idv || !secv) { toast('Both client_id and client_secret are required', 'err'); return; }
    btn.disabled = true;
    try {
      const data = await M.api('/api/mobile/register', {
        client_id: idv, client_secret: secv,
        ua: navigator.userAgent.slice(0, 100),
      });
      M.ident = {
        client_id: data.client.client_id, client_secret: secv,
        name: data.client.name, device_type: data.client.device_type,
        icon: data.client.icon, host_device_id: data.host.device_id,
        host_name: data.host.name, host_version: data.host.version,
        registered: M.ident?.registered || Date.now(),
      };
      M.save();
      location.reload();
    } catch (e) {
      toast(e.message || 'restore failed', 'err');
      btn.disabled = false;
    }
  });

  document.getElementById('ob-register').addEventListener('click', async () => {
    const name = document.getElementById('ob-name').value.trim() || 'My Phone';
    const type = document.getElementById('ob-type').value;
    const btn = document.getElementById('ob-register');
    btn.disabled = true;
    try {
      const data = await M.api('/api/mobile/register', {
        name, device_type: type, icon, ua: navigator.userAgent.slice(0, 100),
      });
      M.ident = {
        client_id: data.client.client_id, client_secret: data.client.secret,
        name: data.client.name, device_type: data.client.device_type,
        icon: data.client.icon, host_device_id: data.host.device_id,
        host_name: data.host.name, host_version: data.host.version,
        registered: Date.now(),
      };
      M.save();
      document.getElementById('onboard').classList.add('hidden');
      await startApp();
      toast(`Registered as ${name} on ${data.host.name}`, 'ok');
    } catch (e) {
      document.getElementById('ob-err').textContent = errText(e);
      btn.disabled = false;
    }
  });
}

async function startApp() {
  document.getElementById('mapp').classList.remove('hidden');
  document.getElementById('m-title').textContent = M.ident.name;
  connectStream();
  await M.refreshFleet();
  await syncInbox();
  setInterval(() => { heartbeat(); }, 10000);
  setInterval(() => { M.refreshFleet(); }, 8000);
  setInterval(() => { syncInbox(); }, 20000);
  wireTabs();
  renderTab();
  document.addEventListener('m-refresh', () => {
    if (M.tab === 'home' || M.tab === 'devices') renderTab();
    updateBadge();
  });
  updateBadge();
  registerServiceWorker();
}

function registerServiceWorker() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').then(reg => {
      reg.addEventListener('updatefound', () => toast('Phantom update installing…', 'info'));
    }).catch(() => { /* http origin: PWA install needs HTTPS; app works anyway */ });
  }
}

/* ---------------- presence heartbeat ---------------- */
let hbBusy = false;
async function heartbeat() {
  if (hbBusy || document.hidden && Math.random() > 0.15) return; // best effort
  hbBusy = true;
  try {
    await M.api('/api/mobile/heartbeat', M.authed());
    if (!M.connected) { M.connected = true; setConn(true); }
  } catch (e) {
    if (M.connected !== 'down') { M.connected = false; setConn(false); }
  } finally { hbBusy = false; }
}
function setConn(up) {
  const dot = document.getElementById('m-conn');
  dot.className = 'mconn ' + (up ? 'up' : 'down');
  dot.textContent = up ? '●' : '○';
}

/* ---------------- SSE ---------------- */
function connectStream() {
  if (M.sse) M.sse.close();
  const url = `/api/mobile/stream?client_id=${encodeURIComponent(M.ident.client_id)}&secret=${encodeURIComponent(M.ident.client_secret)}`;
  const es = new EventSource(url);
  M.sse = es;
  es.addEventListener('mobile', (ev) => {
    let data = null;
    try { data = JSON.parse(ev.data); } catch { return; }
    handleMobileEvent(data);
  });
  es.onopen = () => { M.connected = true; setConn(true); syncInbox(); };
  es.onerror = () => { M.connected = false; setConn(false); /* EventSource auto-reconnects */ };
}

function handleMobileEvent(data) {
  if (data.type === 'message' && data.data) {
    const msg = data.data;
    stashMessage(msg);
    if (M.tab === 'chat' && M.activeConv === msg.conv) renderTab();
    else {
      M.unread[msg.conv] = (M.unread[msg.conv] || 0) + 1;
      updateBadge();
      M.cacheSave();
    }
    notifyIncoming(msg);
  } else if (data.type === 'receipt' && data.data) {
    const { ids, state } = data.data;
    const marks = new Set(ids);
    for (const msgs of Object.values(M.convs)) {
      for (const m of msgs) {
        if (marks.has(m.id) && m.outgoing) m.status = state;
      }
    }
    M.cacheSave();
    if (M.tab === 'chat' && M.activeConv) renderTab();
  } else if (data.type === 'voice' && data.data) {
    handleVoiceEvent(data.data);
  } else if (data.type === 'clipboard' && data.data) {
    if (M.tab === 'clip') renderTab();
    const d = data.data;
    if (d.kind === 'text' && d.text) toast('⧉ Clipboard: ' + d.text.slice(0, 60));
  } else if (data.type === 'broadcast' && data.data) {
    const d = data.data;
    toast('📣 ' + (d.text || 'Announcement'));
    if (d.tone && M.prefs.broadcast_tones !== false) { try { beepTone(); } catch {} }
  } else if (data.type === 'presence' || data.type === 'client_updated') {
    M.refreshFleet();
  }
}

function beepTone() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.frequency.value = 660; g.gain.value = 0.18;
    o.connect(g); g.connect(ctx.destination);
    o.start();
    setTimeout(() => { o.stop(); ctx.close(); }, 260);
  } catch {}
}

/* ---------------- Voice (v1.4.0) ---------------- */
const mrtc = {
  pc: null, local: null, remote: null, audioEl: null,
  callId: '', role: null, muted: false,
  caps() {
    return {
      webrtc: typeof RTCPeerConnection !== 'undefined',
      mic: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
      secure: window.isSecureContext || ['localhost', '127.0.0.1'].includes(location.hostname),
    };
  },
  async connect(kind = 'call') {
    const c = this.caps();
    if (!c.webrtc) throw new Error('This browser has no WebRTC support');
    if (!c.secure) throw new Error('Microphone needs HTTPS — ask the host for the secure port');
    this.pc = new RTCPeerConnection({ iceServers: (window.PHANTOM_STUN || []) });
    this.pc.onicecandidate = (ev) => {
      if (ev.candidate) this.signal('ice', ev.candidate.toJSON());
    };
    this.pc.ontrack = (ev) => {
      this.remote = ev.streams[0];
      if (!this.audioEl) { this.audioEl = new Audio(); this.audioEl.autoplay = true; }
      this.audioEl.srcObject = this.remote;
      this.audioEl.play().catch(() => toast('Tap once to allow audio playback'));
    };
    this.pc.onconnectionstatechange = () => {
      if (this.pc.connectionState === 'failed') {
        toast('Voice connection failed — host may need the PCM intercom fallback', 'err');
      }
    };
    if (kind !== 'screen') {
      this.local = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      });
      for (const t of this.local.getTracks()) this.pc.addTrack(t, this.local);
    }
    if (this.role === 'caller') {
      const offer = await this.pc.createOffer();
      await this.pc.setLocalDescription(offer);
      this.signal('offer', { sdp: this.pc.localDescription.sdp, type: this.pc.localDescription.type, media: kind });
    }
  },
  signal(kind, payload) {
    if (!this.callId) return;
    M.api('/api/voice/signal', { call_id: this.callId, kind, payload }).catch(() => {});
  },
  teardown() {
    if (this.local) for (const t of this.local.getTracks()) t.stop();
    if (this.pc) { try { this.pc.close(); } catch {} }
    if (this.audioEl) this.audioEl.srcObject = null;
    this.pc = this.local = this.remote = null;
    this.callId = ''; this.role = null;
  },
};

async function handleVoiceEvent(d) {
  if (d.type === 'voice_invite' && !mrtc.callId) {
    M.ring = d;
    beepTone();
    setTimeout(beepTone, 700);
    showRingSheet(d);
  } else if (d.type === 'voice_update') {
    const c = d.data || d;
    if (M.ring && c.id === M.ring.id && ['ended', 'rejected', 'missed'].includes(c.state)) {
      M.ring = null; toast('Call ' + c.state);
      if (M.tab === 'voice') renderTab();
    }
    if (mrtc.callId === c.id && ['ended', 'rejected', 'missed'].includes(c.state)) {
      mrtc.teardown();
      toast('Call ended');
      if (M.tab === 'voice') renderTab();
    }
  }
}

function showRingSheet(d) {
  const who = (d.origin && d.origin.name) || 'a device';
  const sheetEl = modal(`☎ Incoming call — ${who}`, (body) => {
    body.appendChild(el('div', { class: 'dim small' },
      'Accept to open a direct WebRTC audio session with ' + who +
      '. Your microphone becomes active only after you accept.'));
  }, [
    { label: '✓ Accept', class: 'btn cyan', onclick: async () => {
      const call = M.ring; M.ring = null;
      try {
        await M.api('/api/voice/answer', { call_id: call.id, accept: true });
        mrtc.callId = call.id; mrtc.role = 'callee';
        await mrtc.connect('call');
        goTab('voice');
        renderTab();
      } catch (e) { toast(e.message || 'accept failed', 'err'); mrtc.teardown(); }
    } },
    { label: '✕ Decline', class: 'btn danger', onclick: async () => {
      const call = M.ring; M.ring = null;
      try { await M.api('/api/voice/answer', { call_id: call.id, accept: false }); } catch {}
    } },
  ]);
}

async function renderVoice(view) {
  const caps = mrtc.caps();
  view.appendChild(el('div', { class: 'card' },
    el('div', { class: 'dim small' },
      caps.secure ? 'Direct WebRTC calls on your LAN — every call needs an explicit accept.'
        : '⚠ Insecure context: microphone needs HTTPS — open the host’s secure port.')));

  if (mrtc.callId) {
    const dur = el('span', { class: 'mono' }, '00:00');
    const t0 = Date.now();
    const timer = setInterval(() => {
      const s = Math.floor((Date.now() - t0) / 1000);
      dur.textContent = String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
    }, 500);
    view.appendChild(el('div', { class: 'card' },
      el('h3', {}, '☎ In call'),
      el('div', { class: 'spread' }, el('span', { class: 'dim' }, 'duration'), dur),
      el('div', { class: 'row' },
        el('button', { class: 'btn', onclick: () => {
          mrtc.muted = !mrtc.muted;
          if (mrtc.local) for (const t of mrtc.local.getAudioTracks()) t.enabled = !mrtc.muted;
          toast(mrtc.muted ? 'Muted' : 'Unmuted');
        } }, mrtc.muted ? '🎙 Unmute' : '🎙 Mute'),
        el('button', { class: 'btn danger', onclick: async () => {
          clearInterval(timer);
          try { await M.api('/api/voice/end', { call_id: mrtc.callId }); } catch {}
          mrtc.teardown(); renderTab();
        } }, '✖ Hang up'))));
    return;
  }

  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Call a device'),
    (() => {
      const sel = el('select', { style: { width: '100%' } });
      for (const p of M.peers || []) {
        if (p.self || p.status !== 'online') continue;
        sel.appendChild(el('option', { value: JSON.stringify({ kind: 'computer', device_id: p.device_id, name: p.name }) },
          (p.icon || '💻') + ' ' + p.name));
      }
      for (const d of devices()) {
        if (d.self || d.client_id === M.ident.client_id) continue;
        sel.appendChild(el('option', { value: JSON.stringify({ kind: 'mobile', client_id: d.client_id, name: d.name, host_device_id: d.host_device_id }) },
          '📱 ' + d.name));
      }
      if (!sel.children.length) sel.appendChild(el('option', {}, 'no other devices online'));
      const btn = el('button', { class: 'btn cyan', style: { width: '100%', marginTop: '8px' }, onclick: async () => {
        if (!sel.value || !sel.value.startsWith('{')) { toast('No callable device', 'err'); return; }
        try {
          const r = await M.api('/api/voice/call', { mode: 'call', targets: [JSON.parse(sel.value)] });
          mrtc.callId = r.call.id; mrtc.role = 'caller';
          await mrtc.connect('call');
          renderTab();
        } catch (e) { toast(e.message || 'call failed', 'err'); }
      } }, '☎ Call');
      const wrap = el('div', {}, sel, btn);
      return wrap;
    })()));

  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Push-to-talk'),
    el('button', { class: 'btn', style: { width: '100%' }, onclick: () => {
      toast('Use the desktop Intercom view or the companion PCM channel for classic PTT');
    } }, '◐ Open intercom (PCM fallback)')));

  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Announce (Broadcast)'),
    (() => {
      const input = el('input', { placeholder: 'Announcement text…', style: { width: '100%' } });
      const btn = el('button', { class: 'btn violet', style: { width: '100%', marginTop: '8px' }, onclick: async () => {
        if (!input.value.trim()) { toast('Type an announcement', 'err'); return; }
        try {
          await M.api('/api/broadcast/announce', {
            text: input.value.trim(), urgency: 'normal',
            targets: [{ kind: 'mobile', client_id: M.ident.client_id }],
          });
          input.value = '';
          toast('Announced (this device receives the banner; other devices depend on their participation settings)', 'ok');
        } catch (e) { toast(e.message || 'failed', 'err'); }
      } }, '📣 Announce');
      return el('div', {}, input, btn);
    })()));
}

async function renderMedia(view) {
  view.appendChild(el('div', { class: 'card' },
    el('div', { class: 'dim small' }, 'Shared queues and synchronized playback. Files stream over HTTP Range from the device that owns them.')));
  let rooms = [];
  try { rooms = (await M.api('/api/media/rooms', {}, 'GET')).rooms || []; } catch {}
  if (!rooms.length) {
    view.appendChild(el('div', { class: 'card' },
      el('span', { class: 'dim small' }, 'No media rooms yet — create one on a computer.')));
    return;
  }
  for (const r of rooms) {
    const card = el('div', { class: 'card' });
    card.appendChild(el('div', { class: 'row' },
      el('h3', {}, '♪ ' + r.name),
      el('span', { class: 'dim small' }, r.playing ? '● playing' : 'paused')));
    try {
      const st = (await M.api('/api/media/state?room_id=' + encodeURIComponent(r.id), {}, 'GET')).room;
      const item = st.item;
      card.appendChild(el('div', { class: 'small' },
        item ? (st.playing ? '▶ ' : '⏸ ') + item.name + ' · ' + Math.round(st.offset_s || 0) + 's'
             : 'queue empty'));
      const audio = new Audio();
      audio.preload = 'none';
      card.appendChild(el('div', { class: 'row' },
        el('button', { class: 'btn cyan', onclick: async () => {
          await M.api('/api/media/control', { room_id: r.id, action: 'play' });
          if (item && item.stream_url) {
            audio.src = item.stream_url; audio.volume = st.max_volume || 0.8;
            audio.currentTime = st.offset_s || 0;
            audio.play().catch(() => toast('Tap play again to allow audio'));
          }
          renderTab();
        } }, '▶ Play'),
        el('button', { class: 'btn', onclick: async () => {
          audio.pause();
          await M.api('/api/media/control', { room_id: r.id, action: 'pause' });
          renderTab();
        } }, '⏸'),
        el('button', { class: 'btn', onclick: async () => {
          await M.api('/api/media/control', { room_id: r.id, action: 'next' });
          renderTab();
        } }, '⏭'),
        el('button', { class: 'btn', onclick: async () => {
          await M.api('/api/media/control', { room_id: r.id, action: 'stop' });
          audio.pause(); audio.src = ''; renderTab();
        } }, '⏹')));
      card.appendChild(el('div', { class: 'small dim' },
        'Sync precision is ±50–150 ms on a LAN — good for music, not sample-accurate.'));
    } catch (e) {
      card.appendChild(el('span', { class: 'small', style: { color: 'var(--err)' } }, e.message));
    }
    view.appendChild(card);
  }
}

async function renderClip(view) {
  view.appendChild(el('div', { class: 'card' },
    el('div', { class: 'dim small' },
      'Shared clipboard. Phones sync manually (mobile OSes do not let a web page watch the clipboard) — copy, pin and send entries.')));
  const list = el('div', {});
  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'History'),
    (() => {
      const search = el('input', { placeholder: 'Search…', style: { width: '100%' } });
      const btn = el('button', { class: 'btn', style: { width: '100%', marginTop: '6px' }, onclick: load }, '↻ Refresh');
      async function load() {
        try {
          const r = await M.api('/api/clipboard/items?q=' + encodeURIComponent(search.value.trim()), {}, 'GET');
          list.innerHTML = '';
          const items = r.items || [];
          if (!items.length) {
            list.appendChild(el('span', { class: 'dim small' }, 'Empty.'));
            return;
          }
          for (const it of items.slice(0, 30)) {
            list.appendChild(el('div', { style: { borderBottom: '1px solid #221c2b', padding: '5px 0' } },
              el('div', { class: 'row' },
                it.pinned ? el('span', { style: { color: 'var(--violet)' } }, '★') : null,
                el('span', { class: 'dim small mono' }, new Date(it.ts).toLocaleTimeString()),
                el('span', { class: 'dim small' }, 'from ' + (it.origin && it.origin.name || '?'))),
              el('div', { class: 'mono small', style: { whiteSpace: 'pre-wrap', maxHeight: '60px', overflow: 'auto' } },
                it.kind === 'text' ? it.text : '[image]'),
              el('div', { class: 'row', style: { marginTop: '4px' } },
                el('button', { class: 'btn small-btn', onclick: async () => {
                  if (it.kind !== 'text') return;
                  try {
                    await navigator.clipboard.writeText(it.text);
                    toast('Copied to phone clipboard', 'ok');
                  } catch { toast('Browser blocked clipboard write', 'err'); }
                } }, '⧉ copy'),
                el('button', { class: 'btn small-btn', onclick: async () => {
                  await M.api('/api/clipboard/pin', { id: it.id, pinned: !it.pinned });
                  load();
                } }, it.pinned ? '☆ unpin' : '★ pin'))));
          }
        } catch (e) {
          list.innerHTML = '';
          list.appendChild(el('span', { class: 'small', style: { color: 'var(--err)' } }, e.message));
        }
      }
      search.oninput = load;
      setTimeout(load, 0);
      return el('div', {}, search, btn, list);
    })()));
}

function stashMessage(msg) {
  const conv = msg.conv;
  if (!M.convs[conv]) M.convs[conv] = [];
  const arr = M.convs[conv];
  if (arr.some(m => m.id === msg.id)) return;    // duplicate prevention
  arr.push(msg);
  if (arr.length > 300) arr.splice(0, arr.length - 300);
}

async function syncInbox() {
  try {
    const maxTs = Math.max(0, ...Object.values(M.lastSync).map(x => x || 0), 0);
    const data = await M.api('/api/mobile/inbox', M.authed({ since_ts: 0, limit: 300 }));
    for (const msg of data.messages || []) stashMessage(msg);
    M.lastSync._all = Date.now();
    // read marks: conversations currently open count as read
    if (M.activeConv) {
      M.unread[M.activeConv] = 0;
      const msgs = (M.convs[M.activeConv] || []).filter(m => !m.outgoing);
      const ids = msgs.map(m => m.id);
      if (ids.length) {
        M.api('/api/mobile/chat/ack', M.authed({ ids, state: 'read' })).catch(() => {});
      }
    }
    M.cacheSave();
    updateBadge();
    if (M.tab === 'chat') renderTab();
  } catch (e) { /* host unreachable */ }
}

function updateBadge() {
  const total = Object.values(M.unread).reduce((a, b) => a + b, 0);
  const badge = document.getElementById('m-chat-badge');
  badge.hidden = total === 0;
  badge.textContent = total > 99 ? '99+' : total;
}

function notifyIncoming(msg) {
  if (M.prefs.silent) return;
  const body = msg.body || (msg.attachment ? `[${msg.attachment.kind === 'voice' ? 'voice' : 'file'}: ${msg.attachment.name}]` : '');
  if (M.prefs.sounds) playBlip();
  if ('Notification' in window && Notification.permission === 'granted' && document.hidden) {
    try {
      new Notification(`GhostChat — ${msg.from?.name || 'unknown'}`, { body: body.slice(0, 120), tag: msg.id });
    } catch {}
  }
}
let audioCtx = null;
function playBlip() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    o.connect(g); g.connect(audioCtx.destination);
    o.type = 'sine'; o.frequency.value = 880;
    g.gain.setValueAtTime(0.12, audioCtx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.18);
    o.start(); o.stop(audioCtx.currentTime + 0.2);
  } catch {}
}

/* ============================================================
   tabs
   ============================================================ */
function wireTabs() {
  document.getElementById('m-tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.mtab');
    if (!btn) return;
    M.tab = btn.dataset.tab;
    M.activeConv = null;
    document.querySelectorAll('.mtab').forEach(t => t.classList.toggle('active', t === btn));
    renderTab();
    if (M.tab === 'chat') { M.unread = {}; updateBadge(); M.cacheSave(); }
  });
}

const VIEWS = { home: renderHome, chat: renderChat, devices: renderDevices, files: renderFiles, more: renderMore, voice: renderVoice, media: renderMedia, clip: renderClip };
function renderTab() {
  document.getElementById('m-title').textContent = M.activeConv ? 'Conversation'
    : { home: M.ident.name, chat: 'GhostChat', devices: 'Devices', files: 'Files', more: 'More', voice: 'Phantom Voice', media: 'Phantom Media', clip: 'Phantom Clipboard' }[M.tab] || M.tab;
  const view = document.getElementById('m-view');
  view.innerHTML = '';
  VIEWS[M.tab](view);
}

/* ---------------- Home ---------------- */
function renderHome(view) {
  const online = devices().filter(d => d.online && !d.self).length;
  const totalUnread = Object.values(M.unread).reduce((a, b) => a + b, 0);
  const host = M.peers.find(p => p.device_id === M.ident.host_device_id);

  view.appendChild(el('div', { class: 'card' },
    el('div', { class: 'row' },
      el('div', { style: { fontSize: '34px' } }, M.ident.icon || TYPE_ICONS[M.ident.device_type] || '📱'),
      el('div', {},
        el('div', { style: { fontWeight: '800', fontSize: '16px' } }, M.ident.name),
        el('div', { class: 'dim small' }, `${TYPE_ICONS[M.ident.device_type] || ''} ${M.ident.device_type} · independent participant`),
        el('div', { class: 'faint small mono' }, `id: ${M.ident.client_id}`)))));

  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Fleet status'),
    el('div', { class: 'spread' }, el('span', { class: 'dim' }, 'Devices online'), el('b', {}, String(online))),
    el('div', { class: 'spread' }, el('span', { class: 'dim' }, 'Unread messages'), el('b', {}, String(totalUnread))),
    el('div', { class: 'spread' }, el('span', { class: 'dim' }, 'Host companion'),
      el('b', {}, host ? `${host.name} (${host.status})` : M.ident.host_name || '?')),
    el('div', { class: 'spread' }, el('span', { class: 'dim' }, 'Connection'), el('b', { style: { color: M.connected ? 'var(--ok)' : 'var(--err)' } }, M.connected ? 'live' : 'reconnecting…')),
    el('div', { class: 'spread' }, el('span', { class: 'dim' }, 'PWA'), el('span', { class: 'faint small' }, 'installable via browser menu'))));

  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Quick actions'),
    el('div', { class: 'row', style: { flexWrap: 'wrap' } },
      el('button', { class: 'btn cyan', style: { flex: '1 1 45%' }, onclick: () => goTab('chat') }, '◌  Chat'),
      el('button', { class: 'btn', style: { flex: '1 1 45%' }, onclick: () => goTab('files') }, '📎  Send photos'),
      el('button', { class: 'btn', style: { flex: '1 1 45%' }, onclick: () => goTab('devices') }, '◉  Devices'),
      el('button', { class: 'btn', style: { flex: '1 1 45%' }, onclick: () => goTab('voice') }, '☎  Voice'),
      el('button', { class: 'btn', style: { flex: '1 1 45%' }, onclick: () => goTab('clip') }, '⧉  Clipboard'),
      el('button', { class: 'btn', style: { flex: '1 1 45%' }, onclick: () => goTab('more') }, '⋯  More'))));

  view.appendChild(el('div', { class: 'faint small', style: { textAlign: 'center' } },
    `Phantom Network v1.4.0 · hosted by ${M.ident.host_name} · no cloud involved`));
}
function goTab(name) {
  document.querySelector(`.mtab[data-tab="${name}"]`).click();
}

/* ---------------- Chat ---------------- */
function convMeta(conv) {
  const [kind, key] = conv.split(/:(.+)/);
  if (kind === 'direct') {
    const d = deviceById(key);
    if (d) return { icon: deviceIconFor(d), title: d.name + (d.self ? ' (this device)' : ''), kind: 'direct', peer: key };
    // v1.4.0: a mob_* author hosted by ANOTHER companion — resolve its
    // registered name from the fleet identity payloads instead of showing
    // the internal client id
    if (key.startsWith('mob_')) {
      for (const p of M.peers || []) {
        const found = (p.mobile_clients || []).find(c => c.client_id === key);
        if (found) return { icon: found.icon || '📱', title: found.name || key, kind: 'direct', peer: key };
      }
    }
    return { icon: '❓', title: key, kind: 'direct', peer: key };
  }
  if (kind === 'group') {
    return { icon: '👥', title: groupName(key), kind: 'group', peer: key };
  }
  if (kind === 'system') return { icon: '🔔', title: 'System Alerts', kind: 'system', peer: '' };
  return { icon: '📢', title: 'Broadcast', kind: 'broadcast', peer: 'all' };
}
function groupName(gid) {
  const g = (M.groups || []).find(g => g.id === gid);
  return g ? g.name : `Group ${gid.slice(0, 10)}`;
}

function renderChat(view) {
  if (M.activeConv) return renderConversation(view);
  const convs = Object.keys(M.convs)
    .map(c => ({ conv: c, msgs: M.convs[c], last: M.convs[c][M.convs[c].length - 1] }))
    .filter(e => e.last)
    .sort((a, b) => b.last.ts - a.last.ts);
  if (!convs.length) {
    view.appendChild(el('div', { class: 'empty-note' },
      'No conversations yet. Tap a device on the Devices tab to start chatting — messages stay on your LAN.'));
    return;
  }
  for (const c of convs) {
    const meta = convMeta(c.conv);
    const unread = M.unread[c.conv] || 0;
    const lastBody = c.last.deleted ? 'message deleted'
      : c.last.body || (c.last.attachment ? `[${c.last.attachment.kind === 'voice' ? '🎤 voice' : '📎 file'}: ${c.last.attachment.name}]` : '');
    view.appendChild(el('div', { class: 'conv', onclick: () => { M.activeConv = c.conv; renderTab(); } },
      el('div', { class: 'cico' }, meta.icon),
      el('div', { style: { flex: 1, minWidth: 0 } },
        el('div', { class: 'spread' }, el('b', {}, meta.title), el('span', { class: 'cts' }, fmtTime(c.last.ts))),
        el('div', { class: 'clast' }, (c.last.outgoing ? 'You: ' : '') + lastBody)),
      unread ? el('span', { class: 'cunread' }, String(unread)) : null));
  }
  view.appendChild(el('button', { class: 'btn block', onclick: showBroadcastSheet }, '📢  Broadcast to the fleet'));
}

function showBroadcastSheet() {
  modal('Broadcast announcement', (body) => {
    const ta = el('textarea', { placeholder: 'Announce something to every participant…', style: { minHeight: '80px' } });
    body.appendChild(ta);
    body.appendChild(el('div', { class: 'faint small', style: { marginTop: '8px' } },
      'Reaches every online computer and every registered phone through their hosts.'));
    body._ta = ta;
  }, [
    { label: 'Cancel', handler: (close) => close() },
    { label: 'Broadcast', kind: 'primary', handler: async (close, body) => {
      const text = body._ta.value.trim();
      if (!text) return;
      await sendMsg('broadcast', 'all', text);
      close();
      M.activeConv = 'broadcast:all';
      renderTab();
    } },
  ]);
}

async function sendMsg(to_kind, to_id, body, extra = {}) {
  try {
    const payload = M.authed({ to_kind, to_id, body, ...extra });
    const data = await M.api('/api/mobile/chat/send', payload);
    const msg = data.message;
    msg.conv = msg.conv === `mobile:${to_id}` && to_kind === 'mobile'
      ? `direct:${to_id}` : msg.conv;
    if (to_kind === 'direct' && msg.conv.startsWith('mobile:')) {
      msg.conv = `direct:${to_id}`;
    }
    stashMessage(msg);
    M.cacheSave();
    return msg;
  } catch (e) {
    toast(errText(e), 'err');
    throw e;
  }
}

function renderConversation(view) {
  const conv = M.activeConv;
  const meta = convMeta(conv);
  const msgs = M.convs[conv] || [];
  const pane = el('div', { class: 'chatpane' });
  const back = el('button', { class: 'btn', onclick: () => { M.activeConv = null; renderTab(); } }, '←');
  const hist = el('div', { class: 'chathist' });
  pane.appendChild(el('div', { class: 'row', style: { marginBottom: '8px' } }, back,
    el('div', { style: { fontWeight: '700' } }, meta.icon + ' ' + meta.title),
    el('button', { class: 'iconbtn', style: { marginLeft: 'auto' }, onclick: () => convActions(conv, meta) }, '⋯')));
  for (const m of msgs) pane.appendChild(hist);
  // re-append properly (above loop was just sizing)
  hist.innerHTML = '';
  for (const m of msgs) hist.appendChild(renderMessage(m, conv));
  hist.scrollTop = hist.scrollHeight;

  /* composer */
  const ta = el('textarea', { placeholder: 'Message…' });
  const send = async () => {
    const text = ta.value.trim();
    if (!text) return;
    ta.value = '';
    await sendMsg(meta.kind === 'direct' ? (meta.peer.startsWith('mob_') ? 'mobile' : 'direct') : meta.kind,
      meta.peer, text, { reply_to: M.replyTo || '' });
    M.replyTo = null;
    renderTab();
  };
  ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
  const attachBtn = el('button', { class: 'iconbtn accent', title: 'Attach photo/file', onclick: () => attachFile(conv, meta) }, '📎');
  const voiceBtn = el('button', { class: 'iconbtn', title: 'Voice message', onclick: () => recordVoice(conv, meta) }, '🎤');
  pane.appendChild(el('div', { class: 'chatin' }, attachBtn, voiceBtn, ta,
    el('button', { class: 'btn primary', style: { minWidth: '70px' }, onclick: send }, 'Send')));
  view.appendChild(pane);
}

function renderMessage(m, conv) {
  const mine = !!m.outgoing;
  const wrap = el('div', { class: `msg ${mine ? 'me' : 'them'} ${m.deleted ? 'deleted' : ''}`,
    onclick: () => msgActions(m, conv) });
  if (!mine && m.from?.name) wrap.appendChild(el('div', { class: 'mfrom' }, m.from.name));
  if (m.reply_to && !m.deleted) {
    const src = (M.convs[conv] || []).find(x => x.id === m.reply_to);
    wrap.appendChild(el('div', { class: 'mreply' }, src ? `↩ ${src.body || '[attachment]'}` : '↩ reply'));
  }
  if (m.deleted) {
    wrap.appendChild(el('div', {}, 'message deleted'));
  } else {
    if (m.body) wrap.appendChild(el('div', {}, m.body));
    if (m.attachment) {
      const a = m.attachment;
      if (a.kind === 'voice') {
        const audio = el('audio', { controls: true, src: `/api/chat/file/${a.file_id}`, preload: 'metadata' });
        wrap.appendChild(el('div', { class: 'matt' }, el('div', { class: 'faint small' }, `🎤 voice ${a.duration_s || 0}s`), audio));
      } else if (a.kind === 'image') {
        wrap.appendChild(el('div', { class: 'matt' },
          el('img', { src: `/api/chat/file/${a.file_id}`, alt: a.name, loading: 'lazy' })));
      } else {
        wrap.appendChild(el('div', { class: 'matt' },
          el('a', { href: `/api/chat/file/${a.file_id}`, download: a.name, style: { color: 'var(--cyan)' } },
            `📎 ${a.name} (${fmtSize(a.size)})`)));
      }
    }
    if (m.edited) wrap.appendChild(el('div', { class: 'faint', style: { fontSize: '9px' } }, 'edited'));
  }
  const ticks = mine ? ({ sent: '·', queued: '⏳', delivered: '✓', read: '✓✓', failed: '✗' }[m.status] || '·') : '';
  if (m.reactions && Object.keys(m.reactions).length) {
    const r = el('div', { class: 'mreacts' });
    for (const [emoji, who] of Object.entries(m.reactions)) {
      if (who.length) r.appendChild(el('span', {}, `${emoji} ${who.length}`));
    }
    wrap.appendChild(r);
  }
  wrap.appendChild(el('div', { class: 'mtime' }, `${fmtTime(m.ts)} ${mine ? ticks : ''}`));
  return wrap;
}

function msgActions(m, conv) {
  if (m.deleted) return;
  const items = [];
  if (!m.outgoing) {
    items.push({ icon: '↩', label: 'Reply', handler: () => { M.replyTo = m.id; toast('Tap a message body to reply is implicit — just type; reply attached', 'info'); } });
    items.push({ icon: '👍', label: 'React 👍', handler: () => reactMsg(m, conv, '👍') });
    items.push({ icon: '❤️', label: 'React ❤️', handler: () => reactMsg(m, conv, '❤️') });
  }
  items.push({ icon: '📋', label: 'Copy text', handler: async () => {
    try { await navigator.clipboard.writeText(m.body || ''); toast('Copied', 'ok'); } catch {}
  } });
  if (m.outgoing) {
    items.push({ icon: '✏️', label: 'Edit', handler: () => editMsg(m, conv) });
    items.push({ icon: '🗑', label: 'Delete', danger: true, handler: () => deleteMsg(m, conv) });
  }
  sheet(items);
}
async function reactMsg(m, conv, emoji) {
  try {
    await M.api('/api/mobile/chat/mutate', M.authed({ action: 'react', msg_id: m.id, emoji }));
    m.reactions = m.reactions || {};
    const who = M.ident.client_id;
    const arr = m.reactions[emoji] = m.reactions[emoji] || [];
    const i = arr.indexOf(who);
    if (i >= 0) arr.splice(i, 1); else arr.push(who);
    renderTab();
  } catch (e) { toast(errText(e), 'err'); }
}
function editMsg(m, conv) {
  modal('Edit message', (body) => {
    const ta = el('textarea', { style: { minHeight: '70px' } });
    ta.value = m.body || '';
    body.appendChild(ta); body._ta = ta;
  }, [
    { label: 'Cancel', handler: (close) => close() },
    { label: 'Save', kind: 'primary', handler: async (close, body) => {
      const text = body._ta.value.trim();
      if (!text) return;
      await M.api('/api/mobile/chat/mutate', M.authed({ action: 'edit', msg_id: m.id, body: text }));
      m.body = text; m.edited = true;
      M.cacheSave(); close(); renderTab();
    } },
  ]);
}
async function deleteMsg(m, conv) {
  modal('Delete message?', (body) => body.appendChild(el('div', { class: 'dim small' },
    'A tombstone replaces the message for every recipient. This cannot be undone.')), [
    { label: 'Cancel', handler: (close) => close() },
    { label: 'Delete', kind: 'danger', handler: async (close) => {
      await M.api('/api/mobile/chat/mutate', M.authed({ action: 'delete', msg_id: m.id }));
      m.deleted = true; m.body = ''; m.attachment = null;
      M.cacheSave(); close(); renderTab();
    } },
  ]);
}
function convActions(conv, meta) {
  sheet([
    { icon: '🗑', label: 'Clear this conversation locally', danger: true, handler: () => {
      delete M.convs[conv]; M.unread[conv] = 0; M.cacheSave(); M.activeConv = null; renderTab();
    } },
    ...(meta.kind === 'direct' && !meta.peer.startsWith('mob_') ? [
      { icon: '📎', label: 'Send a file to this device', handler: () => { M.fileTarget = meta.peer; goTab('files'); } },
    ] : []),
  ]);
}

/* ---------------- attachments ---------------- */
async function attachFile(conv, meta) {
  const input = el('input', { type: 'file', multiple: true, style: { display: 'none' } });
  input.setAttribute('accept', 'image/*,video/*,audio/*,.pdf,.txt,.doc,.docx,.zip');
  document.body.appendChild(input);
  input.addEventListener('change', async () => {
    input.remove();
    for (const file of input.files || []) {
      try {
        const toKind = meta.kind === 'direct' ? (meta.peer.startsWith('mob_') ? 'mobile' : 'direct') : meta.kind;
        toast(`Uploading ${file.name}…`, 'info');
        if (toKind === 'direct' && !meta.peer.startsWith('mob_')) {
          // v1.3.2 fix: computer destination gets the bytes on THAT machine
          // (the phone's host used to receive them into the wrong share).
          const targetDev = devices().find(d => d.device_id === meta.peer);
          if (!targetDev) throw new Error('destination computer not found');
          const base = devAddress(targetDev);
          const roots = await fetch(`http://${base}/api/fs/roots`).then(r => r.json());
          const rwShare = (roots.shares || []).find(s => s.mode === 'rw');
          if (!rwShare) throw new Error(`${targetDev.name} has no read-write share`);
          const session = await fetch(`http://${base}/api/fs/upload/session`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: rwShare.path, name: file.name, size: file.size, conflict: 'rename' }),
          }).then(r => r.json());
          if (!session.sid) throw new Error(session.message || 'upload session failed');
          const CHUNK = 512 * 1024;
          let offset = 0;
          while (offset < file.size) {
            const buf = await file.slice(offset, offset + CHUNK).arrayBuffer();
            const resp = await fetch(`http://${base}/api/fs/upload/${session.sid}`, { method: 'PUT', body: buf });
            if (!resp.ok) throw new Error(`upload failed at ${offset}`);
            offset += buf.byteLength;
          }
          const fin = await fetch(`http://${base}/api/fs/upload/${session.sid}/finish`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
          }).then(r => r.json());
          if (!fin.ok) throw new Error(fin.message || 'finalize failed');
          await sendMsg('direct', meta.peer, `[file shared] ${file.name}`, {
            attachment: {
              kind: (file.type || '').startsWith('image/') ? 'image' : 'file',
              name: file.name, size: file.size, path: fin.target || fin.path,
              src_base: `http://${base}`,
              mime: file.type || 'application/octet-stream',
            },
          });
        } else {
          // Phone-to-phone / host chat-file store (recipient pulls from host)
          const fid = await uploadChatFile(file, (p) => {});
          await sendMsg(toKind, meta.peer, '', {
            attachment: {
              kind: (file.type || '').startsWith('image/') ? 'image' : 'file',
              file_id: fid.id, name: file.name, size: file.size, mime: file.type || 'application/octet-stream',
              host_device_id: M.ident.host_device_id,
            },
          });
        }
        toast(`Sent ${file.name}`, 'ok');
        renderTab();
      } catch (e) { toast(errText(e), 'err'); }
    }
  });
  input.click();
}

async function uploadChatFile(file, onProgress) {
  const s = await M.api('/api/chat/file/session', {
    name: file.name, size: file.size, mime: file.type || 'application/octet-stream',
  });
  const sid = s.sid;
  const CHUNK = 512 * 1024;
  let offset = 0;
  while (offset < file.size) {
    const buf = await file.slice(offset, offset + CHUNK).arrayBuffer();
    const resp = await fetch(`/api/chat/file/${sid}`, { method: 'PUT', body: buf });
    if (!resp.ok) throw new Error(`upload failed at ${offset} (HTTP ${resp.status})`);
    offset += buf.byteLength;
    if (onProgress) onProgress(offset / file.size);
  }
  const fin = await M.api(`/api/chat/file/${sid}/finish`, {});
  return fin.file;
}

/* ---------------- voice messages ---------------- */
let mediaRec = null, recChunks = [], recTimer = null, recStart = 0, recBlob = null, recConv = null;
async function recordVoice(conv, meta) {
  if (!navigator.mediaDevices?.getUserMedia) {
    toast('Voice recording needs a browser with microphone support (and HTTPS)', 'err', 5000);
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recConv = { conv, meta };
    recChunks = [];
    mediaRec = new MediaRecorder(stream);
    mediaRec.ondataavailable = (e) => { if (e.data.size) recChunks.push(e.data); };
    mediaRec.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      recBlob = new Blob(recChunks, { type: mediaRec.mimeType || 'audio/webm' });
      showPreview();
    };
    mediaRec.start();
    recStart = Date.now();
    document.getElementById('rec-overlay').classList.remove('hidden');
    recTimer = setInterval(() => {
      const s = Math.floor((Date.now() - recStart) / 1000);
      if (s >= 60) { stopRec(); return; }        // 60 s cap
      document.getElementById('rec-time').textContent = `0:${String(s).padStart(2, '0')}`.replace('0:60', '1:00');
    }, 250);
  } catch (e) {
    toast('Microphone permission denied', 'err');
  }
}
function stopRec() {
  clearInterval(recTimer);
  document.getElementById('rec-overlay').classList.add('hidden');
  if (mediaRec && mediaRec.state !== 'inactive') mediaRec.stop();
}
function showPreview() {
  const dur = Math.round((Date.now() - recStart) / 1000);
  document.getElementById('pv-title').textContent = `Voice message — ${dur}s`;
  const audio = document.getElementById('pv-audio');
  audio.src = URL.createObjectURL(recBlob);
  document.getElementById('play-overlay').classList.remove('hidden');
}
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('rec-stop').addEventListener('click', stopRec);
  document.getElementById('rec-discard').addEventListener('click', () => {
    clearInterval(recTimer);
    document.getElementById('rec-overlay').classList.add('hidden');
    if (mediaRec && mediaRec.state !== 'inactive') mediaRec.stop();
    recBlob = null;
  });
  document.getElementById('pv-discard').addEventListener('click', () => {
    document.getElementById('play-overlay').classList.add('hidden');
    recBlob = null;
  });
  document.getElementById('pv-send').addEventListener('click', async () => {
    if (!recBlob || !recConv) return;
    document.getElementById('play-overlay').classList.add('hidden');
    const dur = Math.round((Date.now() - recStart) / 1000);
    const ext = (recBlob.type.split('/')[1] || 'webm').split(';')[0];
    const file = new File([recBlob], `voice-${Date.now()}.${ext}`, { type: recBlob.type });
    try {
      const fid = await uploadChatFile(file, () => {});
      const { conv, meta } = recConv;
      await sendMsg(meta.kind === 'direct' ? (meta.peer.startsWith('mob_') ? 'mobile' : 'direct') : meta.kind,
        meta.peer, '', {
        attachment: { kind: 'voice', file_id: fid.id, name: file.name, size: file.size, duration_s: dur, mime: recBlob.type },
      });
      toast('Voice message sent', 'ok');
      recBlob = null;
      M.activeConv = conv;
      renderTab();
    } catch (e) { toast(errText(e), 'err'); }
  });
});

/* ---------------- Devices ---------------- */
function renderDevices(view) {
  const list = devices();
  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Fleet'),
    el('div', { class: 'faint small' }, `${list.filter(d => d.online).length} of ${list.length} participants online`)));
  for (const d of list) {
    const isSelf = d.self;
    const row = el('div', { class: 'dev', onclick: () => deviceActions(d) },
      el('div', { class: 'dico' }, deviceIconFor(d)),
      el('div', { style: { flex: 1, minWidth: 0 } },
        el('div', { class: 'dname' }, d.name + (isSelf ? ' (this device)' : '')),
        el('div', { class: 'dsub' }, describeDevice(d))),
      el('div', { class: 'row', style: { flexDirection: 'column', gap: '4px', alignItems: 'flex-end' } },
        el('div', { class: `dot ${d.online ? 'on' : ''}` }),
        el('div', { class: `cap ${d.online ? 'on' : ''}` }, d.online ? 'online' : 'offline')));
    view.appendChild(row);
  }
}
function describeDevice(d) {
  if (d.self) return `${M.ident.device_type} · hosted by ${M.ident.host_name}`;
  if (d.kind === 'mobile') return `mobile client · host: ${d.host_device_id || '?'}`;
  const bits = [];
  if (d.os) bits.push(d.os);
  if (d.participating === false) bits.push('discovered device (not a companion)');
  else {
    if (d.shares_count) bits.push(`${d.shares_count} share${d.shares_count === 1 ? '' : 's'}`);
    if (d.mobile_clients && d.mobile_clients.length) bits.push(`${d.mobile_clients.length} mobile`);
    if (d.latency_ms) bits.push(`${Math.round(d.latency_ms)} ms`);
  }
  return bits.join(' · ') || 'computer companion';
}
function deviceActions(d) {
  if (d.self) {
    sheet([
      { icon: '✏️', label: 'Rename this device', handler: renameSelf },
      { icon: '🔄', label: 'Refresh fleet', handler: () => M.refreshFleet() },
    ]);
    return;
  }
  const items = [];
  if (d.kind === 'computer' && d.participating !== false) {
    items.push({ icon: '◌', label: 'Chat', handler: () => { M.activeConv = `direct:${d.device_id}`; M.tab = 'chat'; document.querySelectorAll('.mtab').forEach(t => t.classList.toggle('active', t.dataset.tab === 'chat')); renderTab(); } });
    items.push({ icon: '📎', label: 'Send files', handler: () => { M.fileTarget = d.device_id; goTab('files'); } });
    if (d.online) items.push({ icon: '⇄', label: 'Browse shared files', handler: () => { M.fileTarget = d.device_id; M.browseOnly = true; goTab('files'); } });
  }
  if (d.kind === 'mobile') {
    items.push({ icon: '◌', label: 'Chat', handler: () => { M.activeConv = `direct:${d.device_id}`; M.tab = 'chat'; document.querySelectorAll('.mtab').forEach(t => t.classList.toggle('active', t.dataset.tab === 'chat')); renderTab(); } });
    if (!d.online) items.push({ icon: '💤', label: `${d.name} is offline — messages will queue`, handler: () => {} });
  }
  if (!items.length) items.push({ icon: 'ℹ️', label: 'No actions for this device', handler: () => {} });
  sheet(items);
}
function renameSelf() {
  modal('Device settings', (body) => {
    const name = el('input', { type: 'text', value: M.ident.name, maxlength: 40 });
    const type = el('select', {},
      ...Object.entries(TYPE_ICONS).map(([v, ic]) =>
        el('option', { value: v, ...(v === M.ident.device_type ? { selected: true } : {}) }, `${ic} ${v}`)));
    body.appendChild(el('label', { class: 'fld' }, 'Name', name));
    body.appendChild(el('label', { class: 'fld' }, 'Type', type));
    body._f = { name, type };
  }, [
    { label: 'Cancel', handler: (close) => close() },
    { label: 'Save', kind: 'primary', handler: async (close, body) => {
      const name = body._f.name.value.trim() || M.ident.name;
      const type = body._f.type.value;
      const data = await M.api('/api/mobile/register', M.authed({
        name, device_type: type, icon: TYPE_ICONS[type],
      }));
      M.ident.name = data.client.name;
      M.ident.device_type = data.client.device_type;
      M.ident.icon = data.client.icon;
      M.save();
      close(); renderTab();
      toast('Saved', 'ok');
    } },
  ]);
}

/* ---------------- Files ---------------- */
function renderFiles(view) {
  const computers = devices().filter(d => d.kind === 'computer' && d.participating !== false);
  if (M.browseOnly) { /* browse mode clears after use */ }
  const target = M.fileTarget;
  const targetDev = computers.find(c => c.device_id === target);

  const sel = el('select', {},
    el('option', { value: '' }, '— choose a computer —'),
    ...computers.map(c => el('option', { value: c.device_id, ...(c.device_id === target ? { selected: true } : {}) },
      `${c.name}${c.online ? '' : ' (offline)'}`)));
  sel.addEventListener('change', () => { M.fileTarget = sel.value || null; M.deviceCache = null; renderTab(); });

  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Destination computer'),
    sel,
    el('div', { class: 'faint small', style: { marginTop: '8px' } },
      'Files go only into directories your computers have explicitly authorized. Nothing on this phone is read without you picking it.')));

  if (!targetDev) {
    view.appendChild(el('div', { class: 'empty-note' }, 'Pick a computer to send photos, videos or documents.'));
    return;
  }
  if (!targetDev.online) {
    view.appendChild(el('div', { class: 'empty-note' }, `${targetDev.name} is offline right now. Transfers need the destination online.`));
    return;
  }

  /* upload buttons */
  const upPhoto = el('button', { class: 'btn cyan block', onclick: () => pickUpload(targetDev, 'image/*', true) }, '📷  Photos / camera');
  const upAny = el('button', { class: 'btn block', onclick: () => pickUpload(targetDev, null, true) }, '📎  Any files');
  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, `Send to ${targetDev.name}`),
    upPhoto, el('div', { style: { height: '8px' } }), upAny,
    el('div', { id: 'up-progress' })));

  /* browser */
  const listCard = el('div', { class: 'card' }, el('h3', {}, `Authorized files on ${targetDev.name}`));
  const pathState = M.browsePath && M.browsePath.device === target
    ? M.browsePath : { device: target, path: null };
  const listBody = el('div', {});
  listCard.appendChild(listBody);
  view.appendChild(listCard);
  loadDir(listBody, targetDev, pathState);
}

async function loadDir(container, dev, pathState) {
  container.innerHTML = '';
  container.appendChild(el('div', { class: 'spinner' }, 'Loading authorized directories…'));
  try {
    let entries;
    if (!pathState.path) {
      const roots = await fetch(`http://${devAddress(dev)}/api/fs/roots`).then(r => r.json());
      entries = (roots.shares || []).map(s => ({ type: 'share', name: s.label || s.path, path: s.path, mode: s.mode }));
      container.innerHTML = '';
      container.appendChild(el('div', { class: 'faint small' }, 'Authorized directories:'));
      for (const e of entries) {
        container.appendChild(el('div', { class: 'dev', onclick: () => { pathState.path = e.path; loadDir(container, dev, pathState); } },
          el('div', { class: 'dico' }, '📂'),
          el('div', {},
            el('div', { class: 'dname' }, e.name),
            el('div', { class: 'dsub mono' }, `${e.mode === 'rw' ? 'read-write' : 'read-only'} · ${e.path}`))));
      }
      if (!entries.length) container.appendChild(el('div', { class: 'empty-note' }, 'No directories authorized on this computer.'));
      return;
    }
    const data = await fetch(`http://${devAddress(dev)}/api/fs/list?path=${encodeURIComponent(pathState.path)}`).then(r => r.json());
    if (!data.ok) throw new Error(data.message || 'list failed');
    container.innerHTML = '';
    const up = el('div', { class: 'dev', onclick: () => { pathState.path = parentOf(pathState.path); loadDir(container, dev, pathState); } },
      el('div', { class: 'dico' }, '↰'), el('div', { class: 'dname' }, '..'));
    container.appendChild(up);
    M.browsePath = pathState;
    for (const e of (data.entries || []).sort((a, b) => (a.type === 'dir' ? 0 : 1) - (b.type === 'dir' ? 0 : 1))) {
      const child = pathJoin(pathState.path, e.name);
      if (e.type === 'dir') {
        container.appendChild(el('div', { class: 'dev', onclick: () => { pathState.path = child; loadDir(container, dev, pathState); } },
          el('div', { class: 'dico' }, '📁'),
          el('div', {}, el('div', { class: 'dname' }, e.name))));
      } else {
        const url = `http://${devAddress(dev)}/api/fs/download?path=${encodeURIComponent(child)}`;
        container.appendChild(el('div', { class: 'dev' },
          el('div', { class: 'dico' }, '🗎'),
          el('div', { style: { flex: 1, minWidth: 0 } },
            el('div', { class: 'dname', style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, e.name),
            el('div', { class: 'dsub' }, fmtSize(e.size))),
          el('a', { href: url, download: e.name, class: 'btn', style: { padding: '8px 12px', minHeight: '36px' } }, '⬇')));
      }
    }
  } catch (e) {
    container.innerHTML = '';
    container.appendChild(el('div', { class: 'empty-note' }, `Cannot browse: ${errText(e)}`));
  }
}
function preferLan(addrs) {
  const list = (addrs || []).filter(Boolean);
  if (!list.length) return '';
  const rank = (a) => {
    if (/^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[0-1])\.)/.test(a)) return 0;
    if (a.startsWith('100.')) return 2;
    return 1;
  };
  return [...list].sort((a, b) => rank(a) - rank(b))[0];
}
function devAddress(dev) {
  // same-origin when the target IS the host; otherwise preferred LAN address
  // (v1.3.2: RFC1918 beats CGNAT/Tailscale so multi-homed hosts route right)
  if (dev.device_id === M.ident.host_device_id) return location.host;
  const peer = M.peers.find(p => p.device_id === dev.device_id);
  if (peer && peer.addresses && peer.addresses.length) {
    const a = preferLan(peer.addresses) || peer.addresses[0];
    return `${a}:${peer.api_port || 8415}`;
  }
  throw new Error('no reachable address');
}
function parentOf(p) {
  const norm = p.replace(/\\/g, '/').replace(/\/$/, '');
  const idx = norm.lastIndexOf('/');
  if (idx <= 0) return null;
  return p.includes('\\') ? norm.slice(0, idx + 1).replace(/\//g, '\\') : norm.slice(0, idx + 1);
}
function pathJoin(dir, name) {
  if (dir.includes('\\')) return dir.endsWith('\\') ? dir + name : dir + '\\' + name;
  return dir.endsWith('/') ? dir + name : dir + '/' + name;
}

async function pickUpload(dev, accept, multiple) {
  const input = el('input', { type: 'file', style: { display: 'none' } });
  if (accept) input.setAttribute('accept', accept);
  if (multiple) input.setAttribute('multiple', '1');
  if (accept === 'image/*') input.setAttribute('capture', 'environment');  // camera hint
  document.body.appendChild(input);
  input.addEventListener('change', async () => {
    input.remove();
    const files = [...(input.files || [])];
    if (!files.length) return;
    const bar = document.getElementById('up-progress');
    try {
      const base = devAddress(dev);
      for (const file of files) {
        const prog = el('div', {},
          el('div', { class: 'small dim' }, `${file.name} (${fmtSize(file.size)})`),
          el('div', { class: 'uploadbar' }, el('div', {})));
        bar.appendChild(prog);
        const barFill = prog.querySelector('.uploadbar > div');
        // destination: the first rw share, else ask nothing (ro shares refuse)
        const roots = await fetch(`http://${base}/api/fs/roots`).then(r => r.json());
        const rwShare = (roots.shares || []).find(s => s.mode === 'rw');
        if (!rwShare) throw new Error(`${dev.name} has no read-write authorized directory — add one on the computer`);
        const session = await fetch(`http://${base}/api/fs/upload/session`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: rwShare.path, name: file.name, size: file.size, conflict: 'rename' }),
        }).then(r => r.json());
        if (!session.sid) throw new Error(session.message || 'session failed');
        const CHUNK = 512 * 1024;
        let offset = 0;
        while (offset < file.size) {
          const buf = await file.slice(offset, offset + CHUNK).arrayBuffer();
          const resp = await fetch(`http://${base}/api/fs/upload/${session.sid}`, { method: 'PUT', body: buf });
          if (!resp.ok) throw new Error(`upload interrupted at ${fmtSize(offset)} (HTTP ${resp.status}) — retry the file`);
          offset += buf.byteLength;
          barFill.style.width = `${Math.round(100 * offset / file.size)}%`;
        }
        const fin = await fetch(`http://${base}/api/fs/upload/${session.sid}/finish`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
        }).then(r => r.json());
        if (!fin.ok) throw new Error(fin.message || 'finalize failed');
        toast(`Sent ${file.name} → ${dev.name}`, 'ok');
        setTimeout(() => prog.remove(), 1500);
      }
    } catch (e) { toast(errText(e), 'err', 6000); }
  });
  input.click();
}

/* ---------------- More ---------------- */
function renderMore(view) {
  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'This device'),
    el('button', { class: 'btn block', onclick: renameSelf }, '✏️  Name, type & icon'),
    el('div', { style: { height: '8px' } }),
    el('button', { class: 'btn block', onclick: resetIdentity }, '⚠️  Reset identity (fresh registration)'),
    el('div', { class: 'faint small', style: { marginTop: '8px' } },
      `Registered ${fmtAgo(M.ident.registered)} · host ${M.ident.host_name} · id ${M.ident.client_id}`)));

  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Identity & recovery'),
    el('div', { class: 'dim small', style: { lineHeight: '1.6' } },
      'Moving between HTTP and HTTPS (a different origin) starts a fresh ' +
      'browser profile. Copy your identity below and use “Restore an ' +
      'existing identity” on the new origin — the same id, inbox and ' +
      'history come back. Keep the secret private: it IS your identity.'),
    el('div', { class: 'faint small mono', style: { marginTop: '6px',
      wordBreak: 'break-all' } }, `id: ${M.ident.client_id}`),
    el('div', { class: 'faint small mono', style: { wordBreak: 'break-all' } },
      `secret: ${M.ident.client_secret}`),
    el('div', { class: 'row', style: { marginTop: '6px' } },
      el('button', { class: 'btn', onclick: async () => {
        try {
          await navigator.clipboard.writeText(JSON.stringify({
            client_id: M.ident.client_id,
            client_secret: M.ident.client_secret }, null, 2));
          toast('Identity JSON copied — store it somewhere private', 'ok');
        } catch { toast('Copy blocked by the browser — copy the fields above manually', 'err'); }
      } }, '⧉ Copy identity JSON'),
      el('button', { class: 'btn', onclick: () => goTab('home') }, '⌂ Home'))));

  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Notifications'),
    toggleRow('Sounds', 'sounds'),
    toggleRow('Silent mode (overrides sounds)', 'silent'),
    el('div', { class: 'faint small', style: { marginTop: '8px' } },
      'Browser notifications: notifications only fire while this tab is alive. Locked phones and killed browsers cannot be woken without extra platform infrastructure — Phantom keeps everything local instead.')));

  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Secure context (microphone / voice)'),
    (() => {
      const secure = window.isSecureContext;
      const mic = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
      const webrtc = typeof RTCPeerConnection !== 'undefined';
      const rows = [
        ['Secure context', secure ? '✓ yes' : '✗ no — HTTP origin'],
        ['Microphone API', mic ? '✓ available' : '✗ blocked (needs HTTPS)'],
        ['WebRTC voice', webrtc ? '✓ available' : '✗ unavailable'],
      ];
      return el('div', {},
        rows.map(([k, v]) => el('div', { class: 'spread' },
          el('span', { class: 'dim small' }, k),
          el('b', { class: 'small', style: { color: v.startsWith('✓') ? 'var(--ok)' : 'var(--err)' } }, v))),
        el('div', { class: 'faint small', style: { marginTop: '8px', lineHeight: '1.6' } },
          secure ? 'Voice calls and push-to-talk can use the microphone on this origin.'
            : 'Open the HTTPS port (host port + 1 when dual listeners are on), install the certificate from /api/tls/cert, then use that origin. Plain HTTP messaging keeps working.'));
    })()));

  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Install as app'),
    el('div', { class: 'dim small', style: { lineHeight: '1.6' } },
      'Use your browser menu → "Add to Home Screen" / "Install app". A service worker provides the offline shell; a live connection is still required for real data. On iOS, notifications and microphone need HTTPS with a trusted certificate (see HTTPS_SETUP.md).')));

  view.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Open full interfaces'),
    el('button', { class: 'btn block', onclick: () => location.href = '/' }, '🖥️  Desktop command center'),
    el('div', { style: { height: '8px' } }),
    el('button', { class: 'btn block', onclick: () => toast('Intercom and Audio Command live in the desktop UI for now', 'info', 4200) }, '🎧  Intercom / Audio (desktop UI)')));
}
function toggleRow(label, key) {
  const input = el('input', { type: 'checkbox' });
  input.checked = !!M.prefs[key];
  input.addEventListener('change', () => {
    M.prefs[key] = input.checked;
    M.savePrefs();
    if (key === 'silent' && input.checked) toast('Silent mode on', 'ok');
  });
  return el('div', { class: 'settings-row' },
    el('span', { class: 'small' }, label),
    el('label', { class: 'toggle' }, input, el('span', { class: 'track' })));
}
function resetIdentity() {
  modal('Reset identity?', (body) => body.appendChild(el('div', { class: 'dim small' },
    'You will register again as a NEW device. The old entry remains visible to fleet members until someone removes it from the desktop Settings.')), [
    { label: 'Cancel', handler: (close) => close() },
    { label: 'Reset', kind: 'danger', handler: (close) => {
      localStorage.removeItem(LS_KEY); localStorage.removeItem(LS_CACHE);
      location.reload();
    } },
  ]);
}

/* ---------------- go ---------------- */
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
