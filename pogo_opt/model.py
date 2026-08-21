"""
The optimization model.

WHAT CHANGED FROM THE ORIGINAL, AND WHY
---------------------------------------
1. Real cost coefficients. The old stardust constraint charged a flat 1000 per
   Pokemon regardless of level, and the candy constraint was
   `binary_var * 50 <= 500`, which is satisfied for every value a binary can
   take -- a no-op. With uniform weights and no binding second constraint the
   problem degenerates to "sort by score, take the top N", which a sort solves
   without an LP. Costs now come from the real per-level tables.

2. Marginal gain, not absolute score. Maximizing the total score of the chosen
   set rewards picking Pokemon that were already good. What you actually want
   is the improvement bought per unit of stardust. The objective is now the
   delta between the post-power-up and current rating.

3. Decision variables are per owned Pokemon, not per species. The original
   keyed its dict on species id, so a second Machamp silently overwrote the
   first and vanished from the problem.

4. Candy is constrained per species, which is how candy actually works, and XL
   candy is tracked as a separate resource rather than converted at
   1 XL = 100 candy (a conversion that does not exist in the game and badly
   distorted the budget).

Together these make it a genuine multi-dimensional knapsack: one shared
stardust budget, one candy budget per species, and a cap on megas.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pulp

from .costs import MAX_LEVEL, STEP, cp_multiplier, cumulative_cost
from .data import PokemonInstance

# Species with a Mega/Primal form. Trimmed to the set the original tracked.
MEGA_SPECIES = {
    3, 6, 9, 15, 18, 65, 80, 94, 115, 127, 130, 142, 150, 181, 208, 212,
    214, 229, 248, 254, 257, 260, 282, 302, 303, 306, 308, 310, 319, 323,
    334, 354, 359, 362, 373, 376, 380, 381, 382, 383, 384, 428, 445, 448, 460,
    475, 531, 719,   # Gallade, Audino, Diancie
}

# Damage is dealt against a target, so it scales with the TARGET's defense.
# The original divided by the attacker's own defense, which penalised bulky
# attackers for being bulky. A fixed reference defender keeps ratings
# comparable across the collection.
REFERENCE_DEFENSE = 180.0

STAB_BONUS = 1.2

# Shadow Pokemon deal 20% more damage and take more damage: attack x1.2,
# defense x0.833 (5/6). The defense penalty was previously omitted, so Shadows
# were scored with the upside and none of the cost.
SHADOW_ATTACK_MULT = 1.2
SHADOW_DEFENSE_MULT = 5.0 / 6.0


@dataclass(frozen=True)
class Weights:
    """Tunable preferences. These are opinions, so they live in one place."""

    mega: float = 1.20
    hundo: float = 1.15       # all IVs 15
    nundo: float = 1.10       # all IVs 0, rare enough to be a collectible
    lucky: float = 1.05
    bulk_exponent: float = 0.5   # 0 = pure DPS, 1 = DPS x bulk


# --------------------------------------------------------------------------
# Rating
# --------------------------------------------------------------------------

def _adjusted(base: int, iv: int, cpm: float, multiplier: float = 1.0) -> float:
    return (base + iv) * cpm * multiplier


def combat_power(p: PokemonInstance, level: float) -> int:
    """CP as the game computes it.

        CP = floor( Atk * sqrt(Def) * sqrt(Sta) * CPM^2 / 10 ), minimum 10

    Not used by the solver -- it exists so an OCR-read CP can be checked
    against the CP implied by the species, level and IVs. A mismatch means one
    of the three was misread.
    """
    cpm = cp_multiplier(level)
    atk = p.base_attack + p.attack_iv
    dfn = p.base_defense + p.defense_iv
    sta = p.base_stamina + p.stamina_iv
    return max(10, math.floor(atk * math.sqrt(dfn) * math.sqrt(sta) * cpm * cpm / 10))


def _damage(attack: float, power: int, stab: float) -> float:
    return math.floor(0.5 * power * (attack / REFERENCE_DEFENSE) * stab) + 1


def _stab(move_type: str, p: PokemonInstance) -> float:
    types = {t.lower() for t in (p.type1, p.type2) if t}
    return STAB_BONUS if move_type.lower() in types else 1.0


def rating(p: PokemonInstance, level: float, w: Weights) -> float:
    """
    Score for one Pokemon at a hypothetical level.

    Uses a full fast/charge cycle rather than summing two independent DPS
    figures (which the original did, and which is not a rate of anything).
    """
    cpm = cp_multiplier(level)
    atk = _adjusted(p.base_attack, p.attack_iv, cpm,
                    SHADOW_ATTACK_MULT if p.is_shadow else 1.0)
    dfn = _adjusted(p.base_defense, p.defense_iv, cpm,
                    SHADOW_DEFENSE_MULT if p.is_shadow else 1.0)
    sta = _adjusted(p.base_stamina, p.stamina_iv, cpm)

    fast_dmg = _damage(atk, p.fast_power, _stab(p.fast_type, p))
    charge_dmg = _damage(atk, p.charge_power, _stab(p.charge_type, p))

    # Charge moves cost 33, 50 or 100 energy depending on the move. Assuming
    # 100 for all of them (the previous behaviour) doubled the cycle time of
    # every 50-energy move and made cheap-charge movesets look far worse than
    # they are.
    energy_per_fast = max(p.fast_energy, 1)
    charge_energy = abs(p.charge_energy) if p.charge_energy else 100
    fasts_per_charge = math.ceil(charge_energy / energy_per_fast)

    cycle_time = fasts_per_charge * max(p.fast_duration, 0.1) + max(p.charge_duration, 0.1)
    cycle_damage = fasts_per_charge * fast_dmg + charge_dmg
    cycle_dps = cycle_damage / cycle_time

    bulk = math.sqrt(max(dfn * sta, 1.0))
    score = cycle_dps * (bulk ** w.bulk_exponent)

    if p.species_id in MEGA_SPECIES:
        score *= w.mega
    if p.attack_iv == p.defense_iv == p.stamina_iv == 15:
        score *= w.hundo
    if p.attack_iv == p.defense_iv == p.stamina_iv == 0:
        score *= w.nundo
    if p.is_lucky:
        score *= w.lucky

    return score


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

def candy_pool_key(species_id: int):
    """The key a species' candy stock is really held under.

    The evolution family where one is known, and the dex number otherwise --
    for a species missing from the reference, or when reference.json is absent
    entirely. Falling back to the dex number reproduces the old per-species
    behaviour for that species rather than dropping its constraint, which would
    be the worse failure.
    """
    try:
        from .reference import default_reference

        family = default_reference().family_by_dex().get(species_id)
    except Exception:
        family = None
    return family or species_id


def _as_pool_keyed(inventory: dict | None) -> dict:
    """Re-key an inventory onto candy pools, whatever it arrived keyed by.

    An int key is a dex number and gets mapped to its family; anything else is
    already a pool key and is left alone. Where both forms name one pool they
    must agree, because they describe a single pile of candy.
    """
    if not inventory:
        return {}
    out: dict = {}
    for key, value in inventory.items():
        pool = candy_pool_key(key) if isinstance(key, int) else key
        if pool in out and out[pool] != value:
            raise ValueError(
                f"candy inventory gives two different counts for the {pool} "
                f"family ({out[pool]} and {value}); candy is pooled per family, "
                f"so those describe the same pile"
            )
        out[pool] = value
    return out


@dataclass
class Selection:
    pokemon: PokemonInstance
    target_level: float
    stardust: int
    candy: int
    xl_candy: int
    gain: float


@dataclass
class Result:
    status: str
    selections: list[Selection]
    total_gain: float
    stardust_used: int
    stardust_budget: int
    shadow_prices: dict[str, float] | None = None
    unbacked_candy: dict[int, tuple[int, int]] = field(default_factory=dict)

    @property
    def feasible(self) -> bool:
        return self.status == "Optimal"

    @property
    def fully_costed(self) -> bool:
        """Whether every resource the plan spends was actually constrained.

        False means the plan is optimal against stardust but is spending candy
        whose stock nobody supplied, so it may be impossible to execute. See
        `unbacked_candy`.
        """
        return not self.unbacked_candy

    def candy_warning(self) -> str | None:
        """One line naming what the plan assumes it can afford, or None."""
        if not self.unbacked_candy:
            return None
        worst = sorted(self.unbacked_candy.items(), key=lambda kv: -sum(kv[1]))
        shown = ", ".join(
            f"species {sid} needs {c}" + (f" + {x} XL" if x else "")
            for sid, (c, x) in worst[:4]
        )
        more = f", and {len(worst) - 4} more" if len(worst) > 4 else ""
        return (
            f"plan spends candy for {len(worst)} species with no known stock "
            f"({shown}{more}) -- those constraints did not bind, so the plan "
            f"may not be affordable"
        )


def shadow_prices(prob: pulp.LpProblem) -> dict[str, float]:
    """Dual value per resource constraint, from the LP relaxation.

    Re-solves with every binary relaxed to [0, 1]: an integer program has no
    duals, so asking CBC for `.pi` after a MIP solve gives None. The relaxation's
    duals are an approximation -- they answer "what would one more unit of this
    resource have been worth if the plan could be fractional" -- but that is
    still the question worth surfacing, and it is the only version of it that
    is cheap to compute.

    Reported per unit of resource, so stardust prices are tiny by construction
    (gain per single stardust) while candy prices are large. Compare a resource
    against itself over time, not against a different resource.
    """
    relaxed = prob.copy()
    for var in relaxed.variables():
        # Note: PuLP does NOT keep cat == "Binary". A variable declared binary
        # is stored as LpInteger with bounds [0, 1], so testing against
        # LpBinary here silently matches nothing and every dual comes back
        # empty -- which looks exactly like "no constraint is binding".
        if var.cat == pulp.LpInteger:
            var.cat = pulp.LpContinuous
            if var.lowBound is None:
                var.lowBound = 0
    relaxed.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[relaxed.status] != "Optimal":
        return {}
    out = {}
    for name, con in relaxed.constraints.items():
        if name.startswith("One_Target_"):
            continue   # not a resource; its dual is not actionable
        pi = con.pi
        if pi is not None and abs(pi) > 1e-12:
            out[name] = float(pi)
    return dict(sorted(out.items(), key=lambda kv: -abs(kv[1])))


def _candidate_levels(current: float, max_steps: int) -> list[float]:
    out = []
    lvl = current
    for _ in range(max_steps):
        lvl = round(lvl + STEP, 1)
        if lvl > MAX_LEVEL:
            break
        out.append(lvl)
    return out


def build_and_solve(
    collection: list[PokemonInstance],
    stardust_budget: int,
    candy_inventory: dict[int, int] | None = None,
    xl_candy_inventory: dict[int, int] | None = None,
    max_steps_per_pokemon: int = 6,
    max_megas: int | None = None,
    max_pokemon: int | None = None,
    weights: Weights | None = None,
    verbose: bool = False,
    with_shadow_prices: bool = False,
) -> Result:
    """Solve the power-up allocation problem."""
    if not collection:
        raise ValueError("collection is empty -- nothing to optimize")

    w = weights or Weights()
    # A constraint that silently stops binding is the worst failure mode this
    # code has -- it looks like it's working and quietly returns a wrong plan.
    # Both original defects (flat stardust cost, `binary * 50 <= 500`) were of
    # exactly this kind, so bad inventory arguments raise rather than no-op.
    for label, inv in (("candy_inventory", candy_inventory),
                       ("xl_candy_inventory", xl_candy_inventory)):
        if inv is None:
            continue
        if not isinstance(inv, dict):
            raise TypeError(
                f"{label} must be a dict of species_id -> count, got "
                f"{type(inv).__name__}. load_candy_inventory() returns a "
                f"(candy, xl_candy) tuple -- unpack it."
            )
        for k, v in inv.items():
            # A key is either a dex number or a candy-pool name ("Gible"), and
            # both are accepted -- see _as_pool_keyed. What is still rejected is
            # a count that is not a number, because that is how a malformed
            # inventory silently stops constraining anything.
            if not isinstance(k, (int, str)) or not isinstance(v, (int, float)):
                raise ValueError(
                    f"{label} must map species_id or family name -> numeric "
                    f"count; got {k!r} -> {v!r}"
                )

    # Accept either keying. load_candy_inventory already returns family keys,
    # but a caller with a dict of species ids in hand -- which is the obvious
    # thing to write -- must not have its constraints silently disappear
    # because the key no longer matches. Normalizing here means an unmapped
    # key is impossible rather than merely unlikely.
    candy_inventory = _as_pool_keyed(candy_inventory)
    xl_candy_inventory = _as_pool_keyed(xl_candy_inventory)

    prob = pulp.LpProblem("Pokemon_PowerUp_Allocation", pulp.LpMaximize)

    # x[(instance_id, target_level)] = 1 if we power this Pokemon up to exactly
    # that level.
    x: dict[tuple[str, float], pulp.LpVariable] = {}
    meta: dict[tuple[str, float], tuple[PokemonInstance, int, int, int, float]] = {}

    for p in collection:
        base_score = rating(p, p.level, w)
        for target in _candidate_levels(p.level, max_steps_per_pokemon):
            dust, candy, xl = cumulative_cost(p.level, target, p.friendship)
            gain = rating(p, target, w) - base_score
            if gain <= 0:
                continue
            key = (p.instance_id, target)
            x[key] = pulp.LpVariable(
                f"x_{p.instance_id}_{str(target).replace('.', '_')}", cat="Binary"
            )
            meta[key] = (p, dust, candy, xl, gain)

    if not x:
        return Result("NoCandidates", [], 0.0, 0, stardust_budget)

    # Objective: total rating improvement bought.
    prob += pulp.lpSum(x[k] * meta[k][4] for k in x), "Total_Rating_Gain"

    # At most one target level per Pokemon.
    by_instance: dict[str, list[tuple[str, float]]] = {}
    for k in x:
        by_instance.setdefault(k[0], []).append(k)
    for inst, keys in by_instance.items():
        prob += pulp.lpSum(x[k] for k in keys) <= 1, f"One_Target_{inst}"

    # Shared stardust budget.
    prob += (
        pulp.lpSum(x[k] * meta[k][1] for k in x) <= stardust_budget,
        "Stardust_Budget",
    )

    # Candy is pooled per evolution FAMILY, not per species. A Gible and a
    # Garchomp spend from one pile, and the game says so on screen -- a
    # Garchomp's detail view reads "GIBLE CANDY".
    #
    # Grouping by species_id instead gave each its own constraint against the
    # same stock: with 20 Gible candy the plan spent 20 on the Gible AND 20 on
    # the Garchomp, then reported itself fully costed. Twice the candy that
    # exists, presented as affordable. Any collection holding a pre-evolution
    # alongside its evolution is affected, which is most of them.
    by_pool: dict[object, list[tuple[str, float]]] = {}
    for k in x:
        by_pool.setdefault(candy_pool_key(meta[k][0].species_id), []).append(k)

    for pool, keys in by_pool.items():
        label = str(pool).replace(" ", "_")
        if pool in candy_inventory:
            prob += (
                pulp.lpSum(x[k] * meta[k][2] for k in keys) <= candy_inventory[pool],
                f"Candy_Species_{label}",
            )
        # XL candy is a distinct resource with its own stock. Constraining it
        # against the regular candy count (as this did) let a species with 180
        # candy "afford" 180 XL, which is roughly 18,000 candy of buying power.
        if pool in xl_candy_inventory:
            prob += (
                pulp.lpSum(x[k] * meta[k][3] for k in keys) <= xl_candy_inventory[pool],
                f"XLCandy_Species_{label}",
            )

    # Optional cap on mega-capable Pokemon in the plan.
    #
    # OFF BY DEFAULT, deliberately. The original hard-coded `<= 1` here, which
    # reads as "only one mega at a time" -- but mega evolution is paid for with
    # mega energy, a separate per-species resource that has nothing to do with
    # the stardust being allocated. In the sample collection 14 of 24 Pokemon
    # are mega-capable, so that constraint was silently excluding more than half
    # the collection from ever being chosen. Keep the knob for anyone who wants
    # to concentrate investment, but it is not a real resource constraint.
    if max_megas is not None:
        mega_keys = [k for k in x if meta[k][0].species_id in MEGA_SPECIES]
        if mega_keys:
            prob += pulp.lpSum(x[k] for k in mega_keys) <= max_megas, "Mega_Cap"

    # Optional cap on how many Pokemon to touch at once.
    if max_pokemon is not None:
        prob += pulp.lpSum(x.values()) <= max_pokemon, "Max_Pokemon"

    prob.solve(pulp.PULP_CBC_CMD(msg=1 if verbose else 0))
    status = pulp.LpStatus[prob.status]

    selections: list[Selection] = []
    for k, var in x.items():
        if var.value() is not None and var.value() > 0.5:
            p, dust, candy, xl, gain = meta[k]
            selections.append(Selection(p, k[1], dust, candy, xl, gain))

    selections.sort(key=lambda s: s.gain, reverse=True)

    # Which species is the plan spending candy on without anyone having said how
    # much candy exists? Those species got no constraint at all, so the solver
    # treated their candy as unlimited.
    #
    # The guard above rejects a malformed inventory; this reports an ABSENT one,
    # which is the more dangerous case precisely because it looks like success.
    # A Poke Genie export carries no candy counts at all, so importing one and
    # solving produces a plan constrained only by stardust -- correct as far as
    # it goes, and potentially impossible to actually carry out.
    unbacked: dict = {}
    for s in selections:
        pool = candy_pool_key(s.pokemon.species_id)
        need_candy = s.candy if pool not in candy_inventory else 0
        need_xl = s.xl_candy if pool not in xl_candy_inventory else 0
        if need_candy or need_xl:
            have = unbacked.get(pool, (0, 0))
            unbacked[pool] = (have[0] + need_candy, have[1] + need_xl)

    return Result(
        status=status,
        selections=selections,
        total_gain=sum(s.gain for s in selections),
        stardust_used=sum(s.stardust for s in selections),
        stardust_budget=stardust_budget,
        shadow_prices=shadow_prices(prob) if with_shadow_prices else None,
        unbacked_candy=unbacked,
    )
