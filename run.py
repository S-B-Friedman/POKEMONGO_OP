#!/usr/bin/env python3
"""
Pokemon GO power-up allocation solver.

    python run.py                             # sample data, 100k stardust
    python run.py --stardust 250000
    python run.py --source mysql              # reads POGO_DB_* from env
    python run.py --candy sample_data/candy_inventory.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pogo_opt.data import load_candy_inventory, load_from_csv, load_from_mysql
from pogo_opt.model import Weights, build_and_solve

HERE = Path(__file__).parent
DEFAULT_CSV = HERE / "sample_data" / "collection.csv"
DEFAULT_CANDY = HERE / "sample_data" / "candy_inventory.csv"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=("csv", "mysql"), default="csv")
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="collection CSV (source=csv)")
    ap.add_argument("--candy", type=Path, default=DEFAULT_CANDY, help="candy inventory CSV")
    ap.add_argument("--stardust", type=int, default=100_000, help="stardust budget")
    ap.add_argument("--max-steps", type=int, default=6, help="max power-ups per Pokemon")
    ap.add_argument("--max-megas", type=int, default=None,
                    help="optional cap on mega-capable Pokemon; off by default")
    ap.add_argument("--max-pokemon", type=int, default=None, help="cap how many to touch")
    ap.add_argument("--bulk", type=float, default=0.5, help="0 = pure DPS, 1 = DPS x bulk")
    ap.add_argument("-v", "--verbose", action="store_true", help="show solver log")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.source == "mysql":
        collection = load_from_mysql()
    else:
        collection = load_from_csv(args.csv)

    # An explicitly named file that is not there is a mistake, not a request to
    # solve without candy. Falling back silently would drop every candy
    # constraint and still print a confident plan.
    if args.candy and args.candy != DEFAULT_CANDY and not args.candy.exists():
        raise SystemExit(f"--candy file not found: {args.candy}")

    candy, xl_candy = load_candy_inventory(
        args.candy if args.candy and args.candy.exists() else None
    )

    print(f"Loaded {len(collection)} Pokemon"
          f"{f', candy inventory for {len(candy)} species' if candy else ''}.")
    print(f"Stardust budget: {args.stardust:,}\n")

    result = build_and_solve(
        collection,
        stardust_budget=args.stardust,
        candy_inventory=candy,
        xl_candy_inventory=xl_candy,
        max_steps_per_pokemon=args.max_steps,
        max_megas=args.max_megas,
        max_pokemon=args.max_pokemon,
        weights=Weights(bulk_exponent=args.bulk),
        verbose=args.verbose,
    )

    if not result.feasible:
        print(f"Solver status: {result.status}. No plan produced.", file=sys.stderr)
        return 1

    if not result.selections:
        print("Budget too small to fund any power-up.")
        return 0

    name_w = max(len(s.pokemon.name) for s in result.selections)
    name_w = max(name_w, 8)

    header = (f"{'Pokemon':<{name_w}}  {'From':>5} {'':2} {'To':<5} "
              f"{'Stardust':>9} {'Candy':>6} {'XL':>4} {'Gain':>8}")
    print(header)
    print("-" * len(header))

    for s in result.selections:
        # The marker names the state, because they do not pull the same way:
        # lucky and purified make a power-up cheaper, shadow makes it dearer.
        # A single "*" for all three said only "this one is unusual".
        tag = {"lucky": "L", "shadow": "S", "purified": "P"}.get(
            s.pokemon.friendship, ""
        )
        label = f"{s.pokemon.name}{tag}"
        print(
            f"{label:<{name_w}}  {s.pokemon.level:>5} ->  {s.target_level:<5} "
            f"{s.stardust:>9,} {s.candy:>6} {s.xl_candy:>4} {s.gain:>8.1f}"
        )

    print("-" * len(header))
    leftover = result.stardust_budget - result.stardust_used
    print(f"{len(result.selections)} Pokemon | "
          f"stardust {result.stardust_used:,} / {result.stardust_budget:,} "
          f"({leftover:,} unspent) | total gain {result.total_gain:.1f}")
    states = set().union(*(s.pokemon.friendship for s in result.selections)) - {"normal"}
    if states:
        legend = {
            "lucky": "L lucky (half stardust)",
            "purified": "P purified (10% off)",
            "shadow": "S shadow (20% surcharge)",
        }
        print("  ".join(legend[s] for s in ("lucky", "purified", "shadow") if s in states))

    warning = result.candy_warning()
    if warning:
        print(f"\nWARNING: {warning}.")
        print("         Pass --candy with your per-species counts to constrain it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
