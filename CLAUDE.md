# Nonprofit Funding Research

A data pipeline to build a vetted list of U.S. nonprofit organizations as candidates for grants or donations.

## Why this approach (no scraping)

The major nonprofit directories — Candid/GuideStar, Charity Navigator, GreatNonprofits, Cause IQ — all prohibit scraping in their Terms of Service. We don't need to scrape them. The same underlying data is available through official, free channels:

1. **IRS Exempt Organizations Business Master File (BMF)** — the source of truth. Free bulk CSV. ~1.8M organizations with EIN, name, address, NTEE classification, revenue, deductibility.
2. **ProPublica Nonprofit Explorer API** — free, no API key required. Adds 990 filings, executive comp, year-over-year financials.
3. **Candid API** (optional) — free tier with registration. Adds mission text, leadership, programs.

This produces cleaner data than scraping with no legal risk and far fewer broken pipelines.

## Project layout

```
.
├── CLAUDE.md                        # this file
├── README.md                        # short human pointer to CLAUDE.md
├── requirements.txt
├── .gitignore
├── app.py                           # Flask filter/export web UI
├── templates/
│   └── index.html                   # web UI page (used by app.py)
├── data/                            # raw downloads (gitignored)
├── output/                          # filtered results (gitignored)
└── scripts/
    ├── 01_download_irs_bmf.py            # bulk IRS download
    ├── 01b_download_auto_revocation.py   # current revoked-EIN list (more current than BMF STATUS)
    ├── 02_filter_candidates.py           # apply funding criteria
    └── 03_enrich_propublica.py           # API enrichment for shortlist
```

## Common commands

```bash
# setup
pip install -r requirements.txt

# pipeline (run in order)
python scripts/01_download_irs_bmf.py            # ~3-5 min, ~500MB on disk
python scripts/01b_download_auto_revocation.py   # ~30 sec, ~46MB; required by 02
python scripts/02_filter_candidates.py           # edit FILTERS block first
python scripts/03_enrich_propublica.py           # optional, slow (rate-limited)

# interactive UI for browsing/filtering/exporting the candidate pool
python app.py                                # Flask, opens at http://127.0.0.1:8501
```

## Conventions

- Raw data lives in `data/` (never committed).
- Filtered outputs land in `output/` (never committed).
- All filtering knobs are in the `FILTERS` block at the top of `02_filter_candidates.py`.
- API rate limits: ProPublica calls are spaced ~0.5s. Respect it.
- Do **not** add scrapers for any of the directory sites (Candid, Charity Navigator, GreatNonprofits, Cause IQ, GuideStar). Their ToS forbid it. Use APIs instead.

## Key columns in the IRS BMF

| Column | Meaning |
|---|---|
| `EIN` | 9-digit federal employer ID |
| `NAME` | legal name |
| `STATE` | 2-letter US state |
| `SUBSECTION` | 501(c) subsection — `03` = public charity |
| `DEDUCTIBILITY` | `1` = contributions are tax-deductible |
| `NTEE_CD` | 4-char mission classification (e.g. `B82` = scholarships) |
| `REVENUE_AMT` | most recently reported gross receipts |
| `ASSET_AMT` | most recently reported total assets |
| `STATUS` | IRS recognition status |
| `FOUNDATION` | foundation/non-foundation classification |

NTEE major codes (the first letter of `NTEE_CD`):

```
A Arts, Culture, Humanities    N Recreation, Sports
B Education                    O Youth Development
C Environment                  P Human Services
D Animal-Related               Q International, Foreign Affairs
E Health                       R Civil Rights, Social Action
F Mental Health                S Community Improvement
G Diseases, Disorders          T Philanthropy, Voluntarism
H Medical Research             U Science & Technology
I Crime, Legal-Related         V Social Science
J Employment                   W Public, Societal Benefit
K Food, Agriculture, Nutrition X Religion-Related
L Housing, Shelter             Y Mutual/Membership Benefit
M Public Safety, Disaster      Z Unknown / Unclassified
```

## Extending

- **Candid API enrichment** — register at developer.candid.org, then add a step after filtering.
- **990 PDF download** — ProPublica returns `pdf_url` per filing; pull them for due diligence.
- **Geocoding** — Census Geocoder is free; useful for mapping candidates.
- **Deduplication / parent-sub linking** — the BMF includes `GROUP` exemption numbers.

## Data refresh cadence

- IRS BMF: re-run `01_download_irs_bmf.py` monthly.
- ProPublica: enrichment can be cached per EIN; re-fetch quarterly.
