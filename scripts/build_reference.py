#!/usr/bin/env python3
"""
Build pogo_opt/data/reference.json from PokeMiners' GAME_MASTER.

This is the file that lets an import supply base stats, typing and move stats
for a Pokemon the sample CSV has never heard of. Without it, any importer can
only handle the 26 species that happen to be pre-joined into sample_data.

    python scripts/build_reference.py                 # fetch latest
    python scripts/build_reference.py --gm path.json  # use a local dump

The GAME_MASTER is ~19MB; the emitted reference is ~1MB, which is small enough
to commit. Committing it matters: it pins the game data to a known revision, so
a solver run is reproducible and a surprise Niantic rebalance shows up as a diff
rather than as a silent change in yesterday's plan.

It also emits the cost tables, which are the authoritative version of what
costs.py hardcodes. `--check-costs` diffs the two.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path

GM_URL = "https://raw.githubusercontent.com/PokeMiners/game_masters/master/latest/latest.json"

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "pogo_opt" / "data" / "reference.json"

_DEX = re.compile(r"^V(\d{4})_POKEMON_")


def _title(raw: str) -> str:
    """MOVEMENT_ID / POKEMON_ID -> display name.

    Poke Genie, the game UI and every wiki use display names; GAME_MASTER uses
    SCREAMING_SNAKE. Matching happens on a normalized key rather than on this,
    so the exact casing here is cosmetic.
    """
    return " ".join(w.capitalize() for w in raw.split("_") if w)


def normalize(name: str) -> str:
    """Matching key: lowercase alphanumerics only.

    Collapses 'Dragon Tail' / 'DRAGON_TAIL_FAST' / 'dragon-tail' onto one key,
    which is what makes an importer robust against whatever the source app
    happens to call a move.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def fetch(url: str = GM_URL) -> list:
    with urllib.request.urlopen(url) as fh:
        return json.load(fh)


def extract_moves(gm: list) -> dict:
    moves: dict[str, dict] = {}
    for entry in gm:
        ms = entry.get("data", {}).get("moveSettings")
        if not ms:
            continue
        # Newer entries carry a numeric movementId and put the name only in
        # the templateId (V0406_MOVE_AURA_WHEEL_ELECTRIC). Skipping those
        # would silently drop every recently-added move.
        move_id = ms.get("movementId")
        if not isinstance(move_id, str):
            m = re.match(r"^V\d{4}_MOVE_(.+)$", tid := entry.get("templateId", ""))
            if not m:
                continue
            move_id = m.group(1)
        if not move_id:
            continue
        energy = int(ms.get("energyDelta", 0) or 0)
        is_fast = move_id.endswith("_FAST")
        display = _title(move_id[:-5] if is_fast else move_id)
        moves[normalize(display)] = {
            "name": display,
            "kind": "fast" if is_fast else "charge",
            "type": ms.get("pokemonType", "POKEMON_TYPE_NORMAL")
                      .replace("POKEMON_TYPE_", "").lower(),
            "power": int(ms.get("power", 0) or 0),
            # Fast moves gain energy (positive), charge moves spend it
            # (negative in GAME_MASTER). Stored as a magnitude; the sign is
            # implied by `kind`, which is less error-prone downstream than a
            # signed field everyone has to remember to abs().
            "energy": abs(energy),
            "duration": round(int(ms.get("durationMs", 0) or 0) / 1000.0, 3),
        }
    return moves


def extract_species(gm: list) -> dict:
    species: dict[str, dict] = {}
    for entry in gm:
        tid = entry.get("templateId", "")
        m = _DEX.match(tid)
        ps = entry.get("data", {}).get("pokemonSettings")
        if not (m and ps):
            continue
        stats = ps.get("stats") or {}
        if not stats.get("baseAttack"):
            continue   # placeholder rows carry no stats

        dex = int(m.group(1))
        pokemon_id = ps.get("pokemonId", "")
        form = ps.get("form")
        # form is e.g. CHARIZARD_NORMAL / EXEGGUTOR_ALOLA; strip the species
        # prefix so 'Alola' survives as a distinguishing label.
        form_label = ""
        if form and form.startswith(pokemon_id + "_"):
            form_label = form[len(pokemon_id) + 1:]
        if form_label in ("NORMAL", "STANDARD"):
            form_label = ""

        display = _title(pokemon_id)
        key = normalize(display + form_label)
        record = {
            "dex": dex,
            "name": display,
            "form": _title(form_label) if form_label else "",
            "base_attack": int(stats["baseAttack"]),
            "base_defense": int(stats["baseDefense"]),
            "base_stamina": int(stats["baseStamina"]),
            "type1": ps.get("type", "POKEMON_TYPE_NORMAL")
                       .replace("POKEMON_TYPE_", "").lower(),
            "type2": (ps.get("type2") or "").replace("POKEMON_TYPE_", "").lower() or None,
            "mega": bool(ps.get("tempEvoOverrides") or ps.get("temporaryEvolutions")),
        }
        # A plain-form entry wins over a later same-key duplicate.
        species.setdefault(key, record)
    return species


def extract_costs(gm: list) -> dict:
    up = next(e["data"]["pokemonUpgrades"] for e in gm
              if e.get("templateId") == "POKEMON_UPGRADE_SETTINGS")
    cpm = next(e["data"]["playerLevel"]["cpMultiplier"] for e in gm
               if e.get("templateId") == "PLAYER_LEVEL_SETTINGS")
    return {
        "upgrades_per_level": up["upgradesPerLevel"],
        # All three arrays are indexed by (level - 1).
        "stardust_cost": up["stardustCost"],
        "candy_cost": up["candyCost"],
        # xl_candy_cost is indexed from xl_candy_min_pokemon_level.
        "xl_candy_cost": up["xlCandyCost"],
        "xl_candy_min_pokemon_level": up["xlCandyMinPokemonLevel"],
        "shadow_stardust_multiplier": up["shadowStardustMultiplier"],
        "shadow_candy_multiplier": up["shadowCandyMultiplier"],
        "purified_stardust_multiplier": up["purifiedStardustMultiplier"],
        "purified_candy_multiplier": up["purifiedCandyMultiplier"],
        "max_normal_upgrade_level": up["maxNormalUpgradeLevel"],
        "cp_multiplier": cpm,
    }


def check_costs(costs: dict) -> int:
    """Diff the GAME_MASTER cost tables against what costs.py hardcodes."""
    import sys
    sys.path.insert(0, str(HERE.parent))
    from pogo_opt.costs import XL_CANDY_THRESHOLD, step_cost

    bad = 0
    xl_min = costs["xl_candy_min_pokemon_level"]
    if XL_CANDY_THRESHOLD != float(xl_min):
        print(f"XL_CANDY_THRESHOLD is {XL_CANDY_THRESHOLD}, "
              f"GAME_MASTER says {float(xl_min)}")
        bad += 1

    for level in range(1, 50):
        want_dust = costs["stardust_cost"][level - 1]
        want_candy = costs["candy_cost"][level - 1]
        want_xl = 0
        if level >= xl_min:
            want_xl = costs["xl_candy_cost"][level - xl_min]
        got_dust, got_candy, got_xl = step_cost(float(level))
        if (got_dust, got_candy, got_xl) != (want_dust, want_candy, want_xl):
            print(f"level {level}: costs.py {(got_dust, got_candy, got_xl)} "
                  f"!= GAME_MASTER {(want_dust, want_candy, want_xl)}")
            bad += 1

    print("costs.py matches GAME_MASTER" if not bad else f"{bad} mismatches")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gm", type=Path, help="local GAME_MASTER json (skips download)")
    ap.add_argument("-o", "--out", type=Path, default=OUT)
    ap.add_argument("--check-costs", action="store_true",
                    help="diff costs.py against GAME_MASTER and exit")
    args = ap.parse_args()

    gm = json.loads(args.gm.read_text(encoding="utf-8")) if args.gm else fetch()

    costs = extract_costs(gm)
    if args.check_costs:
        return check_costs(costs)

    reference = {
        "source": "PokeMiners/game_masters latest",
        "species": extract_species(gm),
        "moves": extract_moves(gm),
        "costs": costs,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(reference, separators=(",", ":")), encoding="utf-8")

    kb = args.out.stat().st_size / 1024
    print(f"Wrote {args.out} ({kb:,.0f} KB): "
          f"{len(reference['species'])} species, {len(reference['moves'])} moves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
