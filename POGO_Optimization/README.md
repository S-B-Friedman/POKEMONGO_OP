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
Blastoise   22.5 ->  24.5     13,500     12    0     27.9
Gengar      29.0 ->  31.0     20,000     16    0     17.4
Tyranitar   32.5 ->  33.5     13,000      8    0     13.9
Togekiss    27.0 ->  28.5     13,500     12    0     10.6
Dragonite   15.0 ->  18.0     12,000     12    0      8.3
Metagross*   18.5 ->  21.5      7,600     17    0      7.7
...
9 Pokemon | stardust 99,100 / 100,000 (900 unspent) | total gain 95.4
* lucky (half stardust) or purified (10% off)
```

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
    Budget    Greedy    Solver    Delta       %
    10,000       9.3      13.6     4.26  45.64%
    25,000      37.3      42.6     5.31  14.24%
    50,000      58.8      65.9     7.11  12.10%
   100,000      88.7     102.6    13.92  15.70%
   200,000     113.2     143.5    30.25  26.71%
   400,000     124.3     190.2    65.85  52.97%
   800,000     124.3     222.9    98.57  79.29%
```

Greedy now loses at every budget tested (correcting the candy table made the second resource bind harder). It fails in two ways. It
commits stardust to a high-efficiency small step and then cannot afford the
larger step on the same Pokémon that would have been worth more. And it
plateaus — past ~242,000 stardust it cannot deploy additional budget at all,
because every Pokémon is already locked to a small upgrade. The solver keeps
finding uses for the marginal dust.

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

**MySQL** — the original `POGOR` schema, joining `my_pokemon` against
`species_stats`, `fast_move_stats` and `charge_move_stats`. Credentials come
from the environment:

```bash
pip install -r requirements-mysql.txt
cp .env.example .env   # then fill it in; .env is gitignored
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
credentials, Tesseract, or a phone. The appraisal-bar regions are expressed as
fractions of image size rather than pixel rows, so a screenshot from any device
works; `--calibrate` dumps the crops to check them against a real screenshot.

Reads that fail are flagged, not dropped. `iv_confidence` reports how close each
bar landed to a legal IV — a value near 0.5 means the crop region is wrong
rather than the Pokémon being unusual. Output goes to CSV so a bad read gets
fixed in a spreadsheet instead of by re-running OCR.

Frames are sampled and near-duplicates skipped. Scanning every frame of a
60-second clip is 1,800 OCR calls to read a collection that scrolls past maybe
forty Pokémon.

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
- **Type effectiveness is not modeled** (see above), so `--bulk` and the
  collector weights are the only tuning available.

## Layout

```
run.py              CLI
benchmark.py        greedy baseline comparison
ocr_ingest.py       screenshot/video -> CSV (optional extras)
pogo_opt/ingest.py  pure screenshot parsing
tests/              150 tests: mechanics, solver, parsing, OCR round-trip
pogo_opt/
  costs.py          CP multipliers, per-level power-up costs
  data.py           CSV and MySQL loaders
  model.py          rating function and the MIP
sample_data/        runnable example collection
```
