"""Error codes sent in ERROR and MOVE_REJECTED messages, and the exception that carries them."""

from enum import IntEnum, unique


@unique
class ErrorCode(IntEnum):
    """Grouped by hundreds, like HTTP status codes."""

    # 1xx: handshake and session
    HELLO_REQUIRED = 100  # a message other than HELLO arrived before HELLO
    UNSUPPORTED_VERSION = 101
    ALREADY_GREETED = 102  # HELLO sent twice
    BAD_RESUME_TOKEN = 103

    # 2xx: lobby
    GAME_NOT_FOUND = 200
    GAME_FULL = 201
    ALREADY_IN_GAME = 202
    NOT_IN_GAME = 203
    CANNOT_JOIN_OWN_GAME = 204
    GAME_NOT_STARTED = 205  # the game is still waiting for an opponent
    GAME_ALREADY_STARTED = 206  # too late to cancel; resign instead

    # 3xx: moves and other game actions
    ILLEGAL_MOVE = 300
    NOT_YOUR_TURN = 301
    STALE_PLY = 302  # the move's ply doesn't match the game's current ply
    GAME_ALREADY_OVER = 303
    NO_DRAW_OFFER = 304  # ANSWER_DRAW with no draw offer pending

    # 4xx: framing and malformed messages
    FRAME_TOO_LARGE = 400
    MALFORMED_JSON = 401
    INVALID_MESSAGE = 402  # valid JSON, but the envelope or body breaks the schema
    UNKNOWN_TYPE = 403  # unknown type, or one the sender isn't allowed to send

    # 5xx: server
    INTERNAL_ERROR = 500
    SERVER_FULL = 501
    SHUTTING_DOWN = 502


# After sending one of these, the sender closes the connection. A bad frame
# length means the byte stream can't be trusted any more, and the other codes
# mean there is nothing useful left to say.
FATAL_ERRORS = frozenset({
    ErrorCode.UNSUPPORTED_VERSION,
    ErrorCode.FRAME_TOO_LARGE,
    ErrorCode.SERVER_FULL,
    ErrorCode.SHUTTING_DOWN,
})


class ProtocolError(Exception):
    """Something broke the protocol. ``code`` is what to report to the peer.

    ``seq`` is the sequence number of the offending message when it could be
    read, so the ERROR reply can point back at it with ``re``.
    """

    def __init__(self, code: ErrorCode, message: str, *, seq: int | None = None) -> None:
        super().__init__(f"{code.value} {code.name}: {message}")
        self.code = code
        self.message = message
        self.seq = seq

    @property
    def fatal(self) -> bool:
        return self.code in FATAL_ERRORS
