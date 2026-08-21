"""Reading IVs off the appraisal bars.

Calibrated against a real 1206x2622 screen recording. The recording itself is
personal data and is not in the repo, so these tests rebuild the same geometry
synthetically: bar rows at the measured fractions, three segments with gaps at
the measured widths, filled to a known IV. Everything asserted here was first
confirmed against the real capture, where the extracted IVs reproduced both the
displayed CP and the displayed HP at exactly one level -- twice.

The three failures these guard against all produce a plausible-looking number
rather than an error, which is why they survived so long. In increasing order
of damage:

  1. Counting the gaps between segments as unfilled. Worth 3.3% here, about
     half an IV -- small, but one-directional and worst at the top of the
     range, where it is the difference between a 15 and a 14.
  2. A bar span that is too wide. The old default ran to 0.945 of the image
     width, roughly twice the bar, and read 11 where the truth was 14.
  3. Detecting only orange. A maxed stat is drawn red, so an orange-only
     reader scores a perfect stat as zero -- turning the best Pokemon in a
     collection into the worst.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pogo_opt.ingest import (
    IV_BAR_SEGMENTS,
    BarLayout,
    bar_reading,
    contiguous_runs,
    find_bar_cluster,
    iv_from_bar_fill,
)

# Measured from the real capture, in pixels at 1206 wide.
SEG_WIDTH = 136
GAP = 7
TRACK_LEFT = 142


def make_bar(iv: int, *, width: int = SEG_WIDTH, gap: int = GAP,
             left: int = TRACK_LEFT, total: int = 1206):
    """Build (fill, track) scanlines for a bar showing `iv` out of 15."""
    fill = [False] * total
    track = [False] * total
    filled_px = round(iv / 15 * (width * IV_BAR_SEGMENTS))
    x = left
    remaining = filled_px
    for _ in range(IV_BAR_SEGMENTS):
        for i in range(width):
            track[x + i] = True
            if remaining > 0:
                fill[x + i] = True
                remaining -= 1
        x += width + gap
    return fill, track


# --------------------------------------------------------------- primitives

def test_contiguous_runs_bridges_antialiasing_but_not_gaps():
    flags = [False] * 100
    for i in range(10, 40):
        flags[i] = True
    flags[25] = False          # a single-pixel dropout inside one run
    for i in range(60, 90):
        flags[i] = True
    assert contiguous_runs(flags) == [(10, 39), (60, 89)]


def test_contiguous_runs_drops_specks():
    flags = [False] * 100
    flags[5] = flags[6] = True             # too short to be a segment
    for i in range(40, 80):
        flags[i] = True
    assert contiguous_runs(flags) == [(40, 79)]


# ------------------------------------------------------------- segmentation

@pytest.mark.parametrize("iv", range(16))
def test_every_iv_round_trips(iv):
    """0 through 15, measured against segments rather than span."""
    fill, track = make_bar(iv)
    assert iv_from_bar_fill(bar_reading(fill, track).ratio) == iv


def test_gaps_are_not_counted_as_unfilled():
    """A full bar reads exactly 15, and the span-based reading does not.

    Measuring filled/(span) instead of filled/(sum of segments) charges the two
    inter-segment gaps against the fill. It is a small, one-directional bias --
    3.3% here -- but it always rounds downward, never up.
    """
    fill, track = make_bar(15)
    r = bar_reading(fill, track)
    assert r.segments == IV_BAR_SEGMENTS
    assert r.ratio == pytest.approx(1.0)
    assert iv_from_bar_fill(r.ratio) == 15

    span = SEG_WIDTH * IV_BAR_SEGMENTS + GAP * (IV_BAR_SEGMENTS - 1)
    naive = r.filled_px / span
    assert naive < 1.0, "span-based reading must under-report a full bar"
    assert r.ratio - naive == pytest.approx(GAP * 2 / span, abs=1e-3)


def test_reading_is_flagged_implausible_when_bar_is_missed():
    """A scanline that misses the bar must not yield a confident IV."""
    fill = [False] * 400
    track = [False] * 400
    for i in range(50, 300):       # one solid block: not a segmented bar
        track[i] = True
    r = bar_reading(fill, track)
    assert not r.plausible


def test_cluster_survives_extra_ui_on_the_same_row():
    """The row also crosses the team leader, so it is not exactly three runs."""
    fill, track = make_bar(9)
    for i in range(900, 1000):     # unrelated UI further right
        track[i] = True
    for i in range(1050, 1120):
        track[i] = True

    runs = contiguous_runs(track)
    assert len(runs) > IV_BAR_SEGMENTS
    trio = find_bar_cluster(runs)
    assert trio is not None and len(trio) == IV_BAR_SEGMENTS
    assert trio[0][0] == TRACK_LEFT
    assert iv_from_bar_fill(bar_reading(fill, track).ratio) == 9


def test_cluster_rejects_dissimilar_runs():
    """Three runs of wildly different widths are not a bar."""
    runs = [(0, 30), (60, 300), (400, 420)]
    assert find_bar_cluster(runs) is None


# ------------------------------------------------------------------- layout

def test_barlayout_fractions_land_on_the_measured_bars():
    """The calibrated fractions must frame the bars on the real resolution."""
    h, w = 2622, 1206
    regions = BarLayout().regions(h, w)

    # Measured bar rows on the real capture.
    for stat, y_actual in (("attack", 2022), ("defense", 2132), ("hp", 2243)):
        y0, y1, x0, x1 = regions[stat]
        assert y0 <= y_actual <= y1, f"{stat} row {y_actual} outside {y0}-{y1}"

    # Measured track on the real capture: x 142 through 560.
    TRACK_RIGHT = 560
    y0, y1, x0, x1 = regions["attack"]
    assert x0 <= TRACK_LEFT, "left edge must not start right of the bar"
    assert x1 >= TRACK_RIGHT, "right edge cuts the bar short"
    # The old right=0.945 ran to x=1139, well past the card and into the
    # team leader; anything beyond ~0.55 of the width is not bar.
    assert x1 < w * 0.55


# --------------------------------------------------------------------------
# The resource row: stardust / <species> candy / <species> candy XL
# --------------------------------------------------------------------------
#
# Word positions below are as measured off a real 1206-wide detail screen
# showing 521,865 stardust, 1,645 Swinub candy and 293 Swinub candy XL.

from pogo_opt.ingest import ResourceRow, parse_resource_row  # noqa: E402

REAL_VALUES = [(141, "521,865"), (574, "1,645"), (962, "293")]
REAL_LABELS = [(180, "STARDUST"), (502, "SWINUB"), (650, "CANDY"),
               (871, "SWINUB"), (985, "XL"), (1019, "CANDY")]


def test_reads_a_real_resource_row():
    r = parse_resource_row(REAL_VALUES, REAL_LABELS)
    assert r == ResourceRow(species="Swinub", stardust=521865,
                            candy=1645, xl_candy=293)
    assert r.usable


def test_edge_speck_does_not_steal_the_stardust_column():
    """Regression: a stray "7" at x=0 claimed stardust before the real value.

    Assigning per number to its nearest column let the first token seen win.
    Columns must instead take the nearest number to themselves.
    """
    noisy = [(0, "7")] + REAL_VALUES + [(1174, ";")]
    assert parse_resource_row(noisy, REAL_LABELS).stardust == 521865


def test_each_number_is_used_once():
    """Two columns must not both claim the same number."""
    r = parse_resource_row(REAL_VALUES, REAL_LABELS)
    assert len({r.stardust, r.candy, r.xl_candy}) == 3


def test_xl_column_survives_a_missed_second_candy():
    """OCR drops a word now and then; "XL" alone still marks the column."""
    labels = [(180, "STARDUST"), (502, "SWINUB"), (650, "CANDY"),
              (871, "SWINUB"), (985, "XL")]
    assert parse_resource_row(REAL_VALUES, labels).xl_candy == 293


def test_a_non_detail_screen_reads_as_nothing():
    """Most frames of a scroll-through are not a detail view."""
    r = parse_resource_row([(100, "42")], [(100, "FAVORITE")])
    assert not r.usable


def test_stardust_alone_is_not_usable():
    """Stardust is one global number the trainer already knows. The species
    and its candy are the part nothing else can supply."""
    r = parse_resource_row([(141, "521,865")], [(180, "STARDUST")])
    assert r.stardust == 521865
    assert not r.usable


def test_missing_xl_is_none_not_zero():
    """An unread XL count is unknown, not empty -- zero would constrain the
    solver to never spend XL on that species."""
    labels = [(180, "STARDUST"), (502, "SWINUB"), (650, "CANDY")]
    r = parse_resource_row(REAL_VALUES[:2], labels)
    assert r.xl_candy is None


def test_majority_wins_over_a_corrupted_reading():
    """Cross-frame voting, which is not optional for the resource row.

    On one short clip Anorith read {63, 631, 631, 6315} across four frames and
    Meowth {4, 4351, 4351, 4357}. Taking the maximum or the first picks a
    corrupted value in both cases. Only the majority picks the right one.
    """
    from collections import Counter

    for readings, expected in (
        ([63, 631, 631, 6315], 631),
        ([4, 4351, 4351, 4357], 4351),
        ([1645] * 12 + [164], 1645),
    ):
        assert Counter(readings).most_common(1)[0][0] == expected


def test_a_single_frame_is_not_enough_to_trust():
    """One reading is a coin flip on whether a digit was dropped."""
    from collections import Counter

    counter = Counter([63, 631])
    value, agreeing = counter.most_common(1)[0]
    assert agreeing < sum(counter.values()) / 2 + 1, (
        "a 1-1 split must not be treated as a majority"
    )


# --------------------------------------------------------------------------
# The candy column is found by its label, never by a fixed position
# --------------------------------------------------------------------------
#
# Mega-capable Pokemon carry an extra Mega Energy element on this screen, which
# shifts the resource row. Any hardcoded column fraction reads the wrong number
# for exactly those Pokemon -- and they are disproportionately the ones worth
# investing in, so the error lands where it hurts most.

from pogo_opt.ingest import find_candy_anchor  # noqa: E402


def _anchor_candy(labels, numbers, species, width=1206):
    """Locate the column with the real function, then take its number."""
    anchor = find_candy_anchor(labels, species)
    if anchor is None:
        return None
    near = [(abs(anchor - x), v) for x, v in numbers if abs(anchor - x) <= width * 0.15]
    return min(near)[1] if near else None


def test_candy_found_by_label_on_a_dimmed_screen():
    """Real tokens off the appraisal overlay, Pikachu and Anorith."""
    assert _anchor_candy(
        [(464, "PIKACHUCANDY"), (980, "CANDY")], [(539, 9331), (993, 4)], "Pikachu"
    ) == 9331
    assert _anchor_candy(
        [(495, "ANORITHCAN\\"), (966, "//HCANDY"), (852, "XL")],
        [(313, 45), (595, 631), (993, 2)], "Anorith",
    ) == 631


def test_a_shifted_layout_still_reads():
    """The mega case: same row, moved. A fixed fraction would miss it."""
    shifted_labels = [(140, "CHARIZARDCANDY"), (700, "CANDY")]
    shifted_numbers = [(60, 521865), (215, 348), (712, 12)]
    assert _anchor_candy(shifted_labels, shifted_numbers, "Charizard") == 348


def test_a_half_read_xl_label_cannot_pose_as_candy():
    """Without the species check, "//HCANDY" anchors the XL column and its
    count is handed back as though it were candy."""
    assert _anchor_candy(
        [(966, "//HCANDY")], [(993, 2)], "Anorith"
    ) is None


def test_bare_candy_label_is_not_an_anchor():
    """The XL column's second word is a bare "CANDY" with no species prefix."""
    assert _anchor_candy([(980, "CANDY")], [(993, 4)], "Pikachu") is None


def test_wrong_species_label_is_rejected():
    """A label left over from the previous Pokemon mid-swipe must not match."""
    assert _anchor_candy(
        [(464, "PIKACHUCANDY")], [(539, 9331)], "Anorith"
    ) is None
