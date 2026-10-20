"""A single game from start to finish: the board plus the history the rules need."""

from collections import Counter
from dataclasses import dataclass

from .fen import STARTING_FEN, parse_fen, to_fen
from .move import Move, parse_uci
from .movegen import legal_moves
from .san import to_san
from .status import Outcome, game_outcome, repetition_key


class MoveError(ValueError):
    """Base class for a move the game won't accept."""


class MoveFormatError(MoveError):
    """The move text isn't valid UCI."""


class IllegalMoveError(MoveError):
    """The move is well-formed but not legal in the current position."""


class GameOverError(MoveError):
    """The game has already ended."""


@dataclass(frozen=True, slots=True)
class PlayedMove:
    uci: str
    san: str
    fen: str  # the position after the move


class ChessGame:
    """Plays moves given as UCI text, and detects when the game ends by the rules."""

    def __init__(self, fen: str = STARTING_FEN) -> None:
        self.board = parse_fen(fen)
        self.start_fen = to_fen(self.board)
        self.history: list[PlayedMove] = []
        self._position_counts = Counter([repetition_key(self.board)])
        self.outcome: Outcome | None = game_outcome(self.board, self._position_counts)

    @property
    def ply(self) -> int:
        """The number of half-moves played so far."""
        return len(self.history)

    @property
    def turn(self) -> str:
        return self.board.turn

    @property
    def is_over(self) -> bool:
        return self.outcome is not None

    def fen(self) -> str:
        return to_fen(self.board)

    def legal_moves(self) -> list[Move]:
        return [] if self.is_over else legal_moves(self.board)

    def legal_uci(self) -> list[str]:
        return [move.uci() for move in self.legal_moves()]

    def play(self, text: str) -> PlayedMove:
        """Play a move given as UCI text, such as ``"e2e4"`` or ``"e7e8q"``.

        Raises a ``MoveError`` subclass if the move can't be played.
        """
        if self.is_over:
            raise GameOverError("the game is over")
        try:
            from_sq, to_sq, promotion = parse_uci(text)
        except ValueError as exc:
            raise MoveFormatError(str(exc)) from None

        legal = legal_moves(self.board)
        for move in legal:
            if move.from_sq == from_sq and move.to_sq == to_sq and move.promotion == promotion:
                break
        else:
            raise IllegalMoveError(f"{text} is not a legal move")

        san = to_san(self.board, move, legal)
        self.board.make_move(move)
        self._position_counts[repetition_key(self.board)] += 1
        self.outcome = game_outcome(self.board, self._position_counts)

        played = PlayedMove(move.uci(), san, self.fen())
        self.history.append(played)
        return played
