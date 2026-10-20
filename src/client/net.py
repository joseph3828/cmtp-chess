"""The client's connection to the server, run on background threads.

The CLI and the GUI both have a main loop that must never stop to wait for the
network. So a reader thread receives frames, decodes them, and puts each
message on a queue; the main loop takes messages off the queue when it's
ready. Sending happens directly from the caller's thread, under a lock so two
threads can never interleave the bytes of two frames.

Two things happen here without the UI's help:

- Heartbeats. A second thread sends PING every few seconds, which keeps the
  server from timing the client out. If nothing at all comes back from the
  server for too long, the connection is treated as lost, because TCP alone
  can take many minutes to notice a dead network.
- Resuming. If the connection drops (but not because the server shut down or
  the user quit), the reader thread reconnects and sends HELLO with the
  resume token from the last WELCOME, so the player gets their seat back.
  The UI only sees a ``Reconnecting`` event, then ``Reconnected``, then the
  usual WELCOME and STATE messages.
"""

import queue
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any

from cmtp import (
    DEFAULT_PORT, IDLE_TIMEOUT_SECONDS, PING_INTERVAL_SECONDS, RESUME_WINDOW_SECONDS, ErrorCode,
    FrameReader, Message, MessageType, ProtocolError, Sender, decode, frame_message,
)

T = MessageType


@dataclass(frozen=True)
class Closed:
    """The connection has ended for good. Always the last event."""

    reason: str


@dataclass(frozen=True)
class Reconnecting:
    """The connection dropped; the client is trying to get back in."""

    reason: str


@dataclass(frozen=True)
class Reconnected:
    """Back online. ``resumed`` is False if the server had nothing to resume, so the player starts fresh in the lobby."""

    resumed: bool


Event = Message | Closed | Reconnecting | Reconnected


class Connection:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        *,
        timeout: float = 5.0,
        heartbeat: float | None = PING_INTERVAL_SECONDS,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
        reconnect_for: float = RESUME_WINDOW_SECONDS,
        retry_delay: float = 1.0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout  # for opening a connection; once open, reads wait as long as needed
        self.heartbeat = heartbeat  # seconds between PINGs, or None for none
        self.idle_timeout = idle_timeout  # this long without hearing from the server means it's gone
        self.reconnect_for = reconnect_for  # keep trying to get back in this long; 0 never tries
        self.retry_delay = retry_delay
        self.last_rtt: float | None = None  # seconds, from the latest heartbeat

        self._events: queue.Queue[Event] = queue.Queue()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()  # set by close(); wakes threads that are waiting
        self._sock: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._seq = 0
        self._closing = False
        self._reconnecting = False
        self._last_received = 0.0
        self._lost_reason: str | None = None  # why the heartbeat thread cut the connection
        self._no_way_back = False  # the server shut down or misbehaved, so don't reconnect
        self._name: str | None = None  # from the HELLO the UI sent, for HELLO on reconnect
        self._resume_token: str | None = None  # from the latest WELCOME
        self._resume_seq: int | None = None  # seq of the HELLO sent to resume
        self._heartbeats: dict[int, float] = {}  # seq of each heartbeat PING -> when it was sent

    @property
    def connected(self) -> bool:
        return (
            self._sock is not None and not self._closing and not self._reconnecting
            and self._threads[0].is_alive()
        )

    def connect(self) -> None:
        """Open the TCP connection and start the threads. Raises ``OSError`` on failure."""
        self._sock = self._open()
        self._last_received = time.monotonic()
        self._threads = [threading.Thread(target=self._run, name="cmtp-reader", daemon=True)]
        if self.heartbeat:
            self._threads.append(threading.Thread(target=self._send_heartbeats, name="cmtp-heartbeat", daemon=True))
        for thread in self._threads:
            thread.start()

    def _open(self) -> socket.socket:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock

    def send(self, message_type: MessageType | str, body: dict[str, Any] | None = None) -> int:
        """Send a message and return its ``seq``. Raises ``OSError`` if not connected right now."""
        with self._send_lock:
            if self._sock is None or self._closing or self._reconnecting:
                raise ConnectionError("not connected")
            return self._send_locked(message_type, body)

    def _send_locked(self, message_type: MessageType | str, body: dict[str, Any] | None = None) -> int:
        self._seq += 1
        message = Message(message_type, self._seq, body or {})
        if message.type is T.HELLO:
            self._name = message.body["name"]
        self._sock.sendall(frame_message(message))
        return self._seq

    def get(self, timeout: float | None = None) -> Event | None:
        """Wait for the next event. Returns ``None`` if ``timeout`` runs out."""
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def poll(self) -> list[Event]:
        """Everything that has arrived so far, without waiting."""
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def close(self) -> None:
        """Say BYE and disconnect. Safe to call more than once."""
        if self._sock is None or self._closing:
            return
        # Mark closing first, so the reader doesn't take the server's goodbye for a dropped connection.
        self._closing = True
        self._stop.set()
        with self._send_lock:
            try:
                if not self._reconnecting:
                    self._send_locked(T.BYE)
            except OSError:
                pass
            self._shut(self._sock)
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)

    @staticmethod
    def _shut(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    # Reader thread

    def _run(self) -> None:
        reason = self._read_until_lost()
        while self._should_reconnect():
            self._events.put(Reconnecting(reason))
            if not self._reconnect():
                if not self._closing:
                    reason = f"couldn't get back in within {self.reconnect_for:g} s ({reason})"
                break
            reason = self._read_until_lost()
        self._events.put(Closed("disconnected" if self._closing else reason))

    def _read_until_lost(self) -> str:
        """Receive and deliver messages until the connection fails. Returns why it did."""
        sock = self._sock
        frames = FrameReader()
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    if frames.pending:
                        return "the connection closed partway through a message"
                    return self._lost_reason or "the server closed the connection"
                self._last_received = time.monotonic()
                for payload in frames.feed(data):
                    self._deliver(decode(payload, sender=Sender.SERVER))
        except ProtocolError as exc:
            self._no_way_back = True  # a server that sends garbage won't do better next time
            self._shut(sock)
            return f"the server sent a bad message ({exc})"
        except OSError as exc:
            return self._lost_reason or exc.strerror or str(exc)  # such as "Connection reset by peer"

    def _deliver(self, message: Message) -> None:
        """Pass a message to the UI, after handling the ones that belong to this layer."""
        if message.type is T.PONG and message.re in self._heartbeats:
            self.last_rtt = time.monotonic() - self._heartbeats.pop(message.re)
            return  # the UI didn't send this PING, so it doesn't need the PONG
        if message.type is T.ERROR and message.body["code"] in (ErrorCode.SHUTTING_DOWN, ErrorCode.SERVER_FULL):
            self._no_way_back = True
        if self._reconnecting and self._resume_seq is not None and message.re == self._resume_seq:
            if message.type is T.ERROR and message.body["code"] == ErrorCode.BAD_RESUME_TOKEN:
                # Nothing to resume (the seat wasn't held, or it expired). Start over as a new session.
                with self._send_lock:
                    self._resume_seq = None
                    self._send_locked(T.HELLO, {"name": self._name})
                return
        if message.type is T.WELCOME:
            self._resume_token = message.body["resume_token"]
            if self._reconnecting:
                resumed = self._resume_seq is not None
                self._resume_seq = None
                self._events.put(message)
                self._events.put(Reconnected(resumed))
                self._reconnecting = False
                return
        self._events.put(message)

    def _should_reconnect(self) -> bool:
        return (
            not self._closing and not self._no_way_back and self.reconnect_for > 0
            and self._name is not None and self._resume_token is not None
        )

    def _reconnect(self) -> bool:
        """Open a new connection and ask to resume. Returns False if it gave up."""
        with self._send_lock:
            self._reconnecting = True
            self._shut(self._sock)
        deadline = time.monotonic() + self.reconnect_for
        while not self._closing and time.monotonic() < deadline:
            try:
                sock = self._open()
            except OSError:
                if self._stop.wait(self.retry_delay):
                    return False
                continue
            with self._send_lock:
                if self._closing:
                    self._shut(sock)
                    return False
                self._sock = sock
                self._seq = 0  # seq numbers are per connection
                self._heartbeats.clear()
                self._lost_reason = None
                self._last_received = time.monotonic()
                self._resume_seq = self._send_locked(T.HELLO, {"name": self._name, "resume_token": self._resume_token})
            return True
        return False

    # Heartbeat thread

    def _send_heartbeats(self) -> None:
        while not self._stop.wait(self.heartbeat):
            if self._reconnecting:
                continue
            if time.monotonic() - self._last_received > self.idle_timeout:
                # The server has gone quiet. Cutting the socket wakes the reader,
                # which then reconnects or reports the connection closed.
                self._lost_reason = "the server stopped responding"
                self._shut(self._sock)
                continue
            with self._send_lock:
                if self._closing or self._reconnecting:
                    continue
                try:
                    seq = self._send_locked(T.PING)
                except OSError:
                    continue  # the reader thread will notice the connection is gone
                self._heartbeats[seq] = time.monotonic()
