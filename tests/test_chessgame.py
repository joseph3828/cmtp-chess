import pytest

from chessrules import (
    BLACK, STARTING_FEN, WHITE, ChessGame, IllegalMoveError, MoveError, MoveFormatError,
    PlayedMove, parse_fen,
)


def test_new_game():
    game = ChessGame()
    assert game.fen() == STARTING_FEN
    assert game.start_fen == STARTING_FEN
    assert game.ply == 0
    assert game.turn == WHITE
    assert not game.is_over
    assert len(game.legal_uci()) == 20


def test_play_returns_the_move_record():
    game = ChessGame()
    played = game.play("e2e4")
    assert played == PlayedMove(
        uci="e2e4",
        san="e4",
        fen="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
    )
    assert game.ply == 1
    assert game.turn == BLACK
    assert game.history == [played]


@pytest.mark.parametrize("text", ["", "e2", "e2e9", "i2i4", "E2E4", "e2e4 ", "e7e8k", "e2-e4"])
def test_badly_formatted_moves(text):
    with pytest.raises(MoveFormatError):
        ChessGame().play(text)


@pytest.mark.parametrize(
    "text",
    [
        "e2e5",  # pawn can't move three squares
        "e7e5",  # black's pawn, but white is to move
        "e1e2",  # blocked by own pawn
        "e3e4",  # no piece on e3
        "e2e4q",  # promotion piece on a move that doesn't promote
    ],
)
def test_illegal_moves(text):
    with pytest.raises(IllegalMoveError):
        ChessGame().play(text)


def test_promotion_needs_a_piece():
    game = ChessGame("8/P7/8/8/8/8/8/k6K w - - 0 1")
    with pytest.raises(IllegalMoveError):
        game.play("a7a8")
    assert game.play("a7a8q").san == "a8=Q+"  # the new queen checks down the a-file


def test_rejected_move_changes_nothing():
    game = ChessGame()
    with pytest.raises(MoveError):
        game.play("e2e5")
    assert game.fen() == STARTING_FEN
    assert game.ply == 0


def test_move_errors_are_value_errors():
    assert issubclass(MoveError, ValueError)
    assert issubclass(MoveFormatError, MoveError)
    assert issubclass(IllegalMoveError, MoveError)


def test_board_text():
    lines = str(parse_fen(STARTING_FEN)).splitlines()
    assert lines[0] == "8  r n b q k b n r"
    assert lines[7] == "1  R N B Q K B N R"
    assert lines[8] == "   a b c d e f g h"
