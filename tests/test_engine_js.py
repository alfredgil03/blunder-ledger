"""Runs the practice bot's JS engine checks (tests/js/engine_tests.js) against
the real page built from examples/. Uses node when installed, else macOS's
built-in JavaScriptCore via osascript; skips when neither is available."""
import json
import os
import shutil
import subprocess
import sys

import pytest

from build_engine_bundle import build_bundle
from conftest import EXAMPLES, REPO


def js_runner():
    if shutil.which("node"):
        return ["node"]
    if sys.platform == "darwin" and shutil.which("osascript"):
        return ["osascript", "-l", "JavaScript"]
    return None


@pytest.mark.skipif(js_runner() is None, reason="needs node or macOS osascript")
def test_practice_engine(tmp_path):
    page = tmp_path / "practice.html"
    subprocess.run([sys.executable, os.path.join(REPO, "scripts", "practice.py"), "--data-dir", EXAMPLES,
                    "--out", str(page)], check=True, capture_output=True)
    bundle = tmp_path / "bundle.js"
    bundle.write_text(build_bundle(page.read_text()))
    proc = subprocess.run(js_runner() + [str(bundle)], capture_output=True, text=True, timeout=300)
    lines = [l for l in proc.stdout.strip().splitlines() if l.startswith("{")]
    assert lines, proc.stdout + proc.stderr
    result = json.loads(lines[-1])
    assert result["failed"] == 0, result["failures"]
    assert result["passed"] >= 20
