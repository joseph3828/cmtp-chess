"""What the client knows about its session, the lobby, and its game. Shared by the CLI and the GUI.

The model changes only when a message arrives from the server. Sending a move
doesn't touch the board; the board changes when the server's MOVE_MADE comes
back. That keeps the client's board identical to the server's.
"""

from chessrules import ChessGame, MoveError, PlayedMove
from cmtp import ErrorCode, Message, MessageType

T = MessageType

WHITE = "white"
BLACK = "black"

_END_REASONS = {
    "checkmate": "checkmate",
    "stalemate": "stalemate",
    "insufficient_material": "neither side can checkmate",
    "fifty_move": "fifty moves without a capture or pawn move",
    "threefold_repetition": "the same position three times",
    "agreement": "draw agreed",
}


class ClientModel:
    def __init__(self) -> None:
        self.name: str | None = None
        self.session_id: str | None = None
        self.resume_token: str | None = None
        self.open_games: list[dict] = []
        self.reset_session()

    def reset_session(self) -> None:
        """Forget any game, as when a reconnect couldn't resume and the server started a new session."""
        self.hosting: str | None = None  # ID of the game we created, while it waits for an opponent
        self.host_color: str | None = None
        self.game_id: str | None = None  # the game being played, or the one that just ended
        self.my_color: str | None = None
        self.opponent: str | None = None
        self.opponent_connected = True
        self.chess: ChessGame | None = None  # local copy of the board, for display and move checking
        self.last_move: PlayedMove | None = None
        self.result: str | None = None  # "1-0", "0-1", or "1/2-1/2" once the game ends
        self.end_reason: str | None = None
        self.opponent_offered_draw = False
        self.i_offered_draw = False
        self.out_of_sync = False  # the local board disagrees with the server's; ask for STATE
        self._resync_requested = False

    @property
    def in_game(self) -> bool:
        """A game is under way (started and not finished)."""
        return self.game_id is not None and self.result is None

    @property
    def turn(self) -> str | None:
        if self.chess is None:
            return None
        return WHITE if self.chess.turn == "w" else BLACK

    @property
    def my_turn(self) -> bool:
        return self.in_game and self.turn == self.my_color

    @property
    def ply(self) -> int:
        return self.chess.ply if self.chess is not None else 0

    def apply(self, message: Message) -> None:
        """Update the model from one server message."""
        handler = getattr(self, "_on_" + message.type.lower(), None)
        if handler is not None:
            handler(message.body)

    def _on_welcome(self, body: dict) -> None:
        self.session_id = body["session_id"]
        self.resume_token = body["resume_token"]

    def _on_game_list(self, body: dict) -> None:
        self.open_games = body["games"]

    def _on_game_created(self, body: dict) -> None:
        self.hosting = body["game_id"]
        self.host_color = body["color"]

    def _on_game_cancelled(self, body: dict) -> None:
        self.hosting = None
        self.host_color = None

    def _on_game_start(self, body: dict) -> None:
        self.hosting = None
        self.host_color = None
        self.game_id = body["game_id"]
        self.my_color = body["your_color"]
        self.opponent = body["opponent"]
        self.opponent_connected = True
        self.chess = ChessGame(body["fen"])
        self.last_move = None
        self.result = None
        self.end_reason = None
        self.opponent_offered_draw = False
        self.i_offered_draw = False
        self.out_of_sync = False

    def _on_move_made(self, body: dict) -> None:
        if body["game_id"] != self.game_id or self.chess is None:
            return
        if body["ply"] != self.chess.ply:
            self.out_of_sync = True
            return
        try:
            self.last_move = self.chess.play(body["move"])
        except MoveError:
            self.out_of_sync = True
            return
        # The server sends its FEN with every move; if ours differs, we missed something.
        if self.chess.fen() != body["fen"]:
            self.out_of_sync = True
        # Whoever moved instead of answering a draw offer has turned it down.
        if body["by"] == self.my_color:
            self.opponent_offered_draw = False
        else:
            self.i_offered_draw = False

    def _on_move_rejected(self, body: dict) -> None:
        if body["code"] == ErrorCode.STALE_PLY:
            self.out_of_sync = True  # the server is at a different ply than we thought

    def _on_opponent_status(self, body: dict) -> None:
        if body["game_id"] == self.game_id:
            self.opponent_connected = body["connected"]

    def _on_draw_offered(self, body: dict) -> None:
        self.opponent_offered_draw = True

    def _on_draw_declined(self, body: dict) -> None:
        self.i_offered_draw = False

    def _on_game_over(self, body: dict) -> None:
        if body["game_id"] != self.game_id:
            return
        self.result = body["result"]
        self.end_reason = body["reason"]
        self.opponent_offered_draw = False
        self.i_offered_draw = False

    def _on_state(self, body: dict) -> None:
        """Rebuild the game from scratch from the server's full state."""
        if body["game_id"] != self.game_id:
            self.result = None
            self.end_reason = None
        self.hosting = None
        self.game_id = body["game_id"]
        self.my_color = body["your_color"]
        self.opponent = body["opponent"]
        self.chess = ChessGame()
        self.last_move = None
        try:
            for move in body["moves"]:
                self.last_move = self.chess.play(move)
        except MoveError:
            self.out_of_sync = True
            return
        self.out_of_sync = self.chess.fen() != body["fen"]
        if not self.out_of_sync:
            self._resync_requested = False

    def should_resync(self) -> bool:
        """True once each time the board falls out of step with the server.

        The caller then sends GET_STATE. If the STATE that comes back still
        doesn't match, this stays False rather than asking again forever.
        """
        if self.out_of_sync and self.game_id and not self._resync_requested:
            self._resync_requested = True
            return True
        return False

    def offered_draw(self) -> None:
        """Note that we sent OFFER_DRAW; the server doesn't confirm it."""
        self.i_offered_draw = True

    def answered_draw(self) -> None:
        self.opponent_offered_draw = False

    def i_won(self) -> bool | None:
        """True, False, or None for a draw or a game still going."""
        if self.result in (None, "1/2-1/2"):
            return None
        return (self.result == "1-0") == (self.my_color == WHITE)

    def ending_text(self) -> str | None:
        """How the game ended, from this player's point of view, such as "Ben resigned"."""
        if self.result is None:
            return None
        if self.end_reason in ("resignation", "abandoned"):
            loser = BLACK if self.result == "1-0" else WHITE
            who = "You" if loser == self.my_color else self.opponent
            return f"{who} resigned" if self.end_reason == "resignation" else f"{who} left the game"
        return _END_REASONS.get(self.end_reason, self.end_reason)
