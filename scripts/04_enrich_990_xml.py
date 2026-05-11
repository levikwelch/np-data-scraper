"""
04_enrich_990_xml.py

Enrich the candidate list with phone, website, principal officer, and mission
text — pulled from the IRS Form 990 XML filings (free, no API key).

This data is NOT in the BMF (mailing address only) or in the ProPublica API
(financials only). Phone + website live on Form 990 page 1.

This is a thin CLI around scripts/enrich_990_lib.py — the same library used
by the Flask UI's on-demand "Scrape & download" action.

Output: output/grant_candidates_with_contact.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from tqdm import tqdm

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# Allow running this file directly (not as `python -m scripts.04_...`).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.enrich_990_lib import enrich_eins  # noqa: E402

OUTPUT_DIR = ROOT / "output"


def main() -> None:
    # Pull from the full filtered pool by default. The historical behavior
    # (read from grant_candidates_enriched.csv when present) silently capped
    # this to whatever scripts/03 enriched, which is small. We want all leads.
    src = OUTPUT_DIR / "grant_candidates.csv"
    if not src.exists():
        raise FileNotFoundError(
            "No candidate file found. Run scripts/02_filter_candidates.py first."
        )
    print(f"Reading {src.name} ({src.stat().st_size / 1e6:.1f} MB)")
    df = pd.read_csv(src, dtype=str, low_memory=False)

    eins = [e for e in df["EIN"].fillna("") if e]
    print(f"  {len(eins):,} EIN rows; will look up the unique set\n")

    bar = tqdm(total=0, desc="enrich")

    def log(msg: str) -> None:
        bar.write(msg)

    parsed_by_ein = enrich_eins(eins, log=log)
    bar.close()

    # Align enrichment back to dataframe order.
    records: list[dict] = []
    for ein in df["EIN"].fillna(""):
        ein_clean = "".join(c for c in str(ein) if c.isdigit()).zfill(9)
        records.append(parsed_by_ein.get(ein_clean, {}))

    enrichment = pd.DataFrame(records)
    enriched = pd.concat([df.reset_index(drop=True), enrichment], axis=1)

    out = OUTPUT_DIR / "grant_candidates_with_contact.csv"
    enriched.to_csv(out, index=False)

    have_phone = enrichment["phone"].notna().sum() if "phone" in enrichment else 0
    have_web = enrichment["website"].notna().sum() if "website" in enrichment else 0
    have_mission = enrichment["mission"].notna().sum() if "mission" in enrichment else 0

    print(f"\n[OK] Wrote {out}")
    print(f"  phone:        {have_phone:>7,} / {len(df):,}")
    print(f"  website:      {have_web:>7,} / {len(df):,}")
    print(f"  mission text: {have_mission:>7,} / {len(df):,}")
    print("  EINs with no full 990 in the index file 990-N postcards instead "
          "(no website field on that form).")


if __name__ == "__main__":
    main()
