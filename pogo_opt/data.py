"""
Loading the collection.

Two interchangeable sources:

  * MySQL  -- the original POGOR schema. Credentials come from the environment,
              never from source. See .env.example.
  * CSV    -- bundled sample files, so the project runs end-to-end with no
              database. This is what makes the repo reviewable by someone who
              is not you.

Both produce the same list of `PokemonInstance` records, so the model layer
does not care which was used.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PokemonInstance:
    """One Pokemon you actually own."""

    instance_id: str          # unique per owned Pokemon, NOT per species
    species_id: int
    name: str
    level: float
    attack_iv: int
    defense_iv: int
    stamina_iv: int
    base_attack: int
    base_defense: int
    base_stamina: int
    fast_move: str
    fast_power: int
    fast_duration: float      # seconds
    fast_energy: int
    fast_type: str
    charge_move: str
    charge_power: int
    charge_duration: float    # seconds
    charge_energy: int
    charge_type: str
    type1: str
    type2: str | None = None
    is_shadow: bool = False
    is_lucky: bool = False
    is_purified: bool = False

    @property
    def friendship(self) -> frozenset[str]:
        """Every cost-affecting state this Pokemon is in.

        A set, not one label. These flags are independent -- a purified Pokemon
        that was later traded is both purified and lucky -- and returning a
        single string meant picking one by precedence and silently discarding
        the rest. `lucky` outranked `purified`, so a lucky purified Pokemon was
        priced without its 10% discount on either resource.

        `costs.step_cost` accepts either form, so callers passing a plain string
        still work.
        """
        states = set()
        if self.is_lucky:
            states.add("lucky")
        if self.is_shadow:
            states.add("shadow")
        if self.is_purified:
            states.add("purified")
        return frozenset(states or {"normal"})


def _as_bool(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y", "t"}


def _as_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _as_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# CSV source
# --------------------------------------------------------------------------

def load_from_csv(path: str | Path) -> list[PokemonInstance]:
    """Load a pre-joined collection from a single flat CSV."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no such CSV: {path}")

    out: list[PokemonInstance] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            out.append(
                PokemonInstance(
                    instance_id=row.get("instance_id") or f"row{i}",
                    species_id=_as_int(row["species_id"]),
                    name=row["name"],
                    level=_as_float(row["level"], 1.0),
                    attack_iv=_as_int(row["attack_iv"]),
                    defense_iv=_as_int(row["defense_iv"]),
                    stamina_iv=_as_int(row["stamina_iv"]),
                    base_attack=_as_int(row["base_attack"]),
                    base_defense=_as_int(row["base_defense"]),
                    base_stamina=_as_int(row["base_stamina"]),
                    fast_move=row["fast_move"],
                    fast_power=_as_int(row["fast_power"]),
                    fast_duration=_as_float(row["fast_duration"], 1.0),
                    fast_energy=_as_int(row["fast_energy"]),
                    fast_type=row["fast_type"],
                    charge_move=row["charge_move"],
                    charge_power=_as_int(row["charge_power"]),
                    charge_duration=_as_float(row["charge_duration"], 1.0),
                    charge_energy=_as_int(row["charge_energy"]),
                    charge_type=row["charge_type"],
                    type1=row["type1"],
                    type2=(row.get("type2") or "").strip() or None,
                    is_shadow=_as_bool(row.get("is_shadow")),
                    is_lucky=_as_bool(row.get("is_lucky")),
                    is_purified=_as_bool(row.get("is_purified")),
                )
            )
    return out


# --------------------------------------------------------------------------
# MySQL source
# --------------------------------------------------------------------------

_QUERY = """
SELECT
    mp.pokemon_id                AS instance_id,
    ss.species_id                AS species_id,
    ss.name                      AS name,
    mp.level                     AS level,
    mp.attack_iv, mp.defense_iv, mp.stamina_iv,
    ss.base_attack, ss.base_defense, ss.base_stamina,
    ss.type1, ss.type2,
    mp.Shadow                    AS is_shadow,
    mp.Lucky                     AS is_lucky,
    mp.fast_move,
    fm.power                     AS fast_power,
    fm.duration                  AS fast_duration,
    fm.energy                    AS fast_energy,
    fm.type                      AS fast_type,
    mp.charge_move,
    cm.power                     AS charge_power,
    cm.CD                        AS charge_duration,
    cm.EPS                       AS charge_energy,
    cm.type                      AS charge_type
FROM my_pokemon AS mp
JOIN species_stats     AS ss ON mp.id = ss.species_id
JOIN fast_move_stats   AS fm ON LOWER(mp.fast_move)   = LOWER(fm.Move)
JOIN charge_move_stats AS cm ON LOWER(mp.charge_move) = LOWER(cm.Move)
"""


def db_config_from_env() -> dict:
    """
    Credentials from the environment only.

    The original committed a live MySQL root password into a public repo.
    Nothing here reads a secret from source.
    """
    missing = [k for k in ("POGO_DB_USER", "POGO_DB_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "missing environment variables: "
            + ", ".join(missing)
            + " (copy .env.example to .env and fill it in)"
        )
    return {
        "host": os.environ.get("POGO_DB_HOST", "localhost"),
        "port": int(os.environ.get("POGO_DB_PORT", "3306")),
        "user": os.environ["POGO_DB_USER"],
        "password": os.environ["POGO_DB_PASSWORD"],
        "database": os.environ.get("POGO_DB_NAME", "POGOR"),
    }


def load_from_mysql(config: dict | None = None) -> list[PokemonInstance]:
    try:
        import mysql.connector as mysql
    except ImportError as exc:
        raise RuntimeError(
            "mysql-connector-python is not installed; "
            "run with --source csv or pip install -r requirements.txt"
        ) from exc

    config = config or db_config_from_env()
    conn = mysql.connect(**config)
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(_QUERY)
        rows = cursor.fetchall()
    finally:
        conn.close()

    out: list[PokemonInstance] = []
    for i, r in enumerate(rows):
        out.append(
            PokemonInstance(
                instance_id=str(r.get("instance_id") or f"row{i}"),
                species_id=_as_int(r["species_id"]),
                name=str(r.get("name") or f"species_{r['species_id']}"),
                level=_as_float(r["level"], 1.0),
                attack_iv=_as_int(r["attack_iv"]),
                defense_iv=_as_int(r["defense_iv"]),
                stamina_iv=_as_int(r["stamina_iv"]),
                base_attack=_as_int(r["base_attack"]),
                base_defense=_as_int(r["base_defense"]),
                base_stamina=_as_int(r["base_stamina"]),
                fast_move=str(r["fast_move"]),
                fast_power=_as_int(r["fast_power"]),
                fast_duration=_as_float(r["fast_duration"], 1.0),
                fast_energy=_as_int(r["fast_energy"]),
                fast_type=str(r["fast_type"]),
                charge_move=str(r["charge_move"]),
                charge_power=_as_int(r["charge_power"]),
                charge_duration=_as_float(r["charge_duration"], 1.0),
                charge_energy=_as_int(r["charge_energy"]),
                charge_type=str(r["charge_type"]),
                type1=str(r["type1"]),
                type2=(str(r["type2"]) if r.get("type2") else None),
                is_shadow=_as_bool(r.get("is_shadow")),
                is_lucky=_as_bool(r.get("is_lucky")),
            )
        )
    return out


def load_candy_inventory(
    path: str | Path | None,
) -> tuple[dict[int, int], dict[int, int]]:
    """Return (candy, xl_candy) keyed by species_id.

    Absent species are treated as unconstrained. XL candy is a genuinely
    separate resource -- it cannot be spent as regular candy and vice versa,
    so it gets its own inventory rather than being derived from the candy
    count.
    """
    if path is None:
        return {}, {}
    path = Path(path)
    if not path.exists():
        return {}, {}

    # Keys are evolution families where one is known, because that is how the
    # game pools candy -- a Gible and a Garchomp spend from one pile. The file
    # is still written with species_id, which is what a person reads off their
    # own screen; the mapping happens here so nobody has to know about it.
    from .model import candy_pool_key

    candy: dict = {}
    xl: dict = {}
    conflicts: list[str] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sid = _as_int(row["species_id"])
            pool = candy_pool_key(sid)
            value = _as_int(row["candy"])
            # Two rows for one family should agree -- they describe one pile.
            # Disagreement means the file is wrong, so say so rather than
            # letting whichever row came last decide.
            if pool in candy and candy[pool] != value:
                conflicts.append(f"{pool}: {candy[pool]} vs {value}")
            candy[pool] = value
            if row.get("xl_candy") not in (None, ""):
                xl[pool] = _as_int(row["xl_candy"])

    if conflicts:
        raise ValueError(
            "candy inventory disagrees with itself. Candy is pooled per "
            "evolution family, so every species in a family must show the same "
            "count: " + "; ".join(conflicts)
        )
    return candy, xl
