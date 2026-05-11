"""
01b_download_auto_revocation.py

Download the IRS Auto-Revocation List — the canonical, frequently-updated
roster of organizations whose tax-exempt status was revoked for failing to
file a 990-series return for three consecutive years.

The BMF's STATUS field can lag revocations by weeks. This list closes that
gap so the candidate pool excludes orgs that aren't actually exempt anymore.

Source page:
  https://www.irs.gov/charities-non-profits/tax-exempt-organization-search-bulk-data-downloads

Output: data/revoked_eins.csv with one column (EIN) listing every EIN that
is currently revoked (i.e. has a revocation date and no reinstatement date).
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

import pandas as pd
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

DATA_ROOT = Path(os.environ.get("DATA_ROOT",
                                Path(__file__).resolve().parent.parent))
DATA_DIR = DATA_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

REVOCATION_URL = "https://apps.irs.gov/pub/epostcard/data-download-revocation.zip"

# IRS pipe-delimited columns, per the file's published spec.
COLUMNS = [
    "EIN",
    "LEGAL_NAME",
    "DBA_NAME",
    "ADDRESS",
    "CITY",
    "STATE",
    "ZIP",
    "COUNTRY",
    "EXEMPTION_TYPE",
    "REVOCATION_DATE",
    "REVOCATION_POSTING_DATE",
    "REINSTATEMENT_DATE",
]


def main() -> None:
    zip_path = DATA_DIR / "auto_revocation.zip"

    print(f"Downloading {REVOCATION_URL}")
    r = requests.get(REVOCATION_URL, timeout=120)
    r.raise_for_status()
    zip_path.write_bytes(r.content)
    print(f"  saved {len(r.content) / 1e6:.1f} MB to {zip_path.name}")

    print("Extracting...")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
        print(f"  archive members: {names}")
        # IRS publishes a single pipe-delimited text file; pick the largest.
        member = max(names, key=lambda n: zf.getinfo(n).file_size)
        with zf.open(member) as fh:
            raw = fh.read().decode("latin-1", errors="replace")

    # The file has no header row — fixed column order per IRS spec.
    df = pd.read_csv(
        io.StringIO(raw),
        sep="|",
        header=None,
        names=COLUMNS,
        dtype=str,
        engine="python",
        on_bad_lines="skip",
    )
    print(f"  {len(df):,} revocation records loaded")

    # Normalize EIN (strip dashes/whitespace, pad to 9 digits where possible).
    df["EIN"] = df["EIN"].fillna("").str.replace("-", "", regex=False).str.strip()

    # An org is "currently revoked" if it has a revocation date AND no
    # reinstatement date. Reinstated orgs are exempt again and should NOT be
    # filtered out of the candidate pool.
    revoked_mask = (
        df["REVOCATION_DATE"].fillna("").str.strip().ne("")
        & df["REINSTATEMENT_DATE"].fillna("").str.strip().eq("")
    )
    currently_revoked = df.loc[revoked_mask, ["EIN"]].drop_duplicates()

    out = DATA_DIR / "revoked_eins.csv"
    currently_revoked.to_csv(out, index=False)

    print(f"\n✓ {len(currently_revoked):,} currently-revoked EINs → {out}")
    print(f"  ({len(df) - len(currently_revoked):,} reinstated rows excluded)")


if __name__ == "__main__":
    main()
