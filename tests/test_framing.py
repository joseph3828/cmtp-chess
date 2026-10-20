import asyncio

import pytest

from cmtp import (
    MAX_PAYLOAD, ErrorCode, FrameReader, FrameTooLargeError, IncompleteFrameError,
    encode_frame, read_frame, write_frame,
)


def test_header_is_four_byte_big_endian_length():
    assert encode_frame(b"abc") == b"\x00\x00\x00\x03abc"
    assert encode_frame(b"") == b"\x00\x00\x00\x00"


def test_single_frame():
    assert FrameReader().feed(encode_frame(b"hello")) == [b"hello"]


def test_frame_split_into_single_bytes():
    reader = FrameReader()
    data = encode_frame(b"hello")
    received = []
    for i in range(len(data)):
        received += reader.feed(data[i:i + 1])
        if i < len(data) - 1:
            assert received == []  # nothing until the last byte arrives
    assert received == [b"hello"]
    assert reader.pending == 0


def test_several_frames_in_one_read():
    data = encode_frame(b"one") + encode_frame(b"two") + encode_frame(b"three")
    assert FrameReader().feed(data) == [b"one", b"two", b"three"]


def test_two_and_a_half_frames():
    reader = FrameReader()
    third = encode_frame(b"third")
    data = encode_frame(b"first") + encode_frame(b"second") + third[:6]
    assert reader.feed(data) == [b"first", b"second"]
    assert reader.pending == 6
    assert reader.feed(third[6:]) == [b"third"]
    assert reader.pending == 0


def test_zero_length_frame():
    assert FrameReader().feed(encode_frame(b"") + encode_frame(b"x")) == [b"", b"x"]


def test_largest_allowed_frame():
    payload = b"x" * MAX_PAYLOAD
    assert FrameReader().feed(encode_frame(payload)) == [payload]


def test_encoding_too_large_a_frame_fails():
    with pytest.raises(FrameTooLargeError) as info:
        encode_frame(b"x" * (MAX_PAYLOAD + 1))
    assert info.value.code == ErrorCode.FRAME_TOO_LARGE
    assert info.value.fatal


def test_reader_rejects_large_header_without_waiting_for_the_body():
    header = (MAX_PAYLOAD + 1).to_bytes(4, "big")
    with pytest.raises(FrameTooLargeError) as info:
        FrameReader().feed(header)
    assert info.value.length == MAX_PAYLOAD + 1


def test_reader_rejects_garbage_that_looks_like_a_huge_length():
    with pytest.raises(FrameTooLargeError):
        FrameReader().feed(b"GET / HTTP/1.1\r\n")


# asyncio stream helpers


def stream_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_read_frames_then_clean_close():
    reader = stream_with(encode_frame(b"a") + encode_frame(b"bc"))
    assert await read_frame(reader) == b"a"
    assert await read_frame(reader) == b"bc"
    assert await read_frame(reader) is None


@pytest.mark.asyncio
async def test_close_in_the_middle_of_a_header():
    with pytest.raises(IncompleteFrameError):
        await read_frame(stream_with(b"\x00\x00"))


@pytest.mark.asyncio
async def test_close_in_the_middle_of_a_payload():
    with pytest.raises(IncompleteFrameError):
        await read_frame(stream_with(encode_frame(b"hello")[:-1]))


@pytest.mark.asyncio
async def test_read_rejects_large_header():
    with pytest.raises(FrameTooLargeError):
        await read_frame(stream_with((MAX_PAYLOAD + 1).to_bytes(4, "big")))


@pytest.mark.asyncio
async def test_frames_over_real_tcp_even_when_written_one_byte_at_a_time():
    """An echo server reads frames; the client dribbles them out a byte at a time."""

    async def echo(reader, writer):
        while (payload := await read_frame(reader)) is not None:
            await write_frame(writer, payload)
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        payloads = [b"first", b"", "second ♞".encode(), b"x" * 5000]
        for byte in b"".join(encode_frame(p) for p in payloads):
            writer.write(bytes([byte]))
            await writer.drain()
        echoed = [await read_frame(reader) for _ in payloads]
        writer.close()
        await writer.wait_closed()
    assert echoed == payloads
