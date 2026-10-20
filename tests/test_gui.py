"""The GUI, run headless: clicks go in, messages it sends come out."""

import os
import time

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")  # no window needed
pygame = pytest.importorskip("pygame")

from chessrules import parse_square  # noqa: E402
from client import Connection  # noqa: E402
from client.gui import (  # noqa: E402
    BOARD_LEFT, BOARD_TOP, HEIGHT, SQUARE, WIDTH, ChessGui, PieceSprites, square_at, square_rect,
)
from client.net import Closed, Reconnected, Reconnecting  # noqa: E402
from cmtp import ErrorCode, Message, MessageType  # noqa: E402
from fakes import FakeConnection  # noqa: E402

T = MessageType
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"


@pytest.fixture(scope="module", autouse=True)
def pygame_started():
    pygame.init()
    yield
    pygame.quit()


class Harness:
    def __init__(self, name="Ana"):
        self.conn = FakeConnection()
        self.gui = ChessGui(name=name, connection_factory=lambda host, port: self.conn)
        self.surface = pygame.Surface((WIDTH, HEIGHT))

    def frame(self):
        self.gui.draw(self.surface)

    def receive(self, message_type, body=None, re=None):
        self.gui.process(Message(message_type, 1, body or {}, re))
        self.frame()

    def click(self, pos):
        self.frame()  # buttons come from the last drawn frame, as in the real loop
        self.gui.click(pos)
        self.frame()

    def click_square(self, name):
        self.click(square_rect(parse_square(name), self.gui.flip).center)

    def press(self, label):
        self.frame()
        button = next(b for b in self.gui.buttons if b.label == label)
        self.click(button.rect.center)

    def labels(self):
        self.frame()
        return [b.label for b in self.gui.buttons]

    @property
    def sent(self):
        return self.conn.sent

    def start_game(self, color="white", fen=START_FEN):
        self.receive(T.WELCOME, {"session_id": "s1", "resume_token": "tok"})
        self.receive(T.GAME_START, {"game_id": "g1", "your_color": color, "opponent": "Ben", "fen": fen})
        self.conn.sent.clear()


@pytest.fixture
def h():
    return Harness()


# Board geometry


@pytest.mark.parametrize("flip", [False, True])
def test_every_square_maps_back_to_itself(flip):
    for rank in range(8):
        for file in range(8):
            sq = rank * 16 + file
            assert square_at(square_rect(sq, flip).center, flip) == sq


def test_orientation():
    a1, h8 = parse_square("a1"), parse_square("h8")
    bottom_left = (BOARD_LEFT + 1, BOARD_TOP + 8 * SQUARE - 1)
    assert square_at(bottom_left, flip=False) == a1
    assert square_at(bottom_left, flip=True) == h8
    assert square_at((0, 0), flip=False) is None


def test_piece_pictures():
    sprites = PieceSprites(SQUARE)
    assert set(sprites.images) == set("KQRBNPkqrbnp")
    assert all(image.get_size() == (SQUARE, SQUARE) for image in sprites.images.values())


# Connecting


def test_connects_right_away_when_given_a_name(h):
    assert h.sent == [(T.HELLO, {"name": "Ana"})]
    h.receive(T.WELCOME, {"session_id": "s1", "resume_token": "tok"})
    assert h.sent[-1] == (T.LIST_GAMES, {})


def test_name_entry():
    h = Harness(name=None)
    assert h.gui.conn is None
    for text in ["Bx", "en"]:
        h.gui.handle_event(pygame.event.Event(pygame.TEXTINPUT, text=text))
    h.gui.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_BACKSPACE))
    assert h.gui.name_input == "Bxe"
    h.gui.name_input = "Ben"
    h.frame()
    h.gui.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN))
    assert h.gui.conn is h.conn
    assert h.sent == [(T.HELLO, {"name": "Ben"})]


def test_blank_name_is_refused():
    h = Harness(name=None)
    h.gui.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN))
    assert h.gui.conn is None
    assert "1 to 32 characters" in h.gui.connect_error


def test_connection_failure_is_shown():
    class Refused(FakeConnection):
        def connect(self):
            raise ConnectionRefusedError(61, "Connection refused")

    gui = ChessGui(name="Ana", connection_factory=lambda host, port: Refused())
    assert gui.conn is None
    assert gui.connect_error.startswith("Couldn't connect to 127.0.0.1:5328")


# Lobby


def test_create_buttons(h):
    h.receive(T.WELCOME, {"session_id": "s1", "resume_token": "tok"})
    h.press("White")
    assert h.sent[-1] == (T.CREATE_GAME, {"color": "white"})
    h.press("Random")
    assert h.sent[-1] == (T.CREATE_GAME, {})


def test_join_from_the_list(h):
    h.receive(T.WELCOME, {"session_id": "s1", "resume_token": "tok"})
    h.receive(T.GAME_LIST, {"games": [{"game_id": "g7", "host": "Ben", "host_color": "white"}]})
    h.press("Join Ben (plays white)")
    assert h.sent[-1] == (T.JOIN_GAME, {"game_id": "g7"})


def test_lobby_refreshes_itself(h):
    h.receive(T.WELCOME, {"session_id": "s1", "resume_token": "tok"})
    count = len(h.sent)
    h.gui.tick(time.monotonic())
    assert len(h.sent) == count  # too soon
    h.gui.tick(time.monotonic() + 2.5)
    assert h.sent[-1] == (T.LIST_GAMES, {})


def test_waiting_and_cancelling(h):
    h.receive(T.WELCOME, {"session_id": "s1", "resume_token": "tok"})
    h.receive(T.GAME_CREATED, {"game_id": "g3", "color": None})
    assert "White" not in h.labels()
    h.press("Cancel")
    assert h.sent[-1] == (T.CANCEL_GAME, {"game_id": "g3"})
    count = len(h.sent)
    h.gui.tick(time.monotonic() + 10)
    assert len(h.sent) == count  # no lobby refresh while still waiting in a game
    h.receive(T.GAME_CANCELLED, {"game_id": "g3"})
    assert "White" in h.labels()
    h.gui.tick(time.monotonic() + 20)
    assert h.sent[-1] == (T.LIST_GAMES, {})


# Moving


def test_click_a_piece_then_a_square(h):
    h.start_game()
    h.click_square("e2")
    assert h.gui.selected == parse_square("e2")
    h.click_square("e4")
    assert h.sent == [(T.MOVE, {"game_id": "g1", "ply": 0, "move": "e2e4"})]
    assert h.gui.pending_move
    assert h.gui.model.ply == 0  # the board waits for the server


def test_no_second_move_while_one_is_on_its_way(h):
    h.start_game()
    h.click_square("e2")
    h.click_square("e4")
    h.click_square("d2")
    h.click_square("d4")
    assert len(h.sent) == 1
    h.receive(T.MOVE_MADE, {"game_id": "g1", "ply": 0, "move": "e2e4", "san": "e4", "fen": AFTER_E4, "by": "white"})
    assert not h.gui.pending_move
    assert h.gui.model.ply == 1


def test_selection_rules(h):
    h.start_game()
    h.click_square("e7")  # the opponent's piece
    assert h.gui.selected is None
    h.click_square("e1")  # own piece with no moves
    assert h.gui.selected is None
    h.click_square("g1")
    h.click_square("b1")  # switch to another piece
    assert h.gui.selected == parse_square("b1")
    h.click_square("b1")  # click again to put it down
    assert h.gui.selected is None
    h.click_square("g1")
    h.click_square("g4")  # not a knight move
    assert h.gui.selected is None
    assert h.sent == []


def test_cannot_move_on_opponents_turn(h):
    h.start_game("black")
    h.click_square("e7")
    assert h.gui.selected is None


def test_black_clicks_on_a_flipped_board(h):
    h.start_game("black")
    h.receive(T.MOVE_MADE, {"game_id": "g1", "ply": 0, "move": "e2e4", "san": "e4", "fen": AFTER_E4, "by": "white"})
    h.click_square("e7")
    h.click_square("e5")
    assert h.sent == [(T.MOVE, {"game_id": "g1", "ply": 1, "move": "e7e5"})]


def test_promotion_asks_which_piece(h):
    h.start_game(fen="8/P7/8/8/8/8/8/k6K w - - 0 1")
    h.click_square("a7")
    h.click_square("a8")
    assert h.sent == []
    assert h.gui.promotion is not None
    knight_rect = dict(h.gui._promotion_rects())["n"]
    h.click(knight_rect.center)
    assert h.sent == [(T.MOVE, {"game_id": "g1", "ply": 0, "move": "a7a8n"})]


def test_promotion_can_be_called_off(h):
    h.start_game(fen="8/P7/8/8/8/8/8/k6K w - - 0 1")
    h.click_square("a7")
    h.click_square("a8")
    h.click_square("h1")  # outside the choices
    assert h.gui.promotion is None
    assert h.sent == []


def test_rejected_move(h):
    h.start_game()
    h.click_square("e2")
    h.click_square("e4")
    h.receive(T.MOVE_REJECTED, {"game_id": "g1", "code": ErrorCode.ILLEGAL_MOVE, "reason": "nope"})
    assert not h.gui.pending_move
    assert h.gui.notices[-1][0] == "Move rejected: nope."


def test_out_of_step_board_is_refreshed(h):
    h.start_game()
    h.receive(T.MOVE_MADE, {"game_id": "g1", "ply": 0, "move": "e2e4", "san": "e4", "fen": START_FEN, "by": "white"})
    assert h.sent[-1] == (T.GET_STATE, {"game_id": "g1"})


# Draws, resigning, and the end of the game


def test_answering_a_draw_offer(h):
    h.start_game()
    assert "Accept" not in h.labels()
    h.receive(T.DRAW_OFFERED, {"game_id": "g1"})
    h.press("Accept")
    assert h.sent[-1] == (T.ANSWER_DRAW, {"game_id": "g1", "accept": True})
    assert "Accept" not in h.labels()


def test_offering_a_draw(h):
    h.start_game()
    h.press("Offer draw")
    assert h.sent[-1] == (T.OFFER_DRAW, {"game_id": "g1"})
    button = next(b for b in (h.frame() or h.gui.buttons) if b.label == "Draw offered")
    assert not button.enabled
    h.receive(T.DRAW_DECLINED, {"game_id": "g1"})
    assert "Offer draw" in h.labels()


def test_resign_takes_two_clicks(h):
    h.start_game()
    h.press("Resign")
    assert h.sent == []
    h.press("Really resign?")
    assert h.sent == [(T.RESIGN, {"game_id": "g1"})]


def test_game_over_returns_to_the_lobby(h):
    h.start_game()
    h.receive(T.GAME_OVER, {"game_id": "g1", "result": "0-1", "reason": "resignation"})
    labels = h.labels()
    assert "Resign" not in labels
    assert {"White", "Black", "Random"} <= set(labels)


def test_disconnect_is_shown_and_clicks_are_ignored(h):
    h.start_game()
    h.gui.process(Closed("the server closed the connection"))
    h.frame()
    assert h.gui.disconnected == "the server closed the connection"
    h.gui.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=square_rect(parse_square("e2"), False).center))
    assert h.gui.selected is None


# Two GUIs through a real server


def pump(guis, condition, timeout=3):
    deadline = time.monotonic() + timeout
    surface = pygame.Surface((WIDTH, HEIGHT))
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        for gui in guis:  # the same steps as the real main loop, minus the window
            for event in gui.conn.poll():
                gui.process(event)
            gui.tick(time.monotonic())
            gui.draw(surface)
        time.sleep(0.005)


def test_two_guis_play_fools_mate(live_server):
    def connection(host, port):
        return Connection(host, live_server.port)

    ana = ChessGui(name="Ana", connection_factory=connection)
    ben = ChessGui(name="Ben", connection_factory=connection)
    guis = [ana, ben]
    try:
        pump(guis, lambda: ana.model.session_id and ben.model.session_id)
        surface = pygame.Surface((WIDTH, HEIGHT))
        ben.draw(surface)
        next(b for b in ben.buttons if b.label == "Black").action()
        pump(guis, lambda: ben.model.hosting)
        ana.last_lobby_refresh = 0  # refresh now instead of on the next 2-second tick
        pump(guis, lambda: ana.model.open_games)
        ana.draw(surface)
        next(b for b in ana.buttons if b.label.startswith("Join Ben")).action()
        pump(guis, lambda: ana.model.in_game and ben.model.in_game)

        for ply, (frm, to) in enumerate([("f2", "f3"), ("e7", "e5"), ("g2", "g4"), ("d8", "h4")]):
            mover = ana if ply % 2 == 0 else ben
            for name in (frm, to):
                mover.draw(surface)
                mover.click(square_rect(parse_square(name), mover.flip).center)
            pump(guis, lambda: ana.model.ply == ben.model.ply == ply + 1)
        pump(guis, lambda: ana.model.result and ben.model.result)
        assert ana.model.result == ben.model.result == "0-1"
        assert ben.model.i_won() and not ana.model.i_won()
    finally:
        for gui in guis:
            gui.close()


# Losing the connection and getting back in


def test_reconnecting_blocks_clicks(h):
    h.start_game()
    h.gui.process(Reconnecting("connection lost"))
    h.frame()
    pos = square_rect(parse_square("e2"), False).center
    h.gui.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=pos))
    assert h.gui.selected is None
    h.gui.process(Reconnected(resumed=True))
    assert h.gui.reconnecting is None
    h.click_square("e2")
    assert h.gui.selected == parse_square("e2")


def test_failed_resume_returns_to_the_lobby(h):
    h.start_game()
    h.gui.process(Reconnected(resumed=False))
    assert not h.gui.model.in_game
    assert "White" in h.labels()
    assert "couldn't be resumed" in h.gui.notices[-1][0]


def test_opponent_dropping_out_is_shown(h):
    h.start_game()
    h.receive(T.OPPONENT_STATUS, {"game_id": "g1", "connected": False})
    assert not h.gui.model.opponent_connected
    assert "lost their connection" in h.gui.notices[-1][0]
