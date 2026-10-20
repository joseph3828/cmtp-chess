"""Length-prefixed framing: how CMTP turns TCP's byte stream back into messages.

TCP delivers a stream of bytes, not separate messages. One read can return
half a message, or two and a half. So every frame starts with a 4-byte
big-endian unsigned length, followed by exactly that many bytes of payload:

    +----------------------+-------------------------------+
    | length: uint32 (BE)  | payload: ``length`` bytes      |
    +----------------------+-------------------------------+

The receiver buffers bytes until a whole frame has arrived, then hands the
payload on. ``FrameReader`` does that for any byte source; ``read_frame`` and
``write_frame`` do it for asyncio streams.
"""

import asyncio
import struct

from .constants import MAX_PAYLOAD
from .errors import ErrorCode, ProtocolError

HEADER = struct.Struct(">I")


class FrameTooLargeError(ProtocolError):
    """A frame's length is over the limit, so the stream can't be trusted."""

    def __init__(self, length: int, limit: int = MAX_PAYLOAD) -> None:
        super().__init__(
            ErrorCode.FRAME_TOO_LARGE, f"frame of {length} bytes is over the {limit}-byte limit"
        )
        self.length = length


class IncompleteFrameError(ConnectionError):
    """The connection closed partway through a frame."""


def encode_frame(payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise FrameTooLargeError(len(payload))
    return HEADER.pack(len(payload)) + payload


class FrameReader:
    """Collects bytes as they arrive and returns each complete frame's payload."""

    def __init__(self, max_payload: int = MAX_PAYLOAD) -> None:
        self._buffer = bytearray()
        self._max_payload = max_payload

    def feed(self, data: bytes) -> list[bytes]:
        """Add received bytes; return the payloads of any frames now complete.

        Raises ``FrameTooLargeError`` as soon as a header announces too large a
        frame, without waiting for its bytes.
        """
        self._buffer += data
        payloads = []
        while len(self._buffer) >= HEADER.size:
            (length,) = HEADER.unpack_from(self._buffer)
            if length > self._max_payload:
                raise FrameTooLargeError(length, self._max_payload)
            end = HEADER.size + length
            if len(self._buffer) < end:
                break
            payloads.append(bytes(self._buffer[HEADER.size:end]))
            del self._buffer[:end]
        return payloads

    @property
    def pending(self) -> int:
        """Bytes received that don't make up a whole frame yet.

        If the connection closes while this is nonzero, a frame was cut off.
        """
        return len(self._buffer)


async def read_frame(reader: asyncio.StreamReader, max_payload: int = MAX_PAYLOAD) -> bytes | None:
    """Read one frame's payload, or return ``None`` if the peer closed cleanly between frames."""
    try:
        header = await reader.readexactly(HEADER.size)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise IncompleteFrameError("connection closed partway through a frame header") from None
    (length,) = HEADER.unpack(header)
    if length > max_payload:
        raise FrameTooLargeError(length, max_payload)
    try:
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        raise IncompleteFrameError("connection closed partway through a frame") from None


async def write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(encode_frame(payload))
    await writer.drain()
