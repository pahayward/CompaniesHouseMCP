"""Read-only query layer shared by the HTTP API and the MCP server."""
import os
import re
import sqlite3

DB_PATH = os.environ.get("COMPANIES_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "companies.db"))
CANDIDATES = 3000  # max FTS hits to rank in Python

_db = None


def conn():
    global _db
    if _db is None:
        uri = "file:" + DB_PATH.replace("\\", "/") + "?mode=ro"
        _db = sqlite3.connect(uri, uri=True, check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA query_only=ON")
    return _db


def _split_sic(text):
    code, _, desc = (text or "").partition(" - ")
    return {"code": code.strip(), "description": desc.strip()}


def _shape(r):
    d = dict(r)
    d["sic"] = [_split_sic(d[f"sic{i}"]) for i in range(1, 5) if d[f"sic{i}"]]
    for i in range(1, 5):
        del d[f"sic{i}"]
    d["previous_names"] = [p for p in d.pop("prev_names", "").split(" | ") if p]
    return d


def _rank(name, q):
    n = name.upper()
    if n == q:
        return 0
    if n.startswith(q):
        return 1
    if re.search(rf"\b{re.escape(q)}\b", n):
        return 2
    return 3


def search(query, limit=10, active_only=False):
    """Find companies whose current or previous name contains `query`."""
    q = query.strip().upper()
    if not q:
        return []
    c = conn()
    where = "AND c.status = 'Active'" if active_only else ""
    if len(q) < 3:  # trigram index needs >=3 chars; use name prefix
        sql = f"SELECT c.* FROM companies c WHERE c.name >= ? AND c.name < ? {where} LIMIT ?"
        rows = c.execute(sql, (q, q + "￿", CANDIDATES)).fetchall()
    else:
        fts = '"' + q.replace('"', '""') + '"'
        sql = f"""SELECT c.* FROM names_fts f JOIN companies c ON c.rowid = f.rowid
                  WHERE names_fts MATCH ? {where} LIMIT ?"""
        rows = c.execute(sql, (fts, CANDIDATES)).fetchall()
    rows.sort(key=lambda r: (_rank(r["name"], q), r["status"] != "Active", len(r["name"])))
    return [_shape(r) for r in rows[:limit]]


def by_postcode(postcode, sic=None, limit=50, active_only=True):
    """List companies in a postcode area/district/full postcode, optionally filtered by SIC.
    `postcode` can be e.g. 'S17', 'S17 3', or 'S17 3GW'; `sic` is a code prefix or description text."""
    p = " ".join(postcode.upper().split())
    if not p:
        return {"total": 0, "companies": []}
    if " " not in p and re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?", p):
        p += " "  # outward code only: stop 'S1' matching 'S17 ...'
    where, args = ["postcode >= ?", "postcode < ?"], [p, p + "￿"]
    if active_only:
        where.append("status LIKE 'Active%'")
    if sic:
        where.append("(" + " OR ".join(f"sic{i} LIKE ?" for i in range(1, 5)) + ")")
        args += [f"%{sic.strip()}%"] * 4
    clause = " AND ".join(where)
    c = conn()
    total = c.execute(f"SELECT COUNT(*) FROM companies WHERE {clause}", args).fetchone()[0]
    rows = c.execute(f"SELECT * FROM companies WHERE {clause} ORDER BY name LIMIT ?", args + [limit])
    return {"total": total, "companies": [_shape(r) for r in rows]}


def get(number):
    number = number.strip().upper()
    if number.isdigit():
        number = number.zfill(8)
    r = conn().execute("SELECT * FROM companies WHERE number = ?", (number,)).fetchone()
    return _shape(r) if r else None
