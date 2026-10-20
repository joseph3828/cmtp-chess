from chessrules import STARTING_FEN, ChessGame, legal_moves, parse_fen, parse_square


def legal_uci(fen):
    return {move.uci() for move in legal_moves(parse_fen(fen))}


def moves_from(fen, square):
    return {uci for uci in legal_uci(fen) if uci.startswith(square)}


def play(*moves, fen=STARTING_FEN):
    game = ChessGame(fen)
    for move in moves:
        game.play(move)
    return game


def test_starting_position_has_twenty_moves():
    moves = legal_uci(STARTING_FEN)
    assert len(moves) == 20
    assert {"e2e4", "e2e3", "g1f3", "b1a3"} <= moves


# Castling

CASTLING_READY = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"


def test_castling_both_sides():
    assert {"e1g1", "e1c1"} <= legal_uci(CASTLING_READY)
    assert {"e8g8", "e8c8"} <= legal_uci(CASTLING_READY.replace(" w ", " b "))


def test_castling_blocked_by_pieces():
    moves = legal_uci("r3k2r/8/8/8/8/8/8/RN2K1NR w KQkq - 0 1")
    assert "e1g1" not in moves
    assert "e1c1" not in moves


def test_no_castling_out_of_check():
    moves = legal_uci("4k3/4r3/8/8/8/8/8/R3K2R w KQ - 0 1")
    assert "e1g1" not in moves
    assert "e1c1" not in moves


def test_no_castling_through_an_attacked_square():
    moves = legal_uci("4k3/5r2/8/8/8/8/8/R3K2R w KQ - 0 1")
    assert "e1g1" not in moves
    assert "e1c1" in moves


def test_no_castling_into_check():
    moves = legal_uci("4k3/6r1/8/8/8/8/8/R3K2R w KQ - 0 1")
    assert "e1g1" not in moves
    assert "e1c1" in moves


def test_queen_side_castling_allowed_when_only_b1_is_attacked():
    assert "e1c1" in legal_uci("4k3/1r6/8/8/8/8/8/R3K2R w KQ - 0 1")
    assert "e1c1" not in legal_uci("4k3/3r4/8/8/8/8/8/R3K2R w KQ - 0 1")


def test_castling_moves_the_rook():
    game = play("e1g1", fen=CASTLING_READY)
    assert game.fen() == "r3k2r/8/8/8/8/8/8/R4RK1 b kq - 1 1"
    game.play("e8c8")
    assert game.fen() == "2kr3r/8/8/8/8/8/8/R4RK1 w - - 2 2"


def test_castling_rights_lost_when_king_moves():
    assert play("e1f1", fen=CASTLING_READY).board.castling == "kq"


def test_castling_rights_lost_when_rook_moves():
    assert play("h1h2", fen=CASTLING_READY).board.castling == "Qkq"


def test_castling_rights_lost_when_rook_is_captured():
    # Rxa8 loses white's queen-side right (rook left a1) and black's (rook taken).
    assert play("a1a8", fen=CASTLING_READY).board.castling == "Kk"


# En passant


def test_en_passant_capture():
    game = play("e2e4", "a7a6", "e4e5", "d7d5")
    assert "e5d6" in game.legal_uci()
    game.play("e5d6")
    assert game.fen() == "rnbqkbnr/1pp1pppp/p2P4/8/8/8/PPPP1PPP/RNBQKBNR b KQkq - 0 3"


def test_en_passant_only_right_after_the_double_push():
    game = play("e2e4", "a7a6", "e4e5", "d7d5", "g1f3", "a6a5")
    assert "e5d6" not in game.legal_uci()


def test_en_passant_that_exposes_the_king_is_illegal():
    # Taking on d6 would clear both pawns off the 5th rank, so the rook would check the king.
    moves = legal_uci("8/8/8/K2pP2r/8/8/8/7k w - d6 0 1")
    assert "e5d6" not in moves
    assert "e5e6" in moves


# Promotion


def test_promotion_offers_four_pieces():
    assert moves_from("8/P7/8/8/8/8/8/k6K w - - 0 1", "a7") == {
        "a7a8q", "a7a8r", "a7a8b", "a7a8n",
    }


def test_capture_promotion():
    moves = moves_from("1n6/P7/8/8/8/8/8/k6K w - - 0 1", "a7")
    assert moves == {
        "a7a8q", "a7a8r", "a7a8b", "a7a8n",
        "a7b8q", "a7b8r", "a7b8b", "a7b8n",
    }


def test_black_promotion():
    game = play("a2a1n", fen="k6K/8/8/8/8/8/p7/8 b - - 0 1")
    assert game.board.piece_at(parse_square("a1")) == "n"


# Check and pins


def test_pinned_piece_cannot_move():
    assert moves_from("4k3/4r3/8/8/8/8/4N3/4K3 w - - 0 1", "e2") == set()


def test_only_king_moves_escape_rook_check():
    assert legal_uci("4k3/8/8/8/8/8/8/r3K3 w - - 0 1") == {"e1d2", "e1e2", "e1f2"}


def test_check_can_be_captured_or_blocked():
    assert legal_uci("4k3/8/8/8/8/8/1B6/r3K3 w - - 0 1") == {
        "e1d2", "e1e2", "e1f2", "b2a1", "b2c1",
    }


def test_king_cannot_capture_a_defended_piece():
    assert legal_uci("4k3/8/8/8/8/2b5/3q4/4K3 w - - 0 1") == {"e1f1"}


# Clocks


def test_halfmove_clock_and_fullmove_number():
    game = play("g1f3")
    assert (game.board.halfmove_clock, game.board.fullmove_number) == (1, 1)
    game.play("g8f6")
    assert (game.board.halfmove_clock, game.board.fullmove_number) == (2, 2)
    game.play("e2e4")  # pawn move resets the clock
    assert (game.board.halfmove_clock, game.board.fullmove_number) == (0, 2)
    game.play("f6e4")  # so does a capture
    assert (game.board.halfmove_clock, game.board.fullmove_number) == (0, 3)
