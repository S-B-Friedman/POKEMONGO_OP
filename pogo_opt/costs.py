"""
Level-dependent constants: CP multipliers and power-up costs.

NOTE ON DATA PROVENANCE
-----------------------
The integer-level CPM anchors below are the widely published values. Before
relying on this for anything load-bearing, verify them against the authoritative
source: PokeMiners' GAME_MASTER (`POKEMON_UPGRADE_SETTINGS` / `cpMultiplier`).
This module is deliberately the single place where that swap happens.

The previous implementation approximated CPM as a quadratic
(0.095*L^2 - 0.854*L + 10), which is an upward-opening parabola and diverges
badly from the real curve -- it returns ~128 at level 40 where the true value
is ~0.79. Every downstream stat was therefore wrong by orders of magnitude.
"""

from __future__ import annotations

import math
from functools import lru_cache

# Official CPM values at integer levels.
_CPM_INTEGER = {
    1: 0.094, 2: 0.16639787, 3: 0.21573247, 4: 0.25572005, 5: 0.29024988,
    6: 0.3210876, 7: 0.34921268, 8: 0.37523559, 9: 0.39956728, 10: 0.4225,
    11: 0.44310755, 12: 0.46279839, 13: 0.48168495, 14: 0.49985844,
    15: 0.51739395, 16: 0.53435433, 17: 0.55079269, 18: 0.56675452,
    19: 0.58227891, 20: 0.5974, 21: 0.61215729, 22: 0.62656713,
    23: 0.64065295, 24: 0.65443563, 25: 0.667934, 26: 0.68116492,
    27: 0.69414365, 28: 0.70688421, 29: 0.71939909, 30: 0.7317,
    31: 0.73776948, 32: 0.74378943, 33: 0.74976104, 34: 0.75568551,
    35: 0.76156384, 36: 0.76739717, 37: 0.77318643, 38: 0.77893275,
    39: 0.78463697, 40: 0.7903, 41: 0.79530001, 42: 0.8003,
    43: 0.8053, 44: 0.8103, 45: 0.81529999, 46: 0.82029999,
    47: 0.82529999, 48: 0.83029999, 49: 0.83529999, 50: 0.84029999,
}

MIN_LEVEL = 1.0
MAX_LEVEL = 50.0
STEP = 0.5


@lru_cache(maxsize=256)
def cp_multiplier(level: float) -> float:
    """CP multiplier for a (possibly half-) level in [1, 50]."""
    if not (MIN_LEVEL <= level <= MAX_LEVEL):
        raise ValueError(f"level {level} outside [{MIN_LEVEL}, {MAX_LEVEL}]")

    lo = int(math.floor(level))
    if level == lo:
        return _CPM_INTEGER[lo]

    hi = lo + 1
    a, b = _CPM_INTEGER[lo], _CPM_INTEGER[hi]
    if lo >= 40:
        # Above 40 the published curve is linear, so half-levels are the
        # arithmetic midpoint.
        return (a + b) / 2.0
    # Below 40 half-levels are the quadratic mean of the neighbouring values.
    return math.sqrt((a * a + b * b) / 2.0)


def all_levels() -> list[float]:
    """Every reachable level, 1.0 -> 50.0 in half steps."""
    n = int((MAX_LEVEL - MIN_LEVEL) / STEP) + 1
    return [round(MIN_LEVEL + i * STEP, 1) for i in range(n)]


# Cost of ONE power-up (a +0.5 level step) taken FROM the given level.
# Each bracket spans four half-level steps. Levels 41+ consume XL candy
# rather than regular candy, so the two are tracked separately instead of
# being collapsed via a 1 XL = 100 candy fudge factor (which distorts the
# budget constraint).
_STARDUST_BRACKETS = [
    200, 400, 600, 800, 1000, 1300, 1600, 1900, 2200, 2500,
    3000, 3500, 4000, 4500, 5000, 6000, 7000, 8000, 9000, 10000,
    11000, 12000, 13000, 14000, 15000,
]

# Candy tiers do NOT align to the stardust brackets. Stardust changes every two
# levels; candy holds flat for ten levels, then five, then five, then switches
# to two-level steps at 31. Indexing candy with the stardust bracket index (the
# previous approach) silently mispriced 24 of the 78 pre-XL levels -- level 39
# was charged 10 candy against a real cost of 15.
#
# (from_level_inclusive, to_level_inclusive, candy)
_CANDY_TIERS: list[tuple[float, float, int]] = [
    (1.0, 10.5, 1),
    (11.0, 20.5, 2),
    (21.0, 25.5, 3),
    (26.0, 30.5, 4),
    (31.0, 32.5, 6),
    (33.0, 34.5, 8),
    (35.0, 36.5, 10),
    (37.0, 38.5, 12),
    (39.0, 39.5, 15),
    # XL candy from level 40. Amounts restart their own progression, which is
    # what made 41.0 a plausible guess -- but GAME_MASTER pins the first XL
    # tier to levels 40-41, not 41-42, so the whole ladder sat one level high.
    (40.0, 41.5, 10),
    (42.0, 43.5, 12),
    (44.0, 45.5, 15),
    (46.0, 47.5, 17),
    (48.0, 49.5, 20),
]

# POKEMON_UPGRADE_SETTINGS.xlCandyMinPokemonLevel. Verified by
# `python scripts/build_reference.py --check-costs`, which is also a test.
XL_CANDY_THRESHOLD = 40.0

# Niantic removed the Shadow power-up cost surcharge; see step_cost().
SHADOW_COST_SURCHARGE = False


def _candy_for(level: float) -> int:
    for lo, hi, candy in _CANDY_TIERS:
        if lo - 1e-9 <= level <= hi + 1e-9:
            return candy
    raise ValueError(f"no candy tier covers level {level}")


def _bracket_index(level: float) -> int:
    idx = int((level - MIN_LEVEL) / (STEP * 4))
    return min(idx, len(_STARDUST_BRACKETS) - 1)


def step_cost(level: float, friendship: str = "normal") -> tuple[int, int, int]:
    """
    Cost of powering up once from `level`.

    Returns (stardust, candy, xl_candy). Exactly one of candy / xl_candy is
    non-zero.

    `friendship` applies the standard multipliers:
      lucky    -> half stardust
      purified -> 10% off stardust and candy
      shadow   -> 20% surcharge on stardust and candy
    """
    if level >= MAX_LEVEL:
        raise ValueError("cannot power up beyond level 50")

    i = _bracket_index(level)
    dust = _STARDUST_BRACKETS[i]
    candy = _candy_for(level)

    # Lucky halves stardust; purified is 10% off both.
    #
    # SHADOW: Niantic removed the Shadow power-up surcharge (it was 1.2x on both
    # stardust and candy). Kept as a flag rather than deleted because anyone
    # reproducing older numbers will want it, but it defaults OFF because that
    # matches the current game. Set SHADOW_COST_SURCHARGE = True to restore.
    multipliers = {
        "normal": (1.0, 1.0),
        "lucky": (0.5, 1.0),
        "purified": (0.9, 0.9),
        "shadow": (1.2, 1.2) if SHADOW_COST_SURCHARGE else (1.0, 1.0),
    }
    if friendship not in multipliers:
        raise ValueError(f"unknown friendship state: {friendship}")
    dm, cm = multipliers[friendship]

    dust = int(round(dust * dm))
    candy = int(math.ceil(candy * cm))

    if level >= XL_CANDY_THRESHOLD:
        return dust, 0, candy
    return dust, candy, 0


def cumulative_cost(
    from_level: float, to_level: float, friendship: str = "normal"
) -> tuple[int, int, int]:
    """Total (stardust, candy, xl_candy) to walk from one level to another."""
    if to_level < from_level:
        raise ValueError("to_level must be >= from_level")

    dust = candy = xl = 0
    lvl = from_level
    while lvl < to_level - 1e-9:
        d, c, x = step_cost(lvl, friendship)
        dust += d
        candy += c
        xl += x
        lvl = round(lvl + STEP, 1)
    return dust, candy, xl
