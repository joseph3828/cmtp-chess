"""Chess rules engine, written from scratch. Used by both the server and the client."""

from .board import Board
from .fen import STARTING_FEN, FenError, parse_fen, to_fen
from .game import (
    ChessGame, GameOverError, IllegalMoveError, MoveError, MoveFormatError, PlayedMove,
)
from .move import Move, parse_uci
from .movegen import in_check, is_attacked, legal_moves, perft, perft_divide
from .san import parse_san, to_san
from .squares import BLACK, WHITE, parse_square, square_name
from .status import (
    BLACK_WINS, CHECKMATE, DRAW, FIFTY_MOVE, INSUFFICIENT_MATERIAL, STALEMATE,
    THREEFOLD_REPETITION, WHITE_WINS, Outcome, game_outcome, insufficient_material,
    repetition_key,
)

__all__ = [
    "Board", "STARTING_FEN", "FenError", "parse_fen", "to_fen",
    "ChessGame", "GameOverError", "IllegalMoveError", "MoveError", "MoveFormatError",
    "PlayedMove", "Move", "parse_uci", "in_check", "is_attacked", "legal_moves",
    "perft", "perft_divide", "parse_san", "to_san", "BLACK", "WHITE", "parse_square", "square_name",
    "BLACK_WINS", "CHECKMATE", "DRAW", "FIFTY_MOVE", "INSUFFICIENT_MATERIAL",
    "STALEMATE", "THREEFOLD_REPETITION", "WHITE_WINS", "Outcome", "game_outcome",
    "insufficient_material", "repetition_key",
]
