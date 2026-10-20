"""CMTP messages: the JSON envelope, the message catalog, and validation.

Every frame's payload is one UTF-8 JSON object:

    {"v": 1, "type": "MOVE", "seq": 7, "re": null, "body": {...}}

- ``v``: protocol version.
- ``type``: one of ``MessageType``; each type is sent by only the client or only the server.
- ``seq``: counts the messages each side has sent on this connection, starting at 1.
- ``re``: the ``seq`` of the message this one answers, or null.
- ``body``: the type's fields, as listed in ``BODY_SCHEMAS``.

Validation is strict in both directions: a missing field, an extra field, or a
value of the wrong kind is an error with a specific code. A bad message is
reported instead of being half-understood.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NoReturn

from .constants import PROTOCOL_VERSION
from .errors import ErrorCode, ProtocolError
from .framing import encode_frame


class Sender(StrEnum):
    CLIENT = "client"
    SERVER = "server"


class MessageType(StrEnum):
    # Client to server
    HELLO = "HELLO"
    LIST_GAMES = "LIST_GAMES"
    CREATE_GAME = "CREATE_GAME"
    JOIN_GAME = "JOIN_GAME"
    CANCEL_GAME = "CANCEL_GAME"
    MOVE = "MOVE"
    RESIGN = "RESIGN"
    OFFER_DRAW = "OFFER_DRAW"
    ANSWER_DRAW = "ANSWER_DRAW"
    GET_STATE = "GET_STATE"
    PING = "PING"
    BYE = "BYE"

    # Server to client
    WELCOME = "WELCOME"
    GAME_LIST = "GAME_LIST"
    GAME_CREATED = "GAME_CREATED"
    GAME_CANCELLED = "GAME_CANCELLED"
    GAME_START = "GAME_START"
    MOVE_MADE = "MOVE_MADE"
    MOVE_REJECTED = "MOVE_REJECTED"
    DRAW_OFFERED = "DRAW_OFFERED"
    DRAW_DECLINED = "DRAW_DECLINED"
    GAME_OVER = "GAME_OVER"
    OPPONENT_STATUS = "OPPONENT_STATUS"
    STATE = "STATE"
    ERROR = "ERROR"
    PONG = "PONG"


GAME_OVER_REASONS = (
    "checkmate", "stalemate", "insufficient_material", "fifty_move",
    "threefold_repetition", "resignation", "agreement", "abandoned",
)

# Field checks. Each returns a short description of what's wrong with a
# value, or None if the value is fine.
Check = Callable[[Any], str | None]


def _is_int(value: Any) -> bool:
    # bool is a subclass of int in Python, but true/false aren't numbers in JSON.
    return isinstance(value, int) and not isinstance(value, bool)


def _text(max_len: int, pattern: re.Pattern | None = None, hint: str = "") -> Check:
    def check(value):
        if not isinstance(value, str):
            return "must be a string"
        if not 1 <= len(value) <= max_len:
            return f"must be 1 to {max_len} characters"
        if not value.isprintable():
            return "must not contain control characters"
        if pattern is not None and not pattern.fullmatch(value):
            return f"must be {hint}"
        return None
    return check


def _choice(*options: str) -> Check:
    def check(value):
        return None if value in options else f"must be one of {', '.join(options)}"
    return check


def _integer(minimum: int = 0, maximum: int | None = None) -> Check:
    def check(value):
        if not _is_int(value):
            return "must be an integer"
        if value < minimum or (maximum is not None and value > maximum):
            upper = f" and at most {maximum}" if maximum is not None else ""
            return f"must be at least {minimum}{upper}"
        return None
    return check


def _boolean(value: Any) -> str | None:
    return None if isinstance(value, bool) else "must be true or false"


def _nullable(check: Check) -> Check:
    def nullable_check(value):
        return None if value is None else check(value)
    return nullable_check


def _list_of(check: Check) -> Check:
    def list_check(value):
        if not isinstance(value, list):
            return "must be a list"
        for index, item in enumerate(value):
            problem = check(item)
            if problem:
                return f"item {index} {problem}"
        return None
    return list_check


def _object(schema: dict[str, Check]) -> Check:
    def object_check(value):
        problem = _object_problem(value, schema)
        return f"({problem})" if problem else None
    return object_check


def _object_problem(value: Any, schema: dict[str, Check]) -> str | None:
    """Check a JSON object against a schema. A key ending in ``?`` is optional."""
    if not isinstance(value, dict):
        return "must be an object"
    allowed = set()
    for key, check in schema.items():
        name = key.removesuffix("?")
        allowed.add(name)
        if name not in value:
            if key.endswith("?"):
                continue
            return f"missing field {name!r}"
        problem = check(value[name])
        if problem:
            return f"{name!r} {problem}"
    extra = sorted(value.keys() - allowed)
    if extra:
        return f"unexpected field {extra[0]!r}"
    return None


_UCI_PATTERN = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?")

_NAME = _text(32)
_ID = _text(64)
_TOKEN = _text(128)
_FEN = _text(100)
_SAN = _text(10)
_UCI = _text(5, _UCI_PATTERN, "a UCI move such as e2e4 or e7e8q")
_DETAIL = _text(500)
_PLY = _integer(0)
_ERROR_CODE = _integer(100, 599)
_COLOR = _choice("white", "black")
_RESULT = _choice("1-0", "0-1", "1/2-1/2")
_REASON = _choice(*GAME_OVER_REASONS)

T = MessageType
BODY_SCHEMAS: dict[MessageType, dict[str, Check]] = {
    T.HELLO: {"name": _NAME, "resume_token?": _TOKEN},
    T.LIST_GAMES: {},
    T.CREATE_GAME: {"color?": _COLOR},  # leave out for a random color
    T.JOIN_GAME: {"game_id": _ID},
    T.CANCEL_GAME: {"game_id": _ID},  # only before an opponent joins
    T.MOVE: {"game_id": _ID, "ply": _PLY, "move": _UCI},
    T.RESIGN: {"game_id": _ID},
    T.OFFER_DRAW: {"game_id": _ID},
    T.ANSWER_DRAW: {"game_id": _ID, "accept": _boolean},
    T.GET_STATE: {"game_id": _ID},
    T.PING: {},
    T.BYE: {},

    T.WELCOME: {"session_id": _TOKEN, "resume_token": _TOKEN},
    T.GAME_LIST: {
        "games": _list_of(_object({"game_id": _ID, "host": _NAME, "host_color": _nullable(_COLOR)})),
    },
    T.GAME_CREATED: {"game_id": _ID, "color": _nullable(_COLOR)},
    T.GAME_CANCELLED: {"game_id": _ID},
    T.GAME_START: {"game_id": _ID, "your_color": _COLOR, "opponent": _NAME, "fen": _FEN},
    T.MOVE_MADE: {
        "game_id": _ID, "ply": _PLY, "move": _UCI, "san": _SAN, "fen": _FEN, "by": _COLOR,
    },
    T.MOVE_REJECTED: {"game_id": _ID, "code": _ERROR_CODE, "reason": _DETAIL},
    T.DRAW_OFFERED: {"game_id": _ID},
    T.DRAW_DECLINED: {"game_id": _ID},
    T.GAME_OVER: {"game_id": _ID, "result": _RESULT, "reason": _REASON},
    T.OPPONENT_STATUS: {"game_id": _ID, "connected": _boolean},
    T.STATE: {
        "game_id": _ID, "your_color": _COLOR, "opponent": _NAME,
        "moves": _list_of(_UCI), "fen": _FEN, "turn": _COLOR,
    },
    T.ERROR: {"code": _ERROR_CODE, "message": _DETAIL},
    T.PONG: {},
}

_CLIENT_TYPES = {
    T.HELLO, T.LIST_GAMES, T.CREATE_GAME, T.JOIN_GAME, T.CANCEL_GAME, T.MOVE, T.RESIGN,
    T.OFFER_DRAW, T.ANSWER_DRAW, T.GET_STATE, T.PING, T.BYE,
}
SENDERS: dict[MessageType, Sender] = {
    t: Sender.CLIENT if t in _CLIENT_TYPES else Sender.SERVER for t in MessageType
}
del T

# The envelope around every body. "v" is checked on its own first, so a
# future version with a different envelope gets a version error, not a
# confusing schema error.
_ENVELOPE_SCHEMA: dict[str, Check] = {
    "v": _integer(1),
    "type": _text(32),
    "seq": _integer(1),
    "re?": _nullable(_integer(1)),
    "body": lambda value: None if isinstance(value, dict) else "must be an object",
}


@dataclass(frozen=True, slots=True)
class Message:
    type: MessageType
    seq: int
    body: dict[str, Any] = field(default_factory=dict)
    re: int | None = None

    def __post_init__(self) -> None:
        # Accept plain strings like "MOVE" too.
        object.__setattr__(self, "type", MessageType(self.type))

    @property
    def sender(self) -> Sender:
        return SENDERS[self.type]


def encode(message: Message) -> bytes:
    """Turn a message into payload bytes, checking it against the schema first.

    Checking outgoing messages too means a bug on this side shows up here,
    instead of as a confusing error from the peer.
    """
    envelope = {
        "v": PROTOCOL_VERSION,
        "type": message.type.value,
        "seq": message.seq,
        "re": message.re,
        "body": message.body,
    }
    problem = _object_problem(envelope, _ENVELOPE_SCHEMA)
    if problem:
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, f"envelope: {problem}")
    problem = _object_problem(message.body, BODY_SCHEMAS[message.type])
    if problem:
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, f"{message.type} body: {problem}")
    return json.dumps(
        envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def frame_message(message: Message) -> bytes:
    """Encode a message and wrap it in a frame, ready to send."""
    return encode_frame(encode(message))


def decode(payload: bytes, *, sender: Sender) -> Message:
    """Parse and validate one frame's payload, sent by ``sender``.

    Raises ``ProtocolError`` with the code to report. Its ``seq`` is set when
    the envelope's ``seq`` was readable.
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError(ErrorCode.MALFORMED_JSON, "payload is not valid UTF-8") from None
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except ValueError as exc:
        raise ProtocolError(ErrorCode.MALFORMED_JSON, f"payload is not valid JSON: {exc}") from None
    except RecursionError:
        # Something like [[[[...]]]] thousands deep. It fits in a frame, but
        # Python's parser gives up, and not with a ValueError.
        raise ProtocolError(ErrorCode.MALFORMED_JSON, "payload is nested too deeply") from None
    if not isinstance(data, dict):
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "message must be a JSON object")

    seq = data.get("seq")
    seq = seq if _is_int(seq) and seq >= 1 else None

    def fail(code: ErrorCode, detail: str) -> NoReturn:
        raise ProtocolError(code, detail, seq=seq)

    version = data.get("v")
    if not _is_int(version):
        fail(ErrorCode.INVALID_MESSAGE, "'v' must be an integer")
    if version != PROTOCOL_VERSION:
        fail(
            ErrorCode.UNSUPPORTED_VERSION,
            f"version {version} is not supported; this side speaks version {PROTOCOL_VERSION}",
        )

    problem = _object_problem(data, _ENVELOPE_SCHEMA)
    if problem:
        fail(ErrorCode.INVALID_MESSAGE, f"envelope: {problem}")

    try:
        message_type = MessageType(data["type"])
    except ValueError:
        fail(ErrorCode.UNKNOWN_TYPE, f"unknown message type {data['type']!r}")
    if SENDERS[message_type] != sender:
        fail(ErrorCode.UNKNOWN_TYPE, f"{message_type} is not a message the {sender} sends")

    problem = _object_problem(data["body"], BODY_SCHEMAS[message_type])
    if problem:
        fail(ErrorCode.INVALID_MESSAGE, f"{message_type} body: {problem}")

    return Message(message_type, data["seq"], data["body"], data.get("re"))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # Plain json.loads keeps the last of two equal keys; the sender may have
    # meant the first, so treat it as a malformed payload.
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    # json.loads accepts NaN and Infinity by default, but they aren't JSON.
    raise ValueError(f"{name} is not allowed")
