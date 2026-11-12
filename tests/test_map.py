"""Tests for the map pipeline. Run from the repo root:  python -m unittest discover -s tests -v

They build a tiny companies.db and a fake ONSPD zip in a temp folder, so they never touch the real data.
"""
import csv
import io
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bands  # noqa: E402
import build_db  # noqa: E402
import build_geo  # noqa: E402
import companies_db  # noqa: E402
import map_db  # noqa: E402
from postcode import norm_pc  # noqa: E402

# postcode -> (lat, lng) as it would appear in ONSPD
GEO = {
    "S17 3GW": (53.3290, -1.5400), "S17 3GX": (53.3300, -1.5410), "S1 2AB": (53.3800, -1.4700),
    "EC1V 2NX": (51.5270, -0.0877), "CF14 8LH": (51.5200, -3.1700),
    "BT1 1AA": (99.999999, 0.0),  # ONSPD placeholder for "no coordinates"
}
SIC_SAMPLES = ["62012 - Business and domestic software development", "47110 - Retail", "99999 - Dormant Company",
               "None Supplied", "7499 - Non-trading company", "99000 - Activities of extraterritorial"]


def make_companies(path):
    db = sqlite3.connect(path)
    db.executescript(build_db.SCHEMA)
    rows, n = [], 0

    def add(pc, count, acc="MICRO ENTITY", cat="Private Limited Company", sic=SIC_SAMPLES[0], status="Active"):
        nonlocal n
        for _ in range(count):
            n += 1
            rows.append((f"{n:08d}", f"CO {n}", "", cat, status, "United Kingdom", "2020-01-01", "", acc,
                         sic, "", "", "", "1 High St", "", "Town", "", pc))

    add("S17 3GW", 5)
    add("S17 3GW", 2, acc="SMALL", sic=SIC_SAMPLES[1])
    add("S173GW", 1, acc="SMALL", sic=SIC_SAMPLES[1])          # same postcode, no space
    add("S17 3GX", 3, acc="DORMANT", sic=SIC_SAMPLES[2], status="Liquidation")
    add("S1 2AB", 4, acc="FULL", cat="Public Limited Company", sic=SIC_SAMPLES[3])
    add("EC1V 2NX", 210, acc="NO ACCOUNTS FILED", sic=SIC_SAMPLES[4])  # crowded (over 200)
    add("CF14 8LH", 2, acc="MEDIUM", cat="Limited Liability Partnership", sic=SIC_SAMPLES[5])
    add("CF14 8LH", 1, acc="SOMETHING NEW", cat="Brand New Type")      # falls into the catch-all bands
    add("ZZ9 9ZZ", 6)                                                 # valid format, not in ONSPD
    add("BT1 1AA", 2)                                                 # in ONSPD with a placeholder coordinate
    add("", 7)                                                        # no postcode
    add(".....", 3)                                                   # junk postcode
    db.executemany("INSERT INTO companies VALUES (" + ",".join("?" * 18) + ")", rows)
    db.commit()
    db.close()
    return n


def make_onspd(path):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["pcd", "pcd2", "pcds", "dointr", "doterm", "lat", "long"])
    for i, (pc, (lat, lng)) in enumerate(GEO.items()):
        w.writerow([pc.replace(" ", ""), pc, pc, "198001", "202001" if i == 4 else "", lat, lng])
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Data/ONSPD_TEST_UK.csv", buf.getvalue())
        z.writestr("Data/multi_csv/ONSPD_TEST_UK_S.csv", "should,be,ignored\n")
        z.writestr("Documents/readme.csv", "ignored\n")


class NormPostcode(unittest.TestCase):
    def test_samples(self):
        self.assertEqual(norm_pc("s173gw"), "S17 3GW")
        self.assertEqual(norm_pc(", DN22 7SW"), "DN22 7SW")
        self.assertEqual(norm_pc("EC1V  2NX"), "EC1V 2NX")
        for bad in ("", ".", "....", "00-227", "(1062 EA)", "0000", "12345", None):
            self.assertIsNone(norm_pc(bad), bad)


class Bands(unittest.TestCase):
    def test_python_and_sql_agree(self):
        """For every raw value, the band chosen in Python must satisfy the matching SQL clause."""
        cats = [c for b in bands.SIZE for c in b[3]] + ["UNKNOWN", ""]
        ltypes = [c for b in bands.LTYPE for c in b[3]] + ["Private Unlimited Company", "x", ""]
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE companies (accounts_category, category, sic1)")
        db.executemany("INSERT INTO companies VALUES (?,?,?)",
                       [(c, "", "") for c in cats] + [("", c, "") for c in ltypes] + [("", "", s) for s in SIC_SAMPLES])
        for i in range(len(bands.SIZE)):
            clause, args = bands.size_sql((i,))
            got = {r[0] for r in db.execute(f"SELECT accounts_category FROM companies WHERE {clause}", args)}
            want = {c for c in cats if bands.size_of(c) == i}
            self.assertTrue(want <= got, (i, want - got))
            self.assertTrue(all(bands.size_of(c) == i for c in got if c in cats), i)
        for i in range(len(bands.LTYPE)):
            clause, args = bands.ltype_sql((i,))
            got = {r[0] for r in db.execute(f"SELECT category FROM companies WHERE {clause}", args)}
            self.assertTrue({c for c in ltypes if bands.ltype_of(c) == i} <= got, i)
            self.assertTrue(all(bands.ltype_of(c) == i for c in got if c in ltypes), i)
        for i in range(len(bands.SIC)):
            clause, args = bands.sic_sql((i,))
            got = {r[0] for r in db.execute(f"SELECT sic1 FROM companies WHERE {clause}", args)}
            self.assertEqual({s for s in SIC_SAMPLES if bands.sic_of(s) == i}, got & set(SIC_SAMPLES), i)

    def test_sic_sections(self):
        self.assertEqual(bands.SIC[bands.sic_of("62012 - x")][0][0], "J")
        self.assertEqual(bands.SIC[bands.sic_of("99000 - x")][0][0], "U")
        for s in ("99999 - Dormant Company", "None Supplied", "7499 - Non-trading company", ""):
            self.assertEqual(bands.sic_of(s), bands.SIC_NONE, s)

    def test_every_band_has_a_distinct_colour_and_label(self):
        for table in (bands.SIZE, bands.LTYPE, bands.SIC):
            self.assertEqual(len({r[0] for r in table}), len(table))
            self.assertEqual(len({r[1] for r in table}), len(table))


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        cls.total = make_companies(os.path.join(d, "companies.db"))
        make_onspd(os.path.join(d, "onspd.zip"))
        build_geo.build(os.path.join(d, "onspd.zip"), os.path.join(d, "companies.db"), os.path.join(d, "map.db"))
        map_db.MAP_DB_PATH = os.path.join(d, "map.db")
        companies_db.DB_PATH = os.path.join(d, "companies.db")
        companies_db._db = None
        map_db._local.__dict__.clear()
        map_db._points.cache_clear()

    @classmethod
    def tearDownClass(cls):
        if getattr(map_db._local, "db", None) is not None:
            map_db._local.db.close()
        map_db._local.__dict__.clear()
        if companies_db._db is not None:
            companies_db._db.close()
            companies_db._db = None
        cls.tmp.cleanup()

    UK = (-9.0, 49.0, 2.5, 61.0)

    def test_counts_reconcile(self):
        m = map_db.meta()
        placed = 5 + 2 + 1 + 3 + 4 + 210 + 3
        self.assertEqual(int(m["companies_total"]), self.total)
        self.assertEqual(int(m["companies_mapped"]), placed)
        self.assertEqual(int(m["companies_no_postcode"]), 7 + 3)
        self.assertEqual(int(m["companies_no_coordinates"]), 6 + 2)
        self.assertEqual(placed + 10 + 8, self.total)
        db = map_db.conn()
        for level in range(4):
            tot = db.execute("""SELECT SUM(f.n) FROM facets f JOIN places p ON p.id = f.place_id
                                WHERE p.level = ?""", (level,)).fetchone()[0]
            self.assertEqual(tot, placed, f"level {level}")

    def test_hierarchy(self):
        codes = {lvl: {r[0] for r in map_db.conn().execute("SELECT code FROM places WHERE level=?", (lvl,))}
                 for lvl in range(4)}
        self.assertEqual(codes[0], {"S", "EC", "CF"})
        self.assertEqual(codes[1], {"S17", "S1", "EC1V", "CF14"})
        self.assertEqual(codes[2], {"S17 3", "S1 2", "EC1V 2", "CF14 8"})
        self.assertEqual(len(codes[3]), 5)

    def test_levels_by_zoom(self):
        self.assertEqual([map_db.level_for_zoom(z) for z in (3, 6.9, 7, 9.9, 10, 13.4, 13.5, 18)],
                         [0, 0, 1, 1, 2, 2, 3, 3])

    def test_points_totals_and_labels(self):
        r = map_db.points(self.UK, 5, map_db.Filters())
        by = {f["properties"]["code"]: f["properties"]["n"] for f in r["features"]}
        self.assertEqual(by, {"S": 5 + 2 + 1 + 3 + 4, "EC": 210, "CF": 3})
        self.assertEqual(r["total"], sum(by.values()))
        self.assertFalse(r["truncated"])
        lng, lat = next(f for f in r["features"] if f["properties"]["code"] == "S")["geometry"]["coordinates"]
        self.assertTrue(53.3 < lat < 53.4 and -1.6 < lng < -1.4)  # Sheffield

    def test_size_filter(self):
        micro = map_db.points(self.UK, 5, map_db.Filters(size=(0,)))
        self.assertEqual(micro["total"], 5)
        small = map_db.points(self.UK, 5, map_db.Filters(size=(1,)))
        self.assertEqual(small["total"], 3)
        none = map_db.points(self.UK, 5, map_db.Filters(size=()))
        self.assertEqual(none["features"], [])

    def test_catch_all_bands(self):
        r = map_db.points(self.UK, 5, map_db.Filters(size=(bands.SIZE_OTHER,)))
        self.assertEqual(r["total"], 210 + 1)  # NO ACCOUNTS FILED + unknown category
        r = map_db.points(self.UK, 5, map_db.Filters(ltype=(bands.LTYPE_OTHER,)))
        self.assertEqual(r["total"], 1)

    def test_other_filters(self):
        self.assertEqual(map_db.points(self.UK, 5, map_db.Filters(active=True))["total"], 5 + 2 + 1 + 4 + 210 + 3)
        self.assertEqual(map_db.points(self.UK, 5, map_db.Filters(hide_crowded=True))["total"], 5 + 2 + 1 + 3 + 4 + 3)
        j = bands.sic_of(SIC_SAMPLES[0])
        self.assertEqual(map_db.points(self.UK, 5, map_db.Filters(sic=(j,)))["total"], 5 + 1)  # S17 3GW + CF14 8LH

    def test_colour_by_dominant(self):
        r = map_db.points(self.UK, 5, map_db.Filters(), colour="size")
        s = next(f for f in r["features"] if f["properties"]["code"] == "S")
        self.assertEqual(s["properties"]["dom"], 0)  # 5 micro beats 3 small/4 full/3 dormant

    def test_bbox_limits_results(self):
        r = map_db.points((-2.0, 53.0, -1.0, 54.0), 9, map_db.Filters())
        self.assertEqual({f["properties"]["code"] for f in r["features"]}, {"S17", "S1"})

    def test_unit_level_and_cap(self):
        r = map_db.points((-2.0, 53.0, -1.0, 54.0), 15, map_db.Filters())
        self.assertEqual({f["properties"]["code"] for f in r["features"]}, {"S17 3GW", "S17 3GX", "S1 2AB"})
        old = map_db.MAX_PLACES
        map_db.MAX_PLACES = 1
        try:
            map_db._points.cache_clear()
            r = map_db.points((-2.0, 53.0, -1.0, 54.0), 15, map_db.Filters())
            self.assertTrue(r["truncated"])
            self.assertEqual(len(r["features"]), 1)
            self.assertEqual(r["features"][0]["properties"]["code"], "S17 3GW")  # busiest kept
        finally:
            map_db.MAX_PLACES = old
            map_db._points.cache_clear()

    def test_companies_at(self):
        r = map_db.companies_at("s17 3gw", map_db.Filters())
        self.assertEqual(r["total"], 5 + 2 + 1)  # the spaceless variant is listed too
        self.assertEqual(r["place_total"], 8)
        self.assertEqual(len(map_db.companies_at("S17 3GW", map_db.Filters(), limit=3)["companies"]), 3)
        micro = map_db.companies_at("S17 3GW", map_db.Filters(size=(0,)))
        self.assertEqual(micro["total"], 5)
        self.assertTrue(all(c["size"] == "Micro" for c in micro["companies"]))
        self.assertEqual(map_db.companies_at("S17 3GX", map_db.Filters(active=True))["total"], 0)
        self.assertIsNone(map_db.companies_at("nonsense", map_db.Filters()))

    def test_locate(self):
        lat, lng = map_db.locate("EC1V2NX")
        self.assertAlmostEqual(lat, 51.527, places=2)
        self.assertIsNone(map_db.locate("ZZ9 9ZZ"))


if __name__ == "__main__":
    unittest.main()
