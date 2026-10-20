"""The client's threaded connection, against a real server."""

import socket
import threading
import time

import pytest

from client import Closed, Connection, Reconnected, Reconnecting
from cmtp import ErrorCode, Message, MessageType, frame_message

T = MessageType


def expect(conn, message_type, timeout=2) -> Message:
    event = conn.get(timeout)
    assert event is not None, f"timed out waiting for {message_type}"
    assert isinstance(event, Message) and event.type == message_type, event
    return event


def greeted(open_connection, name):
    conn = open_connection()
    conn.send(T.HELLO, {"name": name})
    expect(conn, T.WELCOME)
    return conn


def test_hello(open_connection):
    conn = open_connection()
    seq = conn.send(T.HELLO, {"name": "Ana"})
    assert seq == 1
    assert expect(conn, T.WELCOME).re == seq


def test_get_times_out_when_nothing_arrives(open_connection):
    assert open_connection().get(timeout=0.05) is None


def test_poll_returns_everything_waiting(open_connection):
    conn = greeted(open_connection, "Ana")
    seqs = [conn.send(T.PING) for _ in range(3)]
    deadline = time.monotonic() + 2
    events = []
    while len(events) < 3 and time.monotonic() < deadline:
        events += conn.poll()
        time.sleep(0.01)
    assert [e.re for e in events] == seqs


def test_two_players_play_a_game(open_connection):
    ana = greeted(open_connection, "Ana")
    ben = greeted(open_connection, "Ben")
    ana.send(T.CREATE_GAME, {"color": "white"})
    game_id = expect(ana, T.GAME_CREATED).body["game_id"]
    ben.send(T.JOIN_GAME, {"game_id": game_id})
    expect(ben, T.GAME_START)
    expect(ana, T.GAME_START)
    for ply, move in enumerate(["f2f3", "e7e5", "g2g4", "d8h4"]):
        (ana if ply % 2 == 0 else ben).send(T.MOVE, {"game_id": game_id, "ply": ply, "move": move})
        for conn in (ana, ben):
            assert expect(conn, T.MOVE_MADE).body["move"] == move
    for conn in (ana, ben):
        assert expect(conn, T.GAME_OVER).body["result"] == "0-1"


def test_frames_from_two_threads_never_mix(open_connection):
    """Both threads send at once; the send lock keeps each frame's bytes together."""
    conn = greeted(open_connection, "Ana")
    count = 200

    def ping_many():
        for _ in range(count):
            conn.send(T.PING)

    threads = [threading.Thread(target=ping_many) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    replies = [expect(conn, T.PONG) for _ in range(2 * count)]
    assert sorted(reply.re for reply in replies) == list(range(2, 2 * count + 2))


def test_server_shutdown_is_reported(live_server, open_connection):
    conn = greeted(open_connection, "Ana")
    live_server.stop()
    assert expect(conn, T.ERROR).body["code"] == ErrorCode.SHUTTING_DOWN
    event = conn.get(timeout=2)
    assert event == Closed("the server closed the connection")
    assert not conn.connected


def test_close_says_bye(live_server, open_connection):
    conn = greeted(open_connection, "Ana")
    assert live_server.lobby.session_count == 1
    conn.close()
    assert conn.get(timeout=2) == Closed("disconnected")
    deadline = time.monotonic() + 2
    while live_server.lobby.session_count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert live_server.lobby.session_count == 0


def test_send_after_close_fails(open_connection):
    conn = open_connection()
    conn.close()
    conn.close()  # closing twice is fine
    with pytest.raises(OSError):
        conn.send(T.PING)


def test_connection_refused():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Nothing is listening on that port now.
    with pytest.raises(OSError):
        Connection("127.0.0.1", port, timeout=1).connect()


def test_bad_bytes_from_the_server_end_the_connection():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        conn = Connection("127.0.0.1", listener.getsockname()[1])
        conn.connect()
        peer, _ = listener.accept()
        with peer:
            peer.sendall(b"\x00\x00\x00\x02{]")
            event = conn.get(timeout=2)
        conn.close()
    assert isinstance(event, Closed)
    assert event.reason.startswith("the server sent a bad message")


# Heartbeats and getting back in after a drop


def collect_until(conn, stop, timeout=3):
    """Gather events until one satisfies ``stop``; returns them all, including that one."""
    events = []
    deadline = time.monotonic() + timeout
    while True:
        event = conn.get(timeout=max(0.0, deadline - time.monotonic()))
        assert event is not None, f"timed out; got {events}"
        events.append(event)
        if stop(event):
            return events


def kinds(events):
    return [e.type if isinstance(e, Message) else type(e).__name__ for e in events]


def start_game(open_connection, **options):
    ana = open_connection(**options)
    ben = open_connection()
    for conn, name in ((ana, "Ana"), (ben, "Ben")):
        conn.send(T.HELLO, {"name": name})
        expect(conn, T.WELCOME)
    ana.send(T.CREATE_GAME, {"color": "white"})
    game_id = expect(ana, T.GAME_CREATED).body["game_id"]
    ben.send(T.JOIN_GAME, {"game_id": game_id})
    expect(ben, T.GAME_START)
    expect(ana, T.GAME_START)
    return ana, ben, game_id


def test_heartbeats_measure_the_round_trip_and_stay_hidden(open_connection):
    conn = greeted(lambda: open_connection(heartbeat=0.05), "Ana")
    time.sleep(0.3)
    assert conn.poll() == []  # the heartbeat PONGs aren't passed on
    assert conn.last_rtt is not None and conn.last_rtt < 1


def test_a_silent_server_is_noticed():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        conn = Connection("127.0.0.1", listener.getsockname()[1], heartbeat=0.05, idle_timeout=0.2, reconnect_for=0)
        conn.connect()
        peer, _ = listener.accept()
        with peer:  # accepts the connection, then never says anything
            event = conn.get(timeout=3)
        conn.close()
    assert event == Closed("the server stopped responding")


def test_resumes_after_the_network_drops(live_server, open_connection):
    ana, ben, game_id = start_game(open_connection)
    ana.send(T.MOVE, {"game_id": game_id, "ply": 0, "move": "e2e4"})
    expect(ana, T.MOVE_MADE)
    expect(ben, T.MOVE_MADE)

    live_server.drop("Ana")
    events = collect_until(ana, lambda e: isinstance(e, Message) and e.type is T.STATE)
    assert kinds(events) == ["Reconnecting", T.WELCOME, "Reconnected", T.STATE]
    assert events[2] == Reconnected(resumed=True)
    assert events[3].body["moves"] == ["e2e4"]
    assert expect(ben, T.OPPONENT_STATUS).body["connected"] is False
    assert expect(ben, T.OPPONENT_STATUS).body["connected"] is True

    # The game carries on over the new connection.
    for mover, ply, move in ((ben, 1, "e7e5"), (ana, 2, "g1f3")):
        mover.send(T.MOVE, {"game_id": game_id, "ply": ply, "move": move})
        for conn in (ana, ben):
            assert expect(conn, T.MOVE_MADE).body["move"] == move


def test_starts_fresh_when_there_is_nothing_to_resume(live_server, open_connection):
    conn = greeted(open_connection, "Ana")
    conn._resume_token = "expired-token"  # as if the held seat had already been given up
    live_server.drop()
    events = collect_until(conn, lambda e: isinstance(e, Reconnected))
    assert kinds(events) == ["Reconnecting", T.WELCOME, "Reconnected"]  # the BAD_RESUME_TOKEN error stays hidden
    assert events[-1] == Reconnected(resumed=False)
    seq = conn.send(T.PING)
    assert expect(conn, T.PONG).re == seq


def fake_server_that_vanishes():
    """A listener that welcomes one client and then disappears for good. Returns (port, finish)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def finish():
        peer, _ = listener.accept()
        peer.recv(1024)  # the HELLO
        welcome = Message(T.WELCOME, 1, {"session_id": "s1", "resume_token": "tok"}, re=1)
        peer.sendall(frame_message(welcome))
        peer.close()
        listener.close()

    return listener.getsockname()[1], finish


def test_gives_up_when_the_server_does_not_come_back():
    port, finish = fake_server_that_vanishes()
    conn = Connection("127.0.0.1", port, reconnect_for=0.3, retry_delay=0.05)
    conn.connect()
    conn.send(T.HELLO, {"name": "Ana"})
    finish()
    events = collect_until(conn, lambda e: isinstance(e, Closed))
    conn.close()
    assert kinds(events) == [T.WELCOME, "Reconnecting", "Closed"]
    assert events[-1].reason.startswith("couldn't get back in within 0.3 s")


def test_closing_while_reconnecting_stops_trying():
    port, finish = fake_server_that_vanishes()
    conn = Connection("127.0.0.1", port, reconnect_for=30, retry_delay=0.05)
    conn.connect()
    conn.send(T.HELLO, {"name": "Ana"})
    finish()
    collect_until(conn, lambda e: isinstance(e, Reconnecting))
    started = time.monotonic()
    conn.close()
    assert conn.get(timeout=2) == Closed("disconnected")
    assert time.monotonic() - started < 1
