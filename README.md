# Constrained Resource Allocation Solver

A mixed-integer program that decides where to spend a limited upgrade budget
across a large inventory of assets, each with its own cost curve and its own
marginal return. Pokémon GO supplies the dataset: a few hundred owned Pokémon,
a shared stardust budget, and a per-species candy inventory that caps how much
can be invested in any one line.

The interesting part is not the game. It's that the resource structure is a
genuine multi-dimensional knapsack — one global budget, many local ones, and a
non-linear return curve — which is the same shape as a capital allocation or
maintenance scheduling problem.

## Quickstart

```bash
pip install -r requirements.txt
python run.py
```

That runs against the bundled sample collection with a 100,000 stardust budget.
No database required.

```
Pokemon     From    To     Stardust  Candy   XL     Gain
--------------------------------------------------------
Blastoise   22.5 ->  24.5     13,500     12    0     26.5
Gengar      29.0 ->  31.0     20,000     16    0     21.4
Dragonite   15.0 ->  18.0     12,000     12    0     19.2
Lucario     28.5 ->  31.0     24,500     20    0     19.1
Blissey     26.0 ->  27.5     12,500     12    0     18.4
MetagrossL   18.5 ->  21.5      7,600     13    0     13.5
...
TyranitarS   20.0 ->  20.5      3,000      3    0      1.7
--------------------------------------------------------
9 Pokemon | stardust 99,850 / 100,000 (150 unspent) | total gain 129.2
L lucky (half stardust)  S shadow (20% surcharge)
```

The marker names the state rather than just flagging it, because they do not
pull the same way: lucky and purified make a power-up cheaper, shadow makes it
dearer.

Above level 40 the candy cost switches to XL Candy, tracked as its own budget:

```
Metagross    40.0 ->  41.0     20,000      0   20      1.8
```

Useful flags:

```bash
python run.py --stardust 250000        # different budget
python run.py --max-steps 10           # allow larger single investments
python run.py --bulk 0                 # optimize pure damage, ignore survivability
python run.py --source mysql           # read the real collection from MySQL
```

## The model

**Decision variables.** One binary per (owned Pokémon, candidate target level)
pair. A Pokémon can be taken to exactly one target level, or left alone.

Keying on the *owned instance* rather than the species matters: if you own three
Machamps they are three separate decisions with three separate current levels
and three different cost curves.

**Objective.** Maximize total *marginal* rating gain — the improvement each
upgrade buys, not the absolute rating of the chosen set. Maximizing absolute
rating just rewards picking things that were already good, which is not a
decision, it's a ranking.

The rating itself combines a full fast/charge attack cycle with a survivability
term:

```
cycle_dps = (n_fast x fast_damage + charge_damage) / cycle_time
rating    = cycle_dps x bulk^alpha x collector_multipliers
```

where `n_fast` is how many fast moves it takes to fill the energy bar. Summing
independent fast and charge DPS figures — the obvious shortcut — is not a rate
of anything, since the two moves compete for the same time.

`alpha` (`--bulk`) is the one genuinely subjective knob: 0 optimizes pure
damage, 1 weights damage and survivability equally.

**Constraints.**

| Constraint | Form |
|---|---|
| One target level per Pokémon | `sum_t x[i,t] <= 1` |
| Shared stardust budget | `sum stardust(i,t) * x[i,t] <= B` |
| Candy, per species | `sum candy(i,t) * x[i,t] <= C_s` for each species `s` |
| XL candy, per species | tracked separately from regular candy |
| Optional cap on plan size | `sum x <= N` |

Candy is per-species, which is what makes the problem hard: two Dragonites
compete with each other for candy while competing with everything else for
stardust. That coupling is why a global sort gives the wrong answer.

Solved with CBC via PuLP. The sample instance is ~140 binaries and solves
instantly; a full 600-Pokémon collection is still well inside CBC's comfort zone.

## Does the solver earn its place?

The fair objection to any optimizer this small is "why not sort by efficiency
and take from the top?" `benchmark.py` answers it against exactly that greedy
baseline:

```
    Budget    Greedy    Solver    Delta       %     G dust    S dust
    10,000      19.6      20.9     1.31   6.70%      9,800     9,300
    25,000      50.8      50.8     0.00   0.00%     24,800    24,800
    50,000      76.4      84.4     8.01  10.49%     48,050    49,700
   100,000     124.0     129.2     5.14   4.15%     99,050    99,850
   200,000     167.0     186.3    19.28  11.55%    199,450   199,300
   400,000     184.2     239.4    55.22  29.98%    326,450   399,100
   800,000     184.2     273.8    89.59  48.63%    326,450   794,500
```

The solver is strictly better at six of seven budgets and never worse. **At
25,000 it ties** — greedy finds the optimum there, and that is worth stating
rather than rounding away, because it marks where the extra machinery starts
earning its keep. Below that, the collection is small enough relative to the
budget that ranking is very nearly sufficient.

Greedy fails in two ways. It commits stardust to a high-efficiency small step
and then cannot afford the larger step on the same Pokémon that would have been
worth more. And it plateaus — past ~323,550 stardust it cannot deploy
additional budget at all, because every Pokémon is already locked to a small
upgrade. The solver keeps finding uses for the marginal dust, which is where
the gap widens to 49%.

Run it yourself:

```bash
python benchmark.py
pip install -r requirements-dev.txt
python -m pytest tests/
```

## Data sources

Two interchangeable loaders behind one record type, so the model doesn't care
which you used.

**CSV** (default) — a flat pre-joined `sample_data/collection.csv`, plus an
optional `candy_inventory.csv`. This is what makes the repo runnable by someone
who is not me.

Because it is pre-joined it is a cache, and a cache with no way to rebuild it
goes stale silently — which it had. Its stat columns are now generated from
`reference.json`:

```bash
python scripts/rebuild_sample_data.py           # regenerate stat columns
python scripts/rebuild_sample_data.py --check   # exit 1 if it has drifted
```

The roster — which Pokémon, at what level, with which IVs and moves — is
curated and untouched by that script.

**Poke Genie export** — the shortest path from a real collection on your phone
to a solved plan. Poke Genie's Scan Pro tier exports scan history as CSV, with
IVs already resolved:

```bash
python -c "from pogo_opt.importers.pokegenie import import_csv; \
           print(len(import_csv('scan_history.csv')[0]))"
```

Columns are matched by normalized alias rather than exact header text, because
Poke Genie has renamed them across versions; `map_columns()` is exported so a
caller can show the mapping before committing an import.

The export carries `Level Min` / `Level Max` rather than a level, because CP is
floored and adjacent half-levels can share a CP. The importer resolves it the
same way the OCR path does — recompute CP and HP across the range, keep the
levels that reproduce the scan. A 34–37 range on a Dragonite collapses to
exactly 35.5.

Rows that can't resolve are reported, not defaulted. An unappraised scan with
missing IVs is skipped rather than imported as zeros: a confident plan built on
invented IVs is the same failure mode as a constraint that silently stops
binding.

**MySQL** — the original `POGOR` schema, joining `my_pokemon` against
`species_stats`, `fast_move_stats` and `charge_move_stats`. Credentials come
from the environment (`.env.example` lists them; nothing auto-loads it):

```bash
pip install -r requirements-mysql.txt
set -a; source .env; set +a      # after copying .env.example -> .env
python run.py --source mysql
```

**Screenshot ingestion** — `ocr_ingest.py` turns screenshots or a scroll-through
video into a CSV:

```bash
pip install -r requirements-ocr.txt
python ocr_ingest.py --video swipe.mp4 -o scanned.csv
python ocr_ingest.py --images shots/ --calibrate   # check bar crops first
```

Parsing is separated from image I/O on purpose. `pogo_opt/ingest.py` is pure —
strings and numbers in, records out — so it's unit-tested without Vision
credentials, Tesseract, or a phone. `--calibrate` dumps the crops to check them
against a real screenshot.

The appraisal bars are found in the image rather than assumed: `detect_bars()`
looks for three similar-width segments sharing a left edge, so it does not
depend on the device or resolution. `BarLayout`'s fractions are only the
fallback. Two details that are easy to get wrong and produce a confident wrong
answer rather than an error:

- **The bar is three segments with gaps**, so fill must be measured against the
  summed segment widths. Counting the gaps biases every reading downward.
- **A maxed stat is drawn red, not orange.** An orange-only reader scores a
  perfect stat as zero.

Calibrated against a real 1206×2622 capture, where the extracted IVs reproduced
both the displayed CP and the displayed HP at exactly one level for each test
subject. That double agreement is the check worth repeating on a new device.

Reads that fail are flagged, not dropped. `iv_confidence` reports how close each
bar landed to a legal IV — a value near 0.5 means the crop region is wrong
rather than the Pokémon being unusual. Output goes to CSV so a bad read gets
fixed in a spreadsheet instead of by re-running OCR.

Frames are sampled and near-duplicates skipped. Scanning every frame of a
60-second clip is 1,800 OCR calls to read a collection that scrolls past maybe
forty Pokémon.

## Reference data

`pogo_opt/data/reference.json` holds 1,486 species and 384 moves — base stats,
typing and move stats — built from PokeMiners' GAME_MASTER by
`scripts/build_reference.py`.

It exists because `sample_data/collection.csv` is pre-joined: every row already
carries the stats it needs, so nothing in the repo could supply them for a
species the sample has never seen. That is every species in a real import.

Committing it (264 KB) also pins game data to a known revision, so a Niantic
rebalance shows up as a reviewable diff rather than as a silent change in
yesterday's plan. Rebuild and re-verify with:

```bash
python scripts/build_reference.py                  # rebuild from GAME_MASTER
python scripts/build_reference.py --check-costs    # diff costs.py against it
```

`--check-costs` is also a test. It is what caught the XL candy boundary being
one level high, and it fails loudly rather than drifting.

## HTTP API

```bash
pip install -r requirements-api.txt
uvicorn api:app --reload          # http://127.0.0.1:8000/docs
```

| Route | Does |
|---|---|
| `GET /health` | collection size, source, reference revision |
| `GET /collection?bulk=` | rows with CP and rating |
| `POST /solve` | the MIP; returns plan, per-species usage, and duals |
| `POST /import/pokegenie` | CSV upload; `?dry_run=true` returns the column mapping |
| `GET` / `PUT /candy` | per-species stock, flagging which are unknown |

State is in-memory (a `STATE` dict) so the API is exercisable before the SQLite
wiring exists — `pogo_opt/db.py` and `schema.sql` are already written and
already scoped by `trainer_id`, so that swap is the next step rather than a
rewrite.

`GET /candy` reports `known: false` for species with no inventory set. Those are
unconstrained in the model, which is a reasonable default and a silent one;
surfacing it lets a UI ask for the handful of numbers that would actually change
the answer instead of demanding two hundred rows up front.

## Which constraint is actually binding

`build_and_solve(..., with_shadow_prices=True)` populates `Result.shadow_prices`
with the dual value of each resource constraint:

```
budget  20,000  ->  Stardust budget          = 0.000169  per stardust
budget  60,000  ->  Stardust budget          = 0.000090  per stardust
budget 400,000  ->  XL candy, species 376    = 0.081781  per XL candy
                    XL candy, species 149    = 0.017844  per XL candy
```

At 400k the answer stops being "buy more dust" and becomes "you are out of
Metagross XL" — which is the thing a sorted list structurally cannot tell you.

Two caveats, both structural. An integer program has no duals, so this re-solves
the LP relaxation; the numbers answer "what would one more unit have been worth
if the plan could be fractional." And they are per *unit* of resource, so
stardust prices are tiny by construction while candy prices are large — compare
a resource against itself over time, not against a different resource.

## What isn't modeled

Being explicit about this, because the gap between a model and reality is
usually where the interesting conversation is:

- **Type effectiveness against specific opponents.** The rating scores a
  Pokémon against a generic reference defender. Real value depends on what
  you're fighting, which would make this a portfolio problem — maximize
  coverage across a threat distribution — rather than a single-objective one.
  That's the most worthwhile extension.
- **Mega evolution.** Megas are paid for in mega energy, a separate per-species
  resource. It isn't a stardust constraint and is off by default. Modeling it
  properly means a second coupled budget.
- **Second charge moves, best-buddy, and move rerolls**, all of which consume
  the same stardust and belong in the same budget.
- **Time.** This is a single-period allocation. Stardust accrues daily, so the
  real problem is multi-period, and spending early compounds differently than
  spending late.
- **CPM provenance.** `costs.py` carries the full published table for levels
  1-50 and derives half-levels the way the game does: the quadratic mean
  `sqrt((CPM(n)^2 + CPM(n+1)^2)/2)` below 40, arithmetic above 40 where the
  curve goes linear. Validated indirectly — `combat_power()` reproduces the
  published CP of five known perfect-IV Pokémon at level 40 exactly, which only
  works if the CPM table and the stat formulas are both right.
- **The XL candy boundary is level 40.0**, verified against GameMaster's
  `xlCandyMinPokemonLevel` (see `scripts/build_reference.py --check-costs`).
  An earlier revision had this at 41.0, which looked plausible because XL
  amounts restart their own 10/12/15/17/20 progression — but the whole ladder
  sat one level high.

Because type effectiveness is absent, `--bulk` and the collector weights are the
only tuning available.

## Layout

```
run.py                        CLI
api.py                        FastAPI wrapper over the solver
benchmark.py                  greedy baseline comparison
ocr_ingest.py                 screenshot/video -> CSV (optional extras)

pogo_opt/
  costs.py                    CP multipliers, per-level power-up costs
  model.py                    rating function, the MIP, shadow prices
  data.py                     CSV and MySQL loaders
  reference.py                species/move lookup with normalized matching
  data/reference.json         committed GAME_MASTER extract
  importers/pokegenie.py      Poke Genie CSV -> PokemonInstance
  ingest.py                   pure screenshot parsing
  resolve.py                  CP + HP -> exact level
  db.py, schema.sql           SQLite layer, scoped by trainer_id (not yet wired)

scripts/build_reference.py    GAME_MASTER -> reference.json; --check-costs
sample_data/                  runnable example collection
tests/                        262 tests: mechanics, solver, parsing, import, DB
```
