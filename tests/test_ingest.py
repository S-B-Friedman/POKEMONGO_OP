"""Tests for screenshot parsing.

These run with no images, no Tesseract, no Vision credentials, and no network,
which is the whole point of keeping the parsing pure.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pogo_opt.ingest import (
    BarLayout,
    ScannedPokemon,
    build_record,
    extract_fields,
    iv_confidence,
    iv_from_bar_fill,
    match_species_name,
)

NAMES = ["Charizard", "Blastoise", "Machamp", "Tyranitar", "Metagross", "Chansey"]

CLEAN = """
CP2451
Charizard
150/150 HP
POWER UP
10000
Stardust
25 Charizard Candy
WEIGHT 96.4kg   HEIGHT 1.72m
Caught on 4/18/2023
Caught around Hackensack, New Jersey, US
"""


def test_extracts_core_fields():
    f = extract_fields(CLEAN)
    assert f["cp"] == 2451
    assert f["current_hp"] == 150
    assert f["total_hp"] == 150
    assert f["power_up_stardust"] == 10000
    assert f["candy_count"] == 25


def test_extracts_catch_metadata():
    f = extract_fields(CLEAN)
    assert f["date_caught"] == "4/18/2023"
    assert f["city"] == "Hackensack"
    assert f["region"] == "New Jersey"


def test_missing_fields_are_absent_not_zero():
    """A Pokemon with no visible stardust cost differs from one costing zero."""
    f = extract_fields("CP1200\nMachamp")
    assert f["cp"] == 1200
    assert "power_up_stardust" not in f
    assert "candy_count" not in f


def test_empty_text_is_safe():
    assert extract_fields("") == {}
    assert extract_fields(None) == {}


def test_digit_confusions_are_corrected():
    """OCR reads O for 0 and l for 1 constantly."""
    assert extract_fields("CP245l")["cp"] == 2451
    assert extract_fields("CP1OO")["cp"] == 100


def test_comma_separated_stardust():
    assert extract_fields("10,000 Stardust")["power_up_stardust"] == 10000


def test_impossible_hp_is_rejected():
    """Current above total is a misread, not a real state."""
    f = extract_fields("300/150 HP")
    assert "current_hp" not in f


def test_status_flags():
    assert extract_fields("Shadow Machamp")["is_shadow"] is True
    assert extract_fields("Lucky Chansey")["is_lucky"] is True
    assert extract_fields("CP100 Machamp")["is_shadow"] is False


# --------------------------------------------------------------------------
# Name matching
# --------------------------------------------------------------------------

def test_exact_name_match():
    assert match_species_name("CP2451 CHARIZARD", NAMES) == "Charizard"


def test_fuzzy_match_survives_a_misread():
    """The original required an exact substring, so one bad character meant
    the Pokemon was simply never identified."""
    assert match_species_name("CP2451 CHARLZARD", NAMES) == "Charizard"


def test_no_match_returns_none():
    assert match_species_name("CP2451 QQQQQQQQ", NAMES) is None


def test_empty_inputs_return_none():
    assert match_species_name("", NAMES) is None
    assert match_species_name("CHARIZARD", []) is None


# --------------------------------------------------------------------------
# IV estimation
# --------------------------------------------------------------------------

def test_iv_endpoints():
    assert iv_from_bar_fill(0.0) == 0
    assert iv_from_bar_fill(1.0) == 15


def test_iv_is_monotone():
    vals = [iv_from_bar_fill(i / 100) for i in range(101)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))


def test_iv_rejects_out_of_range():
    with pytest.raises(ValueError):
        iv_from_bar_fill(1.5)
    with pytest.raises(ValueError):
        iv_from_bar_fill(-0.1)


def test_confidence_peaks_on_legal_values():
    assert iv_confidence(1.0) == pytest.approx(1.0)
    assert iv_confidence(10 / 15) == pytest.approx(1.0)


def test_confidence_bottoms_out_between_values():
    """Halfway between two IVs usually means a wrong crop region."""
    assert iv_confidence(0.5 / 15) == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------
# Bar layout -- fractions, not hardcoded pixel rows
# --------------------------------------------------------------------------

def test_regions_scale_with_resolution():
    layout = BarLayout()
    small = layout.regions(1920, 1080)
    large = layout.regions(3840, 2160)
    assert large["attack"][0] == pytest.approx(small["attack"][0] * 2, rel=0.01)


def test_regions_stay_inside_the_image():
    layout = BarLayout()
    for h, w in ((1920, 1080), (2340, 1080), (2778, 1284)):
        for _, (y0, y1, x0, x1) in layout.regions(h, w).items():
            assert 0 <= y0 < y1 <= h
            assert 0 <= x0 < x1 <= w


def test_bars_do_not_overlap():
    regions = BarLayout().regions(1920, 1080)
    tops = sorted(r[0] for r in regions.values())
    heights = [r[1] - r[0] for r in regions.values()]
    assert all(b - a > max(heights) for a, b in zip(tops, tops[1:]))


# --------------------------------------------------------------------------
# Assembled record
# --------------------------------------------------------------------------

def test_build_record_populates_and_flags():
    rec = build_record(CLEAN, NAMES, {"attack": 1.0, "defense": 1.0, "hp": 1.0})
    assert rec.name == "Charizard"
    assert rec.cp == 2451
    assert (rec.attack_iv, rec.defense_iv, rec.stamina_iv) == (15, 15, 15)
    assert rec.is_usable
    assert rec.warnings == []


def test_unusable_record_is_flagged_not_dropped():
    rec = build_record("some unreadable noise", NAMES)
    assert not rec.is_usable
    assert "species name not recognized" in rec.warnings
    assert "CP not found" in rec.warnings


def test_low_confidence_bar_read_warns():
    rec = build_record(CLEAN, NAMES, {"attack": 0.5 / 15, "defense": 1.0, "hp": 1.0})
    assert rec.iv_confidence < 0.5
    assert any("low IV read confidence" in w for w in rec.warnings)


def test_to_row_is_csv_safe():
    rec = build_record("noise", NAMES)
    row = rec.to_row()
    assert isinstance(row["warnings"], str)
    assert all(not isinstance(v, (list, dict)) for v in row.values())


# --------------------------------------------------------------------------
# Voting across the frames that show one Pokemon.
#
# These import from ocr_ingest, which needs no image libraries for the voting
# itself -- only group_consecutive touches OpenCV, and that is covered in
# test_ocr_pipeline.py where the extras are guaranteed.

from ocr_ingest import vote_records  # noqa: E402


def _rec(**kw):
    return ScannedPokemon(**kw)


def test_duplicates_are_not_collapsed():
    """The bug this replaced: three Machamps at one CP became one Machamp.

    Identity-based collapsing keyed on (name, cp) and could not tell three
    Pokemon apart from three readings of one. A box of 1,500 is full of exactly
    that, and two of the three vanished with nothing logged. Frames are grouped
    in time now, so each swipe is its own Pokemon and votes on its own.
    """
    spreads = [(15, 10, 7), (2, 3, 4), (11, 12, 13)]
    voted = [
        vote_records([_rec(name="Machamp", cp=2451, attack_iv=a,
                           defense_iv=d, stamina_iv=s)])
        for a, d, s in spreads
    ]
    assert [(v.attack_iv, v.defense_iv, v.stamina_iv) for v in voted] == spreads


def test_a_single_bad_frame_is_outvoted():
    """Three good frames and one mangled one. The old code kept whichever
    record carried the fewest warnings, which a confident misread also has."""
    good = dict(name="Pikachu", cp=292, attack_iv=15, defense_iv=14, stamina_iv=14)
    voted = vote_records([
        _rec(**good),
        _rec(**good),
        _rec(name="Pikachu", cp=992, attack_iv=1, defense_iv=1, stamina_iv=1),
        _rec(**good),
    ])
    assert voted.cp == 292
    assert (voted.attack_iv, voted.defense_iv, voted.stamina_iv) == (15, 14, 14)


def test_fields_are_voted_independently():
    """OCR does not fail on a whole frame at once. A name read on only one
    frame still beats silence, and it should not drag that frame's CP along."""
    voted = vote_records([
        _rec(name=None, cp=313),
        _rec(name="Anorith", cp=313),
        _rec(name=None, cp=313),
    ])
    assert voted.name == "Anorith"
    assert voted.cp == 313


def test_ivs_are_voted_as_one_spread():
    """Never per stat. The three bars are read off one image, so a frame caught
    mid-animation is wrong about all three together -- pairing an attack from
    one frame with a defense from another invents a spread nothing showed."""
    voted = vote_records([
        _rec(name="X", cp=1, attack_iv=15, defense_iv=0, stamina_iv=0),
        _rec(name="X", cp=1, attack_iv=15, defense_iv=0, stamina_iv=0),
        _rec(name="X", cp=1, attack_iv=0, defense_iv=15, stamina_iv=0),
    ])
    assert (voted.attack_iv, voted.defense_iv, voted.stamina_iv) == (15, 0, 0)


def test_nothing_agreed_is_said_out_loud():
    voted = vote_records([_rec(name=None, cp=None), _rec(name=None, cp=None)])
    assert not voted.is_usable
    assert any("no species name agreed" in w for w in voted.warnings)
    assert any("no CP agreed" in w for w in voted.warnings)


def test_a_standing_warning_survives_the_vote():
    """Regression, and the worst run this project has produced.

    vote_records built a fresh record and kept none of the per-frame warnings,
    so a real 50-second capture came back as "16 records (0 flagged for review)"
    with the IVs read off the wrong part of the screen. Every single frame had
    said "low IV read confidence -- check bar crop region"; the consensus record
    said nothing. A vote can settle a disagreement, but it cannot fix a
    condition every frame agrees on, and must not hide it.
    """
    def warned(**kw):
        r = ScannedPokemon(name="Palkia", cp=4627, attack_iv=15,
                           defense_iv=15, stamina_iv=15, **kw)
        r.warnings.append("low IV read confidence (0.497) -- check bar crop region")
        return r

    voted = vote_records([warned() for _ in range(6)])
    assert any("check bar crop region" in w for w in voted.warnings)
    assert not voted.is_usable or voted.warnings


def test_a_one_off_warning_does_not_survive_the_vote():
    """The other half: a warning from a single outvoted frame describes a
    reading that is no longer being reported, so repeating it is noise."""
    clean = [ScannedPokemon(name="Palkia", cp=4627, attack_iv=15,
                            defense_iv=15, stamina_iv=15) for _ in range(5)]
    bad = ScannedPokemon(name="Palkia", cp=None, attack_iv=15,
                         defense_iv=15, stamina_iv=15)
    bad.warnings.append("CP not found")

    voted = vote_records(clean + [bad])
    assert voted.cp == 4627
    assert voted.warnings == []


# --------------------------------------------------------------------------
# Refereeing a voted record against the arithmetic.

from ocr_ingest import verify_record  # noqa: E402


def test_an_impossible_cp_is_removed_not_reported():
    """From a real capture: a Dragonite at CP 7 beside a good IV spread.

    cp_candidates() and verify_cp() both existed and were tested, and the scan
    driver called neither -- the module docstring described a propose-and-verify
    pipeline that was never wired up. Whatever the regex scraped off the text
    went straight to the CSV.
    """
    rec = ScannedPokemon(name="Dragonite", cp=7, total_hp=184,
                         attack_iv=15, defense_iv=14, stamina_iv=15)
    verify_record(rec, [7])
    assert rec.cp is None
    assert any("discarded" in w for w in rec.warnings)


def test_a_correct_cp_survives_verification():
    rec = ScannedPokemon(name="Garchomp", cp=4365, total_hp=208,
                         attack_iv=15, defense_iv=15, stamina_iv=11)
    verify_record(rec, [4365])
    assert rec.cp == 4365
    assert rec.warnings == []


def test_an_alternate_form_is_not_rejected_as_impossible():
    """Regression caught before shipping, and the dangerous direction.

    A screenshot says "Palkia" for both the base species and the Origin Forme.
    At level 49 with perfect IVs those are CP 4458 and CP 4627, so checking the
    base form alone rejects a completely correct reading of the other -- 4627
    with HP 170, which resolves exactly. Deleting good data is worse than the
    unchecked CP this verification exists to catch.
    """
    rec = ScannedPokemon(name="Palkia", cp=4627, total_hp=170,
                         attack_iv=15, defense_iv=15, stamina_iv=15)
    verify_record(rec, [4627])
    assert rec.cp == 4627
    assert rec.warnings == []


def test_verification_declines_without_the_numbers_it_needs():
    """No HP means no second equation, so there is nothing to check against.
    Leaving the CP alone is right; inventing a verdict would not be."""
    rec = ScannedPokemon(name="Dragonite", cp=7, total_hp=None,
                         attack_iv=15, defense_iv=14, stamina_iv=15)
    verify_record(rec, [7])
    assert rec.cp == 7
