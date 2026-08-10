"""Tests for swipe-through resolution: level inference, segmentation, voting."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pogo_opt.resolve import (
    Run, cp_at, hp_at, iv_floor_for, levels_from_cp, resolve_run,
    segment_runs, star_tier, vote_continuous, vote_discrete,
)

CHARIZARD = (223, 173, 186)
BLISSEY = (129, 169, 496)
BASE = {"Charizard": CHARIZARD, "Blissey": BLISSEY}


# ------------------------------------------------------------- level solve

def test_cp_matches_published_values():
    assert cp_at(*CHARIZARD, 15, 15, 15, 40.0) == 2889
    assert cp_at(*BLISSEY, 15, 15, 15, 40.0) == 2757


def test_level_recovered_exactly():
    """The whole point: CP + known IVs pins the level."""
    for level in (12.5, 25.0, 31.5, 40.0, 50.0):
        cp = cp_at(*CHARIZARD, 10, 13, 7, level)
        hp = hp_at(CHARIZARD[2], 7, level)
        assert levels_from_cp(*CHARIZARD, 10, 13, 7, cp, hp) == [level]


def test_level_solve_round_trips_across_iv_spreads():
    for ivs in [(0, 0, 0), (15, 15, 15), (2, 14, 9), (15, 0, 15)]:
        cp = cp_at(*CHARIZARD, *ivs, 33.5)
        hp = hp_at(CHARIZARD[2], ivs[2], 33.5)
        assert 33.5 in levels_from_cp(*CHARIZARD, *ivs, cp, hp)


def test_hp_narrows_an_ambiguous_cp():
    """CP is floored, so low levels can collapse. HP breaks most ties."""
    cp = cp_at(*CHARIZARD, 5, 5, 5, 2.0)
    without_hp = levels_from_cp(*CHARIZARD, 5, 5, 5, cp)
    with_hp = levels_from_cp(*CHARIZARD, 5, 5, 5, cp, hp_at(CHARIZARD[2], 5, 2.0))
    assert len(with_hp) <= len(without_hp)
    assert 2.0 in with_hp


def test_impossible_cp_yields_no_level():
    assert levels_from_cp(*CHARIZARD, 15, 15, 15, 99999) == []


# ------------------------------------------------------------- star tiers

@pytest.mark.parametrize("ivs,tier", [
    ((15, 15, 15), 4), ((15, 15, 13), 3), ((13, 12, 11), 2),
    ((10, 8, 7), 1), ((0, 0, 0), 0),
])
def test_star_tier_matches_in_game_search(ivs, tier):
    assert star_tier(*ivs) == tier


def test_lucky_and_purified_iv_floors():
    assert iv_floor_for(is_lucky=True) == 12
    assert iv_floor_for(is_purified=True) == 2
    assert iv_floor_for() == 0


# ------------------------------------------------------------ segmentation

def test_segments_split_on_swipe_spikes():
    """Low diffs are settled frames; spikes are the swipe animation."""
    diffs = [0] + [1.0] * 30 + [40.0] + [1.0] * 30 + [40.0] + [1.0] * 30
    runs = segment_runs(diffs, threshold=8.0, min_run=5)
    assert len(runs) == 3
    assert all(r.length >= 25 for r in runs)


def test_short_runs_are_discarded():
    """Mid-swipe frames must not become their own Pokemon."""
    diffs = [0] + [1.0] * 20 + [40.0, 39.0] + [1.0] * 20
    runs = segment_runs(diffs, threshold=8.0, min_run=5)
    assert all(r.length >= 5 for r in runs)


def test_empty_input_is_safe():
    assert segment_runs([]) == []


def test_sampling_avoids_run_edges():
    """Frames right after a swipe are still animating."""
    picks = Run(0, 89).sample(5)
    assert len(picks) == 5
    assert picks[0] > 0 and picks[-1] < 89
    assert picks == sorted(picks)


def test_sampling_a_short_run_returns_everything():
    assert Run(10, 13).sample(8) == [10, 11, 12, 13]


# ------------------------------------------------------------------ voting

def test_majority_outvotes_a_bad_frame():
    """One frame reading 245l instead of 2451 must not win."""
    reads = [2451] * 89 + [2451 - 2000]
    v = vote_discrete(reads)
    assert v.value == 2451 and v.is_confident


def test_vote_ignores_missing_reads():
    v = vote_discrete([None, "Charizard", None, "Charizard"])
    assert v.value == "Charizard" and v.n == 2


def test_vote_returns_none_when_nothing_was_read():
    assert vote_discrete([None, None]) is None
    assert vote_continuous([None]) is None


def test_continuous_vote_tolerates_antialiasing():
    """Bar fills never repeat exactly; exact-match voting would report 0%."""
    v = vote_continuous([0.667, 0.669, 0.666, 0.671, 0.668])
    assert v.value == pytest.approx(0.668, abs=0.005)
    assert v.agreement == 1.0


def test_continuous_vote_flags_a_drifting_bar():
    v = vote_continuous([0.20, 0.55, 0.90, 0.31, 0.77])
    assert v.agreement < 0.6


# ------------------------------------------------------------- end to end

def _frames(n, **over):
    base = dict(name="Charizard", cp=cp_at(*CHARIZARD, 15, 10, 7, 25.0),
                hp=hp_at(CHARIZARD[2], 7, 25.0),
                attack_fill=15 / 15, defense_fill=10 / 15, stamina_fill=7 / 15)
    base.update(over)
    return [dict(base) for _ in range(n)]


def test_run_resolves_to_a_complete_record():
    rec = resolve_run(_frames(90), BASE)
    assert rec.name == "Charizard"
    assert (rec.attack_iv, rec.defense_iv, rec.stamina_iv) == (15, 10, 7)
    assert rec.level == 25.0
    assert rec.star_tier == 2   # 32/45 = 71% -> 2 stars
    assert rec.is_complete
    assert rec.warnings == []


def test_noisy_frames_still_resolve():
    reads = _frames(88)
    reads += [dict(reads[0], cp=11, name=None, attack_fill=0.1)]
    reads += [dict(reads[0], cp=99999)]
    rec = resolve_run(reads, BASE)
    assert rec.name == "Charizard" and rec.level == 25.0
    assert rec.attack_iv == 15


def test_lucky_floor_lifts_a_low_bar_read():
    """Lucky trades guarantee 12+, so a bar reading below that was misread."""
    reads = _frames(30, defense_fill=0.05)
    rec = resolve_run(reads, BASE, is_lucky=True)
    assert rec.defense_iv >= 12


def test_empty_run_is_flagged_not_crashed():
    rec = resolve_run([], BASE)
    assert not rec.is_complete
    assert "no frames in run" in rec.warnings


def test_unknown_species_leaves_level_unsolved():
    rec = resolve_run(_frames(20, name="Missingno"), BASE)
    assert rec.level is None
    assert rec.attack_iv == 15
