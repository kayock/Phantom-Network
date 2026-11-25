"""Pytest wrapper: runs the real Signal-Laboratory DSP test suite in node."""
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_js_dsp_suite():
    r = subprocess.run([sys.executable if False else "node",
                        os.path.join(ROOT, "tests", "js", "test_dsp.mjs")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"JS DSP tests failed:\n{r.stdout}\n{r.stderr}"
    assert "0 failed" in r.stdout
