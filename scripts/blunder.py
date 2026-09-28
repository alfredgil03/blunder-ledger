#!/usr/bin/env python3
"""
One entry point for the three tools: analyze your games, build the report,
build the practice bot, and review a single game. Each step calls the
script that does the work, so any of them can still be run
on its own.

  blunder.py analyze  --username <you>              # engine pass on your last 50 games (plus the 50 before)
  blunder.py report                                 # ledger dashboard from the two newest batches
  blunder.py practice                               # playable bot tuned to the newest batch
  blunder.py practice --with-reviews --username <you>   # ...and a full review page for each puzzle's game
  blunder.py review   --username <you> [--url URL]  # per-move review of one game (default: most recent)
  blunder.py all      --username <you>              # everything, in order

Batches are saved to --data-dir (default ./data); pages go to --out-dir
(default ./out). Open out/ledger.html, out/practice.html, out/review.html.
Try it without an account or engine: blunder.py report --data-dir examples
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def run(script, *args):
    cmd = [sys.executable, os.path.join(HERE, script), *[str(a) for a in args]]
    print("$ " + " ".join(os.path.basename(c) if i < 2 else c for i, c in enumerate(cmd)), flush=True)
    subprocess.run(cmd, check=True)


def batches(data_dir):
    """Every bulk_review.py batch in data_dir, newest games first."""
    found = []
    if os.path.isdir(data_dir):
        for fn in os.listdir(data_dir):
            path = os.path.join(data_dir, fn)
            if not fn.endswith(".json"):
                continue
            try:
                d = json.load(open(path))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            games = d.get("games") if isinstance(d, dict) else None
            if games and "annotations" in games[0]:
                found.append((max(g.get("end_time") or 0 for g in games), path))
    return [p for _, p in sorted(found, reverse=True)]


def cmd_analyze(a):
    os.makedirs(a.data_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    common = ["--username", a.username, "--count", a.count, "--time-class", a.time_class, "--depth", a.depth]
    if a.stockfish:
        common += ["--stockfish", a.stockfish]
    run("bulk_review.py", *common, "--out", os.path.join(a.data_dir, f"batch-{stamp}-last{a.count}.json"))
    if not a.no_previous:
        run("bulk_review.py", *common, "--offset", a.count, "--skip-elo-history",
            "--out", os.path.join(a.data_dir, f"batch-{stamp}-prev{a.count}.json"))


def cmd_report(a):
    found = batches(a.data_dir)
    if not found:
        raise SystemExit(f"no batches in {a.data_dir}; run `blunder.py analyze` first (or use --data-dir examples)")
    os.makedirs(a.out_dir, exist_ok=True)
    args = ["--from-json", found[0], "--out", os.path.join(a.out_dir, "ledger.html")]
    if len(found) > 1:
        args += ["--previous", found[1]]
    if a.tz:
        args += ["--tz", a.tz]
    run("bulk_review.py", *args)


def cmd_practice(a):
    os.makedirs(a.out_dir, exist_ok=True)
    if getattr(a, "with_reviews", False):
        review_puzzle_games(a)
    args = ["--data-dir", a.data_dir, "--out", os.path.join(a.out_dir, "practice.html")]
    if getattr(a, "username", None):
        args += ["--username", a.username]
    run("practice.py", *args)


def review_puzzle_games(a):
    """A full review page for every game a practice puzzle comes from, so each
    puzzle can link to it. Skips games already reviewed; about 15s per game."""
    if not a.username:
        raise SystemExit("--with-reviews needs --username (the side to review from)")
    sys.path.insert(0, HERE)
    import practice
    puzzles, _ = practice.find_week_puzzles(a.data_dir)
    urls = sorted({p["url"] for p in puzzles if p.get("url")})
    for i, url in enumerate(urls, 1):
        page = practice.review_page_name(url)
        if not page or os.path.exists(os.path.join(a.out_dir, page)):
            continue
        print(f"[{i}/{len(urls)}] reviewing {url}", flush=True)
        args = ["--username", a.username, "--url", url, "--no-h2h", "--data-dir", a.data_dir,
                "--practice-url", "practice.html", "--out", os.path.join(a.out_dir, page)]
        if a.stockfish:
            args += ["--stockfish", a.stockfish]
        run("review.py", *args)


def cmd_review(a):
    os.makedirs(a.out_dir, exist_ok=True)
    args = ["--username", a.username, "--data-dir", a.data_dir, "--out", os.path.join(a.out_dir, "review.html"),
            "--json", os.path.join(a.out_dir, "review.json")]
    if a.url:
        args += ["--url", a.url]
    if a.stockfish:
        args += ["--stockfish", a.stockfish]
    if os.path.exists(os.path.join(a.out_dir, "practice.html")):
        args += ["--practice-url", "practice.html"]  # same folder, so a relative link works
    run("review.py", *args)


def cmd_all(a):
    cmd_analyze(a)
    cmd_report(a)
    a.with_reviews = False
    cmd_practice(a)
    cmd_review(a)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, user=False, engine=False):
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        p.add_argument("--data-dir", default="data", help="where batches are saved and read (default ./data)")
        p.add_argument("--out-dir", default="out", help="where pages are written (default ./out)")
        p.add_argument("--username", required=user, help="your chess.com username")
        if engine:
            p.add_argument("--stockfish", help="path to Stockfish (default: STOCKFISH_PATH, then PATH)")
        return p

    for name, fn in (("analyze", cmd_analyze), ("all", cmd_all)):
        p = add(name, fn, user=True, engine=True)
        p.add_argument("--count", type=int, default=50, help="games per batch (default 50)")
        p.add_argument("--time-class", default="rapid", help="rapid, blitz, bullet, or daily (default rapid)")
        p.add_argument("--depth", type=int, default=12, help="engine depth per position (default 12)")
        p.add_argument("--no-previous", action="store_true", help="skip the batch before (no comparison switcher)")
        p.add_argument("--tz", help="IANA time zone for the report (default UTC)")
        p.add_argument("--url", help="game to review (default: most recent)")
    add("report", cmd_report).add_argument("--tz", help="IANA time zone for dates (default UTC)")
    p = add("practice", cmd_practice, engine=True)
    p.add_argument("--with-reviews", action="store_true",
                   help="also review every puzzle's source game so each puzzle links to it (needs --username, Stockfish)")
    add("review", cmd_review, user=True, engine=True).add_argument("--url", help="chess.com game URL (default: most recent)")

    a = ap.parse_args()
    try:
        a.fn(a)
    except subprocess.CalledProcessError as e:
        raise SystemExit(e.returncode)


if __name__ == "__main__":
    main()
