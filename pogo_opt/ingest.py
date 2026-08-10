"""Parsing screenshot text into structured records.

Everything here is pure: strings and numbers in, dicts out. No image loading,
no API clients, no database. That's deliberate -- it means the parsing logic
is unit-testable without Vision credentials, Tesseract, or a phone, which is
what makes this part of the pipeline reviewable at all.

The image and video I/O lives in `ocr_ingest.py` at the repo root, isolated
behind lazy imports so this package installs without OpenCV.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict
from difflib import get_close_matches
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------
# Field extraction
# --------------------------------------------------------------------------

# OCR routinely reads O for 0, l/I for 1, S for 5. Applied only inside runs
# that should be numeric, never to whole lines -- otherwise Pokemon names get
# mangled.
_DIGIT_CONFUSIONS = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5"})

_PATTERNS: dict[str, re.Pattern[str]] = {
    "cp": re.compile(r"\bCP\s*([0-9OolIS]{1,5})\b", re.IGNORECASE),
    "hp": re.compile(r"\b([0-9OolIS]{1,4})\s*/\s*([0-9OolIS]{1,4})\s*HP\b", re.IGNORECASE),
    "power_up_stardust": re.compile(
        r"([0-9OolIS,\.]{2,9})\s*(?:\n|\s)*stardust", re.IGNORECASE
    ),
    "candy_count": re.compile(r"([0-9OolIS,\.]{1,6})\s*(?:\w+\s+)?cand(?:y|ies)", re.IGNORECASE),
    "date_caught": re.compile(r"caught on (\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE),
    "weight": re.compile(r"WEIGHT\s*([0-9OolIS\.]+)\s*kg", re.IGNORECASE),
    "height": re.compile(r"HEIGHT\s*([0-9OolIS\.]+)\s*m", re.IGNORECASE),
}

_LOCATION = re.compile(r"around ([^,\n]+),\s*([^,\n]+)(?:,\s*([^,\n]+))?", re.IGNORECASE)


def _to_int(raw: str) -> int | None:
    cleaned = raw.translate(_DIGIT_CONFUSIONS).replace(",", "").replace(".", "").strip()
    return int(cleaned) if cleaned.isdigit() else None


def _to_float(raw: str) -> float | None:
    cleaned = raw.translate(_DIGIT_CONFUSIONS).replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_fields(text: str) -> dict[str, Any]:
    """Pull every recognizable field out of one screenshot's OCR text.

    Missing fields are simply absent from the result rather than being set to
    a sentinel -- a Pokemon with no visible stardust cost is different from one
    that costs zero.
    """
    if not text:
        return {}

    text = unicodedata.normalize("NFKC", text)
    out: dict[str, Any] = {}

    if m := _PATTERNS["cp"].search(text):
        if (v := _to_int(m.group(1))) is not None:
            out["cp"] = v

    if m := _PATTERNS["hp"].search(text):
        cur, total = _to_int(m.group(1)), _to_int(m.group(2))
        if cur is not None and total is not None:
            # OCR sometimes drops a digit from one side; a current HP above
            # total is a misread, not a real state.
            if cur <= total:
                out["current_hp"], out["total_hp"] = cur, total

    for key in ("power_up_stardust", "candy_count"):
        if m := _PATTERNS[key].search(text):
            if (v := _to_int(m.group(1))) is not None:
                out[key] = v

    for key in ("weight", "height"):
        if m := _PATTERNS[key].search(text):
            if (v := _to_float(m.group(1))) is not None:
                out[key] = v

    if m := _PATTERNS["date_caught"].search(text):
        out["date_caught"] = m.group(1)

    if m := _LOCATION.search(text):
        out["city"] = m.group(1).strip()
        out["region"] = m.group(2).strip()
        if m.group(3):
            out["country"] = m.group(3).strip()

    lowered = text.lower()
    out["is_shadow"] = "shadow" in lowered
    out["is_purified"] = "purified" in lowered
    out["is_lucky"] = "lucky" in lowered

    return out


# --------------------------------------------------------------------------
# Name matching
# --------------------------------------------------------------------------

def match_species_name(text: str, known_names: Iterable[str], cutoff: float = 0.82) -> str | None:
    """Best-effort species name from noisy OCR text.

    Exact substring first, then fuzzy per-token. The original scanned for an
    exact substring against a mega-evolution list first, so any Pokemon that
    could mega-evolve was matched by that list and every other one fell through
    to a second loop -- and a misread like "Charlzard" matched nothing at all.
    """
    if not text:
        return None

    names = [n for n in known_names if n]
    if not names:
        return None

    upper = unicodedata.normalize("NFKC", text).upper()
    by_upper = {n.upper(): n for n in names}

    # Longest first, so BLASTOISE wins over any shorter name contained in it.
    for candidate in sorted(by_upper, key=len, reverse=True):
        if candidate in upper:
            return by_upper[candidate]

    tokens = re.findall(r"[A-Z][A-Z'\.\-]{2,}", upper)
    for token in tokens:
        hit = get_close_matches(token, list(by_upper), n=1, cutoff=cutoff)
        if hit:
            return by_upper[hit[0]]

    return None


# --------------------------------------------------------------------------
# IV estimation from the appraisal bars
# --------------------------------------------------------------------------

IV_MAX = 15

# The appraisal bar is drawn as three segments of five IV each, separated by
# small gaps. Those gaps are never filled, so measuring "filled pixels / total
# span" charges them against the fill and under-reads every stat.
#
# On the measured capture the gaps are 3.3% of the span, which is about half an
# IV -- small, but biased in one direction and worst at the top of the range,
# where it is exactly the difference between a 15 and a 14. Fill is therefore
# measured against the summed segment widths.
#
# (The much larger error on real screenshots was horizontal: the old default
# span ran to 0.945 of the width, roughly twice the bar, and read 11 where the
# truth was 14. See BarLayout.)
IV_BAR_SEGMENTS = 3
IV_PER_SEGMENT = IV_MAX // IV_BAR_SEGMENTS


def iv_from_bar_fill(fill_ratio: float) -> int:
    """Convert a 0..1 appraisal bar fill into an IV.

    Reading IVs off the bars is inherently lossy: the bar has 15 discrete
    positions but anti-aliasing and compression blur the edge, so this is an
    estimate. `iv_confidence` reports how close the reading was to a legal
    value, which is the number worth surfacing to the user.
    """
    if not 0.0 <= fill_ratio <= 1.0:
        raise ValueError(f"fill_ratio must be in [0, 1], got {fill_ratio}")
    return max(0, min(IV_MAX, round(fill_ratio * IV_MAX)))


@dataclass(frozen=True)
class BarReading:
    """One appraisal bar, measured against its segments rather than its span."""

    ratio: float
    segments: int
    filled_px: int
    track_px: int

    @property
    def plausible(self) -> bool:
        """Whether this looks like a real appraisal bar.

        Three segments is what the game draws. Anything else means the scanline
        missed the bar, caught the label text, or ran into the team leader
        standing over the right half of the card -- all of which produce a
        number that looks like an IV and is not one.
        """
        return self.segments == IV_BAR_SEGMENTS and self.track_px > 0


def contiguous_runs(
    flags: Sequence[bool], *, max_gap: int = 3, min_length: int = 20
) -> list[tuple[int, int]]:
    """Group truthy indices into (start, end) runs, bridging tiny gaps.

    `max_gap` absorbs anti-aliasing at segment ends; `min_length` drops specks
    so a stray pixel does not read as a fourth segment.
    """
    idx = [i for i, f in enumerate(flags) if f]
    if not idx:
        return []
    runs: list[list[int]] = [[idx[0], idx[0]]]
    for i in idx[1:]:
        if i - runs[-1][1] <= max_gap:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    return [(a, b) for a, b in runs if b - a + 1 >= min_length]


def find_bar_cluster(
    runs: Sequence[tuple[int, int]],
    *,
    width_tolerance: float = 0.30,
    max_gap_fraction: float = 0.25,
    min_width: int = 20,
) -> list[tuple[int, int]] | None:
    """Pick the three runs that look like one appraisal bar.

    Requiring a scanline to contain *exactly* three runs does not survive a real
    screenshot: the same row also crosses the team leader, the appraisal badge
    and whatever else shares that band, so the row reports five or six runs and
    the bar is missed entirely. Instead, look for three consecutive runs of
    similar width separated by gaps small relative to that width, which is what
    a segmented bar looks like and what arbitrary UI does not.
    """
    best: tuple[int, list[tuple[int, int]]] | None = None
    for i in range(len(runs) - 2):
        trio = list(runs[i:i + 3])
        widths = [b - a + 1 for a, b in trio]
        if min(widths) < min_width:
            continue
        if max(widths) / min(widths) > 1.0 + width_tolerance:
            continue
        gaps = [trio[1][0] - trio[0][1], trio[2][0] - trio[1][1]]
        if any(g < 1 or g > max_gap_fraction * min(widths) for g in gaps):
            continue
        span = trio[-1][1] - trio[0][0] + 1
        if best is None or span > best[0]:
            best = (span, trio)
    return best[1] if best else None


def bar_reading(
    fill: Sequence[bool],
    track: Sequence[bool],
    *,
    min_length: int = 20,
    cluster: bool = True,
) -> BarReading:
    """Measure one bar scanline.

    `fill` marks coloured (filled) pixels, `track` marks the bar's own pixels --
    filled or empty. Fill is measured against the summed segment widths, so the
    gaps between segments are excluded rather than counted as unfilled.
    """
    if len(fill) != len(track):
        raise ValueError("fill and track must be the same length")

    runs = contiguous_runs(track, min_length=min_length)
    if cluster and len(runs) > IV_BAR_SEGMENTS:
        runs = find_bar_cluster(runs) or runs

    total = sum(b - a + 1 for a, b in runs)
    filled = sum(1 for a, b in runs for x in range(a, b + 1) if fill[x])
    ratio = (filled / total) if total else 0.0
    return BarReading(ratio=min(1.0, ratio), segments=len(runs),
                      filled_px=filled, track_px=total)


def iv_confidence(fill_ratio: float) -> float:
    """1.0 when the reading sits exactly on a legal IV, 0.0 when maximally between.

    A value near 0.5 means the bar was read halfway between two IVs, which
    usually means the crop region is wrong -- worth flagging rather than
    silently rounding.
    """
    if not 0.0 <= fill_ratio <= 1.0:
        raise ValueError(f"fill_ratio must be in [0, 1], got {fill_ratio}")
    scaled = fill_ratio * IV_MAX
    return 1.0 - 2.0 * abs(scaled - round(scaled))


# --------------------------------------------------------------------------
# Bar regions, expressed as fractions of image size
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BarLayout:
    """Where the appraisal bars sit, as fractions of image height/width.

    The original hardcoded pixel rows (1620:1650, 100:1100), which only worked
    on one device at one resolution. Fractions survive a screenshot from any
    phone; `--calibrate` in ocr_ingest.py dumps the crops so they can be checked
    against a real screenshot.

    CALIBRATION. These were estimates against a synthetic screenshot until they
    were measured on a real 1206x2622 capture. The vertical positions were very
    nearly right (0.760/0.807/0.854 against a measured 0.767/0.809/0.851); the
    horizontal ones were not. `left`/`right` assumed the bars span the screen,
    but the appraisal card occupies only the lower left -- the team leader stands
    over the right half -- so the old span sampled mostly card background and
    the leader's shoulder.

    These remain one device at one resolution. Prefer `detect_bars()`, which
    finds the bars in the image and does not care about any of these numbers;
    this is the fallback for when detection fails.
    """

    attack_top: float = 0.767
    defense_top: float = 0.809
    hp_top: float = 0.851
    bar_height: float = 0.010
    left: float = 0.118
    right: float = 0.466

    def regions(self, height: int, width: int) -> dict[str, tuple[int, int, int, int]]:
        """-> {stat: (y0, y1, x0, x1)} in pixels for a given image size."""
        x0, x1 = int(self.left * width), int(self.right * width)
        dy = max(1, int(self.bar_height * height))
        return {
            stat: (int(top * height), int(top * height) + dy, x0, x1)
            for stat, top in (
                ("attack", self.attack_top),
                ("defense", self.defense_top),
                ("hp", self.hp_top),
            )
        }


# --------------------------------------------------------------------------
# Assembled record
# --------------------------------------------------------------------------

@dataclass
class ScannedPokemon:
    """One parsed screenshot. Deliberately permissive -- partial reads are kept
    and flagged rather than dropped, so a human can fix them in the CSV."""

    name: str | None = None
    cp: int | None = None
    current_hp: int | None = None
    total_hp: int | None = None
    attack_iv: int | None = None
    defense_iv: int | None = None
    stamina_iv: int | None = None
    iv_confidence: float | None = None
    power_up_stardust: int | None = None
    candy_count: int | None = None
    is_shadow: bool = False
    is_lucky: bool = False
    is_purified: bool = False
    date_caught: str | None = None
    source_frame: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """Enough to be worth writing out: we know what it is and how strong."""
        return self.name is not None and self.cp is not None

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["warnings"] = "; ".join(self.warnings)
        return row


def build_record(
    text: str,
    known_names: Iterable[str],
    bar_fills: dict[str, float] | None = None,
    source_frame: str | None = None,
) -> ScannedPokemon:
    """Combine text fields and bar readings into one record, with warnings."""
    fields = extract_fields(text)
    rec = ScannedPokemon(
        name=match_species_name(text, known_names),
        cp=fields.get("cp"),
        current_hp=fields.get("current_hp"),
        total_hp=fields.get("total_hp"),
        power_up_stardust=fields.get("power_up_stardust"),
        candy_count=fields.get("candy_count"),
        is_shadow=bool(fields.get("is_shadow")),
        is_lucky=bool(fields.get("is_lucky")),
        is_purified=bool(fields.get("is_purified")),
        date_caught=fields.get("date_caught"),
        source_frame=source_frame,
    )

    if rec.name is None:
        rec.warnings.append("species name not recognized")
    if rec.cp is None:
        rec.warnings.append("CP not found")

    if bar_fills:
        confidences = []
        for stat, attr in (
            ("attack", "attack_iv"),
            ("defense", "defense_iv"),
            ("hp", "stamina_iv"),
        ):
            if stat not in bar_fills:
                continue
            ratio = bar_fills[stat]
            setattr(rec, attr, iv_from_bar_fill(ratio))
            confidences.append(iv_confidence(ratio))
        if confidences:
            rec.iv_confidence = round(min(confidences), 3)
            if rec.iv_confidence < 0.5:
                rec.warnings.append(
                    f"low IV read confidence ({rec.iv_confidence}) -- check bar crop region"
                )

    return rec
