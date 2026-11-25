/* PHANTOM NETWORK — signal.js (Signal Laboratory)
   Tabs: tone generator · spectrum analyzer · morse lab · acoustic messaging */

import { el, toast, errText } from '../ui.js';
import { audioCtx, playTone } from '../lib/sfx.js';
import {
  textToMorse, morseToText, playMorse, morseTimeline,
  morseTimelineFarnsworth, playMorseAdvanced, randomPracticeMessage,
  encodeAcoustic, acousticDuration, playAcoustic, decodeAcoustic,
  offlineRoundtrip, peakFrequency, goertzel,
  MODEM_RATES, modemPackets, modemDuration, playModem, modemOfflineRoundtrip,
  rmsLevel, peakAmplitude, signalDurationS,
} from '../lib/dsp.js';

export async function render(container) {
  const wrap = el('div', {});
  container.appendChild(wrap);
  const cleanups = [];
  let activeTab = 'tone';

  const tabbar = el('div', { class: 'tabbar' });
  const body = el('div', {});
  wrap.appendChild(tabbar);
  wrap.appendChild(body);

  function setTab(name) {
    activeTab = name;
    cleanups.splice(0).forEach(fn => fn());
    body.innerHTML = '';
    tabbar.innerHTML = '';
    for (const [id, label] of [['tone', '∿ Tone generator'], ['spectrum', '... Spectrum analyzer'],
                               ['waterfall', '🌊 Waterfall'], ['morse', '·−· Morse lab'],
                               ['acoustic', '📡 Acoustic messaging'], ['modem', '🔌 Acoustic modem']]) {
      tabbar.appendChild(el('div', { class: 'tab' + (activeTab === id ? ' active' : ''),
        onclick: () => setTab(id) }, label));
    }
    ({ tone: tabTone, spectrum: tabSpectrum, waterfall: tabWaterfall, morse: tabMorse,
       acoustic: tabAcoustic, modem: tabModem })[name]();
  }

  /* ================= 1 · tone generator ================= */
  function tabTone() {
    const freq = el('input', { type: 'range', min: '20', max: '20000', value: '440', style: { width: '240px' } });
    const freqN = el('span', { class: 'vu' }, '440 Hz');
    freq.oninput = () => freqN.textContent = `${freq.value} Hz`;
    const wave = el('select', {}, ['sine', 'square', 'triangle', 'sawtooth'].map(w => el('option', { value: w }, w)));
    const dur = el('input', { type: 'number', value: '2', min: '0.1', max: '30', step: '0.1', style: { width: '90px' } });
    const vol = el('input', { type: 'range', min: '0', max: '0.8', step: '0.02', value: '0.5', style: { width: '200px' } });
    const volN = el('span', { class: 'vu' }, '0.50');
    vol.oninput = () => volN.textContent = Number(vol.value).toFixed(2);
    let stopFn = null;
    const play = () => {
      stop();
      stopFn = playTone({ freq: Number(freq.value), duration: Number(dur.value),
                          type: wave.value, volume: Number(vol.value) });
      setTimeout(() => { stopFn = null; }, Number(dur.value) * 1000 + 50);
    };
    const stop = () => { if (stopFn) { try { stopFn.stop(); } catch {} stopFn = null; } };

    body.appendChild(el('div', { class: 'panel' },
      el('h3', {}, 'Tone generator'),
      el('div', { class: 'fld-row' },
        el('label', { class: 'fld' }, 'Frequency (20–20000 Hz)', freq, freqN),
        el('label', { class: 'fld' }, 'Waveform', wave),
        el('label', { class: 'fld' }, 'Duration (s)', dur),
        el('label', { class: 'fld' }, 'Volume', vol, volN),
        el('button', { class: 'btn primary', onclick: play }, '▶ Play'),
        el('button', { class: 'btn danger', onclick: stop }, '■ Stop'))));
    body.appendChild(el('div', { class: 'note' },
      'Generated locally in this browser with the Web Audio API. Use Audio Command to play tones on remote devices.'));
  }

  /* ================= 2 · spectrum analyzer ================= */
  function tabSpectrum() {
    let analyser = null, micStream = null, raf = null, sourceNode = null;
    const canvas = el('canvas', { class: 'scope' });
    const waveCanvas = el('canvas', { class: 'scope' });
    const fftSel = el('select', {}, [512, 1024, 2048, 4096].map(n =>
      el('option', { value: n }, `FFT ${n}`)));
    const peakEl = el('span', { class: 'vu' }, 'peak: —');
    const status = el('span', { class: 'dim small' }, 'input idle');

    const fileInput = el('input', { type: 'file', accept: 'audio/*', style: { display: 'none' } });

    async function startMic() {
      try {
        micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const ac = audioCtx();
        sourceNode = ac.createMediaStreamSource(micStream);
        analyser = ac.createAnalyser();
        analyser.fftSize = Number(fftSel.value);
        sourceNode.connect(analyser);
        status.textContent = 'live microphone input (explicitly enabled)';
        draw();
      } catch (e) {
        toast('Microphone unavailable: ' + errText(e) + ' (browsers allow mic only on secure contexts)', 'err', 6000);
      }
    }
    function stopMic() {
      if (micStream) micStream.getTracks().forEach(t => t.stop());
      if (sourceNode) { try { sourceNode.disconnect(); } catch {} }
      micStream = sourceNode = null;
      status.textContent = 'input idle';
    }

    async function analyzeFile(file) {
      try {
        const ac = audioCtx();
        const buf = await ac.decodeAudioData(await file.arrayBuffer());
        const src = ac.createBufferSource();
        src.buffer = buf;
        const an = ac.createAnalyser();
        an.fftSize = Number(fftSel.value);
        src.connect(an);
        src.start();
        analyser = an;
        status.textContent = `analyzing file: ${file.name} (${buf.duration.toFixed(1)}s)`;
        draw();
      } catch (e) { toast('Cannot decode audio: ' + errText(e), 'err'); }
    }

    function draw() {
      raf = requestAnimationFrame(draw);
      const ac = audioCtx();
      const binHz = ac.sampleRate / (analyser ? analyser.fftSize : 2048);
      // spectrum
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width; canvas.height = rect.height;
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#05060a'; ctx.fillRect(0, 0, canvas.width, canvas.height);
      if (analyser) {
        const bins = new Uint8Array(analyser.frequencyBinCount);
        analyser.getByteFrequencyData(bins);
        const n = Math.min(bins.length, Math.floor(20000 / binHz));
        const barW = canvas.width / n;
        for (let i = 0; i < n; i++) {
          const h = (bins[i] / 255) * canvas.height;
          const g = ctx.createLinearGradient(0, canvas.height, 0, canvas.height - h);
          g.addColorStop(0, '#6d28d9'); g.addColorStop(1, '#22d3ee');
          ctx.fillStyle = g;
          ctx.fillRect(i * barW, canvas.height - h, Math.max(1, barW - 1), h);
        }
        const pk = peakFrequency(bins.slice(0, n), binHz);
        if (pk != null && bins[pk / binHz] > 20) peakEl.textContent = `peak: ${pk.toFixed(0)} Hz`;
        else peakEl.textContent = 'peak: —';
        // waveform
        const wctx = waveCanvas.getContext('2d');
        waveCanvas.width = waveCanvas.getBoundingClientRect().width;
        waveCanvas.height = waveCanvas.getBoundingClientRect().height;
        wctx.fillStyle = '#05060a'; wctx.fillRect(0, 0, waveCanvas.width, waveCanvas.height);
        const time = new Uint8Array(analyser.fftSize);
        analyser.getByteTimeDomainData(time);
        wctx.strokeStyle = '#a78bfa';
        wctx.beginPath();
        for (let i = 0; i < time.length; i++) {
          const x = (i / time.length) * waveCanvas.width;
          const y = (time[i] / 255) * waveCanvas.height;
          i === 0 ? wctx.moveTo(x, y) : wctx.lineTo(x, y);
        }
        wctx.stroke();
      }
    }

    body.appendChild(el('div', { class: 'panel' },
      el('h3', {}, 'Audio spectrum analyzer'),
      canvas, waveCanvas,
      el('div', { class: 'row mt' },
        el('button', { class: 'btn primary', onclick: startMic }, '🎤 Enable microphone'),
        el('button', { class: 'btn danger', onclick: stopMic }, '■ Stop input'),
        el('button', { class: 'btn', onclick: () => fileInput.click() }, 'Analyze audio file'),
        fftSel, peakEl, status)));
    body.appendChild(el('div', { class: 'note' },
      'Microphone input is off by default and only runs after you press “Enable microphone”. ' +
      'Adjustable analysis parameters: FFT resolution 512–4096. Peak frequency detection runs on the live spectrum.'));
    body.appendChild(fileInput);
    fileInput.addEventListener('change', () => {
      if (fileInput.files[0]) analyzeFile(fileInput.files[0]);
    });

    draw();
    cleanups.push(() => { if (raf) cancelAnimationFrame(raf); stopMic(); });
  }

  /* ================= 3 · morse lab ================= */
  function tabMorse() {
    const input = el('textarea', { style: { width: '100%', minHeight: '60px' } });
    input.value = 'PHANTOM NETWORK ONLINE';
    const output = el('div', { class: 'log-view', style: { minHeight: '30px' } }, '');
    const wpm = el('input', { type: 'number', value: '12', min: '5', max: '35', style: { width: '70px' } });
    const fwpm = el('input', { type: 'number', value: '0', min: '0', max: '35', style: { width: '70px' },
      title: 'Farnsworth spacing (0 = off): characters keep speed, gaps stretch' });
    const freq = el('input', { type: 'number', value: '700', style: { width: '80px' } });
    const timeline = el('canvas', { class: 'scope', style: { height: '70px' } });
    const practiceOut = el('div', { class: 'vu', style: { fontSize: '14px' } }, 'practice idle');

    input.addEventListener('input', () => { output.textContent = textToMorse(input.value); paintTimeline(); });
    const unit = () => 1.2 / Number(wpm.value);   // PARIS standard: dot = 1.2s/wpm
    const farns = () => { const f = Number(fwpm.value) || 0; return f > 0 && f < Number(wpm.value) ? f : null; };

    function paintTimeline() {
      const m = textToMorse(input.value);
      const tl = farns() ? morseTimelineFarnsworth(m, { wpm: Number(wpm.value), fwpm: farns() })
                         : morseTimeline(m, unit());
      const rect = timeline.getBoundingClientRect();
      timeline.width = rect.width; timeline.height = rect.height;
      const ctx = timeline.getContext('2d');
      ctx.fillStyle = '#05060a'; ctx.fillRect(0, 0, timeline.width, timeline.height);
      const total = tl.reduce((a, s) => Math.max(a, s.start + s.len), 0.0001);
      const scale = timeline.width / total;
      tl.forEach((s, i) => {
        ctx.fillStyle = s.sym === '.' ? '#22d3ee' : '#a78bfa';
        ctx.fillRect(s.start * scale, 14 + (i % 2) * 14, Math.max(2, s.len * scale), 10);
      });
    }

    const play = () => {
      const m = textToMorse(input.value);
      if (farns()) {
        playMorseAdvanced(m, { wpm: Number(wpm.value), fwpm: farns(),
                               freq: Number(freq.value), volume: 0.55, playFn: playTone });
      } else {
        playMorse(m, { unit: unit(), freq: Number(freq.value), volume: 0.55, playFn: playTone });
      }
    };

    /* practice mode: random original words, reveal on demand */
    let practiceWord = '';
    const newPractice = () => {
      practiceWord = randomPracticeMessage(1, Date.now());
      const m = textToMorse(practiceWord);
      if (farns()) {
        playMorseAdvanced(m, { wpm: Number(wpm.value), fwpm: farns(),
                               freq: Number(freq.value), volume: 0.55, playFn: playTone });
      } else {
        playMorse(m, { unit: unit(), freq: Number(freq.value), volume: 0.55, playFn: playTone });
      }
      practiceOut.textContent = 'listen… (decode test below / reveal on demand)';
    };
    const revealPractice = () => practiceOut.textContent =
      practiceWord ? `the word was: ${practiceWord}` : 'no active practice word';

    body.appendChild(el('div', { class: 'fld-row' },
      el('button', { class: 'btn cyan', onclick: newPractice }, '🎓 Practice: play random word'),
      el('button', { class: 'btn', onclick: revealPractice }, 'Reveal'),
      practiceOut));

    // decode (typed)
    const morseIn = el('textarea', { style: { width: '100%', minHeight: '50px' }, placeholder: '.--. .... .- -. - --- -- / -. . - .-- --- .-. -.-' });
    const decOut = el('div', { class: 'vu', style: { fontSize: '15px' } }, '');
    morseIn.addEventListener('input', () => { decOut.textContent = morseToText(morseIn.value); });

    // experimental mic decode via goertzel
    const micDecOut = el('div', { class: 'log-view', style: { minHeight: '40px' } }, 'mic decoder idle — experimental feature');
    let micRaf = null, micAnalyser = null, micBuf = null;
    const startMicDecode = async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const ac = audioCtx();
        const src = ac.createMediaStreamSource(stream);
        micAnalyser = ac.createAnalyser();
        micAnalyser.fftSize = 2048;
        src.connect(micAnalyser);
        micBuf = new Uint8Array(micAnalyser.fftSize);
        const freqs = Number(freq.value);
        const dotSec = unit();
        let lastState = false, stateSince = performance.now(), symbols = [];
        const tick = () => {
          micRaf = requestAnimationFrame(tick);
          micAnalyser.getByteTimeDomainData(micBuf);
          const f32 = new Float32Array(micBuf.length);
          for (let i = 0; i < micBuf.length; i++) f32[i] = (micBuf[i] - 128) / 128;
          const p = goertzel(f32, freqs, ac.sampleRate);
          const on = p > 0.02;
          const now = performance.now();
          if (on !== lastState) {
            const dur = (now - stateSince) / 1000;
            stateSince = now;
            if (lastState) {
              symbols.push(dur > dotSec * 2 ? '-' : '.');
              micDecOut.textContent = 'listening: ' + symbols.join(' ');
            } else {
              const gap = dur;
              if (gap > dotSec * 5) {
                const text = morseToText(symbols.join(' '));
                if (symbols.length && text.trim()) micDecOut.textContent = 'decoded: ' + text;
                symbols = [];
              } else if (gap > dotSec * 2) {
                const text = morseToText(symbols.join(' '));
                if (symbols.length && text.trim()) micDecOut.textContent = 'letter: ' + text;
              }
            }
            lastState = on;
          }
        };
        tick();
        micDecOut.textContent = 'mic decoder listening — key the microphone with tones at ' + freq.value + ' Hz';
      } catch (e) {
        micDecOut.textContent = 'mic unavailable: ' + errText(e);
      }
    };
    const stopMicDecode = () => { if (micRaf) cancelAnimationFrame(micRaf); micRaf = null; };

    body.appendChild(el('div', { class: 'panel' },
      el('h3', {}, 'Text → Morse'),
      input, output,
      el('div', { class: 'row mt' },
        el('label', { class: 'fld' }, 'Speed (WPM)', wpm),
      el('label', { class: 'fld' }, 'Farnsworth WPM (0=off)', fwpm),
        el('label', { class: 'fld' }, 'Tone (Hz)', freq),
        el('button', { class: 'btn primary', onclick: play }, '▶ Play morse'),
        el('span', { class: 'dim small' }, 'dot = 1.2 s / WPM (PARIS standard)')),
      timeline));
    body.appendChild(el('div', { class: 'panel' },
      el('h3', {}, 'Morse → Text'),
      morseIn, decOut));
    body.appendChild(el('div', { class: 'panel' },
      el('h3', {}, 'Microphone decoding (experimental)'),
      el('div', { class: 'row' },
        el('button', { class: 'btn', onclick: startMicDecode }, '🎤 Start listening'),
        el('button', { class: 'btn danger', onclick: stopMicDecode }, '■ Stop')),
      el('div', { class: 'mt' }, micDecOut),
      el('div', { class: 'note warn' },
        'This on/off-keying decoder is experimental: it works with strong, close-range signals and ' +
        'clean timing. Noisy rooms will produce wrong or partial decodes — the decoder reports what it ' +
        'heard and never fabricates text it did not detect.')));
    paintTimeline();
    cleanups.push(stopMicDecode);
  }

  /* ================= 4 · acoustic messaging ================= */
  function tabAcoustic() {
    const input = el('textarea', { style: { width: '100%', minHeight: '54px' } });
    input.value = 'HELLO PHANTOM';
    const info = el('div', { class: 'dim small mono' }, '');
    const sendVol = el('input', { type: 'range', min: '0', max: '0.8', step: '0.05', value: '0.7', style: { width: '180px' } });

    function updateInfo() {
      const tones = encodeAcoustic(input.value);
      info.textContent = `${tones.length} tones · ${acousticDuration(tones).toFixed(1)} s · FSK 1000/1800 Hz · CRC-16 protected`;
    }
    input.addEventListener('input', updateInfo);
    updateInfo();

    const send = () => {
      const tones = encodeAcoustic(input.value);
      playAcoustic(tones, Number(sendVol.value), playTone);
      toast('Transmitting acoustically — hold the receiving device’s mic close', 'ok');
    };

    // receive
    const recOut = el('div', { class: 'log-view', style: { minHeight: '60px' } }, 'receiver idle');
    let recCtx = null, recRaf = null;
    const startRec = async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        recCtx = audioCtx();
        const src = recCtx.createMediaStreamSource(stream);
        recOut.textContent = 'listening for FSK frames…';
        const sampleRate = recCtx.sampleRate;
        const collected = [];
        const processor = recCtx.createScriptProcessor(8192, 1, 1);
        processor.onaudioprocess = (ev) => {
          collected.push(new Float32Array(ev.inputBuffer.getChannelData(0)));
        };
        src.connect(processor);
        recCtx.destination && processor.connect(recCtx.destination);
        recRaf = setInterval(() => {
          // try decode over the last ~8 seconds
          const total = collected.reduce((a, b) => a + b.length, 0);
          if (total < sampleRate * 1.5) return;
          const buf = new Float32Array(total);
          let off = 0;
          for (const c of collected) { buf.set(c, off); off += c.length; }
          if (buf.length > sampleRate * 8) { collected.splice(0, collected.length - 3); }
          const dec = decodeAcoustic(buf, sampleRate);
          if (dec.text || dec.errors.length) {
            recOut.textContent = dec.text
              ? `✓ DECODED (confidence ${dec.confidence.toFixed(2)}): "${dec.text}"`
              : `… ${dec.errors.join('; ')}`;
            if (dec.text) toast(`Acoustic message decoded: ${dec.text}`, 'ok');
          }
        }, 1200);
      } catch (e) {
        recOut.textContent = 'microphone unavailable: ' + errText(e) +
          ' (secure context required)';
      }
    };
    const stopRec = () => { if (recRaf) clearInterval(recRaf); recRaf = null; recOut.textContent = 'receiver idle'; };

    // offline demo
    const demoOut = el('div', { class: 'log-view', style: { minHeight: '60px' } }, '');
    const runDemo = () => {
      const lines = [];
      for (const [text, snr] of [['PHANTOM', 18], ['GHOST PROTOCOL', 12], ['BAD_данные шум', 8]]) {
        const r = offlineRoundtrip(text, { snr });
        lines.push(`SNR ${r.snr} dB · "${r.expected}" → ${r.ok ? `OK "${r.got}"` : `REFUSED (${r.errors.join('; ')})`} · conf ${r.confidence.toFixed(2)}`);
      }
      demoOut.textContent = lines.join('\n') + '\n\nOffline simulation: synthesized FSK + gaussian-ish noise, decoded with the same Goertzel pipeline used for live mic decoding.';
    };

    body.appendChild(el('div', { class: 'panel' },
      el('h3', {}, 'Acoustic messaging (experimental)'),
      input, info,
      el('div', { class: 'row mt' },
        el('label', { class: 'fld' }, 'Send volume', sendVol),
        el('button', { class: 'btn primary', onclick: send }, '📡 Transmit via speakers'),
        el('span', { class: 'dim small' }, 'UTF-8 text, 120-char cap, framed: lead + sync + length + payload + CRC16 + end')),
      el('div', { class: 'row mt' },
        el('button', { class: 'btn', onclick: startRec }, '🎤 Start receiver'),
        el('button', { class: 'btn danger', onclick: stopRec }, '■ Stop receiver')),
      el('div', { class: 'mt' }, recOut)));
    body.appendChild(el('div', { class: 'panel' },
      el('h3', {}, 'Offline encode/decode demonstration'),
      el('button', { class: 'btn cyan', onclick: runDemo }, '▶ Run roundtrip simulation'),
      el('div', { class: 'mt' }, demoOut)));
    body.appendChild(el('div', { class: 'note' },
      'Real-world speaker→microphone decoding depends on room noise, volume and mic quality. ' +
      'The decoder reports confidence and CRC status and REFUSES to display text it cannot verify. ' +
      'Use the offline demonstration for a guaranteed deterministic result.'));
    cleanups.push(stopRec);
  }

  setTab('tone');
  return () => { cleanups.splice(0).forEach(fn => fn()); };
}
