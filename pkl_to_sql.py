# Converts crystal_descriptions.pkl into seed_crystals_full.sql
# (run once: ./.venv/bin/python pkl_to_sql.py)

import pickle

import pandas as pd

SRC = "../crystal_descriptions.pkl"
DST = "seed_crystals_full.sql"


def clean(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text.replace("'", "''")


with open(SRC, "rb") as f:
    df = pickle.load(f)

# The index holds the crystal name; drop duplicate names (compared after
# trimming whitespace), keep the first.
rows = []
seen: set[str] = set()
for name, row in df.iterrows():
    name = clean(name)
    if not name or name.lower() in seen:
        continue
    seen.add(name.lower())
    rows.append(
        f"  ('{name}', '{clean(row.get('Headline'))}', "
        f"'{clean(row.get('Description'))}', "
        f"'{clean(row.get('Star sign '))}', "
        f"'{clean(row.get('Chakras'))}')"
    )

with open(DST, "w") as f:
    f.write(
        "-- Generated from crystal_descriptions.pkl — run in the Supabase SQL editor.\n"
        "insert into crystals (name, headline, description, star_sign, chakras) values\n"
    )
    f.write(",\n".join(rows))
    f.write(
        "\non conflict (name) do update set\n"
        "  headline = excluded.headline,\n"
        "  description = excluded.description,\n"
        "  star_sign = excluded.star_sign,\n"
        "  chakras = excluded.chakras;\n"
    )

print(f"Wrote {DST} with {len(rows)} crystals")
