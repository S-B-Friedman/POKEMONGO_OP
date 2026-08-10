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
    assert p.friendship == "lucky"
    assert cumulative_cost(20.0, 25.0, p.friendship)[0] < cumulative_cost(20.0, 25.0)[0]


def test_instance_ids_are_unique_across_duplicates(ref):
    r = import_rows([row(), row(), row()], HEADERS, ref)
    assert len({p.instance_id for p in r.collection}) == 3
