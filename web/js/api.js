/* ============================================================
   PHANTOM NETWORK — api.js
   Single API client for talking to ANY participating companion.
   The UI is served by one companion ("self"); calls to other
   companions go cross-origin (the companion sends CORS headers).
   ============================================================ */

import { state } from './state.js';

class ApiError extends Error {
  constructor(status, code, message) {
    super(message || code);
    this.status = status;
    this.code = code;
  }
}

function baseUrl(deviceId) {
  if (!deviceId || deviceId === 'self') return '';          // same origin
  const peer = state.peerById(deviceId);
  if (!peer || !peer.addresses || !peer.addresses.length) {
    throw new ApiError(0, 'peer_offline', `Device ${deviceId} has no reachable address`);
  }
  return `http://${peer.addresses[0]}:${peer.api_port}`;
}

async function request(deviceId, method, path, body, opts = {}) {
  const url = baseUrl(deviceId) + path;
  const headers = {};
  if (body !== undefined && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
  }
  const token = state.token();
  if (token) headers['X-PN-Token'] = token;

  const resp = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined
      : (body instanceof FormData || typeof body === 'string' ? body
        : JSON.stringify(body)),
    signal: opts.signal,
  });
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!resp.ok) {
    throw new ApiError(resp.status, data?.error || resp.status,
                       data?.message || `HTTP ${resp.status}`);
  }
  return data;
}

export const api = {
  ApiError,
  baseUrl,
  request,

  get: (d, p, o) => request(d, 'GET', p, undefined, o),
  post: (d, p, body, o) => request(d, 'POST', p, body, o),
  put: (d, p, body, o) => request(d, 'PUT', p, body, o),
  del: (d, p, o) => request(d, 'DELETE', p, undefined, o),

  /* ---- typed convenience calls on the LOCAL companion ---- */
  identity: () => request('', 'GET', '/api/identity'),
  health: (d) => request(d ?? '', 'GET', '/api/health'),
  peers: () => request('', 'GET', '/api/peers'),
  config: () => request('', 'GET', '/api/config'),
  setConfig: (patch) => request('', 'POST', '/api/config', patch),
  stats: () => request('', 'GET', '/api/stats'),
  events: (params = '') => request('', 'GET', '/api/events' + params),

  fsRoots: (d) => request(d ?? '', 'GET', '/api/fs/roots'),
  fsList: (d, path, hidden = false) =>
    request(d ?? '', 'GET', `/api/fs/list?path=${encodeURIComponent(path)}&hidden=${hidden}`),
  fsPreview: (d, path) =>
    request(d ?? '', 'GET', `/api/fs/preview?path=${encodeURIComponent(path)}`),
  fsMkdir: (d, path, name) => request(d ?? '', 'POST', '/api/fs/mkdir', { path, name }),
  fsRename: (d, path, newName) => request(d ?? '', 'POST', '/api/fs/rename', { path, new_name: newName }),
  fsDelete: (d, paths) => request(d ?? '', 'POST', '/api/fs/delete', { paths, confirm: true }),
  fsSearch: (d, root, query) => request(d ?? '', 'POST', '/api/fs/search', { root, query }),
  fsHash: (d, path) => request(d ?? '', 'GET', `/api/fs/hash?path=${encodeURIComponent(path)}`),
  fsWalk: (d, path) => request(d ?? '', 'GET', `/api/fs/walk?path=${encodeURIComponent(path)}`),

  jobs: (d) => request(d ?? '', 'GET', '/api/jobs'),
  createJob: (d, spec) => request(d ?? '', 'POST', '/api/jobs', spec),
  jobControl: (d, jobId, action) => request(d ?? '', 'POST', `/api/jobs/${jobId}/control`, { action }),

  monitor: (d) => request(d ?? '', 'GET', '/api/monitor'),
  monitorHistory: (d) => request(d ?? '', 'GET', '/api/monitor/history'),

  audioStatus: (d) => request(d ?? '', 'GET', '/api/audio/status'),
  audioTone: (d, freq, duration, waveform, volume) =>
    request(d ?? '', 'POST', '/api/audio/tone', { freq, duration, waveform, volume }),
  audioPreset: (d, name, volume) => request(d ?? '', 'POST', '/api/audio/preset', { name, volume }),
  audioStop: (d) => request(d ?? '', 'POST', '/api/audio/stop'),
  audioEnabled: (d, enabled) => request(d ?? '', 'POST', '/api/audio/enabled', { enabled }),

  chatConversations: () => request('', 'GET', '/api/chat/conversations'),
  chatHistory: (params) => request('', 'GET', '/api/chat/history' + params),
  chatSend: (payload) => request('', 'POST', '/api/chat/send', payload),
  chatRead: (conv, upToTs) => request('', 'POST', '/api/chat/read', { conv, up_to_ts: upToTs }),
  chatGroups: () => request('', 'GET', '/api/config').then(() => request('', 'GET', '/api/chat/history?limit=1')),
  createGroup: (name, members) => request('', 'POST', '/api/chat/groups', { name, members }),
  deleteGroup: (id) => request('', 'DELETE', `/api/chat/groups/${id}`),
  /* GhostChat 2.0 */
  chatReact: (msgId, emoji) => request('', 'POST', '/api/chat/react', { msg_id: msgId, emoji }),
  chatEdit: (msgId, body) => request('', 'POST', '/api/chat/edit', { msg_id: msgId, body }),
  chatDelete: (msgId) => request('', 'POST', '/api/chat/delete', { msg_id: msgId }),
  chatFileSession: (name, size, mime) => request('', 'POST', '/api/chat/file/session', { name, size, mime }),
  chatFileFinish: (sid, sha256 = '') => request('', 'POST', `/api/chat/file/${sid}/finish`, { sha256 }),

  /* v1.3.0: mobile clients, discovery diagnostics, alerts, automation, storage, media party */
  mobileClients: () => request('', 'GET', '/api/mobile/clients'),
  mobileRename: (id, patch) => request('', 'POST', `/api/mobile/clients/${id}/rename`, patch),
  mobileRemove: (id) => request('', 'POST', `/api/mobile/clients/${id}/remove`, {}),
  discoveryDiagnostics: () => request('', 'GET', '/api/discovery/diagnostics'),
  alerts: () => request('', 'GET', '/api/alerts'),
  alertSet: (rule) => request('', 'POST', '/api/alerts', { rule }),
  alertDelete: (id) => request('', 'DELETE', `/api/alerts/${id}`),
  automation: () => request('', 'GET', '/api/automation'),
  automationSet: (job) => request('', 'POST', '/api/automation', { job }),
  automationDelete: (id) => request('', 'DELETE', `/api/automation/${id}`),
  automationControl: (id, action) => request('', 'POST', `/api/automation/${id}/control`, { action }),
  automationPreview: (spec) => request('', 'POST', '/api/automation/preview', spec),
  storage: (view) => request('', 'GET', '/api/storage' + (view ? `?view=${view}` : '')),
  mediaParty: (d, spec) => request(d ?? '', 'POST', '/api/media/party', spec),
  mediaPartyStop: (d) => request(d ?? '', 'POST', '/api/media/party/stop', {}),

  intercomSessions: () => request('', 'GET', '/api/intercom/sessions'),
  intercomStart: (participants, label) => request('', 'POST', '/api/intercom/session', { participants, label }),
  intercomEnd: (id) => request('', 'POST', `/api/intercom/session/${id}/end`),
  intercomTalk: (session, pcm_b64, seq) => request('', 'POST', '/api/intercom/talk', { session, pcm_b64, seq }),
  intercomStop: (session) => request('', 'POST', '/api/intercom/stop', { session }),

  diagnostics: () => request('', 'GET', '/api/diagnostics'),
  diagnosticsReport: () => request('', 'GET', '/api/diagnostics/report'),
  setupStatus: () => request('', 'GET', '/api/setup/status'),
  addShare: (path, mode, label) => request('', 'POST', '/api/fs/share', { path, mode, label }),
  selfTest: () => request('', 'POST', '/api/diagnostics/selftest'),
  registerPeer: (address, name, port) => request('', 'POST', '/api/peers/register',
    { address, name, port }),
  peerMetadata: (id, patch) => request('', 'POST', `/api/peers/${id}/metadata`, patch),
  peerProbe: (id) => request('', 'POST', `/api/peers/${id}/probe`),
  radarSweep: (cidr) => request('', 'POST', '/api/radar/sweep', { cidr }),

  syncPreview: (spec) => request('', 'POST', '/api/sync/preview', spec),
  syncApply: (plan, deleteConfirmed) => request('', 'POST', '/api/sync/apply', { plan, delete_confirmed: deleteConfirmed }),

  /* ---- download a file from any device into the browser ---- */
  downloadToBrowser(deviceId, path) {
    const url = `${baseUrl(deviceId)}/api/fs/download?path=${encodeURIComponent(path)}`;
    const a = document.createElement('a');
    a.href = url;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  },

  /* ---- browser upload to a device (uses chunked upload sessions) ---- */
  async uploadFile(deviceId, destDir, file, onProgress, conflict = 'rename') {
    const base = baseUrl(deviceId);
    const session = await request(deviceId, 'POST', '/api/fs/upload/session',
      { path: destDir, name: file.name, size: file.size, conflict });
    const sid = session.sid;
    const CHUNK = 512 * 1024;
    let offset = 0;
    while (offset < file.size) {
      const slice = file.slice(offset, offset + CHUNK);
      const buf = await slice.arrayBuffer();
      await request(deviceId, 'PUT', `/api/fs/upload/${sid}`, buf);
      offset += buf.byteLength;
      if (onProgress) onProgress(offset, file.size);
    }
    const fin = await request(deviceId, 'POST', `/api/fs/upload/${sid}/finish`, {});
    return fin;
  },
};

/* ============================================================
   SSE live stream with auto-reconnect + subscriber registry
   ============================================================ */
class LiveStream {
  constructor() {
    this.handlers = {};
    this.source = null;
    this.status = 'down';   // up | down
    this.onstatus = [];
  }

  connect() {
    if (this.source) { this.source.close(); }
    const es = new EventSource('/api/stream');
    this.source = es;
    es.onopen = () => {
      this.status = 'up';
      this.onstatus.forEach(f => f('up'));
    };
    es.onerror = () => {
      this.status = 'down';
      this.onstatus.forEach(f => f('down'));
      // EventSource auto-reconnects; nothing else to do
    };
    for (const topic of ['event', 'chat', 'transfer', 'intercom', 'presence',
                         'mobile', 'alert', 'media_party']) {
      es.addEventListener(topic, (ev) => {
        let data = null;
        try { data = JSON.parse(ev.data); } catch { data = null; }
        (this.handlers[topic] || []).forEach(fn => {
          try { fn(data); } catch (e) { console.error('handler error', e); }
        });
      });
    }
  }

  on(topic, fn) {
    (this.handlers[topic] = this.handlers[topic] || []).push(fn);
    return () => {
      this.handlers[topic] = this.handlers[topic].filter(f => f !== fn);
    };
  }

  onStatus(fn) { this.onstatus.push(fn); fn(this.status); }
}

export const live = new LiveStream();
