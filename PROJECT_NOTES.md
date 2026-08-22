# Project notes

Working state and the reasoning behind it. The code is in the repo; this file
exists for the things that are expensive to reconstruct from code alone.

---

## Where each piece stands

349 tests, run on 3.11 and 3.12 by CI on every push and pull request, with
**no skips**. 345 need nothing beyond `requirements-dev.txt`; the other 4 are
the image path, needing OpenCV, Pillow and the tesseract binary, all of which
CI installs. A test that skips itself is not a test that passed, and the
summary line does not distinguish them — so the extras are installed rather
than allowed to quietly disable coverage.

| Component | State | Verified how |
|---|---|---|
| Optimizer | Done | 137 tests; beats greedy at 6 of 7 budgets, ties at the 7th |
| Image path | Done | 4 tests, synthetic screenshots painted like the real UI |
| Game mechanics | Done | `combat_power()` reproduces 5 published CPs exactly |
| Cost tables | Done | Diffed against GAME_MASTER across all 49 levels (75 tests) |
| Level solver | Done | 31 tests; round-trips across levels and IV spreads |
| Frame voting | Done | Survives corrupted frames in synthetic runs |
| SQLite schema | Done, **not wired** | 23 tests: scoping, cascades, constraints |
| Parsing layer | Done | 24 tests, no images or API keys needed |
| Reference data | Done | 1,486 species / 384 moves from GAME_MASTER |
| Sample data | Generated | From `reference.json`; `--check` gates CI |
| Poke Genie import | Done | Round-trips level ranges against recomputed CP |
| HTTP API | Done, in-memory | 8 tests; every candy path was untested and broken |
| Appraisal bars | Calibrated | 23 tests; real capture, IVs reproduce CP **and** HP |
| CP from a screenshot | Checked, not trusted | OCR proposes; only a CP the IVs and HP can reproduce is kept |
| Species name from a screenshot | **Weak** | No arithmetic to check it against; misreads survive |
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
- **Candy is pooled per evolution FAMILY, not per species.** Gible, Gabite and
  Garchomp draw on one pile, and the game labels it that way — a Garchomp's
  detail screen reads "GIBLE CANDY". The solver constrained per `species_id`,
  so 20 Gible candy funded 20 on the Gible *and* 20 on the Garchomp, and the
  plan reported itself `fully_costed`. Twice the candy that exists, presented
  as affordable. Any collection holding a pre-evolution alongside its evolution
  was affected, which is most of them.

  Inventories are keyed by family now. Both keyings are accepted and normalized
  at the door, because re-keying a lookup without re-keying its callers drops
  every constraint and looks exactly like success — which it briefly did, and
  three tests caught it.

  The same mismatch broke the OCR: the candy label names the family, so
  matching it against the species name failed for every evolved Pokemon. It
  went unnoticed because the Pokemon it was developed against — Swinub,
  Anorith, Palkia — are all their own family.

  **And it broke both candy endpoints in the HTTP API**, which had no tests at
  all — the only component in the repo with none, and the one that broke.
  `api.py` kept speaking `species_id` while the inventory moved to families, so
  it addressed the dict by keys it does not hold:

  - `GET /candy` reported `known: false` for all twenty species of a fully
    populated inventory. The endpoint exists to tell a UI which numbers are
    worth asking for, and it asked for every one the trainer had already given.
  - `PUT /candy` broke the *next* solve outright rather than misreporting.
    Writing the raw `species_id` left two counts for one pile, and
    `build_and_solve` refuses that deliberately — "candy inventory gives two
    different counts for the Squirtle family (190 and 0)". Every PUT against a
    loaded inventory hit it, so the endpoint could not be used at all.
  - `POST /solve` returned a 500 for any collection with an unrecorded candy
    stock, which is the **default** state after a Poke Genie import — those
    exports carry no candy at all. `unbacked_candy` is keyed by pool, so its
    real keys are family names, while both `Result` and the response model
    declared `dict[int, ...]`; pydantic rejected `'Squirtle'` as an integer.
    The field added to *warn* about missing candy was what made the endpoint
    *fail* on missing candy. This is the worst of the three: the other two
    degrade a UI, this one blocks the primary endpoint on the primary path.

  While fixing it, `candy_warning()` stopped saying "species Dratini needs 28".
  The key names a pile, and calling it a species sends someone looking for a
  Dratini they may not own — the Dragonite they do own is what draws on it.

  Both go through `candy_pool_key` now, and `GET /candy` reports the `family`
  so a caller can see that two rows share one pile rather than reading the
  repeated number as two independent stocks. The six new tests were checked
  against the unfixed code first: all six fail there.

  **One instance is still live and deliberately unfixed.** `pogo_opt/db.py`
  returns candy keyed by `dex`, not by family. Nothing calls it — the DB is not
  wired — so it is not a bug today, but it is a landmine for the "swap `STATE`
  for `db.py`" step: a trainer with rows for both a Gible and a Garchomp would
  hand `build_and_solve` two counts for one pile and get the same refusal
  `PUT /candy` used to trigger. Fix it as part of wiring the DB, where the
  schema question (store per family, or per species and pool on read?) can be
  answered properly rather than patched at the boundary.

  Worth drawing the general lesson, since this is the third instance. Changing
  what a mapping is keyed by is not a local edit. Each time, the lookup was
  re-keyed and one caller was not, and each time the result read as success
  rather than as an error — a dropped constraint, a failed name match, an empty
  inventory. Grepping for the callers is the cheap part; the expensive part is
  that nothing fails loudly when you miss one.

- **Renaming is why the candy label is the better identity.** A nickname is
  user-controlled and can be anything; the candy label is generated by the game
  and names the family. The current code still reads the nickname and treats
  the label as confirmation, which has it backwards for a heavily-renamed
  collection.

- **Friendship states compose; they are not one label.** `PokemonInstance.
  friendship` used to return a single string chosen by precedence, so a
  purified Pokémon that had later been traded came back as `"lucky"` and lost
  its purified discount on both resources — an 11% stardust overcharge. It now
  returns the set of states, and `costs.friendship_multipliers()` composes them
  multiplicatively. Latent on `sample_data`, where nothing is in two states at
  once, so no published figure moved.

  **Confirmed multiplicative**: lucky + purified is 0.5 × 0.9 = 0.45, i.e. 55%
  off stardust, which is what the code produces. GAME_MASTER cannot settle this
  — it has no lucky multiplier at all — so it rests on reported in-game
  behaviour. Still open: whether shadow+lucky is genuinely impossible; it is
  rejected on the reasoning that shadows cannot be traded.

- **A Poke Genie import supplies no candy, and that is not a small gap.** The
  export describes Pokémon, not your bag, so `candy_inventory` comes back empty
  and *every* per-species candy constraint is simply absent. The plan is then
  optimal against stardust alone and can prescribe upgrades you cannot pay the
  candy for.

  This is the failure mode `model.py` already calls the worst one it has: a
  constraint that silently stops binding looks exactly like a satisfied one. It
  is now reported — `Result.fully_costed`, `Result.unbacked_candy`, a warning
  from `run.py`, and `fully_costed` / `candy_warning` on `POST /solve`.

  Worth knowing how little the bundled sample exercises this: candy binds only
  at an 800,000 stardust budget. Below that, solving with and without the candy
  file gives identical results, so `sample_data` cannot demonstrate the
  constraint that the whole design is built around. A real collection at high
  level, where XL is scarce, is where it bites.

  **The detail screen's resource row now reads.** `read_resource_row()` pulls
  stardust, per-species candy and XL candy off it, and `write_candy_csv()`
  emits exactly what `load_candy_inventory` reads. Verified end to end on a real
  capture: 521,865 stardust / 1,645 Swinub candy / 293 XL, all three correct.

  Two things it took to get there, both worth keeping in mind for the rest of
  the screen. The numerals are dark teal and the icons beside them are bright
  orange, and thresholding on brightness alone leaves dark icon edges that
  Tesseract reads as digits — the candy icon turned 1,645 into 21,645. And the
  labels are a lighter grey than the numerals, so one threshold cannot serve
  both: tuned for the numbers it erased the labels, and a frame with three
  perfectly-read values was discarded for having no species name.

  The throughput limit is the real one though. One detail screen shows one
  species, so a collection needs one screen per species — a 7.7s clip yielded
  exactly one.

- **The purified candy discount mostly rounds away.** Per-step candy is small
  and the code takes `ceil`, so `ceil(8 * 0.9) == 8`: the 10% discount is
  invisible on 68 of the 98 half-levels, surfacing only in the 10/12/15/17/20
  tiers. GAME_MASTER ships the multipliers but not the rounding rule. `ceil`
  errs toward overcharging, which is the safer direction, but it is a guess —
  worth one in-game check against a purified Pokémon in a 10+ candy tier.

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

1. Get candy counts in from somewhere. They are the second resource the whole
   model is built around, and no import path supplies them. The numbers are on
   the Pokemon detail screen, so OCR is the obvious route; `PUT /candy` and
   `--candy` already accept them by hand in the meantime.
2. Point the API at `db.py` instead of the in-memory `STATE` dict. The schema
   and its tests already exist and are already scoped by `trainer_id`.
3. Name and CP OCR against real frames. The bars now read correctly from a real
   capture, but nothing else on the screen has ever been run through OCR on
   real pixels — only synthetic images. That is the next thing that will be
   wrong in a way the tests cannot see.
4. Grid tile geometry — the grid is regular, so slice deterministically from a
   calibrated pitch and origin rather than detecting contours.
5. Scroll tracking: phase-correlate consecutive frames, accumulate global Y,
   assign each tile a global (row, col). Every slot is exactly one Pokémon,
   which is how duplicates collapse correctly even with two same-species
   same-CP Pokémon.
6. Wire `ocr_ingest.py` to write into SQLite instead of CSV.
7. Later: type effectiveness as a coverage portfolio.

Also worth one in-game check whenever convenient: a shadow's Power Up cost
against a non-shadow at the same level, which settles `SHADOW_COST_SURCHARGE`.

Done since this list was last written:

- Dual values, to surface which constraint is actually binding
  (`Result.shadow_prices`).
- Committed GAME_MASTER reference data, with `--check-costs` guarding drift.
  This caught the XL candy boundary sitting a level high.
- The greedy baseline's two-pool fix, so the benchmark compares like with like.
- `sample_data` regenerated from `reference.json` — 24 moves and 4 base stats
  had been wrong, and two rows were malformed.
- CI on 3.11 and 3.12, including the OCR extras so the image path actually runs.
- Appraisal bars calibrated and rewritten against a real capture.
- The 20% shadow power-up surcharge.
