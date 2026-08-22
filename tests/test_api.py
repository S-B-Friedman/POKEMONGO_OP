"""The HTTP layer's candy endpoints, which had no coverage at all.

That absence is why this broke. Candy moved to being keyed by evolution family
(a Gible and a Garchomp spend from one pile) and `api.py` went on speaking
species_id, so both endpoints silently addressed a dict by keys it does not
have. Nothing in the suite touched them, so nothing went red.

These call the endpoint functions directly rather than through a test client.
The bug lives in the handler bodies, not in routing or serialization, and a
direct call needs no extra HTTP dependency to expose it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")

import api  # noqa: E402
from api import CandyUpdate, SolveRequest  # noqa: E402

# Blastoise is species 9 and its candy pile is the Squirtle family's. The two
# differing is the whole point of these tests.
BLASTOISE = 9
BLASTOISE_FAMILY = "Squirtle"


@pytest.fixture
def loaded():
    """A fresh STATE with the bundled sample collection and inventory.

    api.py loads these in a FastAPI lifespan hook, which does not run when the
    module is merely imported -- so a test that forgets this sees an empty
    collection and passes for the wrong reason.
    """
    api.STATE.update(
        {"collection": [], "candy": {}, "xl_candy": {}, "source": "none"}
    )
    api._load_defaults()
    assert api.STATE["collection"], "sample collection did not load"
    assert api.STATE["candy"], "sample candy inventory did not load"
    yield
    api.STATE.update(
        {"collection": [], "candy": {}, "xl_candy": {}, "source": "none"}
    )


def test_get_candy_finds_the_stock_it_has(loaded):
    """Regression: every row came back `known: false` with a full inventory.

    The lookup used species_id against a dict keyed by family, so it missed all
    twenty entries. The endpoint exists to tell a UI which numbers are worth
    asking for; reporting all of them as unknown means asking for every number
    the trainer already supplied.
    """
    rows = api.get_candy()
    assert rows, "no species in the collection"
    assert all(r["known"] for r in rows)
    assert all(r["candy"] is not None for r in rows)


def test_get_candy_names_the_family_sharing_the_pile(loaded):
    """Two species of one family report the same number because it IS the same
    number. Without naming the family that reads as two independent stocks."""
    rows = {r["species_id"]: r for r in api.get_candy()}
    assert rows[BLASTOISE]["family"] == BLASTOISE_FAMILY
    assert rows[BLASTOISE]["name"] == "Blastoise"


def test_put_candy_does_not_break_the_next_solve(loaded):
    """Regression, and the sharp end of it: this raised rather than misreported.

    Writing the raw species_id left two counts for one pile, and build_and_solve
    refuses that on purpose -- "candy inventory gives two different counts for
    the Squirtle family (190 and 0)". Every PUT against a loaded inventory hit
    it, so the endpoint could not be used at all.
    """
    api.put_candy([CandyUpdate(species_id=BLASTOISE, candy=0)])
    api.solve(SolveRequest(stardust=300_000))  # raised ValueError before


def test_put_candy_actually_constrains_the_plan(loaded):
    """A constraint that does not bind is worse than one that is absent, so
    check the plan changes rather than just that the write landed."""
    before = api.solve(SolveRequest(stardust=300_000))
    spent_before = sum(s.candy for s in before.selections
                       if s.species_id == BLASTOISE)
    assert spent_before > 0, "sample plan does not power up Blastoise"

    api.put_candy([CandyUpdate(species_id=BLASTOISE, candy=0, xl_candy=0)])
    after = api.solve(SolveRequest(stardust=300_000))
    assert sum(s.candy for s in after.selections
               if s.species_id == BLASTOISE) == 0


def test_put_candy_writes_one_entry_per_family(loaded):
    """Not one per species. A species_id key would be dead weight the model
    never reads, and the count reported back would overstate what was set."""
    families = len(api.STATE["candy"])
    result = api.put_candy([CandyUpdate(species_id=BLASTOISE, candy=42)])

    assert len(api.STATE["candy"]) == families
    assert api.STATE["candy"][BLASTOISE_FAMILY] == 42
    assert BLASTOISE not in api.STATE["candy"]
    assert result["families_with_candy"] == families


def test_put_candy_moves_the_whole_family(loaded):
    """Setting stock for one species sets it for its evolutions too, because
    they draw on one pile. Reporting otherwise would invite double-spending."""
    api.put_candy([CandyUpdate(species_id=BLASTOISE, candy=77)])
    same_pile = [r for r in api.get_candy()
                 if r["family"] == BLASTOISE_FAMILY]
    assert same_pile
    assert all(r["candy"] == 77 for r in same_pile)
