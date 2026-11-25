/* ============================================================
   PHANTOM NETWORK — rtc.js (v1.4.0 Phantom Voice / Screen Share)

   Browser-side WebRTC engine:
   * LAN-first peer connections: iceServers: [] — on one subnet the
     browser's host candidates connect directly; optional STUN servers
     may be configured companion-side (voice_stun_servers) but are NOT
     required and no TURN is used.
   * Signaling rides the existing companion channels: desktop SSE
     'voice' topic / mobile private stream, relayed host-to-host by the
     companions.  The companion never inspects the SDP payloads.
   * Every capture is explicit: getUserMedia / getDisplayMedia happen
     only when the user presses a visible control.  No auto-answer, no
     hidden capture, no recording.
   * Honest transport reporting: we report 'direct' when the selected
     candidate pair is host/host (srflx/relay appear only if STUN was
     configured) and 'signaling via <host>' otherwise.

   The classic PCM intercom stays untouched as the compatibility
   fallback; the UI labels which path a session uses.
   ============================================================ */

import { api, live } from '../api.js';

const PC_CFG = () => ({ iceServers: (window.PHANTOM_STUN || []) });

export class VoiceRTC {
  constructor() {
    this.pc = null;
    this.local = null;            // MediaStream (mic or screen)
    this.remote = null;
    this.audioEl = null;
    this.call = null;             // call descriptor from the companion
    this.role = null;             // 'caller' | 'callee'
    this.muted = false;
    this.analyser = null;
    this.levelTimer = null;
    this.vadTimer = null;
    this.onState = [];            // fn(state, detail)
    this.onLevel = [];            // fn(level0to1, speaking)
    this.statsTimer = null;
    this.iceState = 'new';
    this.transport = 'connecting';
    this.unsubs = [];
  }

  emit(state, detail) {
    this.onState.forEach(f => { try { f(state, detail); } catch {} });
  }

  emitLevel(level, speaking) {
    this.onLevel.forEach(f => { try { f(level, speaking); } catch {} });
  }

  /* -------- permission & capability checks (honest) -------- */
  static caps() {
    return {
      webrtc: typeof RTCPeerConnection !== 'undefined',
      mic: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
      screen: !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia),
      secure: window.isSecureContext ||
        ['localhost', '127.0.0.1'].includes(location.hostname),
    };
  }

  async devices() {
    try {
      const list = await navigator.mediaDevices.enumerateDevices();
      return {
        mics: list.filter(d => d.kind === 'audioinput'),
        speakers: list.filter(d => d.kind === 'audiooutput'),
      };
    } catch { return { mics: [], speakers: [] }; }
  }

  /* -------- media capture -------- */
  async capture(kind = 'call', deviceId = '') {
    if (kind === 'screen') {
      this.local = await navigator.mediaDevices.getDisplayMedia({
        video: true, audio: false,
      });
    } else {
      const constraints = {
        audio: {
          echoCancellation: true, noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      };
      if (deviceId) constraints.audio.deviceId = { exact: deviceId };
      this.local = await navigator.mediaDevices.getUserMedia(constraints);
    }
    this.setupMeter();
    return this.local;
  }

  setupMeter() {
    if (!this.local || this.analyser) return;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctx();
      const src = ctx.createMediaStreamSource(this.local);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      src.connect(analyser);
      this.audioCtx = ctx;
      this.analyser = analyser;
      const buf = new Uint8Array(analyser.fftSize);
      this.levelTimer = setInterval(() => {
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = (buf[i] - 128) / 128;
          sum += v * v;
        }
        const rms = Math.sqrt(sum / buf.length);
        const level = Math.min(1, rms * 4);
        this.emitLevel(level, level > 0.06);
      }, 120);
    } catch { /* metering is best-effort */ }
  }

  stopMeter() {
    clearInterval(this.levelTimer); clearInterval(this.vadTimer);
    this.levelTimer = this.vadTimer = null;
    if (this.audioCtx) { try { this.audioCtx.close(); } catch {} }
    this.audioCtx = this.analyser = null;
  }

  /* -------- call flow -------- */
  async call(target, kind = 'call') {
    /* target: {device_id|client_id, name, kind, host_device_id} */
    const caps = VoiceRTC.caps();
    if (!caps.webrtc) throw new Error('This browser has no WebRTC support');
    if (!caps.secure) throw new Error(
      'Microphone access needs a secure context — open the HTTPS port ' +
      '(see Settings → HTTPS) or use localhost');
    const r = await api.post('', '/api/voice/call', {
      mode: 'call', kind,
      targets: [{
        device_id: target.device_id, client_id: target.client_id,
        name: target.name, kind: target.kind || 'computer',
        host_device_id: target.host_device_id || '',
      }],
    });
    this.call = r.call;
    this.role = 'caller';
    await this.connect(kind);
    return r.call;
  }

  async accept(callId, kind = 'call') {
    const caps = VoiceRTC.caps();
    if (!caps.webrtc) throw new Error('This browser has no WebRTC support');
    if (!caps.secure) throw new Error(
      'Microphone access needs a secure context — open the HTTPS port ' +
      '(see Settings → HTTPS) or use localhost');
    await api.post('', '/api/voice/answer',
                   { call_id: callId, accept: true });
    this.call = { id: callId };
    this.role = 'callee';
    await this.connect(kind);
  }

  async connect(kind = 'call') {
    this.pc = new RTCPeerConnection(PC_CFG());
    this.pc.onicecandidate = (ev) => {
      if (ev.candidate) {
        this.signal('ice', ev.candidate.toJSON());
      }
    };
    this.pc.ontrack = (ev) => {
      this.remote = ev.streams[0];
      this.playRemote();
      this.emit('remote-media');
    };
    this.pc.onconnectionstatechange = () => {
      this.iceState = this.pc.connectionState;
      if (this.iceState === 'failed') {
        this.transport = 'failed';
        this.emit('failed', 'WebRTC connection failed — try the PCM ' +
          'intercom fallback or check both devices are on the same LAN');
      } else if (this.iceState === 'connected' || this.iceState === 'completed') {
        this.detectTransport();
        this.emit('connected');
      } else {
        this.emit(this.iceState);
      }
    };
    if (kind !== 'screen') {
      await this.capture(kind === 'screen' ? 'screen' : 'call');
      for (const track of this.local.getTracks()) {
        this.pc.addTrack(track, this.local);
      }
    }
    if (this.role === 'caller') {
      const offer = await this.pc.createOffer();
      await this.pc.setLocalDescription(offer);
      this.signal('offer', { sdp: this.pc.localDescription.sdp,
                             type: this.pc.localDescription.type,
                             media: kind });
    }
    this.watchStats();
  }

  playRemote() {
    if (!this.audioEl) {
      this.audioEl = new Audio();
      this.audioEl.autoplay = true;
    }
    this.audioEl.srcObject = this.remote;
    try { this.audioEl.setSinkId; } catch {}
    this.audioEl.play().catch(() => {
      this.emit('autoplay-blocked',
        'Tap the session card once to allow audio playback');
    });
  }

  setVolume(v) {
    if (this.audioEl) this.audioEl.volume = Math.max(0, Math.min(1, v));
  }

  setMuted(m) {
    this.muted = m;
    if (this.local) {
      for (const t of this.local.getAudioTracks()) t.enabled = !m;
    }
    return this.muted;
  }

  async switchMic(deviceId) {
    if (this.role === null) return;
    const old = this.local;
    await this.capture('call', deviceId);
    if (this.pc && this.local) {
      const sender = this.pc.getSenders().find(s =>
        s.track && s.track.kind === 'audio');
      if (sender) await sender.replaceTrack(this.local.getAudioTracks()[0]);
      if (sender && old) await sender.replaceTrack(
        this.local.getAudioTracks()[0]);
    }
    if (old) for (const t of old.getTracks()) t.stop();
  }

  signal(kind, payload) {
    if (!this.call) return;
    api.post('', '/api/voice/signal',
             { call_id: this.call.id, kind, payload })
      .catch(() => {});
  }

  async hangup() {
    const id = this.call && this.call.id;
    this.teardown();
    if (id) {
      try { await api.post('', '/api/voice/end', { call_id: id }); }
      catch {}
    }
  }

  teardown() {
    this.stopMeter();
    clearInterval(this.statsTimer);
    this.statsTimer = null;
    if (this.local) for (const t of this.local.getTracks()) t.stop();
    if (this.pc) { try { this.pc.close(); } catch {} }
    if (this.audioEl) { this.audioEl.srcObject = null; }
    this.local = this.remote = this.pc = null;
    this.call = null; this.role = null;
    this.iceState = 'new'; this.transport = 'idle';
    for (const u of this.unsubs) { try { u(); } catch {} }
    this.unsubs = [];
    this.emit('idle');
  }

  async detectTransport() {
    /* Honest reporting: read the selected candidate pair */
    try {
      const stats = await this.pc.getStats();
      let pair = null;
      stats.forEach(s => {
        if (s.type === 'transport') pair = s.selectedCandidatePairId;
      });
      let transport = 'direct';
      if (pair) {
        const p = stats.get(pair);
        const local = p && stats.get(p.localCandidateId);
        const remote = p && stats.get(p.remoteCandidateId);
        if (local && remote) {
          const types = [local.candidateType, remote.candidateType];
          if (types.includes('relay')) transport = 'relay';
          else if (types.includes('srflx')) transport = 'stun';
        }
      }
      this.transport = transport;
      this.emit('transport', transport);
    } catch { this.transport = 'direct'; }
  }

  watchStats() {
    this.statsTimer = setInterval(async () => {
      if (!this.pc || this.iceState !== 'connected') return;
      try {
        const stats = await this.pc.getStats();
        stats.forEach(s => {
          if (s.type === 'inbound-rtp' && s.kind === 'audio') {
            this.emit('quality', {
              jitter_ms: s.jitter ? Math.round(s.jitter * 1000) : 0,
              lost: s.packetsLost || 0,
              recv: s.packetsReceived || 0,
              bytes: s.bytesReceived || 0,
            });
          }
        });
      } catch {}
    }, 2000);
  }

  /* -------- signaling receive side (wire to live stream) -------- */
  attachSignals() {
    const handler = (data) => {
      if (!data || !this.call) return;
      if (data.type === 'voice_signal' &&
          data.data && data.data.from) {
        this.onSignal(data.data);
      }
    };
    this.unsubs.push(live.on('voice', handler));
    this.unsubs.push(live.on('mobile', (data) => {
      if (data && data.type === 'voice') {
        if (data.data && data.data.type === 'voice_signal') {
          this.onSignal(data.data.data);
        } else {
          handler(data.data);
        }
      }
    }));
  }

  async onSignal(sig) {
    if (!this.pc) return;
    try {
      if (sig.kind === 'offer') {
        await this.pc.setRemoteDescription(
          new RTCSessionDescription({ type: 'offer', sdp: sig.payload.sdp }));
        const answer = await this.pc.createAnswer();
        await this.pc.setLocalDescription(answer);
        this.signal('answer', { sdp: this.pc.localDescription.sdp,
                                type: this.pc.localDescription.type });
        if (sig.payload.media === 'screen') this.emit('remote-screen');
      } else if (sig.kind === 'answer') {
        if (this.pc.signalingState !== 'stable') {
          await this.pc.setRemoteDescription(new RTCSessionDescription(
            { type: 'answer', sdp: sig.payload.sdp }));
        }
      } else if (sig.kind === 'ice') {
        try {
          await this.pc.addIceCandidate(new RTCIceCandidate(sig.payload));
        } catch {}
      }
    } catch (e) {
      this.emit('signal-error', String(e && e.message || e));
    }
  }

  /* -------- incoming ring handling -------- */
  static watchIncoming(onInvite, onUpdate) {
    const desktop = (data) => {
      if (!data) return;
      if (data.type === 'voice_invite' && data.for !== myDeviceId()) {
        /* invites carry 'for' = target device; show for everyone else too
           so a computer can decline on behalf of its user */
      }
      if (data.type === 'voice_invite') onInvite(data);
      if (data.type === 'voice_update') onUpdate(data);
    };
    const mobileH = (data) => {
      if (!data) return;
      if (data.type === 'voice') {
        const d = data.data || {};
        if (d.type === 'voice_invite') onInvite({ for: data.for, data: d });
        if (d.type === 'voice_update') onUpdate({ for: data.for, data: d });
      }
    };
    const u1 = live.on('voice', desktop);
    const u2 = live.on('mobile', mobileH);
    return () => { u1(); u2(); };
  }
}

function myDeviceId() {
  try { return window.PHANTOM_DEVICE_ID || ''; } catch { return ''; }
}

export const rtc = new VoiceRTC();
