"""
Library: pull contact + officer + mission fields from IRS Form 990 XML filings
for an arbitrary set of EINs. Used by:
  - scripts/04_enrich_990_xml.py  (bulk CLI)
  - app.py /scrape.csv             (on-demand from the Flask UI)

The IRS publishes Form 990 XML at apps.irs.gov:
  - Annual filing index:   /pub/epostcard/990/xml/<YEAR>/index_<YEAR>.csv
  - Monthly batch ZIPs:    /pub/epostcard/990/xml/<YEAR>/<YEAR>_TEOS_XML_<NN>.zip

Each batch ZIP holds ~10K filings as <OBJECT_ID>_public.xml. We use HTTP Range
requests via the `remotezip` library to extract only the XMLs we need for a
given EIN list — typically a few hundred KB per batch instead of ~70MB.

All XML extracts are cached on disk per-EIN, so repeated calls are cheap.
"""
from __future__ import annotations

import time
import zipfile
from pathlib import Path
from typing import Callable, Iterable
from xml.etree import ElementTree as ET

import inflate64
import pandas as pd
import requests
from remotezip import RemoteIOError, RemoteZip

# IRS batch ZIPs use ZIP compression method 9 (deflate64), which Python's
# stdlib zipfile doesn't support. Register a minimal decompressor wrapper
# around the `inflate64` package so ZipExtFile._read1 can stream through it.
_DEFLATE64 = 9


class _Deflate64Decompressor:
    def __init__(self) -> None:
        self._inflater = inflate64.Inflater()

    def decompress(self, data: bytes, max_length: int = 0) -> bytes:
        return self._inflater.inflate(data)

    @property
    def eof(self) -> bool:
        return self._inflater.eof


_orig_check = zipfile._check_compression
_orig_get = zipfile._get_decompressor


def _patched_check(compression: int) -> None:
    if compression == _DEFLATE64:
        return
    _orig_check(compression)


def _patched_get(compress_type: int):
    if compress_type == _DEFLATE64:
        return _Deflate64Decompressor()
    return _orig_get(compress_type)


zipfile._check_compression = _patched_check
zipfile._get_decompressor = _patched_get


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
XML_CACHE = DATA_DIR / "irs_990_cache"
INDEX_CACHE = DATA_DIR / "irs_990_index"
XML_CACHE.mkdir(parents=True, exist_ok=True)
INDEX_CACHE.mkdir(parents=True, exist_ok=True)

# IRS processing years to search, newest first. The "year" is when the IRS
# *processed* the return, not the tax year — pre-2024 indexes lack the
# XML_BATCH_ID column we rely on, so we skip them.
DEFAULT_YEARS = [2026, 2025, 2024]

INDEX_URL = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.csv"
BATCH_URL = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/{batch_id}.zip"

NS = {"e": "http://www.irs.gov/efile"}

ProgressFn = Callable[[str], None]
StatusFn = Callable[[dict], None]


def _ein9(ein: str) -> str:
    return "".join(c for c in str(ein) if c.isdigit()).zfill(9)


def load_year_index(year: int) -> pd.DataFrame | None:
    """Download & cache one annual 990 filing index. Returns None if 404."""
    path = INDEX_CACHE / f"index_{year}.csv"
    if not path.exists():
        url = INDEX_URL.format(year=year)
        r = requests.get(url, timeout=300)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        path.write_bytes(r.content)
    return pd.read_csv(path, dtype=str, low_memory=False)


def load_indexes(
    years: Iterable[int] = DEFAULT_YEARS, log: ProgressFn | None = None
) -> dict[int, pd.DataFrame]:
    """Load all usable IRS 990 filing indexes keyed by year."""
    out: dict[int, pd.DataFrame] = {}
    for y in years:
        idx = load_year_index(y)
        if idx is None:
            if log:
                log(f"index_{y}.csv not yet published — skipping")
            continue
        if "XML_BATCH_ID" not in idx.columns:
            if log:
                log(f"index_{y}: lacks XML_BATCH_ID — skipping (legacy format)")
            continue
        out[y] = idx
        if log:
            log(f"index_{y}: {len(idx):,} filings")
    return out


def build_ein_lookup(
    target_eins: set[str], indexes: dict[int, pd.DataFrame]
) -> dict[str, tuple[int, str, str]]:
    """For each EIN, find its most-recent (year, OBJECT_ID, XML_BATCH_ID)."""
    lookup: dict[str, tuple[int, str, str]] = {}
    for year in sorted(indexes, reverse=True):
        idx = indexes[year]
        subset = idx[
            idx["EIN"].isin(target_eins)
            & idx["RETURN_TYPE"].isin(["990", "990EZ"])
        ]
        for _, row in subset.iterrows():
            ein = row["EIN"]
            if ein not in lookup:
                lookup[ein] = (year, row["OBJECT_ID"], row["XML_BATCH_ID"])
    return lookup


def _first_text(root: ET.Element, tags: tuple[str, ...]) -> str | None:
    for tag in tags:
        el = root.find(f".//e:{tag}", NS)
        if el is not None and el.text:
            return el.text.strip()
    return None


def parse_xml(xml_text: str) -> dict:
    """Pull contact + officer fields from a 990 / 990-EZ XML, schema-version-agnostic."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    return {
        "phone": _first_text(root, ("PhoneNum", "Phone")),
        "website": _first_text(
            root, ("WebsiteAddressTxt", "WebSiteAddressTxt", "WebsiteAddress")
        ),
        "principal_officer_name": _first_text(
            root, ("PrincipalOfficerNm", "PrincipalOfficerName")
        ),
        "mission": _first_text(root, ("MissionDesc", "ActivityOrMissionDesc")),
    }


def _read_cached(ein: str) -> dict | None:
    p = XML_CACHE / f"{ein}.xml"
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8", errors="replace")
    return parse_xml(text)


def enrich_eins(
    eins: Iterable[str],
    *,
    years: Iterable[int] = DEFAULT_YEARS,
    log: ProgressFn | None = None,
    progress: StatusFn | None = None,
    fetch_remote: bool = True,
) -> dict[str, dict]:
    """
    Look up website / phone / mission / officer for the given EINs.

    Strategy:
      1. Serve everything we can from the on-disk per-EIN XML cache (no network).
      2. For the rest, optionally consult IRS indexes and pull XMLs out of the
         remote batch ZIPs via Range requests. Set fetch_remote=False to skip
         all network I/O (cache-only mode).

    `log` receives free-form text lines (used by the CLI's tqdm).
    `progress` receives structured dicts {stage, current, total, message} that
    a UI can render as a progress bar. Stages, in order:
        scanning_cache, loading_indexes, building_lookup,
        fetching_batches, done.

    Returns dict keyed by 9-digit EIN string. Missing EINs are absent.
    """
    def _emit(stage: str, message: str, current: int = 0, total: int = 0) -> None:
        if progress is not None:
            progress({"stage": stage, "current": current, "total": total, "message": message})

    target_eins: set[str] = {_ein9(e) for e in eins if e}
    target_eins.discard("000000000")
    if not target_eins:
        _emit("done", "no EINs to look up")
        return {}

    _emit("scanning_cache", "scanning local cache...")
    parsed_by_ein: dict[str, dict] = {}
    uncached: set[str] = set()
    for ein in target_eins:
        cached = _read_cached(ein)
        if cached is not None:
            parsed_by_ein[ein] = cached
        else:
            uncached.add(ein)

    cache_msg = (
        f"{len(parsed_by_ein):,} cached / "
        f"{len(uncached):,} need fetching (of {len(target_eins):,} total)"
    )
    if log:
        log(cache_msg)
    _emit("scanning_cache", cache_msg, current=len(parsed_by_ein), total=len(target_eins))

    if not uncached or not fetch_remote:
        _emit("done", "complete (cache only)")
        return parsed_by_ein

    years_list = list(years)
    _emit("loading_indexes", "loading IRS 990 filing indexes...", current=0, total=len(years_list))
    indexes: dict[int, pd.DataFrame] = {}
    for i, y in enumerate(years_list, 1):
        _emit("loading_indexes", f"loading index_{y}.csv...", current=i - 1, total=len(years_list))
        idx = load_year_index(y)
        if idx is None:
            if log:
                log(f"index_{y}.csv not yet published — skipping")
            continue
        if "XML_BATCH_ID" not in idx.columns:
            if log:
                log(f"index_{y}: lacks XML_BATCH_ID — skipping (legacy format)")
            continue
        indexes[y] = idx
        if log:
            log(f"index_{y}: {len(idx):,} filings")
    _emit("loading_indexes", f"loaded {len(indexes)} indexes", current=len(years_list), total=len(years_list))

    if not indexes:
        if log:
            log("no IRS indexes available — returning cached results only")
        _emit("done", "no IRS indexes available")
        return parsed_by_ein

    _emit("building_lookup", "building EIN → batch lookup...")
    if log:
        log("building EIN → batch lookup...")
    lookup = build_ein_lookup(uncached, indexes)
    lookup_msg = f"matched {len(lookup):,} of {len(uncached):,} uncached EINs to a 990 filing"
    if log:
        log(lookup_msg)
    _emit("building_lookup", lookup_msg)

    by_batch: dict[tuple[int, str], list[tuple[str, str]]] = {}
    for ein, (year, object_id, batch_id) in lookup.items():
        by_batch.setdefault((year, batch_id), []).append((ein, object_id))
    total_batches = len(by_batch)
    if log:
        log(f"spans {total_batches} batch ZIPs")
    _emit("fetching_batches", f"fetching {total_batches} batch ZIPs", current=0, total=total_batches)

    for i, ((year, batch_id), members) in enumerate(by_batch.items(), 1):
        url = BATCH_URL.format(year=year, batch_id=batch_id.upper())
        if log:
            log(f"batch {i}/{total_batches}: {batch_id} ({len(members)} members)")
        _emit(
            "fetching_batches",
            f"batch {i}/{total_batches}: {batch_id} ({len(members)} EINs)",
            current=i - 1,
            total=total_batches,
        )

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with RemoteZip(url) as z:
                    for ein, object_id in members:
                        cache_path = XML_CACHE / f"{ein}.xml"
                        if cache_path.exists():
                            continue
                        try:
                            data = z.read(f"{object_id}_public.xml")
                        except KeyError:
                            continue
                        xml_text = data.decode("utf-8", errors="replace").lstrip("﻿")
                        cache_path.write_text(xml_text, encoding="utf-8")
                        parsed = parse_xml(xml_text)
                        parsed["latest_990_year"] = year
                        parsed_by_ein[ein] = parsed
                last_err = None
                break
            except (RemoteIOError, requests.RequestException, ConnectionError) as e:
                last_err = e
                wait = 2 ** attempt
                if log:
                    log(f"  net error on {batch_id} (attempt {attempt + 1}/3): {e} — retry in {wait}s")
                time.sleep(wait)

        if last_err is not None and log:
            log(f"  giving up on {batch_id} after 3 attempts")

    # Attach latest_990_year for the ones served from cache too.
    for ein, info in lookup.items():
        if ein in parsed_by_ein and "latest_990_year" not in parsed_by_ein[ein]:
            parsed_by_ein[ein]["latest_990_year"] = info[0]

    _emit(
        "done",
        f"complete: {len(parsed_by_ein):,} EINs enriched",
        current=total_batches,
        total=total_batches,
    )
    return parsed_by_ein


__all__ = [
    "DEFAULT_YEARS",
    "XML_CACHE",
    "INDEX_CACHE",
    "load_indexes",
    "build_ein_lookup",
    "parse_xml",
    "enrich_eins",
]
