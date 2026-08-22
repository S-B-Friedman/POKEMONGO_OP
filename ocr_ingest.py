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

THE TEXT IS NOT TRUSTED, IT IS CHECKED. Tesseract misreads the game's CP font
into other plausible numbers -- off a clean, tight, high-contrast crop it read
CP292 for a Pokemon displaying CP252, and CP6274 for one displaying CP4627. No
threshold fixes that, and a wrong CP pins the wrong level silently.

So cp_candidates() proposes generously across several preprocessings and
resolve.verify_cp() disposes, keeping only a CP that some level can reproduce
alongside the observed HP for the IVs read off the bars. Verified end to end on
real frames: from noisy candidates it recovered CP 292 at level 11 and CP 313
at level 8. When nothing survives it returns None, which is a real answer.

The species NAME is still read directly and is still the weak point -- it has
no equivalent arithmetic to check it against.

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
from collections import Counter
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
    match_species_name,
    resource_number,
    trim_segment_caps,
)

log = logging.getLogger("ocr_ingest")

# Mean absolute difference, on a 64x64 greyscale thumbnail, below which two
# frames are treated as the same screen. Used when sampling to skip frames that
# repeat one already kept.
_SAME_SCREEN_DIFF = 4.0

# Grouping a swipe-through uses the band carrying the species name and CP, not
# the whole frame. Measured over a real 50s capture (1080x1920, 30fps, sampled
# every 5th frame), the difference between consecutive frames is:
#
#     region              within a Pokemon     across a swipe
#     name band 0.40-0.47        0.34               20+
#     whole frame                2.39                8
#
# The appraisal screen animates -- the team leader moves, the sprite breathes,
# the background shifts -- so a whole-frame comparison sees motion on every
# frame of a Pokemon that is merely sitting there. On that capture it cut a
# 16-Pokemon swipe into 118 groups. The name band holds still until the name
# itself changes, which is exactly the event being looked for.
_IDENTITY_BAND = (0.40, 0.47)
_IDENTITY_DIFF = 5.0

# Groups shorter than this are swipe frames, not Pokemon. Mid-swipe the screen
# matches neither neighbour, so each transitional frame becomes a group of one:
# 91 of those 118 were exactly that. At the default sampling this is about half
# a second, well under the time anyone spends looking at a Pokemon -- but it is
# a frame count, so raising --every-n raises the real duration with it.
_MIN_GROUP_FRAMES = 3

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
        # Same cap trim as bar_reading: the rounded, anti-aliased segment ends
        # count as track and never as fill, which biases every reading low.
        trio = [trim_segment_caps(a, b) for a, b in trio]
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

def iter_video_frames(video_path: Path, every_n: int, work_dir: Path,
                      keep_similar: bool = False):
    """Yield (label, frame_path) for sampled video frames.

    The original wrote every frame to disk and OCR'd each one. A 60-second
    30fps clip is 1,800 frames, so on the Vision API that's 1,800 billed calls
    to scan a collection that scrolls past maybe forty Pokemon. This samples
    every Nth frame.

    `keep_similar` decides what happens to the repeats. Dropping them -- the
    old unconditional behaviour -- leaves exactly one frame per Pokemon, and one
    frame cannot be voted on. That quietly contradicted the design: three
    seconds per Pokemon at 30fps is ~90 frames of one screen, and every claim
    this pipeline makes about robustness rests on having them. Keep them for a
    swipe-through, where they are the evidence; drop them for a quick pass where
    one reading per screen is all that is wanted.
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
            if not keep_similar and previous is not None:
                same = float(np.mean(np.abs(small.astype(int) - previous.astype(int))))
                if same < _SAME_SCREEN_DIFF:
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

def _name_from_numbers(rec: ScannedPokemon, candidates) -> ScannedPokemon:
    """Recover an unread species from the CP, HP and IVs alone.

    A nickname is not a species name, so the matcher declines on them -- rightly,
    since forcing "Sanji 100" onto the nearest species is the confident wrong
    answer this pipeline exists to avoid. But declining used to cost the whole
    record: is_usable needs a name, so every renamed Pokemon was dropped.

    The numbers still identify it. species_consistent_with() asks which species
    can produce a given CP and HP at some level for these IVs, and on real
    readings that cuts 1,486 to a handful. A name is taken only when exactly one
    survives across every CP candidate; anything ambiguous is left unnamed, which
    is the state the record was already in. Recovering the wrong name would be
    worse than recovering none, because a name is what everything downstream
    keys on.
    """
    try:
        from pogo_opt.reference import default_reference
        from pogo_opt.resolve import species_consistent_with

        pool = list(default_reference()._species.values())
    except Exception:
        return rec

    # (species, cp) pairs the arithmetic allows. The CP comes along for free:
    # whichever candidate produced the match is a CP that verifies by
    # construction, so an unnamed record can gain both fields at once.
    allowed: set[tuple[str, int]] = set()
    for cp in dict.fromkeys(c for c in candidates if c):
        for name, _level in species_consistent_with(
            pool, rec.attack_iv, rec.defense_iv, rec.stamina_iv,
            cp, rec.total_hp,
        ):
            allowed.add((name, cp))

    names = {name for name, _ in allowed}
    if len(names) != 1:
        if names:
            rec.warnings.append(
                f"species not named: {len(names)} consistent with CP/HP "
                f"({', '.join(sorted(names)[:4])})"
            )
        return rec

    rec.name = next(iter(names))
    cps = {cp for _, cp in allowed}
    if len(cps) == 1:
        rec.cp = next(iter(cps))
    rec.warnings.append(f"species {rec.name} recovered from CP/HP, not from text")
    return rec


def verify_record(rec: ScannedPokemon, candidates) -> ScannedPokemon:
    """Referee a voted record against the arithmetic, in place.

    The module docstring has claimed since the CP work landed that
    cp_candidates() proposes and resolve.verify_cp() disposes. It did not: both
    exist, both are tested, and nothing in the scan driver ever called either.
    So the CP written to the CSV was whatever the regex scraped off the text,
    unchecked -- which is how a real capture produced a Dragonite at CP 7 and a
    Sceptile at CP 27, each sitting beside a perfectly good IV spread.

    Nothing here invents a value. A CP that no level can reproduce alongside the
    observed HP is removed and said out loud, because no CP is worth more than a
    wrong one.
    """
    if rec.total_hp is None:
        return rec
    if None in (rec.attack_iv, rec.defense_iv, rec.stamina_iv):
        return rec
    if not rec.name:
        return _name_from_numbers(rec, candidates)

    try:
        from pogo_opt.reference import default_reference
        from pogo_opt.resolve import verify_cp

        forms = default_reference().forms(rec.name)
    except Exception:
        return rec
    if not forms:
        return rec

    proposed = list(dict.fromkeys(
        [c for c in candidates if c] + ([rec.cp] if rec.cp else [])
    ))
    if not proposed:
        return rec

    # Every form of the name, not just the base one. A screenshot says "Palkia"
    # for both the base species and the Origin Forme, and at level 49 with
    # perfect IVs those are CP 4458 and CP 4627. Checking the base form alone
    # threw away a completely correct reading of the Origin Forme -- 4627 with
    # HP 170, which resolves exactly.
    verdict = None
    for species in forms:
        verdict = verify_cp(
            species.base_attack, species.base_defense, species.base_stamina,
            rec.attack_iv, rec.defense_iv, rec.stamina_iv,
            proposed, rec.total_hp,
        )
        if verdict is not None:
            break
    if verdict is None:
        if rec.cp is not None:
            rec.warnings.append(
                f"CP {rec.cp} discarded: no level reproduces it with HP "
                f"{rec.total_hp} for {rec.name} at {rec.attack_iv}/"
                f"{rec.defense_iv}/{rec.stamina_iv}"
            )
        rec.cp = None
        return rec

    if rec.cp != verdict.cp:
        rec.warnings.append(f"CP corrected {rec.cp} -> {verdict.cp} by level solve")
    rec.cp = verdict.cp
    return rec


def group_consecutive(sources, threshold: float = _IDENTITY_DIFF,
                      min_frames: int = _MIN_GROUP_FRAMES):
    """Split an ordered list of (label, path) into one group per Pokemon.

    A swipe-through holds on each Pokemon and then moves, so "which Pokemon is
    this" is answered by WHEN the frame was taken, not by what was read off it.
    Consecutive frames whose identity band matches are the same Pokemon; a jump
    is the swipe.

    Grouping in time is what makes duplicates survive. Identity-based collapsing
    cannot tell three Machamps at CP 2451 apart from three readings of one
    Machamp -- and a box of 1,500 is full of exactly that.

    Groups shorter than `min_frames` are dropped as mid-swipe frames. Mid-swipe
    the screen matches neither neighbour, so every transitional frame becomes a
    group of one and would otherwise be reported as a Pokemon that was never on
    screen long enough to read.
    """
    _require("cv2", "opencv-python-headless")
    import cv2
    import numpy as np

    groups: list[list] = []
    previous = None
    for item in sources:
        img = cv2.imread(str(item[1]))
        if img is None:
            continue
        h = img.shape[0]
        band = img[int(h * _IDENTITY_BAND[0]):int(h * _IDENTITY_BAND[1]), :]
        small = cv2.resize(cv2.cvtColor(band, cv2.COLOR_BGR2GRAY), (64, 16))
        moved = previous is None or float(
            np.mean(np.abs(small.astype(int) - previous.astype(int)))
        ) >= threshold
        if moved:
            groups.append([])
        groups[-1].append(item)
        previous = small

    kept = [g for g in groups if len(g) >= min_frames]
    if len(groups) != len(kept):
        log.info("%d runs, %d dropped as mid-swipe (under %d frames)",
                 len(groups), len(groups) - len(kept), min_frames)
    return kept


def _majority(values):
    """Most common non-None value, or None when there are none."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    return Counter(present).most_common(1)[0][0]


def vote_records(records: list[ScannedPokemon]) -> ScannedPokemon:
    """One consensus record from every frame showing the same Pokemon.

    Each field is voted independently, because OCR does not fail on a whole
    frame at once -- it drops the name on one and mangles the CP on another, and
    picking a single "best" frame throws away the good half of every other one.
    That was the previous behaviour: keep whichever record carried the fewest
    warnings and discard the rest.

    IVs are voted as a TRIPLE rather than per stat. They are read from three
    bars of one image, so a frame caught mid-animation is wrong about all three
    together; letting an attack from one frame pair with a defense from another
    would invent a spread that no frame actually showed.
    """
    if not records:
        raise ValueError("no records to vote on")

    ivs = _majority([
        (r.attack_iv, r.defense_iv, r.stamina_iv) for r in records
        if None not in (r.attack_iv, r.defense_iv, r.stamina_iv)
    ]) or (None, None, None)

    # Confidence belongs to the spread that won, not to the best frame overall.
    agreed = [r for r in records
              if (r.attack_iv, r.defense_iv, r.stamina_iv) == ivs]
    confidence = max((r.iv_confidence for r in agreed
                      if r.iv_confidence is not None), default=None)

    winner = ScannedPokemon(
        name=_majority([r.name for r in records]),
        cp=_majority([r.cp for r in records]),
        current_hp=_majority([r.current_hp for r in records]),
        total_hp=_majority([r.total_hp for r in records]),
        attack_iv=ivs[0],
        defense_iv=ivs[1],
        stamina_iv=ivs[2],
        iv_confidence=confidence,
        is_shadow=_majority([r.is_shadow for r in records]) or False,
        is_lucky=_majority([r.is_lucky for r in records]) or False,
        is_purified=_majority([r.is_purified for r in records]) or False,
        date_caught=_majority([r.date_caught for r in records]),
        source_frame=records[0].source_frame,
    )
    # Carry forward any warning that most frames raised. A warning seen once
    # describes a reading that was outvoted and is no longer being reported; a
    # warning seen on nearly every frame describes a standing condition -- a
    # miscalibrated crop region, say -- which the vote cannot fix and must not
    # hide.
    #
    # Dropping them all, which this did, produced the worst run of the project:
    # a real 50-second capture came back as "16 records (0 flagged for review)"
    # with the IVs read off the wrong part of the screen. Every frame had said
    # "low IV read confidence -- check bar crop region" and the consensus record
    # said nothing at all.
    seen = Counter(w for r in records for w in r.warnings)
    for warning, count in seen.items():
        if count * 2 >= len(records):
            winner.warnings.append(warning)

    if winner.name is None:
        winner.warnings.append("no species name agreed across frames")
    if winner.cp is None:
        winner.warnings.append("no CP agreed across frames")
    if ivs == (None, None, None):
        winner.warnings.append("no IV spread agreed across frames")
    return winner


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
    ap.add_argument("--no-verify", action="store_true",
                    help="skip checking the CP against the IVs and HP; faster, "
                         "and lets an unverifiable CP through")
    ap.add_argument("--one-frame-each", action="store_true",
                    help="keep one frame per screen instead of voting across "
                         "all of them; faster, and much less robust")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(levelname)s] %(message)s",
    )

    layout = BarLayout()
    if not args.no_verify:
        _require("cv2", "opencv-python-headless")
    import cv2

    if args.images:
        sources = [
            (p.name, p)
            for p in sorted(args.images.iterdir())
            if p.suffix.lower() in IMAGE_SUFFIXES
        ]
        if not sources:
            raise SystemExit(f"no images found in {args.images}")
    else:
        # Keep the repeats. They are the frames the vote is taken over.
        sources = list(iter_video_frames(args.video, args.every_n, args.work_dir,
                                         keep_similar=not args.one_frame_each))
        if not sources:
            raise SystemExit("no frames extracted")

    if args.calibrate:
        calibrate(sources[0][1], layout, Path("calibration"))
        return 0

    known_names = load_known_names(args.names)

    # A video is a swipe-through, so consecutive look-alike frames are one
    # Pokemon and get voted on. A directory of screenshots is one shot per
    # Pokemon already, and grouping those by appearance would merge two
    # genuinely different Pokemon that happen to look alike -- so each stands
    # alone.
    groups = ([[s] for s in sources] if args.images
              else group_consecutive(sources))
    log.info("%d frames -> %d Pokemon", len(sources), len(groups))

    unique: list[ScannedPokemon] = []
    for group in groups:
        seen: list[ScannedPokemon] = []
        proposed: set[int] = set()
        for label, path in group:
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

            seen.append(build_record(text, known_names, fills, source_frame=label))
            # Collected per frame and pooled for the group: the CP band is the
            # hardest text on the screen, and a candidate any one frame saw is
            # worth offering to the verifier.
            if not args.no_verify:
                try:
                    proposed.update(cp_candidates(cv2.imread(str(path))))
                except Exception as exc:
                    log.debug("CP candidates failed on %s: %s", label, exc)

        if not seen:
            continue
        rec = vote_records(seen)
        if not args.no_verify:
            rec = verify_record(rec, proposed)
        if rec.is_usable:
            unique.append(rec)
        else:
            log.debug("discarded %s: %s", rec.source_frame, "; ".join(rec.warnings))

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


def _words_xy(band, scale: int = 3, psm: int = 6, upscale: bool = True):
    """-> [(x, y_centre, token)] in original pixels, for one strip.

    Same call as _words, keeping the vertical position the caller throws away.
    Unlike _words this does its own upscaling, so the coordinates it divides by
    scale are the ones it actually read at. Pass upscale=False for a band that
    arrives already enlarged -- _strokes returns one -- and it will divide by
    scale without enlarging twice.
    """
    import cv2
    import pytesseract
    from pytesseract import Output

    big = cv2.resize(band, None, fx=scale, fy=scale,
                     interpolation=cv2.INTER_CUBIC) if upscale else band
    data = pytesseract.image_to_data(
        big, config=f"--psm {psm}", output_type=Output.DICT
    )
    out = []
    for i, t in enumerate(data["text"]):
        token = t.strip()
        if not token:
            continue
        centre = data["top"][i] + data["height"][i] / 2
        out.append((data["left"][i] // scale, centre / scale, token))
    return out


# Where to hunt for the resource row when the fixed band misses it. Wide enough
# to cover both screen shapes seen so far and still stop short of the "caught
# on ..." paragraph at ~0.93.
_RESOURCE_SEARCH_BAND = (0.50, 0.90)

# Words that identify the row wherever it has ended up.
_RESOURCE_ANCHORS = ("STARDUST", "CANDY")

# Line geometry, as fractions of screen height. A fraction is what went wrong
# for the row's POSITION, but a fraction is the right unit for its PITCH: the
# game scales its type with the screen, so two lines of the card sit the same
# proportional distance apart on every device, while where that pair lands
# depends on the aspect ratio. Tesseract's own reported glyph heights looked
# like the natural unit and are not -- it called the same label row 2.7px tall
# in one word and 11.3px in the next.
_RESOURCE_LINE_TOLERANCE = 0.006
_RESOURCE_LINE_GAP = 0.030


def locate_resource_row(img):
    """Find the resource row by reading it, rather than assuming where it sits.

    _RESOURCE_BAND is a fraction of screen height calibrated on one phone, and a
    fraction does not survive a change of aspect ratio: on a 1080x1920 capture
    "PALKIA CANDY" sits at 0.707 of the height, a hair past the 0.70 cutoff, and
    the entire row goes unread. The labels are their own landmark -- nothing else
    on the card says STARDUST or CANDY -- so search a wide band for them and take
    the numbers from whichever line turns out to be directly above.

    -> (values, labels) in the (x, text) shape parse_resource_row wants, or None
    when no labelled row is in the band. Returning None rather than a guess is
    the point: most frames of a scroll-through are not detail screens at all.
    """
    import cv2

    h, w = img.shape[:2]
    top = int(h * _RESOURCE_SEARCH_BAND[0])
    bottom = int(h * _RESOURCE_SEARCH_BAND[1])
    # One mask, one OCR pass. The looser label threshold keeps the lighter grey
    # labels AND the darker numerals above them, so both lines come back at once.
    words = _words_xy(_text_mask(img, _LABEL_MAX_VALUE)[top:bottom, :])
    if not words:
        return None

    def normalise(token: str) -> str:
        return re.sub(r"[^A-Z]", "", token.upper())

    anchors = [wd for wd in words
               if any(a in normalise(wd[2]) for a in _RESOURCE_ANCHORS)]
    if not anchors:
        return None

    tolerance = h * _RESOURCE_LINE_TOLERANCE
    ys = sorted(wd[1] for wd in anchors)
    label_y = ys[len(ys) // 2]

    labels = [(int(x), token) for x, y, token in words
              if abs(y - label_y) <= tolerance]

    # The numbers sit on the line immediately above. Bound the gap: reach far
    # enough and the next line up is the weight/height row, and reading that as
    # though it were the resource row is exactly the confident-but-wrong answer
    # this pipeline exists to avoid.
    above = [wd for wd in words if label_y - wd[1] > tolerance]
    if not above:
        return None
    value_y = max(wd[1] for wd in above)
    if label_y - value_y > h * _RESOURCE_LINE_GAP:
        return None

    # Re-read the numbers with the tighter threshold. The loose mask that makes
    # the grey labels legible also keeps the stardust and candy icons, and
    # tesseract reads those as digits welded to the figure beside them -- a
    # 521,865 came back as "1521,865" and a 1,645 as "4,645". The numerals are
    # darker than the labels, so a second pass over the one line now known to
    # hold them gets both right where no single threshold can.
    # Pad generously. Cropped to the glyphs themselves tesseract reads the
    # thousands comma as a period; given room around the line it reads it as a
    # comma. The line pitch is the natural amount of room to give it.
    margin = h * _RESOURCE_LINE_GAP * 0.6
    strip_top = top + int(value_y - margin)
    strip_bottom = top + int(value_y + margin)
    # _words_xy, not _words, so both lines are measured on the same scale.
    # _words divides x by an upscale factor it does not itself apply, which is
    # consistent within the fixed-band path because both its lines are read that
    # way -- and silently wrong here, where the labels came back in real pixels
    # and the numbers at a third of that. Pairing them put the candy count in
    # the stardust column.
    values = [(x, token) for x, _, token in _words_xy(
        _text_mask(img, _VALUE_MAX_VALUE)[max(strip_top, 0):min(strip_bottom, h), :])]
    if not values:
        return None

    return values, labels


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

    # The band missed the row. Before falling back to the overlay reading -- which
    # gives up the stardust and XL columns -- try locating the row by its labels.
    # This is the ordinary case on a screen shaped differently from the one the
    # band was calibrated on, and there the full row is still perfectly legible.
    located = locate_resource_row(img)
    if located is not None:
        row = parse_resource_row(*located)
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
        value = resource_number(token.strip("+-.'|\"_*~ "))
        if value is not None:
            numbers.append((x, value))

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


# --------------------------------------------------------------------------
# CP: several readings, then let the arithmetic choose
# --------------------------------------------------------------------------

def cp_candidates(img, band: tuple[float, float] = (0.02, 0.16)) -> list[int]:
    """Every number the CP area might be, across several preprocessings.

    Deliberately generous and deliberately unfiltered. Tesseract misreads this
    font -- CP252 came back as CP292 off a clean tight crop -- and no single
    preprocessing was reliable across devices: a threshold that read three of
    five frames read zero of five when nudged by one percent of screen height.

    So this stops trying to be right and tries to be complete instead. It
    proposes; resolve.verify_cp disposes, by asking which of these the species,
    IVs and HP can actually produce. A wrong candidate in this list is harmless.
    A missing one is not.
    """
    _require("cv2", "opencv-python-headless")
    _require("pytesseract", "pytesseract")
    import cv2
    import numpy as np
    import pytesseract

    h, w = img.shape[:2]
    top = img[int(h * band[0]):int(h * band[1]), :]
    grey = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)

    variants = []
    # CP is drawn in white; on a bright background that mask is useless, which
    # is why the others are here too.
    white = ((hsv[:, :, 2] > 200) & (hsv[:, :, 1] < 60)).astype(np.uint8) * 255
    variants.append(255 - white)
    variants.append(_strokes(grey))
    _, otsu = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(otsu)
    variants.append(255 - otsu)

    found: list[int] = []
    for variant in variants:
        big = cv2.resize(variant, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        for psm in (6, 7):
            try:
                text = pytesseract.image_to_string(big, config=f"--psm {psm}")
            except Exception:
                continue
            for token in re.findall(r"\d{2,5}", text.replace("O", "0").replace("o", "0")):
                value = int(token)
                if 10 <= value <= 6000 and value not in found:
                    found.append(value)
    return found
