#!/usr/bin/env python3
"""
The Blunder Ledger: engine review across a batch of chess.com games (50 at a
time by default), rendered as an interactive dashboard. Where review.py does
one game in full per-move detail, this trades per-move granularity (single
PV, lower depth) for speed across a batch. What matters here is the aggregate
pattern: blunder rate, hung-material share, phase breakdown,
checkmate-loss rate, rating trend, color and opponent stats. One script:
fetch, analyze, classify, render.

Usage:
  bulk_review.py --username <you> --count 50 --out ledger.html
  bulk_review.py --username <you> --count 50 --time-class rapid --json batch2.json --out ledger.html
  bulk_review.py --username <you> --count 50 --offset 50 --out batch1.json      # the 50 before that, raw JSON
  bulk_review.py --from-json batch2.json --previous batch1.json --out ledger.html
      (re-render without the engine; --previous turns on the page's
       "Last 50 / Previous 50 / Both" switcher so every stat can be compared)

notes.json (optional, --notes) holds the hand-written parts of a report:
  {
    "title": "Blunder Ledger",
    "subtitle": "last 100 rapid games, Aug 16 to Sep 15, 2026",
    "practice_intro": "...",
    "practice": [{"title": "...", "why": "..."}]
  }
Every field is optional. Sections with nothing to show are left out.
("examples"/"examples_intro" — the hand-curated "A few from your own games"
cards — retired 2026-09-17. Repeat motif detection replaced it and needs no
hand-written notes; it's computed live in the page from GAMES.)
"""
import argparse
import datetime as dt
import html as htmlmod
import io
import json
import math
import os
import re
import shutil
from zoneinfo import ZoneInfo

import chess
import chess.engine
import chess.pgn
import requests
from motifs import find_motifs

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(HERE, "..", "templates", "ledger.html")
USER_AGENT = "blunder-ledger/1.0 ({})".format(os.environ.get("CHESSCOM_CONTACT", "https://github.com/blunder-ledger"))
MATE_SCORE = 100000
DRAW_RESULTS = {"agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"}


# ----------------------------------------------------------------------------
# chess.com API + engine discovery
# ----------------------------------------------------------------------------
def api_get(url):
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_archives(username):
    return api_get(f"https://api.chess.com/pub/player/{username}/games/archives")["archives"]


def fetch_all_finished_games(username):
    """Every finished game on the account, oldest first."""
    games = []
    for url in fetch_archives(username):
        games.extend(g for g in api_get(url)["games"] if "pgn" in g)
    games.sort(key=lambda g: g["end_time"])
    return games


def find_stockfish(explicit=None):
    for candidate in (explicit, os.environ.get("STOCKFISH_PATH"), shutil.which("stockfish"),
                      "/opt/homebrew/bin/stockfish", "/usr/local/bin/stockfish", "/usr/bin/stockfish"):
        if candidate and os.path.exists(candidate):
            return candidate
    raise SystemExit("Stockfish not found. Install it (brew install stockfish) or pass --stockfish /path/to/stockfish.")


# ----------------------------------------------------------------------------
# formulas (see docs/methodology.md)
# ----------------------------------------------------------------------------
def win_percent(cp):
    """Centipawns -> win probability (%), the logistic mapping Lichess uses."""
    cp = max(-1000, min(1000, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def move_accuracy(wp_before, wp_after, cpl=0):
    """Per-move accuracy from the win-probability drop (Lichess formula), with
    a centipawn-loss floor once the position is already decided. Ported from
    review.py (kept in sync):
    below 10% or above 90% win probability there's too little probability
    room left for a wp-diff to register a real blunder (a move hanging mate
    at 2.5% win chance still showed ~100% "accuracy" under the old formula),
    so that band scores from raw centipawn loss instead (capped at 500) and
    takes the harsher of the two. The 10-90% band is unchanged."""
    diff = max(0.0, wp_before - wp_after)
    wp_acc = max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * diff) - 3.1668))
    if 10 < wp_before < 90:
        return wp_acc
    cpl_acc = max(0.0, 100.0 - cpl / 5.0)
    return min(wp_acc, cpl_acc)


def outcome_of(chess_com_result):
    if chess_com_result == "win":
        return "win"
    if chess_com_result in DRAW_RESULTS:
        return "draw"
    return "loss"


def phase_of(board, fullmove):
    """Opening = first 10 moves. Endgame = six or fewer non-pawn, non-king
    pieces left on the board (both sides combined). Otherwise middlegame."""
    if fullmove <= 10:
        return "opening"
    non_pawn_king = sum(
        len(board.pieces(pt, chess.WHITE)) + len(board.pieces(pt, chess.BLACK))
        for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
    )
    return "endgame" if non_pawn_king <= 6 else "middlegame"


_SAC_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}


def looks_like_sac(board_before, move, mover_white):
    """Ported from review.py (kept in sync — same rough
    heuristic, same false-positive fix from 2026-09-15/16): the moved piece
    lands on a square where the cheapest LEGAL recapture is strictly cheaper
    than the piece offered. Used here only as one ingredient of the "forcing
    move" style signal (see update_opponent_caches), not for a Brilliant-style
    tag — this script doesn't classify the opponent's moves at that level of
    detail, on purpose, to stay fast across a whole batch."""
    piece = board_before.piece_at(move.from_square)
    if piece is None or piece.piece_type in (chess.PAWN, chess.KING):
        return False
    captured = board_before.piece_at(move.to_square)
    gained = _SAC_VALUES.get(captured.piece_type, 0) if captured else 0
    board_after = board_before.copy()
    board_after.push(move)
    attackers = [sq for sq in board_after.attackers(not mover_white, move.to_square)
                 if board_after.is_legal(chess.Move(sq, move.to_square))]
    if not attackers:
        return False
    cheapest_attacker = min(_SAC_VALUES.get(board_after.piece_at(sq).piece_type, 9) for sq in attackers)
    return cheapest_attacker < _SAC_VALUES[piece.piece_type] - gained

# ----------------------------------------------------------------------------
# analysis
# ----------------------------------------------------------------------------
def fetch_elo_history(username, time_class="rapid"):
    """Cheap, header-only pull across the whole account (no engine). chess.com
    keeps a separate rating per time class, so this filters to one rather
    than mixing incomparable ratings."""
    rows = []
    for url in fetch_archives(username):
        for g in api_get(url)["games"]:
            if "pgn" not in g or g.get("time_class") != time_class:
                continue
            white, black = g["white"], g["black"]
            my_color = "white" if white["username"].lower() == username.lower() else "black"
            me, opp = (white, black) if my_color == "white" else (black, white)
            rows.append({
                "date": dt.datetime.fromtimestamp(g["end_time"], dt.UTC).strftime("%Y-%m-%d"),
                "end_time": g["end_time"],
                "my_elo": me.get("rating"), "opp_elo": opp.get("rating"),
                "my_color": my_color, "opp_name": opp["username"],
                "outcome": outcome_of(me["result"]),
                "diff": me.get("rating", 0) - opp.get("rating", 0),
                "url": g.get("url"),
            })
    rows.sort(key=lambda r: r["end_time"])
    return rows


def analyze_batch(games, username, engine, depth):
    results = []
    for gi, g in enumerate(games):
        game = chess.pgn.read_game(io.StringIO(g["pgn"]))
        if game is None:
            continue
        headers = game.headers
        my_color = chess.WHITE if headers.get("White", "").lower() == username.lower() else chess.BLACK
        me_side = g["white"] if my_color == chess.WHITE else g["black"]

        board = game.board()
        node = game
        positions, moves_san, moves = [board.copy()], [], []
        while node.variations:
            nxt = node.variations[0]
            moves_san.append(board.san(nxt.move))
            moves.append(nxt.move)
            board.push(nxt.move)
            positions.append(board.copy())
            node = nxt

        evals_white, pv_best = [], []
        for pos in positions:
            info = engine.analyse(pos, chess.engine.Limit(depth=depth))
            evals_white.append(info["score"].white().score(mate_score=MATE_SCORE))
            pv = info.get("pv")
            pv_best.append(pv[0] if pv else None)

        record = {
            "index": gi,
            "date": headers.get("Date"),
            "end_time": g.get("end_time"),
            "url": g.get("url"),
            "time_class": g.get("time_class"),
            "my_color": "white" if my_color == chess.WHITE else "black",
            "opp_name": headers.get("Black") if my_color == chess.WHITE else headers.get("White"),
            "my_elo": headers.get("WhiteElo") if my_color == chess.WHITE else headers.get("BlackElo"),
            "opp_elo": headers.get("BlackElo") if my_color == chess.WHITE else headers.get("WhiteElo"),
            "result": headers.get("Result"),
            "outcome": outcome_of(me_side.get("result")),
            "termination": headers.get("Termination", ""),
            "eco": headers.get("ECO", ""),
            "num_moves": len(moves_san),
            # phase_counts and my_move_count cover EVERY move this player made,
            # not just flagged ones: the denominator for blunder rate and for
            # the phase-quality bars.
            "phase_counts": {"opening": 0, "middlegame": 0, "endgame": 0},
            "my_move_count": 0,
            "avg_accuracy": None,
            "annotations": [],
        }
        accuracies = []
        opp_forcing, opp_total = 0, 0

        for i in range(len(moves_san)):
            mover_is_white = (i % 2 == 0)
            if mover_is_white != (my_color == chess.WHITE):
                # Opponent's move — style signal only, for the Repeat
                # Opponents section's forcing-move rate. No new engine call:
                # positions[i] and moves[i] already exist from the loops
                # above, which ran for every ply regardless of side.
                opp_total += 1
                mv = moves[i]
                if positions[i].gives_check(mv) or looks_like_sac(positions[i], mv, mover_is_white):
                    opp_forcing += 1
                continue
            before, after = positions[i], positions[i + 1]
            fullmove = before.fullmove_number
            ph = phase_of(before, fullmove)
            ew_before, ew_after = evals_white[i], evals_white[i + 1]
            cpl = max(0, ew_before - ew_after) if mover_is_white else max(0, ew_after - ew_before)

            record["phase_counts"][ph] += 1
            record["my_move_count"] += 1
            my_before = ew_before if mover_is_white else -ew_before
            my_after = ew_after if mover_is_white else -ew_after
            accuracies.append(move_accuracy(win_percent(my_before), win_percent(my_after), cpl))

            if cpl >= 200:
                tag = "blunder"
            elif cpl >= 100:
                tag = "mistake"
            elif cpl >= 50:
                tag = "inaccuracy"
            else:
                continue

            # The engine's best reply to the move just played. A capture means
            # the move most likely hung material outright.
            punish_san = None
            pb = pv_best[i + 1]
            if pb is not None:
                try:
                    punish_san = after.san(pb)
                except Exception:
                    pass
            best_san = None
            best_motifs = []
            if pv_best[i] is not None:
                try:
                    best_san = before.san(pv_best[i])
                    # What tactical pattern the engine's suggestion would have
                    # created, had it been played — not what actually happened.
                    # Zero new engine cost (find_motifs is pure board mechanics
                    # on positions already computed above); added 2026-09-21
                    # specifically so pin/fork/discovered-attack frequency can
                    # finally be measured in aggregate, not just per-game.
                    best_motifs = find_motifs(before, pv_best[i])
                except Exception:
                    pass

            # The other side of the same coin (added 2026-09-24): what the
            # opponent's best reply to the move just played would have
            # created. best_motifs above is what you MISSED; this is what you
            # ALLOWED. Same find_motifs, same positions already in memory.
            allowed_motifs = []
            if pb is not None:
                try:
                    allowed_motifs = find_motifs(after, pb)
                except Exception:
                    pass

            annotation = {
                "ply": i, "fullmove": fullmove, "phase": ph,
                "played": moves_san[i], "best": best_san, "cpl": cpl, "tag": tag,
                "eval_before_white": ew_before, "eval_after_white": ew_after,
                "opp_punish": punish_san, "fen_before": before.fen(),
                "best_motifs": best_motifs, "allowed_motifs": allowed_motifs,
            }
            if tag == "blunder":
                # "mate_bound" is an approximation: the eval right after the
                # blunder already sits in forced-mate range. Not a hand-verified
                # mating line.
                if punish_san and "x" in punish_san:
                    annotation["blunder_type"] = "hung_material"
                elif abs(ew_after) > 5000:
                    annotation["blunder_type"] = "mate_bound"
                else:
                    annotation["blunder_type"] = "other"
            record["annotations"].append(annotation)

        if accuracies:
            record["avg_accuracy"] = round(sum(accuracies) / len(accuracies), 1)
        record["opp_style"] = {"forcing_moves": opp_forcing, "total_moves": opp_total}
        results.append(record)
    return results


# ----------------------------------------------------------------------------
# render
# ----------------------------------------------------------------------------
def esc(s):
    return htmlmod.escape(str(s or ""))


def outcome_from(record):
    if record.get("outcome"):
        return record["outcome"]
    res, color = record.get("result"), record.get("my_color")
    if res == "1-0":
        return "win" if color == "white" else "loss"
    if res == "0-1":
        return "win" if color == "black" else "loss"
    return "draw"


def motifs_for(annotation):
    """best_motifs from the annotation if the batch already has it; otherwise
    recomputed from fen_before + best SAN (pure board mechanics, no engine),
    since batches saved before 2026-09-21 predate the field."""
    if annotation.get("best_motifs") is not None:
        return annotation["best_motifs"]
    if not annotation.get("fen_before") or not annotation.get("best"):
        return []
    try:
        board = chess.Board(annotation["fen_before"])
        return find_motifs(board, board.parse_san(annotation["best"]))
    except Exception:
        return []


def allowed_motifs_for(annotation):
    """allowed_motifs from the annotation if the batch already has it;
    otherwise rebuilt from fen_before + the move played + the opponent's saved
    best reply (opp_punish). Pure board mechanics, no engine, so every batch
    saved before 2026-09-24 gets it on re-render. This is the "what you
    allowed" counterpart to motifs_for(), which is "what you missed"."""
    if annotation.get("allowed_motifs") is not None:
        return annotation["allowed_motifs"]
    if not (annotation.get("fen_before") and annotation.get("played") and annotation.get("opp_punish")):
        return []
    try:
        board = chess.Board(annotation["fen_before"])
        board.push_san(annotation["played"])
        return find_motifs(board, board.parse_san(annotation["opp_punish"]))
    except Exception:
        return []


def to_game_row(record, batch, tz):
    if record.get("end_time"):
        when = dt.datetime.fromtimestamp(record["end_time"], dt.UTC).astimezone(tz).strftime("%Y-%m-%d %H:%M")
    else:
        when = (record.get("date") or "").replace(".", "-") + " 00:00"
    return {
        "dt": when,
        "color": record["my_color"],
        "my_elo": int(record["my_elo"]) if record.get("my_elo") else None,
        "opp_elo": int(record["opp_elo"]) if record.get("opp_elo") else None,
        "opp_name": record.get("opp_name"),
        "outcome": outcome_from(record),
        "url": record.get("url") or "#",
        "batch": batch,
        "termination": record.get("termination", ""),
        "eco": record.get("eco", ""),
        "my_move_count": record.get("my_move_count", 0),
        "phase_counts": record.get("phase_counts", {"opening": 0, "middlegame": 0, "endgame": 0}),
        "avg_accuracy": record.get("avg_accuracy"),
        "opp_style": record.get("opp_style", {"forcing_moves": 0, "total_moves": 0}),
        "annotations": [
            dict({k: a.get(k) for k in ("fullmove", "played", "best", "phase", "tag", "cpl", "opp_punish", "blunder_type")},
                 best_motifs=motifs_for(a), allowed_motifs=allowed_motifs_for(a))
            for a in record.get("annotations", [])
        ],
    }


def opponent_cache_path(cache_dir, opp_name):
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", opp_name or "unknown").strip("_") or "unknown"
    return os.path.join(cache_dir, f"{safe}.json")


def load_opponent_cache(cache_dir, opp_name):
    path = opponent_cache_path(cache_dir, opp_name)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "opp_name": opp_name, "games": [],
        "totals": {"w": 0, "l": 0, "d": 0, "opp_forcing_moves": 0, "opp_total_moves": 0},
        "last_updated": None,
    }


def save_opponent_cache(cache_dir, cache):
    os.makedirs(cache_dir, exist_ok=True)
    path = opponent_cache_path(cache_dir, cache["opp_name"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def update_opponent_caches(game_rows, cache_dir):
    """Merge this run's games into each opponent's persistent style cache,
    keyed by game URL so re-running on the same batch (--from-json, a
    republish) never double-counts. Deliberately bounded: a game only ever
    enters a cache by being part of some report's analyzed batch — there is
    no separate full-history backfill scan of chess.com here, so a repeat
    opponent's very old games (from before any report ever covered them)
    just aren't counted, rather than triggering a new fetch-and-analyze pass
    outside the normal periodic/on-demand report flow. Keeps this at zero
    added engine time, permanently, at that one known cost.

    Returns {opp_name: {...totals, n_games, forcing_pct}} for every opponent
    who now has 2+ games on file (this batch plus whatever was already
    cached) — the snapshot embedded as OPPONENT_STYLE for Repeat Opponents.
    """
    by_opp = {}
    for g in game_rows:
        by_opp.setdefault(g["opp_name"], []).append(g)
    snapshot = {}
    for opp_name, rows in by_opp.items():
        if not opp_name:
            continue
        cache = load_opponent_cache(cache_dir, opp_name)
        known = set(cache["games"])
        changed = False
        for g in rows:
            if g["url"] in known:
                continue
            changed = True
            known.add(g["url"])
            cache["games"].append(g["url"])
            t = cache["totals"]
            t["w"] += 1 if g["outcome"] == "win" else 0
            t["l"] += 1 if g["outcome"] == "loss" else 0
            t["d"] += 1 if g["outcome"] == "draw" else 0
            t["opp_forcing_moves"] += g["opp_style"]["forcing_moves"]
            t["opp_total_moves"] += g["opp_style"]["total_moves"]
        if changed:
            cache["last_updated"] = dt.date.today().isoformat()
            save_opponent_cache(cache_dir, cache)
        if len(cache["games"]) >= 2:
            t = cache["totals"]
            forcing_pct = round(100 * t["opp_forcing_moves"] / t["opp_total_moves"], 1) if t["opp_total_moves"] else None
            snapshot[opp_name] = dict(t, n_games=len(cache["games"]), forcing_pct=forcing_pct)
    return snapshot


def batch_forcing_baseline(game_rows):
    """Average forcing-move rate across every opponent in this batch — the
    comparison point a single repeat opponent's rate is read against.
    Computed fresh each render from whatever's in the current batch, not
    cached, since it's meant to describe 'your typical opponent right now,'
    not a persistent per-opponent fact."""
    total_forcing = sum(g["opp_style"]["forcing_moves"] for g in game_rows)
    total_moves = sum(g["opp_style"]["total_moves"] for g in game_rows)
    return round(100 * total_forcing / total_moves, 1) if total_moves else None


def curated_html(notes):
    # "examples" (the hand-curated "A few from your own games" cards) is
    # retired as of 2026-09-17 — the user replaced it with Repeat Motif
    # Detection, a built-in section in templates/ledger.html that the page's
    # own script computes live from GAMES (see computeMotifs()/classifyMotif()
    # there), not something this function needs to generate. Only "practice"
    # still gets spliced in here.
    out = []
    practice = notes.get("practice") or []
    if practice:
        items = []
        for i, p in enumerate(practice, 1):
            items.append(f'''
      <div class="plan-item">
        <div class="plan-num">{i}</div>
        <div>
          <div class="plan-title">{esc(p.get("title"))}</div>
          <div class="plan-why">{esc(p.get("why"))}</div>
        </div>
      </div>''')
        out.append(f'''
  <section data-view-content="last50">
    <div class="section-head"><h2>What to actually practice</h2><div class="rule"></div></div>
    <p class="section-note">{esc(notes.get("practice_intro") or "In priority order, based on what is actually costing games in this window.")}</p>
    <div class="plan">{"".join(items)}
    </div>
  </section>''')
    return "\n".join(out)


def render(newest, previous, batch_no, notes, title, subtitle, tz, template_path, out_path, opponent_cache_dir=None):
    games = [to_game_row(g, batch_no, tz) for g in newest["games"]]
    if previous:
        games = [to_game_row(g, batch_no - 1, tz) for g in previous["games"]] + games
    games.sort(key=lambda g: g["dt"])

    # Repeat-opponent style tracking (2026-09-17) — opt-in via
    # --opponent-cache-dir, since the public-repo copy of this script
    # shouldn't assume a fixed path. When omitted, OPPONENT_STYLE ships
    # empty and the page's own mechanical fallback checks handle it.
    opponent_style_payload = {"baseline_forcing_pct": None, "opponents": {}}
    if opponent_cache_dir:
        opponent_style_payload = {
            "baseline_forcing_pct": batch_forcing_baseline(games),
            "opponents": update_opponent_caches(games, opponent_cache_dir),
        }

    username = newest.get("username") or "player"
    time_class = newest.get("time_class")
    title = title or notes.get("title") or "Blunder Ledger"
    subtitle = subtitle or notes.get("subtitle") or (
        f"{len(newest['games'])} games, batch {batch_no}" + (f", {time_class} only" if time_class else ""))
    source = "source: chess.com public game archive" + (f", {time_class} games only" if time_class else "")
    if newest.get("engine", {}).get("depth"):
        source += f" · stockfish depth {newest['engine']['depth']}"

    html = open(template_path, encoding="utf-8").read()
    replacements = {
        "__TITLE__": esc(title),
        "__SUBTITLE__": esc(subtitle),
        "__EYEBROW__": f"cat {re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')}.txt",
        "__PROMPT__": f"{username}@chess.com:~/{time_class or 'games'}$ ",
        "__SOURCE__": esc(source),
        "__GENERATED__": f"Generated {dt.date.today().isoformat()}.",
        "<!--__CURATED__-->": curated_html(notes),
        "/*__GAMES_JSON__*/ []": json.dumps(games),
        '/*__OPPONENT_STYLE_JSON__*/ {"baseline_forcing_pct": null, "opponents": {}}': json.dumps(opponent_style_payload),
    }
    for k, v in replacements.items():
        if k not in html:
            raise SystemExit(f"template is missing marker {k}")
        html = html.replace(k, v)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return len(games)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--username", help="your chess.com username")
    ap.add_argument("--count", type=int, help="analyze the most recent N finished games (default 50)")
    ap.add_argument("--since-count", type=int, help="analyze every finished game after the Nth one (archive order)")
    ap.add_argument("--offset", type=int, default=0, help="with --count: skip the most recent N games first (e.g. --count 50 --offset 50 = the batch before last)")
    ap.add_argument("--time-class", help="only include games of this time class (rapid, blitz, bullet, daily); default: all")
    ap.add_argument("--from-json", help="skip the engine and render from an existing batch JSON")
    ap.add_argument("--previous", help="batch JSON for the batch before this one (turns on the comparison switcher)")
    ap.add_argument("--batch", type=int, help="batch number of the newest batch (default 1, or 2 with --previous)")
    ap.add_argument("--out", required=True, help="output path: .html renders the dashboard, .json writes raw analysis")
    ap.add_argument("--json", help="when rendering HTML, also save the raw batch JSON here")
    ap.add_argument("--notes", help="optional JSON with title/subtitle/examples/practice (see docstring)")
    ap.add_argument("--title", help="page title (overrides notes.title)")
    ap.add_argument("--subtitle", help="one-line scope description (overrides notes.subtitle)")
    ap.add_argument("--tz", default="UTC", help="IANA time zone for the ledger's date/time column (default UTC)")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE)
    ap.add_argument("--depth", type=int, default=12, help="engine search depth per position (default 12)")
    ap.add_argument("--stockfish", help="path to the Stockfish binary (default: STOCKFISH_PATH, then PATH)")
    ap.add_argument("--elo-time-class", default="rapid", help="time class for the full-account rating history (default rapid)")
    ap.add_argument("--skip-elo-history", action="store_true", help="skip the full-account rating pull")
    ap.add_argument("--opponent-cache-dir", help="directory for persistent per-opponent style caches (data/opponents/<name>.json); omit to skip repeat-opponent style tracking (page falls back to its own mechanical checks)")
    args = ap.parse_args()

    if args.from_json:
        newest = json.load(open(args.from_json))
    else:
        if not args.username:
            ap.error("--username is required unless --from-json is given")
        all_games = fetch_all_finished_games(args.username)
        if args.time_class:
            all_games = [g for g in all_games if g.get("time_class") == args.time_class]
        if args.since_count is not None:
            batch = all_games[args.since_count:]
        else:
            count = args.count or 50
            end = len(all_games) - args.offset
            batch = all_games[max(0, end - count):end]
        engine = chess.engine.SimpleEngine.popen_uci(find_stockfish(args.stockfish))
        try:
            results = analyze_batch(batch, args.username, engine, args.depth)
        finally:
            engine.quit()
        newest = {
            "username": args.username,
            "time_class": args.time_class,
            "engine": {"depth": args.depth, "multipv": 1},
            "total_games_now": len(all_games),
            "batch_size": len(batch),
            "games": results,
            "elo_history": None if args.skip_elo_history else fetch_elo_history(args.username, args.elo_time_class),
        }

    if args.out.lower().endswith(".json"):
        with open(args.out, "w") as f:
            json.dump(newest, f, indent=2)
        print(f"wrote {args.out} ({len(newest['games'])} games)")
        return
    if args.json:
        with open(args.json, "w") as f:
            json.dump(newest, f, indent=2)
    previous = json.load(open(args.previous)) if args.previous else None
    notes = json.load(open(args.notes)) if args.notes else {}
    n = render(newest, previous, args.batch or (2 if previous else 1), notes,
               args.title, args.subtitle, ZoneInfo(args.tz), args.template, args.out,
               opponent_cache_dir=args.opponent_cache_dir)
    print(f"wrote {args.out} ({n} games)")


if __name__ == "__main__":
    main()
