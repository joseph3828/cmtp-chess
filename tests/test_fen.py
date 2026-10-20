import pytest

from chessrules import STARTING_FEN, FenError, parse_fen, parse_square, to_fen

ROUND_TRIP = [
    STARTING_FEN,
    "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
    "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2",
    "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
    "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1",
    "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8",
    "4k3/8/8/8/8/8/8/4K2R w K - 37 90",
]


@pytest.mark.parametrize("fen", ROUND_TRIP)
def test_round_trip(fen):
    assert to_fen(parse_fen(fen)) == fen


def test_starting_position_contents():
    board = parse_fen(STARTING_FEN)
    assert board.piece_at(parse_square("e1")) == "K"
    assert board.piece_at(parse_square("d8")) == "q"
    assert board.piece_at(parse_square("e4")) is None
    assert board.turn == "w"
    assert board.castling == "KQkq"
    assert board.ep_square is None
    assert board.king_squares == {"w": parse_square("e1"), "b": parse_square("e8")}


def test_castling_rights_are_put_in_standard_order():
    board = parse_fen("r3k2r/8/8/8/8/8/8/R3K2R w qkQK - 0 1")
    assert board.castling == "KQkq"


@pytest.mark.parametrize(
    "fen",
    [
        "",
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -",  # missing fields
        "rnbqkbnr/pppppppp/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",  # 7 ranks
        "rnbqkbnr/pppppppp/9/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",  # bad digit
        "rnbqkbnr/ppppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",  # 9 squares
        "rnbqkbnr/ppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",  # 7 squares
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNX w KQkq - 0 1",  # bad piece
        "rnbq1bnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQ - 0 1",  # no black king
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBKKBNR w kq - 0 1",  # two white kings
        "rnbqkbnP/pppppppp/8/8/8/8/PPPPPPP1/RNBQKBNR w KQq - 0 1",  # pawn on 8th rank
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR x KQkq - 0 1",  # bad side to move
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkx - 0 1",  # bad castling letter
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KKkq - 0 1",  # repeated right
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBN1 w KQkq - 0 1",  # right without rook
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e4 0 1",  # ep wrong rank
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq e3 0 1",  # ep with no pawn
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - -1 1",  # negative clock
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 0",  # fullmove 0
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1 extra",  # 7 fields
        "4k3/8/8/8/8/8/4R3/4K3 w - - 0 1",  # black, not to move, is in check
    ],
)
def test_invalid_fen(fen):
    with pytest.raises(FenError):
        parse_fen(fen)
