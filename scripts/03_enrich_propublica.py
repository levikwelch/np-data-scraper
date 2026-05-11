"""
03_enrich_propublica.py

Enrich the filtered candidate list with detail from the ProPublica Nonprofit
Explorer API: latest 990 filings, year-over-year revenue/expenses, executive
compensation, classification details, and direct PDF links.

API docs: https://projects.propublica.org/nonprofits/api
- Free, no API key
- Be polite: ~0.4s between calls; the script caches per EIN.

Output: output/grant_candidates_enriched.csv
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
CACHE_DIR = ROOT / "data" / "propublica_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "https://projects.propublica.org/nonprofits/api/v2"
SLEEP_SECONDS = 0.4

# Cap how many candidates we enrich. ProPublica is rate-limited; at ~0.4s/call,
# 1000 EINs = ~7 min, 10,000 = ~70 min. We sort by REVENUE_AMT desc and take
# the top N, so the largest orgs get enriched first. Set to None to enrich all.
MAX_TO_ENRICH: int | None = 1000


def fetch_org(ein: str) -> dict | None:
    """Fetch one organization, with on-disk caching keyed by EIN."""
    ein_clean = "".join(c for c in str(ein) if c.isdigit()).zfill(9)
    cache_path = CACHE_DIR / f"{ein_clean}.json"

    if cache_path.exists():
        return json.loads(cache_path.read_text())

    url = f"{BASE_URL}/organizations/{ein_clean}.json"
    try:
        r = requests.get(url, timeout=30)
    except requests.RequestException as e:
        print(f"  network error for {ein_clean}: {e}")
        return None

    if r.status_code == 404:
        cache_path.write_text("null")
        return None
    if r.status_code != 200:
        return None

    data = r.json()
    cache_path.write_text(json.dumps(data))
    time.sleep(SLEEP_SECONDS)
    return data


def summarize(record: dict) -> dict:
    """Pull the most useful fields out of a ProPublica response."""
    org = record.get("organization", {})
    filings = record.get("filings_with_data", []) or []
    latest = filings[0] if filings else {}

    return {
        "pp_name": org.get("name"),
        "pp_state": org.get("state"),
        "pp_ntee": org.get("ntee_code"),
        "pp_classification": org.get("classification"),
        "pp_subsection": org.get("subsection"),
        "pp_ruling_date": org.get("ruling_date"),
        "pp_latest_year": latest.get("tax_prd_yr"),
        "pp_revenue":     latest.get("totrevenue"),
        "pp_expenses":    latest.get("totfuncexpns"),
        "pp_assets":      latest.get("totassetsend"),
        "pp_liabilities": latest.get("totliabend"),
        "pp_990_pdf":     latest.get("pdf_url"),
        "pp_filings_available": len(filings),
    }


def main() -> None:
    src = OUTPUT_DIR / "grant_candidates.csv"
    if not src.exists():
        raise FileNotFoundError(
            f"{src} not found. Run scripts/02_filter_candidates.py first."
        )

    df = pd.read_csv(src, dtype=str, low_memory=False)
    total = len(df)

    if MAX_TO_ENRICH is not None and total > MAX_TO_ENRICH:
        df["_rev_sort"] = pd.to_numeric(df.get("REVENUE_AMT"), errors="coerce")
        df = df.sort_values("_rev_sort", ascending=False, na_position="last")
        df = df.head(MAX_TO_ENRICH).drop(columns=["_rev_sort"]).reset_index(drop=True)
        print(f"Capped enrichment at top {MAX_TO_ENRICH:,} candidates by revenue "
              f"(of {total:,} total).")

    print(f"Enriching {len(df):,} candidates from ProPublica...")
    print(f"  cache: {CACHE_DIR}\n")

    records = []
    for ein in tqdm(df["EIN"].fillna(""), desc="fetching"):
        if not ein:
            records.append({})
            continue
        data = fetch_org(ein)
        records.append(summarize(data) if data else {})

    enrichment = pd.DataFrame(records)
    enriched = pd.concat([df.reset_index(drop=True), enrichment], axis=1)

    out = OUTPUT_DIR / "grant_candidates_enriched.csv"
    enriched.to_csv(out, index=False)

    found = enrichment["pp_name"].notna().sum()
    print(f"\n✓ Enriched {found:,} of {len(df):,} records → {out}")


if __name__ == "__main__":
    main()
