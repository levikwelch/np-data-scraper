# Nonprofit Funding Research

Pipeline to build a vetted list of U.S. nonprofits as funding candidates, using official IRS data and free public APIs (no scraping).

See [`CLAUDE.md`](./CLAUDE.md) for full documentation, project conventions, and commands.

## Quick start

```bash
pip install -r requirements.txt
python scripts/01_download_irs_bmf.py
# edit the FILTERS block in scripts/02_filter_candidates.py
python scripts/02_filter_candidates.py
# optional:
python scripts/03_enrich_propublica.py
```

Results land in `output/grant_candidates.csv`.

## Web UI

After the pipeline has produced `output/grant_candidates.csv`, launch the
filter/export web UI:

```bash
python app.py
```

Open <http://127.0.0.1:8501> in your browser. The sidebar has filters for
state, city, ZIP, NTEE cause code, revenue/asset/income bands, 501(c)
subsection, foundation type, deductibility, IRS status, ruling year, group
exemption, ProPublica enrichment status, and more. The matching count
updates live as you type, and the **Download** button exports the current
filtered set as a CSV.
