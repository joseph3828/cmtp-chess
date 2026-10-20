"""The text client: commands, what it prints, and two clients playing through a real server."""

import time

import pytest

from client.cli import HELP, ChessCli, render_board
from client.net import Closed, Reconnected, Reconnecting
from chessrules import parse_fen
from cmtp import ErrorCode, Message, MessageType
from fakes import FakeConnection

T = MessageType
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"


class Harness:
    """A ChessCli wired to a fake connection, collecting everything it prints."""

    def __init__(self):
        self.conn = FakeConnection()
        self.lines = []
        self.cli = ChessCli(self.conn, self.lines.append)
        self.cli.hello("Ana")

    def type(self, line):
        self.cli.execute(line)

    def receive(self, message_type, body=None, re=None):
        self.cli.process(Message(message_type, 1, body or {}, re))

    @property
    def last_sent(self):
        return self.conn.sent[-1]

    @property
    def text(self):
        return "\n".join(self.lines)

    def start_game(self, color="white"):
        self.receive(T.GAME_START, {"game_id": "g1", "your_color": color, "opponent": "Ben", "fen": START_FEN})
        self.lines.clear()


@pytest.fixture
def h():
    return Harness()


def test_hello_and_welcome(h):
    assert h.conn.sent == [(T.HELLO, {"name": "Ana"})]
    h.receive(T.WELCOME, {"session_id": "s1", "resume_token": "tok"})
    assert "Connected as Ana" in h.text


def test_help(h):
    h.type("help")
    assert h.lines == [HELP]


def test_lobby_commands(h):
    h.type("list")
    assert h.last_sent == (T.LIST_GAMES, {})
    h.type("create")
    assert h.last_sent == (T.CREATE_GAME, {})
    h.type("create black")
    assert h.last_sent == (T.CREATE_GAME, {"color": "black"})
    h.type("join g7")
    assert h.last_sent == (T.JOIN_GAME, {"game_id": "g7"})


def test_bad_lobby_commands_send_nothing(h):
    for line in ["create purple", "join", "join a b", "cancel", "frobnicate", "e4", "resign"]:
        h.type(line)
    assert h.conn.sent == [(T.HELLO, {"name": "Ana"})]
    assert "Usage: create" in h.text
    assert "Usage: join" in h.text
    assert "aren't waiting in a game" in h.text
    assert "Unknown command 'frobnicate'" in h.text


def test_game_list_display(h):
    h.receive(T.GAME_LIST, {"games": []})
    assert "No open games" in h.text
    h.receive(T.GAME_LIST, {"games": [
        {"game_id": "g1", "host": "Ben", "host_color": "white"},
        {"game_id": "g2", "host": "Cy", "host_color": None},
    ]})
    assert "g1: Ben (plays white)" in h.text
    assert "g2: Cy (color picked at random)" in h.text


def test_waiting_then_cancel(h):
    h.receive(T.GAME_CREATED, {"game_id": "g3", "color": None})
    assert "Created game g3" in h.text
    assert h.cli.prompt() == "Ana (waiting in g3)> "
    h.type("create")
    assert "Type 'cancel' first" in h.text
    h.type("cancel")
    assert h.last_sent == (T.CANCEL_GAME, {"game_id": "g3"})
    h.receive(T.GAME_CANCELLED, {"game_id": "g3"})
    assert h.cli.prompt() == "Ana (lobby)> "


def test_game_start_shows_the_board(h):
    h.receive(T.GAME_START, {"game_id": "g1", "your_color": "white", "opponent": "Ben", "fen": START_FEN})
    assert "You're white against Ben" in h.text
    assert "  8 r n b q k b n r 8" in h.text
    assert h.cli.prompt() == "Ana (white, your move)> "


def test_black_sees_the_board_from_its_side(h):
    h.receive(T.GAME_START, {"game_id": "g1", "your_color": "black", "opponent": "Ben", "fen": START_FEN})
    assert "    h g f e d c b a" in h.text
    assert "  1 R N B K Q B N R 1" in h.text
    assert h.cli.prompt() == "Ana (black, Ben to move)> "


@pytest.mark.parametrize("typed", ["e4", "e2e4", "move e4", "move e2e4"])
def test_moves_are_sent_as_uci_with_the_current_ply(h, typed):
    h.start_game()
    h.type(typed)
    assert h.last_sent == (T.MOVE, {"game_id": "g1", "ply": 0, "move": "e2e4"})


def test_board_waits_for_the_server(h):
    h.start_game()
    h.type("e4")
    assert h.cli.model.ply == 0  # not moved yet
    h.receive(T.MOVE_MADE, {"game_id": "g1", "ply": 0, "move": "e2e4", "san": "e4", "fen": AFTER_E4, "by": "white"})
    assert h.cli.model.ply == 1
    assert "You played e4." in h.text
    assert "Last move: e4" in h.text
    assert h.cli.prompt() == "Ana (white, Ben to move)> "


def test_opponent_move(h):
    h.start_game("black")
    h.receive(T.MOVE_MADE, {"game_id": "g1", "ply": 0, "move": "e2e4", "san": "e4", "fen": AFTER_E4, "by": "white"})
    assert "Ben played e4." in h.text
    h.type("c5")
    assert h.last_sent == (T.MOVE, {"game_id": "g1", "ply": 1, "move": "c7c5"})


def test_moves_that_are_checked_locally(h):
    h.start_game("black")
    h.type("e5")
    assert "It's Ben's turn" in h.text
    h.start_game("white")
    h.type("e5")
    h.type("Ke2")
    h.type("e7e5")
    assert h.text.count("isn't a legal move") == 3
    assert h.conn.sent[-1][0] == T.HELLO  # nothing was sent


def test_listing_legal_moves(h):
    h.start_game()
    h.type("moves")
    assert h.lines[-1].startswith("Your legal moves: ")
    assert "Nf3" in h.lines[-1]
    assert len(h.lines[-1].split(", ")) == 20


def test_rejected_move(h):
    h.start_game()
    h.receive(T.MOVE_REJECTED, {"game_id": "g1", "code": ErrorCode.ILLEGAL_MOVE, "reason": "nope"})
    assert "Move rejected: nope." in h.text


def test_stale_ply_asks_for_the_full_state_once(h):
    h.start_game()
    h.receive(T.MOVE_REJECTED, {"game_id": "g1", "code": ErrorCode.STALE_PLY, "reason": "stale"})
    h.receive(T.MOVE_REJECTED, {"game_id": "g1", "code": ErrorCode.STALE_PLY, "reason": "stale"})
    requests = [sent for sent in h.conn.sent if sent[0] == T.GET_STATE]
    assert requests == [(T.GET_STATE, {"game_id": "g1"})]
    h.receive(T.STATE, {
        "game_id": "g1", "your_color": "white", "opponent": "Ben",
        "moves": ["e2e4"], "fen": AFTER_E4, "turn": "black",
    })
    assert "Board refreshed from the server." in h.text
    assert h.cli.model.ply == 1


def test_mismatched_fen_asks_for_the_full_state(h):
    h.start_game()
    h.receive(T.MOVE_MADE, {"game_id": "g1", "ply": 0, "move": "e2e4", "san": "e4", "fen": START_FEN, "by": "white"})
    assert h.last_sent == (T.GET_STATE, {"game_id": "g1"})
    assert "doesn't match the server's" in h.text


def test_draw_offer_from_opponent(h):
    h.start_game()
    h.type("accept")
    assert "no draw offer to answer" in h.text
    h.receive(T.DRAW_OFFERED, {"game_id": "g1"})
    assert "Ben offers a draw" in h.text
    h.type("decline")
    assert h.last_sent == (T.ANSWER_DRAW, {"game_id": "g1", "accept": False})
    h.receive(T.DRAW_OFFERED, {"game_id": "g1"})
    h.type("accept")
    assert h.last_sent == (T.ANSWER_DRAW, {"game_id": "g1", "accept": True})


def test_offering_a_draw(h):
    h.start_game()
    h.type("draw")
    assert h.last_sent == (T.OFFER_DRAW, {"game_id": "g1"})
    assert "You offered a draw." in h.text
    h.receive(T.DRAW_DECLINED, {"game_id": "g1"})
    assert "Ben declined your draw offer." in h.text


def test_resign_and_state(h):
    h.start_game()
    h.type("resign")
    assert h.last_sent == (T.RESIGN, {"game_id": "g1"})
    h.type("state")
    assert h.last_sent == (T.GET_STATE, {"game_id": "g1"})


@pytest.mark.parametrize(
    "color, result, reason, expected",
    [
        ("white", "1-0", "checkmate", "Game over: 1-0 (checkmate). You won!"),
        ("white", "0-1", "checkmate", "Game over: 0-1 (checkmate). You lost."),
        ("black", "1-0", "resignation", "Game over: 1-0 (You resigned). You lost."),
        ("white", "1-0", "resignation", "Game over: 1-0 (Ben resigned). You won!"),
        ("black", "0-1", "abandoned", "Game over: 0-1 (Ben left the game). You won!"),
        ("white", "1/2-1/2", "stalemate", "Game over: 1/2-1/2 (stalemate). It's a draw."),
    ],
)
def test_game_over(h, color, result, reason, expected):
    h.start_game(color)
    h.receive(T.GAME_OVER, {"game_id": "g1", "result": result, "reason": reason})
    assert h.lines[-1].startswith(expected)
    assert h.cli.prompt() == "Ana (lobby)> "


def test_error_display(h):
    h.receive(T.ERROR, {"code": 202, "message": "you are already in game g1"})
    assert h.lines[-1] == "Error 202 (ALREADY_IN_GAME): you are already in game g1."


def test_ping(h):
    h.type("ping")
    seq = len(h.conn.sent)
    assert h.last_sent == (T.PING, {})
    h.receive(T.PONG, re=seq)
    assert h.lines[-1].startswith("Pong: ") and h.lines[-1].endswith(" ms round trip.")


def test_raw(h):
    h.type('raw move {"game_id": "g1", "ply": 0, "move": "e2e4"}')
    assert h.last_sent == (T.MOVE, {"game_id": "g1", "ply": 0, "move": "e2e4"})
    h.type("raw NOPE")
    h.type('raw MOVE {"game_id": "g1"')
    h.type('raw MOVE {"game_id": "g1"}')
    assert h.text.count("Can't send that") == 3


def test_quit(h):
    h.type("quit")
    assert h.conn.closed
    assert not h.cli.running
    h.cli.process(Closed("disconnected"))
    assert h.lines[-1] == "Bye."


def test_unexpected_disconnect(h):
    h.cli.process(Closed("the server closed the connection"))
    assert not h.cli.running
    assert h.lines[-1] == "Disconnected: the server closed the connection."


def test_unicode_board():
    text = render_board(parse_fen(START_FEN), unicode=True)
    assert "  8 ♜ ♞ ♝ ♛ ♚ ♝ ♞ ♜ 8" in text
    assert "  1 ♖ ♘ ♗ ♕ ♔ ♗ ♘ ♖ 1" in text


# Two real clients through a real server


def pump_until(pairs, condition, timeout=3):
    """Feed connection events to their CLIs until ``condition()`` is true."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        for cli, conn in pairs:
            event = conn.get(timeout=0.01)
            if event is not None:
                cli.process(event)


def test_two_clis_play_scholars_mate(open_connection):
    ana_lines, ben_lines = [], []
    ana_conn, ben_conn = open_connection(), open_connection()
    ana = ChessCli(ana_conn, ana_lines.append)
    ben = ChessCli(ben_conn, ben_lines.append)
    pairs = [(ana, ana_conn), (ben, ben_conn)]

    ana.hello("Ana")
    ben.hello("Ben")
    pump_until(pairs, lambda: ana.model.session_id and ben.model.session_id)

    ana.execute("create white")
    pump_until(pairs, lambda: ana.model.hosting)
    ben.execute("list")
    pump_until(pairs, lambda: ben.model.open_games)
    ben.execute("join " + ben.model.open_games[0]["game_id"])
    pump_until(pairs, lambda: ana.model.in_game and ben.model.in_game)

    for ply, san in enumerate(["e4", "e5", "Qh5", "Nc6", "Bc4", "Nf6", "Qxf7#"]):
        (ana if ply % 2 == 0 else ben).execute(san)
        pump_until(pairs, lambda: ana.model.ply == ben.model.ply == ply + 1)
    pump_until(pairs, lambda: ana.model.result and ben.model.result)

    assert ana_lines[-1].startswith("Game over: 1-0 (checkmate). You won!")
    assert ben_lines[-1].startswith("Game over: 1-0 (checkmate). You lost.")
    assert "Ana played Qxf7#." in ben_lines
    assert not any("rejected" in line or "Error" in line for line in ana_lines + ben_lines)


# Losing the connection and getting back in


def test_reconnect_messages(h):
    h.receive(T.WELCOME, {"session_id": "s1", "resume_token": "tok"})
    h.start_game()
    h.cli.process(Reconnecting("Connection reset by peer"))
    assert h.lines[-1] == "Connection lost (Connection reset by peer). Reconnecting..."
    h.receive(T.WELCOME, {"session_id": "s1", "resume_token": "tok2"})
    assert not any(line.startswith("Connected as") for line in h.lines)  # not repeated
    h.cli.process(Reconnected(resumed=True))
    assert h.lines[-1] == "Reconnected."
    assert h.cli.model.in_game


def test_failed_resume_returns_to_the_lobby(h):
    h.start_game()
    h.cli.process(Reconnected(resumed=False))
    assert not h.cli.model.in_game
    assert "couldn't be resumed" in h.lines[-1]
    assert h.cli.prompt() == "Ana (lobby)> "


def test_opponent_dropping_out(h):
    h.start_game()
    h.receive(T.OPPONENT_STATUS, {"game_id": "g1", "connected": False})
    assert "Ben lost their connection" in h.lines[-1]
    h.receive(T.OPPONENT_STATUS, {"game_id": "g1", "connected": True})
    assert h.lines[-1] == "Ben reconnected."
