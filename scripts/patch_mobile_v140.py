#!/usr/bin/env python3
"""Patch web/js/mobile/app.js + web/mobile.html for v1.4.0 mobile
surfaces: Voice, Media, Clipboard tabs, ring handling, quick actions."""

P1 = 'web/js/mobile/app.js'
P2 = 'web/mobile.html'

s = open(P1).read()
h = open(P2).read()

# ------------------------------------------------------------------ tabs
h = h.replace(
    '    <button class="mtab" data-tab="files"><span class="tico">▤</span><span>Files</span></button>',
    '''    <button class="mtab" data-tab="voice"><span class="tico">☎</span><span>Voice</span></button>
    <button class="mtab" data-tab="media"><span class="tico">♪</span><span>Media</span></button>
    <button class="mtab" data-tab="clip"><span class="tico">⧉</span><span>Clip</span></button>
    <button class="mtab" data-tab="files"><span class="tico">▤</span><span>Files</span></button>''')

# ------------------------------------------------------------------ titles + views map
s = s.replace(
    ": { home: M.ident.name, chat: 'GhostChat', devices: 'Devices', files: 'Files', more: 'More' }[M.tab];",
    ": { home: M.ident.name, chat: 'GhostChat', devices: 'Devices', files: 'Files', more: 'More', voice: 'Phantom Voice', media: 'Phantom Media', clip: 'Phantom Clipboard' }[M.tab] || M.tab;")
s = s.replace(
    "const VIEWS = { home: renderHome, chat: renderChat, devices: renderDevices, files: renderFiles, more: renderMore };",
    "const VIEWS = { home: renderHome, chat: renderChat, devices: renderDevices, files: renderFiles, more: renderMore, voice: renderVoice, media: renderMedia, clip: renderClip };")

# ------------------------------------------------------------------ SSE voice/clipboard/broadcast events
s = s.replace("""  } else if (data.type === 'presence' || data.type === 'client_updated') {
    M.refreshFleet();
  }
}""",
"""  } else if (data.type === 'voice' && data.data) {
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
        : '⚠ Insecure context: microphone needs HTTPS — open the host\u2019s secure port.')));

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
}""")

# ---------------- Home quick actions additions ----------------
_qa_old = ("      el('button', { class: 'btn', style: { flex: '1 1 45%' }, "
           "onclick: () => goTab('more') }, '⋯  More'))));")
_qa_new = ("      el('button', { class: 'btn', style: { flex: '1 1 45%' }, "
           "onclick: () => goTab('voice') }, '☎  Voice'),\n"
           "      el('button', { class: 'btn', style: { flex: '1 1 45%' }, "
           "onclick: () => goTab('clip') }, '⧉  Clipboard'),\n"
           "      el('button', { class: 'btn', style: { flex: '1 1 45%' }, "
           "onclick: () => goTab('more') }, '⋯  More'))));")
assert _qa_old in s, "quick actions anchor missing"
s = s.replace(_qa_old, _qa_new)

open(P1, 'w').write(s)
open(P2, 'w').write(h)
print("mobile patched")
