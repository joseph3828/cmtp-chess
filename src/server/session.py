"""One connected client: reading its messages, answering them, and cleaning up when it leaves."""

import asyncio
import itertools
import logging
from typing import Any

from chessrules import Outcome
from cmtp import (
    IDLE_TIMEOUT_SECONDS, RESUME_WINDOW_SECONDS, ErrorCode, Message, MessageType, ProtocolError,
    Sender, decode, frame_message, read_frame,
)

from .game import RequestError, ServerGame, other
from .lobby import Lobby

log = logging.getLogger(__name__)

T = MessageType

# If this many bytes are waiting to go to a client, it has stopped reading.
# Dropping it keeps one stuck client from using up the server's memory.
MAX_WRITE_BUFFER = 1024 * 1024


class Session:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        lobby: Lobby,
        *,
        idle_timeout: float | None = IDLE_TIMEOUT_SECONDS,
        resume_window: float = RESUME_WINDOW_SECONDS,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.lobby = lobby
        self.idle_timeout = idle_timeout  # this long without a message means the connection is lost
        self.resume_window = resume_window  # how long a dropped player's seat is held
        self.name: str | None = None  # set by HELLO
        self.session_id: str | None = None
        self.resume_token: str | None = None
        self.game: ServerGame | None = None  # the current or most recent game
        self.said_bye = False
        self.replaced = False  # the player came back on another connection, which took over
        self.hold_timer: asyncio.TimerHandle | None = None  # set while a dropped player's seat is held
        self._seq = itertools.count(1)
        host, port, *_ = writer.get_extra_info("peername") or ("?", 0)
        self.peer = f"{host}:{port}"

    def __repr__(self) -> str:
        return f"<Session {self.name or '?'} {self.peer}>"

    @property
    def greeted(self) -> bool:
        return self.session_id is not None

    async def run(self) -> None:
        """Handle messages until the client leaves or breaks the protocol."""
        log.info("%s connected", self.peer)
        try:
            await self._read_loop()
        finally:
            self._leave()
            await self.close()
            log.info("%s disconnected", self)

    async def _read_loop(self) -> None:
        while True:
            try:
                # Clients send PING regularly, so a long silence means the
                # connection died without TCP noticing (a laptop lid closed,
                # Wi-Fi gone).
                payload = await asyncio.wait_for(read_frame(self.reader), self.idle_timeout)
            except TimeoutError:
                log.info("%s sent nothing for %g s; treating the connection as lost", self, self.idle_timeout)
                return
            except ProtocolError as exc:  # frame too large: the stream can't be trusted
                self.send_error(exc.code, exc.message)
                return
            except ConnectionError:  # includes a frame cut off partway
                return
            if payload is None:
                return

            try:
                message = decode(payload, sender=Sender.CLIENT)
            except ProtocolError as exc:
                log.debug("%s sent a bad message: %s", self, exc)
                self.send_error(exc.code, exc.message, re=exc.seq)
                if exc.fatal:
                    return
                continue

            log.debug("%s -> %s %s", self, message.type, message.body)
            if message.type is T.BYE:
                self.said_bye = True
                return
            try:
                self.handle(message)
            except Exception:
                # A bug while handling one message shouldn't take down the
                # connection, let alone the server.
                log.exception("error handling %s from %s", message.type, self)
                self.send_error(ErrorCode.INTERNAL_ERROR, "internal server error", re=message.seq)

    def handle(self, message: Message) -> None:
        if not self.greeted and message.type is not T.HELLO:
            self.send_error(ErrorCode.HELLO_REQUIRED, "send HELLO first", re=message.seq)
            return
        handler = getattr(self, "_on_" + message.type.lower())
        try:
            handler(message)
        except RequestError as exc:
            if message.type is T.MOVE:
                self.send(T.MOVE_REJECTED, {
                    "game_id": message.body["game_id"], "code": exc.code, "reason": exc.reason,
                }, re=message.seq)
            else:
                self.send_error(exc.code, exc.reason, re=message.seq)

    # Sending

    def send(self, message_type: MessageType, body: dict[str, Any] | None = None, *, re: int | None = None) -> None:
        """Queue a message for this client.

        Writing is buffered by asyncio, so this never waits. That way a slow
        client can't hold up the handler that is sending it something, such as
        its opponent's move.
        """
        if self.writer.is_closing():
            return
        message = Message(message_type, next(self._seq), body or {}, re)
        log.debug("%s <- %s %s", self, message_type, message.body)
        self.writer.write(frame_message(message))
        if self.writer.transport.get_write_buffer_size() > MAX_WRITE_BUFFER:
            log.warning("%s isn't reading its messages; dropping it", self)
            self.writer.transport.abort()

    def send_error(self, code: ErrorCode, detail: str, *, re: int | None = None) -> None:
        self.send(T.ERROR, {"code": code, "message": detail}, re=re)

    async def close(self) -> None:
        if not self.writer.is_closing():
            self.writer.close()
        try:
            await self.writer.wait_closed()
        except ConnectionError:
            pass

    # Handlers, one per client message type. Each raises RequestError to refuse.

    def _on_hello(self, message: Message) -> None:
        if self.greeted:
            raise RequestError(ErrorCode.ALREADY_GREETED, "HELLO was already sent")
        if "resume_token" in message.body:
            self._resume(message)
            return
        self.name = message.body["name"]
        self.session_id, self.resume_token = self.lobby.register(self)
        self.send(T.WELCOME, {
            "session_id": self.session_id, "resume_token": self.resume_token,
        }, re=message.seq)
        log.info("%s is %s (%s)", self.peer, self.name, self.session_id)

    def _on_list_games(self, message: Message) -> None:
        games = [
            {"game_id": game.id, "host": game.host.name, "host_color": game.host_color}
            for game in self.lobby.open_games()
        ]
        self.send(T.GAME_LIST, {"games": games}, re=message.seq)

    def _on_create_game(self, message: Message) -> None:
        self._require_free()
        self.game = self.lobby.create_game(self, message.body.get("color"))
        self.send(T.GAME_CREATED, {"game_id": self.game.id, "color": self.game.host_color}, re=message.seq)
        log.info("%s created %s", self, self.game.id)

    def _on_join_game(self, message: Message) -> None:
        game = self.lobby.find(message.body["game_id"])
        if game.host is self:
            raise RequestError(ErrorCode.CANNOT_JOIN_OWN_GAME, "you can't join your own game")
        self._require_free()
        if game.started:
            raise RequestError(ErrorCode.GAME_FULL, f"game {game.id} already has two players")
        game.start(self, self.lobby.rng)
        self.game = game
        for color, player in game.players.items():
            player.send(T.GAME_START, {
                "game_id": game.id,
                "your_color": color,
                "opponent": game.opponent_of(player).name,
                "fen": game.chess.fen(),
            }, re=message.seq if player is self else None)
        log.info("%s started: %s (white) vs %s (black)",
                 game.id, game.players["white"].name, game.players["black"].name)

    def _on_cancel_game(self, message: Message) -> None:
        game = self._current_game(message)
        if game.started:
            raise RequestError(ErrorCode.GAME_ALREADY_STARTED, f"game {game.id} has already started; resign instead")
        self.lobby.remove(game)
        self.game = None
        self.send(T.GAME_CANCELLED, {"game_id": game.id}, re=message.seq)
        log.info("%s cancelled %s", self, game.id)

    def _on_move(self, message: Message) -> None:
        game = self._current_game(message)
        result = game.play(self, message.body["ply"], message.body["move"])
        made = {
            "game_id": game.id,
            "ply": message.body["ply"],
            "move": result.played.uci,
            "san": result.played.san,
            "fen": result.played.fen,
            "by": result.by,
        }
        for player in game.players.values():
            player.send(T.MOVE_MADE, made, re=message.seq if player is self else None)
        if result.draw_declined:
            game.opponent_of(self).send(T.DRAW_DECLINED, {"game_id": game.id})
        if game.is_over:
            self._finish(game)

    def _on_resign(self, message: Message) -> None:
        game = self._current_game(message)
        game.resign(self)
        self._finish(game)

    def _on_offer_draw(self, message: Message) -> None:
        game = self._current_game(message)
        status = game.offer_draw(self)
        if status == "offered":
            game.opponent_of(self).send(T.DRAW_OFFERED, {"game_id": game.id})
        elif status == "agreed":
            self._finish(game)

    def _on_answer_draw(self, message: Message) -> None:
        game = self._current_game(message)
        if game.answer_draw(self, message.body["accept"]):
            self._finish(game)
        else:
            game.opponent_of(self).send(T.DRAW_DECLINED, {"game_id": game.id})

    def _on_get_state(self, message: Message) -> None:
        game = self._current_game(message)
        game.require_started()
        self.send(T.STATE, self._state(game), re=message.seq)

    def _on_ping(self, message: Message) -> None:
        self.send(T.PONG, re=message.seq)

    # Helpers

    def _require_free(self) -> None:
        if self.game is not None and not self.game.is_over:
            raise RequestError(ErrorCode.ALREADY_IN_GAME, f"you are already in game {self.game.id}")

    def _current_game(self, message: Message) -> ServerGame:
        game_id = message.body["game_id"]
        if self.game is None or self.game.id != game_id:
            raise RequestError(ErrorCode.NOT_IN_GAME, f"you are not in game {game_id}")
        return self.game

    def _state(self, game: ServerGame) -> dict[str, Any]:
        return {
            "game_id": game.id,
            "your_color": game.color_of(self),
            "opponent": game.opponent_of(self).name,
            "moves": [played.uci for played in game.chess.history],
            "fen": game.chess.fen(),
            "turn": game.turn,
        }

    def _game_over(self, game: ServerGame) -> dict[str, Any]:
        outcome: Outcome = game.outcome
        return {"game_id": game.id, "result": outcome.result, "reason": outcome.reason}

    def _finish(self, game: ServerGame) -> None:
        for player in game.players.values():
            player.send(T.GAME_OVER, self._game_over(game))
        self.lobby.remove(game)
        log.info("%s ended %s (%s)", game.id, game.outcome.result, game.outcome.reason)

    # Leaving and coming back

    def _leave(self) -> None:
        """Clean up after the connection ends, for whatever reason."""
        if self.replaced:
            return  # the player came back on a new connection, which carries on
        game = self.game
        if game is not None and not game.is_over:
            if not game.started:
                self.lobby.remove(game)  # nobody joined, so just drop it
            elif self.said_bye or self.lobby.closing or not self.resume_window:
                game.abandon(self)  # leaving on purpose forfeits the game
                self._finish(game)
            else:
                self._hold_seat(game)
                return  # stay registered so the resume token keeps working
        if self.session_id is not None:
            self.lobby.unregister(self)

    def _hold_seat(self, game: ServerGame) -> None:
        """The connection dropped mid-game without a BYE. Give the player a chance to come back."""
        log.info("%s dropped out of %s; holding the seat for %g s", self, game.id, self.resume_window)
        game.opponent_of(self).send(T.OPPONENT_STATUS, {"game_id": game.id, "connected": False})
        self.hold_timer = asyncio.get_running_loop().call_later(self.resume_window, self._seat_expired)

    def _seat_expired(self) -> None:
        self.hold_timer = None
        game = self.game
        if game is not None and game.abandon(self):
            log.info("%s didn't come back to %s in time", self, game.id)
            self._finish(game)
        self.lobby.unregister(self)

    def _resume(self, message: Message) -> None:
        """HELLO with a resume token: this connection takes over a player who dropped out."""
        old = self.lobby.find_by_token(message.body["resume_token"])
        if old is None:
            raise RequestError(
                ErrorCode.BAD_RESUME_TOKEN, "nothing to resume with that token; send HELLO without one"
            )
        was_held = old.hold_timer is not None
        if was_held:
            old.hold_timer.cancel()
            old.hold_timer = None
        old.replaced = True
        if not old.writer.is_closing():
            # The old connection is dead, but the server hadn't noticed yet.
            old.writer.close()

        self.name, self.session_id, self.game = old.name, old.session_id, old.game
        self.resume_token = self.lobby.transfer(old, self)
        if self.game is not None:
            self.game.replace_player(old, self)
        self.send(T.WELCOME, {"session_id": self.session_id, "resume_token": self.resume_token}, re=message.seq)
        log.info("%s resumed as %s (%s)", self.peer, self.name, self.session_id)

        game = self.game
        if game is None or not game.started:
            return
        # Bring the player up to date on everything they missed.
        self.send(T.STATE, self._state(game))
        if game.is_over:
            self.send(T.GAME_OVER, self._game_over(game))
            return
        if game.draw_offer_from == other(game.color_of(self)):
            self.send(T.DRAW_OFFERED, {"game_id": game.id})
        if was_held:
            game.opponent_of(self).send(T.OPPONENT_STATUS, {"game_id": game.id, "connected": True})
