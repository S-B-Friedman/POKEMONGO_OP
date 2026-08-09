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

STATUS: the parsing layer (pogo_opt/ingest.py) is unit-tested. This file --
the image and video handling -- is NOT, because it needs real screenshots and
a Vision or Tesseract install. Treat the default bar regions as a starting
point and check them with --calibrate before trusting any IV reading.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from pogo_opt.ingest import BarLayout, ScannedPokemon, build_record

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


def read_bar_fills(image_path: Path, layout: BarLayout) -> dict[str, float]:
    """Fraction of each appraisal bar that is filled."""
    _require("cv2", "opencv-python-headless")
    import cv2
    import numpy as np

    img = cv2.imread(str(image_path))
    if img is None:
        raise RuntimeError(f"could not read image: {image_path}")

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, threshed = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY_INV)

    h, w = threshed.shape
    fills: dict[str, float] = {}
    for stat, (y0, y1, x0, x1) in layout.regions(h, w).items():
        region = threshed[y0:y1, x0:x1]
        if region.size == 0:
            continue
        fills[stat] = float(np.count_nonzero(region) / region.size)
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
