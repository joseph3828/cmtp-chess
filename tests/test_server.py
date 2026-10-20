"""The server, tested over real TCP connections with a minimal protocol client."""

import asyncio
import random
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

from cmtp import (
    MAX_PAYLOAD, ErrorCode, Message, MessageType, Sender, decode, encode, encode_frame,
    read_frame, write_frame,
)
from server import ChessServer

T = MessageType
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
TIMEOUT = 2


class Client:
    """Just enough of a client to drive the server from tests."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.seq = 0

    async def send(self, message_type, body=None) -> int:
        self.seq += 1
        await write_frame(self.writer, encode(Message(message_type, self.seq, body or {})))
        return self.seq

    async def send_raw(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def recv(self) -> Message:
        payload = await asyncio.wait_for(read_frame(self.reader), TIMEOUT)
        assert payload is not None, "the server closed the connection"
        return decode(payload, sender=Sender.SERVER)

    async def expect(self, message_type, re=None) -> Message:
        message = await self.recv()
        assert message.type == message_type, f"expected {message_type}, got {message.type} {message.body}"
        if re is not None:
            assert message.re == re
        return message

    async def request(self, message_type, body=None, *, reply) -> Message:
        seq = await self.send(message_type, body)
        return await self.expect(reply, re=seq)

    async def expect_error(self, message_type, body, code) -> Message:
        """Send a message and check that it's refused with ``code``."""
        seq = await self.send(message_type, body)
        reply_type = T.MOVE_REJECTED if message_type == T.MOVE else T.ERROR
        reply = await self.expect(reply_type, re=seq)
        assert reply.body["code"] == code, reply.body
        return reply

    async def is_closed_by_server(self) -> bool:
        return await asyncio.wait_for(read_frame(self.reader), TIMEOUT) is None

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except ConnectionError:
            pass


@asynccontextmanager
async def serving(**options):
    """Run a server; yields it and a function that opens client connections to it.

    With a name, ``connect`` also completes the handshake and keeps the
    WELCOME as ``client.welcome``.
    """
    chess_server = ChessServer("127.0.0.1", 0, rng=random.Random(1), **options)
    await chess_server.start()
    clients = []

    async def connect(name: str | None = "Player") -> Client:
        reader, writer = await asyncio.open_connection("127.0.0.1", chess_server.port)
        client = Client(reader, writer)
        clients.append(client)
        if name is not None:
            client.welcome = await client.request(T.HELLO, {"name": name}, reply=T.WELCOME)
        return client

    try:
        yield chess_server, connect
    finally:
        for client in clients:
            await client.close()
        await chess_server.close()


@pytest_asyncio.fixture
async def server_and_connect():
    async with serving() as pair:
        yield pair


@pytest_asyncio.fixture
async def server(server_and_connect):
    return server_and_connect[0]


@pytest_asyncio.fixture
async def connect(server_and_connect):
    return server_and_connect[1]


async def start_game(connect, host_color="white"):
    """Create and join a game. Returns (white client, black client, game ID)."""
    host = await connect("Host")
    guest = await connect("Guest")
    created = await host.request(T.CREATE_GAME, {"color": host_color}, reply=T.GAME_CREATED)
    game_id = created.body["game_id"]
    await guest.request(T.JOIN_GAME, {"game_id": game_id}, reply=T.GAME_START)
    await host.expect(T.GAME_START)
    return (host, guest, game_id) if host_color == "white" else (guest, host, game_id)


async def play(white, black, game_id, moves, first_ply=0):
    """Play moves in turn; both players must see each MOVE_MADE. Returns the last one."""
    made = None
    for ply, move in enumerate(moves, first_ply):
        mover = white if ply % 2 == 0 else black
        seq = await mover.send(T.MOVE, {"game_id": game_id, "ply": ply, "move": move})
        for client in (white, black):
            made = await client.expect(T.MOVE_MADE)
            assert made.body["move"] == move
            assert made.body["ply"] == ply
            assert made.re == (seq if client is mover else None)
    return made


# Handshake


@pytest.mark.asyncio
async def test_handshake(connect):
    client = await connect(None)
    welcome = await client.request(T.HELLO, {"name": "Ana"}, reply=T.WELCOME)
    assert welcome.body["session_id"]
    assert len(welcome.body["resume_token"]) >= 16


@pytest.mark.asyncio
async def test_server_numbers_its_messages_from_one(connect):
    client = await connect(None)
    first = await client.request(T.HELLO, {"name": "Ana"}, reply=T.WELCOME)
    second = await client.request(T.PING, reply=T.PONG)
    assert (first.seq, second.seq) == (1, 2)


@pytest.mark.asyncio
async def test_hello_required_first(connect):
    client = await connect(None)
    await client.expect_error(T.LIST_GAMES, {}, ErrorCode.HELLO_REQUIRED)


@pytest.mark.asyncio
async def test_hello_only_once(connect):
    client = await connect("Ana")
    await client.expect_error(T.HELLO, {"name": "Ana"}, ErrorCode.ALREADY_GREETED)


@pytest.mark.asyncio
async def test_unknown_resume_token(connect):
    client = await connect(None)
    await client.expect_error(T.HELLO, {"name": "Ana", "resume_token": "nope"}, ErrorCode.BAD_RESUME_TOKEN)


@pytest.mark.asyncio
async def test_ping(connect):
    client = await connect()
    await client.request(T.PING, reply=T.PONG)


@pytest.mark.asyncio
async def test_bye_closes_the_connection(connect):
    client = await connect()
    await client.send(T.BYE)
    assert await client.is_closed_by_server()


# Bad input


@pytest.mark.asyncio
async def test_unsupported_version_closes_the_connection(connect):
    client = await connect(None)
    await client.send_raw(encode_frame(b'{"v":2,"type":"PING","seq":1,"body":{}}'))
    error = await client.expect(T.ERROR, re=1)
    assert error.body["code"] == ErrorCode.UNSUPPORTED_VERSION
    assert await client.is_closed_by_server()


@pytest.mark.asyncio
async def test_oversized_frame_closes_the_connection(connect):
    client = await connect()
    await client.send_raw((MAX_PAYLOAD + 1).to_bytes(4, "big"))
    error = await client.expect(T.ERROR)
    assert error.body["code"] == ErrorCode.FRAME_TOO_LARGE
    assert await client.is_closed_by_server()


@pytest.mark.asyncio
async def test_malformed_json_keeps_the_connection_open(connect):
    client = await connect()
    await client.send_raw(encode_frame(b"{oops"))
    error = await client.expect(T.ERROR)
    assert error.body["code"] == ErrorCode.MALFORMED_JSON
    assert error.re is None
    await client.request(T.PING, reply=T.PONG)


@pytest.mark.asyncio
async def test_invalid_message_is_reported_with_its_seq(connect):
    client = await connect()
    await client.send_raw(encode_frame(b'{"v":1,"type":"MOVE","seq":7,"body":{}}'))
    error = await client.expect(T.ERROR, re=7)
    assert error.body["code"] == ErrorCode.INVALID_MESSAGE


@pytest.mark.asyncio
async def test_client_cannot_send_server_messages(connect):
    client = await connect()
    await client.send_raw(encode_frame(b'{"v":1,"type":"PONG","seq":2,"body":{}}'))
    error = await client.expect(T.ERROR, re=2)
    assert error.body["code"] == ErrorCode.UNKNOWN_TYPE


@pytest.mark.asyncio
async def test_garbage_from_one_client_does_not_affect_others(connect):
    rude = await connect(None)
    polite = await connect()
    await rude.send_raw(b"GET / HTTP/1.1\r\nHost: example\r\n\r\n")
    error = await rude.expect(T.ERROR)
    assert error.body["code"] == ErrorCode.FRAME_TOO_LARGE
    assert await rude.is_closed_by_server()
    await polite.request(T.PING, reply=T.PONG)


@pytest.mark.asyncio
async def test_internal_error_is_reported_and_the_connection_survives(connect, monkeypatch):
    from server.session import Session

    def broken(self, message):
        raise RuntimeError("bug")

    client = await connect()
    monkeypatch.setattr(Session, "_on_list_games", broken)
    await client.expect_error(T.LIST_GAMES, {}, ErrorCode.INTERNAL_ERROR)
    monkeypatch.undo()
    await client.request(T.PING, reply=T.PONG)


@pytest.mark.asyncio
async def test_server_full():
    small = ChessServer("127.0.0.1", 0, max_clients=1)
    await small.start()
    clients = []

    async def open_client():
        reader, writer = await asyncio.open_connection("127.0.0.1", small.port)
        clients.append(Client(reader, writer))
        return clients[-1]

    try:
        first = await open_client()
        await first.request(T.HELLO, {"name": "First"}, reply=T.WELCOME)
        second = await open_client()
        error = await second.expect(T.ERROR)
        assert error.body["code"] == ErrorCode.SERVER_FULL
        assert await second.is_closed_by_server()
    finally:
        for client in clients:
            await client.close()
        await small.close()


@pytest.mark.asyncio
async def test_shutdown_notifies_clients():
    chess_server = ChessServer("127.0.0.1", 0)
    await chess_server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", chess_server.port)
    client = Client(reader, writer)
    try:
        await client.request(T.HELLO, {"name": "Ana"}, reply=T.WELCOME)
        await chess_server.close()
        error = await client.expect(T.ERROR)
        assert error.body["code"] == ErrorCode.SHUTTING_DOWN
        assert await client.is_closed_by_server()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stopping_serve_forever_shuts_down_cleanly():
    """Ctrl-C and SIGTERM cancel serve_forever; clients must still be told and disconnected."""
    chess_server = ChessServer("127.0.0.1", 0)
    await chess_server.start()
    serving = asyncio.create_task(chess_server.serve_forever())
    reader, writer = await asyncio.open_connection("127.0.0.1", chess_server.port)
    client = Client(reader, writer)
    try:
        await client.request(T.HELLO, {"name": "Ana"}, reply=T.WELCOME)
        serving.cancel()
        error = await client.expect(T.ERROR)
        assert error.body["code"] == ErrorCode.SHUTTING_DOWN
        assert await client.is_closed_by_server()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, TIMEOUT)
    finally:
        await client.close()


# Lobby


@pytest.mark.asyncio
async def test_empty_lobby(connect):
    client = await connect()
    reply = await client.request(T.LIST_GAMES, reply=T.GAME_LIST)
    assert reply.body == {"games": []}


@pytest.mark.asyncio
async def test_created_game_is_listed(connect):
    host = await connect("Ana")
    other = await connect("Ben")
    created = await host.request(T.CREATE_GAME, {"color": "black"}, reply=T.GAME_CREATED)
    assert created.body == {"game_id": created.body["game_id"], "color": "black"}
    reply = await other.request(T.LIST_GAMES, reply=T.GAME_LIST)
    assert reply.body["games"] == [
        {"game_id": created.body["game_id"], "host": "Ana", "host_color": "black"},
    ]


@pytest.mark.asyncio
async def test_join_starts_the_game_for_both_players(connect):
    host = await connect("Ana")
    guest = await connect("Ben")
    created = await host.request(T.CREATE_GAME, {"color": "black"}, reply=T.GAME_CREATED)
    game_id = created.body["game_id"]
    guest_start = await guest.request(T.JOIN_GAME, {"game_id": game_id}, reply=T.GAME_START)
    host_start = await host.expect(T.GAME_START)
    assert host_start.re is None
    assert host_start.body == {"game_id": game_id, "your_color": "black", "opponent": "Ben", "fen": START_FEN}
    assert guest_start.body == {"game_id": game_id, "your_color": "white", "opponent": "Ana", "fen": START_FEN}
    # A game that has started is no longer open.
    assert (await guest.request(T.LIST_GAMES, reply=T.GAME_LIST)).body == {"games": []}


@pytest.mark.asyncio
async def test_random_color_gives_opposite_colors(connect):
    host = await connect("Ana")
    guest = await connect("Ben")
    created = await host.request(T.CREATE_GAME, reply=T.GAME_CREATED)
    assert created.body["color"] is None
    guest_start = await guest.request(T.JOIN_GAME, {"game_id": created.body["game_id"]}, reply=T.GAME_START)
    host_start = await host.expect(T.GAME_START)
    assert {host_start.body["your_color"], guest_start.body["your_color"]} == {"white", "black"}


@pytest.mark.asyncio
async def test_join_errors(connect):
    host = await connect("Ana")
    guest = await connect("Ben")
    third = await connect("Cy")
    await guest.expect_error(T.JOIN_GAME, {"game_id": "g999"}, ErrorCode.GAME_NOT_FOUND)

    game_id = (await host.request(T.CREATE_GAME, reply=T.GAME_CREATED)).body["game_id"]
    await host.expect_error(T.JOIN_GAME, {"game_id": game_id}, ErrorCode.CANNOT_JOIN_OWN_GAME)
    await host.expect_error(T.CREATE_GAME, {}, ErrorCode.ALREADY_IN_GAME)

    await guest.request(T.JOIN_GAME, {"game_id": game_id}, reply=T.GAME_START)
    await host.expect(T.GAME_START)
    await third.expect_error(T.JOIN_GAME, {"game_id": game_id}, ErrorCode.GAME_FULL)
    await guest.expect_error(T.CREATE_GAME, {}, ErrorCode.ALREADY_IN_GAME)


@pytest.mark.asyncio
async def test_cancel_an_open_game(connect):
    host = await connect("Ana")
    other = await connect("Ben")
    game_id = (await host.request(T.CREATE_GAME, reply=T.GAME_CREATED)).body["game_id"]
    cancelled = await host.request(T.CANCEL_GAME, {"game_id": game_id}, reply=T.GAME_CANCELLED)
    assert cancelled.body == {"game_id": game_id}
    assert (await other.request(T.LIST_GAMES, reply=T.GAME_LIST)).body == {"games": []}
    await other.expect_error(T.JOIN_GAME, {"game_id": game_id}, ErrorCode.GAME_NOT_FOUND)
    # The host is free to create another game.
    await host.request(T.CREATE_GAME, reply=T.GAME_CREATED)


@pytest.mark.asyncio
async def test_cancel_errors(connect):
    host = await connect("Ana")
    guest = await connect("Ben")
    game_id = (await host.request(T.CREATE_GAME, reply=T.GAME_CREATED)).body["game_id"]
    await guest.expect_error(T.CANCEL_GAME, {"game_id": game_id}, ErrorCode.NOT_IN_GAME)
    await guest.request(T.JOIN_GAME, {"game_id": game_id}, reply=T.GAME_START)
    await host.expect(T.GAME_START)
    await host.expect_error(T.CANCEL_GAME, {"game_id": game_id}, ErrorCode.GAME_ALREADY_STARTED)


@pytest.mark.asyncio
async def test_open_game_disappears_when_its_host_leaves(connect):
    host = await connect("Ana")
    other = await connect("Ben")
    await host.request(T.CREATE_GAME, reply=T.GAME_CREATED)
    await host.close()
    for _ in range(100):  # the server notices the disconnect asynchronously
        reply = await other.request(T.LIST_GAMES, reply=T.GAME_LIST)
        if not reply.body["games"]:
            break
        await asyncio.sleep(0.01)
    assert reply.body["games"] == []


# Playing


@pytest.mark.asyncio
async def test_fools_mate(connect):
    white, black, game_id = await start_game(connect)
    made = await play(white, black, game_id, ["f2f3", "e7e5", "g2g4", "d8h4"])
    assert made.body == {
        "game_id": game_id,
        "ply": 3,
        "move": "d8h4",
        "san": "Qh4#",
        "fen": "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3",
        "by": "black",
    }
    for client in (white, black):
        over = await client.expect(T.GAME_OVER)
        assert over.body == {"game_id": game_id, "result": "0-1", "reason": "checkmate"}


@pytest.mark.asyncio
async def test_host_can_play_black(connect):
    white, black, game_id = await start_game(connect, host_color="black")
    await play(white, black, game_id, ["e2e4", "e7e5"])


@pytest.mark.asyncio
async def test_move_rejections(connect):
    white, black, game_id = await start_game(connect)
    await black.expect_error(T.MOVE, {"game_id": game_id, "ply": 0, "move": "e7e5"}, ErrorCode.NOT_YOUR_TURN)
    await white.expect_error(T.MOVE, {"game_id": game_id, "ply": 1, "move": "e2e4"}, ErrorCode.STALE_PLY)
    await white.expect_error(T.MOVE, {"game_id": game_id, "ply": 0, "move": "e2e5"}, ErrorCode.ILLEGAL_MOVE)
    await white.expect_error(T.MOVE, {"game_id": "g999", "ply": 0, "move": "e2e4"}, ErrorCode.NOT_IN_GAME)

    await play(white, black, game_id, ["e2e4"])
    # Sending the same move again is caught by the ply check.
    rejected = await white.expect_error(
        T.MOVE, {"game_id": game_id, "ply": 0, "move": "e2e4"}, ErrorCode.STALE_PLY
    )
    assert rejected.body["game_id"] == game_id


@pytest.mark.asyncio
async def test_move_before_the_game_starts(connect):
    host = await connect("Ana")
    game_id = (await host.request(T.CREATE_GAME, reply=T.GAME_CREATED)).body["game_id"]
    await host.expect_error(T.MOVE, {"game_id": game_id, "ply": 0, "move": "e2e4"}, ErrorCode.GAME_NOT_STARTED)
    await host.expect_error(T.GET_STATE, {"game_id": game_id}, ErrorCode.GAME_NOT_STARTED)


@pytest.mark.asyncio
async def test_no_moves_after_the_game_ends(connect):
    white, black, game_id = await start_game(connect)
    await play(white, black, game_id, ["f2f3", "e7e5", "g2g4", "d8h4"])
    await white.expect(T.GAME_OVER)
    await black.expect(T.GAME_OVER)
    await white.expect_error(T.MOVE, {"game_id": game_id, "ply": 4, "move": "e2e4"}, ErrorCode.GAME_ALREADY_OVER)


@pytest.mark.asyncio
async def test_players_can_start_a_new_game_after_one_ends(connect):
    white, black, game_id = await start_game(connect)
    await white.send(T.RESIGN, {"game_id": game_id})
    await white.expect(T.GAME_OVER)
    await black.expect(T.GAME_OVER)
    await white.request(T.CREATE_GAME, reply=T.GAME_CREATED)


@pytest.mark.asyncio
async def test_get_state(connect):
    white, black, game_id = await start_game(connect)
    await play(white, black, game_id, ["e2e4", "c7c5"])
    state = await black.request(T.GET_STATE, {"game_id": game_id}, reply=T.STATE)
    assert state.body == {
        "game_id": game_id,
        "your_color": "black",
        "opponent": "Host",
        "moves": ["e2e4", "c7c5"],
        "fen": "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2",
        "turn": "white",
    }


# Ending a game without checkmate


@pytest.mark.asyncio
async def test_resign(connect):
    white, black, game_id = await start_game(connect)
    await black.send(T.RESIGN, {"game_id": game_id})
    for client in (white, black):
        over = await client.expect(T.GAME_OVER)
        assert over.body == {"game_id": game_id, "result": "1-0", "reason": "resignation"}


@pytest.mark.asyncio
async def test_draw_accepted(connect):
    white, black, game_id = await start_game(connect)
    await white.send(T.OFFER_DRAW, {"game_id": game_id})
    await black.expect(T.DRAW_OFFERED)
    await black.send(T.ANSWER_DRAW, {"game_id": game_id, "accept": True})
    for client in (white, black):
        over = await client.expect(T.GAME_OVER)
        assert over.body == {"game_id": game_id, "result": "1/2-1/2", "reason": "agreement"}


@pytest.mark.asyncio
async def test_draw_declined(connect):
    white, black, game_id = await start_game(connect)
    await white.send(T.OFFER_DRAW, {"game_id": game_id})
    await black.expect(T.DRAW_OFFERED)
    await black.send(T.ANSWER_DRAW, {"game_id": game_id, "accept": False})
    await white.expect(T.DRAW_DECLINED)
    # The game goes on.
    await play(white, black, game_id, ["e2e4"])


@pytest.mark.asyncio
async def test_moving_declines_a_draw_offer(connect):
    white, black, game_id = await start_game(connect)
    await play(white, black, game_id, ["e2e4"])
    await white.send(T.OFFER_DRAW, {"game_id": game_id})
    await black.expect(T.DRAW_OFFERED)
    await play(white, black, game_id, ["e7e5"], first_ply=1)
    await white.expect(T.DRAW_DECLINED)
    await black.expect_error(T.ANSWER_DRAW, {"game_id": game_id, "accept": True}, ErrorCode.NO_DRAW_OFFER)


@pytest.mark.asyncio
async def test_offering_back_accepts_the_draw(connect):
    white, black, game_id = await start_game(connect)
    await white.send(T.OFFER_DRAW, {"game_id": game_id})
    await black.expect(T.DRAW_OFFERED)
    await black.send(T.OFFER_DRAW, {"game_id": game_id})
    for client in (white, black):
        assert (await client.expect(T.GAME_OVER)).body["reason"] == "agreement"


@pytest.mark.asyncio
async def test_answer_without_an_offer(connect):
    white, black, game_id = await start_game(connect)
    await black.expect_error(T.ANSWER_DRAW, {"game_id": game_id, "accept": True}, ErrorCode.NO_DRAW_OFFER)


# Leaving, dropping out, and coming back


async def resume(connect, player, name="Host"):
    """Reconnect as ``player`` with its resume token. Returns the new client and its WELCOME."""
    back = await connect(None)
    welcome = await back.request(
        T.HELLO, {"name": name, "resume_token": player.welcome.body["resume_token"]}, reply=T.WELCOME
    )
    return back, welcome


@pytest.mark.asyncio
async def test_bye_mid_game_forfeits(connect):
    white, black, game_id = await start_game(connect)
    await play(white, black, game_id, ["e2e4"])
    await white.send(T.BYE)
    over = await black.expect(T.GAME_OVER)
    assert over.body == {"game_id": game_id, "result": "0-1", "reason": "abandoned"}


@pytest.mark.asyncio
async def test_dropped_player_loses_when_the_resume_window_ends():
    async with serving(resume_window=0.2) as (chess_server, connect):
        white, black, game_id = await start_game(connect)
        await white.close()  # no BYE, so it looks like the network failed
        status = await black.expect(T.OPPONENT_STATUS)
        assert status.body == {"game_id": game_id, "connected": False}
        over = await black.expect(T.GAME_OVER)
        assert over.body == {"game_id": game_id, "result": "0-1", "reason": "abandoned"}
        late = await connect(None)
        token = white.welcome.body["resume_token"]
        await late.expect_error(T.HELLO, {"name": "Host", "resume_token": token}, ErrorCode.BAD_RESUME_TOKEN)


@pytest.mark.asyncio
async def test_resume_after_a_drop(connect):
    white, black, game_id = await start_game(connect)
    await play(white, black, game_id, ["e2e4", "e7e5"])
    await white.close()
    assert (await black.expect(T.OPPONENT_STATUS)).body["connected"] is False

    back, welcome = await resume(connect, white)
    assert welcome.body["session_id"] == white.welcome.body["session_id"]
    assert welcome.body["resume_token"] != white.welcome.body["resume_token"]  # tokens are single-use
    state = await back.expect(T.STATE)
    assert state.body["moves"] == ["e2e4", "e7e5"]
    assert (state.body["your_color"], state.body["opponent"], state.body["turn"]) == ("white", "Guest", "white")
    assert (await black.expect(T.OPPONENT_STATUS)).body["connected"] is True
    await play(back, black, game_id, ["g1f3", "b8c6"], first_ply=2)


@pytest.mark.asyncio
async def test_resume_token_works_only_once(connect):
    white, black, game_id = await start_game(connect)
    await white.close()
    await black.expect(T.OPPONENT_STATUS)
    back, _ = await resume(connect, white)
    await back.expect(T.STATE)
    again = await connect(None)
    token = white.welcome.body["resume_token"]
    await again.expect_error(T.HELLO, {"name": "Host", "resume_token": token}, ErrorCode.BAD_RESUME_TOKEN)


@pytest.mark.asyncio
async def test_resume_replaces_a_connection_the_server_thinks_is_alive(connect):
    """A client whose network failed reconnects before the server noticed the old connection was dead."""
    white, black, game_id = await start_game(connect)
    back, _ = await resume(connect, white)
    await back.expect(T.STATE)
    assert await white.is_closed_by_server()
    # The opponent never saw a disconnect, and the game carries on with the new connection.
    await play(back, black, game_id, ["e2e4"])


@pytest.mark.asyncio
async def test_returning_player_hears_about_a_draw_offer(connect):
    white, black, game_id = await start_game(connect)
    await white.close()
    await black.expect(T.OPPONENT_STATUS)
    await black.send(T.OFFER_DRAW, {"game_id": game_id})
    back, _ = await resume(connect, white)
    await back.expect(T.STATE)
    await back.expect(T.DRAW_OFFERED)
    assert (await black.expect(T.OPPONENT_STATUS)).body["connected"] is True
    await back.send(T.ANSWER_DRAW, {"game_id": game_id, "accept": True})
    for client in (back, black):
        assert (await client.expect(T.GAME_OVER)).body["reason"] == "agreement"


@pytest.mark.asyncio
async def test_returning_player_learns_the_game_ended(connect):
    white, black, game_id = await start_game(connect)
    await white.close()
    await black.expect(T.OPPONENT_STATUS)
    await black.send(T.RESIGN, {"game_id": game_id})
    await black.expect(T.GAME_OVER)
    back, _ = await resume(connect, white)
    await back.expect(T.STATE)
    over = await back.expect(T.GAME_OVER)
    assert over.body == {"game_id": game_id, "result": "1-0", "reason": "resignation"}


@pytest.mark.asyncio
async def test_nothing_to_resume_from_the_lobby(server, connect):
    client = await connect("Ana")
    token = client.welcome.body["resume_token"]
    await client.close()
    for _ in range(100):  # the server notices the disconnect asynchronously
        if server.lobby.find_by_token(token) is None:
            break
        await asyncio.sleep(0.01)
    other = await connect(None)
    await other.expect_error(T.HELLO, {"name": "Ana", "resume_token": token}, ErrorCode.BAD_RESUME_TOKEN)


@pytest.mark.asyncio
async def test_silent_client_is_disconnected():
    async with serving(idle_timeout=0.2) as (chess_server, connect):
        client = await connect("Ana")
        assert await client.is_closed_by_server()


@pytest.mark.asyncio
async def test_pings_keep_a_client_connected():
    async with serving(idle_timeout=0.3) as (chess_server, connect):
        client = await connect("Ana")
        for _ in range(6):  # 0.6 s in all, twice the idle timeout
            await asyncio.sleep(0.1)
            await client.request(T.PING, reply=T.PONG)


@pytest.mark.asyncio
async def test_silent_player_seat_is_held():
    async with serving(idle_timeout=0.2) as (chess_server, connect):
        white, black, game_id = await start_game(connect)
        for _ in range(40):  # black keeps pinging; white has gone quiet
            await black.send(T.PING)
            message = await black.recv()
            if message.type is T.OPPONENT_STATUS:
                break
            await asyncio.sleep(0.02)
        assert message.body == {"game_id": game_id, "connected": False}
