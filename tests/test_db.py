"""Tests for the SQLite layer: scoping, constraints, and the filter-tag trick."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pogo_opt import db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def trainer(conn):
    tid = db.get_or_create_trainer(conn, "Sam")
    conn.execute(
        """INSERT INTO species (species_id, dex_number, name, base_attack,
                                base_defense, base_stamina, type1, mega_capable)
           VALUES (1, 6, 'Charizard', 223, 173, 186, 'fire', 1)"""
    )
    conn.execute(
        """INSERT INTO move (move_id, name, kind, type, power, energy, duration)
           VALUES (1, 'Fire Spin', 'fast', 'fire', 14, 10, 1.1),
                  (2, 'Blast Burn', 'charge', 'fire', 110, 50, 3.3)"""
    )
    return tid


def test_schema_applies_twice(conn):
    db.init_db(conn)   # must be idempotent


def test_trainer_is_idempotent(conn):
    a = db.get_or_create_trainer(conn, "Sam")
    b = db.get_or_create_trainer(conn, "Sam")
    assert a == b


def test_trainers_are_isolated(conn, trainer):
    other = db.get_or_create_trainer(conn, "Friend")
    db.upsert_pokemon(conn, trainer, species_id=1, cp=2451, level=25.0,
                      attack_iv=15, defense_iv=15, stamina_iv=15,
                      fast_move_id=1, charge_move_id=2)
    assert len(db.load_collection(conn, trainer)) == 1
    assert len(db.load_collection(conn, other)) == 0


def test_foreign_keys_are_enforced(conn, trainer):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO pokemon (trainer_id, species_id) VALUES (?, 999)",
                     (trainer,))


def test_iv_range_is_enforced(conn, trainer):
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_pokemon(conn, trainer, species_id=1, attack_iv=16)


def test_shadow_and_purified_are_mutually_exclusive(conn, trainer):
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_pokemon(conn, trainer, species_id=1, is_shadow=1, is_purified=1)


def test_deleting_a_trainer_cascades(conn, trainer):
    db.upsert_pokemon(conn, trainer, species_id=1, cp=100)
    conn.execute("DELETE FROM trainer WHERE trainer_id = ?", (trainer,))
    assert conn.execute("SELECT COUNT(*) FROM pokemon").fetchone()[0] == 0


# ------------------------------------------------- the shiny filter trick

def test_filtered_pass_tags_everything_in_it(conn, trainer):
    """Membership in a `shiny` scan IS the shiny flag."""
    pid = db.upsert_pokemon(conn, trainer, species_id=1, cp=2451)
    scan = db.start_scan(conn, trainer, "grid", search_filter="shiny")
    db.record_observations(conn, scan, [{"cp": 2451, "grid_row": 0, "grid_col": 0}])
    obs_id = conn.execute("SELECT observation_id FROM observation").fetchone()[0]
    db.link_observation(conn, obs_id, pid)

    assert db.apply_filter_tag(conn, scan) == 1
    assert conn.execute(
        "SELECT is_shiny FROM pokemon WHERE pokemon_id = ?", (pid,)
    ).fetchone()[0] == 1


def test_unfiltered_pass_tags_nothing(conn, trainer):
    scan = db.start_scan(conn, trainer, "grid", search_filter=None)
    assert db.apply_filter_tag(conn, scan) == 0


def test_star_filter_pass_sets_no_boolean(conn, trainer):
    """A `4*` pass carries information but not a boolean column."""
    scan = db.start_scan(conn, trainer, "grid", search_filter="4*")
    assert db.apply_filter_tag(conn, scan) == 0


def test_bad_capture_mode_rejected(conn, trainer):
    with pytest.raises(ValueError):
        db.start_scan(conn, trainer, "sideways")


# ------------------------------------------------------------ observations

def test_observations_survive_partial_reads(conn, trainer):
    """Bad reads are evidence too -- they must not be dropped at write time."""
    scan = db.start_scan(conn, trainer, "detail")
    n = db.record_observations(conn, scan, [
        {"cp": 2451, "species_guess": "Charizard"},
        {"cp": None, "species_guess": None, "warnings": ["CP not found"]},
    ])
    assert n == 2
    assert conn.execute("SELECT COUNT(*) FROM observation").fetchone()[0] == 2


def test_warnings_list_is_flattened(conn, trainer):
    scan = db.start_scan(conn, trainer, "detail")
    db.record_observations(conn, scan, [{"warnings": ["a", "b"]}])
    assert conn.execute("SELECT warnings FROM observation").fetchone()[0] == "a; b"


def test_deleting_a_scan_keeps_resolved_pokemon(conn, trainer):
    """Re-scanning must not wipe the collection."""
    pid = db.upsert_pokemon(conn, trainer, species_id=1, cp=2451)
    scan = db.start_scan(conn, trainer, "detail")
    db.record_observations(conn, scan, [{"cp": 2451}])
    conn.execute("DELETE FROM scan_session WHERE scan_session_id = ?", (scan,))
    assert conn.execute("SELECT COUNT(*) FROM observation").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM pokemon WHERE pokemon_id = ?", (pid,)
    ).fetchone()[0] == 1


# ------------------------------------------------------ resolution + views

def test_ambiguous_pokemon_stay_out_of_the_solver_view(conn, trainer):
    db.upsert_pokemon(conn, trainer, species_id=1, cp=2451, iv_candidates=7)
    assert db.load_collection(conn, trainer) == []


def test_single_candidate_promotes_to_resolved(conn, trainer):
    pid = db.upsert_pokemon(conn, trainer, species_id=1, cp=2451,
                            fast_move_id=1, charge_move_id=2)
    db.set_iv_candidates(conn, pid, [(25.0, 15, 10, 7)])
    roster = db.load_collection(conn, trainer)
    assert len(roster) == 1
    assert roster[0]["level"] == 25.0 and roster[0]["attack_iv"] == 15
    assert roster[0]["charge_energy"] == 50


def test_multiple_candidates_stay_unresolved(conn, trainer):
    pid = db.upsert_pokemon(conn, trainer, species_id=1, cp=2451)
    db.set_iv_candidates(conn, pid, [(25.0, 15, 10, 7), (25.5, 14, 10, 7)])
    assert db.load_collection(conn, trainer) == []
    assert [p["pokemon_id"] for p in db.needs_detail_scan(conn, trainer)] == [pid]


def test_detail_queue_is_worst_first(conn, trainer):
    a = db.upsert_pokemon(conn, trainer, species_id=1, cp=100, iv_candidates=2)
    b = db.upsert_pokemon(conn, trainer, species_id=1, cp=200, iv_candidates=40)
    assert [p["pokemon_id"] for p in db.needs_detail_scan(conn, trainer)] == [b, a]


def test_candy_inventory_keys_on_dex_number(conn, trainer):
    conn.execute(
        """INSERT INTO candy_inventory (trainer_id, species_id, candy, xl_candy)
           VALUES (?, 1, 240, 12)""", (trainer,))
    candy, xl = db.candy_inventories(conn, trainer)
    assert candy == {6: 240} and xl == {6: 12}


def test_device_profile_round_trips(conn, trainer):
    layout = {"attack_top": 0.76, "bar_height": 0.014}
    db.save_device_profile(conn, trainer, "pixel8", 1080, 2400, layout)
    assert db.load_device_profile(conn, trainer, "pixel8") == layout


def test_device_profile_recalibration_overwrites(conn, trainer):
    db.save_device_profile(conn, trainer, "pixel8", 1080, 2400, {"a": 1})
    db.save_device_profile(conn, trainer, "pixel8", 1080, 2400, {"a": 2})
    assert db.load_device_profile(conn, trainer, "pixel8") == {"a": 2}
    assert conn.execute("SELECT COUNT(*) FROM device_profile").fetchone()[0] == 1


def test_summary_counts(conn, trainer):
    p1 = db.upsert_pokemon(conn, trainer, species_id=1, cp=1, iv_candidates=1)
    db.upsert_pokemon(conn, trainer, species_id=1, cp=2, iv_candidates=9)
    scan = db.start_scan(conn, trainer, "detail")
    db.record_observations(conn, scan, [{"cp": 1}, {"cp": 2}])
    s = db.scan_summary(conn, trainer)
    assert s["pokemon"] == 2 and s["resolved"] == 1 and s["ambiguous"] == 1
    assert s["observations"] == 2 and s["scans"] == 1


def test_unknown_field_is_rejected(conn, trainer):
    with pytest.raises(ValueError):
        db.upsert_pokemon(conn, trainer, species_id=1, sparkliness=11)
