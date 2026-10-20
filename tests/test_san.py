import pytest

from chessrules import STARTING_FEN, ChessGame, parse_fen, parse_san


def sans(*moves, fen=STARTING_FEN):
    game = ChessGame(fen)
    return [game.play(move).san for move in moves]


def san(fen, move):
    return ChessGame(fen).play(move).san


@pytest.mark.parametrize(
    "fen, move, expected",
    [
        (STARTING_FEN, "e2e4", "e4"),
        (STARTING_FEN, "g1f3", "Nf3"),
        ("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 2", "e4d5", "exd5"),
        # Two knights reach d7: the file tells them apart.
        ("1n2k3/8/5n2/8/8/8/8/4K3 b - - 0 1", "b8d7", "Nbd7"),
        ("1n2k3/8/5n2/8/8/8/8/4K3 b - - 0 1", "f6d7", "Nfd7"),
        # Two rooks on the a-file reach a3: the rank tells them apart.
        ("7k/8/8/R7/8/8/8/R6K w - - 0 1", "a1a3", "R1a3"),
        ("7k/8/8/R7/8/8/8/R6K w - - 0 1", "a5a3", "R5a3"),
        ("7k/8/8/R7/8/8/8/R6K w - - 0 1", "a1b1", "Rb1"),
        # Three queens reach e1: sometimes only the full square is enough.
        ("8/k7/8/8/4Q2Q/8/8/2K4Q w - - 0 1", "h4e1", "Qh4e1"),
        ("8/k7/8/8/4Q2Q/8/8/2K4Q w - - 0 1", "e4e1", "Qee1"),
        ("8/k7/8/8/4Q2Q/8/8/2K4Q w - - 0 1", "h1e1", "Q1e1"),
        ("8/4P3/8/8/8/8/k7/4K3 w - - 0 1", "e7e8q", "e8=Q"),
        ("8/4P3/8/8/8/8/k7/4K3 w - - 0 1", "e7e8n", "e8=N"),
        ("3r3k/4P3/8/8/8/8/8/K7 w - - 0 1", "e7d8q", "exd8=Q+"),
        ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1", "O-O"),
        ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1c1", "O-O-O"),
    ],
)
def test_san(fen, move, expected):
    assert san(fen, move) == expected


def test_check_suffix():
    assert sans("e2e4", "d7d6", "f1b5") == ["e4", "d6", "Bb5+"]


def test_checkmate_suffix():
    assert sans("f2f3", "e7e5", "g2g4", "d8h4")[-1] == "Qh4#"
    assert sans("e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7")[-1] == "Qxf7#"


def test_en_passant_san():
    assert sans("e2e4", "a7a6", "e4e5", "d7d5", "e5d6")[-1] == "exd6"


# Reading SAN back into moves

SAN_CASES = [
    (STARTING_FEN, "e4", "e2e4"),
    (STARTING_FEN, "Nf3", "g1f3"),
    ("1n2k3/8/5n2/8/8/8/8/4K3 b - - 0 1", "Nbd7", "b8d7"),
    ("7k/8/8/R7/8/8/8/R6K w - - 0 1", "R5a3", "a5a3"),
    ("8/k7/8/8/4Q2Q/8/8/2K4Q w - - 0 1", "Qh4e1", "h4e1"),
    ("3r3k/4P3/8/8/8/8/8/K7 w - - 0 1", "exd8=Q+", "e7d8q"),
    ("3r3k/4P3/8/8/8/8/8/K7 w - - 0 1", "exd8=N", "e7d8n"),
    ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "O-O-O", "e1c1"),
]


@pytest.mark.parametrize("fen, text, uci", SAN_CASES)
def test_parse_san(fen, text, uci):
    assert parse_san(parse_fen(fen), text).uci() == uci


def test_check_marks_are_optional():
    # The full SAN is exd8=Q+, but players often leave off the check mark.
    assert parse_san(parse_fen("3r3k/4P3/8/8/8/8/8/K7 w - - 0 1"), "exd8=Q").uci() == "e7d8q"
    # Annotations people add are ignored too.
    assert parse_san(parse_fen(STARTING_FEN), "Nf3!").uci() == "g1f3"


def test_castling_with_zeros():
    board = parse_fen("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    assert parse_san(board, "0-0").uci() == "e1g1"
    assert parse_san(board, "0-0-0").uci() == "e1c1"


@pytest.mark.parametrize(
    "fen, text",
    [
        (STARTING_FEN, "e5"),  # not reachable
        (STARTING_FEN, "Nf6"),  # black's move, but white is to move
        (STARTING_FEN, "nf3"),  # piece letters are capitals
        ("1n2k3/8/5n2/8/8/8/8/4K3 b - - 0 1", "Nd7"),  # two knights can go there
        (STARTING_FEN, ""),
    ],
)
def test_parse_san_rejects(fen, text):
    with pytest.raises(ValueError):
        parse_san(parse_fen(fen), text)
