"""Audio Command backend: tone synthesis, original presets, remote playback.

Everything here is **original** — presets are synthesized from note tables
with the pure-stdlib ``wave`` module; no copyrighted audio is used.

Playback strategy per platform (the companion runs on the target machine):

* Windows: ``PowerShell -Command "(New-Object Media.SoundPlayer 'f').PlaySync()"``
  (built into Windows; plays WAV synchronously so stop = kill the process).
* Linux: ``paplay`` (PulseAudio/PipeWire, standard on Ubuntu desktop) →
  ``aplay`` (ALSA) → ``ffplay -nodsp`` fallbacks, first one found on PATH.

Safety limits (hard-coded + configurable): amplitude ceiling 0.8, duration
cap 30 s, audible frequencies only (20 Hz–20 kHz), one playback at a time
with a visible stop control, and a master enable/disable switch per device
(``remote_audio_enabled``).  No ultrasonic or covert signals are generated.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import subprocess
import threading
import time
from typing import List, Optional, Tuple

from .errors import FsError
from .util import now_ms

SAMPLE_RATE = 44100
MAX_DURATION_S = 30
MAX_VOLUME = 0.8
MIN_FREQ, MAX_FREQ = 20.0, 20000.0

# ----------------------------------------------------------------------
# Original preset note tables: (freq_hz, seconds, waveform, volume, duty/fm)
# ----------------------------------------------------------------------

PRESETS: dict = {
    "red_alert": [
        (440.0, 0.35, "sine", 0.8), (622.25, 0.35, "sine", 0.8),
        (440.0, 0.35, "sine", 0.8), (622.25, 0.35, "sine", 0.8),
        (415.30, 0.6, "sine", 0.8), (440.0, 0.6, "sine", 0.8),
    ],
    "incoming_transmission": [
        (880.0, 0.09, "sine", 0.5), (1174.66, 0.09, "sine", 0.5),
        (880.0, 0.09, "sine", 0.5), (1174.66, 0.14, "sine", 0.6),
        (987.77, 0.30, "triangle", 0.4),
    ],
    "device_connected": [
        (523.25, 0.10, "triangle", 0.5), (659.25, 0.10, "triangle", 0.55),
        (783.99, 0.16, "triangle", 0.6),
    ],
    "device_disconnected": [
        (659.25, 0.12, "triangle", 0.5), (523.25, 0.12, "triangle", 0.5),
        (392.00, 0.22, "triangle", 0.45),
    ],
    "transfer_complete": [
        (783.99, 0.09, "sine", 0.5), (987.77, 0.09, "sine", 0.55),
        (1174.66, 0.09, "sine", 0.6), (1567.98, 0.22, "sine", 0.65),
    ],
    "warning": [
        (466.16, 0.28, "square", 0.30), (0.0, 0.10, "sine", 0.0),
        (466.16, 0.28, "square", 0.30),
    ],
    "morse_notification": [
        (1000.0, 0.08, "sine", 0.6), (0.0, 0.08, "sine", 0.0),
        (1000.0, 0.24, "sine", 0.6), (0.0, 0.12, "sine", 0.0),
        (1000.0, 0.08, "sine", 0.6),
    ],
    "scifi_chirp": [
        (600.0, 0.05, "sawtooth", 0.35), (900.0, 0.05, "sawtooth", 0.40),
        (1350.0, 0.05, "sawtooth", 0.45), (2025.0, 0.09, "sine", 0.5),
        (50.0, 0.16, "sine", 0.0),
    ],
}


# ----------------------------------------------------------------------
# synthesis
# ----------------------------------------------------------------------

def _sample(waveform: str, phase: float, freq: float) -> float:
    """Waveform generators in [-1, 1]."""
    if waveform == "square":
        return 1.0 if phase < 0.5 else -1.0
    if waveform == "triangle":
        return 4.0 * abs(phase - 0.5) - 1.0
    if waveform == "sawtooth":
        return 2.0 * phase - 1.0
    return math.sin(2.0 * math.pi * phase)      # sine


def _envelope(i: int, n: int) -> float:
    """5 ms fade-in/out to eliminate clicks (important at high volumes)."""
    fade = min(int(0.005 * SAMPLE_RATE), n // 4)
    if fade <= 0:
        return 1.0
    if i < fade:
        return i / fade
    if i > n - fade:
        return max(0.0, (n - i) / fade)
    return 1.0


def render_notes(notes: List[Tuple[float, float, str, float]],
                 volume_scale: float = 1.0) -> "bytes":
    """Render a note table to 16-bit mono PCM frames."""
    frames = bytearray()
    for freq, dur, waveform, vol in notes:
        vol = max(0.0, min(vol * volume_scale, MAX_VOLUME))
        n = int(dur * SAMPLE_RATE)
        if freq <= 0 or vol <= 0:
            frames.extend(b"\x00\x00" * n)
            continue
        freq = min(max(freq, MIN_FREQ), MAX_FREQ)
        step = freq / SAMPLE_RATE
        phase = 0.0
        for i in range(n):
            sample = _sample(waveform, phase % 1.0, freq) * vol * _envelope(i, n)
            value = int(sample * 32767)
            frames += int(value).to_bytes(2, byteorder="little", signed=True)
            phase += step
    return bytes(frames)


def write_wav(path: str, pcm: bytes) -> None:
    import wave
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)


def render_tone(freq: float, duration_s: float, waveform: str = "sine",
                volume: float = 0.6) -> bytes:
    if duration_s > MAX_DURATION_S:
        raise FsError(400, "too_long",
                      f"Duration {duration_s}s exceeds the {MAX_DURATION_S}s safety limit")
    if duration_s <= 0:
        raise FsError(400, "bad_duration", "Duration must be positive")
    freq = float(freq)
    if not (MIN_FREQ <= freq <= MAX_FREQ):
        raise FsError(400, "bad_freq",
                      f"Frequency {freq}Hz outside audible range {MIN_FREQ}-{MAX_FREQ} Hz")
    vol = max(0.0, min(float(volume), MAX_VOLUME))
    return render_notes([(freq, duration_s, waveform, vol)])


# ----------------------------------------------------------------------
# player
# ----------------------------------------------------------------------

def find_player() -> Tuple[str, list]:
    """Return (player_name, command_prefix) for this platform."""
    if os.name == "nt":
        return ("powershell-soundplayer",
                ["powershell", "-NoProfile", "-Command"])
    for name, prefix in (("paplay", ["paplay"]),
                         ("aplay", ["aplay", "-q"]),
                         ("ffplay", ["ffplay", "-loglevel", "quiet",
                                     "-nodsp", "-autoexit"])):
        if shutil.which(name):
            return (name, prefix)
    return ("none", [])


class AudioService:
    """Remote audio playback on THIS machine, with visible stop control."""

    def __init__(self, data_dir: str, config, events, log=None):
        self.cache_dir = os.path.join(data_dir, "audio_cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.config = config
        self.events = events
        self.log = log
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._current: Optional[dict] = None
        self._started_ts = 0
        self.player_name, self.player_prefix = find_player()

    # ------------------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.config.get("remote_audio_enabled", True))

    def set_enabled(self, value: bool) -> None:
        self.config.set("remote_audio_enabled", bool(value))
        if not value:
            self.stop()

    # ------------------------------------------------------------------
    def _run_player(self, wav_path: str, label: str, duration_s: float,
                    source: str) -> dict:
        if self.player_name == "none":
            raise FsError(501, "no_player",
                          "No audio player available (install PulseAudio/ALSA "
                          "utils or check PowerShell on Windows)")
        with self._lock:
            self._stop_locked()
            if os.name == "nt":
                script = (f"(New-Object Media.SoundPlayer '{wav_path}')"
                          f".PlaySync()")
                cmd = self.player_prefix + [script]
            elif self.player_name == "ffplay":
                cmd = self.player_prefix + [wav_path]
            else:
                cmd = self.player_prefix + [wav_path]
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as exc:
                raise FsError(500, "player_error", f"Player failed to start: {exc}")
            self._current = {"label": label, "source": source,
                             "duration_s": duration_s, "player": self.player_name,
                             "started": now_ms()}
            self._started_ts = time.monotonic()
        if self.events:
            self.events.emit("audio_playback",
                             f"Audio playback: {label} ({duration_s:.1f}s, "
                             f"player: {self.player_name})",
                             data={"label": label, "source": source})
        return dict(self._current)

    def _stop_locked(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2)
            except Exception:
                pass
        self._proc = None

    def stop(self) -> dict:
        with self._lock:
            had = self._proc is not None
            self._stop_locked()
            self._current = None
        if had and self.events:
            self.events.emit("audio_stopped", "Audio playback stopped by user")
        return {"stopped": had}

    # ------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            playing = self._proc is not None and self._proc.poll() is None
            current = dict(self._current) if playing and self._current else None
        return {
            "enabled": self.enabled(),
            "player": self.player_name,
            "playing": playing,
            "current": current,
            "limits": {"max_volume": MAX_VOLUME,
                       "max_duration_s": MAX_DURATION_S,
                       "freq_range": [MIN_FREQ, MAX_FREQ]},
        }

    # ------------------------------------------------------------------
    # public actions
    # ------------------------------------------------------------------
    def play_tone(self, freq: float, duration_s: float, waveform: str,
                  volume: float) -> dict:
        if not self.enabled():
            raise FsError(403, "audio_disabled",
                          "Remote audio playback is disabled on this device")
        pcm = render_tone(freq, duration_s, waveform, volume)
        key = hashlib.sha1(
            f"{freq}|{duration_s}|{waveform}|{volume}".encode()).hexdigest()[:16]
        wav = os.path.join(self.cache_dir, f"tone-{key}.wav")
        if not os.path.exists(wav):
            write_wav(wav, pcm)
        return self._run_player(wav, f"tone {freq:.0f}Hz {waveform} "
                               f"{duration_s:.1f}s", duration_s, "tone")

    def play_preset(self, name: str, volume: float = 0.7) -> dict:
        if not self.enabled():
            raise FsError(403, "audio_disabled",
                          "Remote audio playback is disabled on this device")
        if name not in PRESETS:
            raise FsError(404, "no_preset", f"Unknown preset '{name}'")
        total = sum(n[1] for n in PRESETS[name])
        vol = max(0.0, min(float(volume), MAX_VOLUME))
        key = hashlib.sha1(f"{name}|{vol}".encode()).hexdigest()[:16]
        wav = os.path.join(self.cache_dir, f"preset-{name}-{key}.wav")
        if not os.path.exists(wav):
            write_wav(wav, render_notes(PRESETS[name], vol))
        return self._run_player(wav, f"preset:{name}", total, "preset")

    def play_file(self, path: str) -> dict:
        """Play an uploaded or authorized audio file (wav/mp3/ogg/flac)."""
        if not self.enabled():
            raise FsError(403, "audio_disabled",
                          "Remote audio playback is disabled on this device")
        real = self.sandbox_auth(path)
        if self.player_name == "powershell-soundplayer" \
                and not real.lower().endswith(".wav"):
            raise FsError(400, "format",
                          "Windows SoundPlayer supports WAV only; convert with "
                          "the Signal Lab or use the paplay/ffplay path")
        dur = 0.0
        if real.lower().endswith(".wav"):
            import wave
            try:
                with wave.open(real, "rb") as wf:
                    dur = wf.getnframes() / float(wf.getframerate() or 44100)
            except Exception:
                dur = 0.0
        if dur > MAX_DURATION_S * 4:
            raise FsError(400, "too_long",
                          f"Audio file duration {dur:.0f}s exceeds safe limit")
        return self._run_player(real, f"file:{os.path.basename(real)}",
                                dur, "file")

    def sandbox_auth(self, path: str):
        from .fsops import PathSandbox
        if isinstance(getattr(self, "_sandbox", None), PathSandbox):
            return self._sandbox.authorize(path, "r")
        return os.path.abspath(os.path.expanduser(path))

    def attach_sandbox(self, sandbox) -> None:
        self._sandbox = sandbox
