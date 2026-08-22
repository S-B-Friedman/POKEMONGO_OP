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

# Resource-row colours, sampled off a real detail screen rather than invented.
# The reader separates the two lines by brightness at a fixed teal hue -- the
# numerals come in around V=100 and the labels around V=171 -- so a fixture
# painted in plausible-looking neutral greys has no hue and reads as blank.
ROW_VALUE = (84, 99, 99)
ROW_LABEL = (163, 170, 170)
ROW_CARD = (254, 254, 254)

# Type sizes and line pitch in PIXELS, deliberately not as fractions of screen
# height. The variable under test is WHERE the row sits, so everything else is
# held constant; scaling the type with the canvas instead varies legibility at
# the same time and confuses one for the other. It did: at 0.007 of height the
# labels came out 13px on the shortest canvas, too crunchy for tesseract to
# read, and the row looked unlocatable when it was merely unrendered.
#
# The ratio still has to be right. At a plausible-looking value size the glyphs
# grow taller than the gap to the line below and overlap the labels, and
# tesseract reads "SWINUB CANDY" as "RFan ... DY".
ROW_VALUE_FONT = 30
ROW_LABEL_FONT = 17
ROW_LINE_PITCH = 42


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


def _resource_card(path, h, w, row_fraction):
    """A detail card with the resource row painted at a chosen height fraction.

    The row's own colours matter: the numerals are near-black and the labels a
    lighter grey, and read_resource_row uses exactly that difference to tell the
    two lines apart.
    """
    img = Image.new("RGB", (w, h), ROW_CARD)
    d = ImageDraw.Draw(img)
    d.text((int(w * 0.14), int(h * 0.42)), "Swinub", fill=ROW_VALUE,
           font=_font(ROW_VALUE_FONT))
    columns = [(0.10, "521,865", "STARDUST"),
               (0.42, "1,645", "SWINUB CANDY"),
               (0.73, "293", "SWINUB CANDY XL")]
    for x, value, label in columns:
        d.text((int(w * x), int(h * row_fraction)), value,
               fill=ROW_VALUE, font=_font(ROW_VALUE_FONT))
        d.text((int(w * x), int(h * row_fraction) + ROW_LINE_PITCH), label,
               fill=ROW_LABEL, font=_font(ROW_LABEL_FONT))
    img.save(path)
    return path


def test_reading_a_name_against_a_species_list_works_at_all(tmp_path):
    """Regression: this raised NameError on every call that supplied a list.

    read_species_name() delegates to match_species_name() for the accurate path
    -- fuzzy-matching the text against all 1,024 distinct species names -- and
    ocr_ingest never imported it. Every internal caller passes no list and takes
    the regex path, so nothing in the repo touched the broken branch, and the
    one measurement that would have caught it is the one nobody had run: reading
    real names off real frames.
    """
    pytest.importorskip("pytesseract")
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract binary not on PATH")
    from ocr_ingest import load_known_names, read_species_name

    names = load_known_names(None)
    assert len(names) > 900, "reference species list did not load"

    path = _resource_card(tmp_path / "named.png", 2340, 1080, 0.66)
    assert read_species_name(cv2.imread(str(path)), names) == "Swinub"


@pytest.mark.parametrize("h,w,row", [
    (2340, 1080, 0.66),    # the shape _RESOURCE_BAND was calibrated on
    (1920, 1080, 0.71),    # 9:16, where the row falls past the band's cutoff
    (2778, 1284, 0.78),
])
def test_resource_row_is_found_wherever_it_sits(tmp_path, h, w, row):
    """The row is located by its labels, not by a fraction of screen height.

    A fraction does not survive a change of aspect ratio: on a 1080x1920 capture
    "PALKIA CANDY" sits at 0.707, a hair past the 0.70 cutoff, and the whole row
    went unread. Nothing else on the card says STARDUST or CANDY, so the labels
    are a landmark that travels.
    """
    pytest.importorskip("pytesseract")
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract binary not on PATH")
    from ocr_ingest import read_resource_row

    path = _resource_card(tmp_path / f"card_{h}x{w}.png", h, w, row)
    r = read_resource_row(cv2.imread(str(path)))

    # Values are checked against the truth, never merely for being present. A
    # populated row that names the XL count as candy is the failure mode here,
    # and it looks perfectly healthy from the outside.
    assert r.species == "Swinub"
    assert r.candy == 1645
    assert r.stardust in (521865, None)
    assert r.xl_candy in (293, None)


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


def test_frames_are_grouped_by_when_not_by_what(tmp_path):
    """A swipe-through is segmented in time, which is what saves duplicates.

    Consecutive look-alike frames are one Pokemon; a jump is the swipe. Grouping
    on what was READ instead cannot distinguish three Machamps at CP 2451 from
    three readings of one, and silently kept one of them.
    """
    from ocr_ingest import group_consecutive

    a = _resource_card(tmp_path / "a.png", 2340, 1080, 0.66)
    b = tmp_path / "b.png"
    img = Image.new("RGB", (1080, 2340), ROW_CARD)
    d = ImageDraw.Draw(img)
    d.rectangle([100, 100, 900, 2000], fill=(20, 90, 140))   # a different screen
    img.save(b)

    held = [(f"a{i}", a) for i in range(12)]
    swiped = held + [(f"b{i}", b) for i in range(9)]

    assert len(group_consecutive(held)) == 1
    groups = group_consecutive(swiped)
    assert [len(g) for g in groups] == [12, 9]
