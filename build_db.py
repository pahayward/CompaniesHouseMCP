"""Convert the Companies House bulk CSV into a searchable SQLite database."""
import argparse
import csv
import os
import sqlite3
import time

from tqdm import tqdm

csv.field_size_limit(2**31 - 1)

SCHEMA = """
CREATE TABLE companies (
    number TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prev_names TEXT NOT NULL DEFAULT '',
    category TEXT,
    status TEXT,
    country_of_origin TEXT,
    incorporated TEXT,
    dissolved TEXT,
    accounts_category TEXT,
    sic1 TEXT, sic2 TEXT, sic3 TEXT, sic4 TEXT,
    address1 TEXT, address2 TEXT, post_town TEXT, county TEXT, postcode TEXT
);
"""
COLS = ("number name prev_names category status country_of_origin incorporated dissolved "
        "accounts_category sic1 sic2 sic3 sic4 address1 address2 post_town county postcode").split()
INSERT = f"INSERT OR REPLACE INTO companies ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})"


def iso(d):
    """dd/mm/yyyy -> yyyy-mm-dd (empty stays empty)."""
    return f"{d[6:]}-{d[3:5]}-{d[:2]}" if len(d) == 10 else ""


def rows(path):
    with open(path, "rb") as raw:
        text = (line.decode("utf-8-sig", "replace") for line in raw)
        reader = csv.reader(text)
        header = [h.strip() for h in next(reader)]
        ix = {h: i for i, h in enumerate(header)}
        prev_ix = [ix[f"PreviousName_{n}.CompanyName"] for n in range(1, 11)]
        g = lambda r, k: r[ix[k]].strip()
        for r in reader:
            if len(r) < len(header):
                continue
            prev = " | ".join(p for p in (r[i].strip() for i in prev_ix) if p)
            yield raw.tell(), (
                g(r, "CompanyNumber"), g(r, "CompanyName"), prev,
                g(r, "CompanyCategory"), g(r, "CompanyStatus"), g(r, "CountryOfOrigin"),
                iso(g(r, "IncorporationDate")), iso(g(r, "DissolutionDate")),
                g(r, "Accounts.AccountCategory"),
                g(r, "SICCode.SicText_1"), g(r, "SICCode.SicText_2"),
                g(r, "SICCode.SicText_3"), g(r, "SICCode.SicText_4"),
                g(r, "RegAddress.AddressLine1"), g(r, "RegAddress.AddressLine2"),
                g(r, "RegAddress.PostTown"), g(r, "RegAddress.County"), g(r, "RegAddress.PostCode"),
            )


def step(msg):
    print(f"\n==> {msg}", flush=True)
    return time.time()


def build(csv_path, out, batch_size=50_000):
    if os.path.exists(out):
        os.remove(out)
    db = sqlite3.connect(out)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-1000000;")
    db.executescript(SCHEMA)

    total = os.path.getsize(csv_path)
    t0 = step("Loading rows")
    n, last, batch = 0, 0, []
    with tqdm(total=total, unit="B", unit_scale=True, desc="CSV") as bar:
        for pos, row in rows(csv_path):
            batch.append(row)
            if len(batch) >= batch_size:
                db.executemany(INSERT, batch)
                n += len(batch)
                batch.clear()
                bar.update(pos - last)
                last = pos
                bar.set_postfix(rows=f"{n:,}")
        db.executemany(INSERT, batch)
        n += len(batch)
        bar.update(total - last)
    db.commit()
    print(f"Loaded {n:,} rows in {time.time() - t0:.0f}s")

    t = step("Building name index (prefix lookups)")
    db.execute("CREATE INDEX idx_name ON companies(name)")
    db.execute("CREATE INDEX idx_postcode ON companies(postcode)")
    print(f"done in {time.time() - t:.0f}s")

    t = step("Building FTS5 trigram index (slowest step, no progress available)")
    db.executescript("""
        CREATE VIRTUAL TABLE names_fts USING fts5(
            name, prev_names, content='companies', tokenize='trigram');
    """)
    db.execute("INSERT INTO names_fts(names_fts) VALUES('rebuild')")
    db.commit()
    print(f"done in {time.time() - t:.0f}s")

    step("Optimising")
    db.execute("INSERT INTO names_fts(names_fts) VALUES('optimize')")
    db.execute("ANALYZE")
    db.commit()
    db.close()
    print(f"\nWrote {out} ({os.path.getsize(out) / 1e9:.2f} GB)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", help="BasicCompanyData CSV path")
    ap.add_argument("-o", "--out", default="companies.db")
    ap.add_argument("--batch", type=int, default=50_000)
    a = ap.parse_args()
    build(a.csv, a.out, a.batch)


if __name__ == "__main__":
    main()
