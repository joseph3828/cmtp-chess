import json

import pytest

from cmtp import (
    SENDERS, ErrorCode, FrameReader, Message, MessageType, ProtocolError, Sender,
    decode, encode, frame_message,
)

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"

# One valid body for every message type. These double as examples for the spec.
SAMPLE_BODIES = {
    MessageType.HELLO: {"name": "Joseph", "resume_token": "r-7f3a"},
    MessageType.LIST_GAMES: {},
    MessageType.CREATE_GAME: {"color": "white"},
    MessageType.JOIN_GAME: {"game_id": "g42"},
    MessageType.CANCEL_GAME: {"game_id": "g42"},
    MessageType.MOVE: {"game_id": "g42", "ply": 0, "move": "e2e4"},
    MessageType.RESIGN: {"game_id": "g42"},
    MessageType.OFFER_DRAW: {"game_id": "g42"},
    MessageType.ANSWER_DRAW: {"game_id": "g42", "accept": False},
    MessageType.GET_STATE: {"game_id": "g42"},
    MessageType.PING: {},
    MessageType.BYE: {},
    MessageType.WELCOME: {"session_id": "s-1", "resume_token": "r-7f3a"},
    MessageType.GAME_LIST: {
        "games": [
            {"game_id": "g42", "host": "Joseph", "host_color": "white"},
            {"game_id": "g43", "host": "Ana", "host_color": None},
        ]
    },
    MessageType.GAME_CREATED: {"game_id": "g42", "color": None},
    MessageType.GAME_CANCELLED: {"game_id": "g42"},
    MessageType.GAME_START: {
        "game_id": "g42", "your_color": "black", "opponent": "Joseph", "fen": START_FEN,
    },
    MessageType.MOVE_MADE: {
        "game_id": "g42", "ply": 0, "move": "e2e4", "san": "e4", "fen": AFTER_E4, "by": "white",
    },
    MessageType.MOVE_REJECTED: {"game_id": "g42", "code": 301, "reason": "it is not your turn"},
    MessageType.DRAW_OFFERED: {"game_id": "g42"},
    MessageType.DRAW_DECLINED: {"game_id": "g42"},
    MessageType.GAME_OVER: {"game_id": "g42", "result": "0-1", "reason": "checkmate"},
    MessageType.OPPONENT_STATUS: {"game_id": "g42", "connected": False},
    MessageType.STATE: {
        "game_id": "g42", "your_color": "white", "opponent": "Ana",
        "moves": ["e2e4"], "fen": AFTER_E4, "turn": "black",
    },
    MessageType.ERROR: {"code": 402, "message": "MOVE body: missing field 'ply'"},
    MessageType.PONG: {},
}


def test_every_message_type_has_a_sample():
    assert set(SAMPLE_BODIES) == set(MessageType)


@pytest.mark.parametrize("message_type", list(MessageType))
def test_round_trip(message_type):
    message = Message(message_type, seq=5, body=SAMPLE_BODIES[message_type], re=4)
    decoded = decode(encode(message), sender=SENDERS[message_type])
    assert decoded == message


def test_wire_format():
    message = Message(MessageType.MOVE, seq=7, body={"game_id": "g42", "ply": 4, "move": "g1f3"})
    assert encode(message) == (
        b'{"v":1,"type":"MOVE","seq":7,"re":null,'
        b'"body":{"game_id":"g42","ply":4,"move":"g1f3"}}'
    )


def test_type_can_be_given_as_a_string():
    assert Message("PING", seq=1).type is MessageType.PING


def test_non_ascii_text_is_sent_as_utf8():
    message = Message(MessageType.HELLO, seq=1, body={"name": "José ♞"})
    payload = encode(message)
    assert "José ♞".encode() in payload
    assert decode(payload, sender=Sender.CLIENT).body["name"] == "José ♞"


def test_optional_fields_can_be_left_out():
    for message_type, body in [
        (MessageType.HELLO, {"name": "Joseph"}),
        (MessageType.CREATE_GAME, {}),
    ]:
        assert decode(encode(Message(message_type, 1, body)), sender=Sender.CLIENT).body == body


def test_re_can_be_left_out_of_the_envelope():
    payload = b'{"v":1,"type":"PING","seq":3,"body":{}}'
    assert decode(payload, sender=Sender.CLIENT) == Message(MessageType.PING, 3)


def test_frame_message_wraps_the_payload_in_a_frame():
    message = Message(MessageType.PING, seq=1)
    [payload] = FrameReader().feed(frame_message(message))
    assert decode(payload, sender=Sender.CLIENT) == message


def test_encode_rejects_an_invalid_body():
    with pytest.raises(ProtocolError) as info:
        encode(Message(MessageType.MOVE, seq=1, body={"game_id": "g42"}))
    assert info.value.code == ErrorCode.INVALID_MESSAGE


def test_encode_rejects_a_bad_sequence_number():
    with pytest.raises(ProtocolError):
        encode(Message(MessageType.PING, seq=0))


def envelope(message_type="MOVE", body=None, **overrides):
    data = {
        "v": 1,
        "type": message_type,
        "seq": 9,
        "re": None,
        "body": {"game_id": "g42", "ply": 0, "move": "e2e4"} if body is None else body,
    }
    data.update(overrides)
    return json.dumps(data).encode()


def move_body(**changes):
    body = {"game_id": "g42", "ply": 0, "move": "e2e4"}
    body.update(changes)
    return envelope(body=body)


BAD = ErrorCode.INVALID_MESSAGE


@pytest.mark.parametrize(
    "payload, code",
    [
        # Not JSON at all
        (b"", ErrorCode.MALFORMED_JSON),
        (b"\xff\xfe\x00", ErrorCode.MALFORMED_JSON),
        (b"{not json", ErrorCode.MALFORMED_JSON),
        (b'{"v":1,"v":1,"type":"PING","seq":1,"body":{}}', ErrorCode.MALFORMED_JSON),
        (b'{"v":1,"type":"MOVE","seq":1,"body":{"game_id":"g","ply":NaN,"move":"e2e4"}}',
         ErrorCode.MALFORMED_JSON),
        (b"[" * 60000, ErrorCode.MALFORMED_JSON),  # deep enough to make the parser give up
        # Envelope
        (b"[1, 2]", BAD),
        (b'"PING"', BAD),
        (b'{"type":"PING","seq":1,"body":{}}', BAD),
        (envelope(v=True), BAD),
        (envelope(v="1"), BAD),
        (envelope(v=2), ErrorCode.UNSUPPORTED_VERSION),
        (envelope(seq=0), BAD),
        (envelope(seq="9"), BAD),
        (envelope(seq=9.0), BAD),
        (envelope(re="x"), BAD),
        (envelope(re=0), BAD),
        (envelope(extra=1), BAD),
        (envelope(body=[]), BAD),
        (envelope(type=7), BAD),
        # Type
        (envelope(type="FOO"), ErrorCode.UNKNOWN_TYPE),
        (envelope(type="move"), ErrorCode.UNKNOWN_TYPE),
        (envelope("WELCOME", {"session_id": "s", "resume_token": "r"}), ErrorCode.UNKNOWN_TYPE),
        # Body
        (envelope(body={"game_id": "g42", "move": "e2e4"}), BAD),  # missing ply
        (move_body(extra="x"), BAD),
        (move_body(ply="0"), BAD),
        (move_body(ply=-1), BAD),
        (move_body(ply=True), BAD),
        (move_body(move="e2e9"), BAD),
        (move_body(move="E2E4"), BAD),
        (move_body(move="e7e8k"), BAD),
        (move_body(game_id=""), BAD),
        (move_body(game_id=None), BAD),
        (envelope("HELLO", {"name": "x" * 33}), BAD),
        (envelope("HELLO", {"name": ""}), BAD),
        (envelope("HELLO", {"name": "Jo\nseph"}), BAD),
        (envelope("HELLO", {}), BAD),
        (envelope("CREATE_GAME", {"color": "green"}), BAD),
        (envelope("ANSWER_DRAW", {"game_id": "g42", "accept": "yes"}), BAD),
        (envelope("PING", {"anything": 1}), BAD),
    ],
)
def test_client_message_errors(payload, code):
    with pytest.raises(ProtocolError) as info:
        decode(payload, sender=Sender.CLIENT)
    assert info.value.code == code


@pytest.mark.parametrize(
    "message_type, body",
    [
        ("MOVE", {"game_id": "g42", "ply": 0, "move": "e2e4"}),  # a client message
        ("GAME_LIST", {"games": [{"game_id": "g42", "host": "Joseph"}]}),
        ("GAME_LIST", {"games": "g42"}),
        ("STATE", {**SAMPLE_BODIES[MessageType.STATE], "moves": ["e2e4", "nope"]}),
        ("ERROR", {"code": 99, "message": "too low"}),
        ("ERROR", {"code": 600, "message": "too high"}),
        ("GAME_OVER", {"game_id": "g42", "result": "1-0", "reason": "boredom"}),
        ("GAME_OVER", {"game_id": "g42", "result": "2-0", "reason": "checkmate"}),
    ],
)
def test_server_message_errors(message_type, body):
    with pytest.raises(ProtocolError):
        decode(envelope(message_type, body), sender=Sender.SERVER)


def test_error_message_names_the_problem():
    with pytest.raises(ProtocolError) as info:
        decode(envelope(body={"game_id": "g42", "move": "e2e4"}), sender=Sender.CLIENT)
    assert info.value.message == "MOVE body: missing field 'ply'"

    with pytest.raises(ProtocolError) as info:
        decode(move_body(move="e2e9"), sender=Sender.CLIENT)
    assert info.value.message == "MOVE body: 'move' must be a UCI move such as e2e4 or e7e8q"


def test_error_keeps_the_sequence_number_when_readable():
    with pytest.raises(ProtocolError) as info:
        decode(move_body(ply=-1), sender=Sender.CLIENT)
    assert info.value.seq == 9

    with pytest.raises(ProtocolError) as info:
        decode(envelope(seq="9"), sender=Sender.CLIENT)
    assert info.value.seq is None


def test_error_codes_are_grouped_by_hundreds():
    for code in ErrorCode:
        assert 100 <= code < 600
    assert ErrorCode.UNSUPPORTED_VERSION // 100 == 1
    assert ErrorCode.GAME_FULL // 100 == 2
    assert ErrorCode.STALE_PLY // 100 == 3
    assert ErrorCode.MALFORMED_JSON // 100 == 4
    assert ErrorCode.INTERNAL_ERROR // 100 == 5
