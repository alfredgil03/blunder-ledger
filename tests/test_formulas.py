"""The scoring formulas (see docs/methodology.md). review.py and bulk_review.py
carry separate copies on purpose, so both are checked against the same numbers."""
import pytest

import bulk_review
import review


@pytest.mark.parametrize("mod", [review, bulk_review])
def test_win_percent_shape(mod):
    assert mod.win_percent(0) == pytest.approx(50.0)
    assert mod.win_percent(300) + mod.win_percent(-300) == pytest.approx(100.0)
    assert mod.win_percent(100) > mod.win_percent(50) > 50
    # clamped at +/-1000 cp, so a mate score does not blow up
    assert mod.win_percent(100000) == mod.win_percent(1000)


@pytest.mark.parametrize("mod", [review, bulk_review])
def test_move_accuracy(mod):
    assert mod.move_accuracy(50, 50) == pytest.approx(100.0, abs=0.01)
    assert mod.move_accuracy(50, 60) == pytest.approx(100.0, abs=0.01)  # gaining never costs
    assert mod.move_accuracy(60, 40) < mod.move_accuracy(60, 55)
    assert 0 <= mod.move_accuracy(80, 0) <= 100
    # in an already-decided position the centipawn floor catches a real blunder
    assert mod.move_accuracy(3, 2, cpl=500) == pytest.approx(0.0)
    assert mod.move_accuracy(3, 2, cpl=0) > 90


def test_formula_copies_agree():
    for wb, wa, cpl in [(50, 30, 0), (70, 69, 10), (95, 60, 300), (5, 1, 250)]:
        assert review.move_accuracy(wb, wa, cpl) == pytest.approx(bulk_review.move_accuracy(wb, wa, cpl))
    for cp in (-2000, -350, 0, 42, 999):
        assert review.win_percent(cp) == pytest.approx(bulk_review.win_percent(cp))


@pytest.mark.parametrize("cpl,is_best,gap,expected", [
    (0, True, 0, "Best"),
    (0, True, 200, "Great Find"),
    (10, False, 0, "Excellent"),
    (40, False, 0, "Good"),
    (90, False, 0, "Inaccuracy"),
    (150, False, 0, "Mistake"),
    (150, False, 200, "Miss"),
    (400, False, 0, "Blunder"),
    (400, False, 200, "Miss"),
])
def test_classify_thresholds(cpl, is_best, gap, expected):
    assert review.classify(cpl, is_best, gap, 0, False, False) == expected


def test_classify_decisive_position_suppresses_great_find_and_brilliant():
    assert review.classify(0, True, 300, 800, False, False) == "Best"
    assert review.classify(0, True, 0, 800, True, False) == "Best"
    assert review.classify(0, True, 0, 0, True, False) == "Brilliant"
