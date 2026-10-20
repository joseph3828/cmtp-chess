import pytest

from chessrules import (
    BLACK, BLACK_WINS, CHECKMATE, DRAW, FIFTY_MOVE, INSUFFICIENT_MATERIAL, STALEMATE,
    THREEFOLD_REPETITION, WHITE, WHITE_WINS, ChessGame, GameOverError, Outcome,
    insufficient_material, parse_fen, repetition_key,
)


def play(*moves, fen=None):
    game = ChessGame(fen) if fen else ChessGame()
    for move in moves:
        game.play(move)
    return game


def test_fools_mate():
    game = play("f2f3", "e7e5", "g2g4", "d8h4")
    assert game.outcome == Outcome(BLACK_WINS, CHECKMATE)
    assert game.outcome.winner == BLACK


def test_scholars_mate():
    game = play("e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7")
    assert game.outcome == Outcome(WHITE_WINS, CHECKMATE)
    assert game.outcome.winner == WHITE


def test_stalemate():
    game = play("f2f7", fen="7k/8/6K1/8/8/8/5Q2/8 w - - 0 1")
    assert game.outcome == Outcome(DRAW, STALEMATE)
    assert game.outcome.winner is None


def test_game_can_start_already_finished():
    assert ChessGame("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1").outcome == Outcome(DRAW, STALEMATE)


@pytest.mark.parametrize(
    "fen, expected",
    [
        ("8/8/4k3/8/8/4K3/8/8 w - - 0 1", True),  # K vs K
        ("8/8/4k3/8/8/4K3/4B3/8 w - - 0 1", True),  # K+B vs K
        ("8/8/4k3/8/8/4K3/4N3/8 w - - 0 1", True),  # K+N vs K
        ("5b2/8/4k3/8/8/4K3/8/2B5 w - - 0 1", True),  # bishops on the same color
        ("2b5/8/4k3/8/8/4K3/8/2B5 w - - 0 1", False),  # bishops on opposite colors
        ("8/8/4k3/8/8/4K3/3NN3/8 w - - 0 1", False),  # K+N+N vs K
        ("n7/8/4k3/8/8/4K3/3N4/8 w - - 0 1", False),  # K+N vs K+N
        ("8/8/4k3/8/8/4K3/4P3/8 w - - 0 1", False),
        ("8/8/4k3/8/8/4K3/4R3/8 w - - 0 1", False),
    ],
)
def test_insufficient_material(fen, expected):
    assert insufficient_material(parse_fen(fen)) is expected


def test_draw_when_last_piece_is_captured():
    game = play("e1d2", fen="4k3/8/8/8/8/8/3q4/4K3 w - - 0 1")
    assert game.outcome == Outcome(DRAW, INSUFFICIENT_MATERIAL)


def test_fifty_move_rule():
    assert play("a1a2", fen="7k/8/8/8/8/8/8/R3K3 w - - 98 80").outcome is None
    assert play("a1a2", fen="7k/8/8/8/8/8/8/R3K3 w - - 99 80").outcome == Outcome(
        DRAW, FIFTY_MOVE
    )


def test_checkmate_beats_the_fifty_move_rule():
    game = play("a1a8", fen="7k/8/6K1/8/8/8/8/R7 w - - 99 80")
    assert game.outcome == Outcome(WHITE_WINS, CHECKMATE)


def test_threefold_repetition():
    shuffle = ["g1f3", "g8f6", "f3g1", "f6g8"]
    game = play(*shuffle)  # the starting position has now occurred twice
    assert game.outcome is None
    for move in shuffle[:-1]:
        game.play(move)
    assert game.outcome is None
    game.play(shuffle[-1])  # and now three times
    assert game.outcome == Outcome(DRAW, THREEFOLD_REPETITION)


def test_repetition_key_ignores_en_passant_square_nobody_can_use():
    with_ep = parse_fen("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1")
    without_ep = parse_fen("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1")
    assert repetition_key(with_ep) == repetition_key(without_ep)


def test_repetition_key_keeps_en_passant_square_that_can_be_used():
    with_ep = parse_fen("rnbqkbnr/ppp1pppp/8/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 3")
    without_ep = parse_fen("rnbqkbnr/ppp1pppp/8/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq - 0 3")
    assert repetition_key(with_ep) != repetition_key(without_ep)


def test_no_moves_after_the_game_ends():
    game = play("f2f3", "e7e5", "g2g4", "d8h4")
    assert game.legal_moves() == []
    with pytest.raises(GameOverError):
        game.play("e2e4")
