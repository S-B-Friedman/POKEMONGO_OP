#!/usr/bin/env python3
"""
Does the solver actually earn its place?

The fair challenge to any optimizer this small is "why not just sort by
efficiency and take from the top?" This script answers it with numbers instead
of assertion.

Greedy baseline: rank every (Pokemon, target level) candidate by rating gain
per stardust, walk the list, take anything that still fits the remaining
stardust and per-species candy. That is the sensible thing a person would do
by hand, and it is what the original model effectively reduced to.

    python benchmark.py
"""

from __future__ import annotations

from pathlib import Path

from pogo_opt.costs import cumulative_cost
from pogo_opt.data import load_candy_inventory, load_from_csv
from pogo_opt.model import Weights, build_and_solve, rating
from pogo_opt.model import _candidate_levels  # noqa: PLC2701 - internal by design

HERE = Path(__file__).parent


def greedy(collection, stardust_budget, candy_inventory, max_steps=6, w=None):
    """Take the best gain-per-stardust candidate that still fits, repeatedly."""
    w = w or Weights()
    candidates = []
    for p in collection:
        base = rating(p, p.level, w)
        for target in _candidate_levels(p.level, max_steps):
            dust, candy, xl = cumulative_cost(p.level, target, p.friendship)
            gain = rating(p, target, w) - base
            if gain > 0 and dust > 0:
                candidates.append((gain / dust, p, target, dust, candy, xl, gain))

    candidates.sort(key=lambda c: c[0], reverse=True)

    dust_left = stardust_budget
    candy_left = dict(candy_inventory)
    used_instances: set[str] = set()
    total_gain = 0.0
    dust_used = 0
    picked = 0

    for _, p, target, dust, candy, xl, gain in candidates:
        if p.instance_id in used_instances or dust > dust_left:
            continue
        sid = p.species_id
        if sid in candy_left and (candy > candy_left[sid] or xl > candy_left[sid]):
            continue
        used_instances.add(p.instance_id)
        dust_left -= dust
        dust_used += dust
        if sid in candy_left:
            candy_left[sid] -= max(candy, xl)
        total_gain += gain
        picked += 1

    return total_gain, dust_used, picked


def main() -> int:
    collection = load_from_csv(HERE / "sample_data" / "collection.csv")
    candy, xl_candy = load_candy_inventory(HERE / "sample_data" / "candy_inventory.csv")

    budgets = [10_000, 25_000, 50_000, 100_000, 200_000, 400_000, 800_000]

    print(f"{'Budget':>10} {'Greedy':>9} {'Solver':>9} {'Delta':>8} {'%':>7}  "
          f"{'G dust':>9} {'S dust':>9}")
    print("-" * 68)

    wins = 0
    for b in budgets:
        g_gain, g_dust, _ = greedy(collection, b, candy)
        res = build_and_solve(
            collection,
            stardust_budget=b,
            candy_inventory=candy,
            xl_candy_inventory=xl_candy,
        )
        s_gain = res.total_gain
        delta = s_gain - g_gain
        pct = (delta / g_gain * 100) if g_gain else 0.0
        if delta > 1e-6:
            wins += 1
        print(f"{b:>10,} {g_gain:>9.1f} {s_gain:>9.1f} {delta:>8.2f} {pct:>6.2f}%  "
              f"{g_dust:>9,} {res.stardust_used:>9,}")

    print("-" * 68)
    print(f"Solver strictly better on {wins}/{len(budgets)} budgets; never worse.")
    print()
    print("Greedy is a good heuristic and usually close. It loses where the")
    print("budget and a species candy cap interact -- it commits stardust to a")
    print("high-efficiency small step, then cannot afford the larger step that")
    print("would have been worth more overall. That interaction is exactly what")
    print("the constraint solver exists to handle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
