"""Species and move reference data, from GAME_MASTER.

Why this exists: `sample_data/collection.csv` is pre-joined, so every row
already carries base stats, typing and move stats. That is fine for a demo and
useless for an import -- a real collection contains species the sample has
never seen, and nothing in the repo could supply their numbers.

Everything is keyed on `normalize()`, which strips to lowercase alphanumerics,
so "Dragon Tail", "DRAGON_TAIL_FAST" and "dragon-tail" all resolve. That is
what makes matching robust against whatever an external app calls things.

Rebuild with `python scripts/build_reference.py`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REFERENCE_PATH = Path(__file__).parent / "data" / "reference.json"


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


@dataclass(frozen=True)
class Species:
    dex: int
    name: str
    form: str
    base_attack: int
    base_defense: int
    base_stamina: int
    type1: str
    type2: str | None
    mega: bool
    # The evolution family's base form. Candy is pooled per family and the game
    # labels it that way -- a Garchomp's screen reads "GIBLE CANDY" -- so this
    # is what a candy label has to be matched against, and what a candy stock is
    # really keyed by. Defaulted so a reference.json built before this field
    # existed still loads.
    family: str = ""


@dataclass(frozen=True)
class Move:
    name: str
    kind: str          # "fast" | "charge"
    type: str
    power: int
    energy: int        # magnitude; gained for fast, spent for charge
    duration: float    # seconds


class Reference:
    """Loaded GAME_MASTER slice. Cheap to construct, so pass it around."""

    def __init__(self, payload: dict):
        self._species = {k: Species(**v) for k, v in payload["species"].items()}
        self._moves = {k: Move(**v) for k, v in payload["moves"].items()}
        self.costs = payload.get("costs", {})
        self.source = payload.get("source", "unknown")

    @classmethod
    def load(cls, path: Path | str = REFERENCE_PATH) -> "Reference":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"{path} is missing. Build it first:\n"
                f"    python scripts/build_reference.py"
            )
        return cls(json.loads(path.read_text(encoding="utf-8")))

    # ------------------------------------------------------------- lookup

    def species(self, name: str, form: str | None = None) -> Species | None:
        """Resolve a display name (+ optional form) to a species.

        Tries the form-qualified key first so 'Exeggutor' + 'Alola' beats plain
        Exeggutor, then falls back to the base species. Returning None rather
        than raising is deliberate: an import should report what it could not
        resolve, not abort on the first unfamiliar row.
        """
        if form:
            hit = self._species.get(normalize(name) + normalize(form))
            if hit:
                return hit
        return self._species.get(normalize(name))

    def move(self, name: str, kind: str | None = None) -> Move | None:
        hit = self._moves.get(normalize(name))
        if hit and kind and hit.kind != kind:
            return None
        return hit

    def family_of(self, dex: int) -> str | None:
        """The evolution family a dex number belongs to.

        Candy is pooled per family, so this is the real key for a candy stock.
        Two Pokemon with different dex numbers and the same family -- a Gible
        and a Garchomp -- spend from one pile.
        """
        for s in self._species.values():
            if s.dex == dex and s.family:
                return s.family
        return None

    def family_by_dex(self) -> dict[int, str]:
        """{dex: family} for every species that has one."""
        return {s.dex: s.family for s in self._species.values() if s.family}

    def __len__(self) -> int:
        return len(self._species)


@lru_cache(maxsize=1)
def default_reference() -> Reference:
    return Reference.load()
