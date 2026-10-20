"""Graphical client for CMTP chess, using pygame-ce. Run it with ``python -m client.gui``.

Click one of your pieces to see where it can go, then click a square to move
it. The panel on the right has the lobby, draw offers, and resigning.

Like the CLI, the GUI never changes the board itself: it sends MOVE and
redraws when the server's MOVE_MADE comes back.
"""

import argparse
import time
from collections.abc import Callable
from dataclasses import dataclass

try:
    import pygame
except ImportError:
    raise SystemExit("The GUI needs pygame-ce. Install it with: .venv/bin/pip install -e '.[gui]'") from None

from chessrules import STARTING_FEN, Board, Move, in_check, parse_fen, parse_uci
from chessrules.squares import FILES, RANKS, file_of, rank_of, square
from cmtp import DEFAULT_PORT, MessageType

from .model import WHITE, ClientModel
from .net import Closed, Connection, Event, Reconnected, Reconnecting

T = MessageType

# Layout, in pixels
SQUARE = 70
BOARD_LEFT = BOARD_TOP = 40
BOARD_SIZE = 8 * SQUARE
PANEL_LEFT = BOARD_LEFT + BOARD_SIZE + 40
PANEL_WIDTH = 300
WIDTH = PANEL_LEFT + PANEL_WIDTH + 20
HEIGHT = BOARD_TOP + BOARD_SIZE + 40

LOBBY_REFRESH_SECONDS = 2.0  # the lobby isn't pushed to clients, so ask for it regularly
NOTICE_SECONDS = 8.0
RESIGN_CONFIRM_SECONDS = 3.0

BACKGROUND = (38, 36, 33)
LIGHT_SQUARE = (240, 217, 181)
DARK_SQUARE = (181, 136, 99)
TEXT = (232, 230, 227)
MUTED = (150, 146, 140)
ACCENT = (129, 182, 76)
WARNING = (224, 128, 96)
BUTTON = (74, 71, 66)
BUTTON_HOVER = (96, 93, 87)
BUTTON_DISABLED = (56, 54, 50)
SELECTED = (246, 246, 105, 140)
LAST_MOVE = (205, 210, 106, 110)
CHECK = (230, 50, 50, 150)
HINT = (20, 20, 20, 60)

GLYPHS = dict(zip("kqrbnp", "♚♛♜♝♞♟"))
# Fonts that usually have chess symbols: Windows, macOS, Linux, then others.
SYMBOL_FONTS = ("segoeuisymbol", "applesymbols", "dejavusans", "notosanssymbols2", "freeserif", "arialunicodems")
PROMOTION_CHOICES = "qnrb"


def square_rect(sq: int, flip: bool) -> pygame.Rect:
    """The screen rectangle of a square. ``flip`` puts black at the bottom."""
    col = 7 - file_of(sq) if flip else file_of(sq)
    row = rank_of(sq) if flip else 7 - rank_of(sq)
    return pygame.Rect(BOARD_LEFT + col * SQUARE, BOARD_TOP + row * SQUARE, SQUARE, SQUARE)


def square_at(pos: tuple[int, int], flip: bool) -> int | None:
    """The square under a screen position, or None if it's off the board."""
    x, y = pos[0] - BOARD_LEFT, pos[1] - BOARD_TOP
    if not (0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE):
        return None
    col, row = x // SQUARE, y // SQUARE
    return square(7 - col if flip else col, row if flip else 7 - row)


def wrap(text: str, font: pygame.font.Font, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if font.size(trial)[0] <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _symbol_font(size: int) -> pygame.font.Font | None:
    """A font that really has the chess symbols, or None.

    pygame quietly substitutes another font when the one asked for is missing,
    and a font without the symbols draws the same blank box for all of them.
    So a font counts only if the six symbols come out different.
    """
    for name in SYMBOL_FONTS:
        path = pygame.font.match_font(name)
        if path is None:
            continue
        font = pygame.font.Font(path, size)
        if len({tuple(m) if m else None for m in font.metrics("".join(GLYPHS.values()))}) == 6:
            return font
    return None


class PieceSprites:
    """Pictures of the twelve pieces, drawn once at startup.

    They come from a font's chess symbols when one is available, otherwise
    they're lettered discs, so no image files are needed.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        font = _symbol_font(int(size * 0.82))
        self.uses_symbols = font is not None
        letter_font = pygame.font.Font(None, int(size * 0.55))
        self.images = {piece: self._draw(piece, font, letter_font) for piece in "KQRBNPkqrbnp"}

    def _draw(self, piece: str, font, letter_font) -> pygame.Surface:
        size = self.size
        image = pygame.Surface((size, size), pygame.SRCALPHA)
        white = piece.isupper()
        fill, edge = ((250, 248, 245), (30, 28, 26)) if white else ((34, 32, 30), (225, 222, 216))
        if font is None:
            center = (size // 2, size // 2)
            pygame.draw.circle(image, edge, center, int(size * 0.38))
            pygame.draw.circle(image, fill, center, int(size * 0.35))
            letter = letter_font.render(piece.upper(), True, edge)
            image.blit(letter, letter.get_rect(center=center))
            return image

        glyph = GLYPHS[piece.lower()]  # the solid symbols, colored for each side
        body = font.render(glyph, True, fill)
        outline = font.render(glyph, True, edge)
        # Center the visible shape, not the font's line box, which has extra space.
        parts = pygame.mask.from_surface(body).get_bounding_rects()  # one per separate blob of ink
        ink = parts[0].unionall(parts[1:])
        offset = (size // 2 - ink.centerx, size // 2 - ink.centery)
        width = 2 if white else 1
        for dx in range(-width, width + 1):
            for dy in range(-width, width + 1):
                if dx or dy:
                    image.blit(outline, (offset[0] + dx, offset[1] + dy))
        image.blit(body, offset)
        return image


@dataclass
class Button:
    rect: pygame.Rect
    label: str
    action: Callable[[], None]
    enabled: bool = True


class ChessGui:
    """The whole window: what it shows, and what clicks and server messages do.

    Buttons are rebuilt every time the window is drawn, from the current state,
    so they can never get out of step with it.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        name: str | None = None,
        *,
        connection_factory: Callable[[str, int], Connection] = Connection,
    ) -> None:
        self.host = host
        self.port = port
        self.connection_factory = connection_factory
        self.conn: Connection | None = None
        self.model = ClientModel()
        self.running = True
        self.name_input = name or ""
        self.connect_error: str | None = None
        self.disconnected: str | None = None
        self.reconnecting: str | None = None  # why the connection dropped, while getting back in
        self.selected: int | None = None  # square of the piece picked up
        self.promotion: tuple[int, int] | None = None  # (from, to) waiting for a piece choice
        self.pending_move = False  # a MOVE is on its way and MOVE_MADE hasn't come back yet
        self.confirm_resign_until = 0.0
        self.notices: list[tuple[str, float]] = []
        self.buttons: list[Button] = []
        self.last_lobby_refresh = 0.0
        self.idle_board = parse_fen(STARTING_FEN)  # shown before the first game
        self.sprites = PieceSprites(SQUARE)
        self._fonts: dict[int, pygame.font.Font] = {}
        if name:
            self.connect()

    # State

    @property
    def flip(self) -> bool:
        return self.model.my_color is not None and self.model.my_color != WHITE

    @property
    def board(self) -> Board:
        return self.model.chess.board if self.model.chess is not None else self.idle_board

    @property
    def in_lobby(self) -> bool:
        m = self.model
        return m.session_id is not None and not m.in_game and m.hosting is None

    def connect(self) -> None:
        name = self.name_input.strip()
        if not (1 <= len(name) <= 32 and name.isprintable()):
            self.connect_error = "Names are 1 to 32 characters."
            return
        conn = self.connection_factory(self.host, self.port)
        try:
            conn.connect()
        except OSError as exc:
            self.connect_error = f"Couldn't connect to {self.host}:{self.port} ({exc.strerror or exc})."
            return
        self.conn = conn
        self.connect_error = None
        self.model.name = name
        conn.send(T.HELLO, {"name": name})

    def notice(self, text: str) -> None:
        self.notices.append((text, time.monotonic()))
        self.notices = self.notices[-4:]

    def send(self, message_type: MessageType, body: dict | None = None) -> None:
        try:
            self.conn.send(message_type, body)
        except OSError:
            self.notice("Not connected to the server.")

    # Server messages

    def process(self, event: Event) -> None:
        m = self.model
        if isinstance(event, Closed):
            self.disconnected = event.reason
            self.reconnecting = None
            return
        if isinstance(event, Reconnecting):
            self.reconnecting = event.reason
            self.selected = None
            self.promotion = None
            return
        if isinstance(event, Reconnected):
            self.reconnecting = None
            self.pending_move = False  # STATE will show whether a move in flight got through
            if event.resumed:
                self.notice("Reconnected.")
            else:
                if m.in_game or m.hosting is not None:
                    self.notice("Reconnected, but your game couldn't be resumed.")
                m.reset_session()
            return
        m.apply(event)
        body = event.body
        if event.type in (T.MOVE_MADE, T.GAME_START, T.GAME_OVER, T.STATE):
            self.selected = None
            self.promotion = None
            self.pending_move = False
        if event.type is T.WELCOME:
            pygame.display.set_caption(f"CMTP Chess - {m.name}")
            self.send(T.LIST_GAMES)
            self.last_lobby_refresh = time.monotonic()
        elif event.type is T.MOVE_REJECTED:
            self.pending_move = False
            self.notice(f"Move rejected: {body['reason']}.")
        elif event.type is T.ERROR:
            self.notice(f"Error {body['code']}: {body['message']}.")
        elif event.type is T.DRAW_DECLINED:
            self.notice(f"{m.opponent} declined your draw offer.")
        elif event.type is T.OPPONENT_STATUS:
            self.notice(f"{m.opponent} {'reconnected' if body['connected'] else 'lost their connection'}.")
        if m.should_resync():
            self.notice("Your board didn't match the server's, so it was refreshed.")
            self.send(T.GET_STATE, {"game_id": m.game_id})

    def tick(self, now: float) -> None:
        """Work that happens with time rather than input: lobby refresh and expiring notices."""
        if self.in_lobby and self.conn is not None and now - self.last_lobby_refresh >= LOBBY_REFRESH_SECONDS:
            self.last_lobby_refresh = now
            self.send(T.LIST_GAMES)
        self.notices = [(text, at) for text, at in self.notices if now - at < NOTICE_SECONDS]

    # Input

    def handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.QUIT:
            self.running = False
        elif self.conn is None:
            self._handle_name_entry(event)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1 and not (self.disconnected or self.reconnecting):
            self.click(event.pos)
        elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.selected = None
            self.promotion = None

    def _handle_name_entry(self, event: pygame.event.Event) -> None:
        if event.type == pygame.TEXTINPUT:
            self.name_input = (self.name_input + event.text)[:32]
        elif event.type == pygame.KEYDOWN:
            if event.key == pygame.K_BACKSPACE:
                self.name_input = self.name_input[:-1]
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self.connect()

    def click(self, pos: tuple[int, int]) -> None:
        if self.promotion is not None:
            self._click_promotion(pos)
            return
        for button in self.buttons:
            if button.rect.collidepoint(pos):
                if button.enabled:
                    button.action()
                return
        self._click_board(pos)

    def _click_board(self, pos: tuple[int, int]) -> None:
        m = self.model
        sq = square_at(pos, self.flip)
        if sq is None or not m.my_turn or self.pending_move:
            self.selected = None
            return
        if self.selected is not None and sq != self.selected:
            moves = [move for move in self._legal_from(self.selected) if move.to_sq == sq]
            if len(moves) > 1:  # a pawn reaching the last rank: ask which piece
                self.promotion = (self.selected, sq)
                return
            if moves:
                self._send_move(moves[0])
                return
        piece = self.board.piece_at(sq)
        mine = piece is not None and piece.isupper() == (m.my_color == WHITE)
        if sq == self.selected or not mine or not self._legal_from(sq):
            self.selected = None
        else:
            self.selected = sq

    def _click_promotion(self, pos: tuple[int, int]) -> None:
        from_sq, to_sq = self.promotion
        for piece, rect in self._promotion_rects():
            if rect.collidepoint(pos):
                for move in self._legal_from(from_sq):
                    if move.to_sq == to_sq and move.promotion == piece:
                        self._send_move(move)
                        return
        self.promotion = None  # clicking anywhere else cancels
        self.selected = None

    def _legal_from(self, sq: int) -> list[Move]:
        if not self.model.my_turn:
            return []
        return [move for move in self.model.chess.legal_moves() if move.from_sq == sq]

    def _send_move(self, move: Move) -> None:
        m = self.model
        self.send(T.MOVE, {"game_id": m.game_id, "ply": m.ply, "move": move.uci()})
        self.pending_move = True
        self.selected = None
        self.promotion = None

    # Panel actions

    def _create(self, color: str | None) -> None:
        self.send(T.CREATE_GAME, {"color": color} if color else {})

    def _join(self, game_id: str) -> None:
        self.send(T.JOIN_GAME, {"game_id": game_id})

    def _cancel(self) -> None:
        self.send(T.CANCEL_GAME, {"game_id": self.model.hosting})

    def _offer_draw(self) -> None:
        self.send(T.OFFER_DRAW, {"game_id": self.model.game_id})
        self.model.offered_draw()

    def _answer_draw(self, accept: bool) -> None:
        self.send(T.ANSWER_DRAW, {"game_id": self.model.game_id, "accept": accept})
        self.model.answered_draw()

    def _resign(self) -> None:
        now = time.monotonic()
        if now < self.confirm_resign_until:
            self.confirm_resign_until = 0.0
            self.send(T.RESIGN, {"game_id": self.model.game_id})
        else:
            self.confirm_resign_until = now + RESIGN_CONFIRM_SECONDS

    # Drawing

    def draw(self, surface: pygame.Surface) -> None:
        surface.fill(BACKGROUND)
        self.buttons = []
        if self.conn is None:
            self._draw_name_entry(surface)
            return
        self._draw_board(surface)
        self._draw_panel(surface)
        if self.disconnected:
            self._draw_disconnected(surface)
        elif self.reconnecting:
            self._draw_reconnecting(surface)

    def font(self, size: int) -> pygame.font.Font:
        if size not in self._fonts:
            self._fonts[size] = pygame.font.Font(None, size)
        return self._fonts[size]

    def _text(self, surface, text, pos, size=24, color=TEXT, center=False) -> pygame.Rect:
        image = self.font(size).render(text, True, color)
        rect = image.get_rect(center=pos) if center else image.get_rect(topleft=pos)
        surface.blit(image, rect)
        return rect

    def _button(self, surface, rect, label, action, enabled=True) -> None:
        hovered = enabled and rect.collidepoint(pygame.mouse.get_pos())
        color = BUTTON_HOVER if hovered else BUTTON if enabled else BUTTON_DISABLED
        pygame.draw.rect(surface, color, rect, border_radius=6)
        self._text(surface, label, rect.center, 22, TEXT if enabled else MUTED, center=True)
        self.buttons.append(Button(rect, label, action, enabled))

    def _overlay(self, surface, rect, color) -> None:
        shade = pygame.Surface(rect.size, pygame.SRCALPHA)
        shade.fill(color)
        surface.blit(shade, rect)

    def _draw_board(self, surface: pygame.Surface) -> None:
        m, board, flip = self.model, self.board, self.flip
        for rank in range(8):
            for file in range(8):
                sq = square(file, rank)
                color = LIGHT_SQUARE if (file + rank) % 2 else DARK_SQUARE
                pygame.draw.rect(surface, color, square_rect(sq, flip))

        for i in range(8):  # coordinates along the edges
            file = 7 - i if flip else i
            rank = i if flip else 7 - i
            x = BOARD_LEFT + i * SQUARE + SQUARE // 2
            y = BOARD_TOP + i * SQUARE + SQUARE // 2
            self._text(surface, FILES[file], (x, BOARD_TOP + BOARD_SIZE + 16), 22, MUTED, center=True)
            self._text(surface, RANKS[rank], (BOARD_LEFT - 18, y), 22, MUTED, center=True)

        if m.chess is not None and m.last_move is not None:
            from_sq, to_sq, _ = parse_uci(m.last_move.uci)
            for sq in (from_sq, to_sq):
                self._overlay(surface, square_rect(sq, flip), LAST_MOVE)
        if m.in_game and in_check(board):
            self._overlay(surface, square_rect(board.king_squares[board.turn], flip), CHECK)
        if self.selected is not None:
            self._overlay(surface, square_rect(self.selected, flip), SELECTED)

        for sq in (square(f, r) for r in range(8) for f in range(8)):
            piece = board.piece_at(sq)
            if piece is not None:
                surface.blit(self.sprites.images[piece], square_rect(sq, flip))

        if self.selected is not None:  # where the picked-up piece can go
            hints = pygame.Surface((BOARD_SIZE, BOARD_SIZE), pygame.SRCALPHA)
            for move in self._legal_from(self.selected):
                rect = square_rect(move.to_sq, flip).move(-BOARD_LEFT, -BOARD_TOP)
                if move.is_capture:
                    pygame.draw.circle(hints, HINT, rect.center, SQUARE // 2 - 2, width=6)
                else:
                    pygame.draw.circle(hints, HINT, rect.center, SQUARE // 7)
            surface.blit(hints, (BOARD_LEFT, BOARD_TOP))

        if self.promotion is not None:
            self._draw_promotion(surface)
        if m.result is not None:
            self._draw_result_banner(surface)

    def _promotion_rects(self) -> list[tuple[str, pygame.Rect]]:
        """The four choices, stacked from the promotion square toward the middle of the board."""
        _, to_sq = self.promotion
        first = square_rect(to_sq, self.flip)
        step = SQUARE if first.top == BOARD_TOP else -SQUARE
        return [(piece, first.move(0, i * step)) for i, piece in enumerate(PROMOTION_CHOICES)]

    def _draw_promotion(self, surface: pygame.Surface) -> None:
        self._overlay(surface, pygame.Rect(BOARD_LEFT, BOARD_TOP, BOARD_SIZE, BOARD_SIZE), (0, 0, 0, 120))
        white = self.model.my_color == WHITE
        for piece, rect in self._promotion_rects():
            pygame.draw.rect(surface, (235, 235, 235), rect)
            pygame.draw.rect(surface, ACCENT, rect, width=3)
            surface.blit(self.sprites.images[piece.upper() if white else piece], rect)

    def _draw_result_banner(self, surface: pygame.Surface) -> None:
        m = self.model
        verdict = {True: "You won", False: "You lost", None: "Draw"}[m.i_won()]
        band = pygame.Rect(BOARD_LEFT, BOARD_TOP + BOARD_SIZE // 2 - 55, BOARD_SIZE, 110)
        self._overlay(surface, band, (20, 20, 20, 200))
        self._text(surface, f"{verdict} {m.result}", (band.centerx, band.centery - 18), 48, TEXT, center=True)
        self._text(surface, m.ending_text(), (band.centerx, band.centery + 26), 28, MUTED, center=True)

    def _draw_panel(self, surface: pygame.Surface) -> None:
        m = self.model
        x, y, width = PANEL_LEFT, BOARD_TOP, PANEL_WIDTH
        self._text(surface, "CMTP Chess", (x, y), 36)
        y += 36
        status = f"{m.name} on {self.host}:{self.port}" if m.session_id else "Connecting..."
        rtt = getattr(self.conn, "last_rtt", None)
        if m.session_id and rtt is not None:
            status += f"  ({rtt * 1000:.1f} ms)"
        self._text(surface, status, (x, y), 20, MUTED)
        y += 36

        if m.game_id is not None and m.chess is not None:
            y = self._draw_game_panel(surface, x, y, width)
        if not m.in_game:
            y = self._draw_lobby_panel(surface, x, y, width)

        notice_y = HEIGHT - 40
        for text, _ in reversed(self.notices):
            for line in reversed(wrap(text, self.font(20), width)):
                self._text(surface, line, (x, notice_y), 20, WARNING)
                notice_y -= 20
            notice_y -= 6

    def _draw_game_panel(self, surface, x, y, width) -> int:
        m = self.model
        other = "black" if m.my_color == WHITE else "white"
        self._text(surface, f"You ({m.my_color}) vs {m.opponent} ({other})", (x, y), 24)
        y += 30
        if m.in_game and not m.opponent_connected:
            for line in wrap(f"{m.opponent} lost their connection. Waiting for them to come back...",
                             self.font(22), width):
                self._text(surface, line, (x, y), 22, WARNING)
                y += 24
            y += 6
        if m.result is not None:
            self._text(surface, f"Game over: {m.result}, {m.ending_text()}", (x, y), 22, ACCENT)
        elif self.pending_move:
            self._text(surface, "Sending your move...", (x, y), 22, MUTED)
        elif m.my_turn:
            check = " (you're in check)" if in_check(self.board) else ""
            self._text(surface, "Your move" + check, (x, y), 22, ACCENT)
        else:
            self._text(surface, f"Waiting for {m.opponent}...", (x, y), 22, MUTED)
        y += 32

        if m.in_game:
            if m.opponent_offered_draw:
                self._text(surface, f"{m.opponent} offers a draw.", (x, y), 22, WARNING)
                y += 26
                half = (width - 10) // 2
                self._button(surface, pygame.Rect(x, y, half, 34), "Accept", lambda: self._answer_draw(True))
                self._button(surface, pygame.Rect(x + half + 10, y, half, 34), "Decline", lambda: self._answer_draw(False))
                y += 44
            half = (width - 10) // 2
            confirming = time.monotonic() < self.confirm_resign_until
            self._button(surface, pygame.Rect(x, y, half, 34),
                         "Draw offered" if m.i_offered_draw else "Offer draw",
                         self._offer_draw, enabled=not m.i_offered_draw)
            self._button(surface, pygame.Rect(x + half + 10, y, half, 34),
                         "Really resign?" if confirming else "Resign", self._resign)
            y += 48

        history = [played.san for played in m.chess.history]
        rows = [
            f"{i // 2 + 1}. {history[i]}" + (f"  {history[i + 1]}" if i + 1 < len(history) else "")
            for i in range(0, len(history), 2)
        ]
        shown = 8 if not m.in_game else 14
        for row in rows[-shown:]:
            self._text(surface, row, (x, y), 22, MUTED)
            y += 22
        return y + 16

    def _draw_lobby_panel(self, surface, x, y, width) -> int:
        m = self.model
        if m.session_id is None:
            return y
        if m.hosting is not None:
            color = f"you play {m.host_color}" if m.host_color else "color picked when someone joins"
            for line in wrap(f"Waiting for an opponent in game {m.hosting} ({color})...", self.font(22), width):
                self._text(surface, line, (x, y), 22, ACCENT)
                y += 24
            self._button(surface, pygame.Rect(x, y + 6, width, 34), "Cancel", self._cancel)
            return y + 52

        self._text(surface, "New game as:", (x, y), 22)
        y += 26
        third = (width - 20) // 3
        for i, (label, color) in enumerate((("White", "white"), ("Black", "black"), ("Random", None))):
            self._button(surface, pygame.Rect(x + i * (third + 10), y, third, 34), label,
                         lambda color=color: self._create(color))
        y += 50

        self._text(surface, "Open games:", (x, y), 22)
        y += 26
        if not m.open_games:
            self._text(surface, "None yet. Create one, or wait.", (x, y), 20, MUTED)
            return y + 26
        for game in m.open_games[:6]:
            color = game["host_color"]
            plays = f"plays {color}" if color else "random color"
            label = f"Join {game['host']} ({plays})"
            self._button(surface, pygame.Rect(x, y, width, 32), label,
                         lambda game_id=game["game_id"]: self._join(game_id))
            y += 38
        return y

    def _draw_name_entry(self, surface: pygame.Surface) -> None:
        cx = WIDTH // 2
        self._text(surface, "CMTP Chess", (cx, 180), 48, center=True)
        self._text(surface, f"Server {self.host}:{self.port}", (cx, 225), 22, MUTED, center=True)
        self._text(surface, "Your name:", (cx, 290), 28, center=True)
        box = pygame.Rect(cx - 160, 315, 320, 44)
        pygame.draw.rect(surface, (250, 248, 245), box, border_radius=6)
        cursor = "|" if int(time.monotonic() * 2) % 2 == 0 else " "
        self._text(surface, self.name_input + cursor, (box.x + 12, box.y + 12), 28, (30, 28, 26))
        self._text(surface, "Press Enter to connect", (cx, 385), 22, MUTED, center=True)
        if self.connect_error:
            self._text(surface, self.connect_error, (cx, 425), 22, WARNING, center=True)

    def _draw_reconnecting(self, surface: pygame.Surface) -> None:
        self._overlay(surface, surface.get_rect(), (0, 0, 0, 120))
        self._text(surface, "Reconnecting...", (WIDTH // 2, HEIGHT // 2 - 20), 48, center=True)
        self._text(surface, f"The connection was lost ({self.reconnecting}).", (WIDTH // 2, HEIGHT // 2 + 22), 24, MUTED, center=True)

    def _draw_disconnected(self, surface: pygame.Surface) -> None:
        self._overlay(surface, surface.get_rect(), (0, 0, 0, 170))
        self._text(surface, "Disconnected", (WIDTH // 2, HEIGHT // 2 - 30), 48, center=True)
        self._text(surface, self.disconnected, (WIDTH // 2, HEIGHT // 2 + 15), 24, MUTED, center=True)
        self._text(surface, "Close the window to exit.", (WIDTH // 2, HEIGHT // 2 + 50), 22, MUTED, center=True)

    # Main loop

    def run(self, screen: pygame.Surface) -> None:
        clock = pygame.time.Clock()
        while self.running:
            for event in pygame.event.get():
                self.handle_event(event)
            if self.conn is not None:
                for event in self.conn.poll():
                    self.process(event)
            self.tick(time.monotonic())
            self.draw(screen)
            pygame.display.flip()
            clock.tick(30)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m client.gui", description="CMTP chess graphical client")
    parser.add_argument("--host", default="127.0.0.1", help="server address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"server port (default: {DEFAULT_PORT})")
    parser.add_argument("--name", help="your player name (asked for in the window if left out)")
    args = parser.parse_args(argv)

    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("CMTP Chess")
    gui = ChessGui(args.host, args.port, args.name)
    try:
        gui.run(screen)
    finally:
        gui.close()
        pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
