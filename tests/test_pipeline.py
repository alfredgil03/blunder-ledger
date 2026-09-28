"""Batch selection, practice calibration, puzzles, openings, and rendering,
all against the sample data in examples/ (no engine, no network)."""
import json
import os
import re
import shutil
import subprocess
import sys

import chess
import pytest

import practice
import review
from conftest import EXAMPLES, REPO

SCRIPTS = os.path.join(REPO, "scripts")


def run(script, *args):
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args],
                          capture_output=True, text=True, check=True).stdout


def test_latest_batch_is_ranked_by_game_time_not_filename(tmp_path):
    for name in ("batch-1.json", "batch-2.json"):
        shutil.copy(os.path.join(EXAMPLES, name), tmp_path / name)
    newest = max(g["end_time"] for g in json.load(open(tmp_path / "batch-2.json"))["games"])
    os.utime(tmp_path / "batch-2.json", (0, 0))  # oldest file on disk
    for finder in (review.find_latest_batch, practice.find_latest_batch):
        path, data = finder(str(tmp_path))
        assert os.path.basename(path) == "batch-2.json"
        assert max(g["end_time"] for g in data["games"]) == newest


def test_latest_batch_ignores_non_batch_json(tmp_path):
    (tmp_path / "notes.json").write_text('{"title": "x"}')
    (tmp_path / "broken.json").write_text("{not json")
    assert review.find_latest_batch(str(tmp_path)) == (None, None)
    assert review.find_latest_batch(None) == (None, None)


def test_rolling_average_matches_raw_data():
    _, data = review.find_latest_batch(EXAMPLES)
    r = review.rolling_averages(data)
    games = data["games"]
    blunders = sum(a.get("tag") == "blunder" for g in games for a in g["annotations"])
    assert r["n_games"] == len(games)
    assert r["blunders_per_game"] == round(blunders / len(games), 2)


def test_elo_bands_cover_every_rating():
    labels = [practice.band_for(e)["label"] for e in (0, 300, 400, 550, 800, 3000)]
    assert all(labels)
    assert practice.band_for(100)["depth"] <= practice.band_for(3000)["depth"]


def test_material_balance():
    assert practice.material_balance(chess.STARTING_FEN, "white") == 0
    assert practice.material_balance("4k3/8/8/8/8/8/8/3QK3 w - - 0 1", "white") == 9
    assert practice.material_balance("4k3/8/8/8/8/8/8/3QK3 w - - 0 1", "black") == -9


def test_puzzles_are_solvable_and_competitive():
    puzzles, meta = practice.find_week_puzzles(EXAMPLES)
    assert 0 < len(puzzles) <= practice.REVIEW_LIMIT and meta["count"] == len(puzzles)
    for p in puzzles:
        board = chess.Board(p["fen"])
        board.parse_san(p["best"])  # raises if the answer is not legal in the position
        assert p["best"] != p["played"]
        assert abs(practice.material_balance(p["fen"], p["color"])) <= 5
        assert p["url"].startswith("https://www.chess.com/game/")
    assert [p["end_time"] for p in puzzles] == sorted(p["end_time"] for p in puzzles)


def test_explain_best_names_a_free_piece():
    out = practice.explain_best("4k3/8/8/3q4/8/2N5/8/4K3 w - - 0 1", "Nxd5")
    assert "queen" in " ".join(out["why"]).lower()


def test_opening_lookup():
    names = review.load_opening_names()
    hit = review.opening_for(["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3"], names)
    assert hit and "Italian" in hit["name"]
    flags = review.book_flags(["e4", "e5", "Qh5", "Ke7", "Qxe5#"], review.load_book_prefixes())
    assert flags[0] and flags[1] and not flags[-1]
    assert flags == sorted(flags, reverse=True)  # once out of book, never back in


def no_placeholders(html):
    left = re.findall(r"__[A-Z][A-Z_]+__", html)
    assert not left, f"unfilled placeholders: {sorted(set(left))}"


def test_ledger_renders_from_examples(tmp_path):
    out = tmp_path / "ledger.html"
    run("bulk_review.py", "--from-json", os.path.join(EXAMPLES, "batch-2.json"),
        "--previous", os.path.join(EXAMPLES, "batch-1.json"), "--username", "sample", "--out", str(out))
    html = out.read_text()
    no_placeholders(html)
    owner = json.load(open(os.path.join(EXAMPLES, "batch-2.json")))["username"]
    assert f"{owner}@chess.com" in html  # the page names the account the data belongs to


def test_practice_page_builds_from_examples(tmp_path):
    out = tmp_path / "practice.html"
    summary = json.loads(run("practice.py", "--data-dir", EXAMPLES, "--username", "sample", "--out", str(out)))
    html = out.read_text()
    no_placeholders(html)
    assert summary["batch_source"] == "batch-2.json"
    assert "sample@chess.com:~/spar$" in html


def test_review_renders_without_optional_extras(tmp_path):
    out = tmp_path / "review.html"
    run("review.py", "--from-json", os.path.join(EXAMPLES, "game-review.json"), "--username", "sample",
        "--no-h2h", "--out", str(out))
    html = out.read_text()
    no_placeholders(html)
    assert 'class="practice-cta"' not in html  # no --practice-url, so no link


def test_no_personal_data_in_code():
    """Code must stay generic: no sample-account username, home paths, or emails."""
    owner = json.load(open(os.path.join(EXAMPLES, "batch-2.json")))["username"]
    pattern = re.compile(re.escape(owner) + r"|/Users/|/home/|[\w.]+@(?!chess\.com)[\w-]+\.(?:com|edu|org)\b", re.I)
    for folder in ("scripts", "templates", "tests"):
        for root, _, files in os.walk(os.path.join(REPO, folder)):
            for f in files:
                if f.endswith((".py", ".html", ".js")) and "vendor" not in root and f != os.path.basename(__file__):
                    text = open(os.path.join(root, f), encoding="utf-8").read()
                    assert not pattern.search(text), f"personal string in {f}: {pattern.search(text).group(0)}"


def test_pipeline_commands_run_on_sample_data(tmp_path):
    """blunder.py itself: every no-engine step, end to end, on the sample data."""
    for step in ("report", "practice"):
        run("blunder.py", step, "--data-dir", EXAMPLES, "--out-dir", str(tmp_path))
    assert {p.name for p in tmp_path.iterdir()} >= {"ledger.html", "practice.html"}
    help_text = run("blunder.py", "--help")
    for step in ("analyze", "report", "practice", "review", "all"):
        assert step in help_text
    assert "findings" not in help_text
