#!/usr/bin/env python3
"""
Libgen downloader — built for GitHub Actions.

Best inputs (BOOK_URL):
  ee012119554cf6f9a2a4fd5662de8d17          ← bare MD5, most reliable
  https://libgen.li/ads.php?md5=<md5>       ← ads page
  https://library.lol/main/<md5>            ← library.lol page

How Libgen download actually works (two hops):
  1. ads.php?md5=X  →  find link to library.lol/main/X
  2. library.lol/main/X  →  find the GET button  →  actual file on cdn/get.php
"""

import concurrent.futures
import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry

# ──────────────────────────────────────────────────────────────── config ──────

CACHE_DIR  = Path(".download_cache")
CACHE_FILE = CACHE_DIR / "downloads.json"

MAX_RETRIES   = 8          # enough, not excessive
CHUNK_SIZE    = 512 * 1024 # 512 KB
TIMEOUT       = (10, 60)   # connect, read
STALL_TIMEOUT = 90         # seconds with no bytes
BASE_BACKOFF  = 5
MAX_BACKOFF   = 60
MAX_FILE_MB   = 1900

LIBGEN_MIRRORS = [
    "https://libgen.is",
    "https://libgen.rs",
    "https://libgen.st",
    "https://libgen.li",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

BOOK_EXTS = (
    ".epub", ".pdf", ".mobi", ".azw3", ".azw",
    ".djvu", ".fb2", ".cbz", ".cbr", ".lit", ".lrf",
)

CACHE_DIR.mkdir(exist_ok=True)

# ──────────────────────────────────────────────────────────────── session ─────

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection":      "keep-alive",
    })
    retry = Retry(
        total=3, connect=3, read=2,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    a = HTTPAdapter(max_retries=retry, pool_connections=2, pool_maxsize=4)
    s.mount("https://", a)
    s.mount("http://",  a)
    return s


SESSION = make_session()

# ──────────────────────────────────────────────────────────────── cache ───────

def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    except Exception:
        return {}

def save_cache(data: dict) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"  ⚠ cache write: {e}")

# ──────────────────────────────────────────────────────────────── helpers ─────

def fmt(n: float) -> str:
    for u in ("B","KB","MB","GB"):
        if n < 1024: return f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} TB"

def file_md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(4*1024*1024), b""):
            h.update(c)
    return h.hexdigest().lower()

def is_md5(s: str) -> bool:
    return bool(re.fullmatch(r"[a-fA-F0-9]{32}", s.strip()))

def md5_from_url(url: str) -> Optional[str]:
    qs = parse_qs(urlparse(url).query)
    for k in ("md5","MD5"):
        if k in qs and is_md5(qs[k][0]):
            return qs[k][0].lower()
    for part in urlparse(url).path.split("/"):
        if is_md5(part):
            return part.lower()
    m = re.search(r"[=/_-]([a-fA-F0-9]{32})(?:[&/?]|$)", url)
    return m.group(1).lower() if m else None

def sanitize(name: str) -> str:
    name = unquote(name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")
    if len(name) > 180:
        stem, ext = os.path.splitext(name)
        name = stem[:180-len(ext)] + ext
    return name or f"book_{int(time.time())}"

def fname_headers(r: requests.Response) -> Optional[str]:
    cd = r.headers.get("content-disposition","")
    m = re.search(r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)", cd, re.I)
    if m: return sanitize(m.group(1).strip().strip('"\''))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    if m: return sanitize(m.group(1).strip())
    return None

def fname_url(url: str) -> str:
    name = os.path.basename(unquote(urlparse(url).path))
    if name and "." in name and not name.startswith("."):
        return sanitize(name)
    return f"book_{int(time.time())}.epub"

def is_html_bytes(b: bytes) -> bool:
    s = b[:256].lower()
    return any(m in s for m in (b"<!doctype",b"<html",b"<head>"))

def is_binary_ct(ct: str) -> bool:
    ct = ct.lower()
    return any(x in ct for x in (
        "epub","pdf","octet-stream","zip","mobipocket",
        "mobi8","amazon","fictionbook","djvu",
    ))

def dbg(msg: str) -> None:
    """Print with timestamp for debugging slow runs."""
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ──────────────────────────────────────────────────────── page fetching ───────

def get_page(url: str, label: str = "") -> tuple[str, BeautifulSoup]:
    """
    Fetch a page and return (final_url, soup).
    Raises on non-200 or if response looks like a blank/error page.
    """
    dbg(f"GET {label or url[:80]}")
    r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
    dbg(f"  → HTTP {r.status_code}  {len(r.text)} chars  url={r.url[:80]}")
    r.raise_for_status()
    if len(r.text) < 200:
        raise RuntimeError(f"Response too short ({len(r.text)} chars) from {url}")
    return r.url, BeautifulSoup(r.text, "lxml")

# ──────────────────────────────────────────────────── link extraction ─────────

def extract_md5_from_page(soup: BeautifulSoup, html: str) -> Optional[str]:
    # Look for 32-char hex in table cells / links
    for tag in soup.find_all(["td","th","a","span","div","p"]):
        t = tag.get_text(strip=True)
        if is_md5(t):
            return t.lower()
    # labeled in attributes or text
    m = re.search(r'(?:md5|MD5)\s*[=:]\s*["\']?([a-fA-F0-9]{32})', html)
    return m.group(1).lower() if m else None


def extract_download_link(soup: BeautifulSoup, base_url: str, html: str) -> Optional[str]:
    """
    Multi-strategy extraction — returns the best direct download link or None.
    Prints every candidate it considers so we can debug.
    """
    dbg(f"  Scanning page for download link  (base={base_url[:60]})")

    # Strategy 1: explicit GET / DOWNLOAD text on <a>
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).upper()
        href = a["href"]
        if text in {"GET","DOWNLOAD","TÉLÉCHARGER","DESCARGAR","СКАЧАТЬ"}:
            link = urljoin(base_url, href)
            dbg(f"  ✓ [S1-text] {link[:80]}")
            return link
        if "get.php" in href.lower():
            link = urljoin(base_url, href)
            dbg(f"  ✓ [S1-getphp] {link[:80]}")
            return link

    # Strategy 2: href ends with book extension
    for a in soup.find_all("a", href=True):
        clean = a["href"].split("?")[0].lower()
        if any(clean.endswith(e) for e in BOOK_EXTS):
            link = urljoin(base_url, a["href"])
            dbg(f"  ✓ [S2-ext] {link[:80]}")
            return link

    # Strategy 3: CDN / booksdl / library.lol anywhere in href
    cdn_kw = ("booksdl","cdn1","cdn2","cdn3","cdn4","library.lol",
               "cloudflare-ipfs","ipfs.io")
    for a in soup.find_all("a", href=True):
        if any(k in a["href"].lower() for k in cdn_kw):
            link = urljoin(base_url, a["href"])
            dbg(f"  ✓ [S3-cdn] {link[:80]}")
            return link

    # Strategy 4: regex over raw HTML for any URL ending in book ext
    for m in re.findall(r'https?://[^\s"\'<>]{10,}', html):
        if any(m.lower().split("?")[0].endswith(e) for e in BOOK_EXTS):
            dbg(f"  ✓ [S4-regex-ext] {m[:80]}")
            return m
        if "/get.php" in m.lower() or "booksdl" in m.lower():
            dbg(f"  ✓ [S4-regex-cdn] {m[:80]}")
            return m

    # Log all <a> hrefs we saw so we can debug
    all_hrefs = [a["href"] for a in soup.find_all("a", href=True)]
    dbg(f"  ✗ No link found. All hrefs ({len(all_hrefs)}):")
    for h in all_hrefs[:30]:
        dbg(f"       {h}")
    return None

# ──────────────────────────────────────────────────── mirror probing ──────────

def _probe_mirror(mirror: str, md5: str) -> Optional[str]:
    url = f"{mirror}/ads.php?md5={md5}"
    try:
        r = SESSION.get(url, timeout=(6, 12), allow_redirects=True)
        if r.status_code == 200 and len(r.text) > 300:
            dbg(f"  mirror ok: {mirror}  ({len(r.text)} chars)")
            return url
        dbg(f"  mirror bad: {mirror}  status={r.status_code}  len={len(r.text)}")
    except Exception as e:
        dbg(f"  mirror fail: {mirror}  {e}")
    return None

def best_mirror_ads_url(md5: str) -> str:
    dbg("Probing mirrors in parallel...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(LIBGEN_MIRRORS)) as ex:
        futs = {ex.submit(_probe_mirror, m, md5): m for m in LIBGEN_MIRRORS}
        for fut in concurrent.futures.as_completed(futs):
            result = fut.result()
            if result:
                for f in futs: f.cancel()
                return result
    # All failed — go straight to library.lol
    fallback = f"https://library.lol/main/{md5}"
    dbg(f"All mirrors failed → {fallback}")
    return fallback

# ──────────────────────────────────────────────────── resolution ──────────────

def resolve_to_download_url(raw: str) -> tuple[str, Optional[str]]:
    """
    Turn any supported input into (direct_download_url, md5).

    The full chain for a bare MD5:
      MD5
        → best ads.php?md5=MD5   (mirror probe)
        → find library.lol/main/MD5 link on that page
        → fetch library.lol/main/MD5
        → find GET button → actual cdn get.php URL  ← real file
    """
    raw = raw.strip()

    # ── bare MD5 ──────────────────────────────────────────────────────────────
    if is_md5(raw):
        dbg(f"Input is bare MD5: {raw}")
        return _resolve_from_md5(raw.lower())

    parsed = urlparse(raw)
    host   = parsed.netloc.lower()
    path   = parsed.path.lower()

    # ── ads.php?md5= ──────────────────────────────────────────────────────────
    if "ads.php" in path:
        md5 = md5_from_url(raw)
        if md5:
            dbg(f"Input is ads.php, extracted MD5: {md5}")
            return _resolve_from_md5(md5)
        # no md5 in URL — just parse the page
        final_url, soup = get_page(raw, "ads page")
        return _resolve_from_ads_page(final_url, soup, raw.text, None)

    # ── library.lol/main/<md5> ────────────────────────────────────────────────
    if "library.lol" in host and "/main/" in path:
        md5 = md5_from_url(raw)
        dbg(f"Input is library.lol page, MD5={md5}")
        return _resolve_from_lol_page(raw, md5)

    # ── edition.php ───────────────────────────────────────────────────────────
    if "edition.php" in path:
        dbg("Input is edition.php page")
        final_url, soup = get_page(raw, "edition page")
        html = str(soup)
        md5  = extract_md5_from_page(soup, html)
        link = extract_download_link(soup, final_url, html)
        if link:
            return link, md5
        if md5:
            return _resolve_from_md5(md5)
        raise RuntimeError(f"Nothing usable on edition page: {raw}")

    # ── direct get.php or file URL ────────────────────────────────────────────
    if "get.php" in path or any(path.endswith(e) for e in BOOK_EXTS):
        md5 = md5_from_url(raw)
        dbg(f"Input looks like direct URL, MD5={md5}")
        # Validate it — keys expire fast
        try:
            with SESSION.get(raw, stream=True, timeout=(8,20)) as r:
                head = next(r.iter_content(512), b"")
                ct   = r.headers.get("content-type","")
                dbg(f"  Direct probe: HTTP {r.status_code}  CT={ct}  "
                    f"html={is_html_bytes(head)}")
                if r.status_code == 200 and is_binary_ct(ct) and not is_html_bytes(head):
                    dbg("  ✓ Direct URL is live")
                    return r.url, md5 or md5_from_url(r.url)
        except Exception as e:
            dbg(f"  Direct probe error: {e}")
        # Key expired → re-resolve
        if md5:
            dbg(f"  Falling back to MD5 resolution: {md5}")
            return _resolve_from_md5(md5)
        raise RuntimeError(f"Direct URL failed and no MD5 found: {raw}")

    # ── generic fallback ──────────────────────────────────────────────────────
    dbg(f"Unknown URL pattern, attempting generic parse: {raw[:80]}")
    md5 = md5_from_url(raw)
    if md5:
        return _resolve_from_md5(md5)
    final_url, soup = get_page(raw, "unknown page")
    html = str(soup)
    page_md5 = extract_md5_from_page(soup, html)
    link     = extract_download_link(soup, final_url, html)
    if link:
        return link, page_md5
    if page_md5:
        return _resolve_from_md5(page_md5)
    raise RuntimeError(f"Cannot resolve: {raw}")


def _resolve_from_md5(md5: str) -> tuple[str, str]:
    """Full two-hop resolution: MD5 → ads page → lol page → get.php file."""

    # Hop 1: find best ads.php page
    ads_url = best_mirror_ads_url(md5)

    # If we landed on library.lol directly, skip to hop 2
    if "library.lol/main/" in ads_url:
        return _resolve_from_lol_page(ads_url, md5)

    # Parse the ads page to find a library.lol link
    try:
        final_url, soup = get_page(ads_url, "ads page")
        html = str(soup)
    except Exception as e:
        dbg(f"ads page failed: {e} — going straight to library.lol")
        return _resolve_from_lol_page(f"https://library.lol/main/{md5}", md5)

    # Look for library.lol/main link specifically on the ads page
    lol_link = None
    for a in soup.find_all("a", href=True):
        if "library.lol/main" in a["href"] or "library.lol" in a["href"]:
            lol_link = urljoin(final_url, a["href"])
            dbg(f"  Found library.lol link on ads page: {lol_link[:80]}")
            break

    if lol_link:
        return _resolve_from_lol_page(lol_link, md5)

    # Ads page might have a direct download link already
    link = extract_download_link(soup, final_url, html)
    if link:
        return link, md5

    # Last resort: go straight to library.lol
    dbg("No link on ads page, trying library.lol directly")
    return _resolve_from_lol_page(f"https://library.lol/main/{md5}", md5)


def _resolve_from_lol_page(url: str, md5: Optional[str]) -> tuple[str, Optional[str]]:
    """
    Fetch a library.lol/main/<md5> page and extract the GET button link.
    This is always the final hop before the actual file.
    """
    dbg(f"Fetching library.lol page: {url[:80]}")
    try:
        final_url, soup = get_page(url, "library.lol")
    except Exception as e:
        raise RuntimeError(f"library.lol unreachable: {e}") from e

    html = str(soup)
    link = extract_download_link(soup, final_url, html)
    if link:
        dbg(f"  ✓ Got final download link: {link[:80]}")
        return link, md5

    raise RuntimeError(
        f"No download link found on library.lol page. "
        f"Page length: {len(html)} chars. "
        f"URL: {url}"
    )

# ──────────────────────────────────────────────────── stall detection ─────────

class Stall:
    def __init__(self, timeout: int, grace: int = 20):
        self._t   = timeout
        self._g   = grace
        self._s   = time.monotonic()
        self._l   = time.monotonic()
        self._tot = 0

    def tick(self, n: int) -> None:
        self._tot += n
        self._l    = time.monotonic()

    def check(self) -> None:
        idle    = time.monotonic() - self._l
        elapsed = time.monotonic() - self._s
        limit   = self._t if elapsed > self._g else self._t * 2
        if idle > limit:
            raise TimeoutError(f"Stalled: {idle:.0f}s silence, {fmt(self._tot)} received")

# ──────────────────────────────────────────────────── download ────────────────

def _range_supported(url: str) -> bool:
    try:
        r = SESSION.head(url, timeout=(5,8), allow_redirects=True)
        ok = r.headers.get("accept-ranges","").lower() == "bytes"
        dbg(f"  Range support: {ok}  (status={r.status_code})")
        return ok
    except Exception as e:
        dbg(f"  Range HEAD failed: {e}")
        return False


def download_file(url: str, out_dir: Path, expected_md5: Optional[str]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    uid  = hashlib.sha1(url.encode()).hexdigest()[:16]
    tmp  = out_dir / f".{uid}.part"
    meta = out_dir / f".{uid}.meta"

    existing  = tmp.stat().st_size if tmp.exists() else 0
    resumable = existing > 0 and _range_supported(url)

    if existing > 0 and not resumable:
        dbg(f"  No range support — discarding {fmt(existing)}")
        tmp.unlink(missing_ok=True)
        existing = 0

    hdrs: dict[str,str] = {}
    if resumable:
        hdrs["Range"] = f"bytes={existing}-"
        dbg(f"  Resuming from {fmt(existing)}")

    sess = make_session()
    dbg(f"  Opening stream: {url[:80]}")
    resp = sess.get(url, stream=True, timeout=TIMEOUT,
                    headers=hdrs, allow_redirects=True)
    dbg(f"  Stream opened: HTTP {resp.status_code}  "
        f"CT={resp.headers.get('content-type','?')}  "
        f"CL={resp.headers.get('content-length','?')}")

    if resp.status_code == 416:
        dbg("  416 — restarting")
        resp.close()
        tmp.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        return download_file(url, out_dir, expected_md5)

    if resp.status_code not in (200, 206):
        resp.close()
        raise RuntimeError(f"HTTP {resp.status_code} from {url[:80]}")

    # Validate resume offset
    if resp.status_code == 206:
        cr = resp.headers.get("content-range","")
        m  = re.search(r"bytes (\d+)-", cr)
        if m and int(m.group(1)) != existing:
            dbg(f"  Wrong resume offset in Content-Range: {cr}")
            resp.close()
            tmp.unlink(missing_ok=True)
            return download_file(url, out_dir, expected_md5)

    cl    = int(resp.headers.get("content-length", 0))
    total = (cl + existing) if resp.status_code == 206 else cl

    # Resolve filename
    fname = (
        fname_headers(resp)
        or (meta.read_text().strip() if meta.exists() else None)
        or fname_url(resp.url)
    )
    meta.write_text(fname)
    dest = out_dir / fname

    dbg(f"  Filename: {fname}")
    dbg(f"  Total size: {fmt(total) if total else 'unknown'}")

    if total and total > MAX_FILE_MB * 1024 * 1024:
        dbg(f"  ⚠ File > {MAX_FILE_MB} MB")

    stall      = Stall(STALL_TIMEOUT)
    first_seen = False
    last_log   = time.monotonic()

    with open(tmp, "ab" if existing else "wb") as fh, tqdm(
        total=total or None, initial=existing,
        unit="B", unit_scale=True, unit_divisor=1024,
        ascii=True, dynamic_ncols=True, desc="  ↓",
    ) as bar:
        try:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                if not first_seen:
                    first_seen = True
                    dbg(f"  First bytes: {chunk[:16].hex()}  html={is_html_bytes(chunk)}")
                    if is_html_bytes(chunk):
                        resp.close()
                        raise RuntimeError(
                            "Server sent an HTML page instead of the file. "
                            "Likely a CAPTCHA or expired session."
                        )
                fh.write(chunk)
                bar.update(len(chunk))
                stall.tick(len(chunk))
                stall.check()
                # Log progress every 30s for long downloads
                if time.monotonic() - last_log > 30:
                    written = tmp.stat().st_size
                    dbg(f"  Progress: {fmt(written)}" +
                        (f" / {fmt(total)}" if total else ""))
                    last_log = time.monotonic()
        finally:
            resp.close()

    actual = tmp.stat().st_size
    dbg(f"  Download complete: {fmt(actual)}")

    if total and actual < total:
        raise IOError(f"Incomplete: {fmt(actual)} of {fmt(total)}")

    if expected_md5:
        dbg("  Verifying MD5...")
        got = file_md5(tmp)
        if got != expected_md5.lower():
            tmp.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            raise IOError(f"MD5 mismatch: expected={expected_md5} got={got}")
        dbg(f"  ✓ MD5 OK: {got}")
    else:
        dbg("  Skipping MD5 check (none provided)")

    if dest.exists():
        dest.unlink()
    tmp.rename(dest)
    meta.unlink(missing_ok=True)
    return dest

# ──────────────────────────────────────────────────── retry loop ──────────────

def _fallback_urls(primary: str, md5: Optional[str]) -> list[str]:
    """
    Build a list of fallback URLs to cycle through on failure.
    Always includes library.lol and multiple CDN hosts.
    """
    urls = [primary]
    t = md5 or md5_from_url(primary)
    if t:
        candidates = [
            f"https://library.lol/main/{t}",
            f"https://libgen.li/ads.php?md5={t}",
        ]
        for h in [
            "https://cdn1.booksdl.org",
            "https://cdn2.booksdl.org",
            "https://cdn3.booksdl.org",
        ]:
            candidates.append(f"{h}/main/{t}")
        for c in candidates:
            if c not in urls:
                urls.append(c)
    return urls


def run_download(raw_url: str, out_dir: Path, md5: Optional[str]) -> Path:
    """
    Resolve the URL then download with retries and fallback URL cycling.
    """
    dbg(f"Resolving: {raw_url[:80]}")
    try:
        dl_url, page_md5 = resolve_to_download_url(raw_url)
    except Exception as e:
        dbg(f"Resolution failed: {e}")
        raise RuntimeError(f"Could not resolve download URL: {e}") from e

    dbg(f"Resolved to: {dl_url[:80]}")

    final_md5 = (
        md5
        or page_md5
        or (raw_url.strip().lower() if is_md5(raw_url) else None)
        or md5_from_url(raw_url)
        or md5_from_url(dl_url)
    )
    if final_md5:
        dbg(f"Will verify MD5: {final_md5}")

    fallbacks = _fallback_urls(dl_url, final_md5)
    last_err: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        # Use primary for first 2 attempts, then cycle fallbacks
        if attempt <= 2:
            url = dl_url
        else:
            # Re-resolve on attempt 3 in case the link was stale
            if attempt == 3 and final_md5:
                try:
                    dbg("Re-resolving URL (attempt 3)...")
                    dl_url, _ = resolve_to_download_url(
                        final_md5 if is_md5(raw_url) else raw_url
                    )
                    fallbacks = _fallback_urls(dl_url, final_md5)
                    dbg(f"  Re-resolved: {dl_url[:80]}")
                except Exception as e:
                    dbg(f"  Re-resolve failed: {e}")
            url = fallbacks[(attempt - 3) % len(fallbacks)]

        print(f"\n{'━'*55}")
        print(f"  Attempt {attempt}/{MAX_RETRIES}")
        dbg(f"  URL: {url[:80]}")

        try:
            return download_file(url, out_dir, final_md5)
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.Timeout,
            TimeoutError,
            IOError,
        ) as e:
            last_err = e
            dbg(f"  ✗ {type(e).__name__}: {e}")
        except RuntimeError as e:
            last_err = e
            dbg(f"  ✗ RuntimeError: {e}")
            # If it's an HTML/CAPTCHA error, try a different URL next
        except Exception as e:
            last_err = e
            dbg(f"  ✗ {type(e).__name__}: {e}")

        if attempt < MAX_RETRIES:
            wait = min(BASE_BACKOFF * 2**(attempt-1), MAX_BACKOFF)
            wait *= random.uniform(0.8, 1.2)
            dbg(f"  Waiting {wait:.1f}s...")
            time.sleep(wait)

    raise RuntimeError(f"All {MAX_RETRIES} attempts failed. Last: {last_err}")

# ──────────────────────────────────────────────────── main ────────────────────

def main() -> int:
    book_url = os.environ.get("BOOK_URL","").strip()
    raw_md5  = os.environ.get("EXPECTED_MD5","").strip().lower()
    out_dir  = Path(os.environ.get("OUTPUT_DIR","downloads").strip())

    if not book_url:
        print("💥 BOOK_URL is required", file=sys.stderr)
        return 1

    user_md5: Optional[str] = raw_md5 if is_md5(raw_md5) else None

    cache = load_cache()
    if book_url in cache:
        p = Path(cache[book_url]["path"])
        if p.exists():
            print(f"✓ Already downloaded: {p}  ({fmt(p.stat().st_size)})")
            return 0
        dbg("Cache stale — re-downloading")
        del cache[book_url]

    print("="*60)
    print(f"📚 Input : {book_url}")
    print(f"🔐 MD5   : {user_md5 or '(none)'}")
    print(f"📁 OutDir: {out_dir.resolve()}")
    print("="*60)

    try:
        path = run_download(book_url, out_dir, user_md5)
    except RuntimeError as e:
        print(f"\n💥 FAILED: {e}", file=sys.stderr)
        return 1

    size     = path.stat().st_size
    checksum = file_md5(path)

    print(f"\n{'='*60}")
    print(f"✅ {path}")
    print(f"   Size: {fmt(size)}")
    print(f"   MD5 : {checksum}")
    print(f"{'='*60}")

    cache[book_url] = {
        "path": str(path), "size": size,
        "md5": checksum, "ts": time.time(),
    }
    save_cache(cache)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n⚠ Interrupted")
        sys.exit(130)
    except Exception as e:
        print(f"\n💥 {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
