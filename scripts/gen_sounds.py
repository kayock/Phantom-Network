#!/usr/bin/env python3
"""Generate the original PHANTOM NETWORK sound preset WAVs.

Mirrors companion/audio.py PRESETS exactly, so the shipped WAVs in
sounds/ are byte-for-byte what the companion would synthesize.  Runs as
part of packaging (see BUILD.md) and requires only the Python stdlib.
"""

import os
import sys
import wave

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)
import companion.audio as _audio  # noqa: E402

PRESETS = _audio.PRESETS
SAMPLE_RATE = _audio.SAMPLE_RATE
render_notes = _audio.render_notes
write_wav = _audio.write_wav


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for name, notes in PRESETS.items():
        path = os.path.join(out_dir, f"pn-{name}.wav")
        write_wav(path, render_notes(notes, 0.8))
        size = os.path.getsize(path)
        dur = sum(n[1] for n in notes)
        print(f"  {path}  ({size} bytes, {dur:.2f}s)")
    print(f"done: {len(PRESETS)} original presets in {out_dir}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "..", "sounds")
    main(os.path.abspath(out))
