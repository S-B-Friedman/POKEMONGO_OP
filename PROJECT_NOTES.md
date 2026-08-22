# Project notes

Working state and the reasoning behind it. The code is in the repo; this file
exists for the things that are expensive to reconstruct from code alone.

---

## Where each piece stands

370 tests, run on 3.11 and 3.12 by CI on every push and pull request, with
**no skips**. 361 need nothing beyond `requirements-dev.txt`; the other 9 are
the image path, needing OpenCV, Pillow and the tesseract binary, all of which
CI installs. A test that skips itself is not a test that passed, and the
summary line does not distinguish them — so the extras are installed rather
than allowed to quietly disable coverage.

| Component | State | Verified how |
|---|---|---|
| Optimizer | Done | 137 tests; beats greedy at 6 of 7 budgets, ties at the 7th |
| Image path | Done | 9 tests, synthetic screenshots painted like the real UI |
| Game mechanics | Done | `combat_power()` reproduces 5 published CPs exactly |
| Cost tables | Done | Diffed against GAME_MASTER across all 49 levels (75 tests) |
| Level solver | Done | 37 tests; round-trips across levels and IV spreads |
| Frame voting | Done, **now actually used** | The video path kept 1 frame per Pokemon and never voted |
| SQLite schema | Done, **not wired** | 23 tests: scoping, cascades, constraints |
| Parsing layer | Done | 24 tests, no images or API keys needed |
| Reference data | Done | 1,486 species / 384 moves from GAME_MASTER |
| Sample data | Generated | From `reference.json`; `--check` gates CI |
| Poke Genie import | Done | Round-trips level ranges against recomputed CP |
| HTTP API | Done, in-memory | 8 tests; every candy path was untested and broken |
| Appraisal bars | Calibrated | 23 tests; real capture, IVs reproduce CP **and** HP |
| CP from a screenshot | Checked, not trusted | OCR proposes; only a CP the IVs and HP can reproduce is kept |
| Species from a screenshot | Checked, not trusted | 15/18 on real frames, 0 wrong; numbers narrow 1,486, text chooses within |
| Resource row | Located, not assumed | Found by its own labels; reads exactly at 8 of 8 screen positions |
| Swipe segmentation | Done | Grouped in time, so duplicates survive; 8/8 and 11/11 on real frames |
| Grid tile geometry | Not needed | Superseded by swiping through detail screens |

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

## Is the OCR done?

Reading **one** Pokémon: yes, and measured. Reading a **collection** from a
swipe-through: yes as of the segmentation work below, and measured on the frames
available — but not yet run against a full-length capture of a real box.

Measured over all 19 real frames available (two captures, 18 with an identifiable
subject), scoring the name against the "This X was caught on …" line — a
different part of the screen from the one being read:

```
name matched against 1,024 species: 15/18 correct, 0 WRONG, 3 unread
```

**Zero wrong is the number that matters**, and the three misses are all
nicknames: "Daj mahal 96" (a Zamazenta), "Drag'nite", "Sanji 100" (a Blaziken).
The matcher declines rather than forcing a nickname onto the nearest species,
which is the correct failure. Those three are also exactly the case
`verify_species()` exists for — CP, HP and the IV bars identify a Pokémon whose
name is unreadable, so the arithmetic can recover what the text cannot.

That measurement is also what caught `read_species_name()` raising `NameError`
on every call that supplied a species list: `ocr_ingest` never imported
`match_species_name`. Every internal caller passes no list and takes the regex
path, so nothing in the repo touched the broken branch. It took running the
accurate path against real frames — which nobody had done — to find that the
accurate path did not run at all.

### Reading a collection

The intended capture is a **swipe-through of detail screens with appraise open**,
not a grid scan. That choice removes grid tile geometry from the problem
entirely, and it is the better trade: the grid shows a sprite and a CP, while
the detail screen shows everything the solver needs.

Two things were wrong for that workflow, and both were silent.

**`dedupe()` collapsed genuine duplicates.** It keyed on `(name, cp)`, which
cannot tell three Machamps at CP 2451 apart from three readings of one Machamp.
Two of the three were dropped with nothing logged. A box of 1,500 is mostly
duplicates, so this was not an edge case — it was the common case.

**The video path never voted.** `iter_video_frames()` discarded frames that
looked like the previous one, leaving exactly one frame per Pokémon, and one
frame cannot be voted on. That quietly contradicted the design note above: the
~90 frames per Pokémon are the entire basis of the robustness claim, and they
were being thrown away before anything could use them.

Frames are grouped in **time** now — consecutive look-alike frames are one
Pokémon, a jump is the swipe — and every field is voted independently across the
group. Grouping in time is what saves the duplicates: two Machamps are two runs
regardless of reading identically. Measured on the real frames, 8/8 and 11/11
distinct Pokémon separated correctly, one group for 40 copies of one screen, and
two groups of 20 for a swipe in the middle.

IVs are voted as a **triple**, never per stat. Three bars are read off one image,
so a frame caught mid-animation is wrong about all three together; pairing an
attack from one frame with a defense from another would invent a spread that no
frame ever showed.

| Piece | State |
|---|---|
| Per-Pokémon read (bars, CP, species, level) | Done, measured |
| Resource row (candy) | Done, measured |
| Swipe segmentation and voting | Done, measured |
| Grid tile geometry | Not needed — superseded by the swipe-through |

What remains is not code: a swipe-through of a real box, long enough to hold on
each Pokémon, to confirm the grouping thresholds on a genuine capture rather than
on frames sampled every 60.

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

  Four things it took to get there, all worth keeping in mind for the rest of
  the screen. The numerals are dark teal and the icons beside them are bright
  orange, and thresholding on brightness alone leaves dark icon edges that
  Tesseract reads as digits — the candy icon turned 1,645 into 21,645, and the
  stardust icon turned 521,865 into 1,521,865. The fix that generalises is not a
  better threshold but a checkable property: the game groups its numbers in
  threes, so a separator in the wrong place proves the token is not a number the
  game wrote. The same rule accepts the comma that upscaled thin strips render
  as a period, since nothing in this row is a fractional quantity.

  The labels are a lighter grey than the numerals, so one threshold cannot serve
  both: tuned for the numbers it erased the labels, and a frame with three
  perfectly-read values was discarded for having no species name.

  **The row is found by its labels, not by a height fraction.** A fraction is
  calibrated on one phone and does not survive a change of aspect ratio — on a
  1080×1920 capture `PALKIA CANDY` sits at 0.707, a hair past a 0.70 cutoff, and
  the entire row went unread. Nothing else on the card says STARDUST or CANDY,
  so `locate_resource_row()` searches a wide band for those words and takes the
  numbers from whichever line turns out to be directly above. On a real card slid
  from 0.687 to 0.847 of screen height, 8 of 8 positions now read exactly.

  **And the column has to be identified, not counted.** Tesseract runs a label
  together as often as not: it read the candy column as one `SWINUBCANDY` token
  while reading the XL column as `SWINUB` + `CANDY`. Matching labels on equality
  with "CANDY" therefore saw one candy column — the XL one — took it for the
  ordinary one, and returned Swinub's 293 XL candy as 293 candy.

  What that cost end to end is worth stating exactly, because it is not what it
  looks like. `scan_resource_rows` threw the row away: the same merge that broke
  the column also contaminated the species name to `Swinubcandy`, which is not
  in the reference. So the observed damage was a *dropped* row, not a wrong
  budget — the pipeline failed closed.

  It failed closed by luck, though, not by design. Species was taken from the
  first label word long enough to be a name, so had tesseract emitted the XL
  column's clean `SWINUB` before the merged token, the species would have passed
  the reference check and the wrong candy count would have gone straight into
  the inventory with nothing to flag it. The ordering that saved it is not a
  property anyone chose.

  Matching on containment fixes the column; a lone column carrying the XL mark
  is now reported as XL with no candy rather than mislabelled; and the species
  is recovered by stripping the fixed words off whatever tesseract ran together,
  so it no longer depends on which token came first.

  The throughput limit is the real one though. One detail screen shows one
  species, so a collection needs one screen per species — a 7.7s clip yielded
  exactly one.

  **Candy cannot be read off the appraisal overlay, and it is worth knowing why
  rather than trying again.** Measured across 19 frames of two captures, 1 gave
  a usable resource row. The other 18 were appraisal frames, and on those:

  - The *values* read fine. Palkia's `129` candy comes back cleanly at 0.683 of
    screen height under a black-hat.
  - The *labels* do not read at all. Nothing in the band survives the dimming —
    not `PALKIA CANDY`, not `STARDUST`, at any threshold tried. So the column
    cannot be identified by its label, which is how every other path does it.
  - Position is not a substitute. Mega-capable Pokémon carry an extra Mega
    Energy element that shifts the row, so a fixed column index reads the wrong
    number for exactly the Pokémon most worth powering up.
  - Nor is icon colour, which was the remaining idea. On a clean card the candy
    icons are distinctly orange (H≈7–8) against the blue stardust icon (H≈143)
    and would anchor the column well. On the overlay the rating badge (H≈18,
    x 0.14–0.29) and the team leader (H≈100, x 0.33–0.91) saturate the entire
    row, and the icons are not separable from either.

  Three independent ways to find the column, all blocked by the same overlay.
  The practical consequence is a capture instruction, not a code change: **pause
  on the plain detail screen for each species**, with no appraisal open. The
  appraisal screen's job here is the IV bars, and those already read.

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
