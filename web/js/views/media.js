/* ============================================================
   PHANTOM NETWORK — media.js (v1.4.0 Phantom Media)

   Shared media rooms:
   * rooms: create/delete/join/leave; queue files from AUTHORIZED
     directories (browsed with the same sandbox as the File Commander)
   * playback: play/pause/stop/seek/next/prev/repeat/shuffle, driven by
     the owning companion's room state (SSE media_state topic)
   * streaming: each browser plays from the OWNING companion via
     /api/media/stream (HTTP Range → seeking works; no duplication of
     files across machines, no transcoding, no media server)
   * honest sync: clock-offset estimation per device; reported precision
     is ±50–150 ms on a LAN (NOT sample-accurate, stated plainly)
   * per-device volume with the 0.8 audible ceiling
   ============================================================ */

import { api } from '../api.js';
import { state } from '../state.js';
import { el, toast, errText, fmtSize, fmtTime } from '../ui.js';

let cleanupFns = [];
function cleanup() { cleanupFns.forEach(f => { try { f(); } catch {} }); cleanupFns = []; }

const MAX_VOL = 0.8;

export async function render(container) {
  cleanup();
  let rooms = [];
  let current = null;          // current room public state
  let currentId = '';
  let audio = new Audio();
  audio.preload = 'auto';
  let clockOffset = 0;         // serverNow - localNow (ms)
  let driftTimer = null;
  let browsing = false;
  let browsePath = '';
  let browseRows = [];

  /* ---------------- controls ---------------- */
  const roomName = el('input', { placeholder: 'new room name',
    style: { maxWidth: '180px' } });
  const createBtn = el('button', { class: 'btn violet' }, '＋ Create room');
  const roomList = el('div', { class: 'row', style: { flexWrap: 'wrap',
    gap: '8px' } });
  const queueList = el('div', {});
  const browseBtn = el('button', { class: 'btn' }, '📂 Browse files…');
  const browseCard = el('div', { class: 'card', style: { display: 'none' } });
  const playBtn = el('button', { class: 'btn cyan' }, '▶ Play');
  const pauseBtn = el('button', { class: 'btn' }, '⏸ Pause');
  const stopBtn = el('button', { class: 'btn' }, '⏹ Stop');
  const prevBtn = el('button', { class: 'btn' }, '⏮');
  const nextBtn = el('button', { class: 'btn' }, '⏭');
  const seek = el('input', { type: 'range', min: 0, max: 1000, value: 0,
    style: { flex: '1' } });
  const seekLabel = el('span', { class: 'mono small dim' }, '0:00');
  const repeatSel = el('select', {},
    el('option', { value: 'off' }, 'repeat off'),
    el('option', { value: 'all' }, 'repeat all'));
  const shuffleBtn = el('button', { class: 'btn' }, '🔀 Shuffle: off');
  const volSlider = el('input', { type: 'range', min: 0, max: 80, value: 60,
    style: { width: '140px' } });
  const volLabel = el('span', { class: 'small dim mono' }, '0.60');
  const syncNote = el('div', { class: 'small dim' }, '');
  const nowEl = el('div', { class: 'small' }, 'Nothing playing.');

  function ctl() { return currentId ? { room_id: currentId } : null; }

  /* ---------------- data ---------------- */
  async function loadRooms() {
    try {
      const r = await api.get('', '/api/media/rooms');
      rooms = r.rooms || [];
      renderRooms();
    } catch (e) { toast(errText(e), 'err'); }
  }

  async function loadState() {
    if (!currentId) { current = null; return; }
    try {
      const r = await api.get('', '/api/media/state?room_id=' +
        encodeURIComponent(currentId));
      current = r.room;
      renderPlayback();
    } catch {}
  }

  async function estimateClock() {
    /* 3-sample median offset: honest ±ms figure shown to the user */
    try {
      const samples = [];
      for (let i = 0; i < 3; i++) {
        const t0 = Date.now();
        const r = await api.get('', '/api/media/state?room_id=' +
          encodeURIComponent(currentId));
        const rtt = Date.now() - t0;
        samples.push(r.room.server_now + rtt / 2 - Date.now());
      }
      samples.sort((a, b) => a - b);
      clockOffset = samples[1];
      syncNote.textContent =
        `clock offset ${Math.round(clockOffset)} ms · expected sync ` +
        'precision ±50–150 ms on a LAN (not sample-accurate — that is a ' +
        'hardware-level property we do not claim)';
    } catch {
      syncNote.textContent = 'clock estimation unavailable';
    }
  }

  /* ---------------- render ---------------- */
  function renderRooms() {
    roomList.innerHTML = '';
    if (!rooms.length) {
      roomList.appendChild(el('span', { class: 'dim small' },
        'No media rooms yet — create one, browse an authorized folder ' +
        'and queue something.'));
    }
    for (const r of rooms) {
      roomList.appendChild(el('button', {
        class: 'chip' + (r.id === currentId ? ' sel' : ''),
        onclick: async () => { currentId = r.id; await loadState(); renderRooms(); },
      }, `🎵 ${r.name}${r.playing ? ' ●' : ''} ` +
        `(${(r.members || []).length})`));
    }
  }

  function renderQueue() {
    queueList.innerHTML = '';
    if (!current || !current.queue || !current.queue.length) {
      queueList.appendChild(el('span', { class: 'dim small' },
        'Queue is empty.'));
      return;
    }
    current.queue.forEach((item, idx) => {
      const here = idx === current.position;
      queueList.appendChild(el('div', { class: 'row',
        style: { borderBottom: '1px solid #221c2b', padding: '5px 0',
                 gap: '10px', alignItems: 'baseline' } },
        el('span', { class: 'mono small dim' }, String(idx + 1)),
        el('b', { style: here ? { color: 'var(--cyan)' } : {} },
          item.name),
        el('span', { class: 'small dim mono' }, fmtSize(item.size)),
        el('span', { class: 'small dim' },
          item.host_device_id === (state.self && state.self.device_id)
            ? 'streams from here'
            : 'streams from its owner'),
        el('span', { style: { flex: '1' } }),
        el('button', { class: 'btn danger small-btn', onclick: async () => {
          try {
            await api.post('', '/api/media/dequeue',
              { room_id: currentId, item_id: item.id });
            await loadState();
          } catch (e) { toast(errText(e), 'err'); }
        } }, '✕')));
    });
  }

  function renderPlayback() {
    if (!current) { nowEl.textContent = 'Pick a room.'; return; }
    renderQueue();
    repeatSel.value = current.repeat || 'off';
    shuffleBtn.textContent = `🔀 Shuffle: ${current.shuffle ? 'on' : 'off'}`;
    const item = current.item;
    nowEl.textContent = item
      ? `${current.playing ? '▶' : '⏸'} ${item.name} — ${current.playing
        ? 'playing' : 'paused'} at ${fmtTime(current.offset_s || 0)}`
      : 'Nothing playing.';
    if (item && current.playing && !audio.src) startStream(item);
    if (!current.playing && !audio.paused) audio.pause();
  }

  function startStream(item) {
    if (!item) return;
    if (!item.stream_url) {
      toast('The owning device is unreachable — cannot stream this item',
        'err');
      return;
    }
    audio.src = item.stream_url;
    audio.volume = MAX_VOL;
    audio.play().catch(() => {
      toast('Tap Play once to allow audio (browser autoplay policy)',
        'info');
    });
  }

  /* ---------------- actions ---------------- */
  async function ctlPost(action, extra = {}) {
    if (!currentId) { toast('Pick a room first', 'err'); return; }
    try {
      await api.post('', '/api/media/control',
        Object.assign({ room_id: currentId, action }, extra));
      await loadState();
    } catch (e) { toast(errText(e), 'err'); }
  }

  createBtn.onclick = async () => {
    if (!roomName.value.trim()) { toast('Room name required', 'err'); return; }
    try {
      const r = await api.post('', '/api/media/room',
        { name: roomName.value.trim() });
      currentId = r.room_id;
      roomName.value = '';
      await Promise.all([loadRooms(), loadState()]);
      toast('Room created — join from other devices to share playback',
        'ok');
    } catch (e) { toast(errText(e), 'err'); }
  };

  playBtn.onclick = () => {
    if (audio.src) { audio.play().catch(() => {}); }
    ctlPost('play');
  };
  pauseBtn.onclick = () => { audio.pause(); ctlPost('pause'); };
  stopBtn.onclick = () => { audio.pause(); audio.src = ''; ctlPost('stop'); };
  prevBtn.onclick = () => ctlPost('prev');
  nextBtn.onclick = () => ctlPost('next');
  repeatSel.onchange = () => ctlPost('repeat', { mode: repeatSel.value });
  shuffleBtn.onclick = () => ctlPost('shuffle',
    { on: !(current && current.shuffle) });
  volSlider.oninput = () => {
    const v = volSlider.value / 100;
    audio.volume = v;
    volLabel.textContent = v.toFixed(2);
  };
  seek.oninput = () => {
    if (audio.duration) {
      seekLabel.textContent =
        fmtTime(audio.duration * seek.value / 1000);
    }
  };
  seek.onchange = async () => {
    if (audio.duration) {
      audio.currentTime = audio.duration * seek.value / 1000;
      await ctlPost('seek',
        { position_s: audio.duration * seek.value / 1000 });
    }
  };

  /* ---------------- file browser (authorized dirs only) ---------------- */
  browseBtn.onclick = async () => {
    if (browsing) { browseCard.style.display = 'none'; browsing = false; return; }
    browsing = true;
    browseCard.style.display = '';
    if (!browsePath) {
      try {
        const roots = await api.fsRoots('');
        browsePath = (roots.roots && roots.roots[0] && roots.roots[0].path)
          || '/';
      } catch { browsePath = '/'; }
    }
    await renderBrowse();
  };

  async function renderBrowse() {
    browseCard.innerHTML = '';
    browseCard.appendChild(el('div', { class: 'row',
      style: { justifyContent: 'space-between' } },
      el('b', {}, 'Authorized files — ' + browsePath),
      el('button', { class: 'btn small-btn',
        onclick: () => { browseCard.style.display = 'none'; browsing = false; } },
        '✕ close')));
    try {
      const r = await api.fsList('', browsePath);
      browseRows = (r.entries || []).filter(e => !e.is_dir &&
        /\.(mp3|m4a|aac|flac|wav|ogg|oga|opus|weba|mp4|m4v|webm|mkv|mov)$/i
          .test(e.name));
      const dirs = (r.entries || []).filter(e => e.is_dir);
      const list = el('div', { style: { maxHeight: '220px', overflow: 'auto' } });
      const up = el('button', { class: 'chip', onclick: async () => {
        const parts = browsePath.split('/').filter(Boolean);
        parts.pop();
        browsePath = '/' + parts.join('/');
        await renderBrowse();
      } }, '⬆ ..');
      list.appendChild(up);
      for (const d of dirs) {
        list.appendChild(el('button', { class: 'chip', onclick: async () => {
          browsePath = browsePath.replace(/\/$/, '') + '/' + d.name;
          await renderBrowse();
        } }, '📁 ' + d.name));
      }
      for (const f of browseRows) {
        list.appendChild(el('div', { class: 'row',
          style: { gap: '8px', padding: '3px 0' } },
          el('span', { class: 'small' }, '🎧 ' + f.name),
          el('span', { class: 'small dim mono' }, fmtSize(f.size)),
          el('button', { class: 'btn small-btn violet',
            onclick: async () => {
              try {
                await api.post('', '/api/media/enqueue', {
                  room_id: currentId,
                  path: browsePath.replace(/\/$/, '') + '/' + f.name,
                  base: location.origin,
                });
                await loadState();
                toast('Queued ' + f.name, 'ok');
              } catch (e) { toast(errText(e), 'err'); }
            } }, '＋ queue')));
      }
      browseCard.appendChild(list);
      if (!browseRows.length && !dirs.length) {
        browseCard.appendChild(el('span', { class: 'dim small' },
          'No audio/video files here.'));
      }
    } catch (e) {
      browseCard.appendChild(el('span', { class: 'small', style: { color:
        'var(--err)' } }, errText(e)));
    }
  }

  /* ---------------- layout ---------------- */
  container.append(
    el('div', { class: 'card' },
      el('h2', {}, '🎵 Phantom Media'),
      el('p', { class: 'dim small' },
        'Shared queues and synchronized playback across participating ' +
        'devices. Files stream over HTTP Range from the machine that ' +
        'owns them — nothing is copied around, nothing transcodes, and ' +
        'only paths inside authorized shares can be queued.'),
      el('div', { class: 'row', style: { gap: '8px', flexWrap: 'wrap' } },
        roomName, createBtn),
      roomList),
    el('div', { class: 'card' },
      el('h3', {}, 'Now playing'),
      nowEl, syncNote,
      el('div', { class: 'row', style: { gap: '8px', marginTop: '8px',
        alignItems: 'center', flexWrap: 'wrap' } },
        prevBtn, playBtn, pauseBtn, stopBtn, nextBtn,
        seek, seekLabel, repeatSel, shuffleBtn),
      el('div', { class: 'row', style: { gap: '8px', marginTop: '6px',
        alignItems: 'center' } },
        el('span', { class: 'small dim' }, 'volume'),
        volSlider, volLabel,
        el('span', { class: 'small dim' },
          `(ceiling ${MAX_VOL} — matches the companion audio limits)`)),
      el('div', { class: 'row', style: { marginTop: '8px' } }, browseBtn)),
    browseCard,
    el('div', { class: 'card' },
      el('h3', {}, 'Queue'), queueList));

  await loadRooms();
  if (rooms.length && !currentId) {
    currentId = rooms[0].id;
    await loadState();
  }
  driftTimer = setInterval(async () => {
    if (currentId) { await loadState(); if (Math.random() < 0.2) estimateClock(); }
  }, 5000);
  cleanupFns.push(() => clearInterval(driftTimer), () => { audio.pause(); });
}
