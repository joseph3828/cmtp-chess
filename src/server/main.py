"""The CMTP chess server: accepts TCP connections and runs a session for each.

Run it with ``python -m server``. ``--help`` lists the options.
"""

import argparse
import asyncio
import logging
import random
import signal
import socket

from cmtp import DEFAULT_PORT, IDLE_TIMEOUT_SECONDS, RESUME_WINDOW_SECONDS, ErrorCode

from .lobby import Lobby
from .session import Session

log = logging.getLogger(__name__)


class ChessServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        *,
        max_clients: int = 1000,
        idle_timeout: float | None = IDLE_TIMEOUT_SECONDS,
        resume_window: float = RESUME_WINDOW_SECONDS,
        rng: random.Random | None = None,
    ) -> None:
        self.host = host
        self.port = port  # 0 picks a free port; start() fills in the real one
        self.max_clients = max_clients
        self.idle_timeout = idle_timeout
        self.resume_window = resume_window
        self.lobby = Lobby(rng)
        self._sessions: set[Session] = set()
        self._tasks: set[asyncio.Task] = set()  # one per connection, running its session
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        # A deep backlog lets hundreds of clients connect at once; with asyncio's
        # default of 100, the overflow waits a second or more for TCP to retry.
        self._server = await asyncio.start_server(self._on_connect, self.host, self.port, backlog=1024)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("listening on %s:%d", self.host, self.port)

    async def serve_forever(self) -> None:
        """Serve until cancelled (Ctrl-C or SIGTERM), then shut down cleanly.

        This doesn't use asyncio's own ``Server.serve_forever``: when that's
        cancelled, it waits for every connection to close before returning,
        but the connections only close once ``close()`` has told them to.
        """
        try:
            await asyncio.Future()  # never finishes on its own
        finally:
            await self.close()

    async def close(self) -> None:
        """Tell every client the server is going away, then disconnect them."""
        if self._server is not None:
            self._server.close()
        # Nobody can come back to a server that's gone, so don't hold seats.
        self.lobby.closing = True
        for session in self.lobby.sessions():
            if session.hold_timer is not None:
                session.hold_timer.cancel()
                session.hold_timer = None
        sessions = list(self._sessions)
        for session in sessions:
            session.send_error(ErrorCode.SHUTTING_DOWN, "the server is shutting down")
        await asyncio.gather(*(session.close() for session in sessions))
        # Let each session finish its own cleanup, such as ending its game.
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Send small messages right away instead of letting Nagle's algorithm
        # hold them back to batch them. asyncio already does this for TCP;
        # it's set here too because move latency depends on it.
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        session = Session(
            reader, writer, self.lobby, idle_timeout=self.idle_timeout, resume_window=self.resume_window
        )
        if len(self._sessions) >= self.max_clients:
            log.warning("turning away %s: %d clients connected", session.peer, len(self._sessions))
            session.send_error(ErrorCode.SERVER_FULL, "the server is full; try again later")
            await session.close()
            return
        task = asyncio.current_task()
        self._sessions.add(session)
        self._tasks.add(task)
        try:
            await session.run()
        finally:
            self._sessions.discard(session)
            self._tasks.discard(task)


async def _serve(args: argparse.Namespace) -> None:
    server = ChessServer(
        args.host, args.port, max_clients=args.max_clients,
        idle_timeout=args.idle_timeout, resume_window=args.resume_window,
    )
    await server.start()
    # Ctrl-C already stops asyncio.run; make `kill` (SIGTERM) shut down just as cleanly.
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    except NotImplementedError:  # Windows has no add_signal_handler
        pass
    await server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m server", description="CMTP chess server")
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"TCP port (default: {DEFAULT_PORT})")
    parser.add_argument("--max-clients", type=int, default=1000, help="most connections at once")
    parser.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT_SECONDS,
                        help=f"seconds of silence before a client counts as lost (default: {IDLE_TIMEOUT_SECONDS:g})")
    parser.add_argument("--resume-window", type=float, default=RESUME_WINDOW_SECONDS,
                        help=f"seconds a dropped player's seat is held (default: {RESUME_WINDOW_SECONDS:g}; 0 to forfeit at once)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every message sent and received")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    try:
        asyncio.run(_serve(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    log.info("stopped")
