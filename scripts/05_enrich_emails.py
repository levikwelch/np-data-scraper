"""
05_enrich_emails.py

Enrich the candidate list with contact email addresses scraped from each
nonprofit's website (the `website` column populated by 04_enrich_990_xml.py
from Form 990 XML filings).

Two-phase pipeline via scripts/email_scraper.py:
  Phase 1: direct HTTP fan-out, ThreadPoolExecutor(max_workers=MAX_WORKERS).
  Phase 2: headless Chromium for sites that returned nothing from Phase 1.

Output: output/grant_candidates_with_emails.csv
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

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402

from scripts.email_scraper import (  # noqa: E402
    HAS_PLAYWRIGHT,
    email_realness_score,
    scrape_emails_playwright,
    scrape_website_emails,
)

OUTPUT_DIR = ROOT / "output"

# ============================================================================
# KNOBS
# ============================================================================
FILTERS: dict = {
    # Phase 1 concurrency. 20 is the spec's recommendation; keeps SMB sites
    # happy without thrashing.
    "MAX_WORKERS": 20,

    # If True, skip Phase 2 entirely (don't render JS-heavy sites).
    "SKIP_PLAYWRIGHT": False,

    # Cap rows for smoke tests. None = run the full set.
    "LIMIT": None,

    # Flush the output CSV every N completed sites so a crash doesn't lose
    # progress. Resume picks up from this file on the next run.
    "FLUSH_EVERY": 500,
}
# ============================================================================


SRC = OUTPUT_DIR / "grant_candidates_with_contact.csv"
OUT = OUTPUT_DIR / "grant_candidates_with_emails.csv"


def _emails_to_columns(emails: list[str], source: str) -> dict:
    """Render the email list into the four output columns."""
    if not emails:
        return {"emails": "", "email_primary": "", "email_count": 0,
                "email_source": ""}
    ranked = sorted(emails, key=email_realness_score, reverse=True)
    return {
        "emails": ";".join(emails),
        "email_primary": ranked[0],
        "email_count": len(emails),
        "email_source": source,
    }


def _load_resume_cache() -> dict[str, dict]:
    """Load previously-scraped EINs from the output CSV, if it exists."""
    if not OUT.exists():
        return {}
    try:
        prev = pd.read_csv(OUT, dtype=str, low_memory=False)
    except Exception:
        return {}
    if "EIN" not in prev.columns or "email_source" not in prev.columns:
        return {}
    cache: dict[str, dict] = {}
    for _, row in prev.iterrows():
        ein = (row.get("EIN") or "").strip()
        if not ein:
            continue
        cache[ein] = {
            "emails": row.get("emails") or "",
            "email_primary": row.get("email_primary") or "",
            "email_count": int(row.get("email_count") or 0)
                           if str(row.get("email_count") or "").strip().isdigit() else 0,
            "email_source": row.get("email_source") or "",
        }
    return cache


def _write_output(df: pd.DataFrame, results: dict[str, dict]) -> None:
    """Merge `results` (keyed by EIN) onto df and write OUT."""
    new_cols = {"emails": [], "email_primary": [], "email_count": [], "email_source": []}
    for ein in df["EIN"].fillna(""):
        r = results.get(ein, {"emails": "", "email_primary": "",
                              "email_count": 0, "email_source": ""})
        new_cols["emails"].append(r["emails"])
        new_cols["email_primary"].append(r["email_primary"])
        new_cols["email_count"].append(r["email_count"])
        new_cols["email_source"].append(r["email_source"])

    enriched = df.copy()
    for k, v in new_cols.items():
        enriched[k] = v
    enriched.to_csv(OUT, index=False)


def main() -> None:
    if not SRC.exists():
        raise FileNotFoundError(
            f"{SRC} not found. Run scripts/04_enrich_990_xml.py first."
        )

    print(f"Reading {SRC.name} ({SRC.stat().st_size / 1e6:.1f} MB)")
    df = pd.read_csv(SRC, dtype=str, low_memory=False)

    if "website" not in df.columns:
        raise RuntimeError(
            "Input CSV has no `website` column. Run 04_enrich_990_xml.py first."
        )

    mask = df["website"].fillna("").str.strip().ne("")
    work = df.loc[mask, ["EIN", "website"]].copy()
    work["website"] = work["website"].str.strip()
    work["EIN"] = work["EIN"].fillna("").str.strip()
    work = work[work["EIN"].ne("")].drop_duplicates(subset=["EIN"])

    if FILTERS["LIMIT"] is not None:
        work = work.head(FILTERS["LIMIT"])

    cache = _load_resume_cache()
    if cache:
        print(f"  resume: skipping {len(cache):,} EIN(s) already in {OUT.name}")
    todo = work[~work["EIN"].isin(cache.keys())]

    print(f"  {len(work):,} rows have a website; {len(todo):,} to scrape "
          f"({len(work) - len(todo):,} cached)")
    if not HAS_PLAYWRIGHT and not FILTERS["SKIP_PLAYWRIGHT"]:
        print("  [info] playwright not installed -- Phase 2 (JS-rendered "
              "sites) will be skipped.")
        print("         To enable: pip install playwright && playwright "
              "install chromium")
    print()

    results: dict[str, dict] = dict(cache)

    # ---- Phase 1: direct HTTP ----------------------------------------------
    phase1_hits = 0
    phase1_empty: list[tuple[str, str]] = []  # (ein, website)
    completed = 0

    if len(todo):
        with ThreadPoolExecutor(max_workers=FILTERS["MAX_WORKERS"]) as ex:
            futures = {
                ex.submit(scrape_website_emails, row["website"]):
                    (row["EIN"], row["website"])
                for _, row in todo.iterrows()
            }
            with tqdm(total=len(futures), desc="phase1") as bar:
                for fut in as_completed(futures):
                    ein, site = futures[fut]
                    try:
                        emails = fut.result()
                    except Exception:
                        emails = []
                    if emails:
                        results[ein] = _emails_to_columns(emails, "direct")
                        phase1_hits += 1
                    else:
                        phase1_empty.append((ein, site))
                        results[ein] = _emails_to_columns([], "")
                    completed += 1
                    bar.update(1)
                    if completed % FILTERS["FLUSH_EVERY"] == 0:
                        _write_output(df, results)

    print(f"  phase1: {phase1_hits:,} hit / {len(phase1_empty):,} empty")
    _write_output(df, results)

    # ---- Phase 2: Playwright fallback --------------------------------------
    if FILTERS["SKIP_PLAYWRIGHT"]:
        print("  phase2: skipped (SKIP_PLAYWRIGHT=True)")
    elif not HAS_PLAYWRIGHT:
        print("  phase2: skipped (playwright not installed)")
    elif not phase1_empty:
        print("  phase2: nothing to do")
    else:
        sites = [s for _, s in phase1_empty]
        # Map site -> [ein, ein, ...] because we deduped on EIN but the same
        # website could appear under multiple EINs (rare, but harmless).
        site_to_eins: dict[str, list[str]] = {}
        for ein, site in phase1_empty:
            site_to_eins.setdefault(site, []).append(ein)

        unique_sites = list(site_to_eins.keys())
        print(f"  phase2: rendering {len(unique_sites):,} site(s) in Chromium...")
        rendered = scrape_emails_playwright(unique_sites)

        phase2_hits = 0
        for site, emails in rendered.items():
            if not emails:
                continue
            for ein in site_to_eins.get(site, []):
                results[ein] = _emails_to_columns(emails, "playwright")
                phase2_hits += 1
        print(f"  phase2: {phase2_hits:,} additional hit(s)")

    _write_output(df, results)

    total_with_email = sum(1 for v in results.values() if v["email_primary"])
    print(f"\n[OK] Wrote {OUT}")
    print(f"  rows with at least one email: {total_with_email:,} / {len(work):,}")


if __name__ == "__main__":
    main()
