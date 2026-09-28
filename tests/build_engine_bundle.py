"""Builds one plain-JS file (chess.js + the practice bot's engine + the tests)
that runs under any JavaScript engine: `node bundle.js`, or on macOS
`osascript -l JavaScript bundle.js` (JavaScriptCore, no Node needed).

The engine lives inside the practice page's script, so this extracts it from
a page built by scripts/practice.py, with the real calibration constants."""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def extract_engine(page_html):
    m = re.search(r"<script>\s*\(function\(\)\{\s*\"use strict\";(.*?)\n  // ---- session history", page_html, re.S)
    if not m:
        raise RuntimeError("engine block not found in the practice page; did the template markers change?")
    engine = m.group(1)
    h = re.search(r"(  function rate\(.*?)\n  var TYPE_LABEL", page_html, re.S)
    if not h:
        raise RuntimeError("history helpers not found in the practice page")
    return engine, h.group(1)


def build_bundle(page_html):
    engine, history = extract_engine(page_html)
    chess_js = open(os.path.join(HERE, "vendor", "chess.min.js")).read()
    tests = open(os.path.join(HERE, "js", "engine_tests.js")).read()
    return "\n".join([
        chess_js,
        "var __api = (function () {",
        engine,
        history,
        "  return { state: state, computeBotMove: computeBotMove, findHangingPiece: findHangingPiece,",
        "           findFork: findFork, findPin: findPin, findDiscoveredAttack: findDiscoveredAttack,",
        "           evalPosition: evalPosition, summarizeHistory: summarizeHistory, TUNING: TUNING, BANDS: BANDS };",
        "})();",
        tests,
    ])
