# Integration notes

New files drop in as-is. Two of your existing files need edits — given as diffs
rather than replacement files, because my copies were reconstructed from what
was in the conversation and would have stripped your comments.

Everything below was run and tested. 69 tests pass.

---

## 1. `costs.py` — the XL candy boundary is 40.0, not 41.0

Your notes flagged this as "the one number I'd re-verify against GameMaster."
I fetched `POKEMONGO_UPGRADE_SETTINGS` from PokeMiners and diffed all 49 levels.
The boundary is **40.0**, and the XL amounts are also shifted one level.

```
$ python scripts/build_reference.py --check-costs

XL_CANDY_THRESHOLD is 41.0, GAME_MASTER says 40.0
level 40: costs.py (10000, 15,  0) != GAME_MASTER (10000, 0, 10)
level 42: costs.py (11000,  0, 10) != GAME_MASTER (11000, 0, 12)
level 44: costs.py (12000,  0, 12) != GAME_MASTER (12000, 0, 15)
level 46: costs.py (13000,  0, 15) != GAME_MASTER (13000, 0, 17)
level 48: costs.py (14000,  0, 17) != GAME_MASTER (14000, 0, 20)
6 mismatches
```

The authoritative fields:

```
xlCandyMinPokemonLevel: 40
xlCandyCost:  [10, 10, 12, 12, 15, 15, 17, 17, 20, 20]   # indexed from level 40
candyCost[39] (level 40): 0                              # no regular candy at 40
```

Your reasoning was right — the XL progression does restart at 10/12/15/17/20 —
but the ladder starts at 40, not 41. An earlier revision had this correct and
the "fix" moved it. The stardust table, the candy tiers through level 39, and
the entire CPM table all check out exactly.

**Impact:** every candidate crossing level 40 was mispriced in both resources.
Level 40 → 40.5 was charged 15 regular candy against a real cost of 10 XL, so
the regular-candy constraint was over-tightened and the XL constraint was
under-tightened for exactly the high-level Pokémon where XL is scarce. Concretely,
on the sample data `40.0 → 41.0` went from `(20000 dust, 30 candy, 0 XL)` to
`(20000, 0, 20)`.

### The edit

```diff
     (37.0, 38.5, 12),
-    (39.0, 40.5, 15),
-    # XL candy from here -- amounts restart their own progression.
-    (41.0, 42.5, 10),
-    (43.0, 44.5, 12),
-    (45.0, 46.5, 15),
-    (47.0, 48.5, 17),
-    (49.0, 49.5, 20),
+    (39.0, 39.5, 15),
+    # XL candy from level 40. Amounts restart their own progression, which is
+    # what made 41.0 a plausible guess -- but GAME_MASTER pins the first XL
+    # tier to levels 40-41, not 41-42, so the whole ladder sat one level high.
+    (40.0, 41.5, 10),
+    (42.0, 43.5, 12),
+    (44.0, 45.5, 15),
+    (46.0, 47.5, 17),
+    (48.0, 49.5, 20),
 ]

-XL_CANDY_THRESHOLD = 41.0
+# POKEMON_UPGRADE_SETTINGS.xlCandyMinPokemonLevel. Verified by
+# `python scripts/build_reference.py --check-costs`, which is also a test.
+XL_CANDY_THRESHOLD = 40.0
```

### Tests to update

`test_model.py::test_xl_candy_starts_at_forty_one` and
`test_xl_candy_progression_restarts_at_ten` both assert the old behaviour, and
`REAL_CANDY` includes `(39, 40.5, 15)`. Change the range to `(39, 39.5, 15)`.

Worth noting *why* those tests didn't catch this: they assert the repo's own
belief rather than checking it against anything external. The replacements in
`tests/test_reference_and_import.py` parametrize over all 49 levels against
committed GameMaster data, so this class of error can't recur silently.

### Also worth one in-game check

GameMaster still carries `shadowStardustMultiplier: 1.2` and
`shadowCandyMultiplier: 1.2`. That doesn't prove the client applies them — the
field may be vestigial — but it's weaker support for `SHADOW_COST_SURCHARGE =
False` than the PROJECT_NOTES entry implies. Left off; flagged in a comment.

---

## 2. `model.py` — dual values

Add this function and two lines. It answers "which constraint is actually
stopping you," which was on your list as the best low-effort addition.

```python
def shadow_prices(prob: pulp.LpProblem) -> dict[str, float]:
    """Dual value per resource constraint, from the LP relaxation.

    Re-solves with every binary relaxed to [0, 1]: an integer program has no
    duals, so asking CBC for `.pi` after a MIP solve gives None. The relaxation's
    duals are an approximation -- they answer "what would one more unit of this
    resource have been worth if the plan could be fractional" -- but that is
    still the question worth surfacing, and it is the only version of it that
    is cheap to compute.

    Reported per unit of resource, so stardust prices are tiny by construction
    (gain per single stardust) while candy prices are large. Compare a resource
    against itself over time, not against a different resource.
    """
    relaxed = prob.copy()
    for var in relaxed.variables():
        # Note: PuLP does NOT keep cat == "Binary". A variable declared binary
        # is stored as LpInteger with bounds [0, 1], so testing against
        # LpBinary here silently matches nothing and every dual comes back
        # empty -- which looks exactly like "no constraint is binding".
        if var.cat == pulp.LpInteger:
            var.cat = pulp.LpContinuous
            if var.lowBound is None:
                var.lowBound = 0
    relaxed.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[relaxed.status] != "Optimal":
        return {}
    out = {}
    for name, con in relaxed.constraints.items():
        if name.startswith("One_Target_"):
            continue   # not a resource; its dual is not actionable
        pi = con.pi
        if pi is not None and abs(pi) > 1e-12:
            out[name] = float(pi)
    return dict(sorted(out.items(), key=lambda kv: -abs(kv[1])))
```

Then in `build_and_solve`, add a `with_shadow_prices: bool = False` parameter,
add `shadow_prices: dict[str, float] | None = None` to `Result`, and pass
`shadow_prices=shadow_prices(prob) if with_shadow_prices else None` into the
returned `Result`.

The `LpInteger` detail cost me a debugging round — the obvious
`if var.cat == pulp.LpBinary` matches nothing and returns an empty dict, which
is indistinguishable from "nothing is binding."

**What it gives you**, on sample data:

```
budget  20,000  →  Stardust budget          = 0.000169  per stardust
budget  60,000  →  Stardust budget          = 0.000090  per stardust
budget 400,000  →  XL candy, species 376    = 0.081781  per XL candy
                   XL candy, species 149    = 0.017844  per XL candy
```

At 400k the answer stops being "buy more dust" and becomes "you are out of
Metagross XL." That is the thing a sorted list structurally cannot tell you,
and it's a better headline for the project than the benchmark table.

Note this replaces the prototype's 90%-of-stock heuristic, which reports a
constraint as binding when the shelf looks empty rather than when the last unit
would actually change the plan.

---

## 3. New files

```
scripts/build_reference.py          GAME_MASTER -> compact reference; --check-costs
pogo_opt/data/reference.json        1,486 species + 384 moves, 264 KB, commit it
pogo_opt/reference.py               species/move lookup, normalized matching
pogo_opt/importers/pokegenie.py     Poke Genie CSV -> PokemonInstance
api.py                              FastAPI over the solver
requirements-api.txt
tests/test_reference_and_import.py  69 tests
```

### Why the reference file exists

`sample_data/collection.csv` is pre-joined, so every row already carries base
stats, typing and move stats. Nothing in the repo could supply those for a
species the sample has never seen — which is every import. `reference.json`
closes that gap and is small enough to commit, which also pins game data to a
known revision so a Niantic rebalance shows up as a diff rather than as a silent
change in yesterday's plan.

It corrects your move table too. The sample CSV has Dragon Tail at power 13 /
1.1s / 9 energy; GameMaster says 14 / 1.0s / 8. Those were hand-entered.

### Poke Genie import

Poke Genie's Scan Pro tier exports scan history to CSV. It already contains
resolved IVs and a resolved level, so it's the shortest path from "a real
collection exists on my phone" to "the solver ran on it."

Two things worth knowing:

**Columns are matched by normalized alias, never exact header string.** Poke
Genie has renamed columns across versions. `map_columns()` is exported so a UI
can show the mapping before committing an import, and `POST /import/pokegenie?dry_run=true`
returns it along with every unmapped header.

**It exports `Level Min` / `Level Max`, not a level** — because CP is floored,
so adjacent half-levels can share a CP. That's the same ambiguity `resolve.py`
handles for the OCR path, and the importer applies the same fix: recompute CP
(and HP) across the range and keep the levels that reproduce the scan. A 34–37
range on a Dragonite collapses to exactly 35.5. Where a row has no range at all
but does have CP, it searches the full ladder.

Rows that can't resolve are **reported, not defaulted**. Missing IVs (an
unappraised scan) skip the row rather than importing zeros — a confident plan
built on invented IVs is the same failure mode as a constraint that stops
binding.

### API

```bash
pip install -r requirements-api.txt
uvicorn api:app --reload      # http://127.0.0.1:8000/docs
```

| Route | Does |
|---|---|
| `GET /health` | collection size, source, reference revision |
| `GET /collection?bulk=` | rows with CP and rating |
| `POST /solve` | the MIP; returns plan + per-species usage + duals |
| `POST /import/pokegenie` | CSV upload; `?dry_run=true` for the mapping |
| `GET /candy` `PUT /candy` | per-species stock, flags which are unknown |

State is in-memory (`STATE` dict) so the API is exercisable before the SQLite
wiring exists. Swap for `pogo_opt/db.py` — the schema is already there and
already scoped by `trainer_id`.

`GET /candy` reports `known: false` for species with no inventory set. Those are
unconstrained in the model, which is a reasonable default and a silent one;
surfacing it lets the UI ask for the handful of numbers that would actually
change the answer instead of demanding 200 rows up front.

---

## 4. About `pogo-data.js`

Delete it. It reimplements the allocation in JavaScript as greedy-with-
reassignment — the baseline `benchmark.py` exists to beat — plus a second copy
of the cost tables that would have needed this same XL fix independently. Once
the UI calls `POST /solve`, `renderVals()` gets shorter, not longer.

---

## 5. Order I'd do this in

1. Apply the `costs.py` diff, update the two tests, re-run `benchmark.py`. The
   numbers in the README will move — greedy's plateau in particular, since it
   was mispricing the same steps.
2. Fix the `benchmark.py` greedy baseline to use two candy pools (from the
   earlier note). Both corrections land in the same table; do them together and
   re-run once.
3. Drop in the new files, run `python scripts/build_reference.py`, run the tests.
4. Get a Poke Genie export and `POST /import/pokegenie?dry_run=true` it. The
   mapping report will tell you immediately whether the aliases cover your
   version's headers; send me the header row if anything comes back unmapped.
5. Port the UI to call the API.

Security list first, though — the MySQL password is still in the old history.
