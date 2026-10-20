"""Moves and UCI move text, such as ``"e2e4"`` or ``"e7e8q"``."""

import re
from dataclasses import dataclass

from .squares import parse_square, square_name

# Bit flags describing what a move does. Move generation sets them so that
# making a move and writing it as SAN don't have to work it out again.
CAPTURE = 1
DOUBLE_PUSH = 2
EN_PASSANT = 4
CASTLE = 8

PROMOTION_PIECES = "qrbn"

_UCI_PATTERN = re.compile(r"([a-h][1-8])([a-h][1-8])([qrbn]?)")


@dataclass(frozen=True, slots=True)
class Move:
    from_sq: int
    to_sq: int
    promotion: str | None = None  # lowercase piece letter, or None
    flags: int = 0

    @property
    def is_capture(self) -> bool:
        return bool(self.flags & CAPTURE)

    @property
    def is_castle(self) -> bool:
        return bool(self.flags & CASTLE)

    @property
    def is_en_passant(self) -> bool:
        return bool(self.flags & EN_PASSANT)

    def uci(self) -> str:
        return square_name(self.from_sq) + square_name(self.to_sq) + (self.promotion or "")

    def __str__(self) -> str:
        return self.uci()


def parse_uci(text: str) -> tuple[int, int, str | None]:
    """Split UCI move text into ``(from_sq, to_sq, promotion)``.

    This only checks the format. Whether the move is legal depends on the
    position, so callers compare the result against the legal moves.
    """
    match = _UCI_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError(f"not a UCI move: {text!r}")
    from_name, to_name, promotion = match.groups()
    return parse_square(from_name), parse_square(to_name), promotion or None
