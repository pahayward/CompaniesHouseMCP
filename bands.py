"""Size / legal-type / industry bands shared by the map builder and the map API.

The bulk file has no headcount or turnover, so "size" is the *kind of accounts* a company files.
Edit the tables below to change how raw Companies House values are grouped. Both the
build step (Python side, `size_of` etc.) and the live queries (SQL side, `size_sql` etc.) read
the same tables, so they cannot drift apart.

Rebuild map.db after changing a mapping (the facets are stored as small integers).
"""
import colorsys

# --------------------------------------------------------------------------- size
# id -> (label, colour, hint, raw accounts_category values). The last band is also the
# catch-all for any category not listed anywhere.
SIZE = [
    ("Micro", "#3b82f6", "Micro-entity accounts: the smallest filing tier",
     ["MICRO ENTITY"]),
    ("Small", "#10b981", "Small-company accounts (full, abridged or audit-exempt)",
     ["SMALL", "TOTAL EXEMPTION SMALL", "TOTAL EXEMPTION FULL",
      "UNAUDITED ABRIDGED", "AUDITED ABRIDGED", "PARTIAL EXEMPTION"]),
    ("Medium", "#f59e0b", "Medium-sized company accounts",
     ["MEDIUM"]),
    ("Large / group", "#ef4444", "Full or group accounts, and subsidiaries using audit/filing exemptions",
     ["FULL", "GROUP", "AUDIT EXEMPTION SUBSIDIARY", "FILING EXEMPTION SUBSIDIARY"]),
    ("Dormant", "#94a3b8", "Dormant-company accounts",
     ["DORMANT"]),
    ("No accounts yet", "#a855f7", "No accounts filed yet (often new companies), or type not available",
     ["NO ACCOUNTS FILED", "ACCOUNTS TYPE NOT AVAILABLE"]),
]
SIZE_OTHER = len(SIZE) - 1

# ---------------------------------------------------------------------- legal type
# id -> (label, colour, hint, raw category values). The last entry is the catch-all.
LTYPE = [
    ("Private limited company", "#3b82f6", "Private Limited Company",
     ["Private Limited Company"]),
    ("Limited by guarantee", "#14b8a6", "Private company limited by guarantee (no share capital)",
     ["PRI/LTD BY GUAR/NSC (Private, limited by guarantee, no share capital)",
      "PRI/LBG/NSC (Private, Limited by guarantee, no share capital, use of 'Limited' exemption)"]),
    ("Limited partnership", "#f59e0b", "Limited and Scottish partnerships",
     ["Limited Partnership", "Scottish Partnership"]),
    ("LLP", "#ef4444", "Limited liability partnership",
     ["Limited Liability Partnership"]),
    ("Community interest (CIC)", "#a855f7", "Community interest company",
     ["Community Interest Company"]),
    ("Charitable (CIO)", "#22c55e", "Charitable incorporated organisations (England/Wales and Scotland)",
     ["Charitable Incorporated Organisation", "Scottish Charitable Incorporated Organisation"]),
    ("PLC", "#0ea5e9", "Public limited company",
     ["Public Limited Company", "Old Public Company"]),
    ("Overseas entity", "#ec4899", "Overseas entity registered in the UK",
     ["Overseas Entity"]),
    ("Other", "#94a3b8", "Registered societies, unlimited companies, royal charter, ICVCs and the rest",
     []),
]
LTYPE_OTHER = len(LTYPE) - 1

# ------------------------------------------------------------------------ industry
# SIC 2007 sections by two-digit division. Only valid 5-digit SIC 2007 codes are used; the old
# 4-digit SIC 2003 codes mean something different, and "None Supplied" / 99999 (dormant) carry no
# industry, so those all land in the last group.
_SECTIONS = [
    ("A", "Agriculture, forestry and fishing", [(1, 3)]),
    ("B", "Mining and quarrying", [(5, 9)]),
    ("C", "Manufacturing", [(10, 33)]),
    ("D", "Electricity, gas, steam and air conditioning", [(35, 35)]),
    ("E", "Water supply, sewerage and waste management", [(36, 39)]),
    ("F", "Construction", [(41, 43)]),
    ("G", "Wholesale and retail trade; motor vehicle repair", [(45, 47)]),
    ("H", "Transportation and storage", [(49, 53)]),
    ("I", "Accommodation and food service", [(55, 56)]),
    ("J", "Information and communication", [(58, 63)]),
    ("K", "Financial and insurance", [(64, 66)]),
    ("L", "Real estate", [(68, 68)]),
    ("M", "Professional, scientific and technical", [(69, 75)]),
    ("N", "Administrative and support services", [(77, 82)]),
    ("O", "Public administration and defence", [(84, 84)]),
    ("P", "Education", [(85, 85)]),
    ("Q", "Human health and social work", [(86, 88)]),
    ("R", "Arts, entertainment and recreation", [(90, 93)]),
    ("S", "Other service activities", [(94, 96)]),
    ("T", "Households as employers / own use", [(97, 98)]),
    ("U", "Extraterritorial organisations", [(99, 99)]),
]
SIC_NONE = len(_SECTIONS)  # id of "no usable industry"
SIC_NONE_LABEL = "Dormant, none supplied or legacy code"


def _hsl(h, s, l):
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


SIC = [(f"{k} · {name}", _hsl(i / len(_SECTIONS), 0.65, 0.5 if i % 2 else 0.42),
        f"SIC 2007 section {k}", [d for lo, hi in rng for d in range(lo, hi + 1)])
       for i, (k, name, rng) in enumerate(_SECTIONS)]
SIC.append((SIC_NONE_LABEL, "#94a3b8", "Dormant (99999), 'None Supplied' or old 4-digit SIC 2003 code", []))

_DIVISION_TO_SIC = {d: i for i, (_, _, _, divs) in enumerate(SIC) for d in divs}
_SIZE_TO_ID = {c: i for i, b in enumerate(SIZE) for c in b[3]}
_LTYPE_TO_ID = {c: i for i, b in enumerate(LTYPE) for c in b[3]}

# ------------------------------------------------------------- raw value -> band id


def size_of(accounts_category):
    return _SIZE_TO_ID.get((accounts_category or "").strip(), SIZE_OTHER)


def ltype_of(category):
    return _LTYPE_TO_ID.get((category or "").strip(), LTYPE_OTHER)


def sic_division(sic_text):
    """Two-digit division of a valid 5-digit SIC 2007 code, else '' (none / dormant / legacy)."""
    code = (sic_text or "").partition(" - ")[0].strip()
    if len(code) != 5 or not code.isdigit() or code == "99999":
        return ""
    return code[:2]


def sic_of_division(div):
    return _DIVISION_TO_SIC.get(int(div), SIC_NONE) if div else SIC_NONE


def sic_of(sic_text):
    return sic_of_division(sic_division(sic_text))


def is_active(status):
    return (status or "").startswith("Active")


# SQL equivalent of sic_division(), usable in a GROUP BY over the companies table.
SIC_DIV_SQL = ("CASE WHEN sic1 GLOB '[0-9][0-9][0-9][0-9][0-9] - *' AND sic1 NOT LIKE '99999%' "
               "THEN substr(sic1,1,2) ELSE '' END")
ACTIVE_SQL = "CASE WHEN status LIKE 'Active%' THEN 1 ELSE 0 END"

# ------------------------------------------------- band ids -> SQL on `companies`


def _in(col, values):
    return f"{col} IN ({','.join('?' * len(values))})", list(values)


def size_sql(ids):
    """WHERE fragment (clause, params) selecting companies in the given size band ids."""
    explicit = [c for i in ids for c in SIZE[i][3]]
    parts, args = [], []
    if explicit:
        c, a = _in("accounts_category", explicit)
        parts.append(c)
        args += a
    if SIZE_OTHER in ids:  # the last band also takes every category not listed in any band
        every = [c for b in SIZE for c in b[3]]
        parts.append(f"accounts_category NOT IN ({','.join('?' * len(every))})")
        args += every
    return _or(parts), args


def ltype_sql(ids):
    explicit = [c for i in ids for c in LTYPE[i][3]]
    parts, args = [], []
    if explicit:
        c, a = _in("category", explicit)
        parts.append(c)
        args += a
    if LTYPE_OTHER in ids:
        every = [c for b in LTYPE for c in b[3]]
        parts.append(f"category NOT IN ({','.join('?' * len(every))})")
        args += every
    return _or(parts), args


def sic_sql(ids):
    divs = [d for i in ids if i != SIC_NONE for d in SIC[i][3]]
    parts, args = [], []
    if divs:
        parts.append(f"({SIC_DIV_SQL}) IN ({','.join('?' * len(divs))})")
        args += [f"{d:02d}" for d in divs]
    if SIC_NONE in ids:
        every = [f"{d:02d}" for d in _DIVISION_TO_SIC]
        parts.append(f"({SIC_DIV_SQL}) NOT IN ({','.join('?' * len(every))})")
        args += every
    return _or(parts), args


def _or(parts):
    return "(" + " OR ".join(parts) + ")" if parts else "0"


def legend():
    """What the front end needs to draw the layer panel."""
    def dump(rows):
        return [{"id": i, "label": r[0], "colour": r[1], "hint": r[2]} for i, r in enumerate(rows)]
    return {"size": dump(SIZE), "type": dump(LTYPE), "sic": dump(SIC)}
