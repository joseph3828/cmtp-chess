"""Square numbering and piece helpers for the 0x88 board.

A square is ``rank * 16 + file``, where rank 0 is chess rank "1" and file 0 is
file "a". The board list has 128 entries, but only the left half of each row of
16 is real: any index with a bit set in ``0x88`` is off the board. That lets
move generation step from a square and check ``target & 0x88`` to know whether
it walked off an edge, including wrapping around from the h-file to the a-file.
"""

WHITE = "w"
BLACK = "b"

FILES = "abcdefgh"
RANKS = "12345678"

WHITE_PIECES = "PNBRQK"
BLACK_PIECES = "pnbrqk"


def square(file: int, rank: int) -> int:
    """Return the 0x88 index of a square from 0-based file and rank numbers."""
    return rank * 16 + file


def file_of(sq: int) -> int:
    return sq & 7


def rank_of(sq: int) -> int:
    return sq >> 4


def on_board(sq: int) -> bool:
    """True if ``sq`` is a real square.

    Valid for any index reached by stepping from a board square, including
    negative ones, because every such off-board index has bit 3 or bit 7 set.
    """
    return not sq & 0x88


def square_name(sq: int) -> str:
    """Return the algebraic name of a square, such as ``"e4"``."""
    return FILES[file_of(sq)] + RANKS[rank_of(sq)]


def parse_square(name: str) -> int:
    """Return the 0x88 index for an algebraic square name such as ``"e4"``."""
    if len(name) != 2 or name[0] not in FILES or name[1] not in RANKS:
        raise ValueError(f"not a square name: {name!r}")
    return square(FILES.index(name[0]), RANKS.index(name[1]))


def color_of(piece: str) -> str:
    """Return ``WHITE`` for an uppercase piece letter and ``BLACK`` for lowercase."""
    return WHITE if piece.isupper() else BLACK


def opposite(color: str) -> str:
    return BLACK if color == WHITE else WHITE


# The 64 real squares, a1, b1, ..., h1, a2, ..., h8.
SQUARES = tuple(square(f, r) for r in range(8) for f in range(8))

E1, A1, H1 = square(4, 0), square(0, 0), square(7, 0)
E8, A8, H8 = square(4, 7), square(0, 7), square(7, 7)
