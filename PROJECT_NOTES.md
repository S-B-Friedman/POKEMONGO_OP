# Project notes

Working state and the reasoning behind it. The code is in the repo; this file
exists for the things that are expensive to reconstruct from code alone.

---

## Where each piece stands

| Component | State | Verified how |
|---|---|---|
| Optimizer | Done | 262 tests; beats greedy at all 7 benchmark budgets |
| Game mechanics | Done | `combat_power()` reproduces 5 published CPs exactly |
| Cost tables | Done | Diffed against GAME_MASTER across all 49 levels |
| Level solver | Done | Round-trips exactly across levels and IV spreads |
| Frame voting | Done | Survives corrupted frames in synthetic runs |
| SQLite schema | Done, **not wired** | 23 tests: scoping, cascades, constraints |
| Parsing layer | Done | 21 tests, no images or API keys needed |
| Reference data | Done | 1,486 species / 384 moves from GAME_MASTER |
| Poke Genie import | Done | Round-trips level ranges against recomputed CP |
| HTTP API | Done, in-memory | Swap `STATE` for `db.py` next |
| Bar crop geometry | Calibrated | Real 1206x2622 capture; IVs reproduce CP **and** HP |
| Grid tile geometry | **Not built** | Needs a real grid screenshot |
| Scroll tracking | **Not built** | Needs a real swipe video |

---

## Decisions worth not re-litigating

**Shinies are set membership, not image classification.** Pokémon GO's bag
search filters the grid: type `shiny` and only shinies show. Scan that pass and
everything in it is shiny by construction. No sprite reference database, no
badge template matching, and it works identically for species whose shiny form
is nearly indistinguishable (Bulbasaur, Machop). Same trick handles `lucky`,
`shadow`, `purified`, `4*`, `3*`, `cp1000-2000`. This is why
`scan_session.search_filter` exists and why `apply_filter_tag()` is three lines.

**Appraise stays open across swipes**, so IV bars are read directly, not
inferred. That inverts the hard problem: IVs are known, *level* is the unknown.
`levels_from_cp()` solves it exactly. It returns a list because CP is floored —
at low levels adjacent half-levels can collapse to the same CP. HP breaks most
of those ties.

**3 seconds per Pokémon at 30fps is ~90 frames of the same screen.** All of the
robustness comes from exploiting that. Discrete fields (name, CP) use majority
vote; bar fills use median-with-tolerance, because anti-aliasing means no two
frames produce identical fills and exact-match voting reports 0% agreement on
good data.

**Raw observations are stored separately from resolved Pokémon.** OCR is lossy
and the resolver will improve. Keeping the evidence means re-resolving takes
seconds instead of re-scanning thousands of Pokémon. Deleting a scan session
cascades its observations but leaves the collection intact — there's a test
pinning this because getting it backwards would be catastrophic.

**`trainer_id` on everything owned, from day one.** SQLite → Postgres later is a
connection string. Adding a tenant key to thousands of live rows is not.

**Device geometry stored as fractions**, so calibration survives a new phone.

**Objective is marginal gain, not absolute rating.** Maximizing total rating of
the chosen set just picks whatever was already strongest. The question is what
to *spend on*, which is a question about improvement per unit cost.

---

## Caveats and open questions

- **XL candy boundary is level 40.0**, verified against GameMaster's
  `xlCandyMinPokemonLevel` (see `scripts/build_reference.py --check-costs`).
  A prior revision had this at 41.0 — plausible since XL amounts restart
  their own 10/12/15/17/20 progression, but the ladder sat one level high.
- **`BarLayout` is calibrated now, against one device.** Measured on a real
  1206x2622 capture. The vertical fractions had been very nearly right
  (0.760/0.807/0.854 against a measured 0.767/0.809/0.851); the horizontal ones
  were not, because they assumed the bars span the screen when the appraisal
  card occupies only the lower left. Prefer `detect_bars()`, which finds the
  bars in the image and ignores the fractions entirely — they are the fallback.
  One device at one resolution is still a calibration, not a guarantee.

  Validation worth repeating on any new device: extract the IVs, then check
  that exactly one level reproduces both the displayed CP and the displayed HP.
  It did for both test subjects (15/14/14 at L11 → CP 292 / HP 55; 14/4/4 at
  L8 → CP 313 / HP 49). Two independent observations agreeing on one level is
  much stronger evidence than a bar that merely looks right.

- **A maxed stat is drawn red, not orange.** At 15/15 the game recolours the
  whole bar. Any orange-only reader scores a perfect stat as zero, which is the
  worst error available here — it turns the best Pokémon in a collection into
  the worst, confidently.
- **Type effectiveness is not modeled.** The rating scores against a fixed
  reference defender (180). Ratings are comparable to each other, not
  calibrated to any real matchup.
- **Single-period allocation.** Stardust accrues daily, so the real problem is
  multi-period; spending early compounds differently than spending late.
- **Mega energy is not modeled**, which is why the mega cap is off by default.
- **Poke Genie and Calcy IV already do screenshot IV scanning.** The
  differentiator here is the allocation solver, not the OCR. This is also why
  the Poke Genie CSV importer exists: reusing their scan output is cheaper than
  competing with it.
- **`benchmark.py`'s greedy baseline used one candy pool — fixed, and it did
  not move the numbers.** It charged regular and XL candy against the same
  stock, the defect already fixed in the solver. The correction was real but
  **latent on `sample_data`**: the only two Pokémon that can reach XL territory
  are species 149 (120 candy / 42 XL) and 376 (75 / 38), and both stocks exceed
  the 10-XL requirement, so neither pool ever binds. The published table is
  unchanged and was accurate as printed. It would bite on any collection where
  XL is scarce while regular candy is plentiful, which is the normal case at
  high level — hence
  `test_greedy_baseline_tracks_xl_candy_separately`, which constructs the
  binding case rather than trusting the bundled sample.
- **Shadow power-ups now cost 20% more** (`SHADOW_COST_SURCHARGE = True`).
  GAME_MASTER carries `shadowStardustMultiplier: 1.2` and
  `shadowCandyMultiplier: 1.2`; this was previously off on the belief that
  Niantic had removed the surcharge and left the fields vestigial. Turned on as
  a judgement call, not a measurement — the authoritative data says the
  multipliers exist, and the two errors are not symmetric. Charging a surcharge
  that no longer applies just makes the plan mildly conservative about shadows;
  omitting one that does apply underprices every shadow by 20% on *both*
  resources and lets the solver overcommit. Still worth one in-game
  confirmation: compare a shadow's Power Up cost against a non-shadow at the
  same level.

- **Purification cost is deliberately not modelled.** Converting a shadow costs
  stardust and candy of its own, and none of it is in the budget. Judged not
  worth modelling because the conversion is rare in practice; note that this
  makes any purified Pokémon in a collection look retroactively free.
- **sample_data was substantially wrong, and is now generated.** Not "a few
  stats": 24 distinct moves disagreed with GAME_MASTER (Counter 8/0.9/7 against
  a real 13/1.0/9, Stone Edge at half its true energy cost), plus four base
  stamina values, plus **two rows that were physically short one field** — so
  every column after the omission was read one position left. That is why
  Gardevoir carried `type2 == "0"` and silently lost its Psychic typing.
  Nothing validated row width, so it never surfaced.

  `scripts/rebuild_sample_data.py` now generates the stat columns from
  `reference.json` and `--check` fails on drift. **Correcting it moved every
  published figure**, including the benchmark table: the solver now ties greedy
  at the 25,000 budget instead of winning outright. The old numbers were
  computed from bad inputs, so they were never the solver's real margin.

---

## Security items outstanding

**These refer to the earlier repo, not this one.** This repository's full
working tree and all of its history were scanned: no credentials, no
`POGOR.sql`, no service-account paths. Credentials here come from the
environment only (`POGO_DB_*`, see `.env.example`), and `.env` is gitignored.
The items below are still open *elsewhere* and are kept so they don't get lost.

- Rotate the MySQL root password — it was hardcoded in the earlier public repo,
  so treat it as compromised regardless. Deleting the line is not enough; it is
  in that repo's history, which is why this one was started fresh rather than
  filtered.
- Delete the GCP service account key in project `axial-entity-398308`. The key
  contents were never committed, but the path was, and the key was sitting in
  Downloads.
- Read `POGOR.sql` before publishing it anywhere — schema dumps sometimes carry
  `GRANT` statements or seed credentials. It has never been added here.
- Check the 3 open GitHub Issues on the old repo.

---

## Next up

1. Point the API at `db.py` instead of the in-memory `STATE` dict. The schema
   and its tests already exist and are already scoped by `trainer_id`.
2. Name and CP OCR against real frames. The bars now read correctly from a real
   capture, but nothing else on the screen has ever been run through OCR on
   real pixels — only synthetic images. That is the next thing that will be
   wrong in a way the tests cannot see.
3. Grid tile geometry — the grid is regular, so slice deterministically from a
   calibrated pitch and origin rather than detecting contours.
4. Scroll tracking: phase-correlate consecutive frames, accumulate global Y,
   assign each tile a global (row, col). Every slot is exactly one Pokémon,
   which is how duplicates collapse correctly even with two same-species
   same-CP Pokémon.
5. Wire `ocr_ingest.py` to write into SQLite instead of CSV.
6. Later: type effectiveness as a coverage portfolio.

Done since this list was last written: dual values to surface which constraint
is actually binding (`Result.shadow_prices`), committed GAME_MASTER reference
data with a `--check-costs` guard against silent drift, the greedy baseline's
two-pool fix, and CI that runs the suite and the cost check on every push.
