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
    ap.add_argument("--source", choices=("csv", "pokegenie", "mysql"), default="csv",
                    help="csv = flat pre-joined schema; pokegenie = a Scan Pro export")
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
    elif args.source == "pokegenie":
        from pogo_opt.importers.pokegenie import import_csv

        result = import_csv(args.csv)
        print(result.summary())
        for issue in result.warnings:
            print(f"  note: row {issue.row} {issue.name}: {issue.reason}")
        if not result.collection:
            raise SystemExit(
                "nothing imported. Check the column mapping:\n  "
                + ("\n  ".join(f"{k} <- {v}" for k, v in result.columns.items())
                   or "(no columns matched at all)")
            )
        collection = result.collection
        print()
    else:
        # A Poke Genie export handed to the flat loader dies on a missing key,
        # because that loader wants a pre-joined schema with base_attack,
        # fast_power and the rest. Say which flag to use rather than letting a
        # KeyError explain it.
        try:
            collection = load_from_csv(args.csv)
        except KeyError as exc:
            raise SystemExit(
                f"{args.csv} is missing the column {exc}, which the flat CSV "
                f"schema requires.\nIf this is a Poke Genie export, load it with "
                f"--source pokegenie."
            ) from exc

    # An explicitly named file that is not there is a mistake, not a request to
    # solve without candy. Falling back silently would drop every candy
    # constraint and still print a confident plan.
    explicit_candy = args.candy != DEFAULT_CANDY
    if args.candy and explicit_candy and not args.candy.exists():
        raise SystemExit(f"--candy file not found: {args.candy}")

    # The bundled candy file describes the bundled collection and nobody else.
    # Applying it to an imported collection is worse than having no candy data:
    # the species ids overlap, so real Pokemon get invented stock and the plan
    # reports itself fully costed. Only use the default alongside the default
    # collection.
    using_sample_collection = args.source == "csv" and args.csv == DEFAULT_CSV
    candy_path = args.candy if (explicit_candy or using_sample_collection) else None
    if candy_path is None and args.candy == DEFAULT_CANDY:
        print("No --candy given, so candy is unconstrained. The sample candy "
              "file is not applied to an imported collection.\n")

    candy, xl_candy = load_candy_inventory(
        candy_path if candy_path and candy_path.exists() else None
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
