"""Build map.db: postcode centroids plus per-place company counts by size / legal type / industry.

Inputs
  * companies.db      built by build_db.py / init.py (opened read-only)
  * ONSPD             the ONS Postcode Directory (zip, folder or the single CSV). It is the only
                      source of coordinates; download it from the ONS Open Geography Portal.

Output
  * map.db            places (area > district > sector > postcode unit), per-place facet counts,
                      and an R-tree over place coordinates for viewport queries.

Usage: python build_geo.py path\\to\\ONSPD_MAY_2026_UK.zip
"""
import argparse
import csv
import datetime
import io
import os
import pathlib
import sqlite3
import sys
import time
import zipfile

from tqdm import tqdm

import bands
from postcode import norm_pc

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = os.environ.get("COMPANIES_DB", os.path.join(HERE, "companies.db"))
DEFAULT_OUT = os.environ.get("MAP_DB", os.path.join(HERE, "map.db"))
CROWDED = 200  # a postcode holding more companies than this is a "mass registration" address

# Coordinates outside this box are placeholders (ONSPD uses 99.999999) or not on the mainland/islands.
LAT_RANGE, LNG_RANGE = (49.0, 61.5), (-9.0, 2.5)

# Place levels: 0 area (S), 1 district (S17), 2 sector (S17 3), 3 unit (S17 3GW).
# SQL that derives each parent code from a unit code held in column {c}.
PARENT_EXPR = {
    2: "substr({c}, 1, length({c}) - 2)",
    1: "substr({c}, 1, instr({c}, ' ') - 1)",
    0: "CASE WHEN substr({c}, 2, 1) BETWEEN 'A' AND 'Z' THEN substr({c}, 1, 2) ELSE substr({c}, 1, 1) END",
}

SCHEMA = """
CREATE TABLE places (
    id    INTEGER PRIMARY KEY,
    level INTEGER NOT NULL,
    code  TEXT    NOT NULL,
    lat   REAL    NOT NULL,
    lng   REAL    NOT NULL,
    total INTEGER NOT NULL
);
CREATE TABLE facets (
    place_id INTEGER NOT NULL,
    size     INTEGER NOT NULL,
    ltype    INTEGER NOT NULL,
    sic      INTEGER NOT NULL,
    active   INTEGER NOT NULL,
    crowded  INTEGER NOT NULL,
    n        INTEGER NOT NULL,
    PRIMARY KEY (place_id, size, ltype, sic, active, crowded)
) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def step(msg):
    print(f"\n==> {msg}", flush=True)
    return time.time()


# ------------------------------------------------------------------ companies -> stage
def stage_companies(src, db):
    """Group companies by (postcode, size, type, industry, active) into st.stage."""
    s = sqlite3.connect(pathlib.Path(src).resolve().as_uri() + "?mode=ro", uri=True)
    total = s.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    cur = s.execute(f"""
        SELECT postcode, accounts_category, category, {bands.SIC_DIV_SQL}, {bands.ACTIVE_SQL}, COUNT(*)
        FROM companies GROUP BY 1, 2, 3, 4, 5""")
    no_postcode, batch = 0, []
    last_raw = last_pc = None
    with tqdm(total=total, unit="co", unit_scale=True, desc="Companies") as bar:
        for raw, acc, cat, div, active, n in cur:
            if raw != last_raw:
                last_raw, last_pc = raw, norm_pc(raw)
            if last_pc is None:
                no_postcode += n
            else:
                batch.append((last_pc, bands.size_of(acc), bands.ltype_of(cat),
                              bands.sic_of_division(div), active, n))
                if len(batch) >= 100_000:
                    db.executemany("INSERT INTO st.stage VALUES (?,?,?,?,?,?)", batch)
                    batch.clear()
            bar.update(n)
        db.executemany("INSERT INTO st.stage VALUES (?,?,?,?,?,?)", batch)
    s.close()
    db.commit()
    return total, no_postcode


# ----------------------------------------------------------------------- ONSPD reader
def _onspd_files(path):
    """Yield (name, size, opener) for the CSV(s) holding the postcode rows."""
    def pick(names):
        low = [n for n in names if n[0].lower().endswith(".csv") and "document" not in n[0].lower()]
        main = [n for n in low if "multi_csv" not in n[0].lower()]
        return main or low

    if os.path.isdir(path):
        found = [(os.path.join(d, f), os.path.getsize(os.path.join(d, f)))
                 for d, _, fs in os.walk(path) for f in fs]
        for name, size in pick(found):
            yield name, size, (lambda p=name: open(p, "rb"))
    elif zipfile.is_zipfile(path):
        z = zipfile.ZipFile(path)
        for m in pick([(i.filename, i.file_size) for i in z.infolist()]):
            yield m[0], m[1], (lambda n=m[0]: z.open(n))
    else:
        yield path, os.path.getsize(path), (lambda: open(path, "rb"))


def _counted(lines, bar):
    for ln in lines:
        bar.update(len(ln))
        yield ln


def load_geo(onspd, db):
    """Stream ONSPD, keeping coordinates only for postcodes that companies actually use."""
    known = {r[0] for r in db.execute("SELECT DISTINCT pc FROM st.stage")}
    found = terminated = 0
    batch, files = [], list(_onspd_files(onspd))
    if not files:
        sys.exit(f"No postcode CSV found in {onspd}")
    for name, size, opener in files:
        with opener() as raw, tqdm(total=size, unit="B", unit_scale=True, desc=os.path.basename(name)) as bar:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
            reader = csv.reader(_counted(text, bar))
            header = [h.strip().lower() for h in next(reader)]
            ix = {h: i for i, h in enumerate(header)}
            pc_col = next((ix[c] for c in ("pcds", "pcd2", "pcd") if c in ix), None)
            lat_col = ix.get("lat", ix.get("latitude"))
            lng_col = ix.get("long", ix.get("lng", ix.get("longitude")))
            if pc_col is None or lat_col is None or lng_col is None:
                sys.exit(f"{name}: expected postcode (pcds/pcd2/pcd), lat and long columns, got {header[:12]}...")
            term_col = ix.get("doterm")
            need = max(pc_col, lat_col, lng_col)
            for r in reader:
                if len(r) <= need:
                    continue
                pc = norm_pc(r[pc_col])
                if pc is None or pc not in known:
                    continue
                try:
                    lat, lng = float(r[lat_col]), float(r[lng_col])
                except ValueError:
                    continue
                if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LNG_RANGE[0] <= lng <= LNG_RANGE[1]):
                    continue
                if term_col is not None and len(r) > term_col and r[term_col].strip():
                    terminated += 1
                batch.append((pc, lat, lng))
                if len(batch) >= 100_000:
                    db.executemany("INSERT OR IGNORE INTO st.geo VALUES (?,?,?)", batch)
                    found += len(batch)
                    batch.clear()
    db.executemany("INSERT OR IGNORE INTO st.geo VALUES (?,?,?)", batch)
    db.commit()
    return len(known), db.execute("SELECT COUNT(*) FROM st.geo").fetchone()[0], terminated


# ---------------------------------------------------------------------------- build
def build(onspd, companies=DEFAULT_SRC, out=DEFAULT_OUT, crowded=CROWDED):
    new, stage = out + ".new", out + ".stage"
    for p in (new, stage):
        if os.path.exists(p):
            os.remove(p)
    db = sqlite3.connect(new)
    try:
        db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-500000;")
        db.execute("ATTACH DATABASE ? AS st", (stage,))
        db.executescript("""
            CREATE TABLE st.stage (pc TEXT, size INT, ltype INT, sic INT, active INT, n INT);
            CREATE TABLE st.geo (pc TEXT PRIMARY KEY, lat REAL, lng REAL) WITHOUT ROWID;
        """)
        db.executescript(SCHEMA)

        t = step(f"Reading companies from {companies}")
        total, no_postcode = stage_companies(companies, db)
        db.execute("CREATE INDEX st.stage_pc ON stage(pc)")
        print(f"{total:,} companies; {no_postcode:,} without a usable postcode ({time.time() - t:.0f}s)")

        t = step(f"Reading postcode coordinates from {onspd}")
        wanted, matched, terminated = load_geo(onspd, db)
        print(f"{matched:,} of {wanted:,} company postcodes found ({terminated:,} of them terminated) "
              f"in {time.time() - t:.0f}s")

        t = step("Building places")
        db.execute("""
            INSERT INTO places(level, code, lat, lng, total)
            SELECT 3, s.pc, g.lat, g.lng, s.total
            FROM (SELECT pc, SUM(n) AS total FROM st.stage GROUP BY pc) s
            JOIN st.geo g ON g.pc = s.pc
            ORDER BY s.pc""")
        for level in (2, 1, 0):
            expr = PARENT_EXPR[level].format(c="code")
            db.execute(f"""
                INSERT INTO places(level, code, lat, lng, total)
                SELECT {level}, {expr}, SUM(lat * total) / SUM(total), SUM(lng * total) / SUM(total), SUM(total)
                FROM places WHERE level = 3 GROUP BY {expr}""")
        db.execute("CREATE UNIQUE INDEX idx_places_code ON places(level, code)")
        db.commit()
        mapped = db.execute("SELECT SUM(total) FROM places WHERE level = 3").fetchone()[0] or 0
        counts = dict(db.execute("SELECT level, COUNT(*) FROM places GROUP BY level"))
        print(f"places by level {counts}; {mapped:,} companies placed ({time.time() - t:.0f}s)")

        t = step("Building facet counts (unit level)")
        db.execute("""
            INSERT INTO facets
            SELECT u.id, s.size, s.ltype, s.sic, s.active, CAST(u.total > ? AS INTEGER), SUM(s.n)
            FROM st.stage s JOIN places u ON u.level = 3 AND u.code = s.pc
            GROUP BY u.id, s.size, s.ltype, s.sic, s.active""", (crowded,))
        db.commit()
        print(f"done in {time.time() - t:.0f}s")

        for level in (2, 1, 0):
            t = step(f"Rolling facets up to level {level}")
            expr = PARENT_EXPR[level].format(c="u.code")
            db.execute(f"""
                INSERT INTO facets
                SELECT par.id, f.size, f.ltype, f.sic, f.active, f.crowded, SUM(f.n)
                FROM facets f
                JOIN places u   ON u.id = f.place_id AND u.level = 3
                JOIN places par ON par.level = {level} AND par.code = {expr}
                GROUP BY par.id, f.size, f.ltype, f.sic, f.active, f.crowded""")
            db.commit()
            print(f"done in {time.time() - t:.0f}s")

        step("Spatial index and metadata")
        # One R-tree per level: a single shared tree makes a low-zoom query wade through every unit postcode.
        for level in range(4):
            db.execute(f"CREATE VIRTUAL TABLE place_rtree_{level} USING rtree(id, min_lat, max_lat, min_lng, max_lng)")
            db.execute(f"INSERT INTO place_rtree_{level} SELECT id, lat, lat, lng, lng FROM places WHERE level = {level}")
        meta = {
            "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "companies_total": total,
            "companies_mapped": mapped,
            "companies_no_postcode": no_postcode,
            "companies_no_coordinates": total - no_postcode - mapped,
            "places_by_level": counts,
            "crowded_threshold": crowded,
            "source_companies": os.path.basename(companies),
            "source_onspd": os.path.basename(onspd),
        }
        db.executemany("INSERT INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in meta.items()])
        db.execute("ANALYZE")
        db.commit()
        db.execute("DETACH DATABASE st")
    finally:
        db.close()
        if os.path.exists(stage):
            os.remove(stage)

    try:
        os.replace(new, out)
    except PermissionError:
        sys.exit(f"Cannot replace {out}: it is in use. Stop the map server and move {new} over it.")
    print(f"\nWrote {out} ({os.path.getsize(out) / 1e9:.2f} GB); "
          f"{mapped:,} of {total:,} companies placed on the map.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("onspd", help="ONS Postcode Directory: the zip, an extracted folder, or the CSV")
    ap.add_argument("--companies", default=DEFAULT_SRC, help="companies.db path")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT, help="map.db path")
    ap.add_argument("--crowded", type=int, default=CROWDED,
                    help="postcodes with more companies than this count as mass-registration addresses")
    a = ap.parse_args()
    build(a.onspd, a.companies, a.out, a.crowded)


if __name__ == "__main__":
    main()
