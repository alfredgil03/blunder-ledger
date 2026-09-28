#!/usr/bin/env python3
"""Build the Blunder Bait practice page: a playable board whose bot strength
and baited patterns are calibrated from your own bulk_review.py batch data.
Prints the computed Elo band and weakness check as JSON.

usage: practice.py --out page.html [--data-dir DIR] [--username NAME]

DIR is a folder of bulk_review.py batch JSON (default: ../examples, the sample
data shipped with the repo). The page needs no server: open it in a browser or
host it as a static file. Session history is kept in the browser's localStorage.
"""
import argparse
import json
import os

import chess
from motifs import find_motifs, pinned_squares as _pinned_squares

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "templates", "practice.html")
DATA_DIR = os.path.join(HERE, "..", "examples")


def find_latest_batch(data_dir):
    """Picks the batch of games most recently played, across every saved
    bulk_review.py output in data_dir. Duck-types on shape (a "games" list
    whose entries carry avg_accuracy and annotations) rather than a filename
    pattern, matching review.py's find_latest_batch() exactly, since this
    script needs the identical "most recent batch" as its weakness/Elo source. Ranks by
    the newest game's own end_time inside each file, not file mtime, for the
    same reason that function does: two batches saved by the same script run
    can land within milliseconds of each other on disk.
    Returns (path, data) or (None, None).
    """
    if not os.path.isdir(data_dir):
        return None, None
    best = None  # (newest_game_end_time, path, data)
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
        if not end_times:
            continue
        newest = max(end_times)
        if best is None or newest > best[0]:
            best = (newest, path, d)
    if best is None:
        return None, None
    return best[1], best[2]


# Hand-tuned knobs for the bot's shallow search (see blunder-bait-template.html's
# own engine comments for what each one does) — not a validated Elo curve,
# chess.com/Stockfish-style rating calibration isn't available to a 2-ply
# custom search, just a monotonic "weaker/noisier <-> stronger/sharper" dial
# keyed to the user's own current rating band. Revisit if the feel is off at a
# given band; there's no principled source to calibrate against.
ELO_BANDS = [
    (350, {"tolerance": 90, "noise": 30, "replyCap": 8, "candidateCap": 14, "label": "under 350", "depth": 2}),
    (450, {"tolerance": 75, "noise": 26, "replyCap": 9, "candidateCap": 16, "label": "350-450", "depth": 2}),
    (550, {"tolerance": 60, "noise": 22, "replyCap": 10, "candidateCap": 18, "label": "450-550", "depth": 2}),
    (650, {"tolerance": 45, "noise": 18, "replyCap": 11, "candidateCap": 20, "label": "550-650", "depth": 2}),
    (None, {"tolerance": 30, "noise": 14, "replyCap": 12, "candidateCap": 22, "label": "650+", "depth": 3}),
]


def band_for(elo):
    for ceiling, band in ELO_BANDS:
        if ceiling is None or elo < ceiling:
            return dict(band)
    return dict(ELO_BANDS[-1][1])


_MOTIF_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}

def motif_rate(games, motif_name):
    """Share of BLUNDER-tagged moves (same denominator convention as the
    existing hung-material weakness stat, for direct badge comparability)
    where the engine's suggested alternative would have created `motif_name`.
    Recomputed fresh from fen_before/best every time rather than trusting a
    saved best_motifs field, since older batches predate that field."""
    total = 0
    count = 0
    for g in games:
        for a in g.get("annotations", []):
            if a.get("tag") != "blunder":
                continue
            total += 1
            if not a.get("fen_before") or not a.get("best"):
                continue
            try:
                board = chess.Board(a["fen_before"])
                mv = board.parse_san(a["best"])
            except Exception:
                continue
            if motif_name in find_motifs(board, mv):
                count += 1
    pct = round(100 * count / total, 1) if total else 0.0
    return {"count": count, "total": total, "pct": pct}


def allowed_rate(games, kind):
    """Share of BLUNDER-tagged moves where the opponent's best reply (the
    saved opp_punish) was `kind`. This is the "what you ALLOW" measure the
    punish pass needs (added 2026-09-24): motif_rate() above counts what you
    MISSED (the engine's suggestion would have been a fork), which is the
    right data for baiting but not for punishing, because the bot punishes
    what you leave available, not what you fail to play. kind is
    "hung material" (the reply is a capture), or a find_motifs() name."""
    total = 0
    count = 0
    for g in games:
        for a in g.get("annotations", []):
            if a.get("tag") != "blunder":
                continue
            total += 1
            reply = a.get("opp_punish")
            if not reply or not a.get("fen_before") or not a.get("played"):
                continue
            if kind == "hung material":
                count += 1 if "x" in reply else 0
                continue
            try:
                board = chess.Board(a["fen_before"])
                board.push_san(a["played"])
                found = find_motifs(board, board.parse_san(reply))
            except Exception:
                continue
            if kind in found or (kind == "discovered attack" and "discovered check" in found):
                count += 1
    pct = round(100 * count / total, 1) if total else 0.0
    return {"count": count, "total": total, "pct": pct}


def all_bands_for_dropdown():
    """Serializes the full ELO_BANDS table for the Opponent Rating widget,
    one id per band (index, stable within a single build) — see that
    widget's JS for how "auto" (the calibrated TUNING pick) vs. an explicit
    override is chosen at runtime."""
    return [dict(band, id=i) for i, (_, band) in enumerate(ELO_BANDS)]


# ---------------------------------------------------------------------------
# Review mode: "this week's key moments" puzzle set.
# ---------------------------------------------------------------------------
PIECE_VALUES = {"p": 1, "n": 3, "b": 3, "r": 5, "q": 9}
WEEK_SECONDS = 7 * 86400
REVIEW_LIMIT = 25


def material_balance(fen, my_color):
    """Material balance (in pawns) from my_color's perspective, read straight
    off a FEN board field — no engine call, just counting letters. Used only
    to decide whether a flagged moment was still "competitive" (see
    find_week_puzzles), the same literal "down too much material" framing
    the user asked for, deliberately not an engine-eval-based measure."""
    board = fen.split(" ")[0]
    white = sum(PIECE_VALUES.get(c.lower(), 0) for c in board if c.isupper())
    black = sum(PIECE_VALUES.get(c.lower(), 0) for c in board if c.isalpha() and c.islower())
    diff = white - black
    return diff if my_color == "white" else -diff


_PN = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop", chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}


def explain_best(fen, best_san, played_san=None, opp_punish=None, blunder_type=None, cpl=None):
    """Plain-English reason the engine's move works, for Review mode's answer
    reveal. Pure board mechanics on the saved position (no engine call),
    reusing find_motifs() so the label always matches what chess-progress-
    report and the practice bot already call a pin/fork/discovered attack.
    Returns {"label": short pattern tag, "why": [sentences]}. When no single
    tactic is detectable it says so instead of inventing one: the engine's
    preference is then positional or a deeper line than these checks see."""
    try:
        board = chess.Board(fen)
        move = board.parse_san(best_san)
    except Exception:
        return {"label": "unknown", "why": []}
    mover, enemy = board.turn, not board.turn
    piece = board.piece_at(move.from_square)
    why, label = [], None

    def pname(sq, b):
        pc = b.piece_at(sq)
        return _PN[pc.piece_type] if pc else "piece"

    after = board.copy()
    after.push(move)
    if after.is_checkmate():
        label = "checkmate"
        why.append("It is checkmate.")
    else:
        motifs = find_motifs(board, move)
        if board.is_capture(move):
            victim = board.piece_at(move.to_square)
            vtype = victim.piece_type if victim else chess.PAWN  # en passant
            attackers = after.attackers(enemy, move.to_square)
            recapture = any(after.is_legal(chess.Move(a, move.to_square)) for a in attackers)
            if not recapture:
                label = "free piece"
                why.append(f"It captures an undefended {_PN[vtype]}, and nothing can take back.")
            elif _MOTIF_VALUES[vtype] > _MOTIF_VALUES[piece.piece_type]:
                label = "winning trade"
                why.append(f"It takes a {_PN[vtype]} with a {_PN[piece.piece_type]}, so even after the recapture you come out ahead.")
            else:
                why.append(f"It captures the {_PN[vtype]}.")
        if "pin" in motifs:
            new = _pinned_squares(after, enemy) - _pinned_squares(board, enemy)
            sq = next(iter(new))
            label = label or "pin"
            why.append(f"It pins their {pname(sq, after)} to the king, so it can't move without exposing the king.")
        if "fork" in motifs:
            targets = [pname(sq, after) for sq in after.attacks(move.to_square)
                       if (tp := after.piece_at(sq)) and tp.color == enemy and tp.piece_type != chess.KING
                       and _MOTIF_VALUES[tp.piece_type] >= 3]
            label = label or "fork"
            why.append(f"The {_PN[piece.piece_type]} now attacks their {' and '.join(targets[:3])} at once, and they can only save one.")
        if "discovered check" in motifs:
            label = label or "discovered check"
            why.append("Moving this piece uncovers a check from another piece behind it.")
        elif "discovered attack" in motifs:
            label = label or "discovered attack"
            why.append("Moving this piece uncovers an attack from another piece behind it, so two things are threatened at once.")
        if after.is_check() and "discovered check" not in motifs:
            why.append("It gives check, which limits their replies.")
            label = label or "check"
        # Saves something: one of your pieces was attacked and undefended
        # (or attacked by something cheaper) before, and no longer is.
        def loose(b):
            out = set()
            for sq, pc in b.piece_map().items():
                if pc.color != mover or pc.piece_type in (chess.KING, chess.PAWN):
                    continue
                atk = b.attackers(enemy, sq)
                if atk and (not b.attackers(mover, sq) or
                            min(_MOTIF_VALUES[b.piece_at(a).piece_type] for a in atk) < _MOTIF_VALUES[pc.piece_type]):
                    out.add(sq)
            return out
        was = loose(board)
        if was and not (was & loose(after)):
            label = label or "saves a piece"
            why.append("It rescues a piece of yours that was under attack.")
    if not why:
        label = "positional"
        why.append("No single tactic stands out here. The engine prefers it for position or a longer line than a quick check can spell out.")
    if opp_punish:
        if blunder_type == "hung_material":
            why.append(f"Your move in the real game left material hanging: they answered {opp_punish}.")
        else:
            why.append(f"After your real move, their best reply was {opp_punish}.")
    if cpl is not None:
        why.append("The swing here was mate-sized." if cpl >= 1000
                   else f"Your move cost about {cpl / 100:.1f} pawns compared with this one.")
    return {"label": label or "positional", "why": why}


def find_week_games(data_dir):
    """Pools every game across every saved batch file in data_dir (same
    duck-type check as find_latest_batch, so it picks up any rolling or
    custom-range batch that's been saved, not just the most recent one),
    dedupes by game URL, and keeps only games within 7 real days of the
    single most recent game in the whole pool. At the user's current pace
    (2026-09) this needs more than one 50-game batch to actually reach 7
    days — a single batch alone often covers under 4. Zero new I/O or
    engine calls: everything here is already sitting on disk from prior
    bulk_review.py runs."""
    if not os.path.isdir(data_dir):
        return []
    by_url = {}
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
        for g in games:
            url = g.get("url")
            if url and url not in by_url:
                by_url[url] = g
    all_games = list(by_url.values())
    if not all_games:
        return []
    latest = max(g["end_time"] for g in all_games if isinstance(g.get("end_time"), (int, float)))
    cutoff = latest - WEEK_SECONDS
    return [g for g in all_games if (g.get("end_time") or 0) >= cutoff]


def find_week_puzzles(data_dir, limit=REVIEW_LIMIT):
    """One "key moment" per game, from real games in the last 7 days: the
    single highest-centipawn-loss blunder or mistake in that game, but only
    counted if the position was still competitive when it happened (material
    within +/-5 of even) — per the user's explicit ask (2026-09-21), "not
    every game and blunder needs its own puzzle," and specifically excluding
    moments where the game was already a blowout either way. Ranks every
    game's single worst qualifying moment by severity (cpl) across the whole
    week and keeps the top `limit`, then re-sorts that final set
    chronologically for presentation — so severity picks which moments make
    the cut, but the user plays through them in the order the games happened.
    Returns (puzzles, meta) where meta describes the pool for the "how this
    works" text; puzzles is [] (not an error) if nothing qualified.
    """
    week_games = find_week_games(data_dir)
    per_game_worst = []
    for g in week_games:
        candidates = [
            a for a in g.get("annotations", [])
            if a.get("tag") in ("blunder", "mistake")
            and a.get("fen_before") and a.get("best")
            and -5 <= material_balance(a["fen_before"], g["my_color"]) <= 5
        ]
        if not candidates:
            continue
        # bulk_review.py represents a forced-mate eval as +/-100000 (its
        # MATE_SCORE), so a move that swings through a mate score gets a cpl
        # around 100000 — genuinely severe, but not comparable on the same
        # scale as an ordinary blunder (losing a queen is ~900). Picking the
        # single worst move in a game by raw cpl is still fine here, since
        # within one game the mate-related move usually is the worst thing
        # that happened; the scale problem only bites when ranking ACROSS
        # games below.
        worst = max(candidates, key=lambda a: a.get("cpl") or 0)
        per_game_worst.append({
            "fen": worst["fen_before"],
            "best": worst["best"],
            "played": worst["played"],
            "tag": worst["tag"],
            "cpl": worst.get("cpl"),
            "phase": worst.get("phase"),
            "color": "w" if g["my_color"] == "white" else "b",
            "opp": g.get("opp_name"),
            "date": g.get("date"),
            "url": g.get("url"),
            "end_time": g.get("end_time"),
            **explain_best(worst["fen_before"], worst["best"], worst["played"],
                           worst.get("opp_punish"), worst.get("blunder_type"), worst.get("cpl")),
        })

    # Cross-game selection: a straight sort-by-cpl would be dominated by
    # mate-score entries (routinely ~100000, vs. a few hundred for an
    # ordinary blunder) to the point where the whole top-`limit` set would
    # be nothing but missed/allowed mates — real, but not the varied "key
    # moments" set the user asked for. Split into the two populations first
    # (mate-adjacent vs. ordinary, at the same 1000cp threshold used
    # elsewhere as "a queen"), rank each by real severity within its own
    # population, then fill `limit` slots proportionally to how common each
    # population actually was that week — so the mix reflects reality
    # instead of one scale swamping the other.
    mate_pool = sorted([p for p in per_game_worst if (p["cpl"] or 0) >= 1000],
                        key=lambda p: p["cpl"] or 0, reverse=True)
    ordinary_pool = sorted([p for p in per_game_worst if (p["cpl"] or 0) < 1000],
                            key=lambda p: p["cpl"] or 0, reverse=True)
    total = len(mate_pool) + len(ordinary_pool)
    mate_slots = round(limit * len(mate_pool) / total) if total else 0
    puzzles = mate_pool[:mate_slots] + ordinary_pool[:limit - min(mate_slots, len(mate_pool))]
    puzzles = puzzles[:limit]
    puzzles.sort(key=lambda p: p["end_time"] or 0)

    meta = {
        "count": len(puzzles),
        "games_in_week": len(week_games),
        "games_with_key_moment": len(per_game_worst),
        "week_range": (
            f"{min(g['date'] for g in week_games)}–{max(g['date'] for g in week_games)}"
            if week_games else None
        ),
    }
    return puzzles, meta


def compute_profile(data):
    games = sorted(data["games"], key=lambda g: g.get("end_time") or 0)
    elo_history = data.get("elo_history") or []
    if elo_history:
        current_elo = elo_history[-1].get("my_elo")
    else:
        current_elo = games[-1].get("my_elo") if games else None

    total_blunders = 0
    hung = 0
    for g in games:
        for a in g.get("annotations", []):
            if a.get("tag") == "blunder":
                total_blunders += 1
                if a.get("blunder_type") == "hung_material":
                    hung += 1
    pct = round(100 * hung / total_blunders, 1) if total_blunders else 0.0
    # "Dominant" means hung material is still the single largest named
    # blunder-type bucket (as opposed to some other pattern having overtaken
    # it) — this bot only knows how to bait hung material, so this
    # is the signal that its one mechanic is still pointed at the right
    # target, not a claim that hung material explains most of the game.
    dominant = bool(total_blunders) and hung * 2 >= total_blunders

    # Discovered-attack baiting added 2026-09-21, per real frequency data
    #: 10-11% of flagged moves, more than
    # double fork or pin — the natural second pattern to wire up, chosen by
    # measurement rather than a guess. Same blunder-only denominator as
    # hung material above, for direct badge comparability. Fork and pin
    # measured the same way now too (fork baited 2026-09-21 later same
    # session, pin planned next) so the badges/how-it-works text can show
    # all four real percentages side by side, grounded the same way.
    discovered_attack = motif_rate(games, "discovered attack")
    fork = motif_rate(games, "fork")
    pin = motif_rate(games, "pin")

    allowed = {
        "hung_material": allowed_rate(games, "hung material"),
        "discovered_attack": allowed_rate(games, "discovered attack"),
        "fork": allowed_rate(games, "fork"),
        "pin": allowed_rate(games, "pin"),
    }

    return {
        "allowed": allowed,
        "elo": current_elo,
        "hung_count": hung,
        "total_blunders": total_blunders,
        "pct": pct,
        "dominant": dominant,
        "discovered_attack": discovered_attack,
        "fork": fork,
        "pin": pin,
        "n_games": len(games),
        "date_range": f"{games[0]['date']}–{games[-1]['date']}" if games else None,
    }


def review_page_name(game_url):
    """review-<chess.com game id>.html, the file name blunder.py gives a game's review."""
    gid = (game_url or "").rstrip("/").split("/")[-1]
    return f"review-{gid}.html" if gid.isdigit() else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--data-dir", default=DATA_DIR, help="folder of bulk_review.py batch JSON (default: the sample data in examples/)")
    ap.add_argument("--username", default="player", help="name shown in the page's terminal-style prompt")
    args = ap.parse_args()

    path, data = find_latest_batch(args.data_dir)
    if data is None:
        raise SystemExit(
            "no batch data found in " + args.data_dir
            + " - run bulk_review.py --json first, or point --data-dir at examples/"
        )

    profile = compute_profile(data)
    if profile["elo"] is None:
        raise SystemExit("latest batch has no usable my_elo to calibrate the bot from")

    band = band_for(profile["elo"])

    source = f"last {profile['n_games']} rated rapid games ({profile['date_range']})"
    weakness = {
        "pct": profile["pct"],
        "count": profile["hung_count"],
        "total": profile["total_blunders"],
        "source": source,
        "dominant": profile["dominant"],
        # Both patterns the bot now baits (see computeBotMove's two-pass
        # check: hung material first, discovered attack as a fallback when
        # no hung-material bait exists in the near-best pool) — kept as a
        # list so a third/fourth pattern (fork, pin) slots in later without
        # another schema change, not just appended ad hoc.
        # What you ALLOW (the opponent's best reply to each blunder), which
        # is what the punish pass acts on. Keys match PUNISH_CHECKS in the
        # template; the page uses these to order and describe its punishing.
        "allowed": {k: dict(v, source=source) for k, v in profile["allowed"].items()},
        "patterns": [
            {"id": "hung_material", "name": "hung material", "count": profile["hung_count"], "total": profile["total_blunders"],
             "pct": profile["pct"], "source": source},
            {"id": "discovered_attack", "name": "discovered attack", "count": profile["discovered_attack"]["count"],
             "total": profile["discovered_attack"]["total"], "pct": profile["discovered_attack"]["pct"],
             "source": source},
            {"id": "fork", "name": "fork", "count": profile["fork"]["count"],
             "total": profile["fork"]["total"], "pct": profile["fork"]["pct"],
             "source": source},
            {"id": "pin", "name": "pin", "count": profile["pin"]["count"],
             "total": profile["pin"]["total"], "pct": profile["pin"]["pct"],
             "source": source},
        ],
    }
    tuning = dict(band)
    tuning["eloValue"] = profile["elo"]
    bands = all_bands_for_dropdown()

    puzzles, puzzle_meta = find_week_puzzles(args.data_dir)
    # Link each puzzle to a full review of its game when one sits next to the
    # page as review-<game id>.html (blunder.py practice --with-reviews makes them).
    out_dir = os.path.dirname(os.path.abspath(args.out))
    for p in puzzles:
        page = review_page_name(p.get("url"))
        if page and os.path.exists(os.path.join(out_dir, page)):
            p["review"] = page
    puzzle_meta["with_review"] = sum(1 for p in puzzles if p.get("review"))

    tpl = open(TEMPLATE).read()
    placeholders = {
        "/*__WEAKNESS_JSON__*/ {}": weakness,
        "/*__TUNING_JSON__*/ {}": tuning,
        "/*__BANDS_JSON__*/ []": bands,
        "/*__PUZZLES_JSON__*/ []": puzzles,
        "/*__PUZZLE_META_JSON__*/ {}": puzzle_meta,
    }
    for ph in placeholders:
        assert ph in tpl, f"template placeholder missing: {ph}"
    page = tpl.replace("__PROMPT__", f"{args.username}@chess.com:~/spar$ ")
    for ph, value in placeholders.items():
        page = page.replace(ph, json.dumps(value))
    open(args.out, "w").write(page)

    print(json.dumps({
        "batch_source": os.path.basename(path),
        "profile": profile,
        "band": band,
        "puzzle_meta": puzzle_meta,
    }, indent=1))


if __name__ == "__main__":
    main()
