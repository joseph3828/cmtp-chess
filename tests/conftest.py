import asyncio
import threading

import pytest

from client import Connection
from server import ChessServer


@pytest.fixture
def start_live_server():
    """Start real servers on their own event loops in background threads, for clients that use blocking sockets.

    Each server gets ``stop()``, and ``drop(name)``, which cuts that player's
    connection from the server side, as a network failure would.
    """
    running = []

    def _start(**options) -> ChessServer:
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        server = ChessServer("127.0.0.1", 0, **options)
        asyncio.run_coroutine_threadsafe(server.start(), loop).result(5)
        server.loop = loop
        server.stop = lambda: asyncio.run_coroutine_threadsafe(server.close(), loop).result(5)

        def drop(name=None):
            def cut():
                for session in list(server._sessions):
                    if name is None or session.name == name:
                        session.writer.transport.abort()
            loop.call_soon_threadsafe(cut)

        server.drop = drop
        running.append((server, loop, thread))
        return server

    yield _start
    for server, loop, thread in running:
        server.stop()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


@pytest.fixture
def live_server(start_live_server):
    return start_live_server()


@pytest.fixture
def open_connection(live_server):
    """Connect to the live server; every connection opened is closed after the test."""
    connections = []

    def _open(**options) -> Connection:
        conn = Connection("127.0.0.1", live_server.port, **options)
        conn.connect()
        connections.append(conn)
        return conn

    yield _open
    for conn in connections:
        conn.close()


def pytest_addoption(parser):
    parser.addoption(
        "--runslow", action="store_true", default=False, help="also run tests marked slow"
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--runslow"):
        return
    skip_slow = pytest.mark.skip(reason="slow test; run with --runslow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)
