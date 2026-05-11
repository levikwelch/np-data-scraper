"""
app.py - Flask web UI for filtering nonprofit grant candidates and exporting CSVs.

Run:
    python app.py
    # then open http://127.0.0.1:8501

Reads from output/grant_candidates.csv (produced by 02_filter_candidates.py).
Cross-references data/propublica_cache/ for the "enrichment cached" filter.
"""
from __future__ import annotations

import os
import sys
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from flask import Flask, Response, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from concurrent.futures import ThreadPoolExecutor, as_completed

from scripts.email_scraper import (
    email_realness_score,
    is_fake_email,
    scrape_website_emails,
)
from scripts.enrich_990_lib import enrich_eins

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).resolve().parent
# DATA_ROOT lets the data/output directories live on a mounted disk in
# production (e.g. Render persistent disk at /var/data). Defaults to the
# repo root so local development behaves exactly as before.
DATA_ROOT = Path(os.environ.get("DATA_ROOT", ROOT))
OUTPUT_DIR = DATA_ROOT / "output"
# The full filtered pool (typically hundreds of thousands of rows).
MASTER_CSV = OUTPUT_DIR / "grant_candidates.csv"
# Optional sidecar with website/phone/mission/officer columns for whatever
# subset has been enriched via scripts/04_enrich_990_xml.py. We left-join
# these columns into the master pool by EIN at startup so the UI shows
# every lead but with contact data attached when available.
CONTACT_CSV_CANDIDATES = [
    OUTPUT_DIR / "grant_candidates_with_contact.csv",
    OUTPUT_DIR / "grant_candidates_enriched.csv",
]
CONTACT_COLS = ("website", "phone", "principal_officer_name", "mission", "latest_990_year")
CACHE_DIR = DATA_ROOT / "data" / "propublica_cache"

NTEE_MAJOR = {
    "A": "Arts, Culture & Humanities",
    "B": "Education",
    "C": "Environment",
    "D": "Animal-Related",
    "E": "Health",
    "F": "Mental Health",
    "G": "Diseases, Disorders & Medical Disciplines",
    "H": "Medical Research",
    "I": "Crime & Legal-Related",
    "J": "Employment",
    "K": "Food, Agriculture & Nutrition",
    "L": "Housing & Shelter",
    "M": "Public Safety & Disaster",
    "N": "Recreation & Sports",
    "O": "Youth Development",
    "P": "Human Services",
    "Q": "International & Foreign Affairs",
    "R": "Civil Rights & Social Action",
    "S": "Community Improvement",
    "T": "Philanthropy & Voluntarism",
    "U": "Science & Technology",
    "V": "Social Science",
    "W": "Public, Societal Benefit",
    "X": "Religion-Related",
    "Y": "Mutual/Membership Benefit",
    "Z": "Unknown / Unclassified",
}

FOUNDATION_LABELS = {
    "00": "00 - Not classified",
    "02": "02 - Private operating foundation (exempt)",
    "03": "03 - Private operating foundation",
    "04": "04 - Private non-operating foundation",
    "09": "09 - Suspense / pending",
    "10": "10 - Church 170(b)(1)(A)(i)",
    "11": "11 - School 170(b)(1)(A)(ii)",
    "12": "12 - Hospital or medical research 170(b)(1)(A)(iii)",
    "13": "13 - Supports government school 170(b)(1)(A)(iv)",
    "14": "14 - Government unit 170(b)(1)(A)(v)",
    "15": "15 - Publicly supported 170(b)(1)(A)(vi)",
    "16": "16 - Publicly supported 509(a)(2)",
    "17": "17 - Public safety testing 509(a)(4)",
    "18": "18 - Supporting org 509(a)(3)",
    "21": "21 - Public charity, type unspecified",
    "22": "22 - Public charity, supporting type I",
    "23": "23 - Public charity, supporting type II",
    "24": "24 - Public charity, supporting type III",
}

STATUS_LABELS = {
    "01": "01 - Unconditional exemption (active)",
    "02": "02 - Conditional exemption",
    "12": "12 - Trust",
    "25": "25 - Revoked",
}

PREVIEW_COLS = [
    "EIN", "NAME", "STATE", "CITY", "NTEE_CD",
    "website", "phone", "principal_officer_name", "mission",
    "REVENUE_AMT", "ASSET_AMT", "INCOME_AMT",
    "SUBSECTION", "FOUNDATION", "DEDUCTIBILITY", "STATUS",
    "RULING_YEAR", "ZIP", "STREET",
]

# Tilegrid coords (row, col) for the small US map shown in the sidebar.
# Stylized rather than geographically exact -- the goal is a recognizable
# silhouette for clicking IRS source regions.
US_TILEGRID: dict[str, tuple[int, int]] = {
    "ME": (0, 11),
    "VT": (1, 10), "NH": (1, 11),
    "WA": (2, 1), "ID": (2, 2), "MT": (2, 3), "ND": (2, 4), "MN": (2, 5),
    "WI": (2, 6),                "MI": (2, 8), "NY": (2, 9),
    "MA": (2, 11),
    "OR": (3, 1), "NV": (3, 2), "UT": (3, 3), "WY": (3, 4), "SD": (3, 5),
    "IA": (3, 6), "IL": (3, 7), "IN": (3, 8), "OH": (3, 9), "PA": (3, 10),
    "NJ": (3, 11), "CT": (3, 12),
    "CA": (4, 1),               "AZ": (4, 3), "CO": (4, 4), "NE": (4, 5),
    "MO": (4, 6), "KY": (4, 7), "WV": (4, 8), "VA": (4, 9), "MD": (4, 10),
    "DE": (4, 11), "RI": (4, 12),
    "NM": (5, 3), "KS": (5, 4), "AR": (5, 5),
    "TN": (5, 6), "NC": (5, 7), "SC": (5, 8), "DC": (5, 9),
    "OK": (6, 4), "LA": (6, 5), "MS": (6, 6), "AL": (6, 7), "GA": (6, 8),
    "HI": (7, 0), "AK": (7, 1), "TX": (7, 4),
    "FL": (7, 8),               "PR": (7, 11),
}

REGION_LABELS = {
    "region1_northeast": "Northeast",
    "region2_midatlantic_greatlakes": "Mid-Atlantic / Great Lakes",
    "region3_gulf_pacific": "Gulf / Pacific",
    "region4_international_other": "International / Other",
}


def _ein9(ein: str) -> str:
    return "".join(c for c in str(ein) if c.isdigit()).zfill(9)


def _resolve_contact_csv() -> Path | None:
    for p in CONTACT_CSV_CANDIDATES:
        if p.exists():
            return p
    return None


def _load_dataframe() -> pd.DataFrame:
    if not MASTER_CSV.exists():
        raise FileNotFoundError(
            f"{MASTER_CSV} not found. "
            "Run scripts/02_filter_candidates.py first."
        )
    df = pd.read_csv(MASTER_CSV, dtype=str, low_memory=False)
    for col in ("REVENUE_AMT", "ASSET_AMT", "INCOME_AMT"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "RULING" in df.columns:
        df["RULING_YEAR"] = pd.to_numeric(
            df["RULING"].fillna("").str.slice(0, 4), errors="coerce"
        ).astype("Int64")
    if "EIN" in df.columns:
        df["_EIN9"] = (
            df["EIN"].fillna("").str.replace(r"\D", "", regex=True).str.zfill(9)
        )

    contact_csv = _resolve_contact_csv()
    if contact_csv is not None:
        contact = pd.read_csv(contact_csv, dtype=str, low_memory=False)
        keep = [c for c in CONTACT_COLS if c in contact.columns]
        if keep and "EIN" in contact.columns:
            contact["_EIN9"] = (
                contact["EIN"].fillna("").str.replace(r"\D", "", regex=True).str.zfill(9)
            )
            contact = contact[["_EIN9"] + keep].drop_duplicates("_EIN9")
            df = df.merge(contact, on="_EIN9", how="left")
    return df


def _load_enriched(cache_dir: Path) -> set[str]:
    if not cache_dir.exists():
        return set()
    return {p.stem.zfill(9) for p in cache_dir.glob("*.json")}


def _build_state_region(df: pd.DataFrame) -> dict[str, str]:
    """Map each state to the IRS source region it predominantly appears in."""
    if "STATE" not in df.columns or "SOURCE_REGION" not in df.columns:
        return {}
    counts = df.groupby(["STATE", "SOURCE_REGION"]).size()
    out: dict[str, str] = {}
    for state in counts.index.get_level_values(0).unique():
        sub = counts.xs(state, level=0)
        out[str(state)] = str(sub.idxmax())
    return out


print("Loading IRS BMF candidates...", flush=True)
DF = _load_dataframe()
ENRICHED = _load_enriched(CACHE_DIR)
STATE_REGION = _build_state_region(DF)
_contact_csv = _resolve_contact_csv()
print(
    f"  {len(DF):,} candidates loaded from {MASTER_CSV.name}"
    + (f" (joined with {_contact_csv.name})" if _contact_csv else "")
    + f"; {len(ENRICHED):,} ProPublica EINs cached."
)
contact_present = [c for c in ("website", "phone", "mission") if c in DF.columns]
if contact_present:
    coverage = ", ".join(
        f"{c}={DF[c].fillna('').str.strip().ne('').sum():,}" for c in contact_present
    )
    print(f"  contact-field coverage: {coverage}")


def _distinct(col: str) -> list[str]:
    if col not in DF.columns:
        return []
    return sorted(DF[col].dropna().unique().tolist())


def build_form_context() -> dict[str, Any]:
    ruling_min = ruling_max = None
    if "RULING_YEAR" in DF.columns and DF["RULING_YEAR"].notna().any():
        ruling_min = int(DF["RULING_YEAR"].min())
        ruling_max = int(DF["RULING_YEAR"].max())
    distinct_states = _distinct("STATE")
    return {
        "total_rows": len(DF),
        "enriched_count": len(ENRICHED),
        "states": distinct_states,
        "regions": _distinct("SOURCE_REGION"),
        "ntee_major": NTEE_MAJOR,
        "ruling_min": ruling_min,
        "ruling_max": ruling_max,
        "preview_cols": [c for c in PREVIEW_COLS if c in DF.columns],
        "state_region": STATE_REGION,
        "us_tilegrid": US_TILEGRID,
        "region_labels": REGION_LABELS,
        "ntee_options": [
            {"value": k, "label": f"{k} - {v}"} for k, v in NTEE_MAJOR.items()
        ],
        "states_options": [{"value": s, "label": s} for s in distinct_states],
    }


def parse_filters(form) -> dict[str, Any]:
    def get_str(k: str) -> str:
        return (form.get(k) or "").strip()

    def get_list(k: str) -> list[str]:
        return [v for v in form.getlist(k) if v]

    def get_int(k: str, default: int = 0) -> int:
        v = form.get(k)
        if v is None or v == "":
            return default
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default

    return {
        "name": get_str("name"),
        "ein": get_str("ein"),
        "states": get_list("states"),
        "city": get_str("city"),
        "zip": get_str("zip"),
        "regions": get_list("regions"),
        "ntee_major": get_list("ntee_major"),
        "ntee_code": get_str("ntee_code"),
        "min_rev": get_int("min_rev"),
        "max_rev": get_int("max_rev"),
        "min_assets": get_int("min_assets"),
        "max_assets": get_int("max_assets"),
        "min_income": get_int("min_income"),
        "max_income": get_int("max_income"),
        "require_revenue": form.get("require_revenue") == "1",
        "subsection": get_list("subsection"),
        "foundation": get_list("foundation"),
        "deductibility": get_str("deductibility"),
        "status": get_list("status"),
        "classification": get_str("classification"),
        "affiliation": get_list("affiliation"),
        "filing_req": get_list("filing_req"),
        "group": get_str("group"),
        "ruling_min": get_int("ruling_min"),
        "ruling_max": get_int("ruling_max"),
        "enrichment": get_str("enrichment"),
    }


def apply_filters(f: dict[str, Any]) -> pd.DataFrame:
    df = DF
    mask = pd.Series(True, index=df.index)

    if f["name"]:
        mask &= df["NAME"].fillna("").str.contains(f["name"], case=False, regex=False)
    if f["ein"]:
        digits = "".join(c for c in f["ein"] if c.isdigit())
        if digits:
            target = digits.zfill(9) if len(digits) == 9 else digits
            mask &= df["_EIN9"].str.startswith(target)
    if f["states"]:
        mask &= df["STATE"].isin(f["states"])
    if f["city"]:
        mask &= df["CITY"].fillna("").str.contains(f["city"], case=False, regex=False)
    if f["zip"]:
        mask &= df["ZIP"].fillna("").str.startswith(f["zip"])
    if f["regions"] and "SOURCE_REGION" in df.columns:
        mask &= df["SOURCE_REGION"].isin(f["regions"])

    if f["ntee_major"]:
        mask &= df["NTEE_CD"].fillna("").str[:1].isin(f["ntee_major"])
    if f["ntee_code"]:
        mask &= df["NTEE_CD"].fillna("").str.upper().str.startswith(f["ntee_code"].upper())

    if f["min_rev"] > 0:
        mask &= (df["REVENUE_AMT"] >= f["min_rev"]).fillna(False)
    if f["max_rev"] > 0:
        mask &= (df["REVENUE_AMT"] <= f["max_rev"]).fillna(False)
    if f["min_assets"] > 0:
        mask &= (df["ASSET_AMT"] >= f["min_assets"]).fillna(False)
    if f["max_assets"] > 0:
        mask &= (df["ASSET_AMT"] <= f["max_assets"]).fillna(False)
    if f["min_income"] > 0:
        mask &= (df["INCOME_AMT"] >= f["min_income"]).fillna(False)
    if f["max_income"] > 0:
        mask &= (df["INCOME_AMT"] <= f["max_income"]).fillna(False)
    if f["require_revenue"]:
        mask &= df["REVENUE_AMT"].notna() & (df["REVENUE_AMT"] > 0)

    if f["subsection"]:
        mask &= df["SUBSECTION"].isin(f["subsection"])
    if f["foundation"]:
        mask &= df["FOUNDATION"].isin(f["foundation"])
    if f["deductibility"] == "yes":
        mask &= df["DEDUCTIBILITY"] == "1"
    elif f["deductibility"] == "no":
        mask &= df["DEDUCTIBILITY"] != "1"
    if f["status"]:
        mask &= df["STATUS"].isin(f["status"])
    if f["classification"]:
        mask &= df["CLASSIFICATION"].fillna("").str.contains(
            f["classification"], case=False, regex=False
        )
    if f["affiliation"]:
        mask &= df["AFFILIATION"].isin(f["affiliation"])
    if f["filing_req"] and "FILING_REQ_CD" in df.columns:
        mask &= df["FILING_REQ_CD"].isin(f["filing_req"])
    if f["group"] == "in" and "GROUP" in df.columns:
        mask &= df["GROUP"].fillna("0").str.strip().replace("", "0") != "0"
    elif f["group"] == "standalone" and "GROUP" in df.columns:
        mask &= df["GROUP"].fillna("0").str.strip().replace("", "0") == "0"

    if "RULING_YEAR" in df.columns:
        if f["ruling_min"] > 0:
            mask &= (df["RULING_YEAR"] >= f["ruling_min"]).fillna(False)
        if f["ruling_max"] > 0:
            mask &= (df["RULING_YEAR"] <= f["ruling_max"]).fillna(False)

    if f["enrichment"] == "cached":
        mask &= df["_EIN9"].isin(ENRICHED)
    elif f["enrichment"] == "not_cached":
        mask &= ~df["_EIN9"].isin(ENRICHED)

    return df.loc[mask]


def _serializable(v: Any) -> Any:
    if v is None or v is pd.NA:
        return ""
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        f = float(v)
        return "" if f != f else f
    if isinstance(v, float):
        return "" if v != v else v
    return v


app = Flask(__name__)
# Honor X-Forwarded-* headers from the reverse proxy (Caddy) so url_for()
# generates correct absolute URLs when the app is mounted under a path
# prefix like /leads/. Caddy sends X-Forwarded-Prefix; ProxyFix reads it
# into SCRIPT_NAME. Safe locally too (no proxy = no headers = no effect).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.json.sort_keys = False  # preserve count-descending order in breakdown dicts
app.config["TEMPLATES_AUTO_RELOAD"] = True  # pick up index.html edits without restart
app.jinja_env.auto_reload = True


@app.route("/")
def index() -> Response:
    return render_template("index.html", **build_form_context())


@app.route("/api/results", methods=["POST"])
def api_results():
    f = parse_filters(request.form)
    filt = apply_filters(f)
    n = len(filt)

    sort_col = (request.form.get("sort_col") or "REVENUE_AMT").upper()
    sort_desc = request.form.get("sort_desc", "1") == "1"
    if sort_col not in filt.columns:
        sort_col = "REVENUE_AMT" if "REVENUE_AMT" in filt.columns else filt.columns[0]

    preview_cols = [c for c in PREVIEW_COLS if c in filt.columns]
    preview_limit = max(50, min(2500, int(request.form.get("preview_limit", 500))))

    rows: list[list[Any]] = []
    if n:
        sorted_df = filt.sort_values(
            sort_col, ascending=not sort_desc, na_position="last"
        )
        head = sorted_df[preview_cols].head(preview_limit)
        for record in head.itertuples(index=False, name=None):
            rows.append([_serializable(v) for v in record])

    rev_total = float(filt["REVENUE_AMT"].fillna(0).sum()) if "REVENUE_AMT" in filt.columns else 0.0
    states_n = int(filt["STATE"].nunique()) if "STATE" in filt.columns else 0
    ntee_n = int(filt["NTEE_CD"].nunique()) if "NTEE_CD" in filt.columns else 0

    top_states = (
        {str(k): int(v) for k, v in filt["STATE"].value_counts().head(15).items()}
        if "STATE" in filt.columns else {}
    )
    if "NTEE_CD" in filt.columns:
        major = filt["NTEE_CD"].fillna("").str[:1]
        top_ntee = major.value_counts().head(15)
        top_ntee_dict = {
            f"{(code or '(none)')} - {NTEE_MAJOR.get(code, 'Unknown')}": int(cnt)
            for code, cnt in top_ntee.items()
        }
    else:
        top_ntee_dict = {}

    return jsonify({
        "count": n,
        "total": len(DF),
        "rev_total": rev_total,
        "states_n": states_n,
        "ntee_n": ntee_n,
        "preview_cols": preview_cols,
        "preview_rows": rows,
        "top_states": top_states,
        "top_ntee_major": top_ntee_dict,
    })


@app.route("/export.csv", methods=["POST"])
def export_csv() -> Response:
    f = parse_filters(request.form)
    filt = apply_filters(f)
    compact = request.form.get("compact") == "1"

    if compact:
        cols = [c for c in PREVIEW_COLS if c in filt.columns]
        out = filt[cols]
    else:
        out = filt.drop(columns=[c for c in ("_EIN9",) if c in filt.columns])

    csv_str = out.to_csv(index=False)

    fname = (request.form.get("fname") or "").strip()
    if not fname:
        fname = f"grant_candidates_filtered_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    if not fname.lower().endswith(".csv"):
        fname += ".csv"

    return Response(
        csv_str,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# --------------------- Async scrape with progress ---------------------
# Browser flow:
#   POST /scrape/start     →  { job_id }
#   GET  /scrape/status/<id> → { stage, current, total, message, done, error }
#   GET  /scrape/result/<id> → CSV download (only valid once done=true)
#
# Job state lives in process memory. The Flask dev server is multi-threaded
# so background work runs alongside polling requests on the same process.
SCRAPE_JOBS: dict[str, dict[str, Any]] = {}
SCRAPE_LOCK = threading.Lock()
NEW_CONTACT_COLS = ("phone", "website", "principal_officer_name", "mission", "latest_990_year")


EMAIL_SCRAPE_WORKERS = 20  # spec's recommendation for Phase 1 fan-out


def _scrape_emails_for(parsed: dict[str, dict],
                       progress_cb) -> dict[str, list[str]]:
    """Phase 1 only: HTTP-fetch emails from every website returned by enrich_eins.

    Returns {ein_clean: ranked_emails}. Skips EINs without a website. Phase 2
    (Playwright) is deliberately not run here -- it's too slow for an
    interactive download flow. Run scripts/05_enrich_emails.py for thorough
    batch enrichment that includes JS-rendered sites.
    """
    targets: list[tuple[str, str]] = []
    for ein, rec in parsed.items():
        site = (rec or {}).get("website") if isinstance(rec, dict) else None
        if site and str(site).strip():
            targets.append((ein, str(site).strip()))

    total = len(targets)
    progress_cb({"stage": "scraping_emails", "current": 0, "total": total,
                 "message": f"scraping emails from {total:,} sites"})
    if not total:
        return {}

    results: dict[str, list[str]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=EMAIL_SCRAPE_WORKERS) as ex:
        futures = {ex.submit(scrape_website_emails, site): ein
                   for ein, site in targets}
        for fut in as_completed(futures):
            ein = futures[fut]
            try:
                emails = fut.result()
            except Exception:
                emails = []
            # Drop placeholders ("yourname@example.com", "12345@gmail.com",
            # etc.) before ranking so they never reach the CSV.
            emails = [e for e in emails if not is_fake_email(e)]
            if emails:
                results[ein] = sorted(emails, key=email_realness_score,
                                      reverse=True)
            done += 1
            if done % 25 == 0 or done == total:
                progress_cb({"stage": "scraping_emails", "current": done,
                             "total": total,
                             "message": f"{done:,}/{total:,} sites scraped, "
                                        f"{len(results):,} with emails"})
    return results


def _build_scrape_csv(filt: pd.DataFrame, parsed: dict[str, dict],
                      emails_by_ein: dict[str, list[str]],
                      compact: bool) -> str:
    """Merge live-fetched fields onto filt and serialize to CSV.

    Emits one row per email: a lead with three scraped addresses becomes
    three rows, identical except for the `email` column. Leads with no
    surviving emails (after placeholder filtering) are dropped entirely.
    Emails within each lead are realness-ranked so the first row for that
    EIN is the most-likely-real personal address.
    """
    for col in NEW_CONTACT_COLS:
        if col not in filt.columns:
            filt[col] = pd.NA

    eins_clean = filt["EIN"].fillna("").map(_ein9)
    for col in NEW_CONTACT_COLS:
        live = eins_clean.map(lambda e, c=col: (parsed.get(e) or {}).get(c))
        filt[col] = live.where(live.notna() & (live.astype(str).str.strip() != ""), filt[col])

    filt["email"] = eins_clean.map(lambda e: emails_by_ein.get(e) or [])
    filt = filt[filt["email"].map(len) > 0].explode("email", ignore_index=True)

    if compact:
        cols = [c for c in PREVIEW_COLS if c in filt.columns] + ["email"]
        out = filt[cols]
    else:
        out = filt.drop(columns=[c for c in ("_EIN9",) if c in filt.columns])

    return out.to_csv(index=False)


@app.route("/scrape/start", methods=["POST"])
def scrape_start():
    f = parse_filters(request.form)
    filt = apply_filters(f).copy()
    compact = request.form.get("compact") == "1"
    fname_raw = (request.form.get("fname") or "").strip()

    job_id = uuid.uuid4().hex[:12]
    target_eins = [e for e in filt["EIN"].fillna("") if e] if "EIN" in filt.columns else []
    unique_eins = len(set(target_eins))

    with SCRAPE_LOCK:
        SCRAPE_JOBS[job_id] = {
            "stage": "queued",
            "current": 0,
            "total": 0,
            "message": "queued",
            "done": False,
            "error": None,
            "lead_count": len(filt),
            "unique_eins": unique_eins,
            "result_csv": None,
            "filename": None,
        }

    def progress_cb(info: dict) -> None:
        with SCRAPE_LOCK:
            job = SCRAPE_JOBS.get(job_id)
            if job is not None:
                job.update(info)

    def run() -> None:
        try:
            print(f"[scrape:{job_id}] filtered={len(filt):,}  unique EINs={unique_eins:,}",
                  flush=True)
            parsed = enrich_eins(
                target_eins,
                log=lambda m: print(f"[scrape:{job_id}] {m}", flush=True),
                progress=progress_cb,
            )
            emails_by_ein = _scrape_emails_for(parsed, progress_cb)
            csv_str = _build_scrape_csv(filt, parsed, emails_by_ein, compact)
            fname = fname_raw or f"grant_candidates_scraped_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            if not fname.lower().endswith(".csv"):
                fname += ".csv"
            total_emails = sum(len(v) for v in emails_by_ein.values())
            with SCRAPE_LOCK:
                job = SCRAPE_JOBS.get(job_id)
                if job is not None:
                    job["result_csv"] = csv_str
                    job["filename"] = fname
                    job["done"] = True
                    job["stage"] = "done"
                    job["message"] = (
                        f"complete: {len(emails_by_ein):,} leads with emails "
                        f"({total_emails:,} rows, one per address); "
                        f"{len(filt) - len(emails_by_ein):,} emailless leads dropped"
                    )
        except Exception as e:
            traceback.print_exc()
            with SCRAPE_LOCK:
                job = SCRAPE_JOBS.get(job_id)
                if job is not None:
                    job["error"] = str(e)
                    job["done"] = True
                    job["stage"] = "error"
                    job["message"] = f"error: {e}"

    threading.Thread(target=run, name=f"scrape-{job_id}", daemon=True).start()
    return jsonify({
        "job_id": job_id,
        "lead_count": len(filt),
        "unique_eins": unique_eins,
    })


@app.route("/scrape/status/<job_id>")
def scrape_status(job_id: str):
    with SCRAPE_LOCK:
        job = SCRAPE_JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "unknown job"}), 404
        # Don't ship the CSV blob over the polling channel.
        return jsonify({k: v for k, v in job.items() if k != "result_csv"})


@app.route("/scrape/result/<job_id>")
def scrape_result(job_id: str):
    with SCRAPE_LOCK:
        job = SCRAPE_JOBS.get(job_id)
        if job is None:
            return Response("unknown job", status=404)
        if not job.get("done"):
            return Response("not ready", status=409)
        if job.get("error"):
            return Response(job["error"], status=500, mimetype="text/plain")
        csv_str = job["result_csv"]
        fname = job["filename"]
        # Free memory once delivered — single-shot download.
        SCRAPE_JOBS.pop(job_id, None)

    return Response(
        csv_str,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8501)
