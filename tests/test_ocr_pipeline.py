"""End-to-end test of the image path against a synthetic screenshot.

Skips cleanly where OpenCV/Tesseract aren't installed, so the core suite still
runs on a bare `pip install -r requirements.txt`.

The painted bars deliberately mimic the real UI: three segments with gaps,
orange fill, mid-grey track, and RED for a maxed stat. An earlier version
painted one solid black rectangle on light grey, which the old greyscale
reader happened to score correctly -- so the test passed while describing a
screen the game never draws, and went on passing when the reader was rewritten
to look for the actual colours. A synthetic fixture that does not resemble its
subject only tests itself.
"""

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

cv2 = pytest.importorskip("cv2")
PIL = pytest.importorskip("PIL")

from PIL import Image, ImageDraw, ImageFont

from pogo_opt.ingest import BarLayout, build_record

W, H = 1080, 2340
TRUE_FILLS = {"attack": 15 / 15, "defense": 10 / 15, "hp": 7 / 15}

# Sampled from a real capture (PIL is RGB; OpenCV reports these as BGR).
ORANGE = (240, 153, 63)
RED_MAXED = (217, 112, 119)      # the game recolours a 15/15 bar
TRACK_GREY = (221, 219, 220)
CARD = (250, 250, 250)

SEGMENTS = 3
GAP_FRACTION = 0.02              # gap as a fraction of the bar's total span


def paint_bar(draw, box, fill_fraction: float) -> None:
    """Draw one appraisal bar: three segments, gaps, correct fill colour."""
    y0, y1, x0, x1 = box
    span = x1 - x0
    gap = max(1, int(span * GAP_FRACTION))
    seg_w = (span - gap * (SEGMENTS - 1)) / SEGMENTS

    colour = RED_MAXED if fill_fraction >= 1.0 else ORANGE
    fillable = seg_w * SEGMENTS
    remaining = fillable * fill_fraction

    x = x0
    for _ in range(SEGMENTS):
        draw.rectangle([x, y0, x + seg_w, y1], fill=TRACK_GREY)
        if remaining > 0:
            draw.rectangle([x, y0, x + min(seg_w, remaining), y1], fill=colour)
            remaining -= seg_w
        x += seg_w + gap


def _font(size):
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


@pytest.fixture(scope="module")
def screenshot(tmp_path_factory):
    img = Image.new("RGB", (W, H), CARD)
    d = ImageDraw.Draw(img)
    d.text((60, 300), "CP2451", fill=(20, 20, 20), font=_font(90))
    d.text((60, 430), "Charizard", fill=(20, 20, 20), font=_font(76))
    d.text((60, 560), "150/150 HP", fill=(20, 20, 20), font=_font(58))
    d.text((60, 660), "10,000 Stardust", fill=(20, 20, 20), font=_font(52))

    for stat, box in BarLayout().regions(H, W).items():
        paint_bar(d, box, TRUE_FILLS[stat])

    path = tmp_path_factory.mktemp("shots") / "shot.png"
    img.save(path)
    return path


def test_bar_fills_recover_painted_values(screenshot):
    from ocr_ingest import read_bar_fills

    fills = read_bar_fills(screenshot, BarLayout())
    for stat, expected in TRUE_FILLS.items():
        assert fills[stat] == pytest.approx(expected, abs=0.03), stat


def test_ivs_round_trip_through_the_bars(screenshot):
    from ocr_ingest import read_bar_fills

    rec = build_record("CP2451 Charizard", ["Charizard"],
                       read_bar_fills(screenshot, BarLayout()))
    assert (rec.attack_iv, rec.defense_iv, rec.stamina_iv) == (15, 10, 7)
    assert rec.iv_confidence > 0.5
    assert rec.warnings == []


def test_text_extraction(screenshot):
    # The precondition is the tesseract BINARY, not the Python wrapper. Guarding
    # on `importorskip("pytesseract")` checks the wrong thing: the wrapper
    # installs from requirements-ocr.txt on its own, so the test stops skipping
    # and starts erroring with TesseractNotFoundError wherever the binary is
    # absent. CI installs the binary; this keeps a bare checkout skipping.
    pytest.importorskip("pytesseract")
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract binary not on PATH")
    from ocr_ingest import read_text

    rec = build_record(read_text(screenshot, "tesseract"), ["Charizard"])
    assert rec.name == "Charizard"
    assert rec.cp == 2451
    assert rec.is_usable


def test_layout_survives_a_different_aspect_ratio(tmp_path):
    """Same content on a differently-shaped screen must still read correctly --
    this is what the original's hardcoded pixel rows could not do."""
    from ocr_ingest import read_bar_fills

    h, w = 2778, 1284
    img = Image.new("RGB", (w, h), CARD)
    d = ImageDraw.Draw(img)
    for stat, box in BarLayout().regions(h, w).items():
        paint_bar(d, box, TRUE_FILLS[stat])
    p = tmp_path / "tall.png"
    img.save(p)

    fills = read_bar_fills(p, BarLayout())
    for stat, expected in TRUE_FILLS.items():
        assert fills[stat] == pytest.approx(expected, abs=0.03), stat
