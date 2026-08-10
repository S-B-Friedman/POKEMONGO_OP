"""SQLite access layer.

One file per collection, no server, trivially shareable. Every owned row is
scoped by trainer_id from the start so this can become a multi-user app later
without a painful migration.

Nothing here does OCR or optimization -- it stores what was scanned and hands
the solver a clean roster.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------

def connect(path: str | Path = "pogo.db") -> sqlite3.Connection:
    """Open (creating if needed) a collection database."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL keeps a long scan-write from blocking reads, which matters once a UI
    # is watching the table fill up during a swipe-through.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Apply the schema. Safe to call repeatedly."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


@contextmanager
def session(path: str | Path = "pogo.db") -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Trainers and devices
# --------------------------------------------------------------------------

def get_or_create_trainer(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(
        "SELECT trainer_id FROM trainer WHERE name = ?", (name,)
    ).fetchone()
    if row:
        return row["trainer_id"]
    cur = conn.execute("INSERT INTO trainer (name) VALUES (?)", (name,))
    conn.execute(
        "INSERT OR IGNORE INTO trainer_resource (trainer_id, stardust) VALUES (?, 0)",
        (cur.lastrowid,),
    )
    return int(cur.lastrowid)


def save_device_profile(
    conn: sqlite3.Connection, trainer_id: int, label: str,
    width: int, height: int, layout: dict[str, Any],
) -> int:
    """Store calibrated crop geometry as fractions, so it survives a new phone."""
    cur = conn.execute(
        """INSERT INTO device_profile
               (trainer_id, label, screen_width, screen_height, layout_json)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (trainer_id, label) DO UPDATE SET
               screen_width  = excluded.screen_width,
               screen_height = excluded.screen_height,
               layout_json   = excluded.layout_json,
               calibrated_at = datetime('now')
           RETURNING device_profile_id""",
        (trainer_id, label, width, height, json.dumps(layout)),
    )
    return int(cur.fetchone()[0])


def load_device_profile(
    conn: sqlite3.Connection, trainer_id: int, label: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT layout_json FROM device_profile WHERE trainer_id = ? AND label = ?",
        (trainer_id, label),
    ).fetchone()
    return json.loads(row["layout_json"]) if row else None


# --------------------------------------------------------------------------
# Scan sessions
# --------------------------------------------------------------------------

def start_scan(
    conn: sqlite3.Connection, trainer_id: int, capture_mode: str,
    search_filter: str | None = None, source_path: str | None = None,
    device_profile_id: int | None = None, notes: str | None = None,
) -> int:
    """Begin a scan pass.

    `search_filter` is the in-game search string used for this pass. That's how
    shiny/lucky/shadow get recorded: run the `shiny` search, scan it, and
    everything in the pass is shiny by construction rather than by pixel
    inspection.
    """
    if capture_mode not in ("grid", "detail"):
        raise ValueError(f"capture_mode must be 'grid' or 'detail', got {capture_mode!r}")
    cur = conn.execute(
        """INSERT INTO scan_session
               (trainer_id, capture_mode, search_filter, source_path,
                device_profile_id, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (trainer_id, capture_mode, search_filter, source_path,
         device_profile_id, notes),
    )
    return int(cur.lastrowid)


def finish_scan(conn: sqlite3.Connection, scan_session_id: int, frame_count: int) -> None:
    conn.execute(
        """UPDATE scan_session
              SET finished_at = datetime('now'), frame_count = ?
            WHERE scan_session_id = ?""",
        (frame_count, scan_session_id),
    )


_OBS_FIELDS = (
    "frame_label", "grid_row", "grid_col", "species_guess", "cp", "hp",
    "stardust_cost", "candy_cost", "level_hint", "sprite_phash",
    "confidence", "warnings", "raw_text",
)


def record_observations(
    conn: sqlite3.Connection, scan_session_id: int, rows: Iterable[Any]
) -> int:
    """Bulk-insert raw reads. Accepts dicts or dataclasses.

    Deliberately does no validation beyond the schema's: a bad read is evidence
    too, and discarding it here means it can't inform the resolver later.
    """
    payload = []
    for row in rows:
        d = asdict(row) if is_dataclass(row) and not isinstance(row, type) else dict(row)
        if isinstance(d.get("warnings"), (list, tuple)):
            d["warnings"] = "; ".join(str(w) for w in d["warnings"])
        payload.append((scan_session_id, *(d.get(f) for f in _OBS_FIELDS)))

    conn.executemany(
        f"""INSERT INTO observation (scan_session_id, {', '.join(_OBS_FIELDS)})
            VALUES ({', '.join('?' * (len(_OBS_FIELDS) + 1))})""",
        payload,
    )
    return len(payload)


# --------------------------------------------------------------------------
# Resolved collection
# --------------------------------------------------------------------------

_POKEMON_FIELDS = (
    "species_id", "nickname", "cp", "hp", "level", "attack_iv", "defense_iv",
    "stamina_iv", "iv_candidates", "star_tier", "is_shiny", "is_lucky",
    "is_shadow", "is_purified", "fast_move_id", "charge_move_id",
    "first_seen_scan", "last_seen_scan",
)


def upsert_pokemon(conn: sqlite3.Connection, trainer_id: int, **fields) -> int:
    unknown = set(fields) - set(_POKEMON_FIELDS) - {"pokemon_id"}
    if unknown:
        raise ValueError(f"unknown pokemon fields: {sorted(unknown)}")

    pokemon_id = fields.pop("pokemon_id", None)
    if pokemon_id is not None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE pokemon SET {sets} WHERE pokemon_id = ? AND trainer_id = ?",
            (*fields.values(), pokemon_id, trainer_id),
        )
        return pokemon_id

    cols = ["trainer_id", *fields]
    cur = conn.execute(
        f"INSERT INTO pokemon ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        (trainer_id, *fields.values()),
    )
    return int(cur.lastrowid)


def apply_filter_tag(conn: sqlite3.Connection, scan_session_id: int) -> int:
    """Tag every Pokemon seen in a filtered pass.

    This is the shiny solution: membership in a `shiny`-filtered scan IS the
    shiny flag. No sprite comparison, no badge template matching, and it works
    equally well for species whose shiny form is nearly indistinguishable.
    """
    row = conn.execute(
        "SELECT search_filter FROM scan_session WHERE scan_session_id = ?",
        (scan_session_id,),
    ).fetchone()
    if not row or not row["search_filter"]:
        return 0

    column = {
        "shiny": "is_shiny", "lucky": "is_lucky",
        "shadow": "is_shadow", "purified": "is_purified",
    }.get(row["search_filter"].strip().lower())
    if column is None:
        return 0   # e.g. a `4*` or `cp1000-2000` pass; no boolean to set

    cur = conn.execute(
        f"""UPDATE pokemon SET {column} = 1
             WHERE pokemon_id IN (
                 SELECT l.pokemon_id FROM observation_link l
                 JOIN observation o ON o.observation_id = l.observation_id
                WHERE o.scan_session_id = ?
             )""",
        (scan_session_id,),
    )
    return cur.rowcount


def link_observation(conn: sqlite3.Connection, observation_id: int, pokemon_id: int) -> None:
    conn.execute(
        """INSERT INTO observation_link (observation_id, pokemon_id)
           VALUES (?, ?)
           ON CONFLICT (observation_id) DO UPDATE SET pokemon_id = excluded.pokemon_id""",
        (observation_id, pokemon_id),
    )


def set_iv_candidates(
    conn: sqlite3.Connection, pokemon_id: int,
    candidates: list[tuple[float, int, int, int]],
) -> None:
    """Replace the surviving (level, IVs) set and update the ambiguity count."""
    conn.execute("DELETE FROM iv_candidate WHERE pokemon_id = ?", (pokemon_id,))
    conn.executemany(
        """INSERT INTO iv_candidate
               (pokemon_id, level, attack_iv, defense_iv, stamina_iv)
           VALUES (?, ?, ?, ?, ?)""",
        [(pokemon_id, *c) for c in candidates],
    )
    conn.execute(
        "UPDATE pokemon SET iv_candidates = ? WHERE pokemon_id = ?",
        (len(candidates), pokemon_id),
    )
    if len(candidates) == 1:
        level, a, d, s = candidates[0]
        conn.execute(
            """UPDATE pokemon
                  SET level = ?, attack_iv = ?, defense_iv = ?, stamina_iv = ?
                WHERE pokemon_id = ?""",
            (level, a, d, s, pokemon_id),
        )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def load_collection(conn: sqlite3.Connection, trainer_id: int) -> list[dict[str, Any]]:
    """Solver-ready roster: only Pokemon with a resolved level and IVs."""
    rows = conn.execute(
        "SELECT * FROM v_solver_collection WHERE trainer_id = ?", (trainer_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def needs_detail_scan(
    conn: sqlite3.Connection, trainer_id: int, limit: int = 50
) -> list[dict[str, Any]]:
    """The swipe-through queue, worst ambiguity first."""
    rows = conn.execute(
        "SELECT * FROM v_needs_detail_scan WHERE trainer_id = ? LIMIT ?",
        (trainer_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def candy_inventories(
    conn: sqlite3.Connection, trainer_id: int
) -> tuple[dict[int, int], dict[int, int]]:
    """(candy, xl_candy) keyed by dex number, matching the solver's expectation."""
    rows = conn.execute(
        """SELECT s.dex_number AS dex, ci.candy, ci.xl_candy
             FROM candy_inventory ci
             JOIN species s ON s.species_id = ci.species_id
            WHERE ci.trainer_id = ?""",
        (trainer_id,),
    ).fetchall()
    return (
        {r["dex"]: r["candy"] for r in rows},
        {r["dex"]: r["xl_candy"] for r in rows},
    )


def stardust(conn: sqlite3.Connection, trainer_id: int) -> int:
    row = conn.execute(
        "SELECT stardust FROM trainer_resource WHERE trainer_id = ?", (trainer_id,)
    ).fetchone()
    return int(row["stardust"]) if row else 0


def scan_summary(conn: sqlite3.Connection, trainer_id: int) -> dict[str, Any]:
    """Counts for a status line: how much is scanned, how much is resolved."""
    def one(sql: str, *args) -> int:
        return int(conn.execute(sql, args).fetchone()[0])

    return {
        "pokemon": one("SELECT COUNT(*) FROM pokemon WHERE trainer_id = ?", trainer_id),
        "resolved": one(
            """SELECT COUNT(*) FROM pokemon
                WHERE trainer_id = ? AND iv_candidates = 1""", trainer_id),
        "ambiguous": one(
            """SELECT COUNT(*) FROM pokemon
                WHERE trainer_id = ? AND (iv_candidates IS NULL OR iv_candidates > 1)""",
            trainer_id),
        "shiny": one(
            "SELECT COUNT(*) FROM pokemon WHERE trainer_id = ? AND is_shiny = 1",
            trainer_id),
        "scans": one(
            "SELECT COUNT(*) FROM scan_session WHERE trainer_id = ?", trainer_id),
        "observations": one(
            """SELECT COUNT(*) FROM observation o
                 JOIN scan_session ss ON ss.scan_session_id = o.scan_session_id
                WHERE ss.trainer_id = ?""", trainer_id),
    }
