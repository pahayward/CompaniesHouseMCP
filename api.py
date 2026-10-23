"""HTTP API: uvicorn api:app --port 8000  (docs at /docs)."""
from fastapi import FastAPI, HTTPException, Query

import companies_db as db

app = FastAPI(title="Companies House lookup")


@app.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=100),
           active_only: bool = False):
    return db.search(q, limit, active_only)


@app.get("/postcode/{postcode}")
def postcode(postcode: str, sic: str = "", limit: int = Query(50, ge=1, le=500),
             active_only: bool = True):
    return db.by_postcode(postcode, sic or None, limit, active_only)


@app.get("/company/{number}")
def company(number: str):
    r = db.get(number)
    if not r:
        raise HTTPException(404, "Company not found")
    return r
