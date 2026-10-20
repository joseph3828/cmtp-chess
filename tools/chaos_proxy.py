"""A TCP proxy that mistreats the bytes passing through it, to show CMTP copes.

It sits between clients and the server and can:

- add a fixed delay to every byte in each direction, like a long-distance link;
- split the stream into tiny pieces, down to one byte per write, so frames
  arrive in fragments and the receiver's buffering has to put them back together;
- delay each piece by a further random amount, like a congested network;
- cut connections, at random or all at once on request, like a network failure.

It never changes or reorders bytes. TCP itself guarantees that, so testing it
would only be testing something TCP already prevents.

Run it in front of a server and point clients at the proxy's port instead:

    python -m tools.chaos_proxy --target-port 5328 --port 6328 --max-chunk 1 --max-delay 0.01
"""

import argparse
import asyncio
import logging
import random
from collections import Counter

from cmtp import DEFAULT_PORT

log = logging.getLogger(__name__)


class ChaosProxy:
    def __init__(
        self,
        target_host: str = "127.0.0.1",
        target_port: int = DEFAULT_PORT,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        latency: float = 0.0,
        max_chunk: int | None = None,
        max_delay: float = 0.0,
        drop_chance: float = 0.0,
        seed: int | None = None,
    ) -> None:
        self.target = (target_host, target_port)
        self.host = host
        self.port = port  # 0 picks a free port; start() fills in the real one
        self.latency = latency  # seconds added to every byte, each way
        self.max_chunk = max_chunk  # largest piece written at once; None forwards reads whole
        self.max_delay = max_delay  # seconds; each piece also waits a random time up to this
        self.drop_chance = drop_chance  # chance, per piece, of cutting the connection
        self.rng = random.Random(seed)
        self.stats: Counter[str] = Counter()
        self._pairs: set[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = set()
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connect, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        self.drop_all()
        if self._server is not None:
            await self._server.wait_closed()

    def drop_all(self) -> int:
        """Cut every connection through the proxy at once. Returns how many were cut."""
        pairs = list(self._pairs)
        for pair in pairs:
            self._cut(pair)
        return len(pairs)

    def _cut(self, pair) -> None:
        # abort() sends a reset instead of closing politely, like a failed network.
        for writer in pair:
            writer.transport.abort()
        self.stats["drops"] += 1

    async def _on_connect(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        try:
            server_reader, server_writer = await asyncio.open_connection(*self.target)
        except OSError as exc:
            log.warning("can't reach %s:%d: %s", *self.target, exc)
            client_writer.close()
            return
        pair = (client_writer, server_writer)
        self._pairs.add(pair)
        self.stats["connections"] += 1
        try:
            await asyncio.gather(
                self._pipe(client_reader, server_writer, pair, "up"),
                self._pipe(server_reader, client_writer, pair, "down"),
            )
        finally:
            self._pairs.discard(pair)

    async def _pipe(self, reader, writer, pair, direction: str) -> None:
        """Copy one direction of the connection, mistreating it on the way."""
        loop = asyncio.get_running_loop()
        in_flight: asyncio.Queue = asyncio.Queue()
        delivery = asyncio.create_task(self._deliver(in_flight, writer, pair, direction))
        try:
            while data := await reader.read(65536):
                # Stamp each read with when it may leave. Every byte is delayed by
                # the same latency without later reads waiting behind earlier ones.
                in_flight.put_nowait((loop.time() + self.latency, data))
        except (ConnectionError, OSError):
            pass
        in_flight.put_nowait(None)
        await delivery

    async def _deliver(self, in_flight: asyncio.Queue, writer, pair, direction: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            while (item := await in_flight.get()) is not None:
                due, data = item
                if due > loop.time():
                    await asyncio.sleep(due - loop.time())
                for piece in self._split(data):
                    if self.max_delay:
                        await asyncio.sleep(self.rng.uniform(0, self.max_delay))
                    if self.drop_chance and self.rng.random() < self.drop_chance:
                        self._cut(pair)
                        return
                    writer.write(piece)
                    await writer.drain()
                    self.stats[f"{direction}_writes"] += 1
                    self.stats[f"{direction}_bytes"] += len(piece)
        except (ConnectionError, OSError):
            pass
        finally:
            # When one side finishes, finish the other too, so neither waits forever.
            for side in pair:
                if not side.is_closing():
                    side.close()

    def _split(self, data: bytes) -> list[bytes]:
        if not self.max_chunk:
            return [data]
        pieces, start = [], 0
        while start < len(data):
            size = self.rng.randint(1, self.max_chunk)
            pieces.append(data[start:start + size])
            start += size
        return pieces


async def _serve(args: argparse.Namespace) -> None:
    proxy = ChaosProxy(
        args.target_host, args.target_port, host=args.host, port=args.port, latency=args.latency,
        max_chunk=args.max_chunk, max_delay=args.max_delay, drop_chance=args.drop_chance, seed=args.seed,
    )
    await proxy.start()
    log.info("forwarding %s:%d -> %s:%d", args.host, proxy.port, args.target_host, args.target_port)
    try:
        await asyncio.Future()
    finally:
        await proxy.close()
        log.info("stats: %s", dict(proxy.stats))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m tools.chaos_proxy", description="A TCP proxy that mistreats traffic")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT + 1000, help=f"port to listen on (default: {DEFAULT_PORT + 1000})")
    parser.add_argument("--target-host", default="127.0.0.1", help="server address")
    parser.add_argument("--target-port", type=int, default=DEFAULT_PORT, help="server port")
    parser.add_argument("--latency", type=float, default=0.0, help="seconds added to every byte, each way")
    parser.add_argument("--max-chunk", type=int, help="split traffic into pieces of at most this many bytes")
    parser.add_argument("--max-delay", type=float, default=0.0, help="delay each piece by up to this many seconds")
    parser.add_argument("--drop-chance", type=float, default=0.0, help="chance per piece of cutting the connection")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
