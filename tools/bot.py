"""Bot players for testing: they connect, play random legal moves, and record what happened.

A bot's moves depend only on its seed, never on timing, so two runs with the
same seeds play exactly the same game. That makes it easy to check that a
game played through the chaos proxy matches one played directly.

Run ``python -m tools.bot --games 4`` to have pairs of bots play each other
on a running server.
"""

import argparse
import asyncio
import random
import statistics
import time
from dataclasses import dataclass, field

from chessrules import ChessGame
from cmtp import DEFAULT_PORT, Message, MessageType, Sender, decode, encode, read_frame, write_frame

T = MessageType


class BotError(Exception):
    """The server did something a correct game never produces."""


@dataclass
class GameRecord:
    game_id: str
    color: str
    result: str
    reason: str
    moves: list[str]
    final_fen: str
    move_times: list[float] = field(default_factory=list)  # seconds from sending each of our moves to its MOVE_MADE


class Bot:
    def __init__(
        self,
        name: str,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        *,
        seed: int | None = None,
        max_plies: int | None = None,
        think: float = 0.0,
        timeout: float = 10.0,
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.rng = random.Random(seed)
        self.max_plies = max_plies  # after this many half-moves, white offers a draw and black accepts
        # Average seconds to wait before each move, like a person thinking. Each wait is
        # between half and one and a half times this, so bots don't all move in step.
        self.think = think
        self._think_rng = random.Random(seed)  # separate, so thinking doesn't change which moves are played
        self.timeout = timeout
        self._seq = 0

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        await self.send(T.HELLO, {"name": self.name})
        await self.expect(T.WELCOME)

    async def close(self) -> None:
        try:
            await self.send(T.BYE)
        except ConnectionError:
            pass
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except ConnectionError:
            pass

    async def send(self, message_type: MessageType, body: dict | None = None) -> int:
        self._seq += 1
        await write_frame(self.writer, encode(Message(message_type, self._seq, body or {})))
        return self._seq

    async def recv(self) -> Message:
        payload = await asyncio.wait_for(read_frame(self.reader), self.timeout)
        if payload is None:
            raise BotError("the server closed the connection")
        return decode(payload, sender=Sender.SERVER)

    async def expect(self, message_type: MessageType) -> Message:
        message = await self.recv()
        if message.type is not message_type:
            raise BotError(f"{self.name} expected {message_type}, got {message.type} {message.body}")
        return message

    async def create_game(self, color: str | None = None) -> str:
        await self.send(T.CREATE_GAME, {"color": color} if color else {})
        return (await self.expect(T.GAME_CREATED)).body["game_id"]

    async def join_game(self, game_id: str) -> None:
        await self.send(T.JOIN_GAME, {"game_id": game_id})  # GAME_START is read by play()

    async def play(self) -> GameRecord:
        """Play the game this bot created or joined, from GAME_START to GAME_OVER."""
        start = await self.expect(T.GAME_START)
        game_id, color = start.body["game_id"], start.body["your_color"]
        game = ChessGame(start.body["fen"])
        record = GameRecord(game_id, color, "", "", [], "")
        sent_at: float | None = None
        offered = False

        async def take_turn() -> None:
            nonlocal sent_at, offered
            turn = "white" if game.turn == "w" else "black"
            if game.is_over or turn != color:
                return
            if self.max_plies is not None and game.ply >= self.max_plies:
                if color == "white" and not offered:
                    offered = True
                    await self.send(T.OFFER_DRAW, {"game_id": game_id})
                    return
                if color == "white":
                    return
            move = self.rng.choice(sorted(game.legal_uci()))
            if self.think:
                await asyncio.sleep(self.think * self._think_rng.uniform(0.5, 1.5))
            sent_at = time.perf_counter()
            await self.send(T.MOVE, {"game_id": game_id, "ply": game.ply, "move": move})

        await take_turn()
        while True:
            message = await self.recv()
            received = time.perf_counter()  # before this bot's own work, which isn't part of the round trip
            body = message.body
            if message.type is T.MOVE_MADE:
                if body["by"] == color and sent_at is not None:
                    record.move_times.append(received - sent_at)
                    sent_at = None
                game.play(body["move"])
                if game.fen() != body["fen"]:
                    raise BotError(f"{self.name}'s board disagrees with the server after {body['move']}")
                record.moves.append(body["move"])
                await take_turn()
            elif message.type is T.DRAW_OFFERED:
                await self.send(T.ANSWER_DRAW, {"game_id": game_id, "accept": True})
            elif message.type is T.GAME_OVER:
                record.result, record.reason = body["result"], body["reason"]
                record.final_fen = game.fen()
                return record
            else:
                raise BotError(f"{self.name} got an unexpected {message.type} {body}")


async def play_pair(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    *,
    seed: int = 0,
    max_plies: int | None = None,
    think: float = 0.0,
    black_port: int | None = None,
) -> tuple[GameRecord, GameRecord]:
    """Two bots play one game. Returns (white's record, black's record).

    ``black_port`` lets black connect somewhere else, such as through a proxy.
    """
    white = Bot(f"White-{seed}", host, port, seed=seed, max_plies=max_plies, think=think)
    black = Bot(f"Black-{seed}", host, black_port or port, seed=seed + 1, max_plies=max_plies, think=think)
    await white.connect()
    await black.connect()
    try:
        game_id = await white.create_game("white")
        await black.join_game(game_id)
        white_record, black_record = await asyncio.gather(white.play(), black.play())
    finally:
        await white.close()
        await black.close()
    return white_record, black_record


async def _main(args: argparse.Namespace) -> None:
    results = await asyncio.gather(*(
        play_pair(args.host, args.port, seed=args.seed + 2 * i, max_plies=args.max_plies)
        for i in range(args.games)
    ))
    times = []
    for white, black in results:
        print(f"{white.game_id}: {white.result} ({white.reason}) after {len(white.moves)} half-moves")
        times += white.move_times + black.move_times
    if times:
        print(f"{len(times)} moves; move round trip median {statistics.median(times) * 1000:.2f} ms, "
              f"max {max(times) * 1000:.2f} ms")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m tools.bot", description="Have bots play each other on a CMTP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--games", type=int, default=1, help="games to play at the same time")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-plies", type=int, default=200, help="agree a draw after this many half-moves")
    asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    main()
