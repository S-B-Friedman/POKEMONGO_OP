#!/usr/bin/env python3
"""
HTTP layer over the solver.

    pip install -r requirements-api.txt
    uvicorn api:app --reload
    open http://127.0.0.1:8000/docs

The browser never solves. The prototype UI shipped a JavaScript reimplementation
of the allocation -- a greedy pass with reassignment -- which is precisely the
baseline benchmark.py exists to beat, and a second copy of the cost tables that
would drift from costs.py within a week. There is one model, it is the MIP, and
it runs here.

State is an in-memory dict, not the database. `pogo_opt/db.py` and its schema
exist and are tested, but nothing here reads them yet — the working set is
`STATE` below, and it is lost on restart. Said plainly because this docstring
previously claimed the opposite, which is a bad thing to be wrong about: the
schema is scoped by trainer_id, so a reader could reasonably have assumed the
API was already multi-tenant and persistent when it is neither.
"""

from __future__ import annotations

import io
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from pogo_opt.data import PokemonInstance, load_candy_inventory, load_from_csv
from pogo_opt.importers.pokegenie import import_rows
from pogo_opt.model import (
    Weights,
    build_and_solve,
    candy_pool_key,
    combat_power,
    rating,
)
from pogo_opt.reference import default_reference

HERE = Path(__file__).parent
DEFAULT_CSV = HERE / "sample_data" / "collection.csv"
DEFAULT_CANDY = HERE / "sample_data" / "candy_inventory.csv"

# In-memory working set. Swap for pogo_opt.db once the import path is settled;
# keeping it explicit here means the API can be exercised before the DB wiring
# exists, rather than blocking on it.
STATE: dict = {"collection": [], "candy": {}, "xl_candy": {}, "source": "none"}


def _load_defaults() -> None:
    if DEFAULT_CSV.exists():
        STATE["collection"] = load_from_csv(DEFAULT_CSV)
        STATE["source"] = "sample_data/collection.csv"
    if DEFAULT_CANDY.exists():
        STATE["candy"], STATE["xl_candy"] = load_candy_inventory(DEFAULT_CANDY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_defaults()
    yield


app = FastAPI(title="POGO_OPT", version="0.1.0", lifespan=lifespan)

# Dev-only: the Vite frontend runs on a different port. Tighten before this is
# reachable from anywhere but localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("POGO_CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class SolveRequest(BaseModel):
    stardust: int = Field(100_000, ge=0)
    bulk: float = Field(0.5, ge=0.0, le=1.0)
    max_steps: int = Field(6, ge=1, le=20)
    max_pokemon: int | None = Field(None, ge=1)
    max_megas: int | None = Field(None, ge=0)
    with_shadow_prices: bool = True


class SelectionOut(BaseModel):
    instance_id: str
    name: str
    species_id: int
    from_level: float
    to_level: float
    stardust: int
    candy: int
    xl_candy: int
    gain: float
    tags: list[str]


class SolveResponse(BaseModel):
    status: str
    selections: list[SelectionOut]
    total_gain: float
    stardust_used: int
    stardust_budget: int
    candy_used: dict[int, int]
    xl_used: dict[int, int]
    binding: list[dict]
    # False when the plan spends candy for a species whose stock nobody
    # supplied. The plan is then optimal against stardust alone and may not
    # be affordable -- a UI should say so rather than present it as a plan.
    fully_costed: bool
    # Keyed by candy pool -- an evolution family name, or a species_id rendered
    # as a string where no family is known. JSON object keys are strings either
    # way. Declared dict[int, ...] before, which made /solve raise a 500 for any
    # collection with an unrecorded candy stock: pydantic rejected 'Squirtle' as
    # an integer. That is the DEFAULT state after a Poke Genie import, since
    # those exports carry no candy at all -- so the field added to warn about
    # missing candy was what made the endpoint fail on missing candy.
    unbacked_candy: dict[str, list[int]]
    candy_warning: str | None = None


def _tags(p: PokemonInstance) -> list[str]:
    out = []
    if p.attack_iv == p.defense_iv == p.stamina_iv == 15:
        out.append("hundo")
    if p.attack_iv == p.defense_iv == p.stamina_iv == 0:
        out.append("nundo")
    for flag, label in ((p.is_lucky, "lucky"), (p.is_shadow, "shadow"),
                        (p.is_purified, "purified")):
        if flag:
            out.append(label)
    return out


def _binding(prices: dict[str, float] | None) -> list[dict]:
    """Turn constraint names into something a person can act on.

    The prototype inferred "binding" from usage crossing 90% of stock. That is a
    proxy; the dual is the actual answer, and it says how much one more unit
    would have been worth rather than merely that the shelf looks empty.
    """
    if not prices:
        return []
    out = []
    for name, pi in prices.items():
        if name == "Stardust_Budget":
            label, unit = "Stardust budget", "per stardust"
        elif name.startswith("XLCandy_Species_"):
            label, unit = f"XL candy — species {name.rsplit('_', 1)[1]}", "per XL candy"
        elif name.startswith("Candy_Species_"):
            label, unit = f"Candy — species {name.rsplit('_', 1)[1]}", "per candy"
        elif name == "Max_Pokemon":
            label, unit = "Plan-size cap", "per extra Pokémon"
        elif name == "Mega_Cap":
            label, unit = "Mega cap", "per extra mega"
        else:
            label, unit = name, ""
        out.append({"constraint": name, "label": label,
                    "shadow_price": round(pi, 6), "unit": unit})
    return out


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    ref = default_reference()
    return {
        "ok": True,
        "collection": len(STATE["collection"]),
        "source": STATE["source"],
        "reference_species": len(ref),
        "reference_source": ref.source,
    }


@app.get("/collection")
def collection(bulk: float = Query(0.5, ge=0.0, le=1.0)) -> list[dict]:
    w = Weights(bulk_exponent=bulk)
    return [
        {
            "instance_id": p.instance_id,
            "name": p.name,
            "species_id": p.species_id,
            "level": p.level,
            "cp": combat_power(p, p.level),
            "ivs": [p.attack_iv, p.defense_iv, p.stamina_iv],
            "types": [t for t in (p.type1, p.type2) if t],
            "fast_move": p.fast_move,
            "charge_move": p.charge_move,
            "rating": round(rating(p, p.level, w), 2),
            "tags": _tags(p),
        }
        for p in STATE["collection"]
    ]


@app.post("/solve", response_model=SolveResponse)
def solve(req: SolveRequest) -> SolveResponse:
    if not STATE["collection"]:
        raise HTTPException(400, "no collection loaded -- POST /import first")

    result = build_and_solve(
        STATE["collection"],
        stardust_budget=req.stardust,
        candy_inventory=STATE["candy"],
        xl_candy_inventory=STATE["xl_candy"],
        max_steps_per_pokemon=req.max_steps,
        max_megas=req.max_megas,
        max_pokemon=req.max_pokemon,
        weights=Weights(bulk_exponent=req.bulk),
        with_shadow_prices=req.with_shadow_prices,
    )

    candy_used: dict[int, int] = {}
    xl_used: dict[int, int] = {}
    for s in result.selections:
        sid = s.pokemon.species_id
        candy_used[sid] = candy_used.get(sid, 0) + s.candy
        xl_used[sid] = xl_used.get(sid, 0) + s.xl_candy

    return SolveResponse(
        status=result.status,
        selections=[
            SelectionOut(
                instance_id=s.pokemon.instance_id,
                name=s.pokemon.name,
                species_id=s.pokemon.species_id,
                from_level=s.pokemon.level,
                to_level=s.target_level,
                stardust=s.stardust,
                candy=s.candy,
                xl_candy=s.xl_candy,
                gain=round(s.gain, 2),
                tags=_tags(s.pokemon),
            )
            for s in result.selections
        ],
        total_gain=round(result.total_gain, 2),
        stardust_used=result.stardust_used,
        stardust_budget=result.stardust_budget,
        candy_used=candy_used,
        xl_used=xl_used,
        binding=_binding(result.shadow_prices),
        fully_costed=result.fully_costed,
        unbacked_candy={str(k): list(v)
                        for k, v in result.unbacked_candy.items()},
        candy_warning=result.candy_warning(),
    )


@app.post("/import/pokegenie")
async def import_pokegenie(
    file: UploadFile = File(...),
    dry_run: bool = Query(False, description="report the mapping without replacing state"),
) -> dict:
    """Upload a Poke Genie CSV export.

    `dry_run=true` returns the column mapping and what would be skipped, so a UI
    can show it for confirmation before overwriting a collection. Poke Genie has
    renamed columns across versions; seeing the mapping is how a rename gets
    caught before it becomes a silently-wrong plan.
    """
    import csv as _csv

    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = _csv.DictReader(io.StringIO(raw))
    headers = reader.fieldnames or []
    result = import_rows(list(reader), headers)

    if result.missing_columns:
        raise HTTPException(
            422,
            {
                "error": "could not map required columns",
                "missing": result.missing_columns,
                "headers_seen": headers,
                "mapped": result.columns,
            },
        )

    if not dry_run:
        STATE["collection"] = result.collection
        STATE["source"] = f"pokegenie:{file.filename}"

    return {
        "dry_run": dry_run,
        "rows_read": result.rows_read,
        "imported": len(result.collection),
        "skipped": [
            {"row": i.row, "name": i.name, "reason": i.reason} for i in result.skipped
        ],
        "warnings": [
            {"row": i.row, "name": i.name, "reason": i.reason} for i in result.warnings
        ],
        "column_mapping": result.columns,
        "unmapped_headers": [
            h for h in headers if h not in set(result.columns.values())
        ],
    }


@app.get("/candy")
def get_candy() -> list[dict]:
    """Per-species stock, plus which species in the collection have none set.

    A species absent from the inventory is unconstrained in the model, which is
    a reasonable default and a silent one. Reporting `known: false` is what lets
    the UI ask for the numbers that would actually change the answer instead of
    demanding the whole inventory up front.

    Stock is looked up by evolution FAMILY, because that is the pile the game
    keeps and the key the inventory uses. Looking it up by species_id -- which
    this did -- misses every entry, so a fully populated inventory reported
    `known: false` for all twenty of its species and the UI would have asked for
    numbers it already had.

    `family` is reported alongside so a caller can see that two rows share one
    pile rather than reading the repeated number as two independent stocks.
    """
    seen = {p.species_id: p.name for p in STATE["collection"]}
    out = []
    for sid, name in sorted(seen.items()):
        pool = candy_pool_key(sid)
        out.append({
            "species_id": sid,
            "name": name,
            "family": pool if isinstance(pool, str) else None,
            "candy": STATE["candy"].get(pool),
            "xl_candy": STATE["xl_candy"].get(pool),
            "known": pool in STATE["candy"],
        })
    return out


class CandyUpdate(BaseModel):
    species_id: int
    candy: int | None = Field(None, ge=0)
    xl_candy: int | None = Field(None, ge=0)


@app.put("/candy")
def put_candy(updates: list[CandyUpdate]) -> dict:
    """Set stock for the family each species belongs to.

    Callers speak species_id, because that is what a person reads off their own
    screen. The inventory is keyed by family, so the translation happens here.

    Writing the raw species_id instead did not fail quietly -- it broke the next
    solve outright. build_and_solve normalizes both keyings and refuses when they
    disagree, so a PUT for a species whose family was already stocked left two
    counts for one pile and raised "candy inventory gives two different counts
    for the Squirtle family (190 and 0)". Every PUT against the loaded sample
    inventory did this, which is to say the endpoint could not be used at all.
    """
    for u in updates:
        pool = candy_pool_key(u.species_id)
        if u.candy is not None:
            STATE["candy"][pool] = u.candy
        if u.xl_candy is not None:
            STATE["xl_candy"][pool] = u.xl_candy
    return {"families_with_candy": len(STATE["candy"]),
            "families_with_xl": len(STATE["xl_candy"])}
