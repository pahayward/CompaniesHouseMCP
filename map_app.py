"""Map UI server: uvicorn map_app:app --port 8001   then open http://localhost:8001

Serves the static front end in web/ and a small JSON API over map.db / companies.db.
Independent of the MCP server and of api.py.
"""
import os
import re

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import bands
import companies_db
import map_db

HERE = os.path.dirname(os.path.abspath(__file__))
CH_URL = "https://find-and-update.company-information.service.gov.uk/company/"
NUMBER_RE = re.compile(r"^[A-Z]{0,2}\d{5,8}$")

app = FastAPI(title="Companies map")


@app.exception_handler(map_db.MapNotBuilt)
async def _not_built(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=503)


def _ids(raw, n, name):
    """'0,2' -> (0, 2). Empty/absent -> None (no restriction). 'none' -> () (nothing selected)."""
    if raw is None or raw.strip() == "":
        return None
    if raw.strip().lower() == "none":
        return ()
    try:
        ids = sorted({int(x) for x in raw.split(",")})
    except ValueError:
        raise HTTPException(422, f"{name}: expected comma-separated integers")
    if any(i < 0 or i >= n for i in ids):
        raise HTTPException(422, f"{name}: ids must be between 0 and {n - 1}")
    return tuple(ids)


def _filters(size, ltype, sic, active, hide_crowded):
    return map_db.Filters(_ids(size, map_db.NSIZE, "size"), _ids(ltype, map_db.NLTYPE, "type"),
                          _ids(sic, map_db.NSIC, "sic"), active, hide_crowded)


@app.get("/api/map/legend")
def legend():
    """Band names and colours, plus build metadata for the footer."""
    return {"legend": bands.legend(), "meta": map_db.meta(), "levels": list(map_db.LEVEL_STARTS)}


@app.get("/api/map/points")
def points(bbox: str = Query(..., description="west,south,east,north"),
           zoom: float = Query(..., ge=0, le=24),
           size: str | None = None, type: str | None = Query(None, alias="type"), sic: str | None = None,
           active: bool = False, hide_crowded: bool = False, colour: str = "size"):
    try:
        w, s, e, n = (float(x) for x in bbox.split(","))
    except ValueError:
        raise HTTPException(422, "bbox must be west,south,east,north")
    if not (w < e and s < n):
        raise HTTPException(422, "bbox must have west < east and south < north")
    box = (max(w, -180.0), max(s, -90.0), min(e, 180.0), min(n, 90.0))
    return map_db.points(box, zoom, _filters(size, type, sic, active, hide_crowded), colour)


@app.get("/api/map/place/{code}/companies")
def place_companies(code: str, size: str | None = None, type: str | None = Query(None, alias="type"),
                    sic: str | None = None, active: bool = False,
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    r = map_db.companies_at(code, _filters(size, type, sic, active, False), limit, offset)
    if r is None:
        raise HTTPException(404, "Not a valid UK postcode")
    return r


@app.get("/api/company/{number}")
def company(number: str):
    r = companies_db.get(number)
    if not r:
        raise HTTPException(404, "Company not found")
    r["size"] = bands.SIZE[bands.size_of(r["accounts_category"])][0]
    r["ch_url"] = CH_URL + r["number"]
    try:
        r["location"] = map_db.locate(r["postcode"])
    except map_db.MapNotBuilt:
        r["location"] = None
    return r


@app.get("/api/search")
def search(q: str = Query(..., min_length=2), limit: int = Query(8, ge=1, le=25)):
    """Find a company by name or number; results carry coordinates so the map can fly to them."""
    found = []
    q = q.strip()
    if NUMBER_RE.match(q.upper().replace(" ", "")):
        exact = companies_db.get(q)
        if exact:
            found.append(exact)
    seen = {c["number"] for c in found}
    found += [c for c in companies_db.search(q, limit) if c["number"] not in seen]
    out = []
    for c in found[:limit]:
        try:
            loc = map_db.locate(c["postcode"])
        except map_db.MapNotBuilt:
            loc = None
        out.append({"number": c["number"], "name": c["name"], "status": c["status"],
                    "postcode": c["postcode"], "post_town": c["post_town"],
                    "lat": loc[0] if loc else None, "lng": loc[1] if loc else None})
    return out


# Static front end last, so it never shadows the API routes.
app.mount("/", StaticFiles(directory=os.path.join(HERE, "web"), html=True), name="web")
