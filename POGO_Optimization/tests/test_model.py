"""Tests for the solver.

Most of these are regression tests for specific defects in the original
version. The constraint tests matter most: a constraint that is present but
never binds is indistinguishable from no constraint at all, which is exactly
how the original shipped a candy limit that did nothing.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pogo_opt.costs import MAX_LEVEL, STEP, cp_multiplier, cumulative_cost, step_cost
from pogo_opt.data import PokemonInstance
from pogo_opt.model import build_and_solve, rating, Weights


def make(instance_id="p1", species_id=6, level=20.0, **kw) -> PokemonInstance:
    """A Charizard-shaped default; override whatever the test cares about."""
    defaults = dict(
        instance_id=instance_id,
        species_id=species_id,
        name="Charizard",
        level=level,
        attack_iv=15,
        defense_iv=15,
        stamina_iv=15,
        base_attack=223,
        base_defense=173,
        base_stamina=186,
        fast_move="Fire Spin",
        fast_power=14,
        fast_duration=1.1,
        fast_energy=10,
        fast_type="fire",
        charge_move="Blast Burn",
        charge_power=110,
        charge_duration=3.3,
        charge_energy=100,
        charge_type="fire",
        type1="fire",
        type2="flying",
    )
    defaults.update(kw)
    return PokemonInstance(**defaults)


# --------------------------------------------------------------------------
# CP multiplier -- replaced a quadratic that returned ~128 at level 40
# --------------------------------------------------------------------------

def test_cpm_is_monotone_increasing():
    levels = [1 + 0.5 * i for i in range(99)]
    values = [cp_multiplier(l) for l in levels]
    assert all(b >= a for a, b in zip(values, values[1:]))


def test_cpm_stays_in_published_range():
    for level in (1, 10, 20, 30, 40, 50):
        assert 0.09 <= cp_multiplier(level) <= 0.85


def test_cpm_matches_published_anchors():
    assert cp_multiplier(40) == pytest.approx(0.7903, abs=1e-4)
    assert cp_multiplier(1) == pytest.approx(0.094, abs=1e-4)


def test_cpm_rejects_levels_outside_range():
    """Out-of-range is a caller bug, so it raises rather than silently clamping."""
    with pytest.raises(ValueError):
        cp_multiplier(0)
    with pytest.raises(ValueError):
        cp_multiplier(99)


# --------------------------------------------------------------------------
# Costs -- the old code computed a cost table and then never used it
# --------------------------------------------------------------------------

def test_no_cost_to_stand_still():
    assert cumulative_cost(20.0, 20.0) == (0, 0, 0)


def test_downgrade_is_rejected():
    """You cannot un-power-up, so asking for it is a caller bug."""
    with pytest.raises(ValueError):
        cumulative_cost(30.0, 25.0)


def test_cost_increases_with_distance():
    short = cumulative_cost(20.0, 21.0)[0]
    long = cumulative_cost(20.0, 25.0)[0]
    assert long > short > 0


def test_cost_increases_with_starting_level():
    early = cumulative_cost(5.0, 6.0)[0]
    late = cumulative_cost(35.0, 36.0)[0]
    assert late > early


def test_lucky_halves_stardust():
    normal = cumulative_cost(20.0, 25.0, "normal")[0]
    lucky = cumulative_cost(20.0, 25.0, "lucky")[0]
    assert lucky == pytest.approx(normal / 2, rel=0.01)


def test_purified_is_cheaper_than_normal():
    normal = cumulative_cost(20.0, 25.0, "normal")
    purified = cumulative_cost(20.0, 25.0, "purified")
    assert purified[0] < normal[0] and purified[1] <= normal[1]


def test_xl_candy_starts_at_forty_one():
    """39.0-40.5 is the last regular-candy tier (15 each); XL begins at 41.0
    and restarts its own 10/12/15/17/20 progression."""
    _, candy, xl = cumulative_cost(30.0, 35.0)
    assert xl == 0 and candy > 0

    _, candy_to_41, xl_to_41 = cumulative_cost(40.0, 41.0)
    assert candy_to_41 == 30 and xl_to_41 == 0

    _, candy_past_41, xl_past_41 = cumulative_cost(41.0, 45.0)
    assert xl_past_41 > 0 and candy_past_41 == 0


# --------------------------------------------------------------------------
# Constraints -- each must be shown to actually bind
# --------------------------------------------------------------------------

def test_never_exceeds_stardust_budget():
    collection = [make(f"p{i}", level=20.0) for i in range(10)]
    for budget in (5_000, 20_000, 100_000):
        r = build_and_solve(collection, stardust_budget=budget)
        assert r.feasible
        assert r.stardust_used <= budget


def test_tiny_budget_funds_nothing():
    r = build_and_solve([make(level=35.0)], stardust_budget=10)
    assert r.selections == []


def test_candy_constraint_binds():
    """With dust to spare, candy alone should cap the plan."""
    collection = [make(f"p{i}", species_id=6, level=20.0) for i in range(6)]

    unconstrained = build_and_solve(collection, stardust_budget=500_000)
    constrained = build_and_solve(
        collection, stardust_budget=500_000, candy_inventory={6: 5}
    )

    assert sum(s.candy for s in constrained.selections) <= 5
    assert len(constrained.selections) < len(unconstrained.selections)


def test_xl_candy_constrained_separately_from_candy():
    """Regression: XL was checked against the regular candy stock.

    A species with 300 candy could therefore spend 300 XL, worth roughly
    30,000 regular candy of buying power.
    """
    collection = [make("p1", species_id=6, level=40.5)]

    generous_candy = build_and_solve(
        collection,
        stardust_budget=2_000_000,
        candy_inventory={6: 300},
        xl_candy_inventory={6: 0},
        max_steps_per_pokemon=10,
    )
    for s in generous_candy.selections:
        assert s.xl_candy == 0
        assert s.target_level <= 41.0


def test_one_target_level_per_pokemon():
    collection = [make(f"p{i}", level=20.0) for i in range(5)]
    r = build_and_solve(collection, stardust_budget=200_000)
    ids = [s.pokemon.instance_id for s in r.selections]
    assert len(ids) == len(set(ids))


def test_mega_cap_binds_when_requested():
    collection = [make(f"p{i}", species_id=6, level=20.0) for i in range(5)]
    r = build_and_solve(collection, stardust_budget=500_000, max_megas=2)
    assert len(r.selections) <= 2


# --------------------------------------------------------------------------
# Regressions against specific original defects
# --------------------------------------------------------------------------

def test_duplicate_species_both_survive():
    """The original keyed decision variables on species_id.

    A second Machamp overwrote the first in the dict and silently vanished
    from the problem.
    """
    collection = [
        make("first", species_id=68, level=20.0),
        make("second", species_id=68, level=20.0),
    ]
    r = build_and_solve(collection, stardust_budget=500_000)
    assert len(r.selections) == 2


def test_powering_up_raises_rating():
    """The original divided attack by the Pokemon's own defense, which
    cancelled most of the level term and made power-ups look worthless."""
    p = make(level=20.0)
    w = Weights()
    assert rating(p, 30.0, w) > rating(p, 20.0, w)


def test_stab_only_applies_on_matching_type():
    matched = make(fast_type="fire", charge_type="fire", type1="fire", type2=None)
    unmatched = make(fast_type="water", charge_type="water", type1="fire", type2=None)
    w = Weights()
    assert rating(matched, 25.0, w) > rating(unmatched, 25.0, w)


def test_empty_collection_raises():
    with pytest.raises(ValueError):
        build_and_solve([], stardust_budget=1000)


def test_solver_beats_naive_greedy():
    """If a sort matched the solver, the LP would be pointless.

    Greedy picks by raw gain until the budget runs out, ignoring cost.
    """
    collection = [make(f"p{i}", level=15.0 + i) for i in range(12)]
    budget = 60_000

    optimal = build_and_solve(collection, stardust_budget=budget)

    candidates = []
    for p in collection:
        target = min(p.level + 3.0, MAX_LEVEL)
        dust = cumulative_cost(p.level, target, p.friendship)[0]
        gain = rating(p, target, Weights()) - rating(p, p.level, Weights())
        candidates.append((gain, dust))
    candidates.sort(key=lambda c: -c[0])

    spent, greedy_gain = 0, 0.0
    for gain, dust in candidates:
        if spent + dust <= budget:
            spent += dust
            greedy_gain += gain

    assert optimal.total_gain >= greedy_gain


# --------------------------------------------------------------------------
# Property tests (ported from the standalone runner, which was passing a
# (candy, xl) tuple as `candy_inventory` -- `species_id in (dict, dict)` is
# always False, so its candy constraint had silently stopped binding while
# still reporting PASS.)
# --------------------------------------------------------------------------

def test_more_budget_never_yields_less_gain():
    collection = [make(f"p{i}", species_id=6 + i, level=18.0 + i) for i in range(10)]
    gains = [
        build_and_solve(collection, stardust_budget=b).total_gain
        for b in (10_000, 25_000, 50_000, 100_000, 200_000)
    ]
    assert all(b >= a for a, b in zip(gains, gains[1:])), gains


def test_zero_budget_yields_empty_plan():
    r = build_and_solve([make(level=20.0)], stardust_budget=0)
    assert r.selections == []
    assert r.stardust_used == 0


def test_pokemon_at_max_level_generates_no_candidates():
    r = build_and_solve([make(level=50.0)], stardust_budget=1_000_000)
    assert r.selections == []


def test_tuple_passed_as_candy_inventory_is_rejected():
    """Guard the exact defect above: a silently-ignored constraint is worse
    than a loud failure."""
    with pytest.raises((TypeError, ValueError)):
        build_and_solve(
            [make(level=20.0)],
            stardust_budget=100_000,
            candy_inventory=({6: 5}, {6: 1}),   # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# Game-mechanics conformance
# --------------------------------------------------------------------------

REAL_CANDY = {}
for _lo, _hi, _c in [(1, 10.5, 1), (11, 20.5, 2), (21, 25.5, 3), (26, 30.5, 4),
                     (31, 32.5, 6), (33, 34.5, 8), (35, 36.5, 10),
                     (37, 38.5, 12), (39, 40.5, 15)]:
    _l = _lo
    while _l <= _hi:
        REAL_CANDY[round(_l, 1)] = _c
        _l += 0.5

REAL_STARDUST = {1.0: 200, 10.0: 1000, 20.0: 2500, 25.0: 4000,
                 30.0: 5000, 35.0: 8000, 39.0: 10000, 45.0: 13000}


@pytest.mark.parametrize("level,expected", sorted(REAL_CANDY.items()))
def test_candy_cost_matches_published_table(level, expected):
    """Candy tiers hold flat for 10 levels, then 5, then 5, then step every 2 --
    they do not align to the stardust brackets."""
    assert step_cost(level)[1] == expected


@pytest.mark.parametrize("level,expected", sorted(REAL_STARDUST.items()))
def test_stardust_cost_matches_published_table(level, expected):
    assert step_cost(level)[0] == expected


def test_xl_candy_progression_restarts_at_ten():
    """If XL began at 40.0 it would start at 15, breaking the 10/12/15/17/20
    progression. That's the evidence the boundary is 41.0."""
    assert step_cost(41.0)[2] == 10
    assert step_cost(43.0)[2] == 12
    assert step_cost(49.0)[2] == 20
    # 39.0-40.5 is still regular candy
    assert step_cost(40.0)[1] == 15 and step_cost(40.0)[2] == 0


def test_half_levels_use_the_quadratic_mean():
    """Below 40 the game defines CPM(n.5) = sqrt((CPM(n)^2 + CPM(n+1)^2)/2)."""
    import math
    a, b = cp_multiplier(20.0), cp_multiplier(21.0)
    assert cp_multiplier(20.5) == pytest.approx(math.sqrt((a * a + b * b) / 2))


def test_cpm_is_linear_above_forty():
    """41-50 rises by a flat 0.005 per level."""
    for lvl in range(41, 50):
        step = cp_multiplier(lvl + 1) - cp_multiplier(lvl)
        assert step == pytest.approx(0.005, abs=1e-6)


def test_shadow_takes_the_defense_penalty():
    """Shadow is attack x1.2 AND defense x5/6. Applying only the bonus scored
    Shadows with the upside and none of the cost."""
    from pogo_opt.model import SHADOW_ATTACK_MULT, SHADOW_DEFENSE_MULT
    assert SHADOW_ATTACK_MULT == pytest.approx(1.2)
    assert SHADOW_DEFENSE_MULT == pytest.approx(5 / 6)

    normal = make("n", is_shadow=False)
    shadow = make("s", is_shadow=True)
    w = Weights(bulk_exponent=1.0)   # weight bulk so the penalty can register
    assert rating(shadow, 25.0, w) < rating(normal, 25.0, w) * 1.2


def test_cheap_charge_move_needs_fewer_fast_moves():
    """Charge energy is per-move (33/50/100), not a constant 100."""
    cheap = make("cheap", charge_energy=50)
    pricey = make("pricey", charge_energy=100)
    w = Weights(bulk_exponent=0.0)
    assert rating(cheap, 25.0, w) > rating(pricey, 25.0, w)


def test_cp_formula_matches_known_values():
    """A perfect-IV Charizard at level 40 is CP 2889 in game."""
    from pogo_opt.model import combat_power
    p = make(level=40.0, attack_iv=15, defense_iv=15, stamina_iv=15)
    assert combat_power(p, 40.0) == 2889


def test_cp_rises_with_level_and_never_below_ten():
    from pogo_opt.model import combat_power
    p = make(level=1.0, attack_iv=0, defense_iv=0, stamina_iv=0)
    cps = [combat_power(p, l) for l in (1.0, 10.0, 20.0, 30.0, 40.0, 50.0)]
    assert all(b > a for a, b in zip(cps, cps[1:]))
    assert cps[0] >= 10


def test_shadow_no_longer_pays_a_power_up_surcharge():
    """Niantic removed it; the flag exists for reproducing older numbers."""
    from pogo_opt.costs import SHADOW_COST_SURCHARGE
    assert SHADOW_COST_SURCHARGE is False
    assert cumulative_cost(20.0, 25.0, "shadow")[0] == cumulative_cost(20.0, 25.0, "normal")[0]
