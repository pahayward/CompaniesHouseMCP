# Companies House lookup

Turns the Companies House "Basic Company Data" bulk file (about 5.7M UK companies) into a
local SQLite database, and exposes it to AI agents through an MCP server (and an optional HTTP API).

Ask an agent things like *"what is the SIC classification of companies with 'Howarth Design' in
the name?"* or *"list IT consultancies in S17"*.

## Setup

Requires Python 3.10+ and roughly 5 GB of free disk during a build (about 2.3 GB afterwards).

```
pip install -r requirements.txt
```

## Initialise from the Companies House zip

1. Download `BasicCompanyDataAsOneFile-YYYY-MM-DD.zip` (about 500 MB) from
   <https://download.companieshouse.gov.uk/en_output.html>. No need to unzip it.
2. Run:

   ```
   python init.py path\to\BasicCompanyDataAsOneFile-YYYY-MM-DD.zip --register
   ```

   This unzips the CSV to a temporary folder, builds `companies.db` with progress bars
   (roughly a minute or two on an SSD), deletes the CSV, and registers the MCP server with
   Claude Code. Drop `--register` to skip the last step.

To refresh the data, repeat with a newer zip (Companies House publishes monthly). The old
database is only replaced once the new one has built successfully. If the API or MCP server
has the file open, Windows blocks the swap; stop it and rerun.

Already have an unzipped CSV? `python build_db.py file.csv -o companies.db` does just the build.

## Use it

### MCP server (for agents)

`mcp_server.py` is a stdio server. `init.py --register` runs this for you:

```
claude mcp add companies --scope user -- python C:\full\path\to\mcp_server.py
```

For **Claude Desktop**, add this under `mcpServers` in `%APPDATA%\Claude\claude_desktop_config.json`
and fully restart the app:

```json
"companies": {
  "command": "python",
  "args": ["C:\\full\\path\\to\\mcp_server.py"]
}
```

| Tool | Purpose |
|---|---|
| `search_companies(name, limit, active_only)` | Find companies whose current or previous name contains `name`. Returns number, status, category, SIC codes. |
| `search_by_postcode(postcode, sic, limit, active_only)` | List companies in `S17`, `S17 3`, `S17 3GW` etc., optionally filtered by SIC code prefix or description text. Returns total count plus companies. |
| `get_company(number)` | Full record for a company number. |

### HTTP API (optional)

```
uvicorn api:app --port 8000
```

- `GET /search?q=tesco&limit=10&active_only=true`
- `GET /postcode/S17?sic=62&limit=50`
- `GET /company/01938272`

Interactive docs at <http://localhost:8000/docs>.

## How it works

| File | Role |
|---|---|
| `init.py` | One-step: zip, then temp CSV, then new database, then atomic swap, then optional MCP registration |
| `build_db.py` | CSV to SQLite with progress; builds the indexes |
| `companies_db.py` | Read-only query layer shared by the API and MCP server |
| `mcp_server.py` / `api.py` | The two front ends |

- **Name search** uses an SQLite FTS5 trigram index, so it matches anywhere in a name (not just
  word starts) in milliseconds. Terms under 3 characters fall back to a prefix match.
- **Ranking:** exact name, then name starts with, then whole word, then substring. Active companies
  come first within each group. Only the first 3,000 index hits are ranked, so very common terms
  like "LTD" return an arbitrary slice.
- **Postcode search** uses a range scan on an indexed column.
- The database is opened read-only.

## What is (and isn't) stored

Stored: company number, name, up to 10 previous names, category, status, country of origin,
incorporation/dissolution dates, accounts category, up to 4 SIC codes, and the registered address.

Dropped from the CSV: due dates, mortgage counts, limited-partnership counts, URI and care-of/PO box
lines. Edit `build_db.py` if you want them.

The bulk file contains no directors, shareholders, financial figures or filing history. For those
use the [Companies House API](https://developer.company-information.service.gov.uk/).

## Configuration

`COMPANIES_DB` environment variable overrides the database path (default: `companies.db` next to the code).

## Data licence

Companies House data is published under the Open Government Licence v3.0. The database is not
committed to this repo; build it yourself with `init.py`.
