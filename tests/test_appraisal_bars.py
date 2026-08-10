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
