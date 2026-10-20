from client import ClientModel
from cmtp import ErrorCode, Message, MessageType

T = MessageType
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"


def receive(model, message_type, body):
    model.apply(Message(message_type, 1, body))


def started(color="white"):
    model = ClientModel()
    receive(model, T.GAME_START, {"game_id": "g1", "your_color": color, "opponent": "Ben", "fen": START_FEN})
    return model


def move_made(model, ply, move, fen, by, game_id="g1"):
    receive(model, T.MOVE_MADE, {
        "game_id": game_id, "ply": ply, "move": move, "san": "?", "fen": fen, "by": by,
    })


def test_welcome():
    model = ClientModel()
    receive(model, T.WELCOME, {"session_id": "s1", "resume_token": "tok"})
    assert (model.session_id, model.resume_token) == ("s1", "tok")


def test_hosting_then_cancelling():
    model = ClientModel()
    receive(model, T.GAME_CREATED, {"game_id": "g1", "color": None})
    assert model.hosting == "g1"
    receive(model, T.GAME_CANCELLED, {"game_id": "g1"})
    assert model.hosting is None


def test_game_start():
    model = started("black")
    assert model.in_game
    assert model.hosting is None
    assert (model.game_id, model.my_color, model.opponent) == ("g1", "black", "Ben")
    assert model.turn == "white"
    assert not model.my_turn
    assert model.ply == 0


def test_move_made_updates_the_board():
    model = started()
    move_made(model, 0, "e2e4", AFTER_E4, "white")
    assert model.chess.fen() == AFTER_E4
    assert model.last_move.san == "e4"
    assert model.ply == 1
    assert not model.out_of_sync


def test_move_for_another_game_is_ignored():
    model = started()
    move_made(model, 0, "e2e4", AFTER_E4, "white", game_id="g2")
    assert model.ply == 0


def test_wrong_ply_means_out_of_sync():
    model = started()
    move_made(model, 1, "e7e5", "whatever", "black")
    assert model.out_of_sync
    assert model.ply == 0


def test_different_fen_means_out_of_sync():
    model = started()
    move_made(model, 0, "e2e4", START_FEN, "white")
    assert model.out_of_sync


def test_illegal_move_from_server_means_out_of_sync():
    model = started()
    move_made(model, 0, "e2e5", AFTER_E4, "white")
    assert model.out_of_sync


def test_stale_ply_rejection_means_out_of_sync():
    model = started()
    receive(model, T.MOVE_REJECTED, {"game_id": "g1", "code": ErrorCode.STALE_PLY, "reason": "stale"})
    assert model.out_of_sync


def test_state_rebuilds_the_game():
    model = started()
    model.out_of_sync = True
    receive(model, T.STATE, {
        "game_id": "g1", "your_color": "white", "opponent": "Ben",
        "moves": ["e2e4"], "fen": AFTER_E4, "turn": "black",
    })
    assert model.chess.fen() == AFTER_E4
    assert model.last_move.uci == "e2e4"
    assert not model.out_of_sync


def test_state_that_does_not_replay_stays_out_of_sync():
    model = started()
    receive(model, T.STATE, {
        "game_id": "g1", "your_color": "white", "opponent": "Ben",
        "moves": ["e2e5"], "fen": AFTER_E4, "turn": "black",
    })
    assert model.out_of_sync


def test_draw_offers():
    model = started()
    receive(model, T.DRAW_OFFERED, {"game_id": "g1"})
    assert model.opponent_offered_draw
    move_made(model, 0, "e2e4", AFTER_E4, "white")  # moving declines it
    assert not model.opponent_offered_draw

    model.offered_draw()
    receive(model, T.DRAW_DECLINED, {"game_id": "g1"})
    assert not model.i_offered_draw


def test_game_over():
    model = started("black")
    receive(model, T.GAME_OVER, {"game_id": "g1", "result": "0-1", "reason": "resignation"})
    assert not model.in_game
    assert not model.my_turn
    assert (model.result, model.end_reason) == ("0-1", "resignation")
    assert model.i_won() is True


def test_i_won():
    model = started("white")
    assert model.i_won() is None
    model.result = "0-1"
    assert model.i_won() is False
    model.result = "1/2-1/2"
    assert model.i_won() is None


def test_should_resync_asks_once_until_back_in_step():
    model = started()
    assert not model.should_resync()
    model.out_of_sync = True
    assert model.should_resync()
    assert not model.should_resync()  # already asked
    receive(model, T.STATE, {
        "game_id": "g1", "your_color": "white", "opponent": "Ben",
        "moves": [], "fen": START_FEN, "turn": "white",
    })
    assert not model.out_of_sync
    model.out_of_sync = True
    assert model.should_resync()  # a new problem gets a new request


def test_opponent_connection_status():
    model = started()
    assert model.opponent_connected
    receive(model, T.OPPONENT_STATUS, {"game_id": "g1", "connected": False})
    assert not model.opponent_connected
    receive(model, T.OPPONENT_STATUS, {"game_id": "g1", "connected": True})
    assert model.opponent_connected


def test_reset_session_forgets_the_game():
    model = started()
    model.reset_session()
    assert not model.in_game
    assert model.game_id is None and model.chess is None and model.hosting is None
