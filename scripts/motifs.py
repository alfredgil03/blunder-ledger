"""Tactical motif detection, shared by every script: review.py (one game),
bulk_review.py (the ledger), and practice.py (bait calibration and puzzle
explanations). One implementation, so the three can never disagree about what counts as a fork or a pin.

Pure board mechanics with python-chess: no engine call, about 0.1 ms a move.
The practice page's JavaScript bot has its own port of these rules (it runs
in the browser); tests/js/engine_tests.js checks it on the same positions."""
import chess

# Piece values for motif decisions. The king is 0 on purpose: a fork the
# enemy king can capture was undefended, so it is refuted, not a fork.
MOTIF_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}


def pinned_squares(board, color):
    """Squares of color's pieces (not the king) absolutely pinned to their king."""
    return {sq for sq in chess.SQUARES
            if board.piece_at(sq) and board.piece_at(sq).color == color
            and board.piece_at(sq).piece_type != chess.KING and board.is_pinned(color, sq)}


def find_motifs(board_before, move):
    """Tactical motifs `move` creates. Returns a list of names; a move can
    trigger more than one (a fork that is also check).

    Exact: "pin" (a new absolute pin) and "discovered check" (check from a
    piece other than the one that moved).

    Heuristic: "discovered attack" (an enemy piece gains an attacker other than
    the moved piece, i.e. a line was opened; it does not check the target was
    undefended) and "fork" (the moved piece attacks two or more enemy pieces
    worth a knight or more, and cannot be taken by something cheaper than
    itself; it does not check both threats are unanswerable).

    Only absolute pins (to the king) are detected."""
    board_after = board_before.copy()
    board_after.push(move)
    mover, enemy = board_before.turn, not board_before.turn
    motifs = []

    if pinned_squares(board_after, enemy) - pinned_squares(board_before, enemy):
        motifs.append("pin")

    # Checkers minus the moved piece's own square, so a double check (the moved
    # piece checks AND reveals a second checker) still counts as discovered.
    if board_after.is_check() and (board_after.checkers() - chess.SquareSet([move.to_square])):
        motifs.append("discovered check")

    if "discovered check" not in motifs:
        for sq, pc in board_before.piece_map().items():
            if pc.color != enemy or pc.piece_type == chess.KING or sq == move.to_square:
                continue
            new_attackers = board_after.attackers(mover, sq) - board_before.attackers(mover, sq)
            # The moved piece can ALSO be a new attacker on the same square; only
            # an attacker other than it makes this a discovery.
            if new_attackers - {move.to_square}:
                motifs.append("discovered attack")
                break

    moved_piece = board_after.piece_at(move.to_square)
    if moved_piece and moved_piece.piece_type != chess.KING:
        targets = [sq for sq in board_after.attacks(move.to_square)
                   if (tp := board_after.piece_at(sq)) and tp.color == enemy
                   and tp.piece_type != chess.KING and MOTIF_VALUES[tp.piece_type] >= 3]
        if len(targets) >= 2:
            takers = [a for a in board_after.attackers(enemy, move.to_square)
                      if board_after.is_legal(chess.Move(a, move.to_square))]
            cheapest = min((MOTIF_VALUES[board_after.piece_at(a).piece_type] for a in takers), default=None)
            if cheapest is None or cheapest >= MOTIF_VALUES[moved_piece.piece_type]:
                motifs.append("fork")

    return motifs
