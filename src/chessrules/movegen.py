"""Attack detection, move generation, and perft.

Legal moves come from two steps. First every pseudo-legal move is generated:
moves that follow how each piece moves but may leave the mover's own king in
check. Then each one is tried on a copy of the board and kept only if the
king is safe afterward. That second step handles pins, check evasions, and
the rare en passant capture that uncovers an attack along a rank.
"""

from .board import Board
from .move import CAPTURE, CASTLE, DOUBLE_PUSH, EN_PASSANT, PROMOTION_PIECES, Move
from .squares import BLACK, E1, E8, SQUARES, WHITE, opposite, rank_of

KNIGHT_STEPS = (33, 31, 18, 14, -14, -18, -31, -33)
KING_STEPS = (1, 15, 16, 17, -1, -15, -16, -17)
ROOK_DIRECTIONS = (1, -1, 16, -16)
BISHOP_DIRECTIONS = (15, 17, -15, -17)
QUEEN_DIRECTIONS = ROOK_DIRECTIONS + BISHOP_DIRECTIONS


def is_attacked(board: Board, sq: int, by_color: str) -> bool:
    """True if any piece of ``by_color`` attacks ``sq``.

    Works backward from the square: it looks where an attacker would have to
    stand, instead of generating every enemy move.
    """
    squares = board.squares
    if by_color == WHITE:
        pawn, knight, bishop, rook, queen, king = "PNBRQK"
        pawn_sources = (sq - 15, sq - 17)  # white pawns attack upward
    else:
        pawn, knight, bishop, rook, queen, king = "pnbrqk"
        pawn_sources = (sq + 15, sq + 17)

    for src in pawn_sources:
        if not src & 0x88 and squares[src] == pawn:
            return True
    for step in KNIGHT_STEPS:
        src = sq + step
        if not src & 0x88 and squares[src] == knight:
            return True
    for step in KING_STEPS:
        src = sq + step
        if not src & 0x88 and squares[src] == king:
            return True
    for direction in ROOK_DIRECTIONS:
        src = sq + direction
        while not src & 0x88:
            piece = squares[src]
            if piece is not None:
                if piece == rook or piece == queen:
                    return True
                break
            src += direction
    for direction in BISHOP_DIRECTIONS:
        src = sq + direction
        while not src & 0x88:
            piece = squares[src]
            if piece is not None:
                if piece == bishop or piece == queen:
                    return True
                break
            src += direction
    return False


def in_check(board: Board, color: str | None = None) -> bool:
    """True if ``color``'s king (by default the side to move) is attacked."""
    color = color or board.turn
    return is_attacked(board, board.king_squares[color], opposite(color))


def pseudo_legal_moves(board: Board) -> list[Move]:
    moves: list[Move] = []
    squares = board.squares
    white = board.turn == WHITE
    for sq in SQUARES:
        piece = squares[sq]
        if piece is None or piece.isupper() != white:
            continue
        kind = piece.lower()
        if kind == "p":
            _pawn_moves(board, sq, white, moves)
        elif kind == "n":
            _step_moves(squares, sq, KNIGHT_STEPS, white, moves)
        elif kind == "b":
            _slide_moves(squares, sq, BISHOP_DIRECTIONS, white, moves)
        elif kind == "r":
            _slide_moves(squares, sq, ROOK_DIRECTIONS, white, moves)
        elif kind == "q":
            _slide_moves(squares, sq, QUEEN_DIRECTIONS, white, moves)
        else:
            _step_moves(squares, sq, KING_STEPS, white, moves)
            _castling_moves(board, sq, white, moves)
    return moves


def _step_moves(squares, sq, steps, white, moves):
    for step in steps:
        to = sq + step
        if to & 0x88:
            continue
        target = squares[to]
        if target is None:
            moves.append(Move(sq, to))
        elif target.isupper() != white:
            moves.append(Move(sq, to, flags=CAPTURE))


def _slide_moves(squares, sq, directions, white, moves):
    for direction in directions:
        to = sq + direction
        while not to & 0x88:
            target = squares[to]
            if target is None:
                moves.append(Move(sq, to))
            else:
                if target.isupper() != white:
                    moves.append(Move(sq, to, flags=CAPTURE))
                break
            to += direction


def _pawn_moves(board, sq, white, moves):
    squares = board.squares
    forward = 16 if white else -16
    start_rank = 1 if white else 6

    one = sq + forward
    if squares[one] is None:
        _add_pawn_move(sq, one, 0, moves)
        two = one + forward
        if rank_of(sq) == start_rank and squares[two] is None:
            moves.append(Move(sq, two, flags=DOUBLE_PUSH))

    for to in (one - 1, one + 1):
        if to & 0x88:
            continue
        target = squares[to]
        if target is not None:
            if target.isupper() != white:
                _add_pawn_move(sq, to, CAPTURE, moves)
        elif to == board.ep_square:
            moves.append(Move(sq, to, flags=CAPTURE | EN_PASSANT))


def _add_pawn_move(from_sq, to_sq, flags, moves):
    if rank_of(to_sq) in (0, 7):
        for piece in PROMOTION_PIECES:
            moves.append(Move(from_sq, to_sq, piece, flags))
    else:
        moves.append(Move(from_sq, to_sq, flags=flags))


def _castling_moves(board, sq, white, moves):
    rights = board.castling
    if not rights:
        return
    if white:
        if sq != E1:
            return
        king_side, queen_side, rook, enemy = "K", "Q", "R", BLACK
    else:
        if sq != E8:
            return
        king_side, queen_side, rook, enemy = "k", "q", "r", WHITE
    if king_side not in rights and queen_side not in rights:
        return

    squares = board.squares
    # The king can't castle out of check, through an attacked square, or into
    # check. The rook's path only has to be empty, so b1/b8 may be attacked.
    if is_attacked(board, sq, enemy):
        return
    if (
        king_side in rights
        and squares[sq + 1] is None
        and squares[sq + 2] is None
        and squares[sq + 3] == rook
        and not is_attacked(board, sq + 1, enemy)
        and not is_attacked(board, sq + 2, enemy)
    ):
        moves.append(Move(sq, sq + 2, flags=CASTLE))
    if (
        queen_side in rights
        and squares[sq - 1] is None
        and squares[sq - 2] is None
        and squares[sq - 3] is None
        and squares[sq - 4] == rook
        and not is_attacked(board, sq - 1, enemy)
        and not is_attacked(board, sq - 2, enemy)
    ):
        moves.append(Move(sq, sq - 2, flags=CASTLE))


def legal_moves_with_positions(board: Board) -> list[tuple[Move, Board]]:
    """Every legal move, paired with the position it leads to."""
    color = board.turn
    enemy = opposite(color)
    result = []
    for move in pseudo_legal_moves(board):
        child = board.copy()
        child.make_move(move)
        if not is_attacked(child, child.king_squares[color], enemy):
            result.append((move, child))
    return result


def legal_moves(board: Board) -> list[Move]:
    return [move for move, _ in legal_moves_with_positions(board)]


def perft(board: Board, depth: int) -> int:
    """Count the move sequences of exactly ``depth`` half-moves from ``board``.

    Published perft counts for standard positions make this the main check
    that move generation is correct.
    """
    if depth == 0:
        return 1
    children = legal_moves_with_positions(board)
    if depth == 1:
        return len(children)
    return sum(perft(child, depth - 1) for _, child in children)


def perft_divide(board: Board, depth: int) -> dict[str, int]:
    """Perft split by first move, for finding which move a wrong count comes from."""
    return {
        move.uci(): perft(child, depth - 1)
        for move, child in legal_moves_with_positions(board)
    }
