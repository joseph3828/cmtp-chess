"""Who is connected, and which games exist: open ones waiting for an opponent and ones under way."""

import random
import secrets
from collections.abc import Iterator
from typing import Any

from cmtp import ErrorCode

from .game import RequestError, ServerGame


class Lobby:
    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self.closing = False  # the server is shutting down, so no seats are held for returning players
        self._sessions: dict[str, Any] = {}  # session_id -> session, including ones whose seat is held
        self._tokens: dict[str, Any] = {}  # resume token -> session
        self._games: dict[str, ServerGame] = {}  # game_id -> game, until it ends
        self._session_count = 0
        self._game_count = 0

    def register(self, session: Any) -> tuple[str, str]:
        """Add a session that has said HELLO. Returns its session ID and resume token."""
        self._session_count += 1
        session_id = f"s{self._session_count}"
        self._sessions[session_id] = session
        return session_id, self._issue_token(session)

    def _issue_token(self, session: Any) -> str:
        # The token is what proves a returning player is who they say, so it
        # has to be unguessable. Session and game IDs don't.
        token = secrets.token_urlsafe(16)
        self._tokens[token] = session
        return token

    def unregister(self, session: Any) -> None:
        if self._sessions.get(session.session_id) is session:
            del self._sessions[session.session_id]
        if self._tokens.get(session.resume_token) is session:
            del self._tokens[session.resume_token]

    def find_by_token(self, token: str) -> Any | None:
        return self._tokens.get(token)

    def transfer(self, old: Any, new: Any) -> str:
        """Give a returning player's identity to their new connection.

        The old token stops working, so it can't be used twice. Returns the new one.
        """
        self._tokens.pop(old.resume_token, None)
        self._sessions[old.session_id] = new
        return self._issue_token(new)

    def sessions(self) -> Iterator[Any]:
        return iter(list(self._sessions.values()))

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    def open_games(self) -> list[ServerGame]:
        return [game for game in self._games.values() if not game.started]

    def create_game(self, host: Any, color: str | None) -> ServerGame:
        self._game_count += 1
        game = ServerGame(f"g{self._game_count}", host, color)
        self._games[game.id] = game
        return game

    def find(self, game_id: str) -> ServerGame:
        game = self._games.get(game_id)
        if game is None:
            raise RequestError(ErrorCode.GAME_NOT_FOUND, f"there is no game {game_id}")
        return game

    def remove(self, game: ServerGame) -> None:
        """Forget a game that ended or was given up before it started."""
        self._games.pop(game.id, None)
