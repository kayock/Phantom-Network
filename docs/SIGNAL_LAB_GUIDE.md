# Signal Laboratory — User Guide (v1.4.0)

An experimental digital-audio communications lab: tone generation,
spectrum analysis with exportable results, a waterfall spectrogram,
Morse code with practice sessions, and packetized acoustic messaging
with FEC and ARQ. Everything runs locally; nothing is covert.

## Tone generator

Sine/square/saw/triangle from 20 Hz–20 kHz with envelopes; send tones to
the OS player or use them as test signals for the analyzer.

## Spectrum analyzer (v1.4.0 upgrades)

- **Adjustable FFT resolution** 512–4096 — click **🎤 Enable microphone**
  for live input, or analyze an audio file.
- **Readouts**: dominant/peak frequency, RMS level, peak amplitude,
  sample rate — all live.
- **Frequency markers** — ⊕ Mark peak pins the current dominant
  frequency as a chip; Clear removes them.
- **Exportable analysis** — ⬇ Export writes a JSON snapshot (sample
  rate, FFT size, dominant Hz, RMS, peak, markers) for offline
  comparison between experiments.
- **Waterfall tab** — scrolling spectrogram (time flows left, frequency
  rises); tones and modem packets appear as bright violet/cyan traces.
  Display-only; nothing is recorded.

## Morse lab

Text↔Morse codec with audio playback, adjustable WPM and **Farnsworth
timing** (separate character speed vs. effective speed — the standard
learner-friendly timing), and a visual event timeline.

## Morse practice (v1.4.0)

- **New session** draws a random word group (3–12 words).
- **Play prompt** transmits it at your chosen speed; **Reveal** shows
  the hidden text.
- **Score my answer** grades your decode character-exactly with
  accuracy percentage and honest feedback. Everything is local.

## Acoustic messaging (v1 modem)

Packetized FSK over speaker/microphone: text is framed into independent
packets `[PH][seq][total][len][payload][CRC16]`, sent as audible tones
(1000/1800 Hz bits, 2800 Hz lead, 600 Hz end) at three rates (fast /
normal / robust). The receiver decodes packets independently, so a
partial loss still recovers the packets that made it, and CRC failure
means a packet is REJECTED — never fabricated as received.

## Acoustic chat (v2 modem — v1.4.0)

Adds on top of the v1 framing:

- **Hamming(7,4) FEC** per nibble: single flipped bits are corrected;
  double-bit patterns may miscorrect, which is exactly why every v2
  frame still carries CRC16 — the CRC is the final arbiter, and a
  CRC-failed packet is reported, not guessed.
- **ARQ** — the receiver builds a NAK frame listing missing/corrupt
  packet numbers; pressing ↻ Request retransmit re-sends exactly those
  packets.
- **Offline self-test** — a DIGITAL simulation: synthesize → optional
  seeded noise → decode, printing an exact-match or honest-failure
  result. Clearly a simulation; real speaker↔microphone range depends
  on room acoustics, volume and sample-rate alignment.

All audible signaling stays within comfortable listening levels
(0.6–0.8 amplitude, 600–2800 Hz). There is no ultrasonic mode, no
covert signaling, no hidden channel.

## Determinism

Simulations are seeded (`mulberry32`) — the same seed always produces
the same noise/corruption pattern, so regression tests assert exact
outcomes. The Python eval harness (`tests/unit/test_v140_dsp.py`) runs
this shipped JS under node and asserts both directions: clean decodes
succeed AND hostile scenarios fail honestly.
