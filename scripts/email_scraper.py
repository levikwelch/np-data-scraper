"""email_scraper.py - extract contact emails from business websites.

Two-phase pipeline:
  1. scrape_website_emails(url)            - direct HTTP, fast.
  2. scrape_emails_playwright([urls])      - headless Chromium, fallback.

Phase 1 covers ~70-90% of small-business sites where the email is in the
static HTML of the homepage or a /contact page. Phase 2 renders the page in
real Chromium and is only worth running on sites Phase 1 returned [] for
(Wix, Squarespace, React, Cloudflare email-protection, etc.).

Neither public function raises. Unreachable hosts, TLS errors, 4xx/5xx, read
timeouts, and Playwright crashes all silently map to an empty list for that
site.
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


# --- regexes --------------------------------------------------------------
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# $-anchored: matches at the END of the email. Filters auto-reply addresses,
# tracking-service stubs (sentry.io, wixpress.com), the example.com placeholder,
# AND image filenames the email regex falsely picks up (logo@2x.png etc.).
JUNK_PATTERNS = re.compile(
    r"(noreply|no-reply|mailer-daemon"
    r"|example\.com|sentry\.|wixpress\.com"
    r"|\.png|\.jpg|\.jpeg|\.gif|\.svg|\.webp|\.css|\.js)$",
    re.IGNORECASE,
)

FAKE_EMAIL_PATTERNS = re.compile(
    r"^(yourname|youremail|your\.name|your\.email|your|email|username|"
    r"someone|sample|placeholder|changeme|firstname|lastname|firstlast|"
    r"john@doe|jane@doe|john\.doe|jane\.doe|test|demo|lorem|ipsum|asdf|xxx|"
    r"abc@|name@|user@)",
    re.IGNORECASE,
)
FAKE_EMAIL_DOMAINS = re.compile(
    r"@(example\.(com|org|net)|test\.com|domain\.com|email\.com|sample\.com|"
    r"placeholder\.com|yourdomain\.com|yoursite\.com|yourcompany\.com|"
    r"mydomain\.com|mysite\.com|website\.com|company\.com|"
    r"lorem\.com|ipsum\.com)$",
    re.IGNORECASE,
)

GENERIC_PREFIXES = {
    "info", "admin", "contact", "support", "help", "hello", "office", "sales",
    "billing", "accounts", "enquiries", "inquiries", "team", "careers", "jobs",
    "hr", "marketing", "press", "media", "legal", "compliance", "privacy",
    "abuse", "webmaster", "postmaster", "hostmaster", "noreply", "no-reply",
    "feedback", "general", "reception", "service", "customerservice",
    "bookings", "reservations", "appointments", "scheduling",
}

# --- config ---------------------------------------------------------------
SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}
CONTACT_PATHS = ["", "/contact", "/contact-us", "/about", "/about-us"]
# Phase 2 runs sites serially within one Chromium session. Playwright's sync
# API is not thread-safe (greenlet dispatcher is bound to the launching thread),
# so cross-site ThreadPoolExecutor concurrency on a shared context isn't safe.
# The early-break-on-first-hit per site is the real speedup anyway.


# --- shared HTTP session --------------------------------------------------
def _build_session() -> requests.Session:
    sess = requests.Session()
    adapter = HTTPAdapter(pool_connections=50, pool_maxsize=500)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    return sess


_session = _build_session()
_session_lock = threading.Lock()  # guards the module-level reference only


# --- internal helpers -----------------------------------------------------
def _clean_matches(html_text: str) -> list[str]:
    """Apply EMAIL_RE, drop junk, lowercase, preserve discovery order."""
    matches = EMAIL_RE.findall(html_text)
    cleaned = [e.lower() for e in matches if not JUNK_PATTERNS.search(e)]
    return list(dict.fromkeys(cleaned))


def _fetch_emails_from_url(url: str) -> list[str]:
    """Fetch one URL and return cleaned emails in discovery order. Never raises."""
    try:
        resp = _session.get(
            url,
            headers=SCRAPE_HEADERS,
            timeout=(5, 10),
            allow_redirects=True,
        )
    except Exception:
        return []
    if resp.status_code != 200:
        return []
    try:
        return _clean_matches(resp.text)
    except Exception:
        return []


# --- public API: Phase 1 --------------------------------------------------
def scrape_website_emails(website: str) -> list[str]:
    """Phase 1: direct HTTP. Returns lowercased, deduplicated emails in
    discovery order. Empty list on any failure. Never raises."""
    if not website:
        return []
    try:
        if not website.startswith("http"):
            website = f"https://{website}"
        parsed = urlparse(website)
        if not parsed.netloc:
            return []
        base = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return []

    # Wave 1: just the root. Most sites that have emails have them here.
    found: list[str] = []
    try:
        found.extend(_fetch_emails_from_url(base + CONTACT_PATHS[0]))
    except Exception:
        pass
    if found:
        return list(dict.fromkeys(found))

    # Wave 2: the remaining contact-ish paths, in parallel.
    remaining = CONTACT_PATHS[1:]
    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(_fetch_emails_from_url, base + p): p
                       for p in remaining}
            # Iterate in submission order so we keep deterministic discovery
            # order across paths (contact > contact-us > about > about-us).
            ordered = sorted(futures.items(), key=lambda kv: remaining.index(kv[1]))
            for fut, _path in ordered:
                try:
                    found.extend(fut.result())
                except Exception:
                    continue
    except Exception:
        return list(dict.fromkeys(found))

    return list(dict.fromkeys(found))


# --- public API: Phase 2 --------------------------------------------------
def _scrape_one_playwright(context, website: str) -> tuple[str, list[str]]:
    """Render a single site's pages in Chromium and extract emails. Returns
    (website, emails). Early-exits on the first path that yields anything."""
    try:
        if not website.startswith("http"):
            url = f"https://{website}"
        else:
            url = website
        parsed = urlparse(url)
        if not parsed.netloc:
            return website, []
        base = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return website, []

    found: list[str] = []
    for path in CONTACT_PATHS:
        page = None
        try:
            page = context.new_page()
            page.goto(base + path, wait_until="domcontentloaded", timeout=15000)
            html = page.content()
            found.extend(_clean_matches(html))
        except Exception:
            pass
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
        if found:
            break  # one hit is enough; Phase 2 is expensive

    return website, list(dict.fromkeys(found))


def scrape_emails_playwright(websites: list[str]) -> dict[str, list[str]]:
    """Phase 2: JS-rendered fallback. Pass in the sites that returned []
    from Phase 1. Sites that still produce nothing map to []. Never raises."""
    if not HAS_PLAYWRIGHT or not websites:
        return {}

    results: dict[str, list[str]] = {w: [] for w in websites}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=SCRAPE_HEADERS["User-Agent"],
                    ignore_https_errors=True,
                )
                context.set_default_timeout(15000)
                try:
                    for site in websites:
                        try:
                            _, emails = _scrape_one_playwright(context, site)
                            results[site] = emails
                        except Exception:
                            results[site] = []
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception:
        pass  # browser crashed; return whatever we already accumulated

    return results


# --- post-processing helpers ---------------------------------------------
def is_fake_email(email: str) -> bool:
    """True if this looks like a placeholder pasted into a template that
    nobody updated (e.g. 'yourname@example.com', '12345@gmail.com')."""
    if not email:
        return True
    e = email.strip().lower()
    if FAKE_EMAIL_PATTERNS.search(e):
        return True
    if FAKE_EMAIL_DOMAINS.search(e):
        return True
    local = e.split("@")[0]
    if re.fullmatch(r"\d+", local):
        return True
    return False


def _generic_base(local: str) -> str:
    """Strip trailing digits so 'info2' and 'contact01' compare as 'info'/'contact'
    against GENERIC_PREFIXES. Spec's prose example: 'digits often mean
    automation, e.g. info2@, contact01@'."""
    return re.sub(r"\d+$", "", local)


def is_personal_email(email: str) -> bool:
    """True if the local part isn't one of the well-known role prefixes."""
    if not email:
        return False
    local = email.strip().lower().split("@")[0]
    return _generic_base(local) not in GENERIC_PREFIXES


def email_realness_score(email: str) -> tuple:
    """Sort key - descending sort puts the most likely real personal email
    first. Returns (personal_flag, no_digits_flag, -len(local))."""
    local = email.split("@")[0].lower()
    personal = 1 if _generic_base(local) not in GENERIC_PREFIXES else 0
    no_digits = 1 if not any(c.isdigit() for c in local) else 0
    return (personal, no_digits, -len(local))
