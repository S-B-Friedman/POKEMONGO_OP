"""Conformance against GAME_MASTER, and Poke Genie import.

The cost tests here are the ones that would have caught the XL boundary being
wrong. The previous version of that test asserted the repo's own belief
(`test_xl_candy_starts_at_forty_one`) rather than checking it against anything,
so it passed while being wrong. These compare against committed GAME_MASTER
data instead.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pogo_opt.costs import XL_CANDY_THRESHOLD, cumulative_cost, step_cost
from pogo_opt.importers.pokegenie import import_rows, map_columns, resolve_level, _cp_at
from pogo_opt.reference import REFERENCE_PATH, Reference, default_reference, normalize

pytestmark = pytest.mark.skipif(
    not REFERENCE_PATH.exists(),
    reason="run scripts/build_reference.py first",
)


@pytest.fixture(scope="module")
def costs():
    return json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["costs"]


@pytest.fixture(scope="module")
def ref():
    return default_reference()


# ------------------------------------------------------- cost conformance

def test_xl_threshold_matches_game_master(costs):
    assert XL_CANDY_THRESHOLD == float(costs["xl_candy_min_pokemon_level"])


# The multipliers below were correct but unpinned: nothing tied the literals in
# costs.py and model.py to the data they came from, which is precisely the state
# the XL candy boundary was in when it was found to be a level high.

@pytest.mark.parametrize("state,dust_key,candy_key", [
    ("shadow", "shadow_stardust_multiplier", "shadow_candy_multiplier"),
    ("purified", "purified_stardust_multiplier", "purified_candy_multiplier"),
])
def test_cost_multipliers_match_game_master(state, dust_key, candy_key, costs):
    from pogo_opt.costs import _STATE_MULTIPLIERS

    assert _STATE_MULTIPLIERS[state] == (
        pytest.approx(costs[dust_key]),
        pytest.approx(costs[candy_key]),
    )


def test_lucky_multiplier_is_not_in_game_master(costs):
    """Documents why lucky cannot be pinned like the other two.

    GAME_MASTER carries no lucky cost multiplier. The half-stardust discount
    rests on observation, so if this ever starts failing, upstream has begun
    shipping the value and it should be read from there instead.
    """
    assert not [k for k in costs if "lucky" in k.lower()]


@pytest.mark.parametrize("const,key", [
    ("SHADOW_ATTACK_MULT", "shadow_attack_multiplier"),
    ("SHADOW_DEFENSE_MULT", "shadow_defense_multiplier"),
    ("STAB_BONUS", "same_type_attack_bonus_multiplier"),
])
def test_combat_multipliers_match_game_master(const, key):
    import json

    from pogo_opt import model

    combat = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["combat"]
    # SHADOW_DEFENSE_MULT is written 5/6; GAME_MASTER ships the rounded
    # 0.8333333. They agree to 3e-8, which is far below anything that can move
    # a rating, so the tolerance is deliberate rather than sloppy.
    assert getattr(model, const) == pytest.approx(combat[key], abs=1e-6)


@pytest.mark.parametrize("level", range(1, 50))
def test_step_cost_matches_game_master(level, costs):
    """Every level, all three resources. No sampled subset, because the bug
    that shipped lived at exactly the levels a hand-picked sample skipped."""
    xl_min = costs["xl_candy_min_pokemon_level"]
    want = (
        costs["stardust_cost"][level - 1],
        costs["candy_cost"][level - 1],
        costs["xl_candy_cost"][level - xl_min] if level >= xl_min else 0,
    )
    assert step_cost(float(level)) == want


def test_first_xl_step_costs_no_regular_candy():
    """Level 40 -> 40.5 is XL, not 15 regular candy."""
    dust, candy, xl = step_cost(40.0)
    assert (candy, xl) == (0, 10)


def test_walk_across_the_boundary_splits_resources():
    _, candy, xl = cumulative_cost(39.0, 41.0)
    assert candy == 30   # 39.0 and 39.5, at 15 each
    assert xl == 20      # 40.0 and 40.5, at 10 each


def test_cpm_anchors_match_game_master(costs):
    from pogo_opt.costs import cp_multiplier
    for level in (1, 20, 40, 50):
        assert cp_multiplier(level) == pytest.approx(
            costs["cp_multiplier"][level - 1], abs=1e-6
        )


# ------------------------------------------------------------- reference

def test_normalize_collapses_source_spellings():
    assert normalize("Dragon Tail") == normalize("DRAGON_TAIL") == "dragontail"


def test_known_base_stats(ref):
    c = ref.species("Charizard")
    assert (c.base_attack, c.base_defense, c.base_stamina) == (223, 173, 186)
    assert (c.type1, c.type2) == ("fire", "flying")


def test_form_beats_base_species(ref):
    assert ref.species("Exeggutor", "Alola").type2 == "dragon"
    assert ref.species("Exeggutor").type2 == "psychic"


def test_move_kind_is_enforced(ref):
    assert ref.move("Counter", kind="fast") is not None
    assert ref.move("Counter", kind="charge") is None


def test_charge_energy_is_a_magnitude(ref):
    """GAME_MASTER stores charge energy as a negative delta. Passing that
    through signed would make ceil(energy / fast_energy) negative and every
    charge move look free."""
    assert ref.move("Outrage").energy > 0


def test_unknown_names_return_none(ref):
    assert ref.species("Missingno") is None
    assert ref.move("Hyperbeam Deluxe") is None


# ---------------------------------------------------------------- import

HEADERS = ["Name", "Form", "CP", "HP", "Atk IV", "Def IV", "Sta IV",
           "Level Min", "Level Max", "Quick Move", "Charge Move",
           "Lucky", "Shadow/Purified"]


def row(**over):
    base = {
        "Name": "Charizard", "Form": "", "CP": "", "HP": "",
        "Atk IV": "15", "Def IV": "15", "Sta IV": "15",
        "Level Min": "40", "Level Max": "40",
        "Quick Move": "Fire Spin", "Charge Move": "Blast Burn",
        "Lucky": "0", "Shadow/Purified": "",
    }
    base.update(over)
    return base


def test_columns_map_by_alias_not_exact_header():
    mapping, missing = map_columns(HEADERS)
    assert not missing
    assert mapping["attack_iv"] == "Atk IV"
    assert mapping["fast_move"] == "Quick Move"


def test_level_min_max_counts_as_a_level():
    _, missing = map_columns([h for h in HEADERS if h != "Level Min"])
    assert missing == ["level (or level_min + level_max)"]


def test_clean_row_imports(ref):
    r = import_rows([row()], HEADERS, ref)
    assert len(r.collection) == 1
    p = r.collection[0]
    assert (p.species_id, p.level) == (6, 40.0)
    assert p.fast_energy > 0 and p.charge_energy > 0


def test_cp_narrows_an_ambiguous_level_range(ref):
    sp = ref.species("Dragonite")
    cp = _cp_at(sp, [15, 15, 14], 35.5)
    r = import_rows(
        [row(Name="Dragonite", CP=str(cp), **{"Sta IV": "14",
             "Level Min": "34", "Level Max": "37",
             "Quick Move": "Dragon Breath", "Charge Move": "Draco Meteor"})],
        HEADERS, ref,
    )
    assert r.collection[0].level == 35.5
    assert not r.warnings


def test_unresolvable_level_warns_but_still_imports(ref):
    r = import_rows([row(CP="999999", **{"Level Min": "20", "Level Max": "25"})],
                    HEADERS, ref)
    assert len(r.collection) == 1
    assert r.warnings and "reproduces CP" in r.warnings[0].reason


def test_unknown_species_is_reported_not_dropped_silently(ref):
    r = import_rows([row(Name="Missingno")], HEADERS, ref)
    assert not r.collection
    assert "not in reference data" in r.skipped[0].reason


def test_missing_ivs_are_skipped_not_zeroed(ref):
    """An unappraised scan has no IVs. Defaulting them to 0 would produce a
    confident plan built on invented data."""
    r = import_rows([row(**{"Atk IV": ""})], HEADERS, ref)
    assert not r.collection
    assert "missing IVs" in r.skipped[0].reason


def test_shadow_and_purified_are_distinguished(ref):
    shadow = import_rows([row(**{"Shadow/Purified": "Shadow"})], HEADERS, ref).collection[0]
    purified = import_rows([row(**{"Shadow/Purified": "Purified"})], HEADERS, ref).collection[0]
    assert shadow.is_shadow and not shadow.is_purified
    assert purified.is_purified and not purified.is_shadow


def test_lucky_flag_reaches_the_cost_curve(ref):
    p = import_rows([row(Lucky="1")], HEADERS, ref).collection[0]
    assert p.friendship == frozenset({"lucky"})
    assert cumulative_cost(20.0, 25.0, p.friendship)[0] < cumulative_cost(20.0, 25.0)[0]


def test_instance_ids_are_unique_across_duplicates(ref):
    r = import_rows([row(), row(), row()], HEADERS, ref)
    assert len({p.instance_id for p in r.collection}) == 3


# ------------------------------------------------- unrecognized movesets

def test_unknown_moveset_falls_back_instead_of_dropping_the_row(ref):
    """Regression: the row was dropped while a comment claimed it was kept.

    Base stats, IVs and level are all known and correct for such a row; only
    the rating scale is uncertain. Dropping it means the Pokemon can never be
    recommended, and quietly shrinks the collection the caller imported.
    """
    r = import_rows([row(**{"Quick Move": "NoSuchMove"})], HEADERS, ref)

    assert len(r.collection) == 1, "row must survive an unrecognized move"
    assert not r.skipped
    assert r.rows_read == len(r.collection)

    p = r.collection[0]
    assert p.fast_move.endswith("?"), "the placeholder must be visible in the name"
    assert p.fast_power > 0 and p.fast_energy > 0
    assert any("moveset not recognized" in w.reason for w in r.warnings)


def test_both_moves_unknown_still_imports(ref):
    r = import_rows(
        [row(**{"Quick Move": "Nope", "Charge Move": "AlsoNope"})], HEADERS, ref
    )
    assert len(r.collection) == 1
    p = r.collection[0]
    assert p.fast_move.endswith("?") and p.charge_move.endswith("?")
    reason = r.warnings[0].reason
    assert "Nope" in reason and "AlsoNope" in reason


def test_a_recognized_moveset_is_not_tagged(ref):
    p = import_rows([row()], HEADERS, ref).collection[0]
    assert not p.fast_move.endswith("?")
    assert not p.charge_move.endswith("?")


def test_row_count_is_conserved_unless_genuinely_unusable(ref):
    """Everything recoverable is recovered; only real losses are skipped."""
    rows = [
        row(),
        row(**{"Quick Move": "NoSuchMove"}),          # recoverable
        row(**{"Atk IV": ""}),                        # not: unappraised
    ]
    r = import_rows(rows, HEADERS, ref)
    assert r.rows_read == 3
    assert len(r.collection) == 2
    assert len(r.skipped) == 1
    assert "IV" in r.skipped[0].reason


# --------------------------------------------------- evolution families

def test_species_carry_their_evolution_family(ref):
    """Candy is pooled per family, and the game labels it with the base form.

    A Garchomp's detail screen reads "GIBLE CANDY". Matching that label against
    the species name fails for every evolved Pokemon there is, so the family is
    what has to be carried.
    """
    assert ref.species("Garchomp").family == "Gible"
    assert ref.species("Charizard").family == "Charmander"
    assert ref.species("Metagross").family == "Beldum"


def test_a_base_form_is_its_own_family(ref):
    for name in ("Palkia", "Swinub", "Anorith"):
        assert ref.species(name).family == name


def test_one_family_spans_the_whole_evolution_line(ref):
    """Gible, Gabite and Garchomp draw on a single candy pool."""
    line = [ref.species(n) for n in ("Gible", "Gabite", "Garchomp")]
    assert {s.family for s in line} == {"Gible"}
    assert len({s.dex for s in line}) == 3, "distinct species, shared candy"
