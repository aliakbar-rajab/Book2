#!/usr/bin/env python3
"""
Fast, direct-file downloader for GitHub Actions.

Important:
  This script downloads direct file URLs only.

It intentionally does NOT:
  - scrape resolver pages
  - bypass CAPTCHA
  - parse ads.php / edition.php / library.lol pages
  - retry forever when the server returns HTML

If the URL returns HTML, CAPTCHA, Cloudflare, or an expired-key page,
the script fails fast.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import random
import re
import socket
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

CHUNK_SIZE = 1024 * 1024
MAX_RETRIES = 4
READ_TIMEOUT = 60
BASE_BACKOFF = 4
MAX_BACKOFF = 30
MAX_FILE_MB_WARNING = 1900

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

BAD_PAGE_PATHS = (
    "ads.php",
    "edition.php",
    "book/index.php",
)

BAD_FILENAME_EXTS = {
    ".php",
    ".html",
    ".htm",
    ".aspx",
    ".asp",
    ".jsp",
    ".cgi",
}

CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/epub+zip": ".epub",
    "application/zip": ".zip",
    "application/x-mobipocket-ebook": ".mobi",
    "application/x-mobipocket": ".mobi",
    "application/vnd.amazon.ebook": ".azw3",
    "image/vnd.djvu": ".djvu",
    "text/plain": ".txt",
}


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class PermanentDownloadError(Exception):
    """Do not retry. URL is bad, expired, forbidden, HTML page, etc."""


class RetryableDownloadError(Exception):
    """Retry may help. Network drop, timeout, server 5xx, incomplete file, etc."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(msg, flush=True)


def tslog(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def format_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def is_md5(value: str) -> bool:
    return bool(re.fullmatch(r"[a-fA-F0-9]{32}", value.strip()))


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def sanitize_filename(name: str) -> str:
    name = unquote(name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")

    if len(name) > 180:
        stem, ext = os.path.splitext(name)
        name = stem[: 180 - len(ext)] + ext

    return name or f"download_{int(time.time())}.bin"


def content_type(headers) -> str:
    return (headers.get("Content-Type") or "").split(";")[0].strip().lower()


def extension_from_content_type(ct: str) -> str:
    return CONTENT_TYPE_EXTENSIONS.get(ct, ".bin")


def filename_from_headers(headers) -> Optional[str]:
    cd = headers.get("Content-Disposition") or ""
    if not cd:
        return None

    # RFC 5987: filename*=UTF-8''file.pdf
    m = re.search(r"filename\*\s*=\s*([^;]+)", cd, re.IGNORECASE)
    if m:
        value = m.group(1).strip().strip("\"'")
        if "''" in value:
            value = value.split("''", 1)[1]
        return sanitize_filename(value)

    # Standard: filename="file.pdf"
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.IGNORECASE)
    if m:
        return sanitize_filename(m.group(1))

    m = re.search(r"filename\s*=\s*([^;]+)", cd, re.IGNORECASE)
    if m:
        return sanitize_filename(m.group(1).strip().strip("\"'"))

    return None


def filename_from_url(url: str, uid: str, ct: str) -> str:
    parsed = urlparse(url)
    name = os.path.basename(unquote(parsed.path))

    if name and "." in name:
        ext = os.path.splitext(name)[1].lower()
        if ext not in BAD_FILENAME_EXTS:
            return sanitize_filename(name)

    ext = extension_from_content_type(ct)
    return f"download_{uid[:12]}{ext}"


def looks_like_html(data: bytes) -> bool:
    sample = data[:2048].lstrip().lower()

    markers = (
        b"<!doctype html",
        b"<html",
        b"<head",
        b"<body",
        b"<title",
        b"captcha",
        b"cloudflare",
        b"checking your browser",
        b"just a moment",
    )

    return any(marker in sample for marker in markers)


def is_obvious_html_page_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if any(bad in path for bad in BAD_PAGE_PATHS):
        return True

    if "library.lol" in host and "/main/" in path:
        return True

    return False


def validate_direct_url(url: str) -> None:
    if is_md5(url):
        raise PermanentDownloadError(
            "Bare MD5 input is not a direct file URL. "
            "This version only downloads direct file URLs."
        )

    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PermanentDownloadError(
            "BOOK_URL must be a direct http(s) file URL."
        )

    if is_obvious_html_page_url(url):
        raise PermanentDownloadError(
            "This URL is an HTML resolver page, not a direct file URL. "
            "Resolver/CAPTCHA pages are not supported in GitHub Actions."
        )


def parse_total_size(headers, existing: int, status: int) -> int:
    cr = headers.get("Content-Range") or ""
    m = re.search(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", cr, re.IGNORECASE)

    if m and m.group(3) != "*":
        return int(m.group(3))

    cl = headers.get("Content-Length")
    if cl and cl.isdigit():
        length = int(cl)
        return existing + length if status == 206 else length

    return 0


def write_github_output(path: Path) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return

    with open(output, "a", encoding="utf-8") as f:
        f.write(f"file={path.as_posix()}\n")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def base_headers() -> dict[str, str]:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*",
        "Connection": "close",
    }

    referer = os.environ.get("REFERER", "").strip()
    if referer:
        headers["Referer"] = referer

    return headers


def open_response(url: str, headers: dict[str, str]):
    req = Request(url, headers=headers, method="GET")

    try:
        return urlopen(req, timeout=READ_TIMEOUT)

    except HTTPError as e:
        sample = b""
        try:
            sample = e.read(1024)
        except Exception:
            pass

        if e.code == 416:
            raise RetryableDownloadError(
                "HTTP 416 Range Not Satisfiable. Restarting cleanly."
            ) from e

        if e.code in {408, 425, 429} or 500 <= e.code <= 599:
            raise RetryableDownloadError(f"HTTP {e.code}") from e

        msg = f"HTTP {e.code}"
        if looks_like_html(sample):
            msg += " with HTML/error page"

        raise PermanentDownloadError(msg) from e

    except (URLError, socket.timeout, TimeoutError) as e:
        raise RetryableDownloadError(str(e)) from e


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #

def download_once(
    url: str,
    output_dir: Path,
    expected_md5: Optional[str],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    uid = expected_md5.lower() if expected_md5 else hashlib.sha1(url.encode()).hexdigest()
    uid = uid[:32]

    temp_path = output_dir / f".{uid}.part"
    meta_path = output_dir / f".{uid}.meta"

    existing = temp_path.stat().st_size if temp_path.exists() else 0

    headers = base_headers()
    if existing:
        headers["Range"] = f"bytes={existing}-"
        tslog(f"Resuming from {format_size(existing)}")

    tslog(f"Opening URL: {url[:120]}")
    resp = open_response(url, headers)
    status = getattr(resp, "status", resp.getcode())

    # Server ignored Range request; restart from zero.
    if existing and status == 200:
        tslog("Server ignored Range header. Restarting from zero.")
        resp.close()
        temp_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)

        existing = 0
        headers = base_headers()
        resp = open_response(url, headers)
        status = getattr(resp, "status", resp.getcode())

    if status not in {200, 206}:
        resp.close()
        raise RetryableDownloadError(f"Unexpected HTTP status {status}")

    # Validate Content-Range offset.
    if existing and status == 206:
        cr = resp.headers.get("Content-Range") or ""
        m = re.search(r"bytes\s+(\d+)-", cr, re.IGNORECASE)
        if m and int(m.group(1)) != existing:
            resp.close()
            temp_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise RetryableDownloadError(
                f"Bad resume offset from server: {cr}"
            )

    ct = content_type(resp.headers)

    if ct in {"text/html", "application/xhtml+xml"}:
        resp.close()
        raise PermanentDownloadError(
            "Server returned HTML, not a file. "
            "This is usually a CAPTCHA page, expired key, or blocked request."
        )

    total_size = parse_total_size(resp.headers, existing, status)

    final_name = (
        filename_from_headers(resp.headers)
        or (meta_path.read_text().strip() if meta_path.exists() else None)
        or filename_from_url(resp.geturl(), uid, ct)
    )

    final_name = sanitize_filename(final_name)
    final_path = output_dir / final_name

    meta_path.write_text(final_name, encoding="utf-8")

    tslog(f"HTTP status: {status}")
    tslog(f"Content-Type: {ct or '(unknown)'}")
    tslog(f"Filename: {final_name}")
    tslog(f"Total size: {format_size(total_size) if total_size else 'unknown'}")

    if total_size and total_size > MAX_FILE_MB_WARNING * 1024 * 1024:
        tslog(f"WARNING: file is larger than {MAX_FILE_MB_WARNING} MB")

    bytes_done = existing
    first_chunk = True
    last_log = time.monotonic()

    mode = "ab" if existing else "wb"

    try:
        with temp_path.open(mode) as f:
            while True:
                try:
                    chunk = resp.read(CHUNK_SIZE)
                except (
                    socket.timeout,
                    TimeoutError,
                    http.client.IncompleteRead,
                    OSError,
                ) as e:
                    raise RetryableDownloadError(f"Read failed: {e}") from e

                if not chunk:
                    break

                if first_chunk:
                    first_chunk = False

                    if looks_like_html(chunk):
                        raise PermanentDownloadError(
                            "Server returned HTML/CAPTCHA/error content instead of file bytes."
                        )

                    tslog(f"First bytes: {chunk[:16].hex()}")

                f.write(chunk)
                bytes_done += len(chunk)

                now = time.monotonic()
                if now - last_log >= 15:
                    if total_size:
                        pct = bytes_done / total_size * 100
                        tslog(
                            f"Progress: {format_size(bytes_done)} / "
                            f"{format_size(total_size)} ({pct:.1f}%)"
                        )
                    else:
                        tslog(f"Progress: {format_size(bytes_done)}")
                    last_log = now

    finally:
        resp.close()

    actual_size = temp_path.stat().st_size

    if total_size and actual_size < total_size:
        raise RetryableDownloadError(
            f"Incomplete download: {format_size(actual_size)} of {format_size(total_size)}"
        )

    if expected_md5:
        tslog("Verifying MD5...")
        actual_md5 = md5_file(temp_path)

        if actual_md5 != expected_md5.lower():
            temp_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise PermanentDownloadError(
                f"MD5 mismatch. Expected {expected_md5.lower()}, got {actual_md5}"
            )

        tslog(f"MD5 OK: {actual_md5}")

    if final_path.exists():
        final_path.unlink()

    temp_path.rename(final_path)
    meta_path.unlink(missing_ok=True)

    return final_path


def download_with_retries(
    url: str,
    output_dir: Path,
    expected_md5: Optional[str],
) -> Path:
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        log("")
        log("━" * 60)
        tslog(f"Attempt {attempt}/{MAX_RETRIES}")

        try:
            return download_once(url, output_dir, expected_md5)

        except PermanentDownloadError:
            raise

        except RetryableDownloadError as e:
            last_error = e
            tslog(f"Retryable error: {e}")

        if attempt < MAX_RETRIES:
            wait = min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
            wait *= random.uniform(0.8, 1.2)
            tslog(f"Waiting {wait:.1f}s before retry...")
            time.sleep(wait)

    raise RetryableDownloadError(
        f"All {MAX_RETRIES} attempts failed. Last error: {last_error}"
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    book_url = os.environ.get("BOOK_URL", "").strip()
    expected_md5_raw = os.environ.get("EXPECTED_MD5", "").strip().lower()
    output_dir = Path(os.environ.get("OUTPUT_DIR", "downloads").strip())

    if not book_url:
        print("BOOK_URL is required.", file=sys.stderr)
        return 2

    expected_md5: Optional[str] = None

    if expected_md5_raw:
        if not is_md5(expected_md5_raw):
            print(
                f"Invalid EXPECTED_MD5: {expected_md5_raw}",
                file=sys.stderr,
            )
            return 2
        expected_md5 = expected_md5_raw

    log("=" * 60)
    log(f"Input URL : {book_url}")
    log(f"MD5       : {expected_md5 or '(none)'}")
    log(f"Output dir: {output_dir.resolve()}")
    log("=" * 60)

    try:
        validate_direct_url(book_url)
        final_path = download_with_retries(book_url, output_dir, expected_md5)

    except PermanentDownloadError as e:
        print("")
        print(f"FAILED: {e}", file=sys.stderr)
        print("")
        print(
            "This was a permanent failure. Retrying will not help unless you provide "
            "a fresh direct file URL that returns file bytes instead of HTML.",
            file=sys.stderr,
        )
        return 2

    except RetryableDownloadError as e:
        print("")
        print(f"FAILED: {e}", file=sys.stderr)
        return 1

    final_size = final_path.stat().st_size
    final_md5 = md5_file(final_path)

    log("")
    log("=" * 60)
    log("SUCCESS")
    log(f"File: {final_path}")
    log(f"Size: {format_size(final_size)}")
    log(f"MD5 : {final_md5}")
    log("=" * 60)

    write_github_output(final_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)
