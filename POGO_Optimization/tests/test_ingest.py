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
