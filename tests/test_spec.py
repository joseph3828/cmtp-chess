"""The protocol spec must match the code: every example is a real message, and nothing is left out."""

import json
import re
from pathlib import Path

import pytest

from cmtp import (
    DEFAULT_PORT, IDLE_TIMEOUT_SECONDS, MAX_PAYLOAD, PING_INTERVAL_SECONDS, PROTOCOL_VERSION,
    RESUME_WINDOW_SECONDS, ErrorCode, Message, MessageType, Sender, decode, frame_message,
)

SPEC = (Path(__file__).parent.parent / "docs" / "protocol-spec.md").read_text()
EXAMPLES = re.findall(r"```json (client|server)\n(.*?)\n```", SPEC, re.S)


def hexdump(data: bytes) -> str:
    lines = []
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:04x}  {hex_part:<47}  {text}")
    return "\n".join(lines)


@pytest.mark.parametrize(
    "sender, example", EXAMPLES, ids=[f"{s}-{json.loads(e)['type']}" for s, e in EXAMPLES]
)
def test_every_example_is_a_valid_message(sender, example):
    decode(example.encode(), sender=Sender(sender))


def test_every_message_type_has_a_section_and_an_example():
    shown = {json.loads(example)["type"] for _, example in EXAMPLES}
    assert shown == {t.value for t in MessageType}
    for message_type in MessageType:
        assert f"#### `{message_type.value}`" in SPEC


def test_every_error_code_is_in_the_table():
    for code in ErrorCode:
        assert re.search(rf"\| {code.value} \| `{code.name}` \|", SPEC), code


def test_fatal_codes_match():
    from cmtp import FATAL_ERRORS
    marked = {int(n) for n in re.findall(r"\| (\d{3}) \| `[A-Z_]+` \| [^|]+\| yes \|", SPEC)}
    assert marked == {code.value for code in FATAL_ERRORS}


def test_hex_dump_is_a_real_frame():
    block = re.search(r"```text hexdump\n(.*?)\n```", SPEC, re.S).group(1)
    frame = frame_message(Message(MessageType.MOVE, 3, {"game_id": "g1", "ply": 0, "move": "e2e4"}))
    assert block == hexdump(frame)


def test_constants_match():
    rows = dict(re.findall(r"^\| ([A-Z][^|]+?) \| ([^|]+?) \|$", SPEC.split("## 18. Constants")[1], re.M))
    assert rows["Protocol version"] == str(PROTOCOL_VERSION)
    assert rows["Default port"] == str(DEFAULT_PORT)
    assert rows["Largest frame payload"] == f"{MAX_PAYLOAD:,} bytes"
    assert rows["Heartbeat interval (client)"] == f"{PING_INTERVAL_SECONDS:g} seconds"
    assert rows["Idle timeout (both sides)"] == f"{IDLE_TIMEOUT_SECONDS:g} seconds"
    assert rows["Resume window"] == f"{RESUME_WINDOW_SECONDS:g} seconds"
