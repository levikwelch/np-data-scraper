"""
01_download_irs_bmf.py

Download the IRS Exempt Organizations Business Master File (BMF) — the
official, public list of all U.S. tax-exempt organizations.

The IRS publishes BMF extracts as four regional CSVs. This script downloads
each and combines them into a single `data/irs_bmf.csv`.

Source page (verify URLs current):
  https://www.irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract-eo-bmf
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

DATA_ROOT = Path(os.environ.get("DATA_ROOT",
                                Path(__file__).resolve().parent.parent))
DATA_DIR = DATA_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Verify these URLs against the IRS page above before each run; the IRS
# occasionally renames or relocates the regional extracts.
BMF_URLS: dict[str, str] = {
    "region1_northeast":               "https://www.irs.gov/pub/irs-soi/eo1.csv",
    "region2_midatlantic_greatlakes":  "https://www.irs.gov/pub/irs-soi/eo2.csv",
    "region3_gulf_pacific":            "https://www.irs.gov/pub/irs-soi/eo3.csv",
    "region4_international_other":     "https://www.irs.gov/pub/irs-soi/eo4.csv",
}


def download_file(url: str, dest: Path) -> None:
    """Stream a URL to disk with a progress bar."""
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(dest, "wb") as f, tqdm(
            desc=dest.name, total=total, unit="B", unit_scale=True
        ) as bar:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
                bar.update(len(chunk))


def main() -> None:
    # 1. Download each regional file
    for name, url in BMF_URLS.items():
        dest = DATA_DIR / f"{name}.csv"
        if dest.exists():
            print(f"✓ {name} already downloaded ({dest.stat().st_size / 1e6:.1f} MB)")
            continue
        print(f"↓ Downloading {name} from {url}")
        download_file(url, dest)

    # 2. Combine into a single file
    print("\nCombining regional files...")
    frames = []
    for name in BMF_URLS:
        path = DATA_DIR / f"{name}.csv"
        df = pd.read_csv(path, dtype=str, low_memory=False)
        df["SOURCE_REGION"] = name
        frames.append(df)
        print(f"  {name}: {len(df):,} rows")

    combined = pd.concat(frames, ignore_index=True)
    out = DATA_DIR / "irs_bmf.csv"
    combined.to_csv(out, index=False)

    print(f"\n✓ Saved {len(combined):,} organizations to {out}")
    print(f"  File size: {out.stat().st_size / 1e6:.1f} MB")
    print(f"  Columns: {list(combined.columns)}")


if __name__ == "__main__":
    main()
