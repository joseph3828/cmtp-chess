"""Draw charts from the CSV files the measurement tools write.

    python -m tools.charts docs/measurements

Each chart is saved as SVG (for documents) and PNG (for quick
viewing) in ``<directory>/charts``. Needs matplotlib: pip install -e ".[report]".
"""

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # files only, no window
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

# Colors: a categorical palette checked for color-blind separation, plus quiet
# ink and chrome. Text always uses the ink colors, never a series color.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]  # blue, orange, aqua, always in this order

plt.rcParams.update({
    "font.family": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 9.5,
    "text.color": INK,
    "axes.labelcolor": INK_2,
    "axes.edgecolor": AXIS,
    "axes.linewidth": 0.8,
    "axes.facecolor": SURFACE,
    "figure.facecolor": SURFACE,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "xtick.labelcolor": INK_2,
    "ytick.labelcolor": INK_2,
    "svg.fonttype": "none",  # keep text as text in SVGs
})

LINE = dict(linewidth=1.8, solid_capstyle="round", solid_joinstyle="round")
DOTS = dict(marker="o", markersize=6.5, markeredgecolor=SURFACE, markeredgewidth=1.5)  # the ring keeps dots legible
REFERENCE = dict(color=MUTED, linewidth=1.2, linestyle=(0, (4, 3)))  # dashed only for predictions, never grids


def read(path: Path) -> list[dict]:
    with open(path, newline="") as file:
        return [{k: float(v) if k != "errors" else int(v) for k, v in row.items()} for row in csv.DictReader(file)]


def figure(title: str, subtitle: str, *, columns: int = 1, width: float = 6.5, height: float = 3.4):
    fig, axes = plt.subplots(1, columns, figsize=(width, height), squeeze=False)
    fig.subplots_adjust(left=0.1, right=0.86, top=0.78, bottom=0.16, wspace=0.35)
    fig.text(0.02, 0.95, title, fontsize=12.5, fontweight="bold", color=INK, va="top")
    fig.text(0.02, 0.875, subtitle, fontsize=9.5, color=INK_2, va="top")
    for ax in axes[0]:
        style(ax)
    return fig, list(axes[0])


def style(ax) -> None:
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0, pad=6)


def end_label(ax, x, y, text, *, dy=0) -> None:
    """A direct label just past a line's last point, in ink (the line carries the color)."""
    ax.annotate(text, (x, y), xytext=(8, dy), textcoords="offset points", va="center", fontsize=9, color=INK_2)


def log_games_axis(ax, games) -> None:
    ax.set_xscale("log")
    # Label each measured point, except where it would run into the next label (250 next to 500).
    labeled, last = [], None
    for g in sorted(games, reverse=True):
        if last is None or last / g >= 2.5:
            labeled.append(g)
            last = g
    ax.set_xticks(games, [f"{g:g}" if g in labeled else "" for g in games])
    ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.set_xlabel("Games played at once")


def save(fig, out: Path, name: str) -> None:
    for suffix in ("svg", "png"):
        fig.savefig(out / f"{name}.{suffix}", dpi=200)
    plt.close(fig)
    print(f"  {name}")


def throughput(rows, out) -> None:
    games = [r["games"] for r in rows]
    rate = [r["moves_per_s"] for r in rows]
    fig, (ax,) = figure("The server levels off near 3,000 moves per second",
                        "Bots moving with no thinking time. One Python server process, one core of an Apple M2.")
    ax.plot(games, rate, color=SERIES[0], **LINE, **DOTS)
    log_games_axis(ax, games)
    ax.set_ylabel("Moves per second")
    ax.set_ylim(0, max(rate) * 1.2)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    peak = max(rows, key=lambda r: r["moves_per_s"])
    ax.annotate(f"{peak['moves_per_s']:,.0f} moves/s\nserver CPU {peak['server_cpu_pct']:.0f}%",
                (peak["games"], peak["moves_per_s"]), xytext=(0, 12), textcoords="offset points",
                ha="center", fontsize=9, color=INK_2)
    save(fig, out, "throughput")


def saturation_latency(rows, out) -> None:
    games = [r["games"] for r in rows]
    peak = max(r["moves_per_s"] for r in rows)
    fig, (ax,) = figure("Past its limit, every move waits in line",
                        f"Move round trip with no thinking time. Dashed: games ÷ {peak:,.0f} moves/s (Little's law).")
    median = [r["p50_ms"] for r in rows]
    p95 = [r["p95_ms"] for r in rows]
    predicted = [g / peak * 1000 for g in games]
    ax.plot(games, p95, color=SERIES[1], label="95th percentile", **LINE, **DOTS)
    ax.plot(games, median, color=SERIES[0], label="Median", **LINE, **DOTS)
    # The prediction runs almost on top of the median, so it's named in the legend, not at the line's end.
    ax.plot(games, predicted, label="Predicted (Little's law)", **REFERENCE)
    end_label(ax, games[-1], p95[-1], "95th percentile", dy=4)
    end_label(ax, games[-1], median[-1], "Median", dy=-8)
    log_games_axis(ax, games)
    ax.set_ylabel("Move round trip (ms)")
    ax.set_ylim(0, max(p95) * 1.1)
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    save(fig, out, "saturation_latency")


def realistic_load(rows, out) -> None:
    games = [r["games"] for r in rows]
    fig, (left, right) = figure("At human pace, 500 games stay around 1 ms a move",
                                "Bots think about 1 s per move. Left: move round trip. Right: server CPU.",
                                columns=2)
    median = [r["p50_ms"] for r in rows]
    p95 = [r["p95_ms"] for r in rows]
    left.plot(games, p95, color=SERIES[1], label="95th percentile", **LINE, **DOTS)
    left.plot(games, median, color=SERIES[0], label="Median", **LINE, **DOTS)
    log_games_axis(left, games)
    left.set_ylabel("Move round trip (ms)")
    left.set_ylim(0, max(p95) * 1.25)
    left.legend(loc="upper right", frameon=False, fontsize=8.5)

    cpu = [r["server_cpu_pct"] for r in rows]
    right.plot(games, cpu, color=SERIES[0], **LINE, **DOTS)
    log_games_axis(right, games)
    right.set_ylabel("Server CPU (% of one core)")
    right.set_ylim(0, 100)
    end_label(right, games[-1], cpu[-1], f"{cpu[-1]:.0f}%\n{rows[-1]['moves_per_s']:.0f} moves/s", dy=0)
    save(fig, out, "realistic_load")


def network_latency(rows, out) -> None:
    added = [r["latency_ms"] for r in rows]
    median = [r["p50_ms"] for r in rows]
    fig, (ax,) = figure("Network delay adds twice over: once each way",
                        "Move round trip through the chaos proxy. Dashed: twice the added one-way delay.")
    ax.plot(added, [2 * a for a in added], **REFERENCE)
    ax.plot(added, median, color=SERIES[0], **LINE, **DOTS)
    ax.set_xticks(added)
    ax.set_xlabel("Delay added each way (ms)")
    ax.set_ylabel("Median move round trip (ms)")
    ax.set_ylim(0, max(median) * 1.15)
    overhead = median[-1] - 2 * added[-1]
    end_label(ax, added[-1], median[-1], f"{median[-1]:.0f} ms\n(+{overhead:.0f} ms over\nthe delay alone)", dy=-14)
    save(fig, out, "network_latency")


def latency_distribution(samples_path, out, chosen=(1, 100, 500)) -> None:
    by_games = defaultdict(list)
    with open(samples_path, newline="") as file:
        for row in csv.DictReader(file):
            by_games[int(row["games"])].append(float(row["move_ms"]))
    fig, (ax,) = figure("Most moves come back in 1 to 3 ms",
                        "Share of moves completed within each time, bots thinking about 1 s per move.")
    # The curves sit too close together for direct labels, so the legend names them.
    for color, games in zip(SERIES, chosen):
        values = sorted(by_games[games])
        share = [100 * (i + 1) / len(values) for i in range(len(values))]
        ax.plot(values, share, color=color, label=f"{games} game{'s' if games > 1 else ''}", **LINE)
    ax.set_xlim(0, 8)
    ax.set_ylim(0, 101)
    ax.set_xlabel("Move round trip (ms)")
    ax.set_ylabel("Moves completed (%)")
    ax.legend(loc="lower right", frameon=False, fontsize=9)
    save(fig, out, "latency_distribution")


def time_budget(engine_rows, round_trip_ms, out) -> None:
    by_name = {r["operation"]: r for r in engine_rows}
    bars = [
        ("Round trip on localhost, 1 game", round_trip_ms * 1000),
        ("Server checks and plays the move", by_name["play_move"]["median_us"]),
        ("Part of that: finding legal moves", by_name["legal_moves"]["median_us"]),
        ("Decode one MOVE_MADE", by_name["decode_move_made"]["median_us"]),
        ("Encode one MOVE_MADE", by_name["encode_move_made"]["median_us"]),
    ]
    fig, (ax,) = figure("Where a move's time goes",
                        "Median times. The rules engine is the server's biggest cost; encoding is minor.",
                        height=3.0)
    fig.subplots_adjust(left=0.4, right=0.9)
    labels = [label for label, _ in bars][::-1]
    values = [value for _, value in bars][::-1]
    ax.barh(labels, values, height=0.42, color=SERIES[0])
    for y, value in enumerate(values):
        ax.annotate(f"{value:,.0f} µs", (value, y), xytext=(5, 0), textcoords="offset points",
                    va="center", fontsize=9, color=INK_2)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_xlim(0, max(values) * 1.18)
    ax.set_xlabel("Microseconds")
    ax.tick_params(axis="y", labelcolor=INK)
    save(fig, out, "time_budget")


def message_sizes(rows, out) -> None:
    names = [r["message"] for r in rows]
    json_sizes = [r["json_mean_bytes"] for r in rows]
    binary = [r["binary_bytes"] for r in rows]
    fig, (ax,) = figure("JSON costs about 6 to 10 times the bytes of a binary encoding",
                        "Average frame size, header included. Binary: a move in 16 bits, no SAN or FEN.",
                        height=2.8)
    fig.subplots_adjust(left=0.18, right=0.9)
    rows_y = list(range(len(names)))[::-1]
    gap = 0.2
    ax.barh([y + gap for y in rows_y], json_sizes, height=0.34, color=SERIES[0], label="JSON (CMTP)")
    ax.barh([y - gap for y in rows_y], binary, height=0.34, color=SERIES[1], label="Minimal binary")
    for y, j, b in zip(rows_y, json_sizes, binary):
        ax.annotate(f"{j:.0f} bytes", (j, y + gap), xytext=(5, 0), textcoords="offset points", va="center",
                    fontsize=9, color=INK_2)
        ax.annotate(f"{b:.0f} bytes", (b, y - gap), xytext=(5, 0), textcoords="offset points", va="center",
                    fontsize=9, color=INK_2)
    ax.set_yticks(rows_y, names)
    ax.tick_params(axis="y", labelcolor=INK)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_xlim(0, max(json_sizes) * 1.2)
    ax.set_xlabel("Bytes per message")
    ax.legend(loc="upper right", frameon=False, fontsize=9)
    save(fig, out, "message_sizes")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m tools.charts", description="Draw charts from measurement CSVs")
    parser.add_argument("directory", type=Path, nargs="?", default=Path("docs/measurements"))
    args = parser.parse_args(argv)
    data = args.directory
    out = data / "charts"
    out.mkdir(parents=True, exist_ok=True)

    throughput_rows = read(data / "throughput_summary.csv")
    throughput(throughput_rows, out)
    saturation_latency(throughput_rows, out)
    realistic_load(read(data / "scaling_summary.csv"), out)
    network_latency(read(data / "latency_summary.csv"), out)
    latency_distribution(data / "scaling_samples.csv", out)
    single_game_ms = next(r for r in throughput_rows if r["games"] == 1)["p50_ms"]
    with open(data / "engine_timing.csv", newline="") as file:
        engine = [{**row, "median_us": float(row["median_us"])} for row in csv.DictReader(file)]
    time_budget(engine, single_game_ms, out)
    with open(data / "message_sizes.csv", newline="") as file:
        sizes = [{**row, "json_mean_bytes": float(row["json_mean_bytes"]), "binary_bytes": int(row["binary_bytes"])}
                 for row in csv.DictReader(file)]
    message_sizes(sizes, out)


if __name__ == "__main__":
    main()
