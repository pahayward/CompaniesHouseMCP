"""Read-only query layer for the map UI (map.db for places, companies.db for company rows)."""
import json
import math
import os
import pathlib
import sqlite3
import threading
from dataclasses import dataclass
from functools import lru_cache

import bands
import companies_db
from postcode import norm_pc

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_DB_PATH = os.environ.get("MAP_DB", os.path.join(HERE, "map.db"))

MAX_PLACES = 6000                  # most places returned for one viewport
LEVEL_STARTS = (7.0, 10.0, 13.5)   # zoom at which district, sector and unit levels begin
SNAP = (1.0, 0.25, 0.05, 0.01)     # bbox snap (degrees) per level, so nearby requests share a cache entry
COLOUR_COL = {"size": "size", "type": "ltype", "sic": "sic"}
NSIZE, NLTYPE, NSIC = len(bands.SIZE), len(bands.LTYPE), len(bands.SIC)


class MapNotBuilt(RuntimeError):
    pass


_local = threading.local()


def conn():
    db = getattr(_local, "db", None)
    if db is None:
        if not os.path.exists(MAP_DB_PATH):
            raise MapNotBuilt(f"{MAP_DB_PATH} not found. Build it with: python build_geo.py <ONSPD zip>")
        db = sqlite3.connect(pathlib.Path(MAP_DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)
        db.execute("PRAGMA query_only=ON")
        _local.db = db
    return db


@dataclass(frozen=True)
class Filters:
    """Selected band ids per facet. None means 'no restriction' (every band is on)."""
    size: tuple = None
    ltype: tuple = None
    sic: tuple = None
    active: bool = False
    hide_crowded: bool = False

    def nothing_selected(self):
        return any(v is not None and len(v) == 0 for v in (self.size, self.ltype, self.sic))

    def _chosen(self):
        for col, ids, n in (("size", self.size, NSIZE), ("ltype", self.ltype, NLTYPE), ("sic", self.sic, NSIC)):
            if ids is not None and len(ids) < n:  # every band selected is the same as no filter
                yield col, ids

    def facet_sql(self):
        """AND-fragment (leading ' AND ') and params restricting rows of the facets table."""
        parts, args = [], []
        for col, ids in self._chosen():
            parts.append(f"{col} IN ({','.join('?' * len(ids))})")
            args += ids
        if self.active:
            parts.append("active = 1")
        if self.hide_crowded:
            parts.append("crowded = 0")
        return "".join(" AND " + p for p in parts), args

    def company_sql(self):
        """List of WHERE fragments and params restricting rows of the companies table."""
        parts, args = [], []
        for col, ids in self._chosen():
            fn = {"size": bands.size_sql, "ltype": bands.ltype_sql, "sic": bands.sic_sql}[col]
            clause, a = fn(ids)
            parts.append(clause)
            args += a
        if self.active:
            parts.append("status LIKE 'Active%'")
        return parts, args


def level_for_zoom(zoom):
    return sum(zoom >= s for s in LEVEL_STARTS)


def _snap(bbox, level):
    w, s, e, n = bbox
    g = SNAP[level]
    pad = 0.05
    dx, dy = (e - w) * pad, (n - s) * pad
    return (math.floor((w - dx) / g) * g, math.floor((s - dy) / g) * g,
            math.ceil((e + dx) / g) * g, math.ceil((n + dy) / g) * g)


def points(bbox, zoom, f, colour="size"):
    """GeoJSON of places inside `bbox` (west, south, east, north) at the level suited to `zoom`."""
    level = level_for_zoom(zoom)
    if f.nothing_selected():
        return {"type": "FeatureCollection", "features": [], "level": level, "truncated": False, "total": 0}
    return _points(level, _snap(bbox, level), f, colour if colour in COLOUR_COL else "size")


@lru_cache(maxsize=512)
def _points(level, bbox, f, colour):
    w, s, e, n = bbox
    db = conn()
    places = db.execute(
        f"""SELECT p.id, p.code, p.lat, p.lng FROM place_rtree_{int(level)} r JOIN places p ON p.id = r.id
            WHERE r.max_lat >= ? AND r.min_lat <= ? AND r.max_lng >= ? AND r.min_lng <= ?
            ORDER BY p.total DESC LIMIT ?""",
        (s, n, w, e, MAX_PLACES + 1)).fetchall()
    truncated = len(places) > MAX_PLACES
    places = places[:MAX_PLACES]

    col = COLOUR_COL[colour]
    where, args = f.facet_sql()
    rows = db.execute(
        f"""SELECT place_id, {col}, SUM(n) FROM facets
            WHERE place_id IN (SELECT value FROM json_each(?)){where}
            GROUP BY place_id, {col}""",
        [json.dumps([p[0] for p in places])] + args).fetchall()

    tot, best = {}, {}
    for pid, key, cnt in rows:
        tot[pid] = tot.get(pid, 0) + cnt
        if cnt > best.get(pid, (0, None))[0]:
            best[pid] = (cnt, key)

    features, grand = [], 0
    for pid, code, lat, lng in places:
        cnt = tot.get(pid)
        if not cnt:
            continue
        grand += cnt
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lng, 5), round(lat, 5)]},
            "properties": {"code": code, "level": level, "n": cnt, "dom": best[pid][1]},
        })
    return {"type": "FeatureCollection", "features": features, "level": level,
            "truncated": truncated, "total": grand}


def locate(postcode):
    """(lat, lng) of a unit postcode, or None."""
    pc = norm_pc(postcode)
    if not pc:
        return None
    r = conn().execute("SELECT lat, lng FROM places WHERE level = 3 AND code = ?", (pc,)).fetchone()
    return (r[0], r[1]) if r else None


def companies_at(postcode, f, limit=50, offset=0):
    """Companies registered at one unit postcode, filtered like the map layers.

    Matches the postcode with or without its space; odder spellings that build_geo
    normalised onto the same place are not listed.
    """
    pc = norm_pc(postcode)
    if not pc:
        return None
    where, args = ["postcode IN (?, ?)"], [pc, pc.replace(" ", "")]
    parts, a = f.company_sql()
    where += parts
    args += a
    clause = " AND ".join(where)
    c = companies_db.conn()
    total = c.execute(f"SELECT COUNT(*) FROM companies WHERE {clause}", args).fetchone()[0]
    rows = c.execute(
        f"""SELECT number, name, status, category, accounts_category, incorporated, sic1
            FROM companies WHERE {clause} ORDER BY name LIMIT ? OFFSET ?""",
        args + [limit, offset]).fetchall()
    out = []
    for number, name, status, category, acc, inc, sic1 in rows:
        sid = bands.size_of(acc)
        out.append({"number": number, "name": name, "status": status, "category": category,
                    "incorporated": inc, "size_id": sid, "size": bands.SIZE[sid][0],
                    "industry": sic1, "type_id": bands.ltype_of(category), "sic_id": bands.sic_of(sic1)})
    place = conn().execute("SELECT lat, lng, total FROM places WHERE level = 3 AND code = ?", (pc,)).fetchone()
    return {"postcode": pc, "total": total, "offset": offset, "limit": limit,
            "place_total": place[2] if place else None,
            "lat": place[0] if place else None, "lng": place[1] if place else None,
            "companies": out}


def meta():
    return dict(conn().execute("SELECT key, value FROM meta").fetchall())
