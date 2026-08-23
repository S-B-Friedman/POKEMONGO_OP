"""Turn a screenshot scan into a collection the solver can read.

`ocr_ingest.py` writes what it saw: a name, a CP, an HP and three IVs. The
solver wants a pre-joined row -- species id, base stats, typing, moves and a
LEVEL. Nothing bridged the two, so the scanner's closing line has always been
"map into the solver's collection.csv schema" and the mapping did not exist.
That left the OCR work ending one step short of being usable.

Two things have to be supplied here.

The level is not on the screen at all. It is recovered the same way the CP is
verified: for these IVs, find the level whose CP and HP both match what was
read. That is the same joint constraint used elsewhere in this codebase, and it
is tight -- a wrong level almost never reproduces both numbers.

The moveset is not on the appraisal screen either, and unlike the level it
cannot be derived. Rather than drop the Pokemon, it is rated with median move
stats and the row is marked, exactly as the Poke Genie importer does: base
stats, IVs and level are all known and correct, and only the rating scale is
uncertain. A Pokemon dropped for a missing moveset can never be recommended,
which is the worse error.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from ..data import PokemonInstance
from ..reference import default_reference
from ..resolve import levels_from_cp
from .pokegenie import _PLACEHOLDER_CHARGE, _PLACEHOLDER_FAST


@dataclass
class ScanImport:
    """What came out of a scan, and what could not be used."""

    collection: list[PokemonInstance] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    placeholder_moves: int = 0

    def summary(self) -> str:
        lines = [
            f"{len(self.collection)} Pokemon imported from the scan",
            f"  {self.placeholder_moves} rated with median move stats "
            f"(the appraisal screen does not show the moveset)",
        ]
        if self.skipped:
            lines.append(f"  {len(self.skipped)} skipped:")
            for label, why in self.skipped[:8]:
                lines.append(f"    {label}: {why}")
            if len(self.skipped) > 8:
                lines.append(f"    ... and {len(self.skipped) - 8} more")
        return "\n".join(lines)


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def import_scan(path: str | Path) -> ScanImport:
    """Read an ocr_ingest CSV into solver-ready instances."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no such scan CSV: {path}")

    ref = default_reference()
    out = ScanImport()

    with path.open(newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            label = row.get("name") or f"row {i}"
            name = (row.get("name") or "").strip()
            cp = _int(row.get("cp"))
            hp = _int(row.get("total_hp"))
            ivs = tuple(_int(row.get(k)) for k in
                        ("attack_iv", "defense_iv", "stamina_iv"))

            if not name:
                out.skipped.append((label, "no species name"))
                continue
            if cp is None or hp is None or None in ivs:
                missing = [n for n, v in (("CP", cp), ("HP", hp),
                                          ("IVs", None if None in ivs else 1))
                           if v is None]
                out.skipped.append((label, f"missing {', '.join(missing)}"))
                continue

            # Every form of the name, for the same reason CP verification needs
            # them: a screenshot says "Palkia" for the base species and for the
            # Origin Forme, and their base stats differ.
            level = None
            species = None
            for candidate in ref.forms(name):
                levels = levels_from_cp(
                    candidate.base_attack, candidate.base_defense,
                    candidate.base_stamina, ivs[0], ivs[1], ivs[2], cp, hp,
                )
                if levels:
                    species, level = candidate, levels[0]
                    break

            if species is None:
                out.skipped.append(
                    (label, f"no level reproduces CP {cp} with HP {hp} at "
                            f"{ivs[0]}/{ivs[1]}/{ivs[2]}")
                )
                continue

            out.placeholder_moves += 1
            out.collection.append(PokemonInstance(
                instance_id=row.get("source_frame") or f"scan{i}",
                species_id=species.dex,
                name=species.name,
                level=level,
                attack_iv=ivs[0], defense_iv=ivs[1], stamina_iv=ivs[2],
                base_attack=species.base_attack,
                base_defense=species.base_defense,
                base_stamina=species.base_stamina,
                fast_move="unknown?", fast_power=_PLACEHOLDER_FAST.power,
                fast_duration=_PLACEHOLDER_FAST.duration,
                fast_energy=_PLACEHOLDER_FAST.energy,
                fast_type=_PLACEHOLDER_FAST.type,
                charge_move="unknown?", charge_power=_PLACEHOLDER_CHARGE.power,
                charge_duration=_PLACEHOLDER_CHARGE.duration,
                charge_energy=_PLACEHOLDER_CHARGE.energy,
                charge_type=_PLACEHOLDER_CHARGE.type,
                type1=species.type1, type2=species.type2,
                is_shadow=str(row.get("is_shadow", "")).lower() == "true",
                is_lucky=str(row.get("is_lucky", "")).lower() == "true",
                is_purified=str(row.get("is_purified", "")).lower() == "true",
            ))

    return out
