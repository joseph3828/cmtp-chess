"""The whole system under stress: bots playing full games, a network that mangles traffic, and fuzzing."""

import asyncio
import json
import random
import time
from contextlib import asynccontextmanager

import pytest

from client import Connection, Reconnected
from cmtp import (
    GAME_OVER_REASONS, ErrorCode, Message, MessageType, ProtocolError, Sender, decode, encode_frame,
    read_frame,
)
from server import ChessServer
from tools.bot import play_pair
from tools.chaos_proxy import ChaosProxy

T = MessageType


@asynccontextmanager
async def running_server(**options):
    server = ChessServer("127.0.0.1", 0, **options)
    await server.start()
    try:
        yield server
    finally:
        await server.close()


@asynccontextmanager
async def running_proxy(target_port, **options):
    proxy = ChaosProxy("127.0.0.1", target_port, **options)
    await proxy.start()
    try:
        yield proxy
    finally:
        await proxy.close()


def assert_same_game(white, black):
    assert white.game_id == black.game_id
    assert white.moves == black.moves
    assert white.final_fen == black.final_fen
    assert (white.result, white.reason) == (black.result, black.reason)
    assert white.reason in GAME_OVER_REASONS


# Whole games


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [1, 2, 3, 4])
async def test_random_games_play_to_the_end(seed):
    async with running_server() as server:
        white, black = await play_pair(port=server.port, seed=seed, max_plies=300)
    assert_same_game(white, black)
    assert len(white.moves) > 0


@pytest.mark.asyncio
async def test_many_games_at_once():
    async with running_server() as server:
        pairs = await asyncio.gather(*(play_pair(port=server.port, seed=s, max_plies=60) for s in range(0, 40, 2)))
    for white, black in pairs:
        assert_same_game(white, black)
    assert len({white.game_id for white, _ in pairs}) == 20


# A hostile network


@pytest.mark.asyncio
@pytest.mark.parametrize("max_chunk, max_delay", [(1, 0.0), (3, 0.0003), (64, 0.003)])
async def test_mangled_network_changes_nothing(max_chunk, max_delay):
    """The same seeded game, played directly and then through a proxy that fragments and delays every byte."""
    async with running_server() as server:
        direct = await play_pair(port=server.port, seed=11, max_plies=40)
        async with running_proxy(server.port, max_chunk=max_chunk, max_delay=max_delay, seed=5) as proxy:
            # White connects directly and black through the proxy, so the two see different networks.
            mangled = await play_pair(port=server.port, black_port=proxy.port, seed=11, max_plies=40)
    assert_same_game(*mangled)
    assert mangled[0].moves == direct[0].moves
    assert mangled[0].result == direct[0].result
    # The proxy really did break the traffic into pieces: many more writes than messages.
    assert proxy.stats["down_writes"] > 3 * len(mangled[0].moves)


# Fuzzing


async def throw(server, data: bytes, *, wait: float = 0.3) -> list[Message]:
    """Send raw bytes on a new connection. Returns what the server says back before going quiet or hanging up."""
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    replies = []
    try:
        writer.write(data)
        await writer.drain()
        while True:
            payload = await asyncio.wait_for(read_frame(reader), wait)
            if payload is None:
                break
            replies.append(decode(payload, sender=Sender.SERVER))  # the server's replies must always be valid
    except (TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass
    return replies


def random_value(rng: random.Random, depth: int = 0):
    kinds = ["int", "float", "str", "bool", "null", "big"] + (["list", "dict"] if depth < 3 else [])
    kind = rng.choice(kinds)
    if kind == "int":
        return rng.randint(-5, 5)
    if kind == "float":
        return rng.uniform(-1, 1)
    if kind == "str":
        return "".join(rng.choice("ae2e4gMOVE\x00\n♞") for _ in range(rng.randint(0, 8)))
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "null":
        return None
    if kind == "big":
        return 10 ** 40
    if kind == "list":
        return [random_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {rng.choice(["game_id", "ply", "move", "name", "x"]): random_value(rng, depth + 1) for _ in range(rng.randint(0, 3))}


def mutated_message(rng: random.Random) -> bytes:
    """A message that's almost right: a real envelope with one or two things broken."""
    message = {"v": 1, "type": rng.choice(list(MessageType)).value, "seq": rng.randint(1, 9), "re": None,
               "body": {"game_id": "g1", "ply": 0, "move": "e2e4", "name": "Fuzz"}}
    for _ in range(rng.randint(1, 2)):
        key = rng.choice(["v", "type", "seq", "re", "body", "extra"])
        if rng.random() < 0.2:
            message.pop(key, None)
        else:
            message[key] = random_value(rng)
    return json.dumps(message).encode()


@pytest.mark.asyncio
async def test_server_survives_fuzzing():
    rng = random.Random(1234)
    async with running_server(idle_timeout=1) as server:
        attacks = []
        attacks += [rng.randbytes(rng.randint(1, 300)) for _ in range(40)]  # pure noise
        attacks += [encode_frame(rng.randbytes(rng.randint(0, 200))) for _ in range(40)]  # noise in valid frames
        attacks += [encode_frame(mutated_message(rng)) for _ in range(150)]  # nearly valid messages
        attacks += [
            encode_frame(b"[" * 60000),  # nested too deep for the JSON parser
            encode_frame(b'{"v":1,"type":"PING","seq":1,"body":' + b"{\"a\":" * 10000 + b"1" + b"}" * 10000 + b"}"),
            encode_frame("\ud800".encode("utf-8", "surrogatepass")),  # invalid UTF-8
            b"\x00\x00\x00\x05{}",  # a frame cut short
            (2 ** 32 - 1).to_bytes(4, "big"),  # the largest length the header can say
        ]
        limit = asyncio.Semaphore(50)  # many attacks at once, but not all 235

        async def attack_with(data):
            async with limit:
                return await throw(server, data, wait=0.1)

        for replies in await asyncio.gather(*(attack_with(attack) for attack in attacks)):
            for reply in replies:
                assert reply.type is T.ERROR, reply
                assert 100 <= reply.body["code"] < 600

        # Through all that, the server kept serving: a real game still works.
        white, black = await play_pair(port=server.port, seed=99, max_plies=30)
    assert_same_game(white, black)


@pytest.mark.asyncio
async def test_deeply_nested_json_gets_a_clean_error():
    async with running_server() as server:
        replies = await throw(server, encode_frame(b"[" * 60000))
    assert [r.body["code"] for r in replies] == [ErrorCode.MALFORMED_JSON]


# Dropped connections, end to end


def drain_until(conn, wanted, timeout=5):
    """Read events until a message of type ``wanted`` arrives; returns everything read."""
    events = []
    deadline = time.monotonic() + timeout
    while True:
        event = conn.get(timeout=max(0.0, deadline - time.monotonic()))
        assert event is not None, f"timed out waiting for {wanted}; got {events}"
        events.append(event)
        if isinstance(event, Message) and event.type is wanted:
            return events


def test_game_survives_a_cut_through_the_proxy(start_live_server):
    """Ana plays through the proxy. Midway, every proxied connection is cut; she resumes and wins."""
    server = start_live_server()
    proxy = ChaosProxy("127.0.0.1", server.port, max_chunk=7)
    asyncio.run_coroutine_threadsafe(proxy.start(), server.loop).result(5)
    ana = Connection("127.0.0.1", proxy.port, retry_delay=0.05)
    ben = Connection("127.0.0.1", server.port)
    try:
        for conn, name in ((ana, "Ana"), (ben, "Ben")):
            conn.connect()
            conn.send(T.HELLO, {"name": name})
            drain_until(conn, T.WELCOME)
        ana.send(T.CREATE_GAME, {"color": "white"})
        game_id = drain_until(ana, T.GAME_CREATED)[-1].body["game_id"]
        ben.send(T.JOIN_GAME, {"game_id": game_id})
        drain_until(ben, T.GAME_START)
        drain_until(ana, T.GAME_START)

        moves = ["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7"]
        for ply, move in enumerate(moves):
            if ply == 3:
                server.loop.call_soon_threadsafe(proxy.drop_all)
                events = drain_until(ana, T.STATE)
                assert Reconnected(resumed=True) in events
                assert events[-1].body["moves"] == moves[:3]
                drain_until(ben, T.OPPONENT_STATUS)
            mover = ana if ply % 2 == 0 else ben
            mover.send(T.MOVE, {"game_id": game_id, "ply": ply, "move": move})
            for conn in (ana, ben):
                assert drain_until(conn, T.MOVE_MADE)[-1].body["move"] == move
        for conn in (ana, ben):
            over = drain_until(conn, T.GAME_OVER)[-1]
            assert (over.body["result"], over.body["reason"]) == ("1-0", "checkmate")
        assert proxy.stats["drops"] >= 1
    finally:
        ana.close()
        ben.close()
        asyncio.run_coroutine_threadsafe(proxy.close(), server.loop).result(5)


@pytest.mark.asyncio
async def test_proxy_latency_shows_up_in_move_times():
    async with running_server() as server:
        async with running_proxy(server.port, latency=0.03) as proxy:
            white, black = await play_pair(port=server.port, black_port=proxy.port, seed=3, max_plies=6)
    # Black's moves cross the proxy twice (MOVE up, MOVE_MADE back), so each takes at least 60 ms.
    assert min(black.move_times) >= 0.06
    assert max(white.move_times) < 0.06  # white connects directly
