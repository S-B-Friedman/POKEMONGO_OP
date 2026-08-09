"""End-to-end test of the image path against a synthetic screenshot.

Skips cleanly where OpenCV/Tesseract aren't installed, so the core suite still
runs on a bare `pip install -r requirements.txt`.
"""

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
    img = Image.new("RGB", (W, H), (250, 250, 250))
    d = ImageDraw.Draw(img)
    d.text((60, 300), "CP2451", fill=(20, 20, 20), font=_font(90))
    d.text((60, 430), "Charizard", fill=(20, 20, 20), font=_font(76))
    d.text((60, 560), "150/150 HP", fill=(20, 20, 20), font=_font(58))
    d.text((60, 660), "10,000 Stardust", fill=(20, 20, 20), font=_font(52))

    for stat, (y0, y1, x0, x1) in BarLayout().regions(H, W).items():
        d.rectangle([x0, y0, x1, y1], fill=(235, 235, 235))
        d.rectangle([x0, y0, x0 + int((x1 - x0) * TRUE_FILLS[stat]), y1], fill=(10, 10, 10))

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
    pytest.importorskip("pytesseract")
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
    img = Image.new("RGB", (w, h), (250, 250, 250))
    d = ImageDraw.Draw(img)
    for stat, (y0, y1, x0, x1) in BarLayout().regions(h, w).items():
        d.rectangle([x0, y0, x1, y1], fill=(235, 235, 235))
        d.rectangle([x0, y0, x0 + int((x1 - x0) * TRUE_FILLS[stat]), y1], fill=(10, 10, 10))
    p = tmp_path / "tall.png"
    img.save(p)

    fills = read_bar_fills(p, BarLayout())
    for stat, expected in TRUE_FILLS.items():
        assert fills[stat] == pytest.approx(expected, abs=0.03), stat
