"""Command line interface."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .analyze import Options, Recommendation, rank, recommend
from .inputs import FunctionUsage, InputError, read_file
from .pricing import DEFAULT_PRICE_PER_GB_SECOND, DEFAULT_PRICE_PER_MILLION_REQUESTS, Pricing, make_pricing


def money(value: float) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value == 0 or value >= 0.995:
        return f"{sign}${value:,.2f}"
    if value >= 0.01:
        return f"{sign}${value:.4f}"
    return f"{sign}${value:.6f}"


def _rows(recommendations: Sequence[Recommendation], scale: float) -> list[dict]:
    return [{
        "function": r.function,
        "invocations": r.invocations,
        "memory_mb": r.memory_mb,
        "max_memory_used_mb": round(r.max_memory_used_mb, 1),
        "used_percent": round(100 * r.used_fraction, 1),
        "recommended_mb": r.recommended_mb,
        "action": r.action,
        "estimated_cost_now_usd": round(r.cost_now * scale, 6),
        "estimated_cost_recommended_usd": round(r.cost_recommended * scale, 6),
        "estimated_saving_usd": round(r.saving * scale, 6),
        "flags": list(r.flags),
    } for r in recommendations]


def render_table(recommendations: Sequence[Recommendation], scale: float, period: str,
                 upper_bound: bool = True) -> str:
    header = ["function", "invocations", "memory MB", "max used MB", "used", "suggested MB",
              "est. cost now", "est. cost suggested", "est. saving", "flags"]
    rows = [header]
    for r in recommendations:
        change = money(r.saving * scale) if r.action != "keep" else "-"
        rows.append([
            r.function, f"{r.invocations:,}", str(r.memory_mb), f"{r.max_memory_used_mb:.0f}",
            f"{100 * r.used_fraction:.0f}%",
            str(r.recommended_mb) if r.action != "keep" else "keep",
            money(r.cost_now * scale), money(r.cost_recommended * scale), change,
            ", ".join(r.flags),
        ])
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    lines = []
    for index, row in enumerate(rows):
        cells = [row[0].ljust(widths[0])]
        cells += [c.rjust(w) for c, w in zip(row[1:-1], widths[1:-1])]
        cells.append(row[-1])
        lines.append("  ".join(cells).rstrip())
        if index == 0:
            lines.append("  ".join("-" * w for w in widths))
    decreases = sum(r.saving for r in recommendations if r.action == "decrease") * scale
    increases = -sum(r.saving for r in recommendations if r.action == "increase") * scale
    lines.append("")
    lines.append(f"Costs are estimates {period}.")
    word = "up to" if upper_bound else "about"
    lines.append(f"Suggested decreases: {word} {money(decreases)} less. "
                 f"Suggested increases: {word} {money(increases)} more.")
    return "\n".join(lines)


def _assumptions(pricing: Pricing, options: Options) -> list[str]:
    if options.cpu_bound_fraction == 0:
        duration = ("duration is assumed not to change with memory, so the cost changes shown "
                    "are upper bounds (see --cpu-bound-fraction)")
    else:
        duration = (f"{options.cpu_bound_fraction:.0%} of billed time is assumed to scale inversely "
                    "with memory")
    return [
        f"Prices: ${pricing.per_gb_second:.10f} per GB-second, "
        f"${pricing.per_million_requests:.2f} per 1M requests ({pricing.source}).",
        f"Assumptions: {options.headroom:.0%} headroom over the highest Max Memory Used, rounded up "
        f"to {options.step_mb} MB; {duration}.",
        "Nothing was changed. These are estimates from a sample, not exact savings; test a new "
        "memory size before relying on it.",
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lambda-rightsize",
        description="Suggest a memory size for each Lambda function from exported usage. Read-only.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="print suggested memory sizes and estimated cost changes")
    report.add_argument("files", nargs="+", metavar="[FUNCTION=]FILE",
                        help=".csv files are read as CloudWatch Logs Insights exports; any other file as "
                             "a log with REPORT lines for one function (named by FUNCTION= or the file name)")
    report.add_argument("--days", type=float, metavar="N",
                        help="number of days the export covers; costs are then scaled to 30 days")
    report.add_argument("--headroom", type=float, default=20.0, metavar="PERCENT",
                        help="free memory to keep above the highest Max Memory Used (default 20)")
    report.add_argument("--step", type=int, default=64, metavar="MB",
                        help="round suggestions up to a multiple of this (default 64)")
    report.add_argument("--arch", choices=sorted(DEFAULT_PRICE_PER_GB_SECOND), default="x86_64",
                        help="selects the default GB-second price (default x86_64)")
    report.add_argument("--price-gb-second", type=float, metavar="USD",
                        help="price per GB-second (default: "
                             f"{DEFAULT_PRICE_PER_GB_SECOND['x86_64']} x86_64, "
                             f"{DEFAULT_PRICE_PER_GB_SECOND['arm64']} arm64; us-east-1 list price)")
    report.add_argument("--price-per-million-requests", type=float, metavar="USD",
                        help=f"price per one million requests (default {DEFAULT_PRICE_PER_MILLION_REQUESTS}; "
                             "us-east-1 list price)")
    report.add_argument("--cpu-bound-fraction", type=float, default=0.0, metavar="0..1",
                        help="share of billed time assumed to scale inversely with memory (default 0: "
                             "duration unchanged)")
    report.add_argument("--timeout", type=float, metavar="SECONDS",
                        help="configured timeout, used to flag functions running close to it")
    report.add_argument("--min-invocations", type=int, default=100,
                        help="flag functions with fewer invocations as small-sample (default 100)")
    report.add_argument("--format", choices=["table", "csv", "json"], default="table")
    return parser


def main(argv: Optional[Sequence[str]] = None, out=None, err=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    problems = []
    if args.days is not None and args.days <= 0:
        problems.append("--days must be greater than 0")
    if args.headroom < 0:
        problems.append("--headroom cannot be negative")
    if args.step < 1:
        problems.append("--step must be at least 1")
    if not 0 <= args.cpu_bound_fraction <= 1:
        problems.append("--cpu-bound-fraction must be between 0 and 1")
    for name in ("price_gb_second", "price_per_million_requests"):
        if getattr(args, name) is not None and getattr(args, name) < 0:
            problems.append(f"--{name.replace('_', '-')} cannot be negative")
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=err)
        return 2

    table: dict[str, FunctionUsage] = {}
    for argument in args.files:
        label, path = Path(argument).stem, Path(argument)
        if "=" in argument and not path.exists():
            label, _, rest = argument.partition("=")
            path = Path(rest)
        try:
            used = read_file(path, label, table)
        except OSError as error:
            print(f"error: cannot read {path}: {error.strerror}", file=err)
            return 2
        except InputError as error:
            print(f"error: {path}: {error}", file=err)
            return 2
        if not used:
            print(f"warning: no usable rows or REPORT lines in {path}", file=err)
    if not table:
        print("error: no usage data found", file=err)
        return 1

    pricing = make_pricing(args.arch, args.price_gb_second, args.price_per_million_requests)
    options = Options(headroom=args.headroom / 100.0, step_mb=args.step,
                      cpu_bound_fraction=args.cpu_bound_fraction,
                      min_invocations=args.min_invocations, timeout_seconds=args.timeout)
    ranked = rank([recommend(usage, pricing, options) for usage in table.values()])
    scale = 30.0 / args.days if args.days else 1.0
    period = (f"per 30 days, scaled from a {args.days:g}-day export" if args.days
              else "for the period the input covers (pass --days N to scale to 30 days)")

    if args.format == "json":
        json.dump({
            "period": period,
            "price_per_gb_second": pricing.per_gb_second,
            "price_per_million_requests": pricing.per_million_requests,
            "price_source": pricing.source,
            "functions": _rows(ranked, scale),
        }, out, indent=2)
        out.write("\n")
    elif args.format == "csv":
        rows = _rows(ranked, scale)
        writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "flags": "; ".join(row["flags"])})
    else:
        print(render_table(ranked, scale, period, options.cpu_bound_fraction == 0), file=out)
        print("", file=out)
        for line in _assumptions(pricing, options):
            print(line, file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
