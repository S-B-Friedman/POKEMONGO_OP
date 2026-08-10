#!/usr/bin/env python3
"""Rewrite sample_data/collection.csv's stat columns from reference.json.

The roster is curated -- which Pokemon, at what level, with which IVs, flags and
moveset -- and this script does not touch any of that. What it rewrites is the
denormalized stat columns that were originally hand-entered and had drifted from
GAME_MASTER: base attack/defense/stamina, and each move's power, duration,
energy and type.

That drift is the reason this exists as a script rather than a one-time edit.
`collection.csv` is pre-joined for a good reason -- it makes the repo runnable
with no database and no network -- but a pre-joined file is a cache, and a cache
with no way to rebuild it silently goes stale. Run with --check in CI-ish
fashion to assert it has not drifted again.

    python scripts/rebuild_sample_data.py            # rewrite in place
    python scripts/rebuild_sample_data.py --check    # exit 1 if stale
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from pogo_opt.reference import default_reference, normalize  # noqa: E402

CSV_PATH = ROOT / "sample_data" / "collection.csv"


def _species_row(ref, species_id: int, name: str):
    """Resolve a row's species by display name, then sanity-check the dex id.

    Looking up by name uses the same public path an import would, so a bad
    lookup here is a bug worth seeing rather than something a dex-id scan would
    paper over. The dex check catches the case where a name resolves to a
    different Pokemon than the row's species_id claims -- silently rewriting
    stats onto the wrong species is exactly the failure this script must not
    introduce.
    """
    sp = ref.species(name)
    if sp is None:
        return None, f"{name}: not found in reference"
    if sp.dex != species_id:
        return None, (
            f"{name}: resolves to dex {sp.dex} but the row says {species_id} "
            f"-- refusing to rewrite"
        )
    return sp, None


def rebuild(rows: list[dict], ref) -> tuple[list[dict], list[str]]:
    """Return corrected rows plus a list of human-readable changes."""
    changes: list[str] = []
    out = []

    for r in rows:
        r = dict(r)

        # A short row (fewer fields than the header) leaves trailing columns as
        # None, and every column after the omission has been read one position
        # to the left. Two rows in the original file were short by one, which is
        # why Gardevoir carried type2 == "0" and lost its Psychic typing --
        # silently, because nothing validated row width. Normalize the flags to
        # 0 and let the species lookup below restore the typing.
        if any(v is None for v in r.values()):
            missing = [k for k, v in r.items() if v is None]
            changes.append(
                f"{r.get('name')}: row was short {len(missing)} field(s) "
                f"({', '.join(missing)}) -- defaulting to 0"
            )
            for k in missing:
                r[k] = "0"

        sid = int(r["species_id"])

        sp, problem = _species_row(ref, sid, r["name"])
        if problem:
            changes.append(problem)
        if sp is not None:
            for col, val in (
                ("base_attack", sp.base_attack),
                ("base_defense", sp.base_defense),
                ("base_stamina", sp.base_stamina),
            ):
                if int(r[col]) != val:
                    changes.append(f"{r['name']:12s} {col}: {r[col]} -> {val}")
                    r[col] = str(val)
            for col, val in (("type1", sp.type1), ("type2", sp.type2 or "")):
                if (r.get(col) or "") != (val or ""):
                    changes.append(f"{r['name']:12s} {col}: {r.get(col)!r} -> {val!r}")
                    r[col] = val or ""

        for kind in ("fast", "charge"):
            mv = ref.move(r[f"{kind}_move"])
            if mv is None:
                changes.append(
                    f"{r['name']:12s} {kind} move {r[f'{kind}_move']!r}: not in reference -- left as-is"
                )
                continue
            # The CSV stores energy as a positive magnitude for both kinds;
            # reference.json stores the raw GAME_MASTER value.
            energy = abs(mv.energy)
            for col, val in (
                (f"{kind}_power", mv.power),
                (f"{kind}_energy", energy),
                (f"{kind}_type", mv.type),
            ):
                if str(r[col]) != str(val):
                    changes.append(f"{r['name']:12s} {r[f'{kind}_move']} {col}: {r[col]} -> {val}")
                    r[col] = str(val)
            if float(r[f"{kind}_duration"]) != float(mv.duration):
                changes.append(
                    f"{r['name']:12s} {r[f'{kind}_move']} {kind}_duration: "
                    f"{r[f'{kind}_duration']} -> {mv.duration}"
                )
                r[f"{kind}_duration"] = str(mv.duration)

        out.append(r)

    return out, changes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="report drift and exit 1 instead of rewriting")
    args = ap.parse_args()

    ref = default_reference()
    with CSV_PATH.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    fixed, changes = rebuild(rows, ref)

    if not changes:
        print(f"{CSV_PATH.relative_to(ROOT)} matches reference.json")
        return 0

    for c in changes:
        print(" ", c)
    print(f"{len(changes)} field(s) differ from reference.json")

    if args.check:
        print("run `python scripts/rebuild_sample_data.py` to update")
        return 1

    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(fixed)
    print(f"wrote {CSV_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
