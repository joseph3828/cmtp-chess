"""CMTP (Chess Move Transfer Protocol): framing and messages shared by the server and client."""

from .constants import (
    DEFAULT_PORT, IDLE_TIMEOUT_SECONDS, MAX_PAYLOAD, PING_INTERVAL_SECONDS,
    PROTOCOL_VERSION, RESUME_WINDOW_SECONDS,
)
from .errors import FATAL_ERRORS, ErrorCode, ProtocolError
from .framing import (
    FrameReader, FrameTooLargeError, IncompleteFrameError, encode_frame, read_frame, write_frame,
)
from .messages import (
    BODY_SCHEMAS, GAME_OVER_REASONS, SENDERS, Message, MessageType, Sender,
    decode, encode, frame_message,
)

__all__ = [
    "DEFAULT_PORT", "IDLE_TIMEOUT_SECONDS", "MAX_PAYLOAD", "PING_INTERVAL_SECONDS",
    "PROTOCOL_VERSION", "RESUME_WINDOW_SECONDS",
    "FATAL_ERRORS", "ErrorCode", "ProtocolError",
    "FrameReader", "FrameTooLargeError", "IncompleteFrameError", "encode_frame",
    "read_frame", "write_frame",
    "BODY_SCHEMAS", "GAME_OVER_REASONS", "SENDERS", "Message", "MessageType", "Sender",
    "decode", "encode", "frame_message",
]
