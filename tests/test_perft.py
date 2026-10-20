"""Perft counts for the standard test positions on the Chess Programming Wiki."""

import pytest

from chessrules import STARTING_FEN, parse_fen, perft

KIWIPETE = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"
POSITION_3 = "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1"
POSITION_4 = "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1"
POSITION_4_MIRRORED = "r2q1rk1/pP1p2pp/Q4n2/bbp1p3/Np6/1B3NBn/pPPP1PPP/R3K2R b KQ - 0 1"
POSITION_5 = "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8"

CASES = [
    (STARTING_FEN, 1, 20),
    (STARTING_FEN, 2, 400),
    (STARTING_FEN, 3, 8_902),
    (STARTING_FEN, 4, 197_281),
    (KIWIPETE, 1, 48),
    (KIWIPETE, 2, 2_039),
    (KIWIPETE, 3, 97_862),
    (POSITION_3, 1, 14),
    (POSITION_3, 2, 191),
    (POSITION_3, 3, 2_812),
    (POSITION_3, 4, 43_238),
    (POSITION_4, 1, 6),
    (POSITION_4, 2, 264),
    (POSITION_4, 3, 9_467),
    (POSITION_4_MIRRORED, 1, 6),
    (POSITION_4_MIRRORED, 2, 264),
    (POSITION_4_MIRRORED, 3, 9_467),
    (POSITION_5, 1, 44),
    (POSITION_5, 2, 1_486),
    (POSITION_5, 3, 62_379),
]

SLOW_CASES = [
    (STARTING_FEN, 5, 4_865_609),
    (KIWIPETE, 4, 4_085_603),
    (POSITION_3, 5, 674_624),
    (POSITION_4, 4, 422_333),
    (POSITION_5, 4, 2_103_487),
]


@pytest.mark.parametrize("fen, depth, expected", CASES)
def test_perft(fen, depth, expected):
    assert perft(parse_fen(fen), depth) == expected


@pytest.mark.slow
@pytest.mark.parametrize("fen, depth, expected", SLOW_CASES)
def test_perft_deep(fen, depth, expected):
    assert perft(parse_fen(fen), depth) == expected
