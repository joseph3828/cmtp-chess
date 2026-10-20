"""One game on the server: two players, the rules engine, and the checks the protocol adds.

Nothing here does any networking. Methods either change the game and return
what happened, or raise ``RequestError`` with the code to send back, so the
session code decides who gets told what.
"""

import random
from dataclasses import dataclass
from typing import Any

import chessrules
from chessrules import BLACK_WINS, DRAW, WHITE_WINS, ChessGame, MoveError, Outcome, PlayedMove
from cmtp import ErrorCode

WHITE = "white"
BLACK = "black"

_FROM_ENGINE = {chessrules.WHITE: WHITE, chessrules.BLACK: BLACK}

# Ways a game can end that aren't board rules.
RESIGNATION = "resignation"
AGREEMENT = "agreement"
ABANDONED = "abandoned"


def other(color: str) -> str:
    return BLACK if color == WHITE else WHITE


def win_for(color: str, reason: str) -> Outcome:
    return Outcome(WHITE_WINS if color == WHITE else BLACK_WINS, reason)


class RequestError(Exception):
    """A client request that can't be carried out. ``code`` goes back to the client."""

    def __init__(self, code: ErrorCode, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class MoveResult:
    played: PlayedMove
    by: str  # the mover's color
    draw_declined: bool  # the mover had a draw offer waiting and moved instead


class ServerGame:
    """A game from the moment it's created in the lobby until it ends.

    Players are the server's session objects; this class only compares them
    and reads their ``name``.
    """

    def __init__(self, game_id: str, host: Any, host_color: str | None) -> None:
        self.id = game_id
        self.host = host
        self.host_color = host_color  # None means random, decided when someone joins
        self.players: dict[str, Any] = {}  # color -> player, filled in by start()
        self.chess = ChessGame()
        self.draw_offer_from: str | None = None  # color of the player offering
        self._ended: Outcome | None = None  # set for resignation, agreement, abandonment

    @property
    def started(self) -> bool:
        return bool(self.players)

    @property
    def outcome(self) -> Outcome | None:
        return self._ended or self.chess.outcome

    @property
    def is_over(self) -> bool:
        return self.outcome is not None

    @property
    def ply(self) -> int:
        return self.chess.ply

    @property
    def turn(self) -> str:
        return _FROM_ENGINE[self.chess.turn]

    def color_of(self, player: Any) -> str | None:
        for color, seated in self.players.items():
            if seated is player:
                return color
        return None

    def opponent_of(self, player: Any) -> Any:
        return self.players[other(self.color_of(player))]

    def replace_player(self, old: Any, new: Any) -> None:
        """A player came back on a new connection."""
        if self.host is old:
            self.host = new
        for color, player in self.players.items():
            if player is old:
                self.players[color] = new

    def start(self, guest: Any, rng: random.Random) -> None:
        host_color = self.host_color or rng.choice((WHITE, BLACK))
        self.players = {host_color: self.host, other(host_color): guest}

    def play(self, player: Any, ply: int, move: str) -> MoveResult:
        color = self._seat(player)
        # The ply is checked before the turn, so a duplicate of a move that was
        # already played is reported as stale rather than as out of turn.
        if ply != self.ply:
            raise RequestError(
                ErrorCode.STALE_PLY, f"the move is for ply {ply}, but the game is at ply {self.ply}"
            )
        if color != self.turn:
            raise RequestError(ErrorCode.NOT_YOUR_TURN, f"it is {self.turn}'s turn")
        try:
            played = self.chess.play(move)
        except MoveError as exc:
            raise RequestError(ErrorCode.ILLEGAL_MOVE, str(exc)) from None

        # Moving instead of answering a draw offer turns it down.
        draw_declined = self.draw_offer_from == other(color)
        if draw_declined:
            self.draw_offer_from = None
        return MoveResult(played, color, draw_declined)

    def resign(self, player: Any) -> Outcome:
        color = self._seat(player)
        self._ended = win_for(other(color), RESIGNATION)
        return self._ended

    def offer_draw(self, player: Any) -> str:
        """Record a draw offer. Returns ``"offered"``, ``"repeated"``, or ``"agreed"``.

        If the opponent already offered a draw, offering back counts as accepting it.
        """
        color = self._seat(player)
        if self.draw_offer_from == other(color):
            self.draw_offer_from = None
            self._ended = Outcome(DRAW, AGREEMENT)
            return "agreed"
        if self.draw_offer_from == color:
            return "repeated"
        self.draw_offer_from = color
        return "offered"

    def answer_draw(self, player: Any, accept: bool) -> Outcome | None:
        """Accept or decline the opponent's draw offer. Returns the outcome if accepted."""
        color = self._seat(player)
        if self.draw_offer_from != other(color):
            raise RequestError(ErrorCode.NO_DRAW_OFFER, "there is no draw offer to answer")
        self.draw_offer_from = None
        if not accept:
            return None
        self._ended = Outcome(DRAW, AGREEMENT)
        return self._ended

    def abandon(self, player: Any) -> Outcome | None:
        """The player left. If the game was under way, the opponent wins."""
        if not self.started or self.is_over:
            return None
        self._ended = win_for(other(self.color_of(player)), ABANDONED)
        return self._ended

    def require_started(self) -> None:
        if not self.started:
            raise RequestError(ErrorCode.GAME_NOT_STARTED, f"game {self.id} is still waiting for an opponent")

    def _seat(self, player: Any) -> str:
        """The player's color, if the game is under way. Otherwise raise."""
        self.require_started()
        if self.is_over:
            raise RequestError(ErrorCode.GAME_ALREADY_OVER, f"game {self.id} is over")
        color = self.color_of(player)
        if color is None:
            raise RequestError(ErrorCode.NOT_IN_GAME, f"you are not playing in game {self.id}")
        return color
