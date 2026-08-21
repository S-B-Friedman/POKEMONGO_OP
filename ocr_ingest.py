#!/usr/bin/env python3
"""
Turn Pokemon GO screenshots or a scroll-through video into a CSV.

    python ocr_ingest.py --images shots/ -o scanned.csv
    python ocr_ingest.py --video swipe.mp4 -o scanned.csv
    python ocr_ingest.py --video swipe.mp4 --calibrate    # dump bar crops

Output feeds the same CSV loader the solver already uses, so ingestion and
optimization stay decoupled: you can fix a bad read by editing a spreadsheet
instead of re-running OCR.

Requires the optional extras:  pip install -r requirements-ocr.txt

STATUS. The parsing layer (pogo_opt/ingest.py) is unit-tested, and so is the
bar reading here -- against synthetic screenshots painted to match the real UI,
plus a calibration measured on a real 1206x2622 capture where the extracted IVs
reproduced both the displayed CP and HP at exactly one level.

What is still unverified on real pixels is the TEXT: name, CP and HP have only
ever been OCR'd from synthetic images. That is the next thing here likely to be
wrong in a way the tests cannot see.

Bar regions no longer need to be guessed. detect_bars() finds the bars in the
image; BarLayout's fractions are only the fallback, and --calibrate is for
checking that fallback on a new device.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from pathlib import Path

from pogo_opt.ingest import (
    IV_BAR_SEGMENTS,
    BarLayout,
    BarReading,
    ScannedPokemon,
    bar_reading,
    ResourceRow,
    build_record,
    contiguous_runs,
    parse_resource_row,
    find_bar_cluster,
    find_candy_anchor,
)

log = logging.getLogger("ocr_ingest")

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _require(module: str, package: str):
    """Import a heavy optional dependency, with a useful message if absent."""
    try:
        return __import__(module)
    except ImportError as exc:
        raise SystemExit(
            f"{module} is required for this command.\n"
            f"  pip install {package}\n"
            f"or install all OCR extras: pip install -r requirements-ocr.txt"
        ) from exc


def load_known_names(path: Path | None) -> list[str]:
    """Species list for name matching.

    The original read this at import time from a file that isn't in the repo,
    so the module raised FileNotFoundError before any function could be called.
    """
    if path and path.exists():
        return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # The committed reference has every species. Falling back to the 26 rows of
    # sample_data gave the matcher 20 names to choose from, so every Pokemon not
    # in the sample was forced onto the nearest one that was -- a Palkia came
    # back as "Gardevoir", confidently and with a plausible CP beside it.
    try:
        from pogo_opt.reference import default_reference

        names = sorted({s.name for s in default_reference()._species.values()})
        if names:
            log.info("matching names against %d species from reference.json", len(names))
            return names
    except Exception:
        pass

    fallback = Path(__file__).parent / "sample_data" / "collection.csv"
    if fallback.exists():
        with fallback.open(newline="", encoding="utf-8") as fh:
            names = sorted({row["name"] for row in csv.DictReader(fh) if row.get("name")})
        log.warning(
            "no --names file given; falling back to %d names from sample_data. "
            "Supply a full species list for real use.", len(names)
        )
        return names

    raise SystemExit("no species name list available -- pass --names path/to/names.txt")


# --------------------------------------------------------------------------
# Image handling
# --------------------------------------------------------------------------

def read_text(image_path: Path, engine: str) -> str:
    if engine == "vision":
        vision = _require("google.cloud.vision", "google-cloud-vision")
        from google.cloud import vision_v1  # noqa: E402

        client = vision_v1.ImageAnnotatorClient()
        image = vision_v1.Image(content=image_path.read_bytes())
        response = client.text_detection(image=image)
        if response.error.message:
            raise RuntimeError(response.error.message)
        texts = response.text_annotations
        return texts[0].description if texts else ""

    _require("pytesseract", "pytesseract")
    import pytesseract
    from PIL import Image, ImageEnhance

    img = Image.open(image_path).convert("L")
    img = ImageEnhance.Contrast(img).enhance(2.0)
    return pytesseract.image_to_string(img, config="--psm 6")


# Appraisal bar colours, in OpenCV HSV (hue 0-179).
#
# A partially filled stat is orange. A MAXED stat is red -- the game recolours
# the whole bar at 15/15. Detecting only orange therefore reads a perfect stat
# as completely empty, which is the single worst error this code can make: it
# turns the best Pokemon in a collection into the worst. Red wraps around the
# hue origin, hence the two ranges.
_ORANGE = ((5, 90, 90), (35, 255, 255))
_RED_LOW = ((0, 90, 90), (4, 255, 255))
_RED_HIGH = ((170, 90, 90), (179, 255, 255))

# The unfilled remainder of the bar: desaturated mid-grey, distinct from the
# near-white card behind it.
_TRACK_MAX_SAT = 40
_TRACK_VALUE = (195, 245)


def _bar_masks(img):
    """-> (fill_mask, track_mask) as bool arrays over the whole image."""
    import cv2
    import numpy as np

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    fill = cv2.inRange(hsv, *_ORANGE)
    for lo, hi in (_RED_LOW, _RED_HIGH):
        fill = cv2.bitwise_or(fill, cv2.inRange(hsv, lo, hi))

    sat = hsv[:, :, 1]
    val = img.mean(axis=2)
    grey = (sat < _TRACK_MAX_SAT) & (val > _TRACK_VALUE[0]) & (val < _TRACK_VALUE[1])

    fill_b = fill.astype(bool)
    return fill_b, (fill_b | grey)


def detect_bars(img, *, min_length: int = 20) -> dict[str, BarReading] | None:
    """Find the three appraisal bars in an image and measure them.

    Scans for scanlines that look like a three-segment bar and keeps the three
    strongest, top to bottom. This is preferred over `BarLayout` because it does
    not assume a device, a resolution, or where the card sits -- the fractions
    are only a fallback for when this finds nothing.
    """
    fill_m, track_m = _bar_masks(img)
    h = img.shape[0]

    # (y, left_x, reading) for every scanline that looks like a segmented bar.
    candidates: list[tuple[int, int, BarReading]] = []
    for y in range(int(h * 0.30), h):
        row_track = track_m[y]
        if row_track.sum() < min_length * 3:
            continue
        runs = contiguous_runs(row_track.tolist(), min_length=min_length)
        if len(runs) < IV_BAR_SEGMENTS:
            continue
        trio = find_bar_cluster(runs)
        if trio is None:
            continue
        row_fill = fill_m[y]
        total = sum(b - a + 1 for a, b in trio)
        filled = sum(1 for a, b in trio for x in range(a, b + 1) if row_fill[x])
        candidates.append((
            y,
            trio[0][0],
            BarReading(ratio=min(1.0, filled / total), segments=len(trio),
                       filled_px=filled, track_px=total),
        ))

    if not candidates:
        return None

    # Group adjacent scanlines sharing a left edge into one bar, then take the
    # median row so a rounded end or a compression artefact cannot swing it.
    groups: list[list[tuple[int, int, BarReading]]] = [[candidates[0]]]
    for cand in candidates[1:]:
        prev = groups[-1][-1]
        if cand[0] - prev[0] <= 3 and abs(cand[1] - prev[1]) <= 15:
            groups[-1].append(cand)
        else:
            groups.append([cand])

    groups = [g for g in groups if len(g) >= 6]
    if len(groups) < 3:
        return None

    # The three bars share a left edge and a track width. Anything that does not
    # is some other part of the UI that happened to look striped.
    groups.sort(key=lambda g: g[0][0])
    best: list[list[tuple[int, int, BarReading]]] | None = None
    for i in range(len(groups) - 2):
        trio = groups[i:i + 3]
        lefts = [g[len(g) // 2][1] for g in trio]
        widths = [g[len(g) // 2][2].track_px for g in trio]
        if max(lefts) - min(lefts) > 15:
            continue
        if max(widths) / max(1, min(widths)) > 1.10:
            continue
        best = trio
        break

    if best is None:
        return None

    return {
        stat: g[len(g) // 2][2]
        for stat, g in zip(("attack", "defense", "hp"), best)
    }


def read_bar_fills(
    image_path: Path, layout: BarLayout, *, autodetect: bool = True
) -> dict[str, float]:
    """Fraction of each appraisal bar that is filled.

    Measured against the bar's own segments, in colour. The previous version
    thresholded greyscale and treated dark pixels as filled, which does not
    describe this UI at all: the filled bar is mid-tone orange (grey ~158) and
    the empty track is light grey (~230), so on a real screenshot it read text
    and the team leader's outline instead of the bar.
    """
    _require("cv2", "opencv-python-headless")
    import cv2

    img = cv2.imread(str(image_path))
    if img is None:
        raise RuntimeError(f"could not read image: {image_path}")

    if autodetect:
        found = detect_bars(img)
        if found:
            return {stat: r.ratio for stat, r in found.items()}
        log.warning("bar auto-detection failed; falling back to BarLayout fractions")

    fill_m, track_m = _bar_masks(img)
    h, w = img.shape[:2]
    fills: dict[str, float] = {}
    for stat, (y0, y1, x0, x1) in layout.regions(h, w).items():
        if y1 <= y0 or x1 <= x0 or y1 > h:
            continue
        mid = min(h - 1, (y0 + y1) // 2)
        r = bar_reading(fill_m[mid][x0:x1].tolist(), track_m[mid][x0:x1].tolist())
        fills[stat] = r.ratio
    return fills


def calibrate(image_path: Path, layout: BarLayout, out_dir: Path) -> None:
    """Write the cropped bar regions so they can be eyeballed against a real
    screenshot. Cheaper than guessing at pixel offsets."""
    _require("cv2", "opencv-python-headless")
    import cv2

    img = cv2.imread(str(image_path))
    if img is None:
        raise SystemExit(f"could not read image: {image_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    h, w = img.shape[:2]
    for stat, (y0, y1, x0, x1) in layout.regions(h, w).items():
        cv2.imwrite(str(out_dir / f"{stat}_bar.png"), img[y0:y1, x0:x1])
    annotated = img.copy()
    for _, (y0, y1, x0, x1) in layout.regions(h, w).items():
        cv2.rectangle(annotated, (x0, y0), (x1, y1), (0, 0, 255), 3)
    cv2.imwrite(str(out_dir / "regions_overlay.png"), annotated)
    print(f"Wrote crops and overlay to {out_dir}/ -- check the bars are framed.")


# --------------------------------------------------------------------------
# Video handling
# --------------------------------------------------------------------------

def iter_video_frames(video_path: Path, every_n: int, work_dir: Path):
    """Yield (label, frame_path) for sampled, visually distinct frames.

    The original wrote every frame to disk and OCR'd each one. A 60-second
    30fps clip is 1,800 frames, so on the Vision API that's 1,800 billed calls
    to scan a collection that scrolls past maybe forty Pokemon. This samples
    every Nth frame and skips frames that are near-identical to the last kept
    one, which is what happens while a scroll is settling.
    """
    _require("cv2", "opencv-python-headless")
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise SystemExit(f"could not open video: {video_path}")

    work_dir.mkdir(parents=True, exist_ok=True)
    idx, kept, previous = 0, 0, None
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % every_n != 0:
                idx += 1
                continue

            small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (64, 64))
            if previous is not None:
                if float(np.mean(np.abs(small.astype(int) - previous.astype(int)))) < 4.0:
                    idx += 1
                    continue
            previous = small

            path = work_dir / f"frame_{idx:06d}.png"
            cv2.imwrite(str(path), frame)
            kept += 1
            yield f"frame_{idx}", path
            idx += 1
    finally:
        cap.release()
        log.info("scanned %d frames, kept %d distinct", idx, kept)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def dedupe(records: list[ScannedPokemon]) -> list[ScannedPokemon]:
    """Collapse repeats of the same Pokemon, keeping the cleanest read.

    A scroll-through shows each Pokemon over several frames; (name, cp) is a
    good enough identity for a single pass.
    """
    best: dict[tuple[str | None, int | None], ScannedPokemon] = {}
    for rec in records:
        key = (rec.name, rec.cp)
        incumbent = best.get(key)
        if incumbent is None or len(rec.warnings) < len(incumbent.warnings):
            best[key] = rec
    return list(best.values())


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--images", type=Path, help="directory of screenshots")
    src.add_argument("--video", type=Path, help="scroll-through video")

    ap.add_argument("-o", "--out", type=Path, default=Path("scanned.csv"))
    ap.add_argument("--engine", choices=("tesseract", "vision"), default="tesseract")
    ap.add_argument("--names", type=Path, help="newline-separated species list")
    ap.add_argument("--every-n", type=int, default=10, help="sample every Nth video frame")
    ap.add_argument("--work-dir", type=Path, default=Path(".ocr_frames"))
    ap.add_argument("--calibrate", action="store_true",
                    help="dump bar crops from the first frame and exit")
    ap.add_argument("--no-ivs", action="store_true", help="skip appraisal bar reading")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(levelname)s] %(message)s",
    )

    layout = BarLayout()

    if args.images:
        sources = [
            (p.name, p)
            for p in sorted(args.images.iterdir())
            if p.suffix.lower() in IMAGE_SUFFIXES
        ]
        if not sources:
            raise SystemExit(f"no images found in {args.images}")
    else:
        sources = list(iter_video_frames(args.video, args.every_n, args.work_dir))
        if not sources:
            raise SystemExit("no frames extracted")

    if args.calibrate:
        calibrate(sources[0][1], layout, Path("calibration"))
        return 0

    known_names = load_known_names(args.names)

    records: list[ScannedPokemon] = []
    for label, path in sources:
        try:
            text = read_text(path, args.engine)
        except Exception as exc:
            log.warning("OCR failed on %s: %s", label, exc)
            continue

        fills = None
        if not args.no_ivs:
            try:
                fills = read_bar_fills(path, layout)
            except Exception as exc:
                log.debug("bar read failed on %s: %s", label, exc)

        rec = build_record(text, known_names, fills, source_frame=label)
        if rec.is_usable:
            records.append(rec)
        else:
            log.debug("discarded %s: %s", label, "; ".join(rec.warnings))

    unique = dedupe(records)
    if not unique:
        raise SystemExit("nothing usable extracted -- try --calibrate, or --engine vision")

    rows = [r.to_row() for r in unique]
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    flagged = sum(1 for r in unique if r.warnings)
    print(f"Wrote {len(unique)} records to {args.out} ({flagged} flagged for review).")
    print("Review flagged rows, then map into the solver's collection.csv schema.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --------------------------------------------------------------------------
# Reading the resource row (stardust / candy / XL candy)
# --------------------------------------------------------------------------

# The numerals are dark teal; the icons beside them are bright orange (candy)
# or a bright vial (stardust). Thresholding on brightness alone leaves dark
# icon edges behind, and Tesseract reads those as digits -- the candy icon
# turned 1,645 into 21,645 and the stardust vial turned 521,865 into 1521,865.
# Requiring the teal hue as well drops them cleanly.
# The numerals are dark (V < 150); the labels beneath them are a lighter grey
# (V up to ~200). One threshold cannot serve both -- tuned for the numerals it
# erases the labels entirely, which is how a frame with three perfectly-read
# numbers produced no species name and was discarded.
_VALUE_MAX_VALUE = 150
_LABEL_MAX_VALUE = 205
_TEXT_HUE = (70, 110)

# Where the row sits, as fractions of screen height. Unlike the appraisal bars
# there is no distinctive shape to search for, so this is a band to look in.
_RESOURCE_BAND = (0.60, 0.70)


def _text_mask(img, max_value: int):
    """Black text on white, with the coloured icons removed."""
    import cv2
    import numpy as np

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    keep = (v < max_value) & (h > _TEXT_HUE[0]) & (h < _TEXT_HUE[1])
    return (255 - keep.astype(np.uint8) * 255)


def _strokes(band, scale: int = 3):
    """Isolate text strokes from whatever is behind them.

    A global threshold works on the white info card and fails completely on the
    appraisal overlay, where the same row is drawn over a blue gradient, a team
    leader's face and a rating badge. A black-hat keeps dark detail smaller than
    the kernel -- letter strokes -- and discards the smooth backgrounds, which
    handles both without knowing which one it is looking at.
    """
    import cv2

    big = cv2.resize(band, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (45, 45))
    hat = cv2.morphologyEx(big, cv2.MORPH_BLACKHAT, kernel)
    hat = cv2.normalize(hat, None, 0, 255, cv2.NORM_MINMAX)
    _, th = cv2.threshold(hat, 60, 255, cv2.THRESH_BINARY)
    return 255 - th


def _words(band, scale: int = 3, psm: int = 6) -> list[tuple[int, str]]:
    """-> [(x_in_original_pixels, token)] for one already-prepared strip."""
    import pytesseract
    from pytesseract import Output

    data = pytesseract.image_to_data(
        band, config=f"--psm {psm}", output_type=Output.DICT
    )
    return [
        (data["left"][i] // scale, t.strip())
        for i, t in enumerate(data["text"])
        if t.strip()
    ]


def read_species_name(img, known_names=None) -> str | None:
    """The species (or nickname) from the top of the info card.

    Read separately from the resource row on purpose. The row's labels carry the
    species too -- "SWINUB CANDY" -- but they are the first thing to disappear
    when the appraisal overlay dims the screen, while this stays legible. Taking
    the identity from here and only the numbers from the row is what lets the
    same code read both screens.
    """
    import cv2

    h, w = img.shape[:2]
    band = cv2.cvtColor(
        img[int(h * 0.40):int(h * 0.46), int(w * 0.15):int(w * 0.85)],
        cv2.COLOR_BGR2GRAY,
    )
    text = " ".join(t for _, t in _words(_strokes(band), psm=7))
    if not text.strip():
        return None
    if known_names:
        return match_species_name(text, known_names)
    # Nicknames are common ("Pikachu96"), so keep the leading alphabetic run.
    m = re.match(r"[A-Za-z][A-Za-z'.\- ]{2,}", text.strip())
    return m.group(0).strip() if m else None


def read_resource_row(img) -> ResourceRow:
    """Read stardust and per-species candy off a Pokemon's detail screen.

    This is the only screen that shows per-species candy, and no export carries
    it, so it is the one route to the second resource the solver constrains.

    Returns an empty ResourceRow rather than guessing when the screen is not a
    detail view -- most frames of a scroll-through are not.
    """
    _require("cv2", "opencv-python-headless")
    _require("pytesseract", "pytesseract")
    import cv2

    h, w = img.shape[:2]
    top, bottom = int(h * _RESOURCE_BAND[0]), int(h * _RESOURCE_BAND[1])
    bh = bottom - top
    # The clean info card: dark teal text on white, icons removed by hue. This
    # reads all three columns, and reads them exactly, so it is tried first.
    values = _words(_text_mask(img, _VALUE_MAX_VALUE)[top:bottom, :][
        int(bh * 0.20):int(bh * 0.55), :])
    labels = _words(_text_mask(img, _LABEL_MAX_VALUE)[top:bottom, :][
        int(bh * 0.55):int(bh * 0.98), :])

    row = parse_resource_row(values, labels)
    if row.usable:
        return row

    grey = cv2.cvtColor(img[top:bottom, :], cv2.COLOR_BGR2GRAY)

    # The labels went unread. On the appraisal overlay they are dimmed to within
    # a few grey levels of their background while the numbers survive, so fall
    # back to fixed column positions and take the identity from the name at the
    # top of the card instead. Guessing the columns is only safe because the
    # layout is fixed; guessing the species never would be, so a frame with no
    # readable name yields nothing.
    species = read_species_name(img)
    if not species:
        return ResourceRow()

    # Search the whole band, not the value sub-slice the clean path uses. The
    # overlay shifts the row slightly, and slicing to where the numbers sit on
    # the info card cut Pikachu's 9,331 out of frame entirely.
    dimmed = _words(_strokes(grey))
    # Strip punctuation the black-hat leaves stuck to a token -- "631+" was a
    # clean 631 with a stray mark welded on. The digits themselves still have to
    # match exactly afterwards; this trims the edges, it does not loosen the
    # match.
    numbers = []
    for x, token in dimmed:
        trimmed = token.strip("+-.'|\"_*~ ")
        if re.fullmatch(r"\d[\d,]*", trimmed):
            numbers.append((x, int(trimmed.replace(",", ""))))

    # ONLY the candy column is taken here. On this screen the rating badge sits
    # over the stardust figure and the team leader stands over the XL one, so
    # both come back truncated -- a 521,865 read as 45, a 293 read as 2. Those
    # are not low-confidence readings, they are confident readings of a partly
    # hidden number, which is the one kind of output this pipeline must not
    # produce. Candy sits between the two and stays clear.
    #
    # The column is found by its LABEL, not by a fixed position. The label reads
    # as one run -- "PIKACHUCANDY", "ANORITHCAN" -- so it both locates the
    # column and names the species, and the species is then checked against the
    # name read from the top of the card. A position would have been simpler and
    # wrong: mega-capable Pokemon carry an extra Mega Energy element that shifts
    # this row, so any hardcoded fraction reads the wrong column for them.
    labels = _words(_strokes(grey[int(bh * 0.55):int(bh * 0.98), :]))
    # The label names the evolution family, so look that up before matching.
    family = None
    try:
        from pogo_opt.reference import default_reference

        hit = default_reference().species(species)
        family = hit.family if hit else None
    except Exception:
        family = None
    anchor = find_candy_anchor(labels, species, family)
    if anchor is None:
        return ResourceRow()
    near = [(abs(anchor - x), v) for x, v in numbers if abs(anchor - x) <= w * 0.15]
    if not near:
        return ResourceRow()
    return ResourceRow(species=species, candy=min(near)[1])


def scan_resource_rows(frames, ref=None, sample_every: int = 5) -> dict[int, ResourceRow]:
    """Read every detail screen in a frame sequence -> {species_id: ResourceRow}.

    Frames that are not a detail view read as empty and are skipped, so a
    scroll-through can be fed in whole.

    Where a species appears on several frames the MAJORITY reading wins. This is
    the same trick the rest of the pipeline uses -- a few seconds of video is
    dozens of frames of one screen -- and it is not optional here. A single
    frame's OCR drops or doubles a digit often enough to matter: across one
    short clip Anorith read {63, 631, 631, 6315} and Meowth {4, 4351, 4351,
    4357}. Taking the maximum, or the first, picks the corrupted value in both
    cases; the majority picks the right one.
    """
    from collections import Counter

    from pogo_opt.reference import default_reference

    ref = ref or default_reference()
    votes: dict[int, Counter] = {}
    seen: dict[int, ResourceRow] = {}

    for i, img in enumerate(frames):
        if i % sample_every:
            continue
        row = read_resource_row(img)
        if not row.usable:
            continue
        species = ref.species(row.species)
        if species is None:
            log.warning("resource row names %r, which is not in the reference", row.species)
            continue
        votes.setdefault(species.dex, Counter())[(row.candy, row.xl_candy)] += 1
        seen.setdefault(species.dex, row)

    out: dict[int, ResourceRow] = {}
    for dex, counter in votes.items():
        (candy, xl), agreeing = counter.most_common(1)[0]
        total = sum(counter.values())
        if agreeing < total / 2:
            log.warning("no majority candy reading for species %d: %r", dex, dict(counter))
            continue
        out[dex] = ResourceRow(species=seen[dex].species, candy=candy, xl_candy=xl)
    return out


def write_candy_csv(rows: dict[int, ResourceRow], path: Path) -> int:
    """Write {species_id: ResourceRow} in the shape load_candy_inventory reads."""
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["species_id", "candy", "xl_candy"])
        for sid, row in sorted(rows.items()):
            writer.writerow([sid, row.candy, row.xl_candy if row.xl_candy is not None else ""])
    return len(rows)
