"""Initialise/refresh the MCP server from a Companies House bulk zip.

Usage: python init.py BasicCompanyDataAsOneFile-YYYY-MM-DD.zip [--register]
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

from tqdm import tqdm

import build_db

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("COMPANIES_DB", os.path.join(HERE, "companies.db"))


def extract_csv(zip_path, dest_dir):
    """Stream the single CSV out of the zip (member name is ignored, never used as a path)."""
    with zipfile.ZipFile(zip_path) as z:
        members = [m for m in z.infolist() if m.filename.lower().endswith(".csv")]
        if len(members) != 1:
            sys.exit(f"Expected exactly one .csv in the zip, found {len(members)}")
        m = members[0]
        out = os.path.join(dest_dir, "companies.csv")
        with z.open(m) as src, open(out, "wb") as dst, tqdm(
                total=m.file_size, unit="B", unit_scale=True, desc="Unzip") as bar:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
                bar.update(len(chunk))
    return out


def swap_in(new_db):
    try:
        os.replace(new_db, DB)
    except PermissionError:
        sys.exit(f"Cannot replace {DB}: it is in use. Stop the API/MCP server "
                 f"and rerun, or move {new_db} over it manually.")


def register():
    cmd = ["claude", "mcp", "add", "companies", "--scope", "user", "--",
           sys.executable, os.path.join(HERE, "mcp_server.py")]
    print("\n==> Registering MCP server with Claude Code")
    try:
        subprocess.run(cmd, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"Could not register automatically ({e}). Run:\n  {' '.join(cmd)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("zip", help="Companies House BasicCompanyData zip")
    ap.add_argument("--register", action="store_true", help="also add the MCP server to Claude Code")
    a = ap.parse_args()

    if not zipfile.is_zipfile(a.zip):
        sys.exit(f"Not a valid zip file: {a.zip}")

    # Work next to the target so the final swap is an atomic same-volume rename.
    with tempfile.TemporaryDirectory(dir=HERE, prefix="init_") as tmp:
        csv_path = extract_csv(a.zip, tmp)
        new_db = os.path.join(tmp, "companies.db")
        build_db.build(csv_path, new_db)
        os.remove(csv_path)
        swap_in(new_db)

    print(f"\nReady: {DB}")
    if a.register:
        register()
    else:
        print("Run again with --register to add it to Claude Code.")


if __name__ == "__main__":
    main()
