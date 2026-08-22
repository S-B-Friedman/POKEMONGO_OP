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


# --------------------------------------------------------------------------
# The resource row on a Pokemon's detail screen
# --------------------------------------------------------------------------
#
# STARDUST | <SPECIES> CANDY | <SPECIES> CANDY XL, each a number over a label.
# This is the only place the game shows per-species candy, and no export
# carries it -- a Poke Genie CSV describes Pokemon, not the bag -- so it is the
# one screen that can close the gap between "the solver has a collection" and
# "the solver knows what you can afford".

# How far a number may sit from its column heading, in original pixels. Wide
# enough for a long value like 521,865 whose left edge is well left of the
# label's, tight enough to exclude specks at the strip's edges.
_RESOURCE_COLUMN_TOLERANCE = 200


@dataclass(frozen=True)
class ResourceRow:
    """What one detail screen says about the trainer's resources."""

    species: str | None = None
    stardust: int | None = None
    candy: int | None = None
    xl_candy: int | None = None

    @property
    def usable(self) -> bool:
        """Whether this is worth writing into a candy inventory.

        Stardust alone is not: it is a single global number the user already
        knows. The species and its candy count are the part nothing else has.
        """
        return bool(self.species) and self.candy is not None


# A number as the game actually renders it in the resource row: either grouped
# in threes with a separator, or a bare run short enough not to need one.
#
# The grouping is worth checking because it catches two OCR failures that a
# permissive \d[\d,]* waves through. The stardust icon welds a digit onto the
# figure beside it -- 521,865 read as "1521,865" -- and that has a separator in
# the wrong place, so requiring correct grouping rejects it instead of banking a
# stardust budget three times the real one. Upscaled thin strips also turn the
# comma into a period, and since nothing in this row is a fractional quantity,
# "1.645" can only be 1,645. The same rule that rejects the first accepts the
# second, and rejects "3.34" off the weight line either way.
_RESOURCE_NUMBER = re.compile(r"\d{1,3}(?:[.,]\d{3})+|\d{1,4}")


def resource_number(token: str) -> int | None:
    """-> the integer a resource-row token denotes, or None if it isn't one."""
    token = token.strip().strip("+-'|\"_*~ ")
    if not _RESOURCE_NUMBER.fullmatch(token):
        return None
    return int(token.replace(",", "").replace(".", ""))


def _strip_label_words(word: str) -> str:
    """Drop the fixed part of a resource label, leaving the species name.

    "SWINUBCANDYXL" -> "SWINUB". OCR merges these tokens unpredictably, so the
    species has to be recovered from whatever it ran together rather than from a
    token that happens to stand alone.
    """
    for fixed in ("XL", "CANDY", "STARDUST"):
        word = word.replace(fixed, "")
    return word


def parse_resource_row(
    values: Sequence[tuple[int, str]],
    labels: Sequence[tuple[int, str]],
) -> ResourceRow:
    """Pair OCR'd numbers with OCR'd labels by horizontal position.

    Both arguments are (x, text) as read off the screen. Pairing on x rather
    than on order matters because the columns are not evenly spaced and the OCR
    drops a token now and then; position survives both.

    The label text also carries the species name, which is what makes the row
    worth reading at all -- "SWINUB CANDY" identifies the stock as well as
    naming it.
    """
    groups: dict[str, list[tuple[int, str]]] = {}
    for x, text in labels:
        word = re.sub(r"[^A-Z]", "", text.upper())
        if not word:
            continue
        groups.setdefault(word, []).append((x, text))

    # Reconstruct each column from its words: STARDUST stands alone; the candy
    # columns are "<NAME> CANDY" and "<NAME> CANDY XL", which share a name.
    columns: list[tuple[int, str]] = []
    xs_by_word = {w: [x for x, _ in v] for w, v in groups.items()}

    if "STARDUST" in xs_by_word:
        columns.append((min(xs_by_word["STARDUST"]), "stardust"))

    species = None
    for word in xs_by_word:
        stripped = _strip_label_words(word)
        if stripped and len(stripped) > 2:
            species = stripped.title()
            break

    # Any word CONTAINING "CANDY" marks a candy column, not only a word that IS
    # "CANDY". OCR runs the label together as often as not -- on the frame this
    # was traced from it read the candy column as one "SWINUBCANDY" token and
    # the XL column as "SWINUB" + "CANDY", so matching on equality saw a single
    # candy column, took it for the ordinary one, and reported Swinub's 293 XL
    # candy as 293 candy. Confidently wrong, which is the one output this must
    # never produce.
    candy_xs = sorted(x for word, xs in xs_by_word.items() if "CANDY" in word
                      for x in xs)
    xl_xs = sorted(x for word, xs in xs_by_word.items() if "XL" in word
                   for x in xs)
    # The XL column is the rightmost. OCR may read "CANDY" twice, or read the
    # "XL" and miss the second "CANDY", so accept either as evidence of it.
    xl_candidates = ([candy_xs[-1]] if len(candy_xs) > 1 else []) + xl_xs
    xl_x = max(xl_candidates) if xl_candidates else None
    # A lone candy column that carries the XL mark is the XL column, and there
    # is then no ordinary candy count on offer. Naming it "candy" would be the
    # same mistake one step further along.
    plain_candy = [x for x in candy_xs if xl_x is None or x < xl_x]
    if plain_candy:
        columns.append((plain_candy[0], "candy"))
    if xl_x is not None:
        columns.append((xl_x, "xl_candy"))

    if not columns:
        return ResourceRow()

    numbers = [(x, value)
               for x, t in values
               if (value := resource_number(t)) is not None]

    # Assign per COLUMN, taking the nearest number to each -- not per number,
    # taking the nearest column. The difference matters: OCR leaves specks at
    # the extreme edges of the strip, and iterating numbers let a stray "7" at
    # x=0 claim the stardust column before the real 521,865 was considered.
    found: dict[str, int] = {}
    claimed: set[int] = set()
    for col_x, field_name in columns:
        candidates = [
            (abs(col_x - x), i, value)
            for i, (x, value) in enumerate(numbers)
            if i not in claimed and abs(col_x - x) <= _RESOURCE_COLUMN_TOLERANCE
        ]
        if not candidates:
            continue
        _, i, value = min(candidates)
        claimed.add(i)
        found[field_name] = value

    return ResourceRow(
        species=species,
        stardust=found.get("stardust"),
        candy=found.get("candy"),
        xl_candy=found.get("xl_candy"),
    )


# Shortest label prefix accepted as naming a species. "//HCANDY" is a half-read
# XL label whose prefix is a lone "H", and a single letter is a substring of
# almost any species name -- it matched "Anorith" and handed back the XL count
# as though it were candy. Four characters is enough to be evidence.
_MIN_SPECIES_PREFIX = 4


def find_candy_anchor(
    labels: Sequence[tuple[int, str]], species: str, family: str | None = None
) -> int | None:
    """x of the regular-candy column, located by its label rather than a position.

    The label reads as one run -- "PIKACHUCANDY", "ANORITHCAN" -- so it both
    marks the column and names the species, and that name is checked against the
    one read elsewhere on the screen.

    Position would have been simpler and wrong. Mega-capable Pokemon carry an
    extra Mega Energy element that shifts this row, so a hardcoded column
    fraction reads the wrong number for exactly those Pokemon -- and they are
    disproportionately the ones worth investing in.
    """
    # The label names the FAMILY, not the Pokemon: a Garchomp's screen reads
    # "GIBLE CANDY". Matching against the species alone fails for every evolved
    # Pokemon there is -- the base forms this was built against, Swinub and
    # Anorith and Palkia, are their own families, which is why it looked right.
    candidates = {re.sub(r"[^a-z]", "", (n or "").lower())
                  for n in (family, species)}
    candidates.discard("")
    if not candidates:
        return None

    anchors = []
    for x, token in labels:
        letters = re.sub(r"[^A-Z]", "", token.upper())
        if "CAN" not in letters:
            continue
        prefix = letters.split("CAN")[0].lower()
        # A bare "CANDY" is the XL column's second word, not a candy label.
        if len(prefix) < _MIN_SPECIES_PREFIX:
            continue
        head = prefix[:_MIN_SPECIES_PREFIX]
        if any(w.startswith(head) or head in w for w in candidates):
            anchors.append(x)

    return min(anchors) if anchors else None
