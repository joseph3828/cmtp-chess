"""Standard Algebraic Notation (SAN), the move text people read: ``Nf3``, ``exd5``, ``O-O``."""

from .board import Board
from .move import Move
from .movegen import in_check, legal_moves
from .squares import FILES, RANKS, file_of, rank_of, square_name


def to_san(board: Board, move: Move, legal: list[Move] | None = None) -> str:
    """Write ``move`` as SAN. ``move`` must be legal in ``board``.

    ``legal`` can pass in the position's legal moves if the caller already has
    them; they're needed to tell apart two pieces that can reach one square.
    """
    if legal is None:
        legal = legal_moves(board)
    piece = board.squares[move.from_sq]
    kind = piece.upper()
    target = square_name(move.to_sq)

    if move.is_castle:
        san = "O-O" if file_of(move.to_sq) == 6 else "O-O-O"
    elif kind == "P":
        san = FILES[file_of(move.from_sq)] + "x" + target if move.is_capture else target
        if move.promotion:
            san += "=" + move.promotion.upper()
    else:
        san = kind + _disambiguation(board, move, piece, legal)
        san += ("x" if move.is_capture else "") + target

    after = board.copy()
    after.make_move(move)
    if in_check(after):
        san += "#" if not legal_moves(after) else "+"
    return san


def parse_san(board: Board, text: str, legal: list[Move] | None = None) -> Move:
    """Find the legal move that ``text`` names in SAN, such as ``"Nf3"`` or ``"exd5"``.

    The ``+`` or ``#`` at the end is optional, and castling may be written with
    zeros (``0-0``). Raises ``ValueError`` if no legal move matches, including
    when the text leaves out a needed file or rank (``Nd7`` with two knights able
    to go there).
    """
    if legal is None:
        legal = legal_moves(board)
    wanted = _normalize_san(text)
    for move in legal:
        if _normalize_san(to_san(board, move, legal)) == wanted:
            return move
    raise ValueError(f"{text!r} is not a legal move here")


def _normalize_san(text: str) -> str:
    text = text.strip().rstrip("+#!?")
    if text in ("0-0", "0-0-0"):
        text = text.replace("0", "O")
    return text


def _disambiguation(board: Board, move: Move, piece: str, legal: list[Move]) -> str:
    """The file, rank, or square to add when another piece of the same type can also reach the target."""
    rivals = [
        m.from_sq for m in legal
        if m.to_sq == move.to_sq and m.from_sq != move.from_sq and board.squares[m.from_sq] == piece
    ]
    if not rivals:
        return ""
    from_file, from_rank = file_of(move.from_sq), rank_of(move.from_sq)
    if all(file_of(sq) != from_file for sq in rivals):
        return FILES[from_file]
    if all(rank_of(sq) != from_rank for sq in rivals):
        return RANKS[from_rank]
    return square_name(move.from_sq)
