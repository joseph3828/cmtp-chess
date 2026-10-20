"""Text client for CMTP chess. Run it with ``python -m client``.

It's also a debugging tool: ``raw`` sends any message type with any body, and
``ping`` measures the round trip to the server.
"""

import argparse
import json
import sys
import threading
import time
from collections.abc import Callable

from chessrules import Board, Move, parse_san, parse_uci, to_san
from chessrules.squares import FILES, square
from cmtp import DEFAULT_PORT, ErrorCode, Message, MessageType, ProtocolError

from .model import WHITE, ClientModel
from .net import Closed, Connection, Event, Reconnected, Reconnecting

T = MessageType

HELP = """\
Commands:
  list                  show open games
  create [white|black]  start a game and wait for an opponent (random color if left out)
  join <game>           join an open game, such as: join g1
  cancel                stop waiting for an opponent
  <move>                make a move: e4, Nf3, exd5, O-O, e8=Q, or UCI like e2e4
  moves                 list your legal moves
  board                 show the board again
  draw                  offer a draw (or accept one you've been offered)
  accept / decline      answer a draw offer
  resign                give up the game
  state                 ask the server for the full game state
  ping                  measure the round trip to the server
  raw <TYPE> [json]     send any message, such as: raw MOVE {"game_id": "g1", "ply": 0, "move": "e2e4"}
  help                  show this list
  quit                  disconnect and exit"""

UNICODE_PIECES = dict(zip("KQRBNPkqrbnp", "♔♕♖♗♘♙♚♛♜♝♞♟"))


def render_board(board: Board, *, flip: bool = False, unicode: bool = False) -> str:
    """Draw the board as text. ``flip`` puts black at the bottom."""
    ranks = range(8) if flip else range(7, -1, -1)
    files = range(7, -1, -1) if flip else range(8)
    labels = "    " + " ".join(FILES[f] for f in files)
    lines = [labels]
    for rank in ranks:
        cells = []
        for file in files:
            piece = board.piece_at(square(file, rank))
            cells.append("." if piece is None else UNICODE_PIECES[piece] if unicode else piece)
        lines.append(f"  {rank + 1} {' '.join(cells)} {rank + 1}")
    lines.append(labels)
    return "\n".join(lines)


class ChessCli:
    """Turns typed commands into messages and server messages into text.

    It doesn't read the keyboard or the network itself: ``execute`` takes a
    typed line, ``process`` takes an event from the connection, and all
    text goes to ``output``. ``main`` connects those to a terminal.
    """

    def __init__(self, connection: Connection, output: Callable[[str], None] = print, *, unicode: bool = False) -> None:
        self.conn = connection
        self.output = output
        self.unicode = unicode
        self.model = ClientModel()
        # The reader thread (process) and the input loop (execute) both use the model.
        self.lock = threading.RLock()
        self.running = True
        self._pings: dict[int, float] = {}  # seq -> time sent

    def hello(self, name: str) -> None:
        with self.lock:
            self.model.name = name
            self.conn.send(T.HELLO, {"name": name})

    def prompt(self) -> str:
        m = self.model
        if m.in_game:
            status = f"{m.my_color}, " + ("your move" if m.my_turn else f"{m.opponent} to move")
        elif m.hosting:
            status = f"waiting in {m.hosting}"
        else:
            status = "lobby"
        return f"{m.name} ({status})> "

    # Typed commands

    def execute(self, line: str) -> None:
        command, _, rest = line.strip().partition(" ")
        if not command:
            return
        handler = getattr(self, "_cmd_" + command.lower(), None) if command.isalpha() else None
        with self.lock:
            try:
                if handler is not None:
                    handler(rest.strip())
                elif self.model.in_game:
                    self._move(line.strip())  # anything that isn't a command is a move
                else:
                    self.output(f"Unknown command {command!r}. Type 'help' for the list.")
            except OSError:
                self.output("Not connected to the server.")

    def _cmd_help(self, rest: str) -> None:
        self.output(HELP)

    def _cmd_list(self, rest: str) -> None:
        self.conn.send(T.LIST_GAMES)

    def _cmd_create(self, rest: str) -> None:
        if rest not in ("", "white", "black"):
            self.output("Usage: create [white|black]")
        elif self._busy():
            pass
        else:
            self.conn.send(T.CREATE_GAME, {"color": rest} if rest else {})

    def _cmd_join(self, rest: str) -> None:
        if not rest or " " in rest:
            self.output("Usage: join <game>, such as: join g1. Type 'list' to see open games.")
        elif not self._busy():
            self.conn.send(T.JOIN_GAME, {"game_id": rest})

    def _cmd_cancel(self, rest: str) -> None:
        if self.model.hosting is None:
            self.output("You aren't waiting in a game.")
        else:
            self.conn.send(T.CANCEL_GAME, {"game_id": self.model.hosting})

    def _cmd_move(self, rest: str) -> None:
        self._move(rest)

    def _cmd_moves(self, rest: str) -> None:
        if self._require_my_turn():
            board = self.model.chess.board
            legal = self.model.chess.legal_moves()
            self.output("Your legal moves: " + ", ".join(sorted(to_san(board, m, legal) for m in legal)))

    def _cmd_board(self, rest: str) -> None:
        if self.model.chess is None:
            self.output("There's no game to show yet.")
        else:
            self._show_board()

    def _cmd_draw(self, rest: str) -> None:
        if not self._require_game():
            return
        if self.model.opponent_offered_draw:
            self.output(f"{self.model.opponent} already offered a draw, so this accepts it.")
        else:
            self.output("You offered a draw.")
        self.conn.send(T.OFFER_DRAW, {"game_id": self.model.game_id})
        self.model.offered_draw()

    def _cmd_accept(self, rest: str) -> None:
        self._answer_draw(True)

    def _cmd_decline(self, rest: str) -> None:
        self._answer_draw(False)

    def _cmd_resign(self, rest: str) -> None:
        if self._require_game():
            self.conn.send(T.RESIGN, {"game_id": self.model.game_id})

    def _cmd_state(self, rest: str) -> None:
        if self.model.game_id is None:
            self.output("You aren't in a game.")
        else:
            self.conn.send(T.GET_STATE, {"game_id": self.model.game_id})

    def _cmd_ping(self, rest: str) -> None:
        sent = time.perf_counter()
        seq = self.conn.send(T.PING)
        self._pings[seq] = sent

    def _cmd_raw(self, rest: str) -> None:
        message_type, _, body_text = rest.partition(" ")
        try:
            body = json.loads(body_text) if body_text.strip() else {}
            seq = self.conn.send(message_type.upper(), body)
        except (ValueError, ProtocolError) as exc:  # bad JSON, unknown type, or a body the schema rejects
            self.output(f"Can't send that: {exc}")
        else:
            self.output(f"Sent {message_type.upper()} with seq {seq}.")

    def _cmd_quit(self, rest: str) -> None:
        self.running = False
        self.conn.close()
        self.output("Bye.")

    _cmd_exit = _cmd_quit

    def _move(self, text: str) -> None:
        if not self._require_my_turn():
            return
        try:
            move = self._find_move(text)
        except ValueError:
            self.output(f"{text!r} isn't a legal move. Type 'moves' to see them.")
            return
        # The board isn't changed here. It changes when the server's MOVE_MADE arrives.
        self.conn.send(T.MOVE, {"game_id": self.model.game_id, "ply": self.model.ply, "move": move.uci()})

    def _find_move(self, text: str) -> Move:
        """Read a move typed as UCI (``e2e4``) or SAN (``e4``). Raises ``ValueError`` if it isn't legal."""
        board = self.model.chess.board
        legal = self.model.chess.legal_moves()
        try:
            from_sq, to_sq, promotion = parse_uci(text)
        except ValueError:
            return parse_san(board, text, legal)
        for move in legal:
            if (move.from_sq, move.to_sq, move.promotion) == (from_sq, to_sq, promotion):
                return move
        raise ValueError(text)

    def _answer_draw(self, accept: bool) -> None:
        if not self.model.opponent_offered_draw:
            self.output("There's no draw offer to answer.")
            return
        self.conn.send(T.ANSWER_DRAW, {"game_id": self.model.game_id, "accept": accept})
        self.model.answered_draw()
        if not accept:
            self.output("You declined the draw.")

    def _busy(self) -> bool:
        """Report and return True if the player can't start or join another game right now."""
        if self.model.in_game:
            self.output("You're already in a game.")
            return True
        if self.model.hosting:
            self.output(f"You're waiting in game {self.model.hosting}. Type 'cancel' first.")
            return True
        return False

    def _require_game(self) -> bool:
        if not self.model.in_game:
            self.output("You aren't in a game.")
            return False
        return True

    def _require_my_turn(self) -> bool:
        if not self._require_game():
            return False
        if not self.model.my_turn:
            self.output(f"It's {self.model.opponent}'s turn.")
            return False
        return True

    # Events from the server

    def process(self, event: Event) -> None:
        with self.lock:
            if isinstance(event, Closed):
                if self.running:  # don't announce a disconnect the user asked for
                    self.running = False
                    self.output(f"Disconnected: {event.reason}.")
                return
            if isinstance(event, Reconnecting):
                self.output(f"Connection lost ({event.reason}). Reconnecting...")
                return
            if isinstance(event, Reconnected):
                self._show_reconnected(event)
                return
            first_welcome = event.type is T.WELCOME and self.model.session_id is None
            self.model.apply(event)
            show = getattr(self, "_show_" + event.type.lower(), None)
            if show is not None and (event.type is not T.WELCOME or first_welcome):
                show(event)
            self._resync_if_needed()

    def _show_reconnected(self, event: Reconnected) -> None:
        if event.resumed:
            self.output("Reconnected.")
            return
        had_game = self.model.in_game or self.model.hosting is not None
        self.model.reset_session()
        if had_game:
            self.output("Reconnected, but your game couldn't be resumed. You're back in the lobby.")
        else:
            self.output("Reconnected.")

    def _show_welcome(self, message: Message) -> None:
        self.output(f"Connected as {self.model.name}. Type 'help' for commands, or 'list' to see open games.")

    def _show_game_list(self, message: Message) -> None:
        games = message.body["games"]
        if not games:
            self.output("No open games. Type 'create' to start one.")
            return
        lines = ["Open games:"]
        for game in games:
            color = game["host_color"]
            plays = f"plays {color}" if color else "color picked at random"
            lines.append(f"  {game['game_id']}: {game['host']} ({plays})")
        lines.append("Type 'join <game>' to play one.")
        self.output("\n".join(lines))

    def _show_game_created(self, message: Message) -> None:
        color = message.body["color"]
        side = f"You'll play {color}." if color else "Your color will be picked at random."
        self.output(f"Created game {message.body['game_id']}. {side} Waiting for an opponent... (type 'cancel' to stop)")

    def _show_game_cancelled(self, message: Message) -> None:
        self.output(f"Game {message.body['game_id']} cancelled.")

    def _show_game_start(self, message: Message) -> None:
        m = self.model
        self.output(f"Game {m.game_id} started. You're {m.my_color} against {m.opponent}.")
        self._show_board()

    def _show_move_made(self, message: Message) -> None:
        body = message.body
        who = "You" if body["by"] == self.model.my_color else self.model.opponent
        self.output(f"{who} played {body['san']}.")
        self._show_board()

    def _show_move_rejected(self, message: Message) -> None:
        self.output(f"Move rejected: {message.body['reason']}.")

    def _show_draw_offered(self, message: Message) -> None:
        self.output(f"{self.model.opponent} offers a draw. Type 'accept' or 'decline' (making a move also declines).")

    def _show_draw_declined(self, message: Message) -> None:
        self.output(f"{self.model.opponent} declined your draw offer.")

    def _show_game_over(self, message: Message) -> None:
        m = self.model
        verdict = {True: "You won!", False: "You lost.", None: "It's a draw."}[m.i_won()]
        self.output(f"Game over: {m.result} ({m.ending_text()}). {verdict} Type 'list' or 'create' to play again.")

    def _show_opponent_status(self, message: Message) -> None:
        if message.body["connected"]:
            self.output(f"{self.model.opponent} reconnected.")
        else:
            self.output(f"{self.model.opponent} lost their connection. Waiting for them to come back...")

    def _show_state(self, message: Message) -> None:
        if self.model.out_of_sync:
            self.output("The board still doesn't match the server's after a full refresh. Something is wrong.")
            return
        self.output("Board refreshed from the server.")
        self._show_board()

    def _show_error(self, message: Message) -> None:
        code = message.body["code"]
        try:
            name = ErrorCode(code).name
        except ValueError:
            name = "unknown"
        self.output(f"Error {code} ({name}): {message.body['message']}.")

    def _show_pong(self, message: Message) -> None:
        sent = self._pings.pop(message.re, None)
        if sent is not None:
            self.output(f"Pong: {(time.perf_counter() - sent) * 1000:.1f} ms round trip.")

    def _show_board(self) -> None:
        m = self.model
        lines = [render_board(m.chess.board, flip=m.my_color != WHITE, unicode=self.unicode)]
        if m.last_move is not None:
            lines.append(f"  Last move: {m.last_move.san}")
        self.output("\n".join(lines))

    def _resync_if_needed(self) -> None:
        """If the local board disagrees with the server's, ask for the full state (once)."""
        if self.model.should_resync():
            self.output("Your board doesn't match the server's. Asking for the full game state...")
            self.conn.send(T.GET_STATE, {"game_id": self.model.game_id})


class Terminal:
    """Shows text that arrives while the user is typing, without losing what they've typed.

    When input or output isn't a terminal (for example, commands piped in from
    a script), there's no prompt to protect, so it prints plain lines.
    """

    def __init__(self, prompt: Callable[[], str]) -> None:
        self._prompt = prompt
        self._lock = threading.Lock()
        self._reading = False
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty()

    def show(self, text: str) -> None:
        with self._lock:
            if self._reading and self.interactive:
                # Clear the prompt line, print the text, then redraw the prompt and typed text.
                sys.stdout.write("\r\x1b[K" + text + "\n" + self._prompt() + _typed_so_far())
            else:
                sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def read(self) -> str:
        with self._lock:
            self._reading = True
        try:
            return input(self._prompt() if self.interactive else "")
        finally:
            with self._lock:
                self._reading = False


def _typed_so_far() -> str:
    try:
        import readline
    except ImportError:  # Windows
        return ""
    return readline.get_line_buffer()


def _valid_name(name: str) -> bool:
    return 1 <= len(name) <= 32 and name.isprintable()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m client", description="CMTP chess text client")
    parser.add_argument("--host", default="127.0.0.1", help="server address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"server port (default: {DEFAULT_PORT})")
    parser.add_argument("--name", help="your player name (asked for if left out)")
    parser.add_argument("--unicode", action="store_true", help="draw pieces as chess symbols")
    args = parser.parse_args(argv)

    name = args.name
    try:
        while name is None or not _valid_name(name):
            if name is not None:
                print("Names are 1 to 32 characters, with no control characters.")
            name = input("Your name: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1

    conn = Connection(args.host, args.port)
    try:
        conn.connect()
    except OSError as exc:
        print(f"Couldn't connect to {args.host}:{args.port}: {exc}")
        return 1

    cli = ChessCli(conn, unicode=args.unicode)
    terminal = Terminal(cli.prompt)
    cli.output = terminal.show

    def pump_events() -> None:
        while True:
            event = conn.get()
            was_running = cli.running
            cli.process(event)
            if isinstance(event, Closed):
                if was_running:
                    terminal.show("Press Enter to exit.")
                return

    threading.Thread(target=pump_events, name="cli-events", daemon=True).start()
    cli.hello(name)
    try:
        while cli.running:
            line = terminal.read()
            if cli.running:
                cli.execute(line)
    except (EOFError, KeyboardInterrupt):
        print()
    finally:
        cli.running = False
        conn.close()
    return 0
