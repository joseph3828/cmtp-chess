"""Load test: many bot games at once against a real server, measuring move latency and server CPU.

A move's latency is the time from a bot sending MOVE to it receiving the
matching MOVE_MADE: the full round trip through the network and the server,
including the server checking the move with the rules engine.

The server runs as its own process, so its CPU time can be measured apart
from the bots'. The bots run in several worker processes, so bot-side work
(each bot follows the game with its own rules engine) doesn't pile up in one
Python process and inflate the measured latency. With ``--latency``, all
traffic goes through the chaos proxy, which adds that delay each way.

Each run writes two CSV files: ``<out>_summary.csv`` (one row per scenario)
and ``<out>_samples.csv`` (every move's latency, for distribution charts).

    python -m tools.load_test --games 1 10 100 500 --think 1 --out docs/measurements/load
    python -m tools.load_test --games 4 --latency 0 0.05 0.1 0.2 --out docs/measurements/latency
"""

import argparse
import asyncio
import csv
import itertools
import multiprocessing
import os
import random
import signal
import socket
import statistics
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from tools.bot import play_pair

try:
    import resource
except ImportError:  # Windows
    resource = None


def _raise_open_file_limit() -> None:
    """Each connection is an open file. macOS allows only 256 by default, which 500 games would exceed."""
    if resource is None:
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 10240 if hard == resource.RLIM_INFINITY else min(hard, 10240)
    if soft < wanted:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _start(module: str, *args: str) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-m", module, *args],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=_raise_open_file_limit,
    )
    return process


def _wait_until_listening(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _stop(process: subprocess.Popen) -> float:
    """Stop a child process and return the CPU seconds it used, from the operating system's own accounting."""
    process.send_signal(signal.SIGTERM)
    _, status, usage = os.wait4(process.pid, 0)
    process.returncode = os.waitstatus_to_exitcode(status)
    return usage.ru_utime + usage.ru_stime


def _start_server(port: int) -> subprocess.Popen:
    server = _start("server", "--host", "127.0.0.1", "--port", str(port), "--max-clients", "100000")
    _wait_until_listening(port)
    return server


# Worker processes


def _play_games(port: int, seeds: list[int], max_plies: int, think: float, spread: float) -> dict:
    _raise_open_file_limit()
    return asyncio.run(_play_games_async(port, seeds, max_plies, think, spread))


async def _play_games_async(port: int, seeds: list[int], max_plies: int, think: float, spread: float) -> dict:
    async def one(seed: int):
        # Start games at different moments, as players arriving would, instead of all at once.
        await asyncio.sleep(random.Random(seed).uniform(0, spread))
        try:
            white, black = await play_pair(port=port, seed=seed, max_plies=max_plies, think=think)
        except Exception as exc:  # report it, but let the other games finish
            return [], 0, f"{type(exc).__name__}: {exc}"
        return white.move_times + black.move_times, len(white.moves), None

    started = time.time()
    results = await asyncio.gather(*(one(seed) for seed in seeds))
    return {
        "started": started,
        "finished": time.time(),
        "move_times": [t for times, _, _ in results for t in times],
        "moves": sum(moves for _, moves, _ in results),
        "errors": [error for _, _, error in results if error],
    }


# Scenarios


@dataclass
class Result:
    games: int
    latency: float
    think: float
    moves: int
    wall: float
    server_cpu: float
    move_times: list[float]
    errors: list[str]

    def summary(self) -> dict:
        ms = sorted(t * 1000 for t in self.move_times)
        # "inclusive" keeps percentiles within the measured values instead of extrapolating past them.
        cuts = statistics.quantiles(ms, n=100, method="inclusive") if len(ms) >= 2 else ms * 99
        return {
            "games": self.games,
            "latency_ms": round(self.latency * 1000),
            "think_s": self.think,
            "moves": self.moves,
            "wall_s": round(self.wall, 2),
            "moves_per_s": round(self.moves / self.wall, 1),
            "p50_ms": round(cuts[49], 2),
            "p95_ms": round(cuts[94], 2),
            "p99_ms": round(cuts[98], 2),
            "max_ms": round(ms[-1], 2) if ms else 0,
            "server_cpu_pct": round(100 * self.server_cpu / self.wall, 1),
            "errors": len(self.errors),
        }


def run_scenario(
    games: int, latency: float, *, think: float, max_plies: int, seed: int, workers: int, baseline_cpu: float,
) -> Result:
    server_port = _free_port()
    server = _start_server(server_port)
    proxy = None
    port = server_port
    if latency:
        port = _free_port()
        proxy = _start("tools.chaos_proxy", "--port", str(port), "--target-port", str(server_port),
                       "--latency", str(latency))
        _wait_until_listening(port)

    seeds = [seed + 2 * i for i in range(games)]
    workers = max(1, min(workers, games))
    shares = [seeds[i::workers] for i in range(workers)]
    # With thinking time, games start at different moments over one thinking period, as
    # players arriving would. Without it, they all start together to find the server's limit.
    spread = think
    try:
        with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            reports = list(pool.map(_play_games, itertools.repeat(port), shares, itertools.repeat(max_plies),
                                    itertools.repeat(think), itertools.repeat(spread)))
    finally:
        server_cpu = _stop(server) - baseline_cpu
        if proxy is not None:
            _stop(proxy)

    wall = max(r["finished"] for r in reports) - min(r["started"] for r in reports)
    return Result(
        games, latency, think, sum(r["moves"] for r in reports), wall, max(server_cpu, 0.0),
        [t for r in reports for t in r["move_times"]], [e for r in reports for e in r["errors"]],
    )


def measure_baseline_cpu() -> float:
    """CPU an idle server uses just starting and stopping, to subtract from each scenario."""
    port = _free_port()
    return _stop(_start_server(port))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m tools.load_test", description="Measure CMTP move latency under load")
    parser.add_argument("--games", type=int, nargs="+", default=[1, 10, 100], help="concurrent games, one scenario each")
    parser.add_argument("--latency", type=float, nargs="+", default=[0.0], help="seconds added each way by the proxy")
    parser.add_argument("--think", type=float, default=0.0, help="average seconds a bot waits before moving")
    parser.add_argument("--max-plies", type=int, default=40, help="agree a draw after this many half-moves")
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1), help="bot processes")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", type=Path, help="write <out>_summary.csv and <out>_samples.csv")
    args = parser.parse_args(argv)

    _raise_open_file_limit()
    baseline = measure_baseline_cpu()
    rows, samples = [], []
    for games, latency in itertools.product(args.games, args.latency):
        result = run_scenario(games, latency, think=args.think, max_plies=args.max_plies, seed=args.seed,
                              workers=args.workers, baseline_cpu=baseline)
        row = result.summary()
        rows.append(row)
        samples += [(games, row["latency_ms"], round(t * 1000, 3)) for t in result.move_times]
        print(", ".join(f"{key}={value}" for key, value in row.items()), flush=True)
        for error in result.errors[:5]:
            print("  error:", error)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with open(f"{args.out}_summary.csv", "w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        with open(f"{args.out}_samples.csv", "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["games", "latency_ms", "move_ms"])
            writer.writerows(samples)
        print(f"wrote {args.out}_summary.csv and {args.out}_samples.csv")


if __name__ == "__main__":
    main()
