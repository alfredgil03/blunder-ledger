#!/usr/bin/env python3
"""
Per-move review of one chess.com game, approximating chess.com's paywalled
Game Review (Brilliant / Great Find / Best / Excellent / Good / Inaccuracy /
Mistake / Miss / Blunder on every move, plus an accuracy score) with a locally
run Stockfish engine. One script: fetch, analyze, classify, render.

Usage:
  review.py --username <you> --out review.html                 # your most recent finished game
  review.py --username <you> --url <chess.com game URL> --out review.html
  review.py --username <you> --pgn-file game.pgn --out review.html
  review.py --username <you> --out review.json                 # raw analysis only
  review.py --from-json review.json --notes notes.json --out review.html   # re-render, no engine

Optional extras on the rendered page:
  --notes notes.json      map of ply number -> one-line explanation, e.g. {"23": "Rxe6 wins the pinned knight."}
  --data-dir DIR          folder of bulk_review.py batch JSON; adds the "vs. your rolling average" strip
  --no-h2h                skip the vs.-this-opponent callout (it makes extra chess.com requests)
  --practice-url URL      adds a link to a practice page (omit to hide it)
"""
import argparse
import datetime as dt
import glob
import io
import json
import math
import os
import re
import shutil
import sys

import chess
import chess.engine
import chess.pgn
import requests
from motifs import find_motifs

DEPTH = 16
MATE_SCORE = 100000
UA = "blunder-ledger/1.0 ({})".format(os.environ.get("CHESSCOM_CONTACT", "https://github.com/blunder-ledger"))
BOOK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openings")


def load_book_prefixes():
    """Every prefix of every known line in Lichess's openings reference
    (lichess-org/chess-openings on GitHub, CC0 public domain, ~3,810 lines across
    a.tsv-e.tsv), as tuples of SAN tokens. A move is "Book" if the exact move
    sequence up to and including it matches one of these prefixes exactly.
    Replicates chess.com's "Book" tag. Known approximation: matches by move order only, not by
    transposed position (reaching the same position via a different move
    order won't register as Book even though chess.com's own book almost
    certainly does), and only as complete as this one reference table —
    rare sidelines chess.com considers book may not appear here. Costs
    nothing at runtime: no engine calls, just a one-time set build (well
    under a second for ~3,800 lines) and O(1) tuple lookups per ply."""
    prefixes = set()
    for path in sorted(glob.glob(os.path.join(BOOK_DIR, "*.tsv"))):
        with open(path, encoding="utf-8") as f:
            next(f, None)  # header row
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 3:
                    continue
                tokens = tuple(tok for tok in parts[2].split() if not tok[0].isdigit())
                for i in range(1, len(tokens) + 1):
                    prefixes.add(tokens[:i])
    return prefixes


def load_opening_names():
    """{SAN-token tuple: (eco, name)} for every complete line in the same
    Lichess openings reference the book check uses. Added 2026-09-24 for the
    masthead's opening name: the name is the LONGEST known line that the
    game's own move sequence starts with (e.g. a game that follows the
    Italian to move 4 then deviates is still "Italian Game: Giuoco
    Pianissimo"), and a game whose first move matches nothing gets None.
    Same move-order-only limitation as the book check."""
    names = {}
    for path in sorted(glob.glob(os.path.join(BOOK_DIR, "*.tsv"))):
        with open(path, encoding="utf-8") as f:
            next(f, None)
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 3:
                    continue
                tokens = tuple(tok for tok in parts[2].split() if not tok[0].isdigit())
                names[tokens] = (parts[0], parts[1])
    return names


def opening_for(moves_san, names):
    """Longest known line that prefixes the game, as {eco, name, plies}."""
    for k in range(len(moves_san), 0, -1):
        hit = names.get(tuple(moves_san[:k]))
        if hit:
            return {"eco": hit[0], "name": hit[1], "plies": k}
    return None


def book_flags(moves_san, book_prefixes):
    """True for ply i iff moves_san[0..i] is a known book prefix. Once a ply
    breaks from every known line, every ply after it is also False — no
    re-entering book later even if the position transposes back into theory,
    since that would need position (FEN) matching, not move-sequence
    matching (see load_book_prefixes's docstring)."""
    flags, seq, still_book = [], [], True
    for san in moves_san:
        seq.append(san)
        still_book = still_book and (tuple(seq) in book_prefixes)
        flags.append(still_book)
    return flags


def fetch_most_recent_pgn(username):
    archives = requests.get(
        f"https://api.chess.com/pub/player/{username}/games/archives",
        headers={"User-Agent": UA}, timeout=20
    ).json()["archives"]
    for url in reversed(archives):
        games = requests.get(url, headers={"User-Agent": UA}, timeout=20).json()["games"]
        finished = [g for g in games if "pgn" in g]
        if finished:
            return finished[-1]["pgn"]
    raise SystemExit("No finished games found in this player's archive.")


def fetch_pgn_by_url(game_url, username):
    m = re.search(r"/game/(?:live|daily)/(\d+)", game_url)
    if not m:
        raise SystemExit(f"Could not parse a game id out of: {game_url}")
    game_id = m.group(1)
    archives = requests.get(
        f"https://api.chess.com/pub/player/{username}/games/archives",
        headers={"User-Agent": UA}, timeout=20
    ).json()["archives"]
    for url in reversed(archives):
        games = requests.get(url, headers={"User-Agent": UA}, timeout=20).json()["games"]
        for g in games:
            if game_id in g.get("url", ""):
                return g["pgn"]
    raise SystemExit(f"Game id {game_id} not found in {username}'s archive.")


def clk_seconds(comment):
    m = re.search(r"\[%clk (\d+):(\d+):([\d.]+)\]", comment or "")
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def win_percent(cp):
    # Standard logistic approximation (Lichess-style) mapping centipawns to win%.
    cp = max(-1000, min(1000, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def move_accuracy(wp_before, wp_after, cpl=0):
    """Per-move accuracy from the win-probability drop (Lichess formula), with
    a centipawn-loss floor applied once the position is already decided.

    Fixed 2026-09-16 (the user flagged the gap against chess.com's own score:
    82.3% here vs. chess.com's 48.3% on the same game). Root cause: once
    win_before is already pinned near 0% or 100%, there's almost no
    probability room left to lose, so the wp-diff formula alone can't
    register a real blunder — e.g. a move that hangs mate while already at
    2.5% win probability showed 100% "accuracy" here, because 2.5% can't
    drop much further no matter how bad the move is. That's true of the real
    Lichess formula too, not just this approximation; chess.com's proprietary
    score evidently doesn't have the same blind spot. Below 10% or above 90%
    win probability, score from raw centipawn loss instead (capped at 500,
    roughly hung-a-queen magnitude) and take the harsher of the two — this
    doesn't touch the formula anywhere it was already working (10-90% band
    is untouched)."""
    diff = max(0.0, wp_before - wp_after)
    wp_acc = max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * diff) - 3.1668))
    if 10 < wp_before < 90:
        return wp_acc
    cpl_acc = max(0.0, 100.0 - cpl / 5.0)
    return min(wp_acc, cpl_acc)


def classify(cpl, is_best, gap, eval_before, is_sac, is_free_cap):
    decisive_already = abs(eval_before) >= 500
    if is_best:
        if is_sac and not decisive_already:
            return "Brilliant"
        if gap >= 150 and not decisive_already and not is_free_cap:
            return "Great Find"
        return "Best"
    if cpl <= 15:
        return "Excellent"
    if cpl <= 50:
        return "Good"
    if cpl <= 100:
        return "Inaccuracy"
    if cpl <= 200:
        return "Miss" if gap >= 150 else "Mistake"
    return "Miss" if gap >= 150 else "Blunder"


def phase_of(board, fullmove):
    """Opening = first 10 moves. Endgame = six or fewer non-pawn, non-king
    pieces left on the board (both sides combined). Otherwise middlegame.
    Same rule as bulk_review.py, kept in sync so the two scripts' "phase"
    concept means the same thing. Computed from the board
    state already in memory during the analysis loop — no extra engine call,
    zero added runtime."""
    if fullmove <= 10:
        return "opening"
    non_pawn_king = sum(
        len(board.pieces(pt, chess.WHITE)) + len(board.pieces(pt, chess.BLACK))
        for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
    )
    return "endgame" if non_pawn_king <= 6 else "middlegame"


def looks_like_sac(board_before, move, mover_white):
    """Very rough sacrifice heuristic: the moved piece lands on a square
    where the cheapest recapture is STRICTLY cheaper than the piece being
    offered (net material loss if taken) — an equal trade offer (e.g. a
    knight landing where only an enemy knight can take it) is not a
    sacrifice and must not qualify, corrected 2026-09-15 after it wrongly
    flagged a routine Nd5 outpost/trade-offer move as 'Brilliant'."""
    values = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
    piece = board_before.piece_at(move.from_square)
    if piece is None or piece.piece_type in (chess.PAWN, chess.KING):
        return False
    captured = board_before.piece_at(move.to_square)
    gained = values.get(captured.piece_type, 0) if captured else 0
    board_after = board_before.copy()
    board_after.push(move)
    # Only recaptures that are actually legal count. A king "attacking" a
    # defended square cannot take, so it is not a recapturer (fixed
    # 2026-09-16 after Rh2+ with a g3 pawn behind it was tagged Brilliant).
    attackers = [sq for sq in board_after.attackers(not mover_white, move.to_square)
                 if board_after.is_legal(chess.Move(sq, move.to_square))]
    if not attackers:
        return False
    cheapest_attacker = min(values.get(board_after.piece_at(sq).piece_type, 9) for sq in attackers)
    return cheapest_attacker < values[piece.piece_type] - gained


def is_free_capture(board_before, move):
    """Great Find's gap>=150 rule can't tell "the only good move was hard to
    find" apart from "the only good move was capturing a piece with zero
    defenders, and the gap is only huge because ignoring it lets some other
    threat through." That second case is just noticing a hanging piece —
    checking captures and threats is the most basic thing a player does on
    any move, not a find. Added 2026-09-16 after the user flagged Qxd4 (a
    completely undefended, forking knight) as tagged Great Find when it was,
    in his words, "a pretty standard move." Only checked for the mover's own
    capture of an undefended enemy piece; unrelated to looks_like_sac, which
    is about the mover's own piece being offered, not the target's defense."""
    captured = board_before.piece_at(move.to_square)
    if captured is None:
        return False
    return len(board_before.attackers(captured.color, move.to_square)) == 0

def compute_motifs(positions, annotations):
    """The "Missed Moves" section's data: motifs from find_motifs() applied
    to two populations. "missed" = the user's own error-tagged moves (a real,
    meaningfully-better alternative existed) where the engine's suggestion
    itself creates a motif. "opponent" = the opponent's own good-or-better
    moves where their actual move creates one — restricted to their good
    moves specifically, not every move, since "they found a fork" is only
    interesting when it was actually a strong choice for them, not just any
    move that happens to touch two pieces. No new engine calls: reuses
    positions[] and the uci/best_uci already computed per annotation."""
    error_tags = {"Blunder", "Miss", "Mistake", "Inaccuracy"}
    good_tags = {"Brilliant", "Great Find", "Excellent", "Best"}
    missed, opponent = [], []
    for a in annotations:
        before = positions[a["ply"]]
        if a["is_me"] and a["tag"] in error_tags and a.get("best_uci"):
            move = chess.Move.from_uci(a["best_uci"])
            if before.is_legal(move):
                motifs = find_motifs(before, move)
                if motifs:
                    missed.append({"ply": a["ply"], "motifs": motifs, "san": before.san(move)})
        elif not a["is_me"] and a["tag"] in good_tags and a.get("uci"):
            move = chess.Move.from_uci(a["uci"])
            if before.is_legal(move):
                motifs = find_motifs(before, move)
                if motifs:
                    opponent.append({"ply": a["ply"], "motifs": motifs, "san": a["san"]})
    return {"missed": missed, "opponent": opponent}


FACT_TAGS = {"Blunder", "Miss", "Mistake", "Inaccuracy", "Brilliant", "Great Find", "Excellent"}
VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 99}


def _sq(s):
    return chess.square_name(s)


def _san_line(board, moves, n=8):
    out, b = [], board.copy()
    for mv in moves[:n]:
        if not b.is_legal(mv):
            break
        out.append(b.san(mv))
        b.push(mv)
    return " ".join(out)


def _loose_pieces(board, color):
    """Pieces of `color` that the side to move (the opponent) can legally capture
    where the piece is undefended or the cheapest legal capturer is worth less.
    Call with `board.turn == not color`."""
    out = []
    for sq, pc in board.piece_map().items():
        if pc.color != color or pc.piece_type == chess.KING:
            continue
        caps = [m for m in board.legal_moves if m.to_square == sq]
        if not caps:
            continue
        cheapest = min(VALUES[board.piece_at(m.from_square).piece_type] for m in caps)
        # A defender only counts if it could LEGALLY recapture (a pinned pawn
        # cannot, e.g. f7 pinned by a queen on h5 does not defend e6).
        cheapest_capture = min(caps, key=lambda m: VALUES[board.piece_at(m.from_square).piece_type])
        probe = board.copy(); probe.push(cheapest_capture)
        recapturers = sorted({_sq(m.from_square) for m in probe.legal_moves if m.to_square == sq})
        static_defenders = [_sq(d) for d in board.attackers(color, sq)]
        if not recapturers or cheapest < VALUES[pc.piece_type]:
            out.append({"piece": pc.symbol() + _sq(sq),
                        "capturable_by": sorted({board.san(m) for m in caps}),
                        "legal_recapture_by": recapturers,
                        "static_defenders_that_cannot_recapture": [d for d in static_defenders if d not in recapturers]})
    return out


def _pins(board, color):
    return [board.piece_at(sq).symbol() + _sq(sq) for sq in chess.SQUARES
            if board.piece_at(sq) and board.piece_at(sq).color == color
            and board.piece_at(sq).piece_type != chess.KING and board.is_pinned(color, sq)]


def _checking_captures(board):
    return sorted(board.san(m) for m in board.legal_moves if board.is_capture(m) and board.gives_check(m))


def _material(board):
    return {("white" if c else "black"): "".join(
        f"{chess.piece_symbol(pt).upper()}{len(board.pieces(pt, c))}" for pt in range(1, 6))
        for c in (chess.WHITE, chess.BLACK)}


def position_facts(positions, moves_san, per_position, i):
    """Everything a notes writer needs about my move at ply i, from the board
    and the PVs already computed. No engine calls. Written so that no claim in a
    note has to be derived by eye."""
    before, after = positions[i], positions[i + 1]
    me = before.turn
    pp = per_position[i]
    best_pv = pp["pv"]
    best_board = before.copy()
    f = {
        "fen_before": before.fen(),
        "material_before": _material(before),
        "my_pinned_pieces_before": _pins(before, me),
        "opponent_could_have_played_before_my_move": None,
        "best_move": None,
        "played_move": moves_san[i],
        "actual_next": " ".join(moves_san[i + 1:i + 5]),
    }
    if best_pv:
        best_board.push(best_pv[0])
        replies = [best_board.san(m) for m in best_board.legal_moves]
        sc = pp["score_obj"].pov(me)
        f["best_move"] = {
            "san": before.san(best_pv[0]),
            "line": _san_line(before, best_pv),
            "mate_in": sc.mate() if sc.is_mate() else None,
            "opponent_legal_replies": replies if len(replies) <= 12 else replies[:12] + [f"... {len(replies)} total"],
            "reply_forced": len(replies) == 1,
        }
    # what my move leaves on the board: opponent to move in `after`
    opp_pv = per_position[i + 1]["pv"]
    f["after_my_move"] = {
        "opponent_best_line": _san_line(after, opp_pv),
        "my_loose_pieces": _loose_pieces(after, me),
        "opponent_checking_captures": _checking_captures(after),
        "my_pinned_pieces": _pins(after, me),
        "mate_against_me_in": (lambda s: s.mate() if s.is_mate() and s.mate() < 0 else None)(per_position[i + 1]["score_obj"].pov(me)),
    }
    # was there a threat I needed to meet? show what the opponent's best would have
    # been if I had passed (approximation: loose pieces with the opponent to move).
    null = before.copy()
    if null.is_valid() and not null.is_check():
        null.push(chess.Move.null())
        f["opponent_could_have_played_before_my_move"] = {
            "my_loose_pieces_if_I_pass": _loose_pieces(null, me),
            "checking_captures_if_I_pass": _checking_captures(null),
        }
    return f


def analyze_pgn(pgn_text, username, stockfish, depth=DEPTH):
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise SystemExit("Could not parse PGN.")
    headers = game.headers
    white_user = headers.get("White", "")
    black_user = headers.get("Black", "")
    my_color = chess.WHITE if white_user.lower() == username.lower() else chess.BLACK

    board = game.board()
    node = game
    positions, moves_san, comments = [board.copy()], [], [None]
    while node.variations:
        nxt = node.variations[0]
        moves_san.append(board.san(nxt.move))
        board.push(nxt.move)
        positions.append(board.copy())
        comments.append(nxt.comment)
        node = nxt

    is_book_flags = book_flags(moves_san, load_book_prefixes())

    engine = chess.engine.SimpleEngine.popen_uci(stockfish)
    per_position = []
    for pos in positions:
        info = engine.analyse(pos, chess.engine.Limit(depth=depth), multipv=2)
        lines = info if isinstance(info, list) else [info]
        top = lines[0]
        score1 = top["score"].white().score(mate_score=MATE_SCORE)
        pv1 = top.get("pv", [None])[0]
        score2 = None
        if len(lines) > 1:
            score2 = lines[1]["score"].white().score(mate_score=MATE_SCORE)
        per_position.append({"score_white": score1, "pv1": pv1, "score2_white": score2,
                             "pv": list(top.get("pv", [])), "score_obj": top["score"]})

    annotations = []
    for i, san in enumerate(moves_san):
        before, after = positions[i], positions[i + 1]
        mover_white = (i % 2 == 0)
        played_move = None
        for mv in before.legal_moves:
            if before.san(mv) == san:
                played_move = mv
                break

        ew_before = per_position[i]["score_white"]
        ew_after = per_position[i + 1]["score_white"]
        ew_before2 = per_position[i]["score2_white"]

        if mover_white:
            eval_before = ew_before
            eval_before2 = ew_before2
            eval_after = ew_after
            cpl = max(0, ew_before - ew_after)
        else:
            eval_before = -ew_before
            eval_before2 = -ew_before2 if ew_before2 is not None else None
            eval_after = -ew_after
            cpl = max(0, ew_after - ew_before)

        gap = (eval_before - eval_before2) if eval_before2 is not None else 0
        pv1 = per_position[i]["pv1"]
        is_best = (pv1 is not None and played_move is not None and pv1 == played_move)
        is_sac = looks_like_sac(before, played_move, mover_white) if played_move else False
        is_free_cap = is_free_capture(before, played_move) if played_move else False
        is_book = is_book_flags[i]

        tag = "Book" if is_book else classify(cpl, is_best, gap, eval_before, is_sac, is_free_cap)

        best_san = None
        best_uci = None
        if pv1 is not None:
            try:
                best_san = before.san(pv1)
                best_uci = pv1.uci()
            except Exception:
                pass

        wp_before = win_percent(eval_before)
        wp_after = win_percent(eval_after)
        acc = move_accuracy(wp_before, wp_after, cpl)

        annotations.append({
            "ply": i,
            "fullmove": before.fullmove_number,
            "color": "white" if mover_white else "black",
            "san": san,
            "tag": tag,
            "phase": phase_of(before, before.fullmove_number),
            "cpl": cpl,
            "best_san": best_san if not is_best and not is_book else None,
            "best_uci": best_uci if not is_best and not is_book else None,
            "uci": played_move.uci() if played_move else None,
            "eval_after_white": ew_after,
            "clock": clk_seconds(comments[i + 1]),
            "move_accuracy": round(acc, 1),
            "is_me": mover_white == (my_color == chess.WHITE),
            "wp_before": round(wp_before, 1),
            "wp_after": round(wp_after, 1),
            "gap_to_second_best": round(min(gap, MATE_SCORE), 1),
        })

    engine.quit()

    facts = {}
    for a in annotations:
        if not a["is_me"] or a["tag"] not in FACT_TAGS:
            continue
        i = a["ply"]
        facts[str(i)] = position_facts(positions, moves_san, per_position, i)

    def side_accuracy(color):
        # Book moves are excluded here (the user's call, 2026-09-16): they
        # aren't graded decisions, so counting them would pull the average
        # up for reasons unrelated to how well anything was actually played.
        # move_accuracy is still computed and stored per move regardless.
        vals = [a["move_accuracy"] for a in annotations if a["color"] == color and a["tag"] != "Book"]
        return round(sum(vals) / len(vals), 1) if vals else None

    return {
        "headers": {k: headers.get(k) for k in [
            "White", "Black", "WhiteElo", "BlackElo", "Result", "Termination",
            "Date", "TimeControl", "ECO", "ECOUrl", "Link"
        ]},
        "my_color": "white" if my_color == chess.WHITE else "black",
        "opening": opening_for(moves_san, load_opening_names()),
        "accuracy": {"white": side_accuracy("white"), "black": side_accuracy("black")},
        # One FEN per ply (n+1 total, index i = position before annotations[i]),
        # for the board replay viewer. The board objects already exist in memory
        # from walking the PGN above — this is free, no extra engine work, just
        # serializing something already built regardless of whether this field
        # is used. Lets the client render any position by lookup, with zero
        # chess logic (move legality, castling, en passant, promotion) needed
        # in JS: the FEN already reflects true post-move board state.
        "fens": [p.fen() for p in positions],
        "annotations": annotations,
        "facts": facts,
        "motifs": compute_motifs(positions, annotations),
    }


# ----------------------------------------------------------------------------
# engine discovery, extras (rolling average, head-to-head), rendering
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(HERE, "..", "templates", "game-review.html")
LOSS = {"checkmated", "resigned", "timeout", "abandoned"}
DRAW = {"agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"}


def find_stockfish(explicit=None):
    for candidate in (explicit, os.environ.get("STOCKFISH_PATH"), shutil.which("stockfish"),
                      "/opt/homebrew/bin/stockfish", "/usr/local/bin/stockfish", "/usr/bin/stockfish"):
        if candidate and os.path.exists(candidate):
            return candidate
    raise SystemExit("Stockfish not found. Install it (brew install stockfish) or pass --stockfish /path/to/stockfish.")


def find_latest_batch(data_dir):
    """Newest batch among the bulk_review.py JSON files in data_dir, ranked by
    the newest game's own end_time (not file mtime). Duck-typed on shape: a
    "games" list whose entries carry avg_accuracy and annotations.
    Returns (path, data) or (None, None)."""
    if not data_dir or not os.path.isdir(data_dir):
        return None, None
    best = None
    for fn in sorted(os.listdir(data_dir)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(data_dir, fn)
        try:
            d = json.load(open(path))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        games = d.get("games") if isinstance(d, dict) else None
        if not games or "avg_accuracy" not in games[0] or "annotations" not in games[0]:
            continue
        end_times = [g["end_time"] for g in games if isinstance(g.get("end_time"), (int, float))]
        if end_times and (best is None or max(end_times) > best[0]):
            best = (max(end_times), path, d)
    return (best[1], best[2]) if best else (None, None)


def rolling_averages(data):
    """Per-game averages from a bulk batch, for the "vs. your rolling average" strip."""
    games = data["games"]
    n = len(games)
    if not n:
        return {"available": False}
    accs = [g["avg_accuracy"] for g in games if g.get("avg_accuracy") is not None]
    counts = {"blunder": 0, "mistake": 0, "inaccuracy": 0}
    for g in games:
        for ann in g.get("annotations", []):
            if ann.get("tag") in counts:
                counts[ann["tag"]] += 1
    dates = [g["date"] for g in games if g.get("date")]
    return {
        "available": True,
        "n_games": n,
        "avg_accuracy": round(sum(accs) / len(accs), 1) if accs else None,
        "blunders_per_game": round(counts["blunder"] / n, 2),
        "mistakes_per_game": round(counts["mistake"] / n, 2),
        "inaccuracies_per_game": round(counts["inaccuracy"] / n, 2),
        "source_label": f"{dates[0]}-{dates[-1]}" if dates else "date range unknown",
    }


def _outcome(code):
    return "w" if code == "win" else "l" if code in LOSS else "d" if code in DRAW else "?"


def head_to_head(username, opponent, current_url):
    """Prior games against this opponent, from chess.com's public monthly archives."""
    me, opp = username.lower(), opponent.lower()
    rows = []
    archives = requests.get(f"https://api.chess.com/pub/player/{me}/games/archives", headers={"User-Agent": UA}, timeout=20).json()["archives"]
    for arch in archives:
        for g in requests.get(arch, headers={"User-Agent": UA}, timeout=20).json()["games"]:
            w, b = g["white"]["username"].lower(), g["black"]["username"].lower()
            if opp not in (w, b):
                continue
            color = "white" if w == me else "black"
            other = "black" if color == "white" else "white"
            rows.append({"end_time": g["end_time"], "url": g["url"], "color": color,
                         "time_class": g.get("time_class"), "result": _outcome(g[color]["result"]),
                         "termination": g[other]["result"] if g[color]["result"] == "win" else g[color]["result"],
                         "my_elo": g[color].get("rating"), "opp_elo": g[other].get("rating"),
                         "accuracy": (g.get("accuracies") or {}).get(color)})
    rows.sort(key=lambda r: r["end_time"])
    cur = next((r for r in rows if r["url"] == current_url), None)
    rows = [r for r in rows if r["url"] != current_url and (cur is None or r["end_time"] < cur["end_time"])]
    return {"available": True, "opponent": opponent, "n": len(rows),
            "w": sum(r["result"] == "w" for r in rows), "l": sum(r["result"] == "l" for r in rows),
            "d": sum(r["result"] == "d" for r in rows), "games": rows}


def render(review, notes, template_path, out_path, username, rolling=None, h2h=None, practice_url=""):
    for ply in notes:
        if int(ply) >= len(review["annotations"]):
            raise SystemExit(f"notes reference plies that do not exist: {ply}")
        if not review["annotations"][int(ply)]["is_me"]:
            raise SystemExit(f"notes written for an opponent ply (the page ignores them): {ply}")
    html = open(template_path, encoding="utf-8").read()
    m = re.search(r"/game/(?:live|daily)/(\d+)", (review.get("headers") or {}).get("Link") or "")
    engine = review.get("engine", {"depth": DEPTH, "multipv": 2})
    swaps = {
        "/*__REVIEW_JSON__*/ null": json.dumps(review),
        "/*__NOTES_JSON__*/ {}": json.dumps(notes),
        '/*__ROLLING_JSON__*/ {"available": false}': json.dumps(rolling or {"available": False}),
        '/*__H2H_JSON__*/ {"available": false}': json.dumps(h2h or {"available": False}),
        "__GENERATED__": dt.date.today().isoformat(),
        "__GAME_ID__": m.group(1) if m else "review",
        "__ENGINE__": f"stockfish depth {engine.get('depth')}, multipv {engine.get('multipv')}",
        "__PROMPT__": f"{username}@chess.com:~/review$ ",
        "__USERNAME__": username,
        "__PRACTICE_URL__": practice_url,
    }
    for placeholder, value in swaps.items():
        if placeholder not in html:
            raise SystemExit(f"template placeholder missing: {placeholder}")
        html = html.replace(placeholder, value)
    if not practice_url:
        html = re.sub(r'<a class="practice-cta".*?</a>', "", html, count=1, flags=re.S)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--username", help="your chess.com username (decides which side is 'you')")
    ap.add_argument("--url", help="a chess.com game URL; default is your most recent finished game")
    ap.add_argument("--pgn-file", help="analyze a local PGN instead of fetching from chess.com")
    ap.add_argument("--from-json", help="skip the engine and re-render an existing review JSON")
    ap.add_argument("--out", required=True, help="output path: .html renders the page, .json writes raw analysis")
    ap.add_argument("--json", help="when rendering HTML, also save the raw analysis JSON here")
    ap.add_argument("--notes", help="optional JSON map of ply -> explanation")
    ap.add_argument("--data-dir", help="folder of bulk_review.py batch JSON, for the rolling-average strip")
    ap.add_argument("--no-h2h", action="store_true", help="skip the vs.-this-opponent callout")
    ap.add_argument("--practice-url", default="", help="link to a practice page; omit to hide the link")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE)
    ap.add_argument("--depth", type=int, default=DEPTH, help=f"engine search depth per position (default {DEPTH})")
    ap.add_argument("--stockfish", help="path to the Stockfish binary (default: STOCKFISH_PATH, then PATH)")
    args = ap.parse_args()

    if args.from_json:
        review = json.load(open(args.from_json))
        username = args.username or ""
    else:
        if not args.username:
            ap.error("--username is required unless --from-json is given")
        username = args.username
        if args.pgn_file:
            pgn_text = open(args.pgn_file).read()
        elif args.url:
            pgn_text = fetch_pgn_by_url(args.url, username)
        else:
            pgn_text = fetch_most_recent_pgn(username)
        review = analyze_pgn(pgn_text, username, find_stockfish(args.stockfish), args.depth)
        review["engine"] = {"depth": args.depth, "multipv": 2}

    if args.out.lower().endswith(".json"):
        with open(args.out, "w") as f:
            json.dump(review, f, indent=2)
        print(f"wrote {args.out}")
        return
    if args.json:
        with open(args.json, "w") as f:
            json.dump(review, f, indent=2)
    notes = json.load(open(args.notes)) if args.notes else {}

    rolling = None
    _, batch = find_latest_batch(args.data_dir)
    if batch:
        rolling = rolling_averages(batch)

    h2h = None
    headers = review.get("headers") or {}
    opponent = headers.get("Black") if review.get("my_color") == "white" else headers.get("White")
    if username and opponent and not args.no_h2h:
        try:
            h2h = head_to_head(username, opponent, headers.get("Link") or "")
        except Exception as e:  # network failure: the callout is an extra, never a reason to fail
            h2h = {"available": False, "error": str(e)}

    render(review, notes, args.template, args.out, username or headers.get("White", "you"), rolling, h2h, args.practice_url)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
