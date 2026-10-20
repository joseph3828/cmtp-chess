"""The position: piece placement plus the state FEN records alongside it."""

from .move import CASTLE, DOUBLE_PUSH, EN_PASSANT, Move
from .squares import (
    A1, A8, BLACK, E1, E8, FILES, H1, H8, RANKS, WHITE,
    color_of, file_of, opposite, square,
)

# Moving a piece from, or capturing on, one of these squares loses the listed
# castling rights. The king's square loses both rights for that side.
_CASTLING_LOST = {E1: "KQ", H1: "K", A1: "Q", E8: "kq", H8: "k", A8: "q"}


class Board:
    """A chess position on a 0x88 board.

    ``make_move`` trusts its move: legality is checked in ``movegen``, which
    only hands out moves that are safe to make.
    """

    __slots__ = (
        "squares", "turn", "castling", "ep_square",
        "halfmove_clock", "fullmove_number", "king_squares",
    )

    def __init__(self) -> None:
        self.squares: list[str | None] = [None] * 128
        self.turn = WHITE
        self.castling = ""  # a subset of "KQkq", kept in that order
        self.ep_square: int | None = None  # the square a pawn just skipped over
        self.halfmove_clock = 0  # half-moves since the last capture or pawn move
        self.fullmove_number = 1
        self.king_squares: dict[str, int] = {}

    def piece_at(self, sq: int) -> str | None:
        return self.squares[sq]

    def set_piece(self, sq: int, piece: str | None) -> None:
        self.squares[sq] = piece
        if piece in ("K", "k"):
            self.king_squares[color_of(piece)] = sq

    def copy(self) -> "Board":
        clone = Board.__new__(Board)
        clone.squares = self.squares[:]
        clone.turn = self.turn
        clone.castling = self.castling
        clone.ep_square = self.ep_square
        clone.halfmove_clock = self.halfmove_clock
        clone.fullmove_number = self.fullmove_number
        clone.king_squares = dict(self.king_squares)
        return clone

    def make_move(self, move: Move) -> None:
        squares = self.squares
        color = self.turn
        piece = squares[move.from_sq]
        captured = squares[move.to_sq]

        if piece in ("P", "p") or captured is not None:
            self.halfmove_clock = 0
        else:
            self.halfmove_clock += 1

        squares[move.from_sq] = None
        if move.promotion:
            piece = move.promotion.upper() if color == WHITE else move.promotion
        squares[move.to_sq] = piece

        if move.flags & EN_PASSANT:
            # The captured pawn is beside the moving pawn, not on the target square.
            behind = move.to_sq - 16 if color == WHITE else move.to_sq + 16
            squares[behind] = None
        elif move.flags & CASTLE:
            if file_of(move.to_sq) == 6:  # king side: rook h -> f
                rook_from, rook_to = move.from_sq + 3, move.from_sq + 1
            else:  # queen side: rook a -> d
                rook_from, rook_to = move.from_sq - 4, move.from_sq - 1
            squares[rook_to] = squares[rook_from]
            squares[rook_from] = None

        if piece in ("K", "k"):
            self.king_squares[color] = move.to_sq

        if self.castling:
            for sq in (move.from_sq, move.to_sq):
                lost = _CASTLING_LOST.get(sq)
                if lost:
                    self.castling = "".join(c for c in self.castling if c not in lost)

        if move.flags & DOUBLE_PUSH:
            self.ep_square = (move.from_sq + move.to_sq) // 2
        else:
            self.ep_square = None

        if color == BLACK:
            self.fullmove_number += 1
        self.turn = opposite(color)

    def __str__(self) -> str:
        """Draw the board as text, white at the bottom, for debugging and the CLI."""
        lines = []
        for rank in range(7, -1, -1):
            row = " ".join(self.squares[square(f, rank)] or "." for f in range(8))
            lines.append(f"{RANKS[rank]}  {row}")
        lines.append("   " + " ".join(FILES))
        return "\n".join(lines)
