"""Reading and writing positions as FEN.

FEN has six space-separated fields: piece placement, side to move, castling
rights, en passant square, halfmove clock, and fullmove number. For example,
the starting position is ``STARTING_FEN``.
"""

import re

from .board import Board
from .movegen import is_attacked
from .squares import (
    A1, A8, BLACK, E1, E8, H1, H8, WHITE,
    opposite, parse_square, rank_of, square, square_name,
)

STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

_PIECE_LETTERS = "PNBRQKpnbrqk"
_NUMBER = re.compile(r"[0-9]+")

# Each castling right needs the king and that rook still on their starting squares.
_CASTLING_PIECES = {
    "K": ((E1, "K"), (H1, "R")),
    "Q": ((E1, "K"), (A1, "R")),
    "k": ((E8, "k"), (H8, "r")),
    "q": ((E8, "k"), (A8, "r")),
}


class FenError(ValueError):
    """The text isn't a valid FEN for a legal position."""


def parse_fen(text: str) -> Board:
    fields = text.split()
    if len(fields) != 6:
        raise FenError(f"FEN needs 6 fields, got {len(fields)}")
    placement, turn, castling, ep, halfmove, fullmove = fields

    board = Board()
    _parse_placement(board, placement)

    if turn not in (WHITE, BLACK):
        raise FenError(f"side to move must be 'w' or 'b', got {turn!r}")
    board.turn = turn

    board.castling = _parse_castling(board, castling)
    board.ep_square = _parse_ep_square(board, ep)

    if not _NUMBER.fullmatch(halfmove):
        raise FenError(f"bad halfmove clock: {halfmove!r}")
    if not _NUMBER.fullmatch(fullmove) or int(fullmove) < 1:
        raise FenError(f"bad fullmove number: {fullmove!r}")
    board.halfmove_clock = int(halfmove)
    board.fullmove_number = int(fullmove)

    waiting = opposite(board.turn)
    if is_attacked(board, board.king_squares[waiting], board.turn):
        raise FenError("the side not to move is in check")
    return board


def _parse_placement(board: Board, placement: str) -> None:
    rows = placement.split("/")
    if len(rows) != 8:
        raise FenError(f"piece placement needs 8 ranks, got {len(rows)}")
    for index, row in enumerate(rows):
        rank = 7 - index
        file = 0
        for ch in row:
            if ch in "12345678":
                file += int(ch)
            elif ch in _PIECE_LETTERS:
                if file > 7:
                    raise FenError(f"rank {rank + 1} has more than 8 squares")
                if ch in "Pp" and rank in (0, 7):
                    raise FenError("a pawn can't stand on the first or last rank")
                if ch == "K" and WHITE in board.king_squares:
                    raise FenError("white has more than one king")
                if ch == "k" and BLACK in board.king_squares:
                    raise FenError("black has more than one king")
                board.set_piece(square(file, rank), ch)
                file += 1
            else:
                raise FenError(f"unexpected character {ch!r} in piece placement")
        if file != 8:
            raise FenError(f"rank {rank + 1} has {file} squares, not 8")
    for color, name in ((WHITE, "white"), (BLACK, "black")):
        if color not in board.king_squares:
            raise FenError(f"{name} has no king")


def _parse_castling(board: Board, castling: str) -> str:
    if castling == "-":
        return ""
    if any(c not in "KQkq" for c in castling) or len(set(castling)) != len(castling):
        raise FenError(f"bad castling field: {castling!r}")
    for right in castling:
        for sq, piece in _CASTLING_PIECES[right]:
            if board.squares[sq] != piece:
                raise FenError(f"castling right {right!r} needs {piece} on {square_name(sq)}")
    return "".join(c for c in "KQkq" if c in castling)


def _parse_ep_square(board: Board, ep: str) -> int | None:
    if ep == "-":
        return None
    try:
        sq = parse_square(ep)
    except ValueError:
        raise FenError(f"bad en passant square: {ep!r}") from None
    # White to move means black just double-pushed, so the square is on rank 6.
    if board.turn == WHITE:
        expected_rank, pushed, pawn = 5, sq - 16, "p"
    else:
        expected_rank, pushed, pawn = 2, sq + 16, "P"
    if rank_of(sq) != expected_rank:
        raise FenError(f"en passant square {ep} is on the wrong rank")
    if board.squares[sq] is not None or board.squares[pushed] != pawn:
        raise FenError(f"en passant square {ep} doesn't follow a double pawn push")
    return sq


def placement_field(board: Board) -> str:
    """Return just the piece placement part of the FEN."""
    rows = []
    for rank in range(7, -1, -1):
        row = ""
        empty = 0
        for file in range(8):
            piece = board.squares[square(file, rank)]
            if piece is None:
                empty += 1
                continue
            if empty:
                row += str(empty)
                empty = 0
            row += piece
        if empty:
            row += str(empty)
        rows.append(row)
    return "/".join(rows)


def to_fen(board: Board) -> str:
    ep = square_name(board.ep_square) if board.ep_square is not None else "-"
    return " ".join((
        placement_field(board),
        board.turn,
        board.castling or "-",
        ep,
        str(board.halfmove_clock),
        str(board.fullmove_number),
    ))
