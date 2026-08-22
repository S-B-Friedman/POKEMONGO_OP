"""Resolving a swipe-through into records.

Capture model: the Appraise panel is left open and you swipe. The panel stays
up, so every Pokemon shows its three IV bars directly -- IVs are read, not
inferred. What the panel does NOT show is the numeric level, so that becomes
the unknown, and CP plus known IVs pins it exactly.

The other thing the capture model gives you is redundancy. Three seconds per
Pokemon at 30fps is ~90 frames of the same screen. Single-frame OCR is noisy;
90 frames of the same screen is not. Everything here is built to exploit that:
segment the video into stable runs, then vote within each run.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from typing import Iterable

from .costs import MAX_LEVEL, MIN_LEVEL, STEP, cp_multiplier

IV_MAX = 15


# --------------------------------------------------------------------------
# Level from CP
# --------------------------------------------------------------------------

def cp_at(
    base_attack: int, base_defense: int, base_stamina: int,
    attack_iv: int, defense_iv: int, stamina_iv: int,
    level: float,
) -> int:
    """CP as the game computes it: floor(Atk * sqrt(Def) * sqrt(Sta) * CPM^2 / 10)."""
    cpm = cp_multiplier(level)
    atk = base_attack + attack_iv
    dfn = base_defense + defense_iv
    sta = base_stamina + stamina_iv
    return max(10, math.floor(atk * math.sqrt(dfn) * math.sqrt(sta) * cpm * cpm / 10))


def hp_at(base_stamina: int, stamina_iv: int, level: float) -> int:
    """Max HP: floor((base_stamina + IV) * CPM), minimum 10."""
    return max(10, math.floor((base_stamina + stamina_iv) * cp_multiplier(level)))


def all_levels() -> list[float]:
    n = int((MAX_LEVEL - MIN_LEVEL) / STEP) + 1
    return [round(MIN_LEVEL + i * STEP, 1) for i in range(n)]


def levels_from_cp(
    base_attack: int, base_defense: int, base_stamina: int,
    attack_iv: int, defense_iv: int, stamina_iv: int,
    cp: int,
    hp: int | None = None,
) -> list[float]:
    """Every level consistent with the observed CP (and HP, if read).

    Usually returns exactly one. CP is strictly increasing in level, but it's
    floored to an integer, so at low levels adjacent half-levels can collapse
    to the same CP. Returning a list rather than a single value keeps that
    ambiguity visible instead of silently picking the first match.
    """
    out = []
    for level in all_levels():
        if cp_at(base_attack, base_defense, base_stamina,
                 attack_iv, defense_iv, stamina_iv, level) != cp:
            continue
        if hp is not None and hp_at(base_stamina, stamina_iv, level) != hp:
            continue
        out.append(level)
    return out


def star_tier(attack_iv: int, defense_iv: int, stamina_iv: int) -> int:
    """Appraisal star rating, 0-4, from the IV total out of 45.

    Matches the in-game `0*`..`4*` search terms, which is how a filtered scan
    pass can be cross-checked against the bars.
    """
    pct = (attack_iv + defense_iv + stamina_iv) / (3 * IV_MAX)
    if pct == 1.0:
        return 4
    if pct >= 0.8222:
        return 3
    if pct >= 0.6666:
        return 2
    if pct >= 0.5111:
        return 1
    return 0


def iv_floor_for(is_lucky: bool = False, is_purified: bool = False) -> int:
    """Minimum possible value for each IV given provenance.

    Lucky trades guarantee 12+ in every stat; purification adds +2 to each IV,
    so a purified Pokemon cannot have an IV below 2. Both are strong priors
    when a bar read is borderline.
    """
    if is_lucky:
        return 12
    if is_purified:
        return 2
    return 0


# --------------------------------------------------------------------------
# Segmenting a swipe-through
# --------------------------------------------------------------------------

@dataclass
class Run:
    """A stretch of frames showing the same Pokemon."""

    start: int
    end: int          # inclusive

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def sample(self, k: int) -> list[int]:
        """k frame indices spread across the run, avoiding the edges where the
        swipe animation is still settling."""
        if self.length <= k:
            return list(range(self.start, self.end + 1))
        margin = max(1, self.length // 6)
        lo, hi = self.start + margin, self.end - margin
        if hi <= lo:
            lo, hi = self.start, self.end
        step = (hi - lo) / max(1, k - 1)
        return [int(round(lo + i * step)) for i in range(k)]


def segment_runs(
    diffs: list[float],
    threshold: float = 8.0,
    min_run: int = 5,
) -> list[Run]:
    """Split a frame sequence into per-Pokemon runs.

    `diffs[i]` is how different frame i is from frame i-1 (so it has one fewer
    entry than the frame list, or a leading 0). During a swipe the screen
    changes a lot; between swipes it's static. Runs of low difference are the
    settled frames for one Pokemon.

    Pure function over difference scores, so it's testable without any video.
    """
    if not diffs:
        return []

    runs: list[Run] = []
    start = 0
    for i, d in enumerate(diffs):
        if i == 0:
            continue
        if d > threshold:
            if i - 1 >= start:
                runs.append(Run(start, i - 1))
            start = i
    runs.append(Run(start, len(diffs) - 1))

    return [r for r in runs if r.length >= min_run]


# --------------------------------------------------------------------------
# Voting across a run
# --------------------------------------------------------------------------

@dataclass
class Vote:
    """Result of combining several reads of the same screen."""

    value: object
    agreement: float          # fraction of reads that agreed
    n: int                    # how many reads were counted

    @property
    def is_confident(self) -> bool:
        return self.n >= 3 and self.agreement >= 0.6


def vote_discrete(values: list) -> Vote | None:
    """Majority vote, ignoring Nones.

    This is where the redundancy pays: a single frame that reads CP as 245l
    instead of 2451 is outvoted by the eighty-nine frames that got it right.
    """
    present = [v for v in values if v is not None]
    if not present:
        return None
    counts = Counter(present)
    value, hits = counts.most_common(1)[0]
    return Vote(value, hits / len(present), len(present))


def vote_continuous(values: list[float | None], tolerance: float = 0.04) -> Vote | None:
    """Median for noisy floats, with agreement measured as the fraction of
    readings landing within `tolerance` of it.

    Bar fill ratios need this rather than a majority vote -- anti-aliasing
    means no two frames produce byte-identical fills, so exact-match voting
    would report zero agreement on perfectly good data.
    """
    present = [v for v in values if v is not None]
    if not present:
        return None
    mid = median(present)
    close = sum(1 for v in present if abs(v - mid) <= tolerance)
    return Vote(mid, close / len(present), len(present))


# --------------------------------------------------------------------------
# Assembled result
# --------------------------------------------------------------------------

@dataclass
class ResolvedPokemon:
    name: str | None = None
    cp: int | None = None
    hp: int | None = None
    attack_iv: int | None = None
    defense_iv: int | None = None
    stamina_iv: int | None = None
    level: float | None = None
    level_candidates: list[float] = field(default_factory=list)
    star_tier: int | None = None
    frames_used: int = 0
    agreement: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return None not in (
            self.name, self.cp, self.attack_iv, self.defense_iv,
            self.stamina_iv, self.level,
        )


def resolve_run(
    reads: list[dict],
    base_stats: dict[str, tuple[int, int, int]] | None = None,
    is_lucky: bool = False,
    is_purified: bool = False,
) -> ResolvedPokemon:
    """Combine several per-frame reads of one Pokemon into a single record.

    Each read is a dict with any of: name, cp, hp, attack_fill, defense_fill,
    stamina_fill. `base_stats` maps species name -> (atk, def, sta) and is only
    needed to solve for level.
    """
    rec = ResolvedPokemon(frames_used=len(reads))
    if not reads:
        rec.warnings.append("no frames in run")
        return rec

    name_vote = vote_discrete([r.get("name") for r in reads])
    cp_vote = vote_discrete([r.get("cp") for r in reads])
    hp_vote = vote_discrete([r.get("hp") for r in reads])

    if name_vote:
        rec.name = name_vote.value
        if not name_vote.is_confident:
            rec.warnings.append(f"name agreement only {name_vote.agreement:.0%}")
    else:
        rec.warnings.append("name not read in any frame")

    if cp_vote:
        rec.cp = cp_vote.value
        if not cp_vote.is_confident:
            rec.warnings.append(f"CP agreement only {cp_vote.agreement:.0%}")
    else:
        rec.warnings.append("CP not read in any frame")

    if hp_vote:
        rec.hp = hp_vote.value

    floor = iv_floor_for(is_lucky, is_purified)
    agreements = [v.agreement for v in (name_vote, cp_vote) if v]

    for key, attr in (
        ("attack_fill", "attack_iv"),
        ("defense_fill", "defense_iv"),
        ("stamina_fill", "stamina_iv"),
    ):
        fills = [r.get(key) for r in reads]
        v = vote_continuous(fills)
        if v is None:
            continue
        iv = max(floor, min(IV_MAX, round(float(v.value) * IV_MAX)))
        setattr(rec, attr, iv)
        agreements.append(v.agreement)
        if v.agreement < 0.6:
            rec.warnings.append(f"{attr} bar unstable across frames")

    if agreements:
        rec.agreement = round(sum(agreements) / len(agreements), 3)

    if None not in (rec.attack_iv, rec.defense_iv, rec.stamina_iv):
        rec.star_tier = star_tier(rec.attack_iv, rec.defense_iv, rec.stamina_iv)

    # Solve for level, the one thing the Appraise panel doesn't display.
    if base_stats and rec.name in (base_stats or {}) and rec.cp is not None \
            and None not in (rec.attack_iv, rec.defense_iv, rec.stamina_iv):
        ba, bd, bs = base_stats[rec.name]
        rec.level_candidates = levels_from_cp(
            ba, bd, bs, rec.attack_iv, rec.defense_iv, rec.stamina_iv, rec.cp, rec.hp
        )
        if len(rec.level_candidates) == 1:
            rec.level = rec.level_candidates[0]
        elif len(rec.level_candidates) > 1:
            rec.level = rec.level_candidates[0]
            rec.warnings.append(
                f"{len(rec.level_candidates)} levels fit this CP; took the lowest"
            )
        else:
            rec.warnings.append("no level fits this CP and IV combination")

    return rec


# --------------------------------------------------------------------------
# Letting arithmetic referee the OCR
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CPVerdict:
    """One CP candidate, and whether the game's own arithmetic accepts it."""

    cp: int
    levels: list[float]

    @property
    def valid(self) -> bool:
        return bool(self.levels)

    @property
    def unambiguous(self) -> bool:
        return len(self.levels) == 1


def verify_cp(
    base_attack: int, base_defense: int, base_stamina: int,
    attack_iv: int, defense_iv: int, stamina_iv: int,
    candidates: Iterable[int],
    hp: int,
) -> CPVerdict | None:
    """Pick the CP reading that the species, IVs and HP can actually produce.

    OCR proposes; this disposes. Tesseract does not merely fail to read the
    game's CP font -- it misreads it into other plausible numbers. On a clean,
    tightly cropped, high-contrast frame it returned CP292 for a Pokemon
    displaying CP252, and CP6274 for one displaying CP4627. Nothing downstream
    could tell those apart from a correct read, and a wrong CP silently pins the
    wrong level, which is the input everything else is built on.

    It cannot be tuned away, so it is checked instead. A CP is only accepted if
    some level reproduces both it and the observed HP for these IVs. Every
    misread above fails that, including one off by a single digit: for a Pikachu
    with 15/14/14 and 55 HP, 292 resolves to level 11 and 291 resolves to
    nothing at all.

    This needs IVs, which is why it belongs to the appraisal screen -- the one
    place that shows the bars and the CP together.

    Returns None when no candidate survives. That is a real answer: it means the
    frame was not read well enough to use, which is worth far more than a
    confident wrong number.
    """
    best: CPVerdict | None = None
    for cp in candidates:
        if cp is None or cp <= 0:
            continue
        levels = levels_from_cp(
            base_attack, base_defense, base_stamina,
            attack_iv, defense_iv, stamina_iv, int(cp), hp,
        )
        if not levels:
            continue
        verdict = CPVerdict(cp=int(cp), levels=levels)
        # An unambiguous reading beats one that leaves several levels open.
        if best is None or (verdict.unambiguous and not best.unambiguous):
            best = verdict
    return best
