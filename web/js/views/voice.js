/* ============================================================
   PHANTOM NETWORK — voice.js (v1.4.0 Phantom Voice)

   Desktop voice view:
   * dial: call a computer or a hosted phone (fleet + /api/mobile/clients)
   * voice rooms: named group sessions, join/leave, member list
   * incoming call ring (sfx) with accept / decline
   * in-call panel: mute, mic/speaker pickers, live level meter (VAD),
     transport + quality indicators, duration, hangup
   * call history with missed-call markers
   * screen share: opt-in getDisplayMedia, only while in a call;
     the other side sees an honest 'screen share' card (no remote
     input control — that is out of scope for this release)
   ============================================================ */

import { api } from '../api.js';
import { state } from '../state.js';
import { el, toast, errText, fmtTime } from '../ui.js';
import { VoiceRTC, rtc } from '../lib/rtc.js';
import { playPreset } from '../lib/sfx.js';

const RING_SFX = 'beacon';

let cleanupFns = [];

function cleanup() {
  cleanupFns.forEach(f => { try { f(); } catch {} });
  cleanupFns = [];
}

export async function render(container) {
  cleanup();
  const caps = VoiceRTC.caps();
  let devices = { mics: [], speakers: [] };
  let history = [];
  let rooms = [];
  let ringingCall = null;
  let durationTimer = null;
  let connectStart = 0;

  /* ---------------- shell ---------------- */
  const capsRow = el('div', { class: 'row dim small', style: { gap: '10px' } });
  const micSel = el('select', { style: { minWidth: '180px' } },
    el('option', { value: '' }, 'Default microphone'));
  const spkSel = el('select', { style: { minWidth: '180px' } },
    el('option', { value: '' }, 'Default output'));
  const targetSel = el('select', { style: { minWidth: '220px' } });
  const callBtn = el('button', { class: 'btn violet' }, '☎ Call');
  const roomName = el('input', { placeholder: 'room name (e.g. kitchen)',
    style: { maxWidth: '180px' } });
  const joinBtn = el('button', { class: 'btn' }, 'Join voice room');
  const roomList = el('div', { class: 'row', style: { flexWrap: 'wrap', gap: '8px' } });
  const meterBar = el('div', { class: 'meter',
    style: { height: '8px', width: '160px', background: '#2a2333' } });
  const meterFill = el('div', { style: { height: '100%', width: '0%',
    background: 'var(--violet)', transition: 'width 80ms' } });
  meterBar.appendChild(meterFill);
  const muteBtn = el('button', { class: 'btn' }, '🎙 Mute');
  const shareBtn = el('button', { class: 'btn' }, '🖥 Share screen');
  const shareAcceptBtn = el('button', { class: 'btn cyan',
    style: { display: 'none' } }, 'Open shared screen');
  const hangupBtn = el('button', { class: 'btn danger' }, '✖ Hang up');
  const sessionCard = el('div', { class: 'card', style: { display: 'none' } });
  const qualityEl = el('span', { class: 'small mono dim' }, '');
  const transportEl = el('span', { class: 'small mono' }, '');
  const durationEl = el('span', { class: 'mono' }, '00:00');
  const historyList = el('div', {});
  const ringCard = el('div', { class: 'card', style: { display: 'none',
    borderColor: 'var(--violet)' } });

  /* ---------------- data ---------------- */
  async function loadTargets() {
    try {
      const [peers, mob] = await Promise.all([
        api.peers(),
        api.get('', '/api/mobile/clients').catch(() => ({ clients: [] })),
      ]);
      state.peers = (peers && peers.peers) || state.peers;
      targetSel.innerHTML = '';
      for (const p of state.peers) {
        if (p.device_id === (state.self && state.self.device_id)) continue;
        if (!p.participating || p.status !== 'online') continue;
        targetSel.appendChild(el('option', {
          value: JSON.stringify({ kind: 'computer', device_id: p.device_id,
                                  name: p.display_name }),
        }, `💻 ${p.display_name}`));
      }
      for (const c of (mob && mob.clients) || []) {
        targetSel.appendChild(el('option', {
          value: JSON.stringify({ kind: 'mobile', client_id: c.client_id,
                                  name: c.name,
                                  host_device_id: c.host_device_id }),
        }, `📱 ${c.name}${c.host_name ? ' · via ' + c.host_name : ''}`));
      }
    } catch {}
  }

  async function loadHistory() {
    try {
      const r = await api.get('', '/api/voice/history?limit=50');
      history = r.history || [];
      renderHistory();
    } catch {}
  }

  async function loadState() {
    try {
      const st = await api.get('', '/api/voice/state');
      rooms = st.rooms || [];
      renderRooms();
      for (const c of st.calls || []) {
        if (c.state === 'ringing' && isTargetedAtMe(c) && !rtc.call) {
          showRing(c);
        }
      }
    } catch {}
  }

  function isTargetedAtMe(c) {
    const me = state.self && state.self.device_id;
    const myClients = (M_mobileClients() || []).map(x => x.client_id);
    return c.targets.some(t => t.device_id === me ||
      (t.kind === 'mobile' && myClients.includes(t.client_id)));
  }

  function M_mobileClients() { return window.__pn_mobile_clients || []; }

  async function refreshMobileClients() {
    try {
      const r = await api.get('', '/api/mobile/clients');
      window.__pn_mobile_clients = r.clients || [];
    } catch {}
  }

  function renderRooms() {
    roomList.innerHTML = '';
    if (!rooms.length) {
      roomList.appendChild(el('span', { class: 'dim small' },
        'No active rooms.'));
    }
    for (const r of rooms) {
      roomList.appendChild(el('span', { class: 'chip' },
        `◉ ${r.name} (${(r.members || []).length}) `));
    }
  }

  function renderHistory() {
    historyList.innerHTML = '';
    if (!history.length) {
      historyList.appendChild(el('span', { class: 'dim small' },
        'No calls yet.'));
      return;
    }
    for (const h of history.slice(0, 30)) {
      const missed = h.state === 'missed';
      const rejected = h.state === 'rejected';
      const who = (h.origin && h.origin.name) || '?';
      const to = (h.targets || []).map(t => t.name || t.device_id || '?')
        .join(', ');
      historyList.appendChild(el('div', { class: 'row',
        style: { borderBottom: '1px solid #221c2b', padding: '6px 0',
                 gap: '12px', alignItems: 'baseline' } },
        el('span', { style: { color: missed ? 'var(--err)'
          : rejected ? 'var(--warn)' : 'var(--ok)' } },
          missed ? '⊘ missed' : rejected ? '× declined' : '✓'),
        el('b', {}, who), el('span', { class: 'dim small' }, `→ ${to}`),
        el('span', { class: 'dim small mono' }, fmtTime(h.created)),
        el('span', { class: 'small mono dim' },
          h.duration_s ? `${Math.round(h.duration_s)}s` : '')));
    }
  }

  /* ---------------- ring ---------------- */
  function showRing(c) {
    ringingCall = c;
    ringCard.innerHTML = '';
    ringCard.style.display = '';
    const who = (c.origin && c.origin.name) || 'a device';
    ringCard.appendChild(el('div', { class: 'row',
      style: { justifyContent: 'space-between' } },
      el('div', {},
        el('h3', { style: { color: 'var(--violet)' } }, `☎ Incoming call — ${who}`),
        el('span', { class: 'dim small' },
          `call ${c.id} · accept to open a direct WebRTC audio session`)),
      el('div', { class: 'row' },
        el('button', { class: 'btn violet', onclick: () => acceptCall() }, '✓ Accept'),
        el('button', { class: 'btn danger', onclick: () => declineCall() }, '✕ Decline'))));
    try { playPreset(RING_SFX); } catch {}
  }

  function hideRing() {
    ringingCall = null;
    ringCard.style.display = 'none';
    ringCard.innerHTML = '';
  }

  async function acceptCall() {
    const c = ringingCall;
    if (!c) return;
    hideRing();
    await openSession(c.id, 'call', 'callee');
  }

  async function declineCall() {
    const c = ringingCall;
    if (!c) return;
    hideRing();
    try {
      await api.post('', '/api/voice/answer',
                     { call_id: c.id, accept: false });
      toast('Call declined', 'info');
    } catch (e) { toast(errText(e), 'err'); }
  }

  /* ---------------- session ---------------- */
  function showSession(kind) {
    sessionCard.style.display = '';
    sessionCard.innerHTML = '';
    sessionCard.appendChild(el('div', { class: 'row',
      style: { justifyContent: 'space-between' } },
      el('div', {},
        el('h3', {}, kind === 'screen' ? '🖥 Screen sharing session'
                                       : '☎ Voice session'),
        el('div', { class: 'row small mono', style: { gap: '14px' } },
          durationEl, transportEl, qualityEl)),
      el('div', { class: 'row' },
        ...(kind === 'call'
          ? [muteBtn, meterBar, micSel, spkSel]
          : [shareAcceptBtn]),
        shareBtn, hangupBtn)));
  }

  async function openSession(callId, kind, role) {
    showSession(kind);
    connectStart = Date.now();
    clearInterval(durationTimer);
    durationTimer = setInterval(() => {
      const s = Math.floor((Date.now() - connectStart) / 1000);
      durationEl.textContent =
        `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
    }, 500);
    try {
      if (role === 'callee') {
        rtc.role = 'callee';
        rtc.call = { id: callId };
        await rtc.connect(kind);
      } else {
        await rtc.connect(kind);
      }
    } catch (e) {
      toast(errText(e), 'err');
      rtc.teardown();
      sessionCard.style.display = 'none';
    }
  }

  muteBtn.onclick = () => {
    const m = rtc.setMuted(!rtc.muted);
    muteBtn.textContent = m ? '🎙 Unmute' : '🎙 Mute';
    muteBtn.classList.toggle('danger', m);
  };

  shareBtn.onclick = async () => {
    if (!caps.screen) {
      toast('This browser does not support screen capture ' +
        '(needs getDisplayMedia + secure context)', 'err');
      return;
    }
    if (!rtc.call) {
      toast('Screen share runs inside an active call — start a voice ' +
        'call first, then share the screen over the same session', 'info');
      return;
    }
    try {
      await rtc.capture('screen');
      const sender = rtc.pc.getSenders().find(s => s.track &&
        s.track.kind === 'video');
      if (sender) {
        await sender.replaceTrack(rtc.local.getVideoTracks()[0]);
      } else {
        for (const t of rtc.local.getTracks()) rtc.pc.addTrack(t, rtc.local);
      }
      toast('Sharing this screen with the call participants — the ' +
        'sharing indicator stays visible while active', 'ok');
    } catch (e) {
      toast(errText(e), 'err');
    }
  };

  shareAcceptBtn.onclick = () => {
    if (rtc.remote) {
      try {
        const w = window.open('', '_blank');
        const v = document.createElement('video');
        v.autoplay = true; v.muted = true;
        v.srcObject = rtc.remote;
        v.style.width = '100%';
        document.body.appendChild(v);
        v.play().catch(() => {});
        w.document.body.appendChild(v);
        w.document.body.style.background = '#0d0a12';
      } catch {
        toast('Allow pop-ups to open the shared screen in a window', 'err');
      }
    }
  };

  hangupBtn.onclick = async () => {
    await rtc.hangup();
    clearInterval(durationTimer);
    sessionCard.style.display = 'none';
    meterFill.style.width = '0%';
    await loadHistory();
  };

  callBtn.onclick = async () => {
    const v = targetSel.value;
    if (!v) { toast('Pick a device to call', 'err'); return; }
    const target = JSON.parse(v);
    try {
      showSession('call');
      connectStart = Date.now();
      await rtc.call(target);
      toast(`Calling ${target.name}… (direct WebRTC, LAN-first)`, 'info');
    } catch (e) {
      sessionCard.style.display = 'none';
      toast(errText(e), 'err');
    }
  };

  joinBtn.onclick = async () => {
    const room = roomName.value.trim();
    if (!room) { toast('Enter a room name', 'err'); return; }
    try {
      await api.post('', '/api/voice/room/join', { room });
      toast(`Joined voice room "${room}" — participants hear each other ` +
        'over direct WebRTC once they join from their devices', 'ok');
      await loadState();
    } catch (e) { toast(errText(e), 'err'); }
  };

  micSel.onchange = () => { if (rtc.call) rtc.switchMic(micSel.value); };
  spkSel.onchange = () => {
    if (rtc.audioEl && rtc.audioEl.setSinkId) {
      rtc.audioEl.setSinkId(spkSel.value).catch(() => {});
    }
  };

  /* ---------------- rtc events ---------------- */
  rtc.onState.push((st, detail) => {
    if (st === 'connected') {
      transportEl.textContent = 'transport: checking…';
    } else if (st === 'transport') {
      transportEl.textContent =
        `transport: ${detail === 'direct' ? 'direct LAN (host↔host)' : detail}`;
    } else if (st === 'failed') {
      transportEl.textContent = 'transport: FAILED';
      transportEl.style.color = 'var(--err)';
      toast(detail || 'WebRTC connection failed', 'err');
    } else if (st === 'autoplay-blocked') {
      toast(detail, 'info');
    } else if (st === 'remote-screen') {
      shareAcceptBtn.style.display = '';
      toast('The other side is sharing a screen', 'info');
    }
  });
  rtc.onLevel.push((level, speaking) => {
    meterFill.style.width = `${Math.round(level * 100)}%`;
    meterFill.style.background = speaking ? 'var(--ok)' : 'var(--violet)';
  });
  rtc.onState.push((st, detail) => {
    if (st === 'quality') {
      const q = detail;
      const pct = q.recv ? Math.max(0, 100 -
        Math.round(q.lost * 100 / Math.max(1, q.recv + q.lost))) : null;
      qualityEl.textContent =
        `jitter ${q.jitter_ms}ms · loss ${pct === null ? '—' : pct + '%'}`;
      qualityEl.style.color = pct === null || pct >= 95 ? 'var(--ok)'
        : pct >= 80 ? 'var(--warn)' : 'var(--err)';
    }
  });

  /* ---------------- devices & caps ---------------- */
  if (caps.mic) {
    try {
      devices = await rtc.devices();
      if (devices.mics.length) {
        micSel.innerHTML = '';
        for (const d of devices.mics) {
          micSel.appendChild(el('option', { value: d.deviceId },
            d.label || d.deviceId.slice(0, 12)));
        }
        for (const d of devices.speakers) {
          spkSel.appendChild(el('option', { value: d.deviceId },
            d.label || d.deviceId.slice(0, 12)));
        }
      }
    } catch {}
  }

  capsRow.append(
    el('span', {}, caps.webrtc ? '✓ WebRTC' : '✗ no WebRTC'),
    el('span', {}, caps.mic ? '✓ mic API' : '✗ no mic API'),
    el('span', {}, caps.screen ? '✓ screen API' : '○ no screen API'),
    el('span', {}, caps.secure ? '✓ secure context'
      : '⚠ insecure context — mic/screen need HTTPS (Settings → HTTPS)'));
  if (!caps.mic) {
    micSel.disabled = spkSel.disabled = true;
  }

  /* ---------------- layout ---------------- */
  container.append(
    el('div', { class: 'card' },
      el('h2', {}, '☎ Phantom Voice'),
      el('p', { class: 'dim small' },
        'Direct computer↔computer and computer↔phone calls on your LAN. ' +
        'Audio flows browser↔browser over WebRTC (host candidates, no ' +
        'external TURN); the companions only relay signaling. Every call ' +
        'needs an explicit accept — nothing records or auto-answers.'),
      capsRow,
      el('div', { class: 'row', style: { marginTop: '10px', gap: '10px',
        flexWrap: 'wrap' } },
        targetSel, callBtn, roomName, joinBtn),
      el('div', { class: 'small dim', style: { marginTop: '6px' } },
        'Output-device picking works where the browser supports setSinkId ' +
        '(Chromium desktops); phones fall back to the default speaker.')),
    ringCard,
    sessionCard,
    el('div', { class: 'card' },
      el('h3', {}, 'Voice rooms'),
      roomList),
    el('div', { class: 'card' },
      el('h3', {}, 'Call history'),
      historyList),
  );

  /* ---------------- wiring ---------------- */
  await Promise.all([loadTargets(), loadHistory(), loadState(),
                     refreshMobileClients()]);
  const unState = state.on('peers', () => loadTargets());
  const unsubLive = VoiceRTC.watchIncoming(
    (data) => {
      if (!rtc.call && data.data) showRing(data.data);
    },
    (data) => {
      const c = data.data || {};
      if (ringingCall && c.id === ringingCall.id &&
          ['ended', 'rejected', 'missed'].includes(c.state)) {
        hideRing();
        toast('Call ' + c.state, 'info');
      }
    });
  const refresh = setInterval(() => { loadState(); loadHistory(); }, 10000);
  cleanupFns.push(unsubLive, () => clearInterval(refresh), unState,
    () => clearInterval(durationTimer), () => rtc.teardown());
}
