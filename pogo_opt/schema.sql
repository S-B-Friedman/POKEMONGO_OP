-- POGO collection database.
--
-- Two design decisions that are expensive to retrofit, so they're here from
-- the start even though there is currently one trainer and one device:
--
--   1. trainer_id on everything owned. Moving SQLite -> Postgres later is a
--      connection string; adding a tenant key to thousands of live rows is not.
--
--   2. Raw `observation` rows are kept separately from resolved `pokemon`.
--      OCR is lossy and the resolver will improve. Keeping the evidence means
--      re-resolving takes seconds instead of re-scanning thousands of Pokemon.
--
-- Tags like shiny/lucky/shadow are NOT detected from pixels. They come from
-- scan_session.search_filter: run the in-game search for `shiny`, scan that
-- pass, and every Pokemon in it is shiny by construction. Set membership
-- instead of image classification.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- identity

CREATE TABLE IF NOT EXISTS trainer (
    trainer_id   INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    friend_code  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Tile geometry and crop regions are per device, stored as fractions of the
-- screen so a profile survives a phone upgrade at a different resolution.
CREATE TABLE IF NOT EXISTS device_profile (
    device_profile_id INTEGER PRIMARY KEY,
    trainer_id        INTEGER NOT NULL REFERENCES trainer(trainer_id) ON DELETE CASCADE,
    label             TEXT NOT NULL,
    screen_width      INTEGER NOT NULL,
    screen_height     INTEGER NOT NULL,
    layout_json       TEXT NOT NULL,
    calibrated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (trainer_id, label)
);

-- ------------------------------------------------------------- game data
-- Shared reference data, not owned by any trainer.

CREATE TABLE IF NOT EXISTS species (
    species_id   INTEGER PRIMARY KEY,
    dex_number   INTEGER NOT NULL,
    name         TEXT NOT NULL,
    form         TEXT NOT NULL DEFAULT 'Normal',
    base_attack  INTEGER NOT NULL CHECK (base_attack  > 0),
    base_defense INTEGER NOT NULL CHECK (base_defense > 0),
    base_stamina INTEGER NOT NULL CHECK (base_stamina > 0),
    type1        TEXT NOT NULL,
    type2        TEXT,
    mega_capable INTEGER NOT NULL DEFAULT 0 CHECK (mega_capable IN (0, 1)),
    UNIQUE (dex_number, form)
);

CREATE TABLE IF NOT EXISTS move (
    move_id    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('fast', 'charge')),
    type       TEXT NOT NULL,
    power      INTEGER NOT NULL,
    energy     INTEGER NOT NULL,   -- fast: gained; charge: cost (33/50/100)
    duration   REAL NOT NULL,      -- seconds
    UNIQUE (name, kind)
);

CREATE TABLE IF NOT EXISTS type_effectiveness (
    attacking_type TEXT NOT NULL,
    defending_type TEXT NOT NULL,
    multiplier     REAL NOT NULL,
    PRIMARY KEY (attacking_type, defending_type)
);

-- ------------------------------------------------------------- scanning

CREATE TABLE IF NOT EXISTS scan_session (
    scan_session_id   INTEGER PRIMARY KEY,
    trainer_id        INTEGER NOT NULL REFERENCES trainer(trainer_id) ON DELETE CASCADE,
    device_profile_id INTEGER REFERENCES device_profile(device_profile_id),
    capture_mode      TEXT NOT NULL CHECK (capture_mode IN ('grid', 'detail')),
    -- The in-game search string used for this pass. NULL means unfiltered.
    -- 'shiny', 'lucky', 'shadow', 'purified', '4*', '3*', 'cp1000-2000', ...
    search_filter     TEXT,
    source_path       TEXT,
    frame_count       INTEGER,
    started_at        TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at       TEXT,
    notes             TEXT
);

-- One row per (frame, tile) read. Deliberately permissive: partial and even
-- contradictory reads are kept so the resolver can weigh them.
CREATE TABLE IF NOT EXISTS observation (
    observation_id  INTEGER PRIMARY KEY,
    scan_session_id INTEGER NOT NULL REFERENCES scan_session(scan_session_id) ON DELETE CASCADE,
    frame_label     TEXT,
    -- Global position after scroll tracking. Every (row, col) is exactly one
    -- Pokemon, which is how duplicates across frames collapse correctly even
    -- when you own two of the same species at the same CP.
    grid_row        INTEGER,
    grid_col        INTEGER,
    species_guess   TEXT,
    cp              INTEGER,
    hp              INTEGER,
    stardust_cost   INTEGER,
    candy_cost      INTEGER,
    level_hint      REAL,
    sprite_phash    TEXT,
    confidence      REAL,
    warnings        TEXT,
    raw_text        TEXT
);

CREATE INDEX IF NOT EXISTS idx_observation_session
    ON observation (scan_session_id);
CREATE INDEX IF NOT EXISTS idx_observation_slot
    ON observation (scan_session_id, grid_row, grid_col);

-- ------------------------------------------------------------ collection

CREATE TABLE IF NOT EXISTS pokemon (
    pokemon_id      INTEGER PRIMARY KEY,
    trainer_id      INTEGER NOT NULL REFERENCES trainer(trainer_id) ON DELETE CASCADE,
    species_id      INTEGER REFERENCES species(species_id),
    nickname        TEXT,
    cp              INTEGER,
    hp              INTEGER,
    level           REAL CHECK (level IS NULL OR (level >= 1 AND level <= 50)),
    attack_iv       INTEGER CHECK (attack_iv  IS NULL OR attack_iv  BETWEEN 0 AND 15),
    defense_iv      INTEGER CHECK (defense_iv IS NULL OR defense_iv BETWEEN 0 AND 15),
    stamina_iv      INTEGER CHECK (stamina_iv IS NULL OR stamina_iv BETWEEN 0 AND 15),
    -- How many (level, IV) combinations remain consistent with what we've
    -- observed. 1 means solved; >1 means ambiguous; NULL means not yet solved.
    iv_candidates   INTEGER,
    star_tier       INTEGER CHECK (star_tier IS NULL OR star_tier BETWEEN 0 AND 4),
    is_shiny        INTEGER NOT NULL DEFAULT 0 CHECK (is_shiny    IN (0, 1)),
    is_lucky        INTEGER NOT NULL DEFAULT 0 CHECK (is_lucky    IN (0, 1)),
    is_shadow       INTEGER NOT NULL DEFAULT 0 CHECK (is_shadow   IN (0, 1)),
    is_purified     INTEGER NOT NULL DEFAULT 0 CHECK (is_purified IN (0, 1)),
    fast_move_id    INTEGER REFERENCES move(move_id),
    charge_move_id  INTEGER REFERENCES move(move_id),
    first_seen_scan INTEGER REFERENCES scan_session(scan_session_id),
    last_seen_scan  INTEGER REFERENCES scan_session(scan_session_id),
    -- A Pokemon cannot be both shadow and purified at the same time.
    CHECK (NOT (is_shadow = 1 AND is_purified = 1))
);

CREATE INDEX IF NOT EXISTS idx_pokemon_trainer  ON pokemon (trainer_id);
CREATE INDEX IF NOT EXISTS idx_pokemon_species  ON pokemon (trainer_id, species_id);

-- Which raw reads were resolved into which Pokemon. Lets you trace any field
-- back to the frame it came from, and re-resolve without re-scanning.
CREATE TABLE IF NOT EXISTS observation_link (
    observation_id INTEGER PRIMARY KEY REFERENCES observation(observation_id) ON DELETE CASCADE,
    pokemon_id     INTEGER NOT NULL REFERENCES pokemon(pokemon_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_link_pokemon ON observation_link (pokemon_id);

-- Surviving (level, IV) combinations while a Pokemon is still ambiguous.
-- Dropped down to a single row once a later observation disambiguates.
CREATE TABLE IF NOT EXISTS iv_candidate (
    pokemon_id  INTEGER NOT NULL REFERENCES pokemon(pokemon_id) ON DELETE CASCADE,
    level       REAL    NOT NULL,
    attack_iv   INTEGER NOT NULL CHECK (attack_iv  BETWEEN 0 AND 15),
    defense_iv  INTEGER NOT NULL CHECK (defense_iv BETWEEN 0 AND 15),
    stamina_iv  INTEGER NOT NULL CHECK (stamina_iv BETWEEN 0 AND 15),
    PRIMARY KEY (pokemon_id, level, attack_iv, defense_iv, stamina_iv)
);

-- ------------------------------------------------------------- resources

CREATE TABLE IF NOT EXISTS candy_inventory (
    trainer_id INTEGER NOT NULL REFERENCES trainer(trainer_id) ON DELETE CASCADE,
    species_id INTEGER NOT NULL REFERENCES species(species_id),
    candy      INTEGER NOT NULL DEFAULT 0 CHECK (candy    >= 0),
    xl_candy   INTEGER NOT NULL DEFAULT 0 CHECK (xl_candy >= 0),
    PRIMARY KEY (trainer_id, species_id)
);

CREATE TABLE IF NOT EXISTS trainer_resource (
    trainer_id INTEGER PRIMARY KEY REFERENCES trainer(trainer_id) ON DELETE CASCADE,
    stardust   INTEGER NOT NULL DEFAULT 0 CHECK (stardust >= 0),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ----------------------------------------------------------------- views

-- The shape the solver's loader expects. Only fully-resolved Pokemon with a
-- known moveset qualify; ambiguous ones are excluded rather than guessed at.
CREATE VIEW IF NOT EXISTS v_solver_collection AS
SELECT
    p.pokemon_id                              AS instance_id,
    p.trainer_id                              AS trainer_id,
    s.dex_number                              AS species_id,
    COALESCE(p.nickname, s.name)              AS name,
    p.level                                   AS level,
    p.attack_iv, p.defense_iv, p.stamina_iv,
    s.base_attack, s.base_defense, s.base_stamina,
    fm.name     AS fast_move,
    fm.power    AS fast_power,
    fm.duration AS fast_duration,
    fm.energy   AS fast_energy,
    fm.type     AS fast_type,
    cm.name     AS charge_move,
    cm.power    AS charge_power,
    cm.duration AS charge_duration,
    cm.energy   AS charge_energy,
    cm.type     AS charge_type,
    s.type1, s.type2,
    p.is_shadow, p.is_lucky, p.is_purified, p.is_shiny
FROM pokemon p
JOIN species s ON s.species_id = p.species_id
LEFT JOIN move fm ON fm.move_id = p.fast_move_id
LEFT JOIN move cm ON cm.move_id = p.charge_move_id
WHERE p.level      IS NOT NULL
  AND p.attack_iv  IS NOT NULL
  AND p.defense_iv IS NOT NULL
  AND p.stamina_iv IS NOT NULL;

-- What still needs a detail scan, worst-ambiguity first. This is the queue
-- the swipe-through works from.
CREATE VIEW IF NOT EXISTS v_needs_detail_scan AS
SELECT
    p.pokemon_id,
    p.trainer_id,
    COALESCE(p.nickname, s.name) AS name,
    p.cp,
    p.iv_candidates,
    p.star_tier
FROM pokemon p
LEFT JOIN species s ON s.species_id = p.species_id
WHERE p.iv_candidates IS NULL OR p.iv_candidates > 1
ORDER BY COALESCE(p.iv_candidates, 99999) DESC, p.cp DESC;
