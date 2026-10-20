"""How a game ends by the rules: checkmate, stalemate, and the automatic draws.

Resignation, draw agreement, and abandonment aren't board rules, so the server
decides those.
"""

from collections import Counter
from dataclasses import dataclass

from .board import Board
from .fen import placement_field
from .movegen import in_check, legal_moves
from .squares import BLACK, SQUARES, WHITE, file_of, rank_of, square_name

WHITE_WINS = "1-0"
BLACK_WINS = "0-1"
DRAW = "1/2-1/2"

CHECKMATE = "checkmate"
STALEMATE = "stalemate"
INSUFFICIENT_MATERIAL = "insufficient_material"
FIFTY_MOVE = "fifty_move"
THREEFOLD_REPETITION = "threefold_repetition"


@dataclass(frozen=True, slots=True)
class Outcome:
    result: str  # WHITE_WINS, BLACK_WINS, or DRAW
    reason: str

    @property
    def winner(self) -> str | None:
        if self.result == WHITE_WINS:
            return WHITE
        if self.result == BLACK_WINS:
            return BLACK
        return None


def game_outcome(board: Board, position_counts: Counter | None = None) -> Outcome | None:
    """Return how the game has ended in this position, or ``None`` if it goes on.

    ``position_counts`` maps ``repetition_key`` values to how often each
    position has occurred; without it threefold repetition isn't checked.
    Checkmate and stalemate are checked first, so a move that mates wins even
    if it also reaches the fifty-move limit.
    """
    if not legal_moves(board):
        if in_check(board):
            return Outcome(BLACK_WINS if board.turn == WHITE else WHITE_WINS, CHECKMATE)
        return Outcome(DRAW, STALEMATE)
    if insufficient_material(board):
        return Outcome(DRAW, INSUFFICIENT_MATERIAL)
    if position_counts is not None and position_counts[repetition_key(board)] >= 3:
        return Outcome(DRAW, THREEFOLD_REPETITION)
    if board.halfmove_clock >= 100:
        return Outcome(DRAW, FIFTY_MOVE)
    return None


def insufficient_material(board: Board) -> bool:
    """True if neither side can ever checkmate.

    That's the case for K vs K, K + one minor piece vs K, and any position
    where the only other pieces are bishops that all stand on one square color.
    """
    minors = []
    for sq in SQUARES:
        piece = board.squares[sq]
        if piece is None or piece in "Kk":
            continue
        if piece in "PpRrQq":
            return False
        minors.append((piece, sq))
    if len(minors) <= 1:
        return True
    if all(piece in "Bb" for piece, _ in minors):
        colors = {(file_of(sq) + rank_of(sq)) % 2 for _, sq in minors}
        return len(colors) == 1
    return False


def repetition_key(board: Board) -> str:
    """What makes two positions "the same" for threefold repetition.

    Pieces, side to move, and castling rights must match. The en passant square
    only counts when an en passant capture is actually legal, since otherwise it
    doesn't change what either player can do.
    """
    ep = "-"
    if board.ep_square is not None and any(m.is_en_passant for m in legal_moves(board)):
        ep = square_name(board.ep_square)
    return f"{placement_field(board)} {board.turn} {board.castling or '-'} {ep}"
