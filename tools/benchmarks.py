"""Benchmarks that don't need a network: where a move's time goes, and how big messages are.

- Timing: how long the rules engine takes to check and play one move on the
  server, and how long encoding and decoding a message takes, measured over
  real random games.
- Message sizes: bytes on the wire for MOVE and MOVE_MADE as CMTP sends them
  (JSON), compared with a minimal binary encoding that sends a move as 16 bits.

    python -m tools.benchmarks --out docs/measurements
"""

import argparse
import csv
import random
import statistics
import time
from pathlib import Path

from chessrules import ChessGame, legal_moves
from cmtp import Message, MessageType, Sender, decode, encode, frame_message

T = MessageType

# A minimal binary MOVE: 4-byte frame length, 1-byte message type, 2-byte seq,
# 4-byte game number, 2-byte ply, and the move in 2 bytes (from square 6 bits,
# to square 6 bits, promotion piece 3 bits).
BINARY_MOVE = 4 + 1 + 2 + 4 + 2 + 2
# MOVE_MADE adds a 2-byte re and a 1-byte color, and drops SAN and FEN: the
# client can work them out with its own rules engine.
BINARY_MOVE_MADE = BINARY_MOVE + 2 + 1


def random_games(count: int, seed: int, max_plies: int = 200) -> list[list[str]]:
    """Move lists of seeded random games, as UCI."""
    rng = random.Random(seed)
    games = []
    for _ in range(count):
        game = ChessGame()
        while not game.is_over and game.ply < max_plies:
            game.play(rng.choice(sorted(game.legal_uci())))
        games.append([played.uci for played in game.history])
    return games


def time_engine(games: list[list[str]]) -> dict[str, list[float]]:
    """Seconds per operation, for every move of every game."""
    times = {"legal_moves": [], "play_move": [], "encode_move_made": [], "decode_move_made": []}
    for moves in games:
        game = ChessGame()
        for ply, move in enumerate(moves):
            start = time.perf_counter()
            legal_moves(game.board)
            times["legal_moves"].append(time.perf_counter() - start)

            # What the server does for each MOVE: check legality, make the move,
            # write SAN, and check for the end of the game.
            start = time.perf_counter()
            played = game.play(move)
            times["play_move"].append(time.perf_counter() - start)

            message = Message(T.MOVE_MADE, ply + 1, {
                "game_id": "g1", "ply": ply, "move": played.uci, "san": played.san, "fen": played.fen, "by": "white",
            })
            start = time.perf_counter()
            payload = encode(message)
            times["encode_move_made"].append(time.perf_counter() - start)
            start = time.perf_counter()
            decode(payload, sender=Sender.SERVER)
            times["decode_move_made"].append(time.perf_counter() - start)
    return times


def message_sizes(games: list[list[str]]) -> dict[str, list[int]]:
    """Frame sizes in bytes for every MOVE and MOVE_MADE in the games."""
    sizes = {"MOVE": [], "MOVE_MADE": []}
    for moves in games:
        game = ChessGame()
        for ply, move in enumerate(moves):
            played = game.play(move)
            sizes["MOVE"].append(len(frame_message(Message(T.MOVE, ply + 1, {
                "game_id": "g1", "ply": ply, "move": move,
            }))))
            sizes["MOVE_MADE"].append(len(frame_message(Message(T.MOVE_MADE, ply + 1, {
                "game_id": "g1", "ply": ply, "move": played.uci, "san": played.san, "fen": played.fen,
                "by": "white" if ply % 2 == 0 else "black",
            }, re=ply + 1))))
    return sizes


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m tools.benchmarks", description="Time the rules engine and size CMTP messages")
    parser.add_argument("--games", type=int, default=50)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", type=Path, help="directory for engine_timing.csv and message_sizes.csv")
    args = parser.parse_args(argv)

    games = random_games(args.games, args.seed)
    print(f"{args.games} random games, {sum(map(len, games))} moves")

    timing_rows = []
    for name, values in time_engine(games).items():
        us = [v * 1e6 for v in values]
        row = {
            "operation": name,
            "median_us": round(statistics.median(us), 1),
            "p95_us": round(statistics.quantiles(us, n=100, method="inclusive")[94], 1),
            "samples": len(us),
        }
        timing_rows.append(row)
        print(f"  {name:18} median {row['median_us']:8.1f} us   p95 {row['p95_us']:8.1f} us")

    size_rows = []
    for name, values in message_sizes(games).items():
        binary = BINARY_MOVE if name == "MOVE" else BINARY_MOVE_MADE
        row = {
            "message": name,
            "json_mean_bytes": round(statistics.mean(values), 1),
            "json_min_bytes": min(values),
            "json_max_bytes": max(values),
            "binary_bytes": binary,
        }
        size_rows.append(row)
        print(f"  {name:10} JSON {row['json_mean_bytes']:6.1f} bytes on average "
              f"({row['json_min_bytes']}-{row['json_max_bytes']}), minimal binary {binary} bytes")

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for filename, rows in (("engine_timing.csv", timing_rows), ("message_sizes.csv", size_rows)):
            with open(args.out / filename, "w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        print(f"wrote {args.out}/engine_timing.csv and {args.out}/message_sizes.csv")


if __name__ == "__main__":
    main()
