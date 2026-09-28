"""Tactical motif detection on known positions. There is one implementation
(scripts/motifs.py); every script must use it rather than a copy of its own."""
import chess
import pytest

import bulk_review
import practice
import motifs as motifs_module
import review

MODS = [motifs_module]


def motifs(mod, fen, san):
    board = chess.Board(fen)
    return set(mod.find_motifs(board, board.parse_san(san)))


@pytest.mark.parametrize("mod", MODS)
def test_fork(mod):
    assert "fork" in motifs(mod, "r3r1k1/8/8/1N6/8/8/8/7K w - - 0 1", "Nc7")


@pytest.mark.parametrize("mod", MODS)
def test_fork_not_counted_when_a_pawn_takes_the_forker(mod):
    # Bd5 hits both rooks (a8, a2); with a black pawn on c6 the bishop just gets taken.
    assert "fork" in motifs(mod, "r6k/8/8/8/4B3/8/r7/7K w - - 0 1", "Bd5")
    assert "fork" not in motifs(mod, "r6k/8/2p5/8/4B3/8/r7/7K w - - 0 1", "Bd5")


@pytest.mark.parametrize("mod", MODS)
def test_fork_not_counted_when_the_king_takes_the_forker(mod):
    # Nd6 hits both rooks, but it is undefended next to the black king: Kxd6 ends it.
    assert "fork" not in motifs(mod, "8/1r2kr2/8/1N6/8/8/8/6K1 w - - 0 1", "Nd6")
    # same idea with the knight defended by a pawn: now the king cannot take, so it is a fork
    assert "fork" in motifs(mod, "8/1r2kr2/8/1NP5/8/8/8/6K1 w - - 0 1", "Nd6")


@pytest.mark.parametrize("mod", MODS)
def test_pin(mod):
    assert "pin" in motifs(mod, "4k3/4n3/8/8/8/8/8/R5K1 w - - 0 1", "Re1")


@pytest.mark.parametrize("mod", MODS)
def test_existing_pin_is_not_new(mod):
    assert "pin" not in motifs(mod, "4k3/4n3/8/8/8/8/8/4R1K1 w - - 0 1", "Kh1")


@pytest.mark.parametrize("mod", MODS)
def test_discovered_attack(mod):
    assert "discovered attack" in motifs(mod, "4q1k1/8/8/8/4N3/8/8/4R1K1 w - - 0 1", "Nc3")


@pytest.mark.parametrize("mod", MODS)
def test_discovered_check(mod):
    found = motifs(mod, "4k3/8/8/8/4N3/8/8/4R1K1 w - - 0 1", "Nc3")
    assert "discovered check" in found and "discovered attack" not in found


@pytest.mark.parametrize("mod", MODS)
def test_quiet_move_has_no_motifs(mod):
    assert motifs(mod, chess.STARTING_FEN, "e4") == set()


@pytest.mark.parametrize("script", [review, bulk_review, practice])
def test_every_script_uses_the_shared_detector(script):
    assert script.find_motifs is motifs_module.find_motifs
