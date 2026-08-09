# Project notes

Working state and the reasoning behind it. The code is in the repo; this file
exists for the things that are expensive to reconstruct from code alone.

---

## Where each piece stands

| Component | State | Verified how |
|---|---|---|
| Optimizer | Done | 150 tests; beats greedy at all 7 benchmark budgets |
| Game mechanics | Done | `combat_power()` reproduces 5 published CPs exactly |
| Level solver | Done | Round-trips exactly across levels and IV spreads |
| Frame voting | Done | Survives corrupted frames in synthetic runs |
| SQLite schema | Done | 23 tests: scoping, cascades, constraints |
| Parsing layer | Done | 21 tests, no images or API keys needed |
| Bar crop geometry | **Estimated** | Synthetic screenshot only — needs a real one |
| Grid tile geometry | **Not built** | Needs a real grid screenshot |
| Scroll tracking | **Not built** | Needs a real swipe video |
| `POGOR.sql` | **Never read** | — |

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

- **XL candy boundary is level 41.0.** This is the one number I'd re-verify
  against GameMaster. Evidence for 41.0: XL amounts restart at 10/12/15/17/20,
  and putting the boundary at 40.0 would start XL at 15 and break that
  progression. An earlier revision had this wrong at 40.0.
- **`BarLayout` fractions are estimates.** They work on a synthetic screenshot
  built to match them, which proves the machinery, not the numbers. Reading the
  wrong pixel rows produces confidently wrong IVs — worse than no IVs.
- **Type effectiveness is not modeled.** The rating scores against a fixed
  reference defender (180). Ratings are comparable to each other, not
  calibrated to any real matchup.
- **Single-period allocation.** Stardust accrues daily, so the real problem is
  multi-period; spending early compounds differently than spending late.
- **Mega energy is not modeled**, which is why the mega cap is off by default.
- **Poke Genie and Calcy IV already do screenshot IV scanning.** The
  differentiator here is the allocation solver, not the OCR.

---

## Security items outstanding

- Rotate the MySQL root password — it was hardcoded in the public repo, so
  treat it as compromised regardless. Deleting the line is not enough; it's in
  the git history.
- Delete the GCP service account key in project `axial-entity-398308`. The key
  contents were never committed, but the path was, and the key was sitting in
  Downloads.
- Read `POGOR.sql` before pushing — last unreviewed file, and schema dumps
  sometimes carry `GRANT` statements or seed credentials.
- Check the 3 open GitHub Issues.
- Force-push clean history (or delete and re-push fresh).

---

## Next up

1. Calibrate `BarLayout` against a real Appraise screenshot.
2. Grid tile geometry — the grid is regular, so slice deterministically from a
   calibrated pitch and origin rather than detecting contours.
3. Scroll tracking: phase-correlate consecutive frames, accumulate global Y,
   assign each tile a global (row, col). Every slot is exactly one Pokémon,
   which is how duplicates collapse correctly even with two same-species
   same-CP Pokémon.
4. Wire `ocr_ingest.py` to write into SQLite instead of CSV.
5. Later: type effectiveness as a coverage portfolio; dual values to surface
   which constraint is actually binding.
