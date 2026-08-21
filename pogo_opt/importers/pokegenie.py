"""Import a Poke Genie CSV export into the solver's record type.

Poke Genie's Scan Pro tier exports your whole scan history to CSV. It already
contains the two things that are genuinely hard to get from a screenshot --
resolved IVs and a resolved level -- so this is the shortest path from "a real
collection exists on my phone" to "the solver ran on it". The OCR pipeline in
this repo stays the interesting project; it stops being the blocker.

DESIGN NOTE
-----------
Poke Genie has changed its column names across versions and localizes some of
them, so nothing here matches on an exact header string. Columns are resolved
by normalized alias, and anything unresolved is REPORTED rather than defaulted.
A silently-defaulted level or IV produces a plan that looks fine and is wrong,
which is the same failure mode as a constraint that stops binding.

    from pogo_opt.importers.pokegenie import import_csv
    result = import_csv("pokegenie_export.csv")
    print(result.summary())
    collection = result.collection
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import math

from ..costs import cp_multiplier
from ..data import PokemonInstance
from ..reference import Move, Reference, default_reference, normalize

# Stand-in stats for a moveset the reference does not recognise -- a move added
# after this reference.json was built, or a name Poke Genie spells differently.
#
# These are the MEDIAN power, energy and duration across every fast and charge
# move in GAME_MASTER, so an unrecognised Pokemon is rated as an unremarkable
# one rather than a good or a terrible one. That is a real approximation and the
# row says so: its move name carries the original text and a "?", and the import
# records a warning.
#
# Dropping the row instead -- which is what this used to do, despite a comment
# claiming otherwise -- is worse. Base stats, IVs and level are all known and
# correct; only the rating scale is uncertain. Discarding a Pokemon entirely
# means it can never be recommended, and quietly shrinks the collection the
# caller thought they imported.
_PLACEHOLDER_FAST = Move(
    name="unknown", kind="fast", type="normal",
    power=10, energy=10, duration=1.0,
)
_PLACEHOLDER_CHARGE = Move(
    name="unknown", kind="charge", type="normal",
    power=65, energy=50, duration=2.5,
)

# Aliases per logical field, normalized. Poke Genie's own headers vary by
# version ("Atk IV" vs "Attack IV" vs "IV Attack"); extras cost nothing and
# make the importer survive an app update.
_ALIASES: dict[str, tuple[str, ...]] = {
    "index":       ("index", "scanid", "id"),
    "name":        ("name", "pokemon", "species", "pokemonname"),
    "form":        ("form", "formname", "variant"),
    "cp":          ("cp", "combatpower"),
    "hp":          ("hp", "maxhp"),
    "level":       ("level", "pokemonlevel", "lvl"),
    "level_min":   ("levelmin", "minlevel"),
    "level_max":   ("levelmax", "maxlevel"),
    "attack_iv":   ("atkiv", "attackiv", "ivattack", "atk", "attack"),
    "defense_iv":  ("defiv", "defenseiv", "ivdefense", "def", "defense"),
    "stamina_iv":  ("staiv", "staminaiv", "ivstamina", "hpiv", "ivhp", "sta", "stamina"),
    "fast_move":   ("quickmove", "fastmove", "quickattack", "fastattack", "move1"),
    "charge_move": ("chargemove", "chargedmove", "chargeattack", "move2", "chargemove1"),
    "shadow":      ("shadowpurified", "shadow", "shadowstatus"),
    "lucky":       ("lucky", "luckystatus"),
    "favorite":    ("favorite", "favourite"),
    "nickname":    ("nickname", "name2"),
}

_TRUE = {"1", "true", "yes", "y", "t", "lucky", "shadow"}


@dataclass
class ImportIssue:
    row: int
    name: str
    reason: str


@dataclass
class ImportResult:
    collection: list[PokemonInstance] = field(default_factory=list)
    skipped: list[ImportIssue] = field(default_factory=list)
    warnings: list[ImportIssue] = field(default_factory=list)
    columns: dict[str, str] = field(default_factory=dict)
    missing_columns: list[str] = field(default_factory=list)
    rows_read: int = 0

    def summary(self) -> str:
        lines = [
            f"{len(self.collection)} of {self.rows_read} rows imported, "
            f"{len(self.skipped)} skipped, {len(self.warnings)} imported with a warning."
        ]
        if self.missing_columns:
            lines.append("Unmapped required columns: " + ", ".join(self.missing_columns))
        reasons: dict[str, int] = {}
        for issue in self.skipped:
            reasons[issue.reason] = reasons.get(issue.reason, 0) + 1
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {n:>4}  {reason}")
        return "\n".join(lines)


def map_columns(headers: Iterable[str]) -> tuple[dict[str, str], list[str]]:
    """-> ({logical: actual header}, [required logical fields not found]).

    Exposed separately so a UI can show the mapping before committing an
    import, and so a user can correct it when Poke Genie renames something.
    """
    lookup = {normalize(h): h for h in headers if h}
    mapping: dict[str, str] = {}
    for logical, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                mapping[logical] = lookup[alias]
                break
    required = ["name", "attack_iv", "defense_iv", "stamina_iv"]
    missing = [f for f in required if f not in mapping]
    # Poke Genie exports Level Min / Level Max rather than a single level,
    # because CP alone does not always pin one. Either shape is acceptable.
    if "level" not in mapping and not ("level_min" in mapping and "level_max" in mapping):
        missing.append("level (or level_min + level_max)")
    return mapping, missing


def _num(row: dict[str, Any], mapping: dict[str, str], key: str):
    header = mapping.get(key)
    if header is None:
        return None
    raw = (row.get(header) or "").strip()
    if not raw:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _text(row: dict[str, Any], mapping: dict[str, str], key: str) -> str:
    header = mapping.get(key)
    return (row.get(header) or "").strip() if header else ""



def _cp_at(species, ivs: list[int], level: float) -> int:
    cpm = cp_multiplier(level)
    atk = species.base_attack + ivs[0]
    dfn = species.base_defense + ivs[1]
    sta = species.base_stamina + ivs[2]
    return max(10, math.floor(atk * math.sqrt(dfn) * math.sqrt(sta) * cpm * cpm / 10))


def _hp_at(species, stamina_iv: int, level: float) -> int:
    return max(10, math.floor((species.base_stamina + stamina_iv) * cp_multiplier(level)))


def _snap(level: float) -> float:
    return round(level * 2) / 2


def resolve_level(row, mapping, species, ivs, cp, hp) -> tuple[float | None, str | None]:
    """Pin the level, narrowing a Poke Genie min/max range with CP and HP.

    Poke Genie reports a range because CP is floored, so adjacent half-levels
    can share a CP. That is the same ambiguity resolve.levels_from_cp handles
    for the OCR path, and the same fix applies: recompute CP (and HP) across
    the range and keep the levels that reproduce what was scanned.

    Returns (level, note). A note means the level was picked under ambiguity
    and is worth surfacing, not that the row should be dropped.
    """
    exact = _num(row, mapping, "level")
    if exact is not None:
        return _snap(exact), None

    lo, hi = _num(row, mapping, "level_min"), _num(row, mapping, "level_max")
    if lo is None and hi is None:
        # No range at all -- but CP plus known IVs still pins the level, which
        # is the same solve the OCR path does. Search the whole ladder.
        if cp is None:
            return None, "no level and no CP to derive one from"
        lo, hi = 1.0, 50.0
    lo = _snap(lo if lo is not None else hi)
    hi = _snap(hi if hi is not None else lo)
    if hi < lo:
        lo, hi = hi, lo
    if lo == hi:
        return lo, None

    candidates = []
    lvl = lo
    while lvl <= hi + 1e-9:
        ok = True
        if cp is not None and _cp_at(species, ivs, lvl) != int(cp):
            ok = False
        if ok and hp is not None and _hp_at(species, ivs[2], lvl) != int(hp):
            ok = False
        if ok:
            candidates.append(lvl)
        lvl = round(lvl + 0.5, 1)

    if len(candidates) == 1:
        return candidates[0], None
    if candidates:
        return candidates[0], f"{len(candidates)} levels fit CP {int(cp)}; took the lowest"
    # Nothing in the range reproduces the scanned CP. Trust the range over the
    # derivation and say so -- a wrong level is a wrong cost curve.
    return lo, f"no level in {lo}-{hi} reproduces CP {cp}; took {lo}"


def build_instance(
    row: dict[str, Any],
    mapping: dict[str, str],
    ref: Reference,
    instance_id: str,
) -> PokemonInstance | str:
    """One row -> a PokemonInstance, or a string explaining why not."""
    name = _text(row, mapping, "name")
    if not name:
        return "no species name"

    species = ref.species(name, _text(row, mapping, "form") or None)
    if species is None:
        return f"species not in reference data ({name})"

    ivs = []
    for key in ("attack_iv", "defense_iv", "stamina_iv"):
        v = _num(row, mapping, key)
        if v is None:
            return "missing IVs (unappraised scan?)"
        if not 0 <= v <= 15:
            return f"IV {v} out of range"
        ivs.append(int(v))

    level, note = resolve_level(
        row, mapping, species, ivs,
        _num(row, mapping, "cp"), _num(row, mapping, "hp"),
    )
    if level is None:
        return note or "no level"
    if not 1.0 <= level <= 50.0:
        return f"level {level} out of range"

    # An unknown moveset is recoverable -- the rating falls back to a neutral
    # placeholder -- but it must be visible, not silent, so the row is tagged in
    # the move name itself rather than dropped.
    fast_text = _text(row, mapping, "fast_move")
    charge_text = _text(row, mapping, "charge_move")
    fast = ref.move(fast_text, kind="fast")
    charge = ref.move(charge_text, kind="charge")

    unknown_moves = []
    if fast is None:
        unknown_moves.append(fast_text or "(blank)")
        fast = _PLACEHOLDER_FAST
        fast_text = f"{fast_text or 'unknown'}?"
    else:
        fast_text = fast.name
    if charge is None:
        unknown_moves.append(charge_text or "(blank)")
        charge = _PLACEHOLDER_CHARGE
        charge_text = f"{charge_text or 'unknown'}?"
    else:
        charge_text = charge.name

    if unknown_moves:
        moveset_note = (
            f"moveset not recognized ({', '.join(unknown_moves)}); "
            f"rated with median move stats"
        )
        note = f"{note}; {moveset_note}" if note else moveset_note

    shadow_col = _text(row, mapping, "shadow").lower()
    is_shadow = "shadow" in shadow_col
    is_purified = "purified" in shadow_col
    is_lucky = _text(row, mapping, "lucky").lower() in _TRUE

    nickname = _text(row, mapping, "nickname")
    label = species.name if not species.form else f"{species.name} ({species.form})"

    instance = PokemonInstance(
        instance_id=instance_id,
        species_id=species.dex,
        name=nickname or label,
        level=level,
        attack_iv=ivs[0],
        defense_iv=ivs[1],
        stamina_iv=ivs[2],
        base_attack=species.base_attack,
        base_defense=species.base_defense,
        base_stamina=species.base_stamina,
        fast_move=fast_text,
        fast_power=fast.power,
        fast_duration=fast.duration,
        fast_energy=fast.energy,
        fast_type=fast.type,
        charge_move=charge_text,
        charge_power=charge.power,
        charge_duration=charge.duration,
        charge_energy=charge.energy,
        charge_type=charge.type,
        type1=species.type1,
        type2=species.type2,
        is_shadow=is_shadow,
        is_lucky=is_lucky,
        is_purified=is_purified,
    )
    return (instance, note) if note else instance


def import_rows(rows: Iterable[dict[str, Any]], headers: Iterable[str],
                ref: Reference | None = None) -> ImportResult:
    """Pure over already-parsed rows, so it is testable without a file."""
    ref = ref or default_reference()
    mapping, missing = map_columns(headers)
    result = ImportResult(columns=mapping, missing_columns=missing)

    if missing:
        return result

    for i, row in enumerate(rows, start=1):
        result.rows_read += 1
        built = build_instance(row, mapping, ref, instance_id=f"pg{i:05d}")
        name = _text(row, mapping, "name") or "?"
        if isinstance(built, str):
            result.skipped.append(ImportIssue(i, name, built))
        elif isinstance(built, tuple):
            instance, note = built
            result.collection.append(instance)
            result.warnings.append(ImportIssue(i, name, note))
        else:
            result.collection.append(built)
    return result


def import_csv(path: str | Path, ref: Reference | None = None) -> ImportResult:
    path = Path(path)
    # Poke Genie writes UTF-8 with a BOM on some platforms; utf-8-sig strips it
    # so the first header does not come through as "\ufeffIndex" and fail to map.
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        return import_rows(list(reader), headers, ref)
