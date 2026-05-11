"""
02_filter_candidates.py

Filter the combined IRS BMF down to a shortlist of nonprofits that match
your funding criteria. Edit the FILTERS block to tune.

Output: output/grant_candidates.csv
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("DATA_ROOT", ROOT))
DATA_DIR = DATA_ROOT / "data"
OUTPUT_DIR = DATA_ROOT / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================================
# CONFIGURE YOUR FUNDING CRITERIA HERE
#
# Knobs you'll commonly tune per run:
#   - states, ntee_prefixes, min_revenue, max_revenue
# Knobs you'll rarely change (set-and-forget for finding grant recipients):
#   - subsection, deductible_only, status, public_charity_only
# ============================================================================
FILTERS: dict = {
    # 2-letter US state codes; empty list = all states.
    "states": [],

    # NTEE major-code prefixes to include. Empty list = all causes.
    # See CLAUDE.md for the full code reference.
    # Examples:  ["A"] arts,  ["B"] education,  ["E", "F"] health,
    #            ["P", "L"] human services + housing
    "ntee_prefixes": [],

    # Revenue band (USD). Use None to skip a bound.
    # Tip: small grants often target the $50k–$5M sweet spot — large enough
    # to have capacity, small enough that your gift is meaningful.
    "min_revenue": 50_000,
    "max_revenue": 5_000_000,

    # 501(c) subsection. "03" = 501(c)(3). Set to None to include all.
    "subsection": "03",

    # Require contributions to be tax-deductible (DEDUCTIBILITY == 1).
    "deductible_only": True,

    # IRS recognition status. "01" = unconditional exemption (active).
    "status": "01",

    # Restrict to public charities (exclude private foundations).
    # FOUNDATION codes 02/03/04 are private foundations — wrong target when
    # you're trying to GIVE money out. Codes 10-18, 21-24 are public charities.
    "public_charity_only": True,

    # Cross-check against the IRS Auto-Revocation List (data/revoked_eins.csv)
    # and drop any EIN whose exempt status has been revoked and not reinstated.
    # BMF STATUS can lag revocations by weeks; this closes that gap.
    "exclude_revoked": True,
}
# ============================================================================

# BMF FOUNDATION codes that indicate a public charity (not a private foundation).
PUBLIC_CHARITY_FOUNDATION_CODES = {
    "10", "11", "12", "13", "14", "15", "16", "17", "18",
    "21", "22", "23", "24",
}


def main() -> None:
    src = DATA_DIR / "irs_bmf.csv"
    if not src.exists():
        raise FileNotFoundError(
            f"{src} not found. Run scripts/01_download_irs_bmf.py first."
        )

    print(f"Loading {src.name}...")
    df = pd.read_csv(src, dtype=str, low_memory=False)
    print(f"  {len(df):,} organizations loaded\n")

    # Coerce numerics
    for col in ("REVENUE_AMT", "ASSET_AMT", "INCOME_AMT"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    mask = pd.Series(True, index=df.index)

    def apply(name: str, condition: pd.Series) -> None:
        nonlocal mask
        before = int(mask.sum())
        mask &= condition.fillna(False)
        after = int(mask.sum())
        print(f"  {name:<30} {before:>10,} → {after:>10,}")

    print("Applying filters:")

    if FILTERS["states"]:
        apply(f"states={FILTERS['states']}", df["STATE"].isin(FILTERS["states"]))

    if FILTERS["ntee_prefixes"]:
        prefixes = tuple(FILTERS["ntee_prefixes"])
        apply(
            f"NTEE prefix={list(prefixes)}",
            df["NTEE_CD"].fillna("").str.startswith(prefixes),
        )

    if FILTERS["subsection"]:
        apply(
            f"subsection={FILTERS['subsection']}",
            df["SUBSECTION"] == FILTERS["subsection"],
        )

    if FILTERS["deductible_only"]:
        apply("deductible only", df["DEDUCTIBILITY"] == "1")

    if FILTERS["status"]:
        apply(f"status={FILTERS['status']}", df["STATUS"] == FILTERS["status"])

    if FILTERS["public_charity_only"]:
        apply(
            "public charity (not private foundation)",
            df["FOUNDATION"].fillna("").isin(PUBLIC_CHARITY_FOUNDATION_CODES),
        )

    if FILTERS["exclude_revoked"]:
        revoked_path = DATA_DIR / "revoked_eins.csv"
        if not revoked_path.exists():
            raise FileNotFoundError(
                f"{revoked_path} not found. Run scripts/01b_download_auto_revocation.py "
                f"first, or set FILTERS['exclude_revoked'] = False to skip this check."
            )
        revoked = pd.read_csv(revoked_path, dtype=str)["EIN"].astype(str)
        revoked_set = set(revoked.str.replace("-", "", regex=False).str.strip())
        bmf_ein = df["EIN"].fillna("").astype(str).str.replace("-", "", regex=False).str.strip()
        apply(
            f"not on auto-revocation list ({len(revoked_set):,} EINs)",
            ~bmf_ein.isin(revoked_set),
        )

    if FILTERS["min_revenue"] is not None:
        apply(f"revenue ≥ ${FILTERS['min_revenue']:,}",
              df["REVENUE_AMT"] >= FILTERS["min_revenue"])
    if FILTERS["max_revenue"] is not None:
        apply(f"revenue ≤ ${FILTERS['max_revenue']:,}",
              df["REVENUE_AMT"] <= FILTERS["max_revenue"])

    filtered = df.loc[mask].copy()

    # Reorder useful columns to the front
    front = ["EIN", "NAME", "STATE", "CITY", "NTEE_CD",
             "REVENUE_AMT", "ASSET_AMT", "RULING", "STATUS"]
    front = [c for c in front if c in filtered.columns]
    rest = [c for c in filtered.columns if c not in front]
    filtered = filtered[front + rest]

    out = OUTPUT_DIR / "grant_candidates.csv"
    filtered.to_csv(out, index=False)
    print(f"\n✓ {len(filtered):,} candidates saved to {out}")

    if len(filtered):
        print("\nTop 10 by revenue:")
        top = (filtered.nlargest(10, "REVENUE_AMT")
                       [["NAME", "STATE", "NTEE_CD", "REVENUE_AMT"]])
        print(top.to_string(index=False))


if __name__ == "__main__":
    main()
